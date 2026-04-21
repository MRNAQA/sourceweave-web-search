"""
title: SourceWeave Web Search
description: Search-first web research tool with compact source discovery in search_web plus stable follow-up reads through read_pages and read_urls. Uses SearXNG for discovery, Crawl4AI for extraction, and MarkItDown for document conversion.
author: Mohammad ElNaqa
author_url: https://github.com/MRNAQA
version: 0.3.0
license: MIT
requirements: aiohttp, loguru, markitdown, redis>=5.0

Three-tool architecture:
    search_web(query, domains?, urls?) -> compact summaries + page_ids for source discovery
    read_pages(page_ids, focus?) -> cleaned page content for one or more stored pages
    read_urls(urls, focus?) -> cleaned page content for one or more direct URLs

Full content is cached in Valkey/Redis, which acts as the canonical page store.
SearXNG defines search ordering; Crawl4AI enriches results in place without reranking.
BM25-style extraction is used only for compact summaries and focused reads.
Supports HTML pages (via Crawl4AI) and automatically detected documents (PDF/DOCX/etc via MarkItDown).
"""

import asyncio
import hashlib
import json
import re
import time
import traceback
from typing import Any, Awaitable, Callable, List, Literal, Mapping, Optional, Union
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlparse

import aiohttp
from loguru import logger
from pydantic import BaseModel, Field

try:
    from markitdown import MarkItDown

    _MD_CONVERTER: MarkItDown | None = MarkItDown(enable_plugins=False)
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
_DEFAULT_PAGE_TTL = 86400
_PAGE_CACHE_VERSION = "v6"
_SEARXNG_HOST_FALLBACK = "http://searxng:8080/search?format=json&q=<query>"
_CRAWL4AI_HOST_FALLBACK = "http://crawl4ai:11235"
_REDIS_HOST_FALLBACK = "redis://redis:6379/2"
_PAGE_QUALITY_CHALLENGE_PATTERNS = (
    "prove your humanity",
    "verify you are human",
    "verify you are a human",
    "complete the challenge below",
    "complete the security check",
    "checking if the site connection is secure",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "press and hold",
)

_PAGE_QUALITY_BLOCKED_PATTERNS = (
    "access denied",
    "request blocked",
    "sorry, you have been blocked",
    "you have been blocked",
    "you don't have permission to access",
    "403 forbidden",
    "error code: 1020",
    "why have i been blocked",
)

_RELATED_LINK_TEXT_BLOCKLIST = {
    "",
    "skip to main content",
    "log in",
    "login",
    "sign in",
    "sign up",
    "register",
    "home",
    "expand navigation",
    "collapse navigation",
}

_RELATED_LINK_URL_SUBSTRINGS = (
    "js_challenge=",
    "cdn-cgi/challenge-platform",
    "/login",
    "/signin",
    "/sign-in",
    "/signup",
    "/sign-up",
    "/register",
    "/auth/",
    "captcha",
)

EventEmitter = Optional[Callable[[dict[str, Any]], Awaitable[Any]]]


def _ttl_for_url(url: str) -> int:
    host = urlparse(url).netloc.lower()
    for pattern, ttl in _TTL_RULES:
        if pattern in host:
            return ttl
    return _DEFAULT_PAGE_TTL


def _negative_ttl(error_type: str) -> int:
    ttls = {
        "timeout": 45,
        "404": 1800,
        "blocked": 300,
        "403": 180,
        "500": 90,
    }
    return ttls.get(error_type, 600)


def _browser_config_payload() -> dict[str, Any]:
    return {
        "type": "BrowserConfig",
        "params": {
            "headers": {
                "type": "dict",
                "value": {
                    "sec-ch-ua": '"Chromium";v="116", "Not_A Brand";v="8", "Google Chrome";v="116"'
                },
            },
            "light_mode": True,
            "extra_args": ["--no-sandbox", "--disable-gpu"],
        },
    }


def _crawl4ai_cache_mode_param(cache_mode: str) -> dict[str, Any] | None:
    normalized = str(cache_mode or "").strip().lower()
    if normalized in ("", "bypass"):
        return None
    if normalized not in {"enabled", "read_only", "write_only", "disabled"}:
        raise ValueError(f"Unsupported Crawl4AI cache mode: {cache_mode!r}")
    return {"type": "CacheMode", "params": normalized}


def _crawler_config_payload(
    tool: "Tools", *, cache_mode: str = "enabled"
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "word_count_threshold": tool.valves.CRAWL4AI_WORD_COUNT_THRESHOLD,
        "markdown_generator": {
            "type": "DefaultMarkdownGenerator",
            "params": {
                "content_filter": {
                    "type": "PruningContentFilter",
                    "params": {},
                },
                "options": {
                    "type": "dict",
                    "value": {
                        "ignore_links": False,
                        "escape_html": False,
                        "body_width": 80,
                    },
                },
            },
        },
        "scraping_strategy": {
            "type": "LXMLWebScrapingStrategy",
            "params": {},
        },
        "table_extraction": {
            "type": "DefaultTableExtraction",
            "params": {},
        },
        "user_agent": tool.valves.CRAWL4AI_USER_AGENT,
    }

    cache_mode_param = _crawl4ai_cache_mode_param(cache_mode)
    if cache_mode_param is not None:
        params["cache_mode"] = cache_mode_param

    exclude_social_media_domains = [
        domain.strip()
        for domain in tool.valves.CRAWL4AI_EXCLUDE_SOCIAL_MEDIA_DOMAINS.split(",")
        if domain.strip()
    ]
    if exclude_social_media_domains:
        params["exclude_social_media_domains"] = exclude_social_media_domains

    exclude_domains = [
        domain.strip()
        for domain in tool.valves.CRAWL4AI_EXCLUDE_DOMAINS.split(",")
        if domain.strip()
    ]
    if exclude_domains:
        params["exclude_domains"] = exclude_domains

    if tool.valves.CRAWL4AI_TEXT_ONLY:
        params["only_text"] = True
    if tool.valves.CRAWL4AI_TIMEOUT != 60:
        params["page_timeout"] = tool.valves.CRAWL4AI_TIMEOUT * 1000
    if tool.valves.CRAWL4AI_EXCLUDE_IMAGES == "All":
        params["exclude_all_images"] = True
    if tool.valves.CRAWL4AI_EXCLUDE_IMAGES == "External":
        params["exclude_external_images"] = True

    return {
        "type": "CrawlerRunConfig",
        "params": params,
    }


def _markdown_content_variants(
    markdown_data: Any, html_fallback: str = ""
) -> tuple[str, str]:
    fallback_content = str(html_fallback or "")
    if isinstance(markdown_data, dict):
        fit_markdown = str(markdown_data.get("fit_markdown", "") or "")
        raw_markdown = str(markdown_data.get("raw_markdown", "") or "")
        page_content = fit_markdown or raw_markdown or fallback_content
        summary_content = fit_markdown or raw_markdown or fallback_content
        return page_content, summary_content

    content = str(markdown_data or "") or fallback_content
    return content, content


class _CacheClient:
    _OP_TIMEOUT_S = 1.0

    def __init__(self, url: str, enabled: bool = True):
        self.url = url
        self.enabled = enabled
        self._redis: Any | None = None
        self._unavailable_until = 0.0

    def configure(self, *, url: str, enabled: bool) -> None:
        self.url = url
        self.enabled = enabled
        self._redis = None
        self._unavailable_until = 0.0

    async def _client(self):
        if not self.enabled or time.monotonic() < self._unavailable_until:
            return None
        if self._redis is None:
            last_exc = None
            try:
                import redis.asyncio as aioredis
            except Exception as exc:
                logger.warning(f"Cache unavailable: {exc}")
                self._unavailable_until = time.monotonic() + 30
                self._redis = None
                return None

            for candidate_url in Tools._redis_url_variants(self.url):
                try:
                    redis_client = aioredis.from_url(
                        candidate_url,
                        socket_timeout=0.5,
                        socket_connect_timeout=0.5,
                        decode_responses=True,
                    )
                    ping_result = redis_client.ping()
                    if not isinstance(ping_result, bool):
                        await ping_result
                    self._redis = redis_client
                    self.url = candidate_url
                    return self._redis
                except Exception as exc:
                    last_exc = exc
                    self._redis = None

            logger.warning(f"Cache unavailable: {last_exc}")
            self._unavailable_until = time.monotonic() + 30
            return None
        return self._redis

    async def get(self, key: str) -> Optional[str]:
        client = await self._client()
        if client is None:
            return None
        try:
            return await asyncio.wait_for(client.get(key), timeout=self._OP_TIMEOUT_S)
        except Exception:
            return None

    async def setex(self, key: str, ttl_s: int, value: str):
        client = await self._client()
        if client is None:
            return
        try:
            await asyncio.wait_for(
                client.setex(key, ttl_s, value), timeout=self._OP_TIMEOUT_S
            )
        except Exception:
            pass

    async def delete(self, key: str) -> None:
        client = await self._client()
        if client is None:
            return
        try:
            await asyncio.wait_for(client.delete(key), timeout=self._OP_TIMEOUT_S)
        except Exception:
            pass

    async def exists(self, key: str) -> bool:
        client = await self._client()
        if client is None:
            return False
        try:
            return bool(
                await asyncio.wait_for(client.exists(key), timeout=self._OP_TIMEOUT_S)
            )
        except Exception:
            return False


class Tools:
    class Valves(BaseModel):
        INITIAL_RESPONSE: str = Field(default="")
        SEARCH_WITH_SEARXNG: bool = Field(default=True)
        SEARXNG_BASE_URL: str = Field(default=_SEARXNG_HOST_FALLBACK)
        SEARXNG_API_TOKEN: str = Field(default="")
        SEARXNG_METHOD: Literal["GET", "POST"] = Field(default="GET")
        SEARXNG_TIMEOUT: int = Field(default=30)
        SEARXNG_MAX_RESULTS: int = Field(default=10)
        CRAWL4AI_BASE_URL: str = Field(default=_CRAWL4AI_HOST_FALLBACK)
        CRAWL4AI_USER_AGENT: str = Field(
            default="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.1.2.3 Safari/537.36"
        )
        CRAWL4AI_TIMEOUT: int = Field(default=60)
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
        CRAWL4AI_MAX_TOKENS: int = Field(default=0)
        BM25_THRESHOLD: float = Field(default=1.0)
        ENABLE_DOCUMENT_CONVERSION: bool = Field(default=True)
        MAX_DOCUMENT_SIZE_MB: int = Field(default=20)
        DOCUMENT_FETCH_TIMEOUT: int = Field(default=20)
        DEADLINE_SECONDS: int = Field(default=60)
        CACHE_ENABLED: bool = Field(default=True)
        CACHE_REDIS_URL: str = Field(default=_REDIS_HOST_FALLBACK)
        MORE_STATUS: bool = Field(default=False)
        DEBUG: bool = Field(default=False)

    class UserValves(BaseModel):
        SEARXNG_MAX_RESULTS: Optional[int] = Field(default=None)

    @staticmethod
    def normalize_searxng_base_url(base_url: str) -> str:
        parsed_url = urlparse(base_url)
        normalized_query = []
        saw_query = False
        saw_format = False
        for key, value in parse_qsl(parsed_url.query, keep_blank_values=True):
            if key == "q":
                if not saw_query:
                    normalized_query.append((key, "<query>"))
                    saw_query = True
                continue
            if key == "format":
                if not saw_format:
                    normalized_query.append((key, "json"))
                    saw_format = True
                continue
            normalized_query.append((key, value))

        if not saw_format:
            normalized_query.append(("format", "json"))
        if not saw_query:
            normalized_query.append(("q", "<query>"))

        reconstructed_query = urlencode(normalized_query, doseq=True).replace(
            "%3Cquery%3E", "<query>"
        )
        return (
            f"{parsed_url.scheme}://{parsed_url.netloc}"
            f"{parsed_url.path}?{reconstructed_query}"
        )

    def __init__(self):
        self.valves = self.Valves()
        self.user_valves = self.UserValves()

        self._depth_budgets = {
            "quick": {
                "search_candidates": 12,
                "crawl_limit": 4,
                "return_limit": 4,
                "crawl_slack": 1,
                "search_timeout": 5,
                "deadline_s": 15,
            },
            "normal": {
                "search_candidates": 24,
                "crawl_limit": 10,
                "return_limit": 6,
                "crawl_slack": 3,
                "search_timeout": 6,
                "deadline_s": 30,
            },
            "deep": {
                "search_candidates": 50,
                "crawl_limit": 16,
                "return_limit": 10,
                "crawl_slack": 4,
                "search_timeout": 8,
                "deadline_s": 55,
            },
        }
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": (
                        "Search the web for relevant sources and return compact summaries with stable page_ids for follow-up reads. "
                        "Use this when you need source discovery before reading full pages. "
                        "Pass domains when you want to constrain results to specific hosts. "
                        "If you already know an important URL, pass it in urls so it is included in the same search pass."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "The search query. Prefer concise noun phrases over conversational filler, quote exact errors, error codes, "
                                    "or function names."
                                ),
                            },
                            "domains": {
                                "type": "array",
                                "description": "Optional domains to constrain results to, such as docs.python.org or developer.mozilla.org.",
                                "items": {"type": "string"},
                                "default": [],
                            },
                            "urls": {
                                "type": "array",
                                "description": "Optional specific URLs to include alongside discovered search results. Pass plain URL strings.",
                                "items": {"type": "string"},
                                "default": [],
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_pages",
                    "description": (
                        "Retrieve cleaned content for one or more stored pages using page_ids returned by search_web. "
                        "Batch related page_ids in one call when comparing multiple sources. "
                        "Use focus to extract the most relevant sections for a specific topic or question."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "page_ids": {
                                "type": "array",
                                "description": (
                                    "One or more page_ids returned by search_web. "
                                    "Batch related pages into a single call when you need to compare or synthesize multiple sources. Prefer this over repeated single-page fetches."
                                ),
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                            "focus": {
                                "type": "string",
                                "default": "",
                                "description": "Optional focus phrase used to extract the most relevant sections from stored page content. Use short topic phrases, exact errors, function names, or concepts.",
                            },
                        },
                        "required": ["page_ids"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_urls",
                    "description": (
                        "Retrieve cleaned content for one or more direct URLs without running search_web first. "
                        "Supported document URLs such as PDFs are converted automatically when detected. "
                        "Use focus to extract the most relevant sections for a specific topic or question."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "urls": {
                                "type": "array",
                                "description": "One or more direct URLs to read.",
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                            "focus": {
                                "type": "string",
                                "default": "",
                                "description": "Optional focus phrase used to extract the most relevant sections from page content. Use short topic phrases, exact errors, function names, or concepts.",
                            },
                        },
                        "required": ["urls"],
                    },
                },
            },
        ]
        self.crawl_counter = 0
        self.content_counter = 0
        logger.info("Web search tool initialized")
        self.total_urls = 0
        self._cache = _CacheClient(
            url=self.valves.CACHE_REDIS_URL,
            enabled=self.valves.CACHE_ENABLED,
        )
        self._sync_runtime_state()
        self._cache_stats = {
            "page_hits": 0,
            "page_misses": 0,
            "search_hits": 0,
            "search_misses": 0,
            "negative_skips": 0,
        }
        self._last_query_metadata: dict[str, Any] = {}
        self._active_query_metadata: dict[str, Any] | None = None

    @property
    def last_query_metadata(self) -> dict[str, Any]:
        return dict(self._last_query_metadata)

    def _sync_runtime_state(self) -> None:
        if self.valves.SEARXNG_BASE_URL:
            self.valves.SEARXNG_BASE_URL = self.normalize_searxng_base_url(
                self.valves.SEARXNG_BASE_URL
            )
        self._cache.configure(
            url=self.valves.CACHE_REDIS_URL,
            enabled=self.valves.CACHE_ENABLED,
        )

    def apply_valve_overrides(
        self, overrides: Mapping[str, Any] | None = None
    ) -> "Tools":
        has_updates = False
        for field_name, value in (overrides or {}).items():
            if value is None or not hasattr(self.valves, field_name):
                continue
            setattr(self.valves, field_name, value)
            has_updates = True

        if has_updates:
            self._sync_runtime_state()
        return self

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
        selected: list[tuple[int, str]] = []
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

    @staticmethod
    def _normalize_query(query: str) -> str:
        return re.sub(r"\s+", " ", (query or "").strip())

    @staticmethod
    def _site_filters_from_query(query: str) -> list[str]:
        filters: list[str] = []
        for raw_filter in re.findall(
            r"(?:^|\s)site:([^\s]+)", query or "", flags=re.IGNORECASE
        ):
            normalized = raw_filter.strip().strip("\"'()[]{}<>.,;")
            if not normalized:
                continue
            if "://" not in normalized:
                normalized = f"https://{normalized}"
            parsed = urlparse(normalized)
            host = (parsed.netloc or parsed.path).lower().lstrip("www.").rstrip("/")
            if host and host not in filters:
                filters.append(host)
        return filters

    @staticmethod
    def _url_matches_site_filters(url: str, site_filters: list[str]) -> bool:
        if not site_filters:
            return True
        host = urlparse(url).netloc.lower().lstrip("www.")
        return any(
            host == site_filter or host.endswith(f".{site_filter}")
            for site_filter in site_filters
        )

    @staticmethod
    def _http_url_variants(url: str) -> list[str]:
        variants = [url]
        parsed = urlparse(url)
        if parsed.netloc.lower() == "searxng:8080":
            fallback_url = _SEARXNG_HOST_FALLBACK
        elif parsed.netloc.lower() == "crawl4ai:11235":
            fallback_url = _CRAWL4AI_HOST_FALLBACK
        else:
            fallback_url = ""

        if fallback_url:
            if parsed.netloc.lower() == "searxng:8080":
                fallback_url = url.replace(
                    parsed.netloc, urlparse(fallback_url).netloc, 1
                )
            else:
                fallback_url = url.replace(
                    parsed.netloc, urlparse(fallback_url).netloc, 1
                )
            if fallback_url not in variants:
                variants.append(fallback_url)

        return variants

    @staticmethod
    def _redis_url_variants(url: str) -> list[str]:
        variants = [url]
        if url == "redis://redis:6379/2":
            variants.append(_REDIS_HOST_FALLBACK)
        return variants

    def _build_candidate(
        self,
        url: str,
        *,
        title: str = "",
        snippet: str = "",
        search_rank: Optional[int] = None,
        engine: Optional[str] = None,
        source_type: str = "search_result",
        explicit_order: Optional[int] = None,
        convert_document: Optional[bool] = None,
    ) -> dict[str, Any]:
        should_convert_document = (
            self._classify_url(url) == "document"
            if convert_document is None
            else convert_document
        )
        return {
            "url": url,
            "title": title or "",
            "snippet": snippet or "",
            "search_rank": search_rank,
            "engine": engine,
            "source_type": source_type,
            "explicit_order": explicit_order,
            "convert_document": bool(should_convert_document),
        }

    def _normalize_cached_search_candidates(self, payload: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        if not isinstance(payload, list):
            return candidates

        for index, item in enumerate(payload, start=1):
            if isinstance(item, str):
                candidate = self._build_candidate(item, search_rank=index)
            elif isinstance(item, dict):
                item_dict: dict[str, Any] = {
                    str(key): value for key, value in item.items()
                }
                raw_url = item_dict.get("url")
                if not isinstance(raw_url, str) or not raw_url:
                    continue

                raw_search_rank = item_dict.get("search_rank")
                search_rank = (
                    raw_search_rank if isinstance(raw_search_rank, int) else None
                )
                raw_engine = item_dict.get("engine")
                engine = raw_engine if isinstance(raw_engine, str) else None
                raw_convert_document = item_dict.get("convert_document")
                candidate = self._build_candidate(
                    raw_url,
                    title=str(item_dict.get("title", "") or ""),
                    snippet=str(item_dict.get("snippet", "") or ""),
                    search_rank=search_rank,
                    engine=engine,
                    source_type=str(
                        item_dict.get("source_type", "search_result") or "search_result"
                    ),
                    explicit_order=(
                        int(item_dict["explicit_order"])
                        if isinstance(item_dict.get("explicit_order"), int)
                        else None
                    ),
                    convert_document=(
                        bool(raw_convert_document)
                        if raw_convert_document is not None
                        else None
                    ),
                )
            else:
                continue
            candidates.append(candidate)

        return candidates

    def _normalize_url_targets(self, urls: Optional[list[Any]]) -> list[dict[str, Any]]:
        normalized_targets: list[dict[str, Any]] = []
        for explicit_order, raw_target in enumerate(urls or []):
            convert_document: Optional[bool] = None
            if isinstance(raw_target, str):
                raw_url = raw_target
            elif isinstance(raw_target, Mapping):
                raw_url = str(raw_target.get("url", "") or "")
                if "convert_document" in raw_target:
                    convert_document = bool(raw_target.get("convert_document"))
            elif hasattr(raw_target, "url"):
                raw_url = str(getattr(raw_target, "url", "") or "")
                if hasattr(raw_target, "convert_document"):
                    raw_value = getattr(raw_target, "convert_document")
                    convert_document = None if raw_value is None else bool(raw_value)
            else:
                continue

            normalized_raw_url = raw_url.strip()
            if not normalized_raw_url:
                continue

            url = (
                normalized_raw_url
                if normalized_raw_url.startswith("http")
                else f"https://{normalized_raw_url}"
            )
            normalized_targets.append(
                self._build_candidate(
                    url,
                    source_type="explicit_url",
                    explicit_order=explicit_order,
                    convert_document=convert_document,
                )
            )
        return normalized_targets

    def _merge_candidates(
        self,
        search_candidates: list[dict[str, Any]],
        explicit_targets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()

        for candidate in explicit_targets + search_candidates:
            url = candidate.get("url")
            if not url:
                continue
            canonical = self._canonicalize_url(url)
            if canonical in seen:
                existing = next(
                    (
                        item
                        for item in merged
                        if self._canonicalize_url(item["url"]) == canonical
                    ),
                    None,
                )
                if existing and candidate.get("source_type") == "explicit_url":
                    existing["source_type"] = "explicit_url"
                    existing["explicit_order"] = candidate.get("explicit_order")
                    existing["convert_document"] = bool(
                        candidate.get("convert_document", False)
                    ) or (self._classify_url(candidate["url"]) == "document")
                    existing["url"] = candidate["url"]
                continue

            seen.add(canonical)
            merged.append(dict(candidate))

        return merged

    def _rank_candidates(
        self, candidates: list[dict], max_per_domain: Optional[int] = 3
    ) -> list[dict]:
        explicit_candidates = sorted(
            [
                candidate
                for candidate in candidates
                if candidate.get("source_type") == "explicit_url"
            ],
            key=lambda item: (
                item.get("explicit_order")
                if item.get("explicit_order") is not None
                else 10_000,
            ),
        )
        ranked_search_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("source_type") != "explicit_url"
        ]
        ranked = explicit_candidates + ranked_search_candidates

        seen_canonical = set()
        domain_counts: dict[str, int] = {}
        diversified = []
        for candidate in ranked:
            url = candidate.get("url")
            if not url or self._classify_url(url) == "skip":
                continue

            canonical = self._canonicalize_url(url)
            if canonical in seen_canonical:
                continue
            seen_canonical.add(canonical)

            host = urlparse(url).netloc.lower().lstrip("www.")
            if (
                max_per_domain is not None
                and max_per_domain > 0
                and candidate.get("source_type") != "explicit_url"
                and domain_counts.get(host, 0) >= max_per_domain
            ):
                continue

            domain_counts[host] = domain_counts.get(host, 0) + 1
            diversified.append(candidate)

        return diversified

    @staticmethod
    def _normalize_related_links(
        base_url: str, raw_links: Any, limit: int = 5
    ) -> tuple[list[dict[str, str]], int]:
        if not isinstance(raw_links, dict):
            return [], 0

        base_canonical = Tools._canonicalize_url(base_url)
        seen: set[str] = set()
        collected: list[dict[str, str]] = []
        for bucket in ("internal", "external"):
            entries = raw_links.get(bucket)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                href = str(entry.get("href", "") or "").strip()
                if not href:
                    continue
                absolute_url = urljoin(base_url, href)
                canonical = Tools._canonicalize_url(absolute_url)
                if canonical == base_canonical or canonical in seen:
                    continue
                text = re.sub(
                    r"\s+",
                    " ",
                    str(entry.get("text") or entry.get("title") or "").strip(),
                )
                text_lower = text.lower()
                canonical_lower = canonical.lower()
                if text_lower in _RELATED_LINK_TEXT_BLOCKLIST:
                    continue
                if any(
                    blocked_fragment in canonical_lower
                    for blocked_fragment in _RELATED_LINK_URL_SUBSTRINGS
                ):
                    continue
                seen.add(canonical)
                collected.append(
                    {
                        "url": absolute_url,
                        "text": text,
                    }
                )
        return collected[:limit], len(collected)

    @staticmethod
    def _normalize_images(
        base_url: str, raw_media: Any, limit: int = 5
    ) -> list[dict[str, str]]:
        if not isinstance(raw_media, dict):
            return []

        images: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in raw_media.get("images") or []:
            if not isinstance(entry, dict):
                continue
            src = str(entry.get("src") or entry.get("url") or "").strip()
            if not src:
                continue
            absolute_url = urljoin(base_url, src)
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            image = {
                "url": absolute_url,
                "alt": re.sub(
                    r"\s+",
                    " ",
                    str(entry.get("alt") or entry.get("description") or "").strip(),
                ),
            }
            desc = re.sub(r"\s+", " ", str(entry.get("desc") or "").strip())
            if desc:
                image["desc"] = desc
            images.append(image)
            if len(images) >= limit:
                break
        return images

    @staticmethod
    def _normalize_tables(raw_tables: Any, limit: int = 5) -> list[dict[str, Any]]:
        if not isinstance(raw_tables, list):
            return []

        normalized: list[dict[str, Any]] = []
        for entry in raw_tables:
            if not isinstance(entry, dict):
                continue

            headers = [
                re.sub(r"\s+", " ", str(cell or "").strip())
                for cell in (entry.get("headers") or [])
            ]
            rows = [
                [re.sub(r"\s+", " ", str(cell or "").strip()) for cell in row]
                for row in (entry.get("rows") or [])
                if isinstance(row, list)
            ]
            table: dict[str, Any] = {"headers": headers, "rows": rows}

            caption = re.sub(r"\s+", " ", str(entry.get("caption") or "").strip())
            if caption:
                table["caption"] = caption

            summary = re.sub(r"\s+", " ", str(entry.get("summary") or "").strip())
            if summary:
                table["summary"] = summary

            metadata = entry.get("metadata")
            if isinstance(metadata, dict) and metadata:
                table["metadata"] = json.loads(json.dumps(metadata))

            normalized.append(table)
            if len(normalized) >= limit:
                break

        return normalized

    def _cached_record_satisfies_candidate(
        self, record: dict[str, Any], candidate: dict[str, Any]
    ) -> bool:
        if not bool(record.get("full_content_available", True)):
            return False
        if (
            candidate.get("convert_document")
            and record.get("content_type") != "document"
        ):
            return False
        if candidate.get("convert_document") and not bool(
            record.get("full_content_available", True)
        ):
            return False
        return True

    @staticmethod
    def _infer_page_quality(
        title: str,
        content: str,
        *,
        content_source: str,
        full_content_available: bool,
    ) -> Optional[str]:
        if content_source != "crawled_page" or not full_content_available:
            return None

        compact_title = re.sub(r"\s+", " ", str(title or "")).strip().lower()
        compact_content = re.sub(r"\s+", " ", str(content or "")).strip().lower()
        sample = f"{compact_title}\n{compact_content[:2000]}"
        short_page = len(compact_content) <= 4000

        if any(pattern in sample for pattern in _PAGE_QUALITY_BLOCKED_PATTERNS) and (
            short_page
            or any(
                pattern in compact_title for pattern in _PAGE_QUALITY_BLOCKED_PATTERNS
            )
        ):
            return "blocked"

        if any(pattern in sample for pattern in _PAGE_QUALITY_CHALLENGE_PATTERNS) and (
            short_page
            or any(
                pattern in compact_title for pattern in _PAGE_QUALITY_CHALLENGE_PATTERNS
            )
        ):
            return "challenge"

        return None

    def _build_result_from_record(
        self,
        page_id: str,
        record: dict[str, Any],
        query: str,
        *,
        search_rank: Optional[int] = None,
        source_type: Optional[str] = None,
        fallback_reason: Optional[str] = None,
    ) -> dict[str, Any]:
        content = self._page_record_content(record)
        compact = self._build_compact_summary(content, query)
        result = {
            "url": record["url"],
            "title": record.get("title", ""),
            "page_id": page_id,
            "summary": compact["summary"],
            "key_points": compact["key_points"],
            "content_length": len(content),
            "content_type": record.get("content_type", "html"),
            "source_type": source_type or record.get("source_type", "search_result"),
            "content_source": record.get("content_source", "crawled_page"),
            "full_content_available": bool(record.get("full_content_available", True)),
        }
        redirected_url = str(record.get("redirected_url", "") or "")
        if redirected_url:
            result["redirected_url"] = redirected_url
        status_code = record.get("status_code")
        if status_code is not None:
            result["status_code"] = int(status_code)
        if search_rank is not None:
            result["search_rank"] = search_rank
        page_quality = self._infer_page_quality(
            result["title"],
            content,
            content_source=str(result["content_source"]),
            full_content_available=bool(result["full_content_available"]),
        )
        if page_quality:
            result["page_quality"] = page_quality
        images = list(record.get("images") or [])
        if images:
            result["images"] = images
        if fallback_reason:
            result["fallback_reason"] = fallback_reason
        return result

    @staticmethod
    def _public_search_result(result: Mapping[str, Any]) -> dict[str, Any]:
        public_result = {
            "page_id": str(result.get("page_id", "") or ""),
            "url": str(result.get("url", "") or ""),
            "title": str(result.get("title", "") or ""),
            "summary": str(result.get("summary", "") or ""),
            "key_points": list(result.get("key_points") or []),
        }
        content_type = str(result.get("content_type", "") or "")
        if content_type and content_type != "html":
            public_result["content_type"] = content_type
        return public_result

    @staticmethod
    def _public_page_result(result: Mapping[str, Any]) -> dict[str, Any]:
        public_result: dict[str, Any] = {
            "page_id": str(result.get("page_id", "") or ""),
            "url": str(result.get("url", "") or ""),
            "title": str(result.get("title", "") or ""),
            "content": str(result.get("content", "") or ""),
        }
        content_type = str(result.get("content_type", "") or "")
        if content_type and content_type != "html":
            public_result["content_type"] = content_type
        if bool(result.get("truncated")):
            public_result["truncated"] = True
        error = str(result.get("error", "") or "")
        if error:
            public_result["error"] = error
        return public_result

    async def _build_search_only_result(
        self, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        title = str(candidate.get("title", "") or candidate.get("url", ""))
        summary = str(candidate.get("snippet", "") or "")
        page_id = await self._store_page_record(
            candidate["url"],
            title,
            summary,
            content_type="search_result",
            source_type=candidate.get("source_type", "search_result"),
            content_source="search_snippet",
            full_content_available=False,
            related_links=[],
            related_links_total=0,
            images=[],
            tables=[],
        )
        result = {
            "url": candidate["url"],
            "title": title,
            "page_id": page_id,
            "summary": summary,
            "key_points": [summary] if summary else [],
            "content_length": len(summary),
            "content_type": "search_result",
            "source_type": candidate.get("source_type", "search_result"),
            "content_source": "search_snippet",
            "full_content_available": False,
            "fallback_reason": "search_only",
        }
        if candidate.get("search_rank") is not None:
            result["search_rank"] = candidate.get("search_rank")
        return result

    def _empty_query_metadata(self, query: str, depth: str) -> dict[str, Any]:
        return {
            "query": query,
            "depth": depth,
            "search": {
                "cache_hit": False,
                "candidate_count": 0,
                "provider_failures": [],
            },
            "crawl": {
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "failed_urls": [],
            },
            "fallbacks_used": [],
            "result_count": 0,
        }

    def _record_failed_url(
        self,
        metadata: dict[str, Any],
        url: str,
        reason: str,
        *,
        stage: str,
        recovered_by_single_retry: bool = False,
    ) -> None:
        metadata["crawl"]["failed_urls"].append(
            {
                "url": url,
                "stage": stage,
                "reason": reason,
                "recovered_by_single_retry": recovered_by_single_retry,
            }
        )

    def _record_provider_failure(
        self,
        metadata: dict[str, Any],
        provider: str,
        error: str,
    ) -> None:
        metadata["search"]["provider_failures"].append(
            {"provider": provider, "error": error}
        )

    async def _emit_status(
        self,
        description: str,
        *,
        done: bool,
        __event_emitter__: EventEmitter = None,
    ) -> None:
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                    },
                }
            )

    async def _search_only_fallback_results(
        self,
        ranked_candidates: list[dict[str, Any]],
        return_limit: int,
    ) -> list[dict[str, Any]]:
        fallback_results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in ranked_candidates:
            canonical = self._canonicalize_url(candidate["url"])
            if canonical in seen:
                continue
            seen.add(canonical)
            fallback_results.append(await self._build_search_only_result(candidate))
            if len(fallback_results) >= return_limit:
                break

        if fallback_results and self.valves.DEBUG:
            logger.info(
                f"Returning {len(fallback_results)} search-only fallback results after crawl degradation"
            )
        return fallback_results

    async def _finalize_crawl_results(
        self,
        crawl_results: list[dict[str, Any]],
        ranked_candidates: list[dict[str, Any]],
        return_limit: int,
    ) -> list[dict[str, Any]]:
        crawled_by_url: dict[str, dict[str, Any]] = {}
        for raw_result in crawl_results:
            candidates = [raw_result.get("url", "")]
            redirected_url = str(raw_result.get("redirected_url", "") or "")
            if redirected_url:
                candidates.append(redirected_url)
            for candidate_url in candidates:
                canonical = self._canonicalize_url(candidate_url)
                if canonical:
                    crawled_by_url[canonical] = raw_result

        finalized_results: list[dict[str, Any]] = []
        for candidate in ranked_candidates:
            canonical = self._canonicalize_url(candidate["url"])
            crawled = crawled_by_url.get(canonical)
            if crawled:
                result = dict(crawled)
                content = str(result.pop("_content", "") or "")
                if content:
                    result["page_id"] = await self._store_page_record(
                        result["url"],
                        result.get("title", ""),
                        content,
                        content_type=str(result.get("content_type", "html") or "html"),
                        source_type=candidate.get("source_type", "search_result"),
                        content_source=str(
                            result.get("content_source", "crawled_page")
                            or "crawled_page"
                        ),
                        full_content_available=bool(
                            result.get("full_content_available", True)
                        ),
                        redirected_url=str(result.get("redirected_url", "") or ""),
                        status_code=(
                            int(result["status_code"])
                            if result.get("status_code") is not None
                            else None
                        ),
                        related_links=(
                            list(result.get("related_links", []))
                            if isinstance(result.get("related_links"), list)
                            else []
                        ),
                        related_links_total=int(
                            result.get(
                                "related_links_total",
                                len(result.get("related_links", [])),
                            )
                            or 0
                        ),
                        images=(
                            list(result.get("images", []))
                            if isinstance(result.get("images"), list)
                            else []
                        ),
                        tables=(
                            list(result.get("tables", []))
                            if isinstance(result.get("tables"), list)
                            else []
                        ),
                    )
                result["source_type"] = candidate.get("source_type", "search_result")
                result["content_source"] = str(
                    result.get("content_source", "crawled_page") or "crawled_page"
                )
                result["full_content_available"] = bool(
                    result.get("full_content_available", True)
                )
                page_quality = self._infer_page_quality(
                    result.get("title", ""),
                    content,
                    content_source=str(result["content_source"]),
                    full_content_available=bool(result["full_content_available"]),
                )
                if page_quality:
                    result["page_quality"] = page_quality
                else:
                    result.pop("page_quality", None)
                if candidate.get("search_rank") is not None:
                    result["search_rank"] = candidate.get("search_rank")
                else:
                    result.pop("search_rank", None)
                result.pop("related_links", None)
                result.pop("related_links_total", None)
                result.pop("related_links_more_available", None)
                result.pop("tables", None)
                if not result.get("images"):
                    result.pop("images", None)
                finalized_results.append(result)
            else:
                finalized_results.append(
                    await self._build_search_only_result(candidate)
                )

            if len(finalized_results) >= return_limit:
                break

        return finalized_results

    async def _search_searxng(
        self,
        query: str,
        __event_emitter__: EventEmitter = None,
    ) -> List[dict[str, Any]]:
        if not self.valves.SEARCH_WITH_SEARXNG and self.valves.DEBUG:
            logger.info("SearXNG search is disabled.")
            return []
        if not self.valves.SEARXNG_BASE_URL:
            return []

        normalized_query = self._normalize_query(query)
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if self.valves.SEARXNG_API_TOKEN:
            headers["Authorization"] = f"Bearer {self.valves.SEARXNG_API_TOKEN}"

        last_exc: Exception | None = None
        for base_url in self._http_url_variants(self.valves.SEARXNG_BASE_URL):
            url = base_url.replace("<query>", quote_plus(normalized_query))
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
                if self.valves.DEBUG:
                    logger.info(
                        f"SearXNG returned {len(results)} raw results for query={normalized_query!r}"
                    )
                max_results = (
                    self.user_valves.SEARXNG_MAX_RESULTS
                    or self.valves.SEARXNG_MAX_RESULTS
                )
                structured_results = []
                for rank, result in enumerate(results[:max_results], start=1):
                    if not result.get("url"):
                        continue
                    engines = result.get("engines") or []
                    if isinstance(engines, list):
                        engine = ", ".join(engine for engine in engines if engine)
                    else:
                        engine = str(engines or "")
                    structured_results.append(
                        self._build_candidate(
                            result["url"],
                            title=str(result.get("title", "") or ""),
                            snippet=str(
                                result.get("content")
                                or result.get("snippet")
                                or result.get("description")
                                or ""
                            ),
                            search_rank=rank,
                            engine=result.get("engine") or engine or None,
                            source_type="search_result",
                        )
                    )
                return structured_results
            except Exception as exc:
                last_exc = exc
                if self._active_query_metadata is not None:
                    self._record_provider_failure(
                        self._active_query_metadata, "searxng", str(exc)
                    )
                if self.valves.DEBUG:
                    logger.warning(
                        f"SearXNG request failed for query={normalized_query!r} base_url={base_url!r}: {exc}"
                    )

        logger.error(f"Error searching SearXNG: {last_exc}")
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
        return f"sc:page:{_PAGE_CACHE_VERSION}:{hashlib.md5(Tools._canonicalize_url(url).encode()).hexdigest()}"

    @staticmethod
    def _page_id_cache_key(page_id: str) -> str:
        return f"sc:pageid:{_PAGE_CACHE_VERSION}:{page_id}"

    @staticmethod
    def _page_id_for_url(url: str) -> str:
        return hashlib.md5(Tools._canonicalize_url(url).encode()).hexdigest()[:10]

    @staticmethod
    def _page_record_content(record: Mapping[str, Any]) -> str:
        content = str(record.get("content") or "")
        if content:
            return content

        internal_content = str(record.get("_content") or "")
        if internal_content:
            return internal_content

        representations = record.get("representations")
        if isinstance(representations, dict):
            return str(
                representations.get("fit_markdown")
                or representations.get("raw_markdown")
                or representations.get("cleaned_html")
                or representations.get("fit_html")
                or representations.get("html")
                or ""
            )
        return ""

    @classmethod
    def _normalize_page_record(cls, record: Mapping[str, Any]) -> dict[str, Any] | None:
        url = str(record.get("url", "") or "")
        if not url:
            return None

        content = cls._page_record_content(record)
        content_type = str(record.get("content_type", "html") or "html")
        raw_status_code = record.get("status_code")
        try:
            status_code = (
                int(raw_status_code)
                if raw_status_code is not None and raw_status_code != ""
                else None
            )
        except (TypeError, ValueError):
            status_code = None

        related_links = record.get("related_links")
        images = record.get("images")
        tables = record.get("tables")
        return {
            "page_id": str(record.get("page_id") or cls._page_id_for_url(url)),
            "url": url,
            "title": str(record.get("title", "") or ""),
            "content": content,
            "content_type": content_type,
            "source_type": str(
                record.get("source_type", "search_result") or "search_result"
            ),
            "content_source": str(
                record.get("content_source", "crawled_page") or "crawled_page"
            ),
            "redirected_url": str(record.get("redirected_url", "") or ""),
            "status_code": status_code,
            "full_content_available": bool(record.get("full_content_available", True)),
            "related_links": list(related_links)
            if isinstance(related_links, list)
            else [],
            "related_links_total": int(
                record.get(
                    "related_links_total",
                    len(related_links) if isinstance(related_links, list) else 0,
                )
                or 0
            ),
            "images": list(images) if isinstance(images, list) else [],
            "tables": list(tables) if isinstance(tables, list) else [],
        }

    async def _store_page_record(
        self,
        url: str,
        title: str,
        content: str,
        *,
        content_type: str = "html",
        source_type: str = "search_result",
        content_source: str = "crawled_page",
        redirected_url: str = "",
        status_code: int | None = None,
        full_content_available: bool = True,
        related_links: Optional[list[dict[str, str]]] = None,
        related_links_total: int = 0,
        images: Optional[list[dict[str, str]]] = None,
        tables: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        page_id = self._page_id_for_url(url)
        stored_content = str(content or "")

        cache_record = json.dumps(
            {
                "page_id": page_id,
                "url": url,
                "title": title,
                "content": stored_content,
                "content_type": content_type,
                "source_type": source_type,
                "content_source": content_source,
                "redirected_url": str(redirected_url or ""),
                "status_code": status_code,
                "full_content_available": full_content_available,
                "related_links": list(related_links or []),
                "related_links_total": int(related_links_total or 0),
                "images": list(images or []),
                "tables": list(tables or []),
            }
        )
        ttl = _ttl_for_url(url)
        await self._cache.setex(self._page_cache_key(url), ttl, cache_record)
        await self._cache.setex(self._page_id_cache_key(page_id), ttl, cache_record)
        return page_id

    def _search_cache_key(self, query: str) -> str:
        providers = []
        if self.valves.SEARCH_WITH_SEARXNG:
            providers.append("searxng:v2")
        provider_str = "+".join(sorted(providers)) or "none"
        raw = self._normalize_query(query).lower() + "|" + provider_str
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

    @classmethod
    def _normalized_result_url(cls, url: str, redirected_url: Any = None) -> str:
        preferred = str(redirected_url or url or "").strip()
        if not preferred:
            return ""
        canonical = cls._canonicalize_url(preferred)
        parsed_original = urlparse(preferred)
        parsed_canonical = urlparse(canonical)
        return parsed_canonical._replace(fragment=parsed_original.fragment).geturl()

    async def _load_page_record(self, page_id: str) -> Optional[dict]:
        cached_page = await self._cache.get(self._page_id_cache_key(page_id))
        if not cached_page:
            return None

        try:
            normalized = self._normalize_page_record(json.loads(cached_page))
            if not normalized or normalized["page_id"] != page_id:
                return None
            return normalized
        except Exception:
            return None

    async def _page_result_from_record(
        self,
        record: dict[str, Any],
        *,
        focus: str = "",
        related_links_limit: int = 3,
        max_chars: int = 8000,
        __event_emitter__: EventEmitter = None,
    ) -> dict[str, Any]:
        page_id = str(record.get("page_id") or self._page_id_for_url(record["url"]))

        if self.valves.MORE_STATUS:
            await self._emit_status(
                f"Read {record['title'][:60]}...",
                done=True,
                __event_emitter__=__event_emitter__,
            )

        full_content = self._page_record_content(record)
        content = (
            self._bm25_extract_sections(full_content, focus, max_chars=max_chars)
            if focus
            else full_content[:max_chars]
        )
        truncated = content != full_content

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
        result = {
            "page_id": page_id,
            "url": record["url"],
            "title": record["title"],
            "content": content,
            "content_length": len(full_content),
            "content_type": str(record.get("content_type", "html") or "html"),
            "source_type": str(
                record.get("source_type", "search_result") or "search_result"
            ),
            "content_source": str(
                record.get("content_source", "crawled_page") or "crawled_page"
            ),
            "full_content_available": bool(record.get("full_content_available", True)),
            "focus_applied": bool(focus),
            "truncated": truncated,
        }
        redirected_url = str(record.get("redirected_url", "") or "")
        if redirected_url:
            result["redirected_url"] = redirected_url
        status_code = record.get("status_code")
        if status_code is not None:
            result["status_code"] = int(status_code)
        page_quality = self._infer_page_quality(
            result["title"],
            full_content,
            content_source=str(result["content_source"]),
            full_content_available=bool(result["full_content_available"]),
        )
        if page_quality:
            result["page_quality"] = page_quality
        images = list(record.get("images") or [])
        if images:
            result["images"] = images
        tables = list(record.get("tables") or [])
        if tables:
            result["tables"] = tables
        related_links = list(record.get("related_links") or [])
        related_links_total = int(
            record.get("related_links_total", len(related_links)) or 0
        )
        effective_related_links_limit = max(0, int(related_links_limit))
        if related_links_total:
            result["related_links_total"] = related_links_total
            returned_links = (
                related_links[:effective_related_links_limit]
                if effective_related_links_limit > 0
                else []
            )
            if effective_related_links_limit > 0:
                result["related_links"] = returned_links
            result["related_links_more_available"] = related_links_total > len(
                returned_links
            )
        return result

    async def _read_single_page(
        self,
        page_id: str,
        focus: str = "",
        related_links_limit: int = 3,
        max_chars: int = 8000,
        __event_emitter__: EventEmitter = None,
    ) -> dict:
        record = await self._load_page_record(page_id)
        if not record:
            return {
                "page_id": page_id,
                "error": f"page_id '{page_id}' not found or expired. Call search_web again.",
            }
        return await self._page_result_from_record(
            record,
            focus=focus,
            related_links_limit=related_links_limit,
            max_chars=max_chars,
            __event_emitter__=__event_emitter__,
        )

    async def _read_single_url(
        self,
        target: Any,
        focus: str = "",
        related_links_limit: int = 3,
        max_chars: int = 8000,
        __event_emitter__: EventEmitter = None,
    ) -> dict[str, Any]:
        normalized_targets = self._normalize_url_targets([target])
        if not normalized_targets:
            return {"error": "urls must contain at least one non-empty URL."}

        candidate = normalized_targets[0]
        url = str(candidate.get("url", "") or "")
        if not url:
            return {"error": "urls must contain at least one non-empty URL."}

        cached_record = None
        try:
            cached_page = await self._cache.get(self._page_cache_key(url))
            if cached_page:
                parsed_record = self._normalize_page_record(json.loads(cached_page))
                if isinstance(
                    parsed_record, dict
                ) and self._cached_record_satisfies_candidate(parsed_record, candidate):
                    cached_record = parsed_record
                    if cached_record:
                        return await self._read_single_page(
                            str(cached_record["page_id"]),
                            focus=focus,
                            related_links_limit=related_links_limit,
                            max_chars=max_chars,
                            __event_emitter__=__event_emitter__,
                        )
        except Exception:
            cached_record = None

        url_type = self._classify_url(url)
        if url_type == "skip":
            return {
                "url": url,
                "error": f"URL '{url}' points to a file type that read_pages does not read directly.",
            }

        if url_type == "document":
            crawled = await self._fetch_document(
                url,
                query=focus,
                timeout_s=self.valves.DOCUMENT_FETCH_TIMEOUT,
                source_type="explicit_url",
                __event_emitter__=__event_emitter__,
            )
            if not crawled:
                return {"url": url, "error": f"Failed to read URL '{url}'."}
            direct_record = self._normalize_page_record(crawled)
            if isinstance(direct_record, dict):
                return await self._page_result_from_record(
                    direct_record,
                    focus=focus,
                    related_links_limit=related_links_limit,
                    max_chars=max_chars,
                    __event_emitter__=__event_emitter__,
                )
            return await self._read_single_page(
                str(crawled["page_id"]),
                focus=focus,
                related_links_limit=related_links_limit,
                max_chars=max_chars,
                __event_emitter__=__event_emitter__,
            )

        crawled_payload = await self._crawl_url(
            [url],
            query=focus or None,
            timeout_s=self.valves.CRAWL4AI_TIMEOUT,
            cache_mode="bypass",
            source_type="explicit_url",
            __event_emitter__=__event_emitter__,
        )
        if "error" in crawled_payload:
            return {"url": url, "error": f"Failed to read URL '{url}'."}

        content_items = crawled_payload.get("content", [])
        if not content_items:
            return {"url": url, "error": f"Failed to read URL '{url}'."}

        direct_record = self._normalize_page_record(content_items[0])
        if isinstance(direct_record, dict):
            return await self._page_result_from_record(
                direct_record,
                focus=focus,
                related_links_limit=related_links_limit,
                max_chars=max_chars,
                __event_emitter__=__event_emitter__,
            )

        page_id = str(content_items[0].get("page_id", "") or "")
        if not page_id:
            return {"url": url, "error": f"Failed to read URL '{url}'."}

        return await self._read_single_page(
            page_id,
            focus=focus,
            related_links_limit=related_links_limit,
            max_chars=max_chars,
            __event_emitter__=__event_emitter__,
        )

    async def _read_pages_internal(
        self,
        page_ids: Union[str, List[str], None] = None,
        urls: Union[str, list[Any], None] = None,
        focus: str = "",
        related_links_limit: int = 3,
        max_chars: int = 8000,
        __event_emitter__: EventEmitter = None,
    ) -> dict:
        if isinstance(page_ids, str) and urls is None:
            single_result = await self._read_single_page(
                page_ids,
                focus=focus,
                related_links_limit=related_links_limit,
                max_chars=max_chars,
                __event_emitter__=__event_emitter__,
            )
            if "error" in single_result:
                return {"error": single_result["error"]}
            return single_result

        if isinstance(urls, str) and page_ids is None:
            single_result = await self._read_single_url(
                urls,
                focus=focus,
                related_links_limit=related_links_limit,
                max_chars=max_chars,
                __event_emitter__=__event_emitter__,
            )
            if "error" in single_result:
                return {"error": single_result["error"]}
            return single_result

        normalized_page_ids = []
        seen_page_ids = set()
        for page_id in page_ids or []:
            normalized_page_id = str(page_id).strip()
            if not normalized_page_id or normalized_page_id in seen_page_ids:
                continue
            seen_page_ids.add(normalized_page_id)
            normalized_page_ids.append(normalized_page_id)

        normalized_url_targets = self._normalize_url_targets(
            [urls] if isinstance(urls, str) else urls
        )
        if not normalized_page_ids and len(normalized_url_targets) == 1:
            single_result = await self._read_single_url(
                normalized_url_targets[0],
                focus=focus,
                related_links_limit=related_links_limit,
                max_chars=max_chars,
                __event_emitter__=__event_emitter__,
            )
            if "error" in single_result:
                return {"error": single_result["error"]}
            return single_result

        if not normalized_page_ids and not normalized_url_targets:
            return {"error": "Provide at least one page_id or URL to read_pages."}

        pages = []
        errors = []
        for page_id in normalized_page_ids:
            page_result = await self._read_single_page(
                page_id,
                focus=focus,
                related_links_limit=related_links_limit,
                max_chars=max_chars,
                __event_emitter__=__event_emitter__,
            )
            if "error" in page_result:
                errors.append({"page_id": page_id, "error": page_result["error"]})
                continue
            pages.append(page_result)

        normalized_url_strings: list[str] = []
        for target in normalized_url_targets:
            url = str(target.get("url", "") or "")
            if not url:
                continue
            normalized_url_strings.append(url)
            page_result = await self._read_single_url(
                target,
                focus=focus,
                related_links_limit=related_links_limit,
                max_chars=max_chars,
                __event_emitter__=__event_emitter__,
            )
            if "error" in page_result:
                errors.append({"url": url, "error": page_result["error"]})
                continue
            pages.append(page_result)

        response = {
            "pages": pages,
            "errors": errors,
            "requested_page_ids": normalized_page_ids,
            "returned_pages": len(pages),
        }
        if normalized_url_strings:
            response["requested_urls"] = normalized_url_strings
        return response

    async def read_pages(
        self,
        page_ids: list[str],
        focus: str = "",
        __event_emitter__: EventEmitter = None,
    ) -> list[dict[str, Any]]:
        results = await self._read_pages_internal(
            page_ids=page_ids,
            focus=focus,
            __event_emitter__=__event_emitter__,
        )
        if isinstance(results, dict) and "pages" in results:
            pages = list(results.get("pages") or [])
            errors = list(results.get("errors") or [])
            return [
                self._public_page_result(page)
                for page in [*pages, *errors]
                if isinstance(page, dict)
            ]
        if isinstance(results, dict):
            return [self._public_page_result(results)]
        return []

    async def read_urls(
        self,
        urls: list[str],
        focus: str = "",
        __event_emitter__: EventEmitter = None,
    ) -> list[dict[str, Any]]:
        results = await self._read_pages_internal(
            urls=urls,
            focus=focus,
            __event_emitter__=__event_emitter__,
        )
        if isinstance(results, dict) and "pages" in results:
            pages = list(results.get("pages") or [])
            errors = list(results.get("errors") or [])
            return [
                self._public_page_result(page)
                for page in [*pages, *errors]
                if isinstance(page, dict)
            ]
        if isinstance(results, dict):
            return [self._public_page_result(results)]
        return []

    @staticmethod
    def _normalize_domains(domains: Optional[list[str]]) -> list[str]:
        normalized_domains: list[str] = []
        seen: set[str] = set()
        for raw_domain in domains or []:
            domain = str(raw_domain or "").strip().lower()
            if not domain:
                continue
            if "://" in domain:
                domain = urlparse(domain).netloc.lower()
            domain = domain.lstrip("www.").rstrip("/")
            if not domain or domain in seen:
                continue
            seen.add(domain)
            normalized_domains.append(domain)
        return normalized_domains

    @staticmethod
    def _append_site_filters(query: str, domains: list[str]) -> str:
        if not domains:
            return query
        site_tokens = " ".join(f"site:{domain}" for domain in domains)
        return f"{query} {site_tokens}".strip()

    async def search_web(
        self,
        query: str,
        domains: Optional[list[str]] = None,
        urls: Optional[list[str]] = None,
        __event_emitter__: EventEmitter = None,
    ) -> list[dict[str, Any]]:
        results = await self._search_web_internal(
            query=self._append_site_filters(
                self._normalize_query(query), self._normalize_domains(domains)
            ),
            urls=urls,
            depth="deep",
            max_results=None,
            fresh=False,
            __event_emitter__=__event_emitter__,
        )
        return [self._public_search_result(result) for result in results]

    async def _search_web_internal(
        self,
        query: str,
        urls: Optional[List[Any]] = None,
        depth: str = "normal",
        max_results: Optional[int] = None,
        fresh: bool = False,
        __event_emitter__: EventEmitter = None,
    ) -> list[dict[str, Any]]:
        logger.info(f"Starting search and crawl for '{query}' (depth={depth})")
        query_metadata = self._empty_query_metadata(query, depth)
        self._last_query_metadata = query_metadata
        self._active_query_metadata = query_metadata
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

        requested_results = max_results if max_results and max_results > 0 else None
        effective_return_limit = (
            min(requested_results, budget["search_candidates"])
            if requested_results is not None
            else budget["return_limit"]
        )
        query = self._normalize_query(query)
        site_filters = self._site_filters_from_query(query)
        explicit_targets = self._normalize_url_targets(urls)
        self.crawl_counter = 0
        self.content_counter = 0
        self.total_urls = 0

        try:
            if __event_emitter__ and str(self.valves.INITIAL_RESPONSE).strip() != "":
                await __event_emitter__(
                    {
                        "type": "chat:message:delta",
                        "data": {"content": str(self.valves.INITIAL_RESPONSE).strip()},
                    }
                )

            await self._emit_status(
                "Searching web sources...",
                done=False,
                __event_emitter__=__event_emitter__,
            )

            search_cache_key = self._search_cache_key(query)
            cached_search = None if fresh else await self._cache.get(search_cache_key)
            search_candidates: list[dict[str, Any]] = []
            if cached_search:
                self._cache_stats["search_hits"] += 1
                query_metadata["search"]["cache_hit"] = True
                try:
                    search_candidates = self._normalize_cached_search_candidates(
                        json.loads(cached_search)
                    )
                except Exception:
                    search_candidates = []
            else:
                self._cache_stats["search_misses"] += 1
                if self.valves.SEARCH_WITH_SEARXNG:
                    try:
                        search_candidates = await asyncio.wait_for(
                            self._search_searxng(query, __event_emitter__),
                            timeout=min(budget["search_timeout"], time_left()),
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "SearXNG timed out, proceeding with what we have"
                        )
                if search_candidates:
                    await self._cache.setex(
                        search_cache_key, 600, json.dumps(search_candidates)
                    )
            if site_filters:
                search_candidates = [
                    candidate
                    for candidate in search_candidates
                    if self._url_matches_site_filters(
                        str(candidate.get("url", "") or ""), site_filters
                    )
                ]
            query_metadata["search"]["candidate_count"] = len(search_candidates)

            ranked_candidates = self._rank_candidates(
                self._merge_candidates(search_candidates, explicit_targets),
                max_per_domain=(
                    None if site_filters else (2 if depth in ("quick", "normal") else 3)
                ),
            )
            ranked_candidates = ranked_candidates[
                : max(budget["search_candidates"], len(explicit_targets))
            ]
            self.total_urls = len(ranked_candidates)
            if not ranked_candidates:
                await self._emit_status(
                    "No matching sources found.",
                    done=True,
                    __event_emitter__=__event_emitter__,
                )
                self._active_query_metadata = None
                query_metadata["result_count"] = 0
                return []

            crawl_limit = min(
                budget["search_candidates"],
                max(
                    budget["crawl_limit"],
                    effective_return_limit + budget["crawl_slack"],
                    len(explicit_targets),
                ),
            )
            planned_reads = min(crawl_limit, len(ranked_candidates))
            candidate_label = "candidate" if len(ranked_candidates) == 1 else "candidates"
            page_label = "page" if planned_reads == 1 else "pages"
            await self._emit_status(
                f"Found {len(ranked_candidates)} {candidate_label}; reading up to {planned_reads} {page_label}...",
                done=False,
                __event_emitter__=__event_emitter__,
            )
            crawl_results: list[dict[str, Any]] = []
            attempted_fetches = 0
            for candidate in ranked_candidates:
                if len(crawl_results) >= crawl_limit:
                    break

                url = candidate["url"]
                url_type = self._classify_url(url)

                if url_type == "skip":
                    continue

                should_skip_negative_cache = candidate.get(
                    "source_type"
                ) != "explicit_url" and not candidate.get("convert_document")
                if url_type == "document":
                    should_skip_negative_cache = True
                if (
                    not fresh
                    and should_skip_negative_cache
                    and await self._cache.exists(self._dead_cache_key(url))
                ):
                    self._cache_stats["negative_skips"] += 1
                    continue

                cached_page_record: Optional[dict[str, Any]] = None
                if not fresh:
                    try:
                        cached_page = await self._cache.get(self._page_cache_key(url))
                        if cached_page:
                            self._cache_stats["page_hits"] += 1
                            parsed_record = self._normalize_page_record(
                                json.loads(cached_page)
                            )
                            if isinstance(parsed_record, dict) and (
                                self._cached_record_satisfies_candidate(
                                    parsed_record, candidate
                                )
                            ):
                                cached_page_record = parsed_record
                                if cached_page_record:
                                    crawl_results.append(
                                        self._build_result_from_record(
                                            str(cached_page_record["page_id"]),
                                            cached_page_record,
                                            query,
                                            search_rank=candidate.get("search_rank"),
                                            source_type=candidate.get(
                                                "source_type", "search_result"
                                            ),
                                        )
                                    )
                                    continue
                    except Exception:
                        pass

                self._cache_stats["page_misses"] += 1
                attempted_fetches += 1

                try:
                    if candidate.get("convert_document") and url_type == "document":
                        crawled = await self._fetch_document(
                            url,
                            query=query,
                            timeout_s=self.valves.DOCUMENT_FETCH_TIMEOUT,
                            source_type=candidate.get("source_type", "search_result"),
                            __event_emitter__=__event_emitter__,
                        )
                        if crawled:
                            crawl_results.append(crawled)
                        continue

                    if url_type == "document":
                        continue

                    remaining = time_left()
                    if remaining < 2.0:
                        break

                    crawl_timeout = min(
                        self.valves.CRAWL4AI_TIMEOUT,
                        max(3.0, remaining - 1),
                    )
                    crawled_payload = await self._crawl_url(
                        [url],
                        query=query,
                        timeout_s=crawl_timeout,
                        cache_mode="write_only" if fresh else "enabled",
                        source_type=candidate.get("source_type", "search_result"),
                        __event_emitter__=__event_emitter__,
                    )
                    if "error" in crawled_payload:
                        continue
                    crawl_results.extend(crawled_payload.get("content", []))
                except Exception as exc:
                    logger.error(
                        f"Crawl error for {url}: {exc}\n{traceback.format_exc()}"
                    )

            query_metadata["crawl"]["attempted"] = attempted_fetches

            finalized_results = await self._finalize_crawl_results(
                crawl_results,
                ranked_candidates=ranked_candidates,
                return_limit=effective_return_limit,
            )
            if (
                any(
                    result.get("fallback_reason") == "search_only"
                    for result in finalized_results
                )
                and "search_only" not in query_metadata["fallbacks_used"]
            ):
                query_metadata["fallbacks_used"].append("search_only")
            query_metadata["crawl"]["succeeded"] = len(crawl_results)
            query_metadata["crawl"]["failed"] = len(
                query_metadata["crawl"]["failed_urls"]
            )
            if finalized_results:
                query_metadata["result_count"] = len(finalized_results)
                result_label = (
                    "result" if len(finalized_results) == 1 else "results"
                )
                page_label = "page" if len(crawl_results) == 1 else "pages"
                await self._emit_status(
                    f"Prepared {len(finalized_results)} {result_label} from {len(crawl_results)} {page_label}.",
                    done=True,
                    __event_emitter__=__event_emitter__,
                )
                self._active_query_metadata = None
                return finalized_results

            if ranked_candidates:
                query_metadata["fallbacks_used"].append("search_only")
                fallback_results = await self._search_only_fallback_results(
                    ranked_candidates,
                    effective_return_limit,
                )
                query_metadata["result_count"] = len(fallback_results)
                result_label = (
                    "result" if len(fallback_results) == 1 else "results"
                )
                await self._emit_status(
                    f"Crawl degraded; returning {len(fallback_results)} search-only {result_label}.",
                    done=True,
                    __event_emitter__=__event_emitter__,
                )
                self._active_query_metadata = None
                return fallback_results

            query_metadata["result_count"] = 0
            await self._emit_status(
                "No readable sources found.",
                done=True,
                __event_emitter__=__event_emitter__,
            )
            self._active_query_metadata = None
            return []
        finally:
            pass

    async def _fetch_document(
        self,
        url: str,
        query: str = "",
        timeout_s: Optional[float] = None,
        source_type: str = "search_result",
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
                        if self._active_query_metadata is not None:
                            self._record_failed_url(
                                self._active_query_metadata,
                                url,
                                f"http_{resp.status}",
                                stage="document_conversion",
                                recovered_by_single_retry=False,
                            )
                        return None
                    cl = resp.headers.get("Content-Length")
                    if cl and int(cl) > max_size:
                        if self._active_query_metadata is not None:
                            self._record_failed_url(
                                self._active_query_metadata,
                                url,
                                "too_large",
                                stage="document_conversion",
                                recovered_by_single_retry=False,
                            )
                        return None
                    chunks = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > max_size:
                            if self._active_query_metadata is not None:
                                self._record_failed_url(
                                    self._active_query_metadata,
                                    url,
                                    "too_large",
                                    stage="document_conversion",
                                    recovered_by_single_retry=False,
                                )
                            return None
                        chunks.append(chunk)
                    raw_bytes = b"".join(chunks)
                    final_url = self._normalized_result_url(url, str(resp.url))
                    redirected_url = (
                        self._normalized_result_url(str(resp.url))
                        if str(resp.url)
                        else ""
                    )
                    response_status = int(resp.status)

            import io

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: converter.convert_stream(io.BytesIO(raw_bytes), url=url),
            )
            title = getattr(result, "title", None) or url.rsplit("/", 1)[-1]
            content = getattr(result, "text_content", None) or ""
            if not content.strip():
                if self._active_query_metadata is not None:
                    self._record_failed_url(
                        self._active_query_metadata,
                        url,
                        "empty_content",
                        stage="document_conversion",
                        recovered_by_single_retry=False,
                    )
                return None

            page_id = await self._store_page_record(
                final_url or url,
                title,
                content,
                content_type="document",
                source_type=source_type,
                content_source="converted_document",
                redirected_url=redirected_url,
                status_code=response_status,
                full_content_available=True,
                related_links=[],
                related_links_total=0,
                images=[],
                tables=[],
            )
            if final_url and self._canonicalize_url(
                final_url
            ) != self._canonicalize_url(url):
                stored_page = await self._cache.get(self._page_id_cache_key(page_id))
                if stored_page:
                    await self._cache.setex(
                        self._page_cache_key(url),
                        _ttl_for_url(url),
                        stored_page,
                    )
            await self._cache.delete(self._dead_cache_key(url))
            compact = self._build_compact_summary(content, query)
            return {
                "url": final_url or url,
                "title": title,
                "page_id": page_id,
                "summary": compact["summary"],
                "key_points": compact["key_points"],
                "content_length": len(content),
                "content_type": "document",
                "source_type": source_type,
                "content_source": "converted_document",
                "redirected_url": redirected_url,
                "status_code": response_status,
                "full_content_available": True,
                "_content": content,
            }
        except Exception as exc:
            if self._active_query_metadata is not None:
                self._record_failed_url(
                    self._active_query_metadata,
                    url,
                    str(exc),
                    stage="document_conversion",
                    recovered_by_single_retry=False,
                )
            await self._cache.setex(
                self._dead_cache_key(url),
                _negative_ttl("timeout"),
                json.dumps({"reason": "document_conversion_failed"}),
            )
            logger.warning(f"Document conversion failed for {url}: {exc}")
            return None

    async def _crawl_url(
        self,
        urls: Union[list, str],
        query: Optional[str] = None,
        timeout_s: Optional[float] = None,
        cache_mode: str = "enabled",
        source_type: str = "search_result",
        __event_emitter__: EventEmitter = None,
    ) -> dict:
        if isinstance(urls, str):
            urls = [urls]

        for idx, url in enumerate(urls):
            if not url.startswith("http"):
                urls[idx] = f"https://{url}"

        payload = {
            "urls": urls,
            "browser_config": _browser_config_payload(),
            "crawler_config": _crawler_config_payload(self, cache_mode=cache_mode),
        }

        async def mark_request_failure(reason: str) -> None:
            for failed_url in urls:
                await self._cache.setex(
                    self._dead_cache_key(failed_url),
                    _negative_ttl(reason),
                    json.dumps({"reason": reason}),
                )
                if self._active_query_metadata is not None:
                    self._record_failed_url(
                        self._active_query_metadata,
                        failed_url,
                        reason,
                        stage="crawl_request",
                        recovered_by_single_retry=False,
                    )

        last_exc: Exception | None = None
        for base_url in self._http_url_variants(self.valves.CRAWL4AI_BASE_URL):
            endpoint = f"{base_url}/crawl"
            try:
                effective_timeout = timeout_s or (
                    self.valves.CRAWL4AI_TIMEOUT * len(urls) + 60
                )
                crawl_timeout = aiohttp.ClientTimeout(total=effective_timeout)
                async with aiohttp.ClientSession(timeout=crawl_timeout) as session:
                    async with session.post(
                        endpoint,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()

                results = []
                sent_urls = set(urls)
                processed_urls = set()
                requested_url_by_canonical = {
                    self._canonicalize_url(sent_url): sent_url for sent_url in urls
                }
                data_list = data.get("results", [])
                for item in data_list:
                    item_url = item.get("url", "")
                    if item_url:
                        processed_urls.add(item_url)
                    if item.get("success") is not True:
                        if item_url:
                            status = int(item.get("status_code") or 0)
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
                            if self._active_query_metadata is not None:
                                self._record_failed_url(
                                    self._active_query_metadata,
                                    item_url,
                                    err_type,
                                    stage="crawl_request",
                                    recovered_by_single_retry=False,
                                )
                        continue

                    page_content, summary_content = _markdown_content_variants(
                        item.get("markdown", ""),
                        html_fallback=str(
                            item.get("cleaned_html")
                            or item.get("fit_html")
                            or item.get("html")
                            or ""
                        ),
                    )
                    result_url = self._normalized_result_url(
                        item_url, item.get("redirected_url")
                    )
                    if result_url:
                        item_url = result_url
                    requested_url = requested_url_by_canonical.get(
                        self._canonicalize_url(str(item.get("url", "") or "")),
                        "",
                    )
                    title = item.get("metadata", {}).get("title", "")
                    related_links, related_links_total = self._normalize_related_links(
                        item_url, item.get("links")
                    )
                    media = item.get("media")
                    images = self._normalize_images(item_url, media)
                    tables = self._normalize_tables(
                        item.get("tables")
                        if isinstance(item.get("tables"), list)
                        else (media.get("tables") if isinstance(media, dict) else None)
                    )
                    page_id = await self._store_page_record(
                        item_url,
                        title,
                        page_content,
                        content_type="html",
                        source_type=source_type,
                        content_source="crawled_page",
                        redirected_url=str(item.get("redirected_url", "") or ""),
                        status_code=(
                            int(item["status_code"])
                            if item.get("status_code") is not None
                            else None
                        ),
                        full_content_available=True,
                        related_links=related_links,
                        related_links_total=related_links_total,
                        images=images,
                        tables=tables,
                    )
                    if requested_url and self._canonicalize_url(
                        requested_url
                    ) != self._canonicalize_url(item_url):
                        stored_page = await self._cache.get(
                            self._page_id_cache_key(page_id)
                        )
                        if stored_page:
                            await self._cache.setex(
                                self._page_cache_key(requested_url),
                                _ttl_for_url(requested_url),
                                stored_page,
                            )
                    compact = self._build_compact_summary(
                        summary_content or page_content, query or ""
                    )
                    results.append(
                        {
                            "url": item_url,
                            "title": title,
                            "page_id": page_id,
                            "summary": compact["summary"],
                            "key_points": compact["key_points"],
                            "content_length": len(page_content),
                            "images": images,
                            "tables": tables,
                            "content_type": "html",
                            "source_type": source_type,
                            "content_source": "crawled_page",
                            "redirected_url": str(item.get("redirected_url", "") or ""),
                            "status_code": (
                                int(item["status_code"])
                                if item.get("status_code") is not None
                                else None
                            ),
                            "full_content_available": True,
                            "related_links": related_links,
                            "related_links_total": related_links_total,
                            "_content": page_content,
                        }
                    )

                for failed_url in sent_urls - processed_urls:
                    await self._cache.setex(
                        self._dead_cache_key(failed_url),
                        _negative_ttl("timeout"),
                        json.dumps({"reason": "no_result"}),
                    )
                    if self._active_query_metadata is not None:
                        self._record_failed_url(
                            self._active_query_metadata,
                            failed_url,
                            "no_result",
                            stage="crawl_request",
                            recovered_by_single_retry=False,
                        )
                return {"content": results, "images": []}
            except asyncio.TimeoutError as exc:
                last_exc = exc
                await mark_request_failure("timeout")
                if self.valves.DEBUG:
                    logger.warning(
                        f"Crawl4AI request timed out for {endpoint!r} urls={urls!r}: {exc}"
                    )
            except aiohttp.ClientError as exc:
                last_exc = exc
                await mark_request_failure("timeout")
                if self.valves.DEBUG:
                    logger.warning(
                        f"Crawl4AI request failed for {endpoint!r} urls={urls!r}: {exc}"
                    )
            except Exception as exc:
                last_exc = exc
                await mark_request_failure("timeout")
                if self.valves.DEBUG:
                    logger.error(
                        f"Unexpected Crawl4AI error for {endpoint!r} urls={urls!r}: {exc}\n{traceback.format_exc()}"
                    )

        if self.valves.DEBUG and last_exc is not None:
            logger.warning(f"Crawl4AI request degraded for urls={urls!r}: {last_exc}")
        return {"error": "timeout", "details": str(last_exc or "timeout")}
