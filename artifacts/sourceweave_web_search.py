"""
title: SourceWeave Web Search
description: AI web search tool with two explicit actions: search_and_crawl for source discovery and read_page for full-page retrieval. Uses SearXNG for search, Crawl4AI for HTML extraction, and MarkItDown for document conversion.
author: Mohammad ElNaqa
author_url: https://github.com/MRNAQA
version: 0.2.0
license: MIT
requirements: aiohttp, loguru, markitdown, redis>=5.0

Two-tool architecture:
    search_and_crawl(query, depth) -> compact summaries + page_ids (token-cheap discovery)
    read_page(page_ids, focus?)    -> full page content for one or more pages (batch related reads when needed)

Full content is stored in an in-process PageStore and cached in Valkey/Redis.
BM25-style scoring is used for pre-crawl ranking, post-crawl reranking, summary generation, and focused reads.
Supports HTML pages (via Crawl4AI) and documents (PDF/DOCX/etc via MarkItDown).
"""

import asyncio
from collections import Counter
import hashlib
import json
import re
import time
import traceback
from typing import Any, Awaitable, Callable, List, Literal, Mapping, Optional, Union
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse

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
_DEFAULT_PAGE_TTL = 1800
_SEARXNG_HOST_FALLBACK = "http://127.0.0.1:19080/search?format=json&q=<query>"
_CRAWL4AI_HOST_FALLBACK = "http://127.0.0.1:19235"
_REDIS_HOST_FALLBACK = "redis://127.0.0.1:16379/2"

_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "with",
}

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


def _crawler_config_payload(tool: "Tools") -> dict[str, Any]:
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
                        "ignore_links": True,
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

    if not tool.valves.CRAWL4AI_EXTERNAL_DOMAINS:
        params["exclude_external_links"] = True
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


class _CacheClient:
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
        CRAWL4AI_BATCH: int = Field(default=5)
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
                    "name": "search_and_crawl",
                    "description": (
                        "Search the web and crawl pages. Returns compact summaries with page_ids. "
                        "If summaries are not enough, batch one or more page_ids into read_page(page_ids=[...]) for full content. "
                        "Prefer concise retrieval-style queries, quote exact error strings, and use site: when domain preference matters. "
                        "Prefer one batched read_page call over repeated single-page calls when comparing multiple sources."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "The search query. Prefer concise noun phrases over conversational filler, quote exact errors, "
                                    "and add site: filters when you want a specific domain."
                                ),
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
                    "description": (
                        "Get the full cleaned content for one or more pages from prior search_and_crawl results. "
                        "Prefer batching related page_ids in one call instead of calling read_page repeatedly."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "page_ids": {
                                "type": "array",
                                "description": (
                                    "One or more page_ids returned by search_and_crawl. "
                                    "Batch related pages into a single call when you need to compare or synthesize multiple sources."
                                ),
                                "items": {"type": "string"},
                                "minItems": 1,
                            },
                            "focus": {"type": "string", "default": ""},
                            "max_chars": {
                                "type": "integer",
                                "default": 8000,
                                "description": "Maximum number of characters to return per page.",
                            },
                        },
                        "required": ["page_ids"],
                    },
                },
            },
        ]
        self.crawl_counter = 0
        self.content_counter = 0
        logger.info("Web search tool initialized")
        self.total_urls = 0
        self._page_store = _PageStore(max_pages=200, ttl_s=1800)
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
    def _tokenize_text(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", (text or "").lower())

    def _query_terms(self, query: str) -> list[str]:
        return [
            term
            for term in self._tokenize_text(query)
            if term and term not in _QUERY_STOPWORDS
        ]

    @staticmethod
    def _quoted_phrases(query: str) -> list[str]:
        return [phrase.strip() for phrase in re.findall(r'"([^"]+)"', query or "")]

    def _score_text_relevance(self, query: str, text: str) -> float:
        if not query or not text:
            return 0.0

        query_terms = self._query_terms(query)
        if not query_terms:
            return 0.0

        text_lower = text.lower()
        tokens = self._tokenize_text(text_lower)
        if not tokens:
            return 0.0

        counts = Counter(tokens)
        unique_hits = sum(1 for term in set(query_terms) if counts.get(term, 0) > 0)
        density = sum(counts.get(term, 0) for term in query_terms) / max(len(tokens), 1)
        bigram_hits = sum(
            1
            for first, second in zip(query_terms, query_terms[1:])
            if f"{first} {second}" in text_lower
        )
        phrase_hits = sum(
            1 for phrase in self._quoted_phrases(query) if phrase.lower() in text_lower
        )
        return round(
            unique_hits * 1.25 + density * 25.0 + bigram_hits * 0.8 + phrase_hits * 1.5,
            4,
        )

    @staticmethod
    def _url_text(url: str) -> str:
        parsed = urlparse(url)
        host_parts = [
            part
            for part in re.split(r"[^a-z0-9]+", parsed.netloc.lower())
            if part and part not in {"www", "com", "org", "net", "io", "co"}
        ]
        path_parts = [
            part for part in re.split(r"[^a-z0-9]+", parsed.path.lower()) if part
        ]
        return " ".join(host_parts + path_parts)

    def _detect_query_intents(self, query: str) -> set[str]:
        normalized = self._normalize_query(query).lower()
        intents = set()

        if any(token in normalized for token in ("reddit", "forum", "discussion")):
            intents.add("reddit")
        if any(
            token in normalized
            for token in ("docs", "documentation", "developer", "reference", "manual")
        ):
            intents.add("docs")
        if any(token in normalized for token in ("api", "sdk", "graphql", "openapi")):
            intents.add("api")
        if any(
            token in normalized
            for token in (
                "historical",
                "history",
                "archive",
                "time series",
                "timeseries",
            )
        ):
            intents.add("historical")
        if (
            "free" in normalized
            or "open source" in normalized
            or "open-source" in normalized
        ):
            intents.add("free")
        if any(
            token in normalized
            for token in ("no key", "without key", "no api key", "without api key")
        ):
            intents.add("no_key")
        if '"' in normalized or "error" in normalized or "exception" in normalized:
            intents.add("error")
        return intents

    def _focused_query_terms(self, query: str) -> list[str]:
        query_terms = self._query_terms(query)
        if not query_terms:
            return []

        priority_terms = [
            term
            for term in (
                "api",
                "docs",
                "documentation",
                "reference",
                "historical",
                "history",
                "archive",
                "free",
                "public",
                "open",
                "openapi",
                "json",
                "sdk",
                "error",
                "exception",
            )
            if term in query_terms
        ]
        return list(dict.fromkeys(priority_terms + query_terms))

    def _generate_query_variants(self, query: str) -> list[str]:
        normalized = self._normalize_query(query)
        if not normalized:
            return []

        variants = [normalized]
        reduced_terms = self._query_terms(normalized)
        reduced_variant_parts = [
            f'"{phrase}"' for phrase in self._quoted_phrases(normalized) if phrase
        ]
        reduced_variant_parts.extend(reduced_terms)
        reduced_variant = " ".join(dict.fromkeys(reduced_variant_parts))
        if reduced_variant and reduced_variant.lower() != normalized.lower():
            variants.append(reduced_variant)

        intents = self._detect_query_intents(normalized)
        if "site:" not in normalized.lower() and "reddit" in intents:
            site_variant = f"site:reddit.com {reduced_variant or normalized}"
            if site_variant.lower() not in {variant.lower() for variant in variants}:
                variants.append(site_variant)

        focused_variant = " ".join(self._focused_query_terms(normalized))
        if focused_variant and focused_variant.lower() not in {
            variant.lower() for variant in variants
        }:
            variants.append(focused_variant)

        return variants[:3]

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
        discovered_from: Optional[str] = None,
        retrieved_by_queries: Optional[list[str]] = None,
        rank_fusion_score: float = 0.0,
        explicit_order: Optional[int] = None,
    ) -> dict[str, Any]:
        return {
            "url": url,
            "title": title or "",
            "snippet": snippet or "",
            "search_rank": search_rank,
            "engine": engine,
            "source_type": source_type,
            "discovered_from": discovered_from,
            "retrieved_by_queries": retrieved_by_queries or [],
            "rank_fusion_score": rank_fusion_score,
            "pre_crawl_score": 0.0,
            "explicit_order": explicit_order,
        }

    def _normalize_cached_search_candidates(
        self, payload: Any, query: str
    ) -> list[dict[str, Any]]:
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
                raw_discovered_from = item_dict.get("discovered_from")
                discovered_from = (
                    raw_discovered_from
                    if isinstance(raw_discovered_from, str)
                    else None
                )
                raw_queries = item_dict.get("retrieved_by_queries")
                retrieved_by_queries = (
                    [value for value in raw_queries if isinstance(value, str)]
                    if isinstance(raw_queries, list)
                    else []
                )
                raw_rank_fusion_score = item_dict.get("rank_fusion_score")
                rank_fusion_score = (
                    float(raw_rank_fusion_score)
                    if isinstance(raw_rank_fusion_score, (int, float))
                    else 0.0
                )
                candidate = self._build_candidate(
                    raw_url,
                    title=str(item_dict.get("title", "") or ""),
                    snippet=str(item_dict.get("snippet", "") or ""),
                    search_rank=search_rank,
                    engine=engine,
                    source_type=str(
                        item_dict.get("source_type", "search_result") or "search_result"
                    ),
                    discovered_from=discovered_from,
                    retrieved_by_queries=retrieved_by_queries,
                    rank_fusion_score=rank_fusion_score,
                )
            else:
                continue
            candidates.append(candidate)

        for candidate in candidates:
            candidate["pre_crawl_score"] = round(
                self._score_search_candidate(candidate, query), 4
            )

        return sorted(
            candidates,
            key=lambda item: (
                item.get("pre_crawl_score", 0.0),
                item.get("rank_fusion_score", 0.0),
                -(item.get("search_rank") or 10_000),
            ),
            reverse=True,
        )

    def _candidate_heuristic_score(self, candidate: dict, query: str) -> float:
        url = candidate.get("url", "")
        parsed = urlparse(url)
        path = (parsed.path or "/").lower()
        host = parsed.netloc.lower()
        text = " ".join(
            part
            for part in (
                candidate.get("title", ""),
                candidate.get("snippet", ""),
                self._url_text(url),
            )
            if part
        ).lower()
        lexical_score = self._score_text_relevance(
            query, f"{candidate.get('title', '')} {candidate.get('snippet', '')}"
        )
        intents = self._detect_query_intents(query)
        score = 0.0

        if path in {"", "/"}:
            score -= 2.5
            if lexical_score < 2.0:
                score -= 2.0

        if any(
            token in text or token in path
            for token in (
                "pricing",
                "plans",
                "contact sales",
                "contact-sales",
                "signup",
                "sign up",
                "sign-in",
                "login",
                "register",
                "about",
                "careers",
                "privacy",
                "terms",
            )
        ):
            score -= 1.5

        if any(marker in host for marker in ("docs.", "developer.", "api.")):
            score += 2.0
        if any(
            marker in path
            for marker in (
                "/docs",
                "/api",
                "/reference",
                "/guide",
                "/tutorial",
                "/developers",
                "/developer",
                "/sdk",
            )
        ):
            score += 1.5

        if candidate.get("snippet") and len(candidate["snippet"].strip()) < 40:
            score -= 0.5

        if "reddit" in intents and "reddit.com" in host:
            score += 4.0
        if "docs" in intents and (
            any(marker in host for marker in ("docs.", "developer.", "api."))
            or any(
                marker in path
                for marker in ("/docs", "/reference", "/developers", "/manual")
            )
        ):
            score += 2.5
        if "api" in intents and "api" in text:
            score += 1.5
        if "historical" in intents and any(
            marker in text
            for marker in ("historical", "history", "archive", "time series")
        ):
            score += 2.0
        if "free" in intents and any(
            marker in text
            for marker in (
                "free",
                "open source",
                "open-source",
                "open data",
                "community",
            )
        ):
            score += 1.5
        if "free" in intents and any(
            marker in text
            for marker in ("pricing", "plans", "enterprise", "contact sales")
        ):
            score -= 2.0
        if "no_key" in intents and any(
            marker in text
            for marker in (
                "no api key",
                "without api key",
                "no key",
                "unauthenticated",
                "anonymous",
            )
        ):
            score += 3.0
        if "no_key" in intents and any(
            marker in text
            for marker in (
                "api key required",
                "requires api key",
                "sign up",
                "signup",
                "register",
            )
        ):
            score -= 2.5

        return score

    def _score_search_candidate(self, candidate: dict, query: str) -> float:
        if candidate.get("source_type") == "explicit_url":
            return 100.0 + self._score_text_relevance(
                query, self._url_text(candidate["url"])
            )

        title_score = self._score_text_relevance(query, candidate.get("title", ""))
        snippet_score = self._score_text_relevance(query, candidate.get("snippet", ""))
        url_score = self._score_text_relevance(query, self._url_text(candidate["url"]))
        search_rank = candidate.get("search_rank") or 99
        score = title_score * 3.0
        score += snippet_score * 2.0
        score += url_score * 1.25
        score += 6.0 / max(search_rank, 1)
        score += float(candidate.get("rank_fusion_score", 0.0) or 0.0) * 120.0
        score += self._candidate_heuristic_score(candidate, query)
        return round(score, 4)

    def _merge_search_candidates(
        self, query: str, query_results: list[tuple[str, list[dict[str, Any]]]]
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}

        for query_variant, results in query_results:
            for rank, raw_candidate in enumerate(results, start=1):
                url = raw_candidate.get("url")
                if not url:
                    continue

                canonical = self._canonicalize_url(url)
                entry = merged.get(canonical)
                if entry is None:
                    entry = self._build_candidate(
                        url,
                        title=str(raw_candidate.get("title", "") or ""),
                        snippet=str(raw_candidate.get("snippet", "") or ""),
                        search_rank=raw_candidate.get("search_rank") or rank,
                        engine=raw_candidate.get("engine"),
                        source_type=str(
                            raw_candidate.get("source_type", "search_result")
                            or "search_result"
                        ),
                        discovered_from=raw_candidate.get("discovered_from"),
                        retrieved_by_queries=[],
                        rank_fusion_score=0.0,
                    )
                    merged[canonical] = entry

                if raw_candidate.get("title") and len(raw_candidate["title"]) > len(
                    entry.get("title", "")
                ):
                    entry["title"] = raw_candidate["title"]
                if raw_candidate.get("snippet") and len(raw_candidate["snippet"]) > len(
                    entry.get("snippet", "")
                ):
                    entry["snippet"] = raw_candidate["snippet"]
                if raw_candidate.get("search_rank"):
                    current_rank = entry.get("search_rank")
                    candidate_rank = int(raw_candidate["search_rank"])
                    entry["search_rank"] = (
                        min(current_rank, candidate_rank)
                        if current_rank is not None
                        else candidate_rank
                    )
                if query_variant and query_variant not in entry["retrieved_by_queries"]:
                    entry["retrieved_by_queries"].append(query_variant)
                if raw_candidate.get("engine"):
                    engines = {
                        engine.strip()
                        for engine in str(entry.get("engine") or "").split(",")
                        if engine.strip()
                    }
                    engines.update(
                        engine.strip()
                        for engine in str(raw_candidate["engine"]).split(",")
                        if engine.strip()
                    )
                    entry["engine"] = ", ".join(sorted(engines)) if engines else None
                entry["rank_fusion_score"] = float(entry["rank_fusion_score"]) + (
                    1.0 / (50 + rank)
                )

        candidates = list(merged.values())
        for candidate in candidates:
            candidate["pre_crawl_score"] = round(
                self._score_search_candidate(candidate, query), 4
            )

        return sorted(
            candidates,
            key=lambda item: (
                item.get("pre_crawl_score", 0.0),
                item.get("rank_fusion_score", 0.0),
                -(item.get("search_rank") or 10_000),
            ),
            reverse=True,
        )

    def _merge_explicit_candidates(
        self, query: str, candidates: list[dict], urls: Optional[list[str]]
    ) -> list[dict]:
        merged = {
            self._canonicalize_url(candidate["url"]): dict(candidate)
            for candidate in candidates
            if candidate.get("url")
        }

        for explicit_order, raw_url in enumerate(urls or []):
            normalized_raw_url = str(raw_url or "").strip()
            if not normalized_raw_url:
                continue

            url = (
                normalized_raw_url
                if normalized_raw_url.startswith("http")
                else f"https://{normalized_raw_url}"
            )
            canonical = self._canonicalize_url(url)
            existing = merged.get(canonical)
            if existing is None:
                merged[canonical] = self._build_candidate(
                    url,
                    source_type="explicit_url",
                    explicit_order=explicit_order,
                )
            else:
                existing["source_type"] = "explicit_url"
                existing["url"] = url
                current_order = existing.get("explicit_order")
                existing["explicit_order"] = (
                    explicit_order
                    if current_order is None
                    else min(current_order, explicit_order)
                )

        merged_candidates = list(merged.values())
        for candidate in merged_candidates:
            candidate["pre_crawl_score"] = round(
                self._score_search_candidate(candidate, query), 4
            )

        return merged_candidates

    def _rank_candidates(
        self, candidates: list[dict], max_per_domain: int = 3
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
                -item.get("pre_crawl_score", 0.0),
            ),
        )
        ranked_search_candidates = sorted(
            [
                candidate
                for candidate in candidates
                if candidate.get("source_type") != "explicit_url"
            ],
            key=lambda item: (
                item.get("pre_crawl_score", 0.0),
                item.get("rank_fusion_score", 0.0),
                -(item.get("search_rank") or 10_000),
            ),
            reverse=True,
        )
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
                candidate.get("source_type") != "explicit_url"
                and domain_counts.get(host, 0) >= max_per_domain
            ):
                continue

            domain_counts[host] = domain_counts.get(host, 0) + 1
            diversified.append(candidate)

        return diversified

    def _enrich_crawl_result(
        self, result: dict, query: str, candidate: Optional[dict], content: str
    ) -> dict:
        enriched = {
            key: value for key, value in result.items() if not key.startswith("_")
        }
        content_length = enriched.get("content_length", len(content))
        content_type = enriched.get("content_type") or (
            "document" if self._classify_url(enriched["url"]) == "document" else "html"
        )
        search_title = candidate.get("title", "") if candidate else ""
        search_snippet = candidate.get("snippet", "") if candidate else ""
        search_context = " ".join(
            part for part in (search_title, search_snippet) if part
        )

        content_score = self._score_text_relevance(query, content[:8000])
        title_score = self._score_text_relevance(query, enriched.get("title", ""))
        consistency_score = (
            self._score_text_relevance(search_context, enriched.get("title", ""))
            + self._score_text_relevance(search_context, enriched.get("summary", ""))
            if search_context
            else 0.0
        )

        base_score = (
            float(candidate.get("pre_crawl_score", 0.0) if candidate else 0.0) * 0.35
        )
        base_score += content_score * 3.2
        base_score += title_score * 1.8
        base_score += consistency_score * 0.75

        if content_length < 120:
            base_score -= 8.0
        elif content_length < 300:
            base_score -= 3.5

        parsed = urlparse(enriched["url"])
        if (parsed.path or "/") in {"", "/"} and content_score < 2.0:
            base_score -= 2.0

        if candidate and candidate.get("source_type") == "explicit_url":
            base_score += 2.0

        enriched.update(
            {
                "source_type": (
                    candidate.get("source_type")
                    if candidate
                    else enriched.get("source_type", "search_result")
                ),
                "content_type": content_type,
                "search_rank": candidate.get("search_rank") if candidate else None,
                "search_title": search_title,
                "search_snippet": search_snippet,
                "pre_crawl_score": round(
                    float(candidate.get("pre_crawl_score", 0.0) if candidate else 0.0),
                    4,
                ),
                "rank_fusion_score": round(
                    float(
                        candidate.get("rank_fusion_score", 0.0) if candidate else 0.0
                    ),
                    4,
                ),
                "engine": candidate.get("engine") if candidate else None,
                "retrieved_by_queries": (
                    list(candidate.get("retrieved_by_queries", [])) if candidate else []
                ),
                "discovered_from": candidate.get("discovered_from")
                if candidate
                else None,
                "explicit_order": candidate.get("explicit_order")
                if candidate
                else None,
                "post_crawl_score": round(base_score, 4),
            }
        )
        enriched["_post_crawl_base_score"] = base_score
        enriched["_content_match_score"] = content_score
        return enriched

    async def _finalize_crawl_results(
        self,
        crawl_results: list[dict],
        query: str,
        candidates_by_url: dict[str, dict],
        return_limit: int,
    ) -> list[dict]:
        enriched_results = []
        for raw_result in crawl_results:
            content = raw_result.get("_content", "")
            if not content and raw_result.get("page_id"):
                stored = await self._load_page_record(raw_result["page_id"])
                content = stored.get("content", "") if stored else ""

            candidate = candidates_by_url.get(
                self._canonicalize_url(raw_result.get("url", ""))
            )
            enriched_results.append(
                self._enrich_crawl_result(raw_result, query, candidate, content)
            )

        pre_sorted = sorted(
            enriched_results,
            key=lambda item: (
                item.get("_post_crawl_base_score", 0.0),
                item.get("pre_crawl_score", 0.0),
            ),
            reverse=True,
        )

        explicit_results = []
        non_explicit_results = []
        for item in pre_sorted:
            if item.get("source_type") == "explicit_url":
                item["post_crawl_score"] = round(
                    item.get("_post_crawl_base_score", 0.0), 4
                )
                item.pop("_post_crawl_base_score", None)
                item.pop("_content_match_score", None)
                explicit_results.append(item)
                continue
            non_explicit_results.append(item)

        explicit_results.sort(
            key=lambda item: (
                item.get("explicit_order")
                if item.get("explicit_order") is not None
                else 10_000,
                -item.get("post_crawl_score", 0.0),
            )
        )

        ranked_results = []
        domain_counts: dict[str, int] = {}
        for item in explicit_results:
            host = urlparse(item["url"]).netloc.lower().lstrip("www.")
            domain_counts[host] = domain_counts.get(host, 0) + 1

        for item in non_explicit_results:
            host = urlparse(item["url"]).netloc.lower().lstrip("www.")
            domain_penalty = domain_counts.get(host, 0) * 1.25
            final_score = item.get("_post_crawl_base_score", 0.0) - domain_penalty
            content_match = item.get("_content_match_score", 0.0)

            if (
                item.get("source_type") != "explicit_url"
                and item.get("content_length", 0) < 120
                and final_score < 6.0
            ):
                continue
            if (
                item.get("source_type") != "explicit_url"
                and content_match <= 0.0
                and item.get("pre_crawl_score", 0.0) < 5.0
            ):
                continue

            item["post_crawl_score"] = round(final_score, 4)
            item.pop("_post_crawl_base_score", None)
            item.pop("_content_match_score", None)
            ranked_results.append(item)
            domain_counts[host] = domain_counts.get(host, 0) + 1

        if not ranked_results:
            ranked_results = non_explicit_results
            for item in ranked_results:
                item["post_crawl_score"] = round(
                    item.get("_post_crawl_base_score", 0.0), 4
                )
                item.pop("_post_crawl_base_score", None)
                item.pop("_content_match_score", None)

        ranked_results = sorted(
            ranked_results,
            key=lambda item: (
                item.get("post_crawl_score", 0.0),
                item.get("pre_crawl_score", 0.0),
            ),
            reverse=True,
        )
        return (explicit_results + ranked_results)[:return_limit]

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

        last_exc = None
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
                            retrieved_by_queries=[normalized_query],
                        )
                    )
                return structured_results
            except Exception as exc:
                last_exc = exc

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
            providers.append("searxng:v2")
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
        domain_counts: dict[str, int] = {}
        result: list[str] = []
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

    async def _load_page_record(self, page_id: str) -> Optional[dict]:
        record = self._page_store.get(page_id)
        if record:
            return record

        cached_page = await self._cache.get(self._page_id_cache_key(page_id))
        if not cached_page:
            return None

        try:
            cached_record = json.loads(cached_page)
            cached_page_id = self._page_store.put(
                cached_record["url"],
                cached_record.get("title", ""),
                cached_record["content"],
            )
            if cached_page_id == page_id:
                return self._page_store.get(page_id)
        except Exception:
            return None

        return None

    async def _read_single_page(
        self,
        page_id: str,
        focus: str = "",
        max_chars: int = 8000,
        __event_emitter__: EventEmitter = None,
    ) -> dict:
        record = await self._load_page_record(page_id)
        if not record:
            return {
                "page_id": page_id,
                "error": f"page_id '{page_id}' not found or expired. Call search_and_crawl again.",
            }

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"Read {record['title'][:60]}...",
                        "done": True,
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
        return {
            "page_id": page_id,
            "url": record["url"],
            "title": record["title"],
            "content": content,
        }

    async def read_page(
        self,
        page_ids: Union[str, List[str]],
        focus: str = "",
        max_chars: int = 8000,
        __event_emitter__: EventEmitter = None,
    ) -> dict:
        if isinstance(page_ids, str):
            single_result = await self._read_single_page(
                page_ids,
                focus=focus,
                max_chars=max_chars,
                __event_emitter__=__event_emitter__,
            )
            if "error" in single_result:
                return {"error": single_result["error"]}
            return single_result

        normalized_page_ids = []
        seen_page_ids = set()
        for page_id in page_ids:
            normalized_page_id = str(page_id).strip()
            if not normalized_page_id or normalized_page_id in seen_page_ids:
                continue
            seen_page_ids.add(normalized_page_id)
            normalized_page_ids.append(normalized_page_id)

        if not normalized_page_ids:
            return {"error": "page_ids must contain at least one non-empty page_id."}

        pages = []
        errors = []
        for page_id in normalized_page_ids:
            page_result = await self._read_single_page(
                page_id,
                focus=focus,
                max_chars=max_chars,
                __event_emitter__=__event_emitter__,
            )
            if "error" in page_result:
                errors.append({"page_id": page_id, "error": page_result["error"]})
                continue
            pages.append(page_result)

        return {
            "pages": pages,
            "errors": errors,
            "requested_page_ids": normalized_page_ids,
            "returned_pages": len(pages),
        }

    async def search_and_crawl(
        self,
        query: str,
        urls: Optional[List[str]] = None,
        depth: str = "normal",
        max_results: Optional[int] = None,
        fresh: bool = False,
        __event_emitter__: EventEmitter = None,
    ) -> list[dict[str, Any]]:
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

        requested_results = max_results if max_results and max_results > 0 else None
        effective_return_limit = (
            min(requested_results, budget["search_candidates"])
            if requested_results is not None
            else budget["return_limit"]
        )
        query = self._normalize_query(query)
        self.crawl_counter = 0
        self.content_counter = 0
        self.total_urls = 0

        if __event_emitter__ and str(self.valves.INITIAL_RESPONSE).strip() != "":
            await __event_emitter__(
                {
                    "type": "chat:message:delta",
                    "data": {"content": str(self.valves.INITIAL_RESPONSE).strip()},
                }
            )

        search_cache_key = self._search_cache_key(query)
        cached_search = None if fresh else await self._cache.get(search_cache_key)
        search_candidates: list[dict[str, Any]] = []
        if cached_search:
            self._cache_stats["search_hits"] += 1
            try:
                search_candidates = self._normalize_cached_search_candidates(
                    json.loads(cached_search), query
                )
            except Exception:
                search_candidates = []
        else:
            self._cache_stats["search_misses"] += 1
            query_variants = self._generate_query_variants(query)
            search_tasks = (
                [
                    self._search_searxng(query_variant, __event_emitter__)
                    for query_variant in query_variants
                ]
                if self.valves.SEARCH_WITH_SEARXNG
                else []
            )
            if search_tasks:
                try:
                    search_results = await asyncio.wait_for(
                        asyncio.gather(*search_tasks, return_exceptions=True),
                        timeout=min(budget["search_timeout"], time_left()),
                    )
                    merged_query_results = []
                    for query_variant, result in zip(query_variants, search_results):
                        if isinstance(result, list):
                            merged_query_results.append((query_variant, result))
                    search_candidates = self._merge_search_candidates(
                        query, merged_query_results
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Search providers timed out, proceeding with what we have"
                    )
            if search_candidates:
                await self._cache.setex(
                    search_cache_key, 600, json.dumps(search_candidates)
                )

        ranked_candidates = self._rank_candidates(
            self._merge_explicit_candidates(query, search_candidates, urls),
            max_per_domain=2 if depth in ("quick", "normal") else 3,
        )
        ranked_candidates = ranked_candidates[
            : max(budget["search_candidates"], len(urls or []))
        ]
        self.total_urls = len(ranked_candidates)
        if not ranked_candidates:
            return []

        crawl_limit = min(
            budget["search_candidates"],
            max(
                budget["crawl_limit"],
                effective_return_limit + budget["crawl_slack"],
                len(urls or []),
            ),
        )
        crawl_results: list[dict[str, Any]] = []
        candidates_by_url: dict[str, dict[str, Any]] = {}
        candidates_to_fetch: list[dict[str, Any]] = []
        for candidate in ranked_candidates:
            if len(crawl_results) + len(candidates_to_fetch) >= crawl_limit:
                break

            url = candidate["url"]
            canonical = self._canonicalize_url(url)
            candidates_by_url[canonical] = candidate

            if fresh:
                candidates_to_fetch.append(candidate)
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
                            "content_type": (
                                "document"
                                if self._classify_url(record["url"]) == "document"
                                else "html"
                            ),
                            "_content": record["content"],
                        }
                    )
                    continue
                except Exception:
                    pass
            self._cache_stats["page_misses"] += 1
            candidates_to_fetch.append(candidate)

        html_candidates = []
        document_candidates = []
        for candidate in candidates_to_fetch:
            if self._classify_url(candidate["url"]) == "document":
                document_candidates.append(candidate)
            else:
                html_candidates.append(candidate)

        for i in range(0, len(html_candidates), self.valves.CRAWL4AI_BATCH):
            remaining = time_left()
            if remaining < 2.0:
                break
            batch = html_candidates[i : i + self.valves.CRAWL4AI_BATCH]
            try:
                crawled_batch = await self._crawl_url(
                    urls=[candidate["url"] for candidate in batch],
                    query=query,
                    timeout_s=min(
                        remaining - 1, self.valves.CRAWL4AI_TIMEOUT * len(batch)
                    ),
                    __event_emitter__=__event_emitter__,
                )
                crawl_results.extend(crawled_batch.get("content", []))
            except Exception as exc:
                logger.error(f"Batch crawl error: {exc}\n{traceback.format_exc()}")

        if document_candidates and depth != "quick" and time_left() > 5:
            doc_concurrency = 2 if depth == "normal" else 4
            doc_batch = document_candidates
            remaining_for_docs = time_left() - 2
            sem = asyncio.Semaphore(doc_concurrency)

            async def bounded_fetch(candidate: dict):
                async with sem:
                    return await self._fetch_document(
                        candidate["url"],
                        query=query,
                        timeout_s=min(
                            remaining_for_docs, self.valves.DOCUMENT_FETCH_TIMEOUT
                        ),
                        __event_emitter__=__event_emitter__,
                    )

            doc_tasks = [bounded_fetch(candidate) for candidate in doc_batch]
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

        return await self._finalize_crawl_results(
            crawl_results,
            query=query,
            candidates_by_url=candidates_by_url,
            return_limit=effective_return_limit,
        )

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
                "content_type": "document",
                "_content": content,
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

        payload = {
            "urls": urls,
            "browser_config": _browser_config_payload(),
            "crawler_config": _crawler_config_payload(self),
        }

        last_exc = None
        last_traceback = ""
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
                        continue

                    markdown_data = item.get("markdown", "")
                    if isinstance(markdown_data, dict):
                        page_content = markdown_data.get(
                            "fit_markdown", ""
                        ) or markdown_data.get("raw_markdown", "")
                    else:
                        page_content = str(markdown_data)
                    title = item.get("metadata", {}).get("title", "")
                    page_id = await self._store_page_record(
                        item_url, title, page_content
                    )
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
                            "content_type": "html",
                            "_content": page_content,
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
                last_exc = exc
                last_traceback = traceback.format_exc()

        logger.error(f"An unexpected error occurred: {last_exc}\n{last_traceback}")
        return {"error": str(last_exc), "details": str(last_exc)}
