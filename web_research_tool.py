"""
title: Web Research Studio
description: Search the web and crawl pages using SearXNG and Crawl4AI. Returns compact summaries with page_ids; call read_page for full content.
author: lexiismadd (modified)
author_url: https://github.com/lexiismadd
funding_url: https://github.com/open-webui
version: 3.1.0
license: MIT
requirements: aiohttp, loguru, crawl4ai, markitdown, redis>=5.0

Two-tool architecture:
  search_and_crawl(query, depth) -> compact summaries + page_ids (token-cheap discovery)
  read_page(page_id, focus?)     -> full page content or focused sections (depth on demand)

Full content is stored in an in-process PageStore and cached in Valkey/Redis.
BM25 scoring is used for summary generation and focused reads, not for filtering stored content.
Supports HTML pages (via Crawl4AI) and documents (PDF/DOCX/etc via MarkItDown).
"""

import asyncio
import hashlib
import json
import re
import time
import traceback
from typing import Any, Awaitable, Callable, List, Literal, Optional, Union
from urllib.parse import parse_qs, urlparse

import aiohttp
from crawl4ai import (
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    DefaultMarkdownGenerator,
    DefaultTableExtraction,
)
from crawl4ai.content_filter_strategy import PruningContentFilter
from loguru import logger
from pydantic import BaseModel, Field

try:
    from markitdown import MarkItDown

    _MD_CONVERTER = MarkItDown(enable_plugins=False)
    MARKITDOWN_AVAILABLE = True
except ImportError:
    _MD_CONVERTER = None
    MARKITDOWN_AVAILABLE = False

_TTL_RULES = [
    ("wikipedia.org", 86400),
    ("arxiv.org", 86400),
    ("docs.python.org", 21600),
    ("docs.docker.com", 21600),
    ("developer.mozilla.org", 21600),
    ("github.com", 7200),
    ("stackoverflow.com", 7200),
    ("news.", 600),
    ("bbc.", 600),
    ("cnn.", 600),
    ("reuters.", 600),
]
_DEFAULT_PAGE_TTL = 1800

EventEmitter = Optional[Callable[[dict[str, Any]], Awaitable[Any]]]


def _ttl_for_url(url: str) -> int:
    host = urlparse(url).netloc.lower()
    for pattern, ttl in _TTL_RULES:
        if pattern in host:
            return ttl
    return _DEFAULT_PAGE_TTL


def _negative_ttl(error_type: str) -> int:
    ttls = {
        "timeout": 300,
        "404": 1800,
        "blocked": 3600,
        "403": 3600,
        "500": 600,
    }
    return ttls.get(error_type, 600)


class _CacheClient:
    def __init__(self, url: str, enabled: bool = True):
        self.url = url
        self.enabled = enabled
        self._redis = None
        self._unavailable_until = 0.0

    async def _client(self):
        if not self.enabled or time.monotonic() < self._unavailable_until:
            return None
        if self._redis is None:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(
                    self.url,
                    socket_timeout=0.5,
                    socket_connect_timeout=0.5,
                    decode_responses=True,
                )
                ping_result = self._redis.ping()
                if not isinstance(ping_result, bool):
                    await ping_result
            except Exception as exc:
                logger.warning(f"Cache unavailable: {exc}")
                self._unavailable_until = time.monotonic() + 30
                self._redis = None
                return None
        return self._redis

    async def get(self, key: str) -> Optional[str]:
        client = await self._client()
        if client is None:
            return None
        try:
            return await asyncio.wait_for(client.get(key), timeout=0.2)
        except Exception:
            return None

    async def setex(self, key: str, ttl_s: int, value: str):
        client = await self._client()
        if client is None:
            return
        try:
            await asyncio.wait_for(client.setex(key, ttl_s, value), timeout=0.2)
        except Exception:
            pass

    async def exists(self, key: str) -> bool:
        client = await self._client()
        if client is None:
            return False
        try:
            return bool(await asyncio.wait_for(client.exists(key), timeout=0.2))
        except Exception:
            return False


class _PageStore:
    def __init__(self, max_pages=200, ttl_s=1800):
        import collections

        self._store = collections.OrderedDict()
        self._url_to_id = {}
        self.max_pages = max_pages
        self.ttl = ttl_s

    def put(self, url: str, title: str, full_markdown: str) -> str:
        canonical = Tools._canonicalize_url(url)
        existing_id = self._url_to_id.get(canonical)
        if existing_id and existing_id in self._store:
            return existing_id

        page_id = hashlib.md5(canonical.encode()).hexdigest()[:10]
        record = {"url": url, "title": title, "content": full_markdown}
        while len(self._store) >= self.max_pages:
            _, (_, evicted_record) = self._store.popitem(last=False)
            evicted_canonical = Tools._canonicalize_url(evicted_record["url"])
            self._url_to_id.pop(evicted_canonical, None)
        self._store[page_id] = (time.monotonic(), record)
        self._url_to_id[canonical] = page_id
        return page_id

    def get(self, page_id: str) -> Optional[dict]:
        entry = self._store.get(page_id)
        if not entry:
            return None
        ts, record = entry
        if time.monotonic() - ts > self.ttl:
            self._store.pop(page_id, None)
            return None
        self._store.move_to_end(page_id)
        return record


class Tools:
    class Valves(BaseModel):
        INITIAL_RESPONSE: str = Field(default="")
        SEARCH_WITH_SEARXNG: bool = Field(default=True)
        SEARXNG_BASE_URL: str = Field(
            default="http://searxng:8080/search?format=json&q=<query>"
        )
        SEARXNG_API_TOKEN: str = Field(default="")
        SEARXNG_METHOD: Literal["GET", "POST"] = Field(default="GET")
        SEARXNG_TIMEOUT: int = Field(default=30)
        SEARXNG_MAX_RESULTS: int = Field(default=10)
        CRAWL4AI_BASE_URL: str = Field(default="http://crawl4ai:11235")
        CRAWL4AI_USER_AGENT: str = Field(
            default="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.1.2.3 Safari/537.36"
        )
        CRAWL4AI_TIMEOUT: int = Field(default=60)
        CRAWL4AI_BATCH: int = Field(default=5)
        CRAWL4AI_MAX_URLS: int = Field(default=20)
        CRAWL4AI_EXTERNAL_DOMAINS: bool = Field(default=False)
        CRAWL4AI_EXCLUDE_DOMAINS: str = Field(default="")
        CRAWL4AI_EXCLUDE_SOCIAL_MEDIA_DOMAINS: str = Field(
            default="facebook.com,twitter.com,x.com,linkedin.com,instagram.com,pinterest.com,tiktok.com,snapchat.com,reddit.com"
        )
        CRAWL4AI_EXCLUDE_IMAGES: Literal["None", "External", "All"] = Field(
            default="None"
        )
        CRAWL4AI_WORD_COUNT_THRESHOLD: int = Field(default=200)
        CRAWL4AI_TEXT_ONLY: bool = Field(default=False)
        CRAWL4AI_DISPLAY_IMAGES: bool = Field(default=True)
        CRAWL4AI_MAX_IMAGES: int = Field(default=5)
        CRAWL4AI_DISPLAY_THUMBNAILS: bool = Field(default=False)
        CRAWL4AI_THUMBNAIL_SIZE: int = Field(default=200)
        CRAWL4AI_VALIDATE_IMAGES: bool = Field(default=True)
        CRAWL4AI_MAX_TOKENS: int = Field(default=0)
        BM25_THRESHOLD: float = Field(default=1.0)
        ENABLE_DOCUMENT_CONVERSION: bool = Field(default=True)
        MAX_DOCUMENT_SIZE_MB: int = Field(default=20)
        DOCUMENT_FETCH_TIMEOUT: int = Field(default=20)
        DEADLINE_SECONDS: int = Field(default=60)
        CACHE_ENABLED: bool = Field(default=True)
        CACHE_REDIS_URL: str = Field(default="redis://redis:6379/2")
        MORE_STATUS: bool = Field(default=False)
        DEBUG: bool = Field(default=False)

    class UserValves(BaseModel):
        SEARXNG_MAX_RESULTS: Optional[int] = Field(default=None)
        CRAWL4AI_MAX_URLS: Optional[int] = Field(default=None)
        CRAWL4AI_DISPLAY_IMAGES: Optional[bool] = Field(default=None)
        CRAWL4AI_MAX_IMAGES: Optional[int] = Field(default=None)
        CRAWL4AI_DISPLAY_THUMBNAILS: Optional[bool] = Field(default=None)
        CRAWL4AI_THUMBNAIL_SIZE: Optional[int] = Field(default=None)

    def __init__(self):
        self.valves = self.Valves()
        self.user_valves = self.UserValves()
        if self.valves.SEARCH_WITH_SEARXNG and self.valves.SEARXNG_BASE_URL:
            searxng_parsed_url = urlparse(self.valves.SEARXNG_BASE_URL)
            searxng_parsed_url_query = parse_qs(searxng_parsed_url.query)
            if "q" not in searxng_parsed_url_query:
                searxng_parsed_url_query["q"] = ["<query>"]
            if "format" in searxng_parsed_url_query:
                if searxng_parsed_url_query["format"][0] != "json":
                    searxng_parsed_url_query["format"][0] = "json"
            reconstructed_query = "&".join(
                [f"{key}={value[0]}" for key, value in searxng_parsed_url_query.items()]
            )
            self.valves.SEARXNG_BASE_URL = (
                f"{searxng_parsed_url.scheme}://{searxng_parsed_url.netloc}"
                f"{searxng_parsed_url.path}?{reconstructed_query}"
            )

        self._depth_budgets = {
            "quick": {"max_urls": 3, "search_timeout": 5, "deadline_s": 15},
            "normal": {"max_urls": 10, "search_timeout": 6, "deadline_s": 30},
            "deep": {"max_urls": 25, "search_timeout": 8, "deadline_s": 55},
        }
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_and_crawl",
                    "description": (
                        "Search the web and crawl pages. Returns compact summaries with page_ids. "
                        "If a summary isn't enough, call read_page(page_id) for full content."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search query",
                            },
                            "urls": {
                                "type": "array",
                                "description": "Optional list of specific URLs to crawl in addition to search results",
                                "items": {"type": "string"},
                                "default": [],
                            },
                            "depth": {
                                "type": "string",
                                "enum": ["quick", "normal", "deep"],
                                "default": "normal",
                            },
                            "max_results": {"type": "integer", "default": None},
                            "fresh": {"type": "boolean", "default": False},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_page",
                    "description": "Get the full cleaned content of a page from a prior search_and_crawl result.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "page_id": {"type": "string"},
                            "focus": {"type": "string", "default": ""},
                            "max_chars": {"type": "integer", "default": 8000},
                        },
                        "required": ["page_id"],
                    },
                },
            },
        ]
        self.crawl_counter = 0
        self.content_counter = 0
        logger.info("Web Research Studio tool initialized")
        self.total_urls = 0
        self._page_store = _PageStore(max_pages=200, ttl_s=1800)
        self._cache = _CacheClient(
            url=self.valves.CACHE_REDIS_URL,
            enabled=self.valves.CACHE_ENABLED,
        )
        self._cache_stats = {
            "page_hits": 0,
            "page_misses": 0,
            "search_hits": 0,
            "search_misses": 0,
            "negative_skips": 0,
        }

    def _bm25_extract_sections(
        self, content: str, query: str, max_chars: int = 4000
    ) -> str:
        if not content or not query:
            return content[:max_chars] if content else ""

        paragraphs = re.split(r"\n\n+|\n(?=#)", content)
        paragraphs = [
            p.strip() for p in paragraphs if p.strip() and len(p.strip()) > 20
        ]
        if not paragraphs:
            return content[:max_chars]

        query_terms = set(query.lower().split())
        scored = []
        for idx, para in enumerate(paragraphs):
            para_lower = para.lower()
            score = sum(
                para_lower.count(term) * (1.0 / max(len(para_lower.split()), 1))
                for term in query_terms
            )
            if para.startswith("#"):
                score *= 1.5
            scored.append((score, idx, para))

        scored.sort(key=lambda item: item[0], reverse=True)
        selected = []
        total_chars = 0
        for score, idx, para in scored:
            if score <= 0 and selected:
                break
            if total_chars + len(para) > max_chars:
                if not selected:
                    selected.append((idx, para[:max_chars]))
                break
            selected.append((idx, para))
            total_chars += len(para)

        if not selected:
            return content[:max_chars]

        selected.sort(key=lambda item: item[0])
        return "\n\n".join(para for _, para in selected)

    def _build_compact_summary(
        self, content: str, query: str, max_points: int = 3, max_chars: int = 500
    ) -> dict:
        if not content:
            return {"summary": "", "key_points": []}

        paragraphs = [
            p.strip()
            for p in content.split("\n\n")
            if p.strip() and len(p.strip()) > 30
        ]
        summary = paragraphs[0][:200] + "..." if paragraphs else content[:200] + "..."

        if query:
            relevant = self._bm25_extract_sections(content, query, max_chars=max_chars)
            sentences = re.split(r"[.!?]\s+", relevant)
            query_terms = set(query.lower().split())
            scored_sentences = []
            for sentence in sentences:
                sentence = sentence.strip()
                if len(sentence) < 20:
                    continue
                score = sum(1 for term in query_terms if term in sentence.lower())
                if score > 0:
                    scored_sentences.append((score, sentence))
            scored_sentences.sort(key=lambda item: item[0], reverse=True)
            key_points = [
                sentence[:150] for _, sentence in scored_sentences[:max_points]
            ]
        else:
            key_points = (
                [p[:150] for p in paragraphs[1 : max_points + 1]]
                if len(paragraphs) > 1
                else []
            )

        return {"summary": summary, "key_points": key_points}

    async def _validate_image_url(self, url: str) -> bool:
        try:
            if not self.valves.CRAWL4AI_VALIDATE_IMAGES:
                return True

            timeout = aiohttp.ClientTimeout(total=4)
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
                skip_auto_headers={"Accept-Encoding", "Content-Type"},
            ) as session:
                async with session.head(url.strip(), allow_redirects=True) as response:
                    if response.status != 200:
                        return False
                    return (
                        response.headers.get("Content-Type", "")
                        .lower()
                        .startswith("image/")
                    )
        except Exception:
            return False

    async def _validate_images_batch(self, urls: List[str]) -> List[str]:
        results = await asyncio.gather(*[self._validate_image_url(url) for url in urls])
        return [url for url, is_valid in zip(urls, results) if is_valid]

    async def _search_searxng(
        self, query: str, __event_emitter__: EventEmitter = None
    ) -> List[str]:
        if not self.valves.SEARCH_WITH_SEARXNG and self.valves.DEBUG:
            logger.info("SearXNG search is disabled.")
            return []
        if not self.valves.SEARXNG_BASE_URL:
            return []

        url = self.valves.SEARXNG_BASE_URL.replace("<query>", query)
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if self.valves.SEARXNG_API_TOKEN:
            headers["Authorization"] = f"Bearer {self.valves.SEARXNG_API_TOKEN}"

        try:
            timeout = aiohttp.ClientTimeout(total=self.valves.SEARXNG_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if self.valves.SEARXNG_METHOD == "POST":
                    async with session.post(
                        url, data={"q": query, "format": "json"}, headers=headers
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                else:
                    async with session.get(url, headers=headers) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
            results = data.get("results", [])
            max_results = (
                self.user_valves.SEARXNG_MAX_RESULTS or self.valves.SEARXNG_MAX_RESULTS
            )
            return [
                result["url"] for result in results[:max_results] if result.get("url")
            ]
        except Exception as exc:
            logger.error(f"Error searching SearXNG: {exc}")
            return []

    @staticmethod
    def _canonicalize_url(url: str) -> str:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        tracking_params = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "gclid",
            "fbclid",
            "mc_cid",
            "mc_eid",
            "ref",
            "ref_src",
            "ref_cta",
            "ref_loc",
            "ref_page",
            "source",
            "medium",
        }
        parts = urlsplit(url)
        query = urlencode(
            [
                (k, v)
                for k, v in parse_qsl(parts.query)
                if k.lower() not in tracking_params
            ]
        )
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))

    @staticmethod
    def _page_cache_key(url: str) -> str:
        return (
            f"sc:page:{hashlib.md5(Tools._canonicalize_url(url).encode()).hexdigest()}"
        )

    @staticmethod
    def _page_id_cache_key(page_id: str) -> str:
        return f"sc:pageid:{page_id}"

    async def _store_page_record(self, url: str, title: str, content: str) -> str:
        page_id = self._page_store.put(url, title, content)
        cache_record = json.dumps({"url": url, "title": title, "content": content})
        ttl = _ttl_for_url(url)
        await self._cache.setex(self._page_cache_key(url), ttl, cache_record)
        await self._cache.setex(self._page_id_cache_key(page_id), ttl, cache_record)
        return page_id

    def _search_cache_key(self, query: str) -> str:
        providers = []
        if self.valves.SEARCH_WITH_SEARXNG:
            providers.append("searxng")
        provider_str = "+".join(sorted(providers)) or "none"
        normalized = re.sub(r"\s+", " ", query.strip().lower())
        tokens = normalized.split()
        if len(tokens) >= 3:
            tokens = sorted(tokens)
        raw = " ".join(tokens) + "|" + provider_str
        return f"sc:search:{hashlib.md5(raw.encode()).hexdigest()}"

    @staticmethod
    def _dead_cache_key(url: str) -> str:
        return (
            f"sc:dead:{hashlib.md5(Tools._canonicalize_url(url).encode()).hexdigest()}"
        )

    @staticmethod
    def _classify_url(url: str) -> str:
        skip_extensions = (
            ".mp4",
            ".mp3",
            ".wav",
            ".avi",
            ".mkv",
            ".mov",
            ".exe",
            ".dmg",
            ".iso",
            ".msi",
            ".deb",
            ".rpm",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".svg",
            ".webp",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
            ".zip",
            ".tar",
            ".gz",
            ".bz2",
            ".xz",
            ".7z",
        )
        doc_extensions = (
            ".pdf",
            ".docx",
            ".doc",
            ".pptx",
            ".ppt",
            ".xlsx",
            ".xls",
            ".epub",
            ".odt",
            ".rtf",
        )
        path = urlparse(url).path.lower().split("?")[0]
        if path.endswith(skip_extensions):
            return "skip"
        if path.endswith(doc_extensions):
            return "document"
        return "html"

    @staticmethod
    def _dedupe_and_diversify(urls: List[str], max_per_domain: int = 3) -> List[str]:
        seen_canonical = set()
        domain_counts = {}
        result = []
        for url in urls:
            canonical = Tools._canonicalize_url(url)
            if canonical in seen_canonical:
                continue
            seen_canonical.add(canonical)
            host = urlparse(url).netloc.lower().lstrip("www.")
            if domain_counts.get(host, 0) >= max_per_domain:
                continue
            domain_counts[host] = domain_counts.get(host, 0) + 1
            result.append(url)
        return result

    async def read_page(
        self,
        page_id: str,
        focus: str = "",
        max_chars: int = 8000,
        __event_emitter__: EventEmitter = None,
    ) -> dict:
        record = self._page_store.get(page_id)
        if not record:
            cached_page = await self._cache.get(self._page_id_cache_key(page_id))
            if cached_page:
                try:
                    cached_record = json.loads(cached_page)
                    cached_page_id = self._page_store.put(
                        cached_record["url"],
                        cached_record.get("title", ""),
                        cached_record["content"],
                    )
                    if cached_page_id == page_id:
                        record = self._page_store.get(page_id)
                except Exception:
                    record = None
        if not record:
            return {
                "error": f"page_id '{page_id}' not found or expired. Call search_and_crawl again."
            }

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"Reading: {record['title'][:60]}...",
                        "done": False,
                    },
                }
            )

        content = record["content"]
        content = (
            self._bm25_extract_sections(content, focus, max_chars=max_chars)
            if focus
            else content[:max_chars]
        )

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "citation",
                    "data": {
                        "document": [content[:500]],
                        "metadata": [{"source": record["url"]}],
                        "source": {"name": record["title"] or record["url"]},
                    },
                }
            )
        return {"url": record["url"], "title": record["title"], "content": content}

    async def search_and_crawl(
        self,
        query: str,
        urls: Optional[List[str]] = None,
        depth: str = "normal",
        max_results: Optional[int] = None,
        fresh: bool = False,
        __event_emitter__: EventEmitter = None,
        __user__: Optional[dict] = None,
    ) -> Union[list, str]:
        logger.info(f"Starting search and crawl for '{query}' (depth={depth})")
        self._cache_stats = {
            "page_hits": 0,
            "page_misses": 0,
            "search_hits": 0,
            "search_misses": 0,
            "negative_skips": 0,
        }

        budget = self._depth_budgets.get(depth, self._depth_budgets["normal"]).copy()
        deadline = time.monotonic() + min(
            budget["deadline_s"], self.valves.DEADLINE_SECONDS
        )

        def time_left():
            return max(0.0, deadline - time.monotonic())

        effective_max_results = min(
            max_results or budget["max_urls"], budget["max_urls"]
        )
        gathered_urls = []
        self.crawl_counter = 0
        self.content_counter = 0
        self.total_urls = 0

        if urls:
            for url in urls:
                if not url.startswith("http"):
                    url = f"https://{url}"
                if url not in gathered_urls:
                    gathered_urls.append(url)

        if __event_emitter__ and str(self.valves.INITIAL_RESPONSE).strip() != "":
            await __event_emitter__(
                {
                    "type": "chat:message:delta",
                    "data": {"content": str(self.valves.INITIAL_RESPONSE).strip()},
                }
            )

        search_cache_key = self._search_cache_key(query)
        cached_search = None if fresh else await self._cache.get(search_cache_key)
        if cached_search:
            cached_urls = json.loads(cached_search)
            for url in cached_urls[:effective_max_results]:
                if url not in gathered_urls:
                    gathered_urls.append(url)
            self._cache_stats["search_hits"] += 1
        else:
            self._cache_stats["search_misses"] += 1
            all_search_urls = []
            search_tasks = []
            if self.valves.SEARCH_WITH_SEARXNG:
                search_tasks.append(self._search_searxng(query, __event_emitter__))
            if search_tasks:
                try:
                    search_results = await asyncio.wait_for(
                        asyncio.gather(*search_tasks, return_exceptions=True),
                        timeout=min(budget["search_timeout"], time_left()),
                    )
                    for result in search_results:
                        if isinstance(result, list):
                            for url in result:
                                if url not in all_search_urls:
                                    all_search_urls.append(url)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Search providers timed out, proceeding with what we have"
                    )
            if all_search_urls:
                await self._cache.setex(
                    search_cache_key, 600, json.dumps(all_search_urls)
                )
            for url in all_search_urls[:effective_max_results]:
                if url not in gathered_urls:
                    gathered_urls.append(url)

        all_processable = [
            url for url in gathered_urls if self._classify_url(url) != "skip"
        ]
        max_per_domain = 2 if depth in ("quick", "normal") else 3
        all_processable = self._dedupe_and_diversify(
            all_processable, max_per_domain=max_per_domain
        )
        all_processable = all_processable[:effective_max_results]
        if not all_processable:
            return f"No URLs found to crawl for the query: {query}."

        crawl_results = []
        urls_to_fetch = []
        for url in all_processable:
            if fresh:
                urls_to_fetch.append(url)
                continue
            if await self._cache.exists(self._dead_cache_key(url)):
                self._cache_stats["negative_skips"] += 1
                continue
            cached_page = await self._cache.get(self._page_cache_key(url))
            if cached_page:
                self._cache_stats["page_hits"] += 1
                try:
                    record = json.loads(cached_page)
                    page_id = await self._store_page_record(
                        record["url"], record.get("title", ""), record["content"]
                    )
                    compact = self._build_compact_summary(record["content"], query)
                    crawl_results.append(
                        {
                            "url": record["url"],
                            "title": record["title"],
                            "page_id": page_id,
                            "summary": compact["summary"],
                            "key_points": compact["key_points"],
                            "content_length": len(record["content"]),
                            "images": [],
                            "from_cache": True,
                        }
                    )
                    continue
                except Exception:
                    pass
            self._cache_stats["page_misses"] += 1
            urls_to_fetch.append(url)

        html_urls = []
        document_urls = []
        for url in urls_to_fetch:
            if self._classify_url(url) == "document":
                document_urls.append(url)
            else:
                html_urls.append(url)

        for i in range(0, len(html_urls), self.valves.CRAWL4AI_BATCH):
            remaining = time_left()
            if remaining < 2.0:
                break
            batch = html_urls[i : i + self.valves.CRAWL4AI_BATCH]
            try:
                crawled_batch = await self._crawl_url(
                    urls=batch,
                    query=query,
                    timeout_s=min(
                        remaining - 1, self.valves.CRAWL4AI_TIMEOUT * len(batch)
                    ),
                    __event_emitter__=__event_emitter__,
                )
                crawl_results.extend(crawled_batch.get("content", []))
            except Exception as exc:
                logger.error(f"Batch crawl error: {exc}\n{traceback.format_exc()}")

        if document_urls and depth != "quick" and time_left() > 5:
            doc_concurrency = 2 if depth == "normal" else 4
            max_docs = max(effective_max_results // 2, 2)
            doc_batch = document_urls[:max_docs]
            remaining_for_docs = time_left() - 2
            sem = asyncio.Semaphore(doc_concurrency)

            async def bounded_fetch(url: str):
                async with sem:
                    return await self._fetch_document(
                        url,
                        query=query,
                        timeout_s=min(
                            remaining_for_docs, self.valves.DOCUMENT_FETCH_TIMEOUT
                        ),
                        __event_emitter__=__event_emitter__,
                    )

            doc_tasks = [bounded_fetch(url) for url in doc_batch]
            try:
                doc_results = await asyncio.wait_for(
                    asyncio.gather(*doc_tasks, return_exceptions=True),
                    timeout=remaining_for_docs,
                )
                for result in doc_results:
                    if isinstance(result, dict) and "page_id" in result:
                        crawl_results.append(result)
            except asyncio.TimeoutError:
                logger.warning(
                    "Document conversion timed out, continuing with HTML results"
                )

        return crawl_results

    async def _fetch_document(
        self,
        url: str,
        query: str = "",
        timeout_s: Optional[float] = None,
        __event_emitter__: EventEmitter = None,
    ) -> Optional[dict]:
        if not self.valves.ENABLE_DOCUMENT_CONVERSION or not MARKITDOWN_AVAILABLE:
            return None

        converter = _MD_CONVERTER
        if converter is None:
            return None

        max_size = self.valves.MAX_DOCUMENT_SIZE_MB * 1024 * 1024
        effective_timeout = timeout_s or self.valves.DOCUMENT_FETCH_TIMEOUT
        timeout = aiohttp.ClientTimeout(total=effective_timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        return None
                    cl = resp.headers.get("Content-Length")
                    if cl and int(cl) > max_size:
                        return None
                    chunks = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > max_size:
                            return None
                        chunks.append(chunk)
                    raw_bytes = b"".join(chunks)

            import io

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: converter.convert_stream(io.BytesIO(raw_bytes), url=url),
            )
            title = getattr(result, "title", None) or url.rsplit("/", 1)[-1]
            content = getattr(result, "text_content", None) or ""
            if not content.strip():
                return None

            page_id = await self._store_page_record(url, title, content)
            compact = self._build_compact_summary(content, query)
            return {
                "url": url,
                "title": title,
                "page_id": page_id,
                "summary": compact["summary"],
                "key_points": compact["key_points"],
                "content_length": len(content),
                "images": [],
                "source_type": "document",
            }
        except Exception as exc:
            logger.warning(f"Document conversion failed for {url}: {exc}")
            return None

    async def _crawl_url(
        self,
        urls: Union[list, str],
        query: Optional[str] = None,
        timeout_s: Optional[float] = None,
        __event_emitter__: EventEmitter = None,
    ) -> dict:
        if isinstance(urls, str):
            urls = [urls]

        for idx, url in enumerate(urls):
            if not url.startswith("http"):
                urls[idx] = f"https://{url}"

        endpoint = f"{self.valves.CRAWL4AI_BASE_URL}/crawl"
        browser_config = BrowserConfig(
            headless=True,
            light_mode=True,
            headers={
                "sec-ch-ua": '"Chromium";v="116", "Not_A Brand";v="8", "Google Chrome";v="116"'
            },
            extra_args=["--no-sandbox", "--disable-gpu"],
        )
        md_generator = DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(),
            options={"ignore_links": True, "escape_html": False, "body_width": 80},
        )
        crawler_config = CrawlerRunConfig(
            markdown_generator=md_generator,
            table_extraction=DefaultTableExtraction(),
            exclude_external_links=not self.valves.CRAWL4AI_EXTERNAL_DOMAINS,
            exclude_social_media_domains=[
                domain.strip()
                for domain in self.valves.CRAWL4AI_EXCLUDE_SOCIAL_MEDIA_DOMAINS.split(
                    ","
                )
                if domain.strip()
            ],
            exclude_domains=[
                domain.strip()
                for domain in self.valves.CRAWL4AI_EXCLUDE_DOMAINS.split(",")
                if domain.strip()
            ],
            user_agent=self.valves.CRAWL4AI_USER_AGENT,
            stream=False,
            cache_mode=CacheMode.BYPASS,
            page_timeout=self.valves.CRAWL4AI_TIMEOUT * 1000,
            only_text=self.valves.CRAWL4AI_TEXT_ONLY,
            word_count_threshold=self.valves.CRAWL4AI_WORD_COUNT_THRESHOLD,
            exclude_all_images=self.valves.CRAWL4AI_EXCLUDE_IMAGES == "All",
            exclude_external_images=self.valves.CRAWL4AI_EXCLUDE_IMAGES == "External",
        )
        payload = {
            "urls": urls,
            "browser_config": browser_config.dump(),
            "crawler_config": crawler_config.dump(),
        }

        try:
            effective_timeout = timeout_s or (
                self.valves.CRAWL4AI_TIMEOUT * len(urls) + 60
            )
            crawl_timeout = aiohttp.ClientTimeout(total=effective_timeout)
            async with aiohttp.ClientSession(timeout=crawl_timeout) as session:
                async with session.post(
                    endpoint, json=payload, headers={"Content-Type": "application/json"}
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            results = []
            sent_urls = set(urls)
            processed_urls = set()
            data_list = data.get("results", [])
            for item in data_list:
                item_url = item.get("url", "")
                if item_url:
                    processed_urls.add(item_url)
                if item.get("success") is not True:
                    if item_url:
                        status = item.get("status_code", 0)
                        err_type = (
                            "404"
                            if status == 404
                            else "403"
                            if status == 403
                            else "500"
                            if status >= 500
                            else "timeout"
                        )
                        await self._cache.setex(
                            self._dead_cache_key(item_url),
                            _negative_ttl(err_type),
                            json.dumps({"reason": err_type, "status": status}),
                        )
                    continue

                markdown_data = item.get("markdown", "")
                if isinstance(markdown_data, dict):
                    page_content = markdown_data.get(
                        "fit_markdown", ""
                    ) or markdown_data.get("raw_markdown", "")
                else:
                    page_content = str(markdown_data)
                title = item.get("metadata", {}).get("title", "")
                page_id = await self._store_page_record(item_url, title, page_content)
                compact = self._build_compact_summary(page_content, query or "")
                results.append(
                    {
                        "url": item_url,
                        "title": title,
                        "page_id": page_id,
                        "summary": compact["summary"],
                        "key_points": compact["key_points"],
                        "content_length": len(page_content),
                        "images": [],
                    }
                )

            for failed_url in sent_urls - processed_urls:
                await self._cache.setex(
                    self._dead_cache_key(failed_url),
                    _negative_ttl("timeout"),
                    json.dumps({"reason": "no_result"}),
                )
            return {"content": results, "images": []}
        except Exception as exc:
            logger.error(
                f"An unexpected error occurred: {exc}\n{traceback.format_exc()}"
            )
            return {"error": str(exc), "details": str(exc)}
