"""Standalone runtime checks that call the tool directly."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sourceweave_web_search.tool import Tools, _negative_ttl


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
) -> dict[str, Any]:
    page_id = tool._page_store.put(
        url,
        title,
        content,
        content_type=content_type,
        content_source=content_source,
        full_content_available=full_content_available,
        related_links=related_links,
        related_links_total=related_links_total,
        images=images,
    )
    compact = tool._build_compact_summary(content, query)
    return {
        "url": url,
        "title": title,
        "page_id": page_id,
        "summary": compact["summary"],
        "key_points": compact["key_points"],
        "content_length": len(content),
        "images": list(images or []),
        "content_type": content_type,
        "content_source": content_source,
        "full_content_available": full_content_available,
        "related_links": list(related_links or []),
        "related_links_total": related_links_total,
        "_content": content,
    }


@pytest.mark.integration
def test_search_and_read_round_trip() -> None:
    async def scenario() -> None:
        tool = Tools()
        results = await tool.search_web(
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

        page = await tool.read_pages(first["page_id"], max_chars=1200)
        assert "error" not in page, page
        assert len(page.get("content", "")) >= 200, page

    asyncio.run(scenario())


@pytest.mark.integration
def test_read_pages_works_from_fresh_tool_instance() -> None:
    async def scenario() -> None:
        tool = Tools()
        results = await tool.search_web(
            query="asyncio python tutorial",
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert results, "search_web returned no results"

        page_id = results[0]["page_id"]
        fresh_tool = Tools()
        page = await fresh_tool.read_pages(page_id, max_chars=1000)
        assert "error" not in page, page
        assert len(page.get("content", "")) >= 200, page

    asyncio.run(scenario())


def test_read_pages_accepts_multiple_page_ids_in_one_call() -> None:
    async def scenario() -> None:
        tool = Tools()
        first_page_id = tool._page_store.put(
            "https://example.com/alpha",
            "Alpha",
            "# Alpha\n\nAlpha content paragraph with enough text to be useful for a multi-page read.",
        )
        second_page_id = tool._page_store.put(
            "https://example.com/beta",
            "Beta",
            "# Beta\n\nBeta content paragraph with enough text to be useful for a multi-page read.",
        )

        pages = await tool.read_pages([first_page_id, second_page_id], max_chars=200)

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


def test_read_pages_multi_returns_partial_errors_for_missing_ids() -> None:
    async def scenario() -> None:
        tool = Tools()
        first_page_id = tool._page_store.put(
            "https://example.com/gamma",
            "Gamma",
            "# Gamma\n\nGamma content paragraph with enough text to survive truncation.",
        )

        pages = await tool.read_pages([first_page_id, "missing-page-id"], max_chars=200)

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


def test_read_pages_returns_content_state_and_related_assets() -> None:
    async def scenario() -> None:
        tool = Tools()
        page_id = tool._page_store.put(
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

        page = await tool.read_pages(
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
        tool._cache.enabled = False
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
            assert urls_to_fetch == [url]
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        url,
                        "Reddit - Prove your humanity",
                        "# Prove your humanity\n\nWe are committed to safety and security. But not for bots. Complete the challenge below and let us know you are a real person.",
                        query or "",
                    )
                ]
            }

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        try:
            results = await tool.search_web(
                query="react useEffect cleanup",
                depth="quick",
                max_results=1,
                fresh=True,
            )
            assert results[0]["page_quality"] == "challenge", results

            page = await tool.read_pages(results[0]["page_id"], max_chars=400)
            assert page["page_quality"] == "challenge", page
        finally:
            monkeypatch.undo()

    asyncio.run(scenario())


def test_read_pages_marks_blocked_pages() -> None:
    async def scenario() -> None:
        tool = Tools()
        page_id = tool._page_store.put(
            "https://example.com/blocked",
            "Access denied",
            "Access denied. You do not have permission to access this resource. Error code: 1020.",
            content_type="html",
            content_source="crawled_page",
            full_content_available=True,
        )

        page = await tool.read_pages(page_id, max_chars=500)

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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
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
                    _make_crawl_result(tool, url, *page_bodies[url], query or "")
                    for url in urls
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_web(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
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

        results = await tool.search_web(
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

        results = await tool.search_web(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
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
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_web(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
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
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_web(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = urls_to_fetch, query, timeout_s, __event_emitter__
            return {"error": "timeout", "details": "timeout"}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_web(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
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
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_web(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        search_url,
                        "useEffect - React",
                        "# useEffect\n\nFull page content saved during search_web.",
                        query or "",
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        first_results = await tool.search_web(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=1,
            fresh=False,
        )
        second_results = await tool.search_web(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {"error": "timeout", "details": "timeout"}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        first_results = await tool.search_web(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        search_url,
                        "useEffect - React",
                        "# useEffect\n\nNow we have real crawled page content.",
                        query or "",
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_crawl_url", succeeding_crawl)

        second_results = await tool.search_web(
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


def test_search_web_can_convert_documents_per_explicit_url(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = query, timeout_s, __event_emitter__
            fetch_calls.append(url)
            return _make_crawl_result(
                tool,
                url,
                "Guide PDF",
                "# Guide PDF\n\nConverted document content.",
                "guide pdf",
                content_type="document",
                content_source="converted_document",
            )

        async def fake_crawl(
            urls_to_fetch: list[str],
            query: str | None = None,
            timeout_s: float | None = None,
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = query, timeout_s, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {"content": [], "images": []}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_fetch_document", fake_fetch_document)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_web(
            query="guide pdf",
            urls=[{"url": pdf_url, "convert_document": True}],
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


def test_search_web_does_not_auto_convert_documents(
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any] | None:
            _ = query, timeout_s, __event_emitter__
            fetch_calls.append(url)
            return None

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_fetch_document", fake_fetch_document)

        results = await tool.search_web(
            query="guide pdf",
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert fetch_calls == [], fetch_calls
        assert results[0]["url"] == pdf_url, results
        assert results[0]["fallback_reason"] == "search_only", results[0]
        assert results[0]["content_source"] == "search_snippet", results[0]

    asyncio.run(scenario())


def test_read_pages_apply_related_links_limit_and_can_omit_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
        url = "https://example.com/guide"
        related_links = [
            {"url": "https://example.com/related-a", "text": "Related A"},
            {"url": "https://example.com/related-b", "text": "Related B"},
        ]
        images = [{"url": "https://example.com/image.png", "alt": "Guide image"}]

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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
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
                    )
                ],
                "images": images,
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        search_results = await tool.search_web(
            query="primary guide",
            depth="quick",
            max_results=1,
            fresh=True,
        )
        page_id = search_results[0]["page_id"]
        default_links = await tool.read_pages(page_id, related_links_limit=3)
        no_links = await tool.read_pages(page_id, related_links_limit=0)

        assert "related_links" not in search_results[0], search_results[0]
        assert search_results[0]["images"] == images, search_results[0]
        assert default_links["related_links"] == related_links, default_links
        assert default_links["related_links_total"] == 7, default_links
        assert default_links["related_links_more_available"] is True, default_links
        assert default_links["images"] == images, default_links
        assert "related_links" not in no_links, no_links
        assert no_links["related_links_total"] == 7, no_links
        assert no_links["related_links_more_available"] is True, no_links

    asyncio.run(scenario())


def test_read_pages_reuse_content_from_initial_crawl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        tool = Tools()
        tool._cache.enabled = False
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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
            crawl_calls.append(list(urls_to_fetch))
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        search_url,
                        "useEffect - React",
                        "# useEffect\n\nFull page content saved during search_web.",
                        query or "",
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_web(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=1,
            fresh=True,
        )
        page_id = results[0]["page_id"]

        pages = await tool.read_pages([page_id], max_chars=500)

        assert crawl_calls == [[search_url]], crawl_calls
        assert pages["returned_pages"] == 1, pages
        assert (
            "Full page content saved during search_web." in pages["pages"][0]["content"]
        ), pages
        assert pages["pages"][0]["content_source"] == "crawled_page", pages
        assert pages["pages"][0]["full_content_available"] is True, pages

    asyncio.run(scenario())


def test_cli_can_include_search_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTool:
        def __init__(self) -> None:
            self.last_query_metadata = {
                "query": "example query",
                "crawl": {"failed": 1},
            }

        async def search_web(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
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


def test_cli_parses_json_url_objects_and_read_related_links_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_search_kwargs: dict[str, Any] = {}
    captured_read_kwargs: dict[str, Any] = {}

    class FakeTool:
        last_query_metadata: dict[str, Any] = {}

        async def search_web(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
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

        async def read_pages(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_read_kwargs["args"] = args
            captured_read_kwargs["kwargs"] = kwargs
            return {"page_id": "page123", "content": "Guide content"}

    async def scenario() -> None:
        from sourceweave_web_search import cli as cli_module

        monkeypatch.setattr(
            cli_module, "build_tools", lambda valve_overrides=None: FakeTool()
        )
        args = cli_module.parse_args(
            [
                "--query",
                "guide pdf",
                "--url",
                '{"url": "https://example.com/guide.pdf", "convert_document": true}',
                "--read-first-page",
                "--related-links-limit",
                "2",
            ]
        )
        await cli_module.run_cli(args)

        assert captured_search_kwargs["urls"] == [
            {
                "url": "https://example.com/guide.pdf",
                "convert_document": True,
            }
        ], captured_search_kwargs
        assert captured_read_kwargs["kwargs"]["related_links_limit"] == 2, (
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
            "--depth",
            "quick",
            "--max-results",
            "2",
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
    assert len(payload["search_web"]) == 2, payload

    batched_read = payload.get("read_pages")
    assert isinstance(batched_read, dict), payload
    assert batched_read["returned_pages"] == 2, batched_read
    assert len(batched_read["requested_page_ids"]) == 2, batched_read
    assert len(batched_read["pages"]) == 2, batched_read
    assert not batched_read["errors"], batched_read


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
            __event_emitter__: Any = None,
        ) -> dict[str, Any]:
            _ = timeout_s, __event_emitter__
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
                    )
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_web(
            query="free gold price api no key historical data",
            depth="normal",
            max_results=8,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert len(results) == 8, results

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
