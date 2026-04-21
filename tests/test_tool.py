"""Standalone runtime checks that call the tool directly."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sourceweave_web_search.tool import (
    Tools,
    _crawler_config_payload,
    _markdown_content_variants,
    _negative_ttl,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _install_memory_cache(
    monkeypatch: pytest.MonkeyPatch, tool: Tools
) -> dict[str, str]:
    store: dict[str, str] = {}

    async def fake_get(key: str) -> str | None:
        return store.get(key)

    async def fake_setex(key: str, ttl_s: int, value: str) -> None:
        _ = ttl_s
        store[key] = value

    async def fake_exists(key: str) -> bool:
        return key in store

    monkeypatch.setattr(tool._cache, "get", fake_get)
    monkeypatch.setattr(tool._cache, "setex", fake_setex)
    monkeypatch.setattr(tool._cache, "exists", fake_exists)
    return store


def _make_crawl_result(
    tool: Tools,
    url: str,
    title: str,
    content: str,
    query: str,
    *,
    content_type: str = "html",
    content_source: str = "crawled_page",
    full_content_available: bool = True,
    related_links: list[dict[str, str]] | None = None,
    related_links_total: int = 0,
    images: list[dict[str, str]] | None = None,
    tables: list[dict[str, Any]] | None = None,
    source_type: str = "search_result",
) -> dict[str, Any]:
    page_id = Tools._page_id_for_url(url)
    compact = tool._build_compact_summary(content, query)
    return {
        "url": url,
        "title": title,
        "page_id": page_id,
        "summary": compact["summary"],
        "key_points": compact["key_points"],
        "content_length": len(content),
        "images": list(images or []),
        "tables": list(tables or []),
        "content_type": content_type,
        "source_type": source_type,
        "content_source": content_source,
        "full_content_available": full_content_available,
        "related_links": list(related_links or []),
        "related_links_total": related_links_total,
        "_content": content,
    }


async def _store_test_page(
    tool: Tools,
    url: str,
    title: str,
    content: str,
    *,
    content_type: str = "html",
    source_type: str = "search_result",
    content_source: str = "crawled_page",
    full_content_available: bool = True,
    related_links: list[dict[str, str]] | None = None,
    related_links_total: int = 0,
    images: list[dict[str, str]] | None = None,
    tables: list[dict[str, Any]] | None = None,
) -> str:
    return await tool._store_page_record(
        url,
        title,
        content,
        content_type=content_type,
        source_type=source_type,
        content_source=content_source,
        full_content_available=full_content_available,
        related_links=related_links,
        related_links_total=related_links_total,
        images=images,
        tables=tables,
    )


@pytest.mark.integration
def test_search_and_read_round_trip() -> None:
    async def scenario() -> None:
        tool = Tools()
        results = await tool._search_web_internal(
            query="python programming",
            depth="quick",
            max_results=2,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert results, "search_web returned no results"

        first = results[0]
        for key in (
            "url",
            "title",
            "page_id",
            "summary",
            "key_points",
            "content_type",
            "source_type",
            "content_source",
            "full_content_available",
        ):
            assert key in first, f"missing key '{key}' in result: {first}"

        page = await tool._read_pages_internal(first["page_id"], max_chars=1200)
        assert "error" not in page, page
        assert len(page.get("content", "")) >= 200, page

    asyncio.run(scenario())


@pytest.mark.integration
def test_read_pages_works_from_fresh_tool_instance() -> None:
    async def scenario() -> None:
        tool = Tools()
        results = await tool._search_web_internal(
            query="asyncio python tutorial",
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert results, "search_web returned no results"

        page_id = results[0]["page_id"]
        fresh_tool = Tools()
        page = await fresh_tool._read_pages_internal(page_id, max_chars=1000)
        assert "error" not in page, page
        assert len(page.get("content", "")) >= 200, page

    asyncio.run(scenario())


def test_read_pages_accepts_multiple_page_ids_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        first_page_id = await _store_test_page(
            tool,
            "https://example.com/alpha",
            "Alpha",
            "# Alpha\n\nAlpha content paragraph with enough text to be useful for a multi-page read.",
        )
        second_page_id = await _store_test_page(
            tool,
            "https://example.com/beta",
            "Beta",
            "# Beta\n\nBeta content paragraph with enough text to be useful for a multi-page read.",
        )

        pages = await tool._read_pages_internal(
            [first_page_id, second_page_id], max_chars=200
        )

        assert "error" not in pages, pages
        assert pages["requested_page_ids"] == [first_page_id, second_page_id], pages
        assert pages["returned_pages"] == 2, pages
        assert not pages["errors"], pages
        assert [page["page_id"] for page in pages["pages"]] == [
            first_page_id,
            second_page_id,
        ], pages
        assert all(page["content"] for page in pages["pages"]), pages

    asyncio.run(scenario())


def test_read_pages_accepts_direct_url_without_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        url = "https://example.com/guide"
        crawl_calls: list[list[str]] = []

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = (timeout_s, cache_mode, source_type, __event_emitter__)
            crawl_calls.append(list(urls_to_fetch))
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        "Guide",
                        "# Guide\n\nDirect URL content.",
                        query or "",
                    )
                ]
            }

        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        page = await tool._read_pages_internal(urls=[url], max_chars=500)

        assert page["url"] == url, page
        assert "error" not in page, page
        assert crawl_calls == [[url]], crawl_calls

    asyncio.run(scenario())


def test_read_pages_accepts_direct_document_url_with_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        pdf_url = "https://example.com/guide.pdf"
        fetch_calls: list[str] = []

        async def fake_fetch_document(
            url: str,
            query: str = "",
            timeout_s: float | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, source_type, __event_emitter__
            fetch_calls.append(url)
            return _make_crawl_result(
                tool,
                url,
                "Guide PDF",
                "# Guide PDF\n\nConverted document content.",
                query,
                content_type="document",
                content_source="converted_document",
            )

        monkeypatch.setattr(tool, "_fetch_document", fake_fetch_document)

        page = await tool._read_pages_internal(urls=[pdf_url], max_chars=500)

        assert fetch_calls == [pdf_url], fetch_calls
        assert page["url"] == pdf_url, page
        assert page["content_type"] == "document", page
        assert page["content_source"] == "converted_document", page

    asyncio.run(scenario())


def test_read_pages_returns_error_when_direct_document_conversion_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        pdf_url = "https://example.com/guide.pdf"

        async def fake_fetch_document(
            url: str,
            query: str = "",
            timeout_s: float | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any] | None:
            _ = url, query, timeout_s, source_type, __event_emitter__
            return None

        monkeypatch.setattr(tool, "_fetch_document", fake_fetch_document)

        page = await tool._read_pages_internal(urls=[pdf_url], max_chars=500)

        assert page == {"error": f"Failed to read URL '{pdf_url}'."}, page

    asyncio.run(scenario())


def test_read_pages_multi_returns_partial_errors_for_missing_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        first_page_id = await _store_test_page(
            tool,
            "https://example.com/gamma",
            "Gamma",
            "# Gamma\n\nGamma content paragraph with enough text to survive truncation.",
        )

        pages = await tool._read_pages_internal(
            [first_page_id, "missing-page-id"], max_chars=200
        )

        assert pages["returned_pages"] == 1, pages
        assert len(pages["pages"]) == 1, pages
        assert pages["pages"][0]["page_id"] == first_page_id, pages
        assert pages["errors"] == [
            {
                "page_id": "missing-page-id",
                "error": "page_id 'missing-page-id' not found or expired. Call search_web again.",
            }
        ], pages

    asyncio.run(scenario())


def test_read_pages_returns_content_state_and_related_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        page_id = await _store_test_page(
            tool,
            "https://example.com/guide",
            "Guide",
            "# Guide\n\nThis section explains the guide in detail.\n\n"
            "## Focus\n\nThis section is specifically about focus extraction and should be retained.\n\n"
            "## Extra\n\nAdditional material that makes the content long enough to be truncated.",
            content_type="html",
            source_type="explicit_url",
            content_source="crawled_page",
            full_content_available=True,
            related_links=[
                {"url": "https://example.com/related-a", "text": "Related A"}
            ],
            related_links_total=2,
            images=[{"url": "https://example.com/image.png", "alt": "Guide image"}],
        )

        page = await tool._read_pages_internal(
            page_id,
            focus="focus extraction",
            related_links_limit=1,
            max_chars=90,
        )

        assert page["page_id"] == page_id, page
        assert page["content_type"] == "html", page
        assert page["source_type"] == "explicit_url", page
        assert page["content_source"] == "crawled_page", page
        assert page["full_content_available"] is True, page
        assert page["focus_applied"] is True, page
        assert page["truncated"] is True, page
        assert "page_quality" not in page, page
        assert page["related_links"] == [
            {"url": "https://example.com/related-a", "text": "Related A"}
        ], page
        assert page["related_links_total"] == 2, page
        assert page["related_links_more_available"] is True, page
        assert page["images"] == [
            {"url": "https://example.com/image.png", "alt": "Guide image"}
        ], page

    asyncio.run(scenario())


def test_search_web_and_read_pages_mark_challenge_pages() -> None:
    async def scenario() -> None:
        tool = Tools()
        url = "https://www.reddit.com/r/reactjs/comments/example"

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    url,
                    title="Reddit - Prove your humanity",
                    snippet="Challenge page",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            assert urls_to_fetch == [url]
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        "Reddit - Prove your humanity",
                        "# Prove your humanity\n\nWe are committed to safety and security. But not for bots. Complete the challenge below and let us know you are a real person.",
                        query or "",
                        source_type=source_type,
                    )
                ]
            }

        monkeypatch = pytest.MonkeyPatch()
        _install_memory_cache(monkeypatch, tool)
        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        try:
            results = await tool._search_web_internal(
                query="react useEffect cleanup",
                depth="quick",
                max_results=1,
                fresh=True,
            )
            assert results[0]["page_quality"] == "challenge", results

            page = await tool._read_pages_internal(results[0]["page_id"], max_chars=400)
            assert page["page_quality"] == "challenge", page
        finally:
            monkeypatch.undo()

    asyncio.run(scenario())


def test_read_pages_marks_blocked_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        page_id = await _store_test_page(
            tool,
            "https://example.com/blocked",
            "Access denied",
            "Access denied. You do not have permission to access this resource. Error code: 1020.",
            content_type="html",
            content_source="crawled_page",
            full_content_available=True,
        )

        page = await tool._read_pages_internal(page_id, max_chars=500)

        assert page["page_quality"] == "blocked", page

    asyncio.run(scenario())


def test_normalize_related_links_skips_challenge_login_and_navigation_junk() -> None:
    base_url = "https://www.reddit.com/r/reactjs/comments/example"
    links, total = Tools._normalize_related_links(
        base_url,
        {
            "internal": [
                {
                    "href": "/r/reactjs/comments/example?js_challenge=1&token=abc",
                    "text": "Skip to main content",
                },
                {"href": "/login/", "text": "Log In"},
                {"href": "/", "text": "Home"},
                {"href": "/r/reactjs/wiki", "text": "Wiki"},
                {"href": "/r/reactjs/comments/next", "text": "Next thread"},
            ],
            "external": [
                {
                    "href": "https://react.dev/reference/react/useEffect",
                    "text": "React docs",
                }
            ],
        },
        limit=5,
    )

    assert total == 3
    assert links == [
        {"url": "https://www.reddit.com/r/reactjs/wiki", "text": "Wiki"},
        {
            "url": "https://www.reddit.com/r/reactjs/comments/next",
            "text": "Next thread",
        },
        {"url": "https://react.dev/reference/react/useEffect", "text": "React docs"},
    ]


def test_explicit_urls_preserve_input_order_ahead_of_search_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        explicit_urls = [
            "https://docs.python.org/3/tutorial/",
            "https://docs.python.org/3/library/",
        ]
        search_url = "https://docs.python.org/3/whatsnew/3.12.html"

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    search_url,
                    title="What's New In Python 3.12",
                    snippet="Release highlights and new features for Python 3.12.",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            page_bodies = {
                explicit_urls[0]: (
                    "Python Tutorial",
                    "# Python Tutorial\n\nLearn the Python tutorial from the official docs.",
                ),
                explicit_urls[1]: (
                    "Python Standard Library",
                    "# Python Standard Library\n\nReference documentation for the standard library.",
                ),
                search_url: (
                    "What's New In Python 3.12",
                    "# What's New In Python 3.12\n\nFeature highlights and upgrade notes for Python 3.12.",
                ),
            }
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        *page_bodies[url],
                        query or "",
                        source_type=source_type,
                    )
                    for url in urls
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="python reference pages",
            urls=explicit_urls,
            depth="quick",
            max_results=2,
            fresh=True,
        )

        assert [result["url"] for result in results] == explicit_urls, results
        assert all(result["source_type"] == "explicit_url" for result in results), (
            results
        )

    asyncio.run(scenario())


def test_explicit_url_failure_returns_search_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        explicit_url = "https://www.python.org/about/"
        search_url = "https://www.python.org/community/"

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    search_url,
                    title="Python Community",
                    snippet="Community resources, events, and Python Software Foundation links.",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = (timeout_s, cache_mode, source_type, __event_emitter__)
            results = []
            if search_url in urls:
                results.append(
                    _make_crawl_result(
                        tool,
                        search_url,
                        "Python Community",
                        "# Python Community\n\nThe Python community page collects news, events, and ways to get involved.",
                        query or "",
                    )
                )
            return {"content": results, "images": []}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="python about page",
            urls=[explicit_url],
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert [result["url"] for result in results] == [explicit_url], results
        assert results[0]["source_type"] == "explicit_url", results[0]
        assert results[0]["fallback_reason"] == "search_only", results[0]
        assert results[0]["content_source"] == "search_snippet", results[0]
        assert results[0]["full_content_available"] is False, results[0]

    asyncio.run(scenario())


def test_search_web_returns_empty_list_when_no_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return []

        monkeypatch.setattr(tool, "_search_searxng", fake_search)

        results = await tool._search_web_internal(
            query="nothing should be found",
            depth="quick",
            fresh=True,
        )

        assert results == [], results

    asyncio.run(scenario())


def test_search_web_uses_single_search_call_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        query = "react useEffect cleanup example official documentation"
        urls = [
            "https://react.dev/reference/react/useEffect",
            "https://legacy.reactjs.org/docs/hooks-effect.html",
            "https://reacttraining.com/blog/useEffect-cleanup",
        ]
        seen_queries: list[str] = []

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = __event_emitter__
            seen_queries.append(query_value)
            return [
                tool._build_candidate(
                    url,
                    title=f"Title {idx}",
                    snippet=f"Snippet {idx}",
                    search_rank=idx,
                )
                for idx, url in enumerate(urls, start=1)
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            url = urls_to_fetch[0]
            idx = urls.index(url) + 1
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        f"Crawled {idx}",
                        f"# Crawled {idx}\n\nUseful crawled content for result {idx}.",
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query=query,
            depth="quick",
            max_results=3,
            fresh=True,
        )

        assert seen_queries == [query], seen_queries
        assert [result["url"] for result in results] == urls, results
        assert [result["search_rank"] for result in results] == [1, 2, 3], results
        assert all(result["content_type"] == "html" for result in results), results

    asyncio.run(scenario())


def test_search_web_fetches_html_pages_one_by_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        urls = [
            "https://docs.example.com/react/use-effect",
            "https://docs.example.com/react/use-layout-effect",
        ]
        crawl_calls: list[list[str]] = []

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    url,
                    title=f"Doc for {idx}",
                    snippet="Official React documentation example.",
                    search_rank=idx,
                )
                for idx, url in enumerate(urls, start=1)
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            url = urls_to_fetch[0]
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        "React Effect Docs",
                        "# React Effect Docs\n\nOfficial cleanup documentation example.",
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=2,
            fresh=True,
        )

        assert len(results) == 2, results
        assert crawl_calls == [[urls[0]], [urls[1]]], crawl_calls

    asyncio.run(scenario())


def test_search_web_falls_back_to_search_snippet_when_crawl_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        search_url = "https://react.dev/reference/react/useEffect"

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    search_url,
                    title="useEffect - React",
                    snippet="Official React documentation covering useEffect cleanup behavior.",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = (
                urls_to_fetch,
                query,
                timeout_s,
                cache_mode,
                source_type,
                __event_emitter__,
            )
            return {"error": "timeout", "details": "timeout"}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert len(results) == 1, results
        assert results[0]["url"] == search_url, results
        assert results[0]["fallback_reason"] == "search_only", results[0]
        assert results[0]["content_type"] == "search_result", results[0]
        assert results[0]["content_source"] == "search_snippet", results[0]
        assert results[0]["full_content_available"] is False, results[0]
        assert "related_links" not in results[0], results[0]

    asyncio.run(scenario())


def test_search_web_records_metadata_for_per_url_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        first_url = "https://react.dev/reference/react/useEffect"
        second_url = "https://blog.example.com/react-cleanup"

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    first_url,
                    title="useEffect - React",
                    snippet="Official React documentation covering useEffect cleanup behavior.",
                    search_rank=1,
                ),
                tool._build_candidate(
                    second_url,
                    title="React cleanup article",
                    snippet="Third-party article about cleanup behavior.",
                    search_rank=2,
                ),
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            url = urls_to_fetch[0]
            if url == second_url:
                if tool._active_query_metadata is not None:
                    tool._record_failed_url(
                        tool._active_query_metadata,
                        second_url,
                        "timeout",
                        stage="crawl",
                        recovered_by_single_retry=False,
                    )
                return {"error": "timeout", "details": "timeout"}
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        first_url,
                        "useEffect - React",
                        "# useEffect\n\nOfficial React documentation covering cleanup behavior.",
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=2,
            fresh=True,
        )

        metadata = tool.last_query_metadata
        assert len(results) == 2, results
        assert metadata["search"]["candidate_count"] == 2, metadata
        assert metadata["crawl"]["attempted"] == 2, metadata
        assert metadata["crawl"]["failed"] == 1, metadata
        assert metadata["fallbacks_used"] == ["search_only"], metadata
        assert results[0]["url"] == first_url, results
        assert results[1]["url"] == second_url, results
        assert results[1]["fallback_reason"] == "search_only", results
        assert any(
            item["url"] == second_url for item in metadata["crawl"]["failed_urls"]
        ), metadata

    asyncio.run(scenario())


def test_search_web_reuses_cached_crawled_page_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        search_url = "https://react.dev/reference/react/useEffect"
        search_calls: list[str] = []
        crawl_calls: list[list[str]] = []

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = __event_emitter__
            search_calls.append(query_value)
            return [
                tool._build_candidate(
                    search_url,
                    title="useEffect - React",
                    snippet="Official React documentation covering useEffect cleanup behavior.",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        search_url,
                        "useEffect - React",
                        "# useEffect\n\nFull page content saved during search_web.",
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        first_results = await tool._search_web_internal(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=1,
            fresh=False,
        )
        second_results = await tool._search_web_internal(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=1,
            fresh=False,
        )

        assert search_calls == [
            "react useEffect cleanup example official documentation"
        ], search_calls
        assert crawl_calls == [[search_url]], crawl_calls
        assert first_results[0]["page_id"] == second_results[0]["page_id"], (
            first_results,
            second_results,
        )
        assert second_results[0]["content_source"] == "crawled_page", second_results
        assert second_results[0]["full_content_available"] is True, second_results

    asyncio.run(scenario())


def test_search_web_does_not_reuse_search_only_fallback_as_full_page_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        search_url = "https://react.dev/reference/react/useEffect"
        crawl_calls: list[list[str]] = []

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    search_url,
                    title="useEffect - React",
                    snippet="Official React documentation covering useEffect cleanup behavior.",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, source_type, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {"error": "timeout", "details": "timeout"}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        first_results = await tool._search_web_internal(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=1,
            fresh=False,
        )
        assert first_results[0]["fallback_reason"] == "search_only", first_results

        crawl_calls.clear()

        async def succeeding_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        search_url,
                        "useEffect - React",
                        "# useEffect\n\nNow we have real crawled page content.",
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_crawl_url", succeeding_crawl)

        second_results = await tool._search_web_internal(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=1,
            fresh=False,
        )

        assert crawl_calls == [[search_url]], crawl_calls
        assert second_results[0]["content_source"] == "crawled_page", second_results
        assert second_results[0]["full_content_available"] is True, second_results
        assert "fallback_reason" not in second_results[0], second_results

    asyncio.run(scenario())


def test_search_web_reuses_redirected_page_for_original_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        original_url = "https://example.com/docs/latest/"
        final_url = "https://example.com/docs/latest"
        crawl_calls: list[list[str]] = []

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    original_url,
                    title="Docs",
                    snippet="Latest docs.",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {
                "content": [
                    {
                        **_make_crawl_result(
                            tool,
                            final_url,
                            "Docs",
                            "# Docs\n\nCanonical content.",
                            query or "",
                            source_type=source_type,
                        ),
                        "redirected_url": final_url,
                    }
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        first_results = await tool._search_web_internal(
            query="latest docs",
            depth="quick",
            max_results=1,
            fresh=False,
        )
        second_results = await tool._search_web_internal(
            query="latest docs",
            depth="quick",
            max_results=1,
            fresh=False,
        )

        assert crawl_calls == [[original_url]], crawl_calls
        assert first_results[0]["url"] == final_url, first_results
        assert second_results[0]["url"] == final_url, second_results
        assert first_results[0]["page_id"] == second_results[0]["page_id"], (
            first_results,
            second_results,
        )

    asyncio.run(scenario())


def test_search_web_auto_converts_explicit_document_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        pdf_url = "https://example.com/guide.pdf"
        fetch_calls: list[str] = []
        crawl_calls: list[list[str]] = []

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return []

        async def fake_fetch_document(
            url: str,
            query: str = "",
            timeout_s: float | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = query, timeout_s, __event_emitter__
            fetch_calls.append(url)
            result = _make_crawl_result(
                tool,
                url,
                "Guide PDF",
                "# Guide PDF\n\nConverted document content.",
                "guide pdf",
                content_type="document",
                content_source="converted_document",
                source_type=source_type,
            )
            return result

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = query, timeout_s, cache_mode, source_type, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {"content": [], "images": []}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_fetch_document", fake_fetch_document)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="guide pdf",
            urls=[pdf_url],
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert fetch_calls == [pdf_url], fetch_calls
        assert crawl_calls == [], crawl_calls
        assert results[0]["url"] == pdf_url, results
        assert results[0]["content_type"] == "document", results[0]
        assert results[0]["content_source"] == "converted_document", results[0]
        assert results[0]["source_type"] == "explicit_url", results[0]

    asyncio.run(scenario())


def test_search_web_auto_converts_document_search_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        pdf_url = "https://example.com/guide.pdf"
        fetch_calls: list[str] = []

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    pdf_url,
                    title="Guide PDF",
                    snippet="A downloadable PDF guide.",
                    search_rank=1,
                )
            ]

        async def fake_fetch_document(
            url: str,
            query: str = "",
            timeout_s: float | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
            fetch_calls.append(url)
            return _make_crawl_result(
                tool,
                url,
                "Guide PDF",
                "# Guide PDF\n\nConverted document content.",
                query,
                content_type="document",
                content_source="converted_document",
                source_type=source_type,
            )

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_fetch_document", fake_fetch_document)

        results = await tool._search_web_internal(
            query="guide pdf",
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert fetch_calls == [pdf_url], fetch_calls
        assert results[0]["url"] == pdf_url, results
        assert results[0]["content_type"] == "document", results[0]
        assert results[0]["content_source"] == "converted_document", results[0]

    asyncio.run(scenario())


def test_search_web_honors_site_filters_after_search_provider_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    "https://docs.python.org/3/library/asyncio.html",
                    title="asyncio docs",
                    snippet="Official Python docs.",
                    search_rank=1,
                ),
                tool._build_candidate(
                    "https://example.com/python-asyncio-guide",
                    title="Third-party guide",
                    snippet="Community write-up.",
                    search_rank=2,
                ),
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        urls_to_fetch[0],
                        "asyncio docs",
                        "# asyncio\n\nOfficial Python asyncio docs.",
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="site:docs.python.org asyncio",
            depth="quick",
            max_results=5,
            fresh=True,
        )

        assert len(results) == 1, results
        assert results[0]["url"] == "https://docs.python.org/3/library/asyncio.html", (
            results
        )

    asyncio.run(scenario())


def test_read_pages_apply_related_links_limit_and_can_omit_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        url = "https://example.com/guide"
        related_links = [
            {"url": "https://example.com/related-a", "text": "Related A"},
            {"url": "https://example.com/related-b", "text": "Related B"},
        ]
        images = [
            {
                "url": "https://example.com/image.png",
                "alt": "Guide image",
                "desc": "Illustrates the main guide workflow.",
            }
        ]
        tables = [
            {
                "headers": ["Plan", "Requests"],
                "rows": [["Free", "100"], ["Pro", "1000"]],
                "caption": "Usage limits",
                "summary": "Compares free and pro request quotas.",
                "metadata": {"row_count": 2, "column_count": 2, "has_headers": True},
            }
        ]

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    url,
                    title="Guide",
                    snippet="Primary guide.",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        "Guide",
                        "# Guide\n\nPrimary guide content.",
                        query or "",
                        related_links=related_links,
                        related_links_total=7,
                        images=images,
                        tables=tables,
                        source_type=source_type,
                    )
                ],
                "images": images,
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        search_results = await tool._search_web_internal(
            query="primary guide",
            depth="quick",
            max_results=1,
            fresh=True,
        )
        page_id = search_results[0]["page_id"]
        default_links = await tool._read_pages_internal(page_id, related_links_limit=3)
        no_links = await tool._read_pages_internal(page_id, related_links_limit=0)

        assert "related_links" not in search_results[0], search_results[0]
        assert search_results[0]["images"] == images, search_results[0]
        assert "tables" not in search_results[0], search_results[0]
        assert default_links["related_links"] == related_links, default_links
        assert default_links["related_links_total"] == 7, default_links
        assert default_links["related_links_more_available"] is True, default_links
        assert default_links["images"] == images, default_links
        assert default_links["tables"] == tables, default_links
        assert "related_links" not in no_links, no_links
        assert no_links["related_links_total"] == 7, no_links
        assert no_links["related_links_more_available"] is True, no_links
        assert no_links["tables"] == tables, no_links

    asyncio.run(scenario())


def test_read_pages_reuse_content_from_initial_crawl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        search_url = "https://react.dev/reference/react/useEffect"
        crawl_calls: list[list[str]] = []

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    search_url,
                    title="useEffect - React",
                    snippet="Official React documentation covering useEffect cleanup behavior.",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        search_url,
                        "useEffect - React",
                        "# useEffect\n\nFull page content saved during search_web.",
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=1,
            fresh=True,
        )
        page_id = results[0]["page_id"]

        pages = await tool._read_pages_internal([page_id], max_chars=500)

        assert crawl_calls == [[search_url]], crawl_calls
        assert pages["returned_pages"] == 1, pages
        assert (
            "Full page content saved during search_web." in pages["pages"][0]["content"]
        ), pages
        assert pages["pages"][0]["content_source"] == "crawled_page", pages
        assert pages["pages"][0]["full_content_available"] is True, pages

    asyncio.run(scenario())


def test_markdown_content_variants_prefer_fit_markdown_with_links_preserved() -> None:
    page_content, summary_content = _markdown_content_variants(
        {
            "fit_markdown": "# PyPI Docs\n\nTo view the developer documentation, visit the [Warehouse documentation](https://warehouse.pypa.io/).",
            "raw_markdown": "# PyPI Docs\n\nTo view the developer documentation, visit the [Warehouse documentation](https://warehouse.pypa.io/).",
        }
    )

    assert "[Warehouse documentation]" in page_content
    assert summary_content == page_content


def test_markdown_content_variants_fall_back_to_html_when_markdown_missing() -> None:
    page_content, summary_content = _markdown_content_variants(
        {"fit_markdown": "", "raw_markdown": ""},
        html_fallback="<main>cleaned html</main>",
    )

    assert page_content == "<main>cleaned html</main>"
    assert summary_content == page_content


def test_page_record_content_prefers_markdown_then_cleaned_html() -> None:
    assert (
        Tools._page_record_content(
            {
                "representations": {
                    "fit_markdown": "",
                    "raw_markdown": "",
                    "cleaned_html": "<main>cleaned html</main>",
                    "fit_html": "<main>fit html</main>",
                    "html": "<html>raw html</html>",
                }
            }
        )
        == "<main>cleaned html</main>"
    )


def test_normalized_result_url_prefers_redirected_url_without_fragment() -> None:
    assert (
        Tools._normalized_result_url(
            "https://docs.crawl4ai.com/core/deep-crawling/#23-bestfirstcrawlingstrategy-recommended-deep-crawl-strategy",
            "https://docs.crawl4ai.com/core/deep-crawling/",
        )
        == "https://docs.crawl4ai.com/core/deep-crawling"
    )


def test_crawler_config_preserves_links_in_markdown_output() -> None:
    tool = Tools()
    payload = _crawler_config_payload(tool)
    markdown_options = payload["params"]["markdown_generator"]["params"]["options"][
        "value"
    ]

    assert markdown_options["ignore_links"] is False
    assert "exclude_external_links" not in payload["params"]


def test_crawl_url_stores_content_with_links_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __init__(self, payload: dict[str, Any]):
            self._payload = payload

        async def __aenter__(self) -> "FakeResponse":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            _ = exc_type, exc, tb
            return False

        def raise_for_status(self) -> None:
            return None

        async def json(self) -> dict[str, Any]:
            return self._payload

    class FakeSession:
        def __init__(self, *args: Any, **kwargs: Any):
            _ = args, kwargs

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            _ = exc_type, exc, tb
            return False

        def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
            _ = args, kwargs
            return FakeResponse(
                {
                    "results": [
                        {
                            "url": "https://docs.pypi.org/",
                            "success": True,
                            "status_code": 200,
                            "metadata": {"title": "PyPI Docs"},
                            "redirected_url": "https://docs.pypi.org/",
                            "fit_html": "<main><h1>PyPI Docs</h1></main>",
                            "cleaned_html": "<main><h1>PyPI Docs</h1></main>",
                            "markdown": {
                                "fit_markdown": "# PyPI Docs\n\nTo view the developer documentation, visit the [Warehouse documentation](https://warehouse.pypa.io/).",
                                "raw_markdown": "[ Skip to content ](https://docs.pypi.org/#welcome-to-pypi-user-documentation)\n\n# PyPI Docs\n\nTo view the developer documentation, visit the [Warehouse documentation](https://warehouse.pypa.io/).",
                            },
                            "links": {"internal": [], "external": []},
                            "media": {},
                        }
                    ]
                }
            )

    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        monkeypatch.setattr(
            "sourceweave_web_search.tool.aiohttp.ClientSession", FakeSession
        )

        result = await tool._crawl_url(["https://docs.pypi.org/"], query="pypi docs")

        assert result["content"][0]["summary"].startswith(
            "To view the developer documentation, visit the"
        )
        assert result["content"][0]["url"] == "https://docs.pypi.org/"
        assert result["content"][0]["redirected_url"] == "https://docs.pypi.org/"
        assert result["content"][0]["status_code"] == 200
        assert "Skip to content" not in result["content"][0]["summary"]
        page = await tool._read_pages_internal(
            result["content"][0]["page_id"], max_chars=500
        )
        assert "[Warehouse documentation]" in page["content"], page
        assert page["redirected_url"] == "https://docs.pypi.org/"
        assert page["status_code"] == 200
        assert "Skip to content" not in page["content"], page
        cached_page = await tool._cache.get(
            tool._page_cache_key("https://docs.pypi.org/")
        )
        assert cached_page is not None
        cached_record = json.loads(cached_page)
        assert cached_record["redirected_url"] == "https://docs.pypi.org/"
        assert cached_record["status_code"] == 200
        assert "[Warehouse documentation]" in cached_record["content"]
        assert "representations" not in cached_record

    asyncio.run(scenario())


def test_normalize_page_record_preserves_redirected_url_and_status_code() -> None:
    normalized = Tools._normalize_page_record(
        {
            "url": "https://example.com/final",
            "title": "Example",
            "content_type": "html",
            "content_source": "crawled_page",
            "redirected_url": "https://example.com/final",
            "status_code": "302",
            "representations": {
                "fit_markdown": "Example content",
                "raw_markdown": "Example content",
            },
        }
    )

    assert normalized is not None
    assert normalized["redirected_url"] == "https://example.com/final"
    assert normalized["status_code"] == 302


def test_read_pages_direct_url_ignores_legacy_page_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        url = "https://docs.pypi.org/"
        legacy_key = f"sc:page:{__import__('hashlib').md5(Tools._canonicalize_url(url).encode()).hexdigest()}"
        cached_values = {
            legacy_key: json.dumps(
                {
                    "url": url,
                    "title": "PyPI Docs",
                    "content": "# Legacy\n\nTo view the developer documentation, visit the",
                    "content_type": "html",
                    "content_source": "crawled_page",
                    "full_content_available": True,
                    "related_links": [],
                    "related_links_total": 0,
                    "images": [],
                }
            )
        }
        crawl_calls: list[list[str]] = []

        async def fake_get(key: str) -> str | None:
            return cached_values.get(key)

        async def fake_setex(key: str, ttl_s: int, value: str) -> None:
            _ = ttl_s
            cached_values[key] = value

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = query, timeout_s, cache_mode, source_type, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        "PyPI Docs",
                        "# PyPI Docs\n\nTo view the developer documentation, visit the [Warehouse documentation](https://warehouse.pypa.io/).",
                        "pypi docs",
                    )
                ]
            }

        monkeypatch.setattr(tool._cache, "get", fake_get)
        monkeypatch.setattr(tool._cache, "setex", fake_setex)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        page = await tool._read_pages_internal(urls=[url], max_chars=500)

        assert crawl_calls == [[url]], crawl_calls
        assert "[Warehouse documentation]" in page["content"], page

    asyncio.run(scenario())


def test_cli_can_include_search_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTool:
        def __init__(self) -> None:
            self.last_query_metadata = {
                "query": "example query",
                "crawl": {"failed": 1},
            }

        async def search_web(
            self, *args: Any, **kwargs: Any
        ) -> list[dict[str, Any]]:
            _ = args, kwargs
            return [
                {
                    "url": "https://example.com/doc",
                    "title": "Example",
                    "page_id": "page123",
                    "summary": "Example summary",
                    "key_points": ["Example summary"],
                }
            ]

        async def read_pages(self, *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            return None

    async def scenario() -> None:
        from sourceweave_web_search import cli as cli_module

        monkeypatch.setattr(
            cli_module, "build_tools", lambda valve_overrides=None: FakeTool()
        )
        args = cli_module.parse_args(["--query", "example query", "--include-metadata"])
        payload = await cli_module.run_cli(args)
        assert payload["search_metadata"]["query"] == "example query", payload
        assert payload["search_metadata"]["crawl"]["failed"] == 1, payload

    asyncio.run(scenario())


def test_cli_reads_direct_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_read_kwargs: dict[str, Any] = {}

    class FakeTool:
        last_query_metadata: dict[str, Any] = {}

        async def read_urls(
            self, *args: Any, **kwargs: Any
        ) -> list[dict[str, Any]]:
            captured_read_kwargs["args"] = args
            captured_read_kwargs["kwargs"] = kwargs
            return [{"url": "https://packaging.python.org/en/latest/", "content": "ok"}]

    async def scenario() -> None:
        from sourceweave_web_search import cli as cli_module

        monkeypatch.setattr(
            cli_module, "build_tools", lambda valve_overrides=None: FakeTool()
        )
        args = cli_module.parse_args(
            [
                "--read-url",
                "https://packaging.python.org/en/latest/",
            ]
        )
        payload = await cli_module.run_cli(args)

        assert (
            payload["read_urls"][0]["url"] == "https://packaging.python.org/en/latest/"
        )
        assert captured_read_kwargs["kwargs"]["urls"] == [
            "https://packaging.python.org/en/latest/"
        ], captured_read_kwargs
        assert captured_read_kwargs["kwargs"]["focus"] == "", captured_read_kwargs

    asyncio.run(scenario())


def test_cli_passes_plain_url_strings_and_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_search_kwargs: dict[str, Any] = {}
    captured_read_kwargs: dict[str, Any] = {}

    class FakeTool:
        last_query_metadata: dict[str, Any] = {}

        async def search_web(
            self, *args: Any, **kwargs: Any
        ) -> list[dict[str, Any]]:
            _ = args
            captured_search_kwargs.update(kwargs)
            return [
                {
                    "url": "https://example.com/guide.pdf",
                    "title": "Guide PDF",
                    "page_id": "page123",
                    "summary": "Guide summary",
                    "key_points": ["Guide summary"],
                }
            ]

        async def read_pages(
            self, *args: Any, **kwargs: Any
        ) -> list[dict[str, Any]]:
            captured_read_kwargs["args"] = args
            captured_read_kwargs["kwargs"] = kwargs
            return [{"page_id": "page123", "content": "Guide content"}]

    async def scenario() -> None:
        from sourceweave_web_search import cli as cli_module

        monkeypatch.setattr(
            cli_module, "build_tools", lambda valve_overrides=None: FakeTool()
        )
        args = cli_module.parse_args(
            [
                "--query",
                "guide pdf",
                "--domain",
                "example.com",
                "--url",
                "https://example.com/guide.pdf",
                "--read-first-page",
            ]
        )
        await cli_module.run_cli(args)

        assert captured_search_kwargs["urls"] == ["https://example.com/guide.pdf"], (
            captured_search_kwargs
        )
        assert captured_search_kwargs["domains"] == ["example.com"], (
            captured_search_kwargs
        )
        assert captured_read_kwargs["kwargs"]["page_ids"] == ["page123"], (
            captured_read_kwargs
        )

    asyncio.run(scenario())


@pytest.mark.integration
def test_run_tool_call_batches_read_pages_results() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_tool_call.py",
            "--query",
            "python programming",
            "--read-first-pages",
            "2",
        ],
        cwd=_repo_root(),
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)

    assert isinstance(payload.get("search_web"), list), payload
    assert len(payload["search_web"]) >= 2, payload

    batched_read = payload.get("read_pages")
    assert isinstance(batched_read, list), payload
    assert len(batched_read) == 2, batched_read
    assert all(page.get("page_id") for page in batched_read), batched_read


def test_search_web_honors_requested_max_results_above_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    f"https://docs{idx}.example/api/{idx}",
                    title=f"Historical gold API page {idx}",
                    snippet="Free historical gold API docs and examples without an API key.",
                    search_rank=idx,
                )
                for idx in range(1, 13)
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            url = urls_to_fetch[0]
            idx = int(url.rsplit("/", 1)[-1])
            content = (
                f"# Historical gold API page {idx}\n\n"
                "Free historical gold API docs, JSON examples, and endpoint notes without an API key."
            )
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        f"Historical gold API page {idx}",
                        content,
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="free gold price api no key historical data",
            depth="normal",
            max_results=8,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert len(results) == 8, results

    asyncio.run(scenario())


def test_public_search_web_defaults_to_normal_and_passes_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        captured_calls: list[dict[str, Any]] = []

        async def fake_search_internal(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            _ = args
            captured_calls.append(kwargs)
            return [
                {
                    "url": "https://example.com/guide",
                    "title": "Guide",
                    "page_id": "page123",
                    "summary": "Guide summary",
                    "key_points": ["Guide summary"],
                }
            ]

        monkeypatch.setattr(tool, "_search_web_internal", fake_search_internal)

        default_results = await tool.search_web(
            query="weather in amman over the next 10 days",
        )
        deep_results = await tool.search_web(
            query="compare vector databases",
            domains=["docs.example.com"],
            urls=["https://example.com/guide"],
            effort="deep",
        )

        assert len(default_results) == 1, default_results
        assert len(deep_results) == 1, deep_results
        assert captured_calls[0]["query"] == "weather in amman over the next 10 days"
        assert captured_calls[0]["depth"] == "normal", captured_calls
        assert captured_calls[0]["urls"] is None, captured_calls
        assert captured_calls[1]["query"] == "compare vector databases site:docs.example.com"
        assert captured_calls[1]["depth"] == "deep", captured_calls
        assert captured_calls[1]["urls"] == ["https://example.com/guide"], captured_calls

    asyncio.run(scenario())


def test_search_searxng_collects_multiple_pages_when_more_candidates_are_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __init__(self, payload: dict[str, Any]):
            self.payload = payload

        async def __aenter__(self) -> "FakeResponse":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            _ = exc_type, exc, tb
            return False

        def raise_for_status(self) -> None:
            return None

        async def json(self) -> dict[str, Any]:
            return self.payload

    class FakeSession:
        requested_urls: list[str] = []

        def __init__(self, *args: Any, **kwargs: Any):
            _ = args, kwargs

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            _ = exc_type, exc, tb
            return False

        def get(self, url: str, headers: dict[str, str]) -> FakeResponse:
            _ = headers
            self.requested_urls.append(url)
            if "pageno=2" in url:
                return FakeResponse(
                    {
                        "results": [
                            {
                                "url": "https://example.com/three",
                                "title": "Three",
                                "content": "Third result.",
                            },
                            {
                                "url": "https://example.com/four",
                                "title": "Four",
                                "content": "Fourth result.",
                            },
                        ]
                    }
                )
            return FakeResponse(
                {
                    "results": [
                        {
                            "url": "https://example.com/one",
                            "title": "One",
                            "content": "First result.",
                        },
                        {
                            "url": "https://example.com/two",
                            "title": "Two",
                            "content": "Second result.",
                        },
                    ]
                }
            )

    async def scenario() -> None:
        tool = Tools()
        monkeypatch.setattr(
            "sourceweave_web_search.tool.aiohttp.ClientSession", FakeSession
        )
        tool.user_valves.SEARXNG_MAX_RESULTS = 4

        results = await tool._search_searxng("example query")

        assert [result["url"] for result in results] == [
            "https://example.com/one",
            "https://example.com/two",
            "https://example.com/three",
            "https://example.com/four",
        ], results
        assert any("pageno=2" in url for url in FakeSession.requested_urls), (
            FakeSession.requested_urls
        )

    asyncio.run(scenario())


def test_search_web_requests_depth_sized_candidate_pool_from_searxng(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        observed_search_limits: list[int | None] = []

        async def fake_search(
            query_value: str,
            __event_emitter__: Any = None,
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            observed_search_limits.append(tool.user_valves.SEARXNG_MAX_RESULTS)
            return [
                tool._build_candidate(
                    f"https://example{idx}.com/result",
                    title=f"Result {idx}",
                    snippet=f"Snippet {idx}",
                    search_rank=idx,
                )
                for idx in range(1, 13)
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            url = urls_to_fetch[0]
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        url.rsplit("/", 1)[-1].title(),
                        f"# {url}\n\nResult content.",
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="example query",
            depth="deep",
            fresh=True,
        )

        assert observed_search_limits == [50], observed_search_limits
        assert len(results) == 10, results
        assert tool.user_valves.SEARXNG_MAX_RESULTS is None

    asyncio.run(scenario())


def test_search_web_emits_search_level_status_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        events: list[dict[str, Any]] = []

        async def fake_emit(event: dict[str, Any]) -> None:
            events.append(event)

        async def fake_search(
            query_value: str, __event_emitter__: Any = None
        ) -> list[dict[str, Any]]:
            _ = query_value, __event_emitter__
            return [
                tool._build_candidate(
                    "https://docs.example.com/guide",
                    title="Guide",
                    snippet="Guide snippet.",
                    search_rank=1,
                )
            ]

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            cache_mode: str | None = None,
            source_type: str = "search_result",
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, cache_mode, __event_emitter__
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        urls_to_fetch[0],
                        "Guide",
                        "# Guide\n\nGuide content.",
                        query or "",
                        source_type=source_type,
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool._search_web_internal(
            query="guide",
            depth="quick",
            max_results=1,
            fresh=True,
            __event_emitter__=fake_emit,
        )

        assert len(results) == 1, results
        status_events = [event for event in events if event.get("type") == "status"]
        assert [event["data"]["description"] for event in status_events] == [
            "Searching web sources...",
            "Found 1 candidate; reading up to 1 page...",
            "Prepared 1 result from 1 page.",
        ], status_events
        assert [event["data"]["done"] for event in status_events] == [
            False,
            False,
            True,
        ], status_events

    asyncio.run(scenario())


def test_read_pages_only_emits_per_page_status_when_more_status_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        _install_memory_cache(monkeypatch, tool)
        page_id = await _store_test_page(
            tool,
            "https://example.com/guide",
            "Guide",
            "# Guide\n\nDetailed guide content for event assertions.",
        )

        quiet_events: list[dict[str, Any]] = []

        async def quiet_emit(event: dict[str, Any]) -> None:
            quiet_events.append(event)

        quiet_result = await tool._read_pages_internal(
            page_id,
            __event_emitter__=quiet_emit,
        )

        tool.valves.MORE_STATUS = True
        verbose_events: list[dict[str, Any]] = []

        async def verbose_emit(event: dict[str, Any]) -> None:
            verbose_events.append(event)

        verbose_result = await tool._read_pages_internal(
            page_id,
            __event_emitter__=verbose_emit,
        )

        assert quiet_result["page_id"] == page_id, quiet_result
        assert verbose_result["page_id"] == page_id, verbose_result
        assert [event["type"] for event in quiet_events] == ["citation"], quiet_events
        assert [event["type"] for event in verbose_events] == [
            "status",
            "citation",
        ], verbose_events
        assert verbose_events[0]["data"] == {
            "description": "Read Guide...",
            "done": True,
        }, verbose_events

    asyncio.run(scenario())


def test_crawl_url_handles_missing_status_code(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict[str, Any]):
            self.payload = payload

        async def __aenter__(self) -> "FakeResponse":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            _ = exc_type, exc, tb
            return False

        def raise_for_status(self) -> None:
            return None

        async def json(self) -> dict[str, Any]:
            return self.payload

    class FakeSession:
        def __init__(self, *args: Any, **kwargs: Any):
            _ = args, kwargs

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            _ = exc_type, exc, tb
            return False

        def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
            _ = args, kwargs
            return FakeResponse(
                {
                    "results": [
                        {
                            "url": "https://bad.example/",
                            "success": False,
                            "status_code": None,
                        }
                    ]
                }
            )

    async def scenario() -> None:
        tool = Tools()
        negative_cache_keys: list[tuple[str, int, str]] = []

        async def fake_setex(key: str, ttl_s: int, value: str) -> None:
            negative_cache_keys.append((key, ttl_s, value))

        monkeypatch.setattr(
            "sourceweave_web_search.tool.aiohttp.ClientSession", FakeSession
        )
        monkeypatch.setattr(tool._cache, "setex", fake_setex)

        result = await tool._crawl_url(["https://bad.example/"], query="gold api")

        assert result == {"content": [], "images": []}, result
        assert negative_cache_keys, negative_cache_keys

    asyncio.run(scenario())


def test_crawl_url_handles_request_timeout_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTimeoutSession:
        def __init__(self, *args: Any, **kwargs: Any):
            _ = args, kwargs

        async def __aenter__(self) -> "FakeTimeoutSession":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            _ = exc_type, exc, tb
            return False

        def post(self, *args: Any, **kwargs: Any) -> Any:
            _ = args, kwargs

            class _TimeoutResponse:
                async def __aenter__(self) -> Any:
                    raise asyncio.TimeoutError()

                async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                    _ = exc_type, exc, tb
                    return False

            return _TimeoutResponse()

    async def scenario() -> None:
        tool = Tools()
        dead_cache_writes: list[tuple[str, int, str]] = []
        tool._active_query_metadata = tool._empty_query_metadata("gold api", "quick")

        async def fake_setex(key: str, ttl_s: int, value: str) -> None:
            dead_cache_writes.append((key, ttl_s, value))

        monkeypatch.setattr(
            "sourceweave_web_search.tool.aiohttp.ClientSession", FakeTimeoutSession
        )
        monkeypatch.setattr(tool._cache, "setex", fake_setex)

        result = await tool._crawl_url(["https://bad.example/"], query="gold api")

        assert result["error"] == "timeout", result
        assert dead_cache_writes, dead_cache_writes
        failed_urls = tool._active_query_metadata["crawl"]["failed_urls"]
        assert failed_urls, tool._active_query_metadata
        assert failed_urls[0]["url"] == "https://bad.example/", failed_urls
        assert failed_urls[0]["reason"] == "timeout", failed_urls
        assert failed_urls[0]["stage"] == "crawl_request", failed_urls

    asyncio.run(scenario())


def test_transient_failure_negative_ttls_are_shorter() -> None:
    assert _negative_ttl("timeout") == 45
    assert _negative_ttl("500") == 90
    assert _negative_ttl("403") == 180
    assert _negative_ttl("blocked") == 300
    assert _negative_ttl("404") == 1800
