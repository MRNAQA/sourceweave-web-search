"""Standalone runtime checks that call the tool directly."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sourceweave_web_search.config import RuntimeOverrides, build_tools
from sourceweave_web_search.tool import Tools, _negative_ttl


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _make_crawl_result(
    tool: Tools, url: str, title: str, content: str, query: str
) -> dict:
    page_id = tool._page_store.put(url, title, content)
    compact = tool._build_compact_summary(content, query)
    return {
        "url": url,
        "title": title,
        "page_id": page_id,
        "summary": compact["summary"],
        "key_points": compact["key_points"],
        "content_length": len(content),
        "images": [],
        "content_type": "html",
        "_content": content,
    }


@pytest.mark.integration
def test_search_and_read_round_trip():
    async def scenario():
        tool = Tools()
        results = await tool.search_and_crawl(
            query="python programming",
            depth="quick",
            max_results=2,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert results, "search_and_crawl returned no results"

        first = results[0]
        for key in ("url", "title", "page_id", "summary", "key_points"):
            assert key in first, f"missing key '{key}' in result: {first}"

        page = await tool.read_page(first["page_id"], max_chars=1200)
        assert "error" not in page, page
        assert len(page.get("content", "")) >= 200, page

    asyncio.run(scenario())


@pytest.mark.integration
def test_read_page_works_from_fresh_tool_instance():
    async def scenario():
        tool = Tools()
        results = await tool.search_and_crawl(
            query="asyncio python tutorial",
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert results, "search_and_crawl returned no results"

        page_id = results[0]["page_id"]
        fresh_tool = Tools()
        page = await fresh_tool.read_page(page_id, max_chars=1000)
        assert "error" not in page, page
        assert len(page.get("content", "")) >= 200, page

    asyncio.run(scenario())


def test_read_page_accepts_multiple_page_ids_in_one_call():
    async def scenario():
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

        pages = await tool.read_page([first_page_id, second_page_id], max_chars=200)

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


def test_read_page_multi_returns_partial_errors_for_missing_ids():
    async def scenario():
        tool = Tools()
        first_page_id = tool._page_store.put(
            "https://example.com/gamma",
            "Gamma",
            "# Gamma\n\nGamma content paragraph with enough text to survive truncation.",
        )

        pages = await tool.read_page([first_page_id, "missing-page-id"], max_chars=200)

        assert pages["returned_pages"] == 1, pages
        assert len(pages["pages"]) == 1, pages
        assert pages["pages"][0]["page_id"] == first_page_id, pages
        assert pages["errors"] == [
            {
                "page_id": "missing-page-id",
                "error": "page_id 'missing-page-id' not found or expired. Call search_and_crawl again.",
            }
        ], pages

    asyncio.run(scenario())


def test_explicit_url_crawl_without_relying_on_search_ranking(monkeypatch):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False
        query = "python about page"
        explicit_url = "https://www.python.org/about/"
        search_url = "https://www.python.org/downloads/release/python-3114/"

        async def fake_search(query_variant, __event_emitter__=None):
            return [
                tool._build_candidate(
                    search_url,
                    title="Python 3.11.4 release",
                    snippet="Release notes and downloads for Python 3.11.4.",
                    search_rank=1,
                    retrieved_by_queries=[query_variant],
                )
            ]

        async def fake_crawl(urls, query=None, timeout_s=None, __event_emitter__=None):
            page_bodies = {
                explicit_url: (
                    "About Python",
                    "# About Python\n\nMission statement.",
                ),
                search_url: (
                    "Python 3.11.4 release",
                    "# Python 3.11.4 release\n\nDetailed release notes, changelog, installers, and download instructions for Python 3.11.4.",
                ),
            }
            return {
                "content": [
                    _make_crawl_result(tool, url, *page_bodies[url], query or "")
                    for url in urls
                    if url in page_bodies
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_and_crawl(
            query=query,
            urls=[explicit_url],
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert results, "explicit URL crawl returned no results"
        assert results[0]["url"] == explicit_url, results[0]
        assert results[0]["source_type"] == "explicit_url", results[0]
        assert results[0]["pre_crawl_score"] >= 100, results[0]

    asyncio.run(scenario())


def test_multiple_explicit_urls_preserve_input_order(monkeypatch):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False
        query = "python reference pages"
        explicit_urls = [
            "https://docs.python.org/3/tutorial/",
            "https://docs.python.org/3/library/",
        ]
        search_url = "https://docs.python.org/3/whatsnew/3.12.html"

        async def fake_search(query_variant, __event_emitter__=None):
            return [
                tool._build_candidate(
                    search_url,
                    title="What's New In Python 3.12",
                    snippet="Release highlights and new features for Python 3.12.",
                    search_rank=1,
                    retrieved_by_queries=[query_variant],
                )
            ]

        async def fake_crawl(urls, query=None, timeout_s=None, __event_emitter__=None):
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
            crawl_order = [explicit_urls[1], search_url, explicit_urls[0]]
            return {
                "content": [
                    _make_crawl_result(tool, url, *page_bodies[url], query or "")
                    for url in crawl_order
                    if url in urls
                ],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_and_crawl(
            query=query,
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


def test_explicit_url_survives_crawl_candidate_truncation(monkeypatch):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False
        query = "relevant api docs historical data no key"
        explicit_url = "https://example.com/user-picked"
        search_urls = [
            f"https://docs{idx}.example.com/relevant-api-docs-{idx}"
            for idx in range(1, 6)
        ]
        crawl_calls = []

        async def fake_search(query_variant, __event_emitter__=None):
            return [
                tool._build_candidate(
                    url,
                    title=f"Relevant API docs {idx}",
                    snippet="Relevant API docs with endpoints, examples, free access, and historical data.",
                    search_rank=idx,
                    retrieved_by_queries=[query_variant],
                )
                for idx, url in enumerate(search_urls, start=1)
            ]

        async def fake_crawl(urls, query=None, timeout_s=None, __event_emitter__=None):
            crawl_calls.extend(urls)
            results = []
            for url in urls:
                if url == explicit_url:
                    title = "User picked"
                    content = "# Picked\n\nA page with short unrelated text."
                else:
                    title = "Relevant API docs"
                    content = (
                        "# Relevant API docs\n\n"
                        "Relevant api docs historical data no api key. " * 30
                    )
                results.append(
                    _make_crawl_result(tool, url, title, content, query or "")
                )
            return {"content": results, "images": []}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_and_crawl(
            query=query,
            urls=[explicit_url],
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert crawl_calls[0] == explicit_url, crawl_calls
        assert explicit_url in crawl_calls, crawl_calls
        assert len(crawl_calls) == 4, crawl_calls
        assert [result["url"] for result in results] == [explicit_url], results
        assert results[0]["source_type"] == "explicit_url", results[0]

    asyncio.run(scenario())


def test_explicit_url_failure_falls_back_to_ranked_search_results(monkeypatch):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False
        query = "python about page"
        explicit_url = "https://www.python.org/about/"
        search_url = "https://www.python.org/community/"

        async def fake_search(query_variant, __event_emitter__=None):
            return [
                tool._build_candidate(
                    search_url,
                    title="Python Community",
                    snippet="Community resources, events, and Python Software Foundation links.",
                    search_rank=1,
                    retrieved_by_queries=[query_variant],
                )
            ]

        async def fake_crawl(urls, query=None, timeout_s=None, __event_emitter__=None):
            return {
                "content": [
                    _make_crawl_result(
                        tool,
                        search_url,
                        "Python Community",
                        "# Python Community\n\nThe Python community page collects news, events, and ways to get involved.",
                        query or "",
                    )
                ]
                if search_url in urls
                else [],
                "images": [],
            }

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_and_crawl(
            query=query,
            urls=[explicit_url],
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert [result["url"] for result in results] == [search_url], results
        assert all(result["source_type"] == "search_result" for result in results), (
            results
        )

    asyncio.run(scenario())


def test_search_and_crawl_returns_empty_list_when_no_candidates(monkeypatch):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False

        async def fake_search(query_variant, __event_emitter__=None):
            return []

        monkeypatch.setattr(tool, "_search_searxng", fake_search)

        results = await tool.search_and_crawl(
            query="nothing should be found",
            depth="quick",
            fresh=True,
        )

        assert results == [], results

    asyncio.run(scenario())


def test_batch_crawl_failure_retries_urls_individually(monkeypatch):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False
        urls = [
            "https://docs.example.com/react/use-effect",
            "https://docs.example.com/react/use-layout-effect",
        ]
        crawl_calls = []

        async def fake_search(query_variant, __event_emitter__=None):
            return [
                tool._build_candidate(
                    url,
                    title=f"Doc for {idx}",
                    snippet="Official React documentation example.",
                    search_rank=idx,
                    retrieved_by_queries=[query_variant],
                )
                for idx, url in enumerate(urls, start=1)
            ]

        async def fake_crawl(urls, query=None, timeout_s=None, __event_emitter__=None):
            crawl_calls.append(list(urls))
            if len(urls) > 1:
                return {"error": "batch timeout", "details": "timeout"}

            url = urls[0]
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

        results = await tool.search_and_crawl(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=2,
            fresh=True,
        )

        assert len(results) == 2, results
        assert crawl_calls[0] == urls, crawl_calls
        assert crawl_calls[1:] == [[urls[0]], [urls[1]]], crawl_calls

    asyncio.run(scenario())


def test_search_and_crawl_falls_back_to_search_snippets_when_crawl_returns_nothing(
    monkeypatch,
):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False
        search_url = "https://react.dev/reference/react/useEffect"

        async def fake_search(query_variant, __event_emitter__=None):
            return [
                tool._build_candidate(
                    search_url,
                    title="useEffect - React",
                    snippet="Official React documentation covering useEffect cleanup behavior.",
                    search_rank=1,
                    retrieved_by_queries=[query_variant],
                )
            ]

        async def fake_crawl(urls, query=None, timeout_s=None, __event_emitter__=None):
            return {"content": [], "images": []}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_and_crawl(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert len(results) == 1, results
        assert results[0]["url"] == search_url, results
        assert results[0]["fallback_reason"] == "search_only", results[0]
        assert results[0]["content_type"] == "search_result", results[0]
        assert results[0]["search_snippet"], results[0]

    asyncio.run(scenario())


def test_transient_failure_negative_ttls_are_shorter():
    assert _negative_ttl("timeout") == 45
    assert _negative_ttl("500") == 90
    assert _negative_ttl("403") == 180
    assert _negative_ttl("blocked") == 300
    assert _negative_ttl("404") == 1800


def test_search_and_crawl_records_query_failure_metadata(monkeypatch):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False
        first_url = "https://react.dev/reference/react/useEffect"
        second_url = "https://blog.example.com/react-cleanup"

        async def fake_search(query_variant, __event_emitter__=None):
            return [
                tool._build_candidate(
                    first_url,
                    title="useEffect - React",
                    snippet="Official React documentation covering useEffect cleanup behavior.",
                    search_rank=1,
                    retrieved_by_queries=[query_variant],
                ),
                tool._build_candidate(
                    second_url,
                    title="React cleanup article",
                    snippet="Third-party article about cleanup behavior.",
                    search_rank=2,
                    retrieved_by_queries=[query_variant],
                ),
            ]

        async def fake_crawl(
            urls,
            query=None,
            timeout_s=None,
            __event_emitter__=None,
        ):
            if len(urls) > 1:
                return {"error": "batch timeout", "details": "timeout"}
            if urls[0] == second_url:
                if tool._active_query_metadata is not None:
                    tool._record_failed_url(
                        tool._active_query_metadata,
                        second_url,
                        "timeout",
                        stage="crawl_single_retry",
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

        results = await tool.search_and_crawl(
            query="react useEffect cleanup example official documentation",
            depth="quick",
            max_results=2,
            fresh=True,
        )

        metadata = tool.last_query_metadata
        assert len(results) == 1, results
        assert metadata["search"]["candidate_count"] == 2, metadata
        assert metadata["crawl"]["attempted"] == 2, metadata
        assert metadata["crawl"]["batch_failures"] == 1, metadata
        assert metadata["crawl"]["single_retry_attempts"] == 2, metadata
        assert metadata["crawl"]["single_retry_successes"] == 1, metadata
        assert metadata["crawl"]["failed"] >= 1, metadata
        assert metadata["fallbacks_used"] == ["batch_to_single"], metadata
        assert any(
            item["url"] == second_url for item in metadata["crawl"]["failed_urls"]
        ), metadata

    asyncio.run(scenario())


def test_cli_can_include_search_metadata(monkeypatch):
    async def fake_search_and_crawl(*args, **kwargs):
        return [
            {
                "url": "https://example.com/doc",
                "title": "Example",
                "page_id": "page123",
                "summary": "Example summary",
                "key_points": ["Example summary"],
            }
        ]

    class FakeTool:
        def __init__(self):
            self.last_query_metadata = {
                "query": "example query",
                "crawl": {"failed": 1},
            }

        async def search_and_crawl(self, *args, **kwargs):
            return await fake_search_and_crawl(*args, **kwargs)

        async def read_page(self, *args, **kwargs):
            return None

    async def scenario():
        from sourceweave_web_search import cli as cli_module

        monkeypatch.setattr(
            cli_module, "build_tools", lambda valve_overrides=None: FakeTool()
        )
        args = cli_module.parse_args(["--query", "example query", "--include-metadata"])
        payload = await cli_module.run_cli(args)
        assert payload["search_metadata"]["query"] == "example query", payload
        assert payload["search_metadata"]["crawl"]["failed"] == 1, payload

    asyncio.run(scenario())


@pytest.mark.integration
def test_run_tool_call_batches_read_page_results():
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

    assert isinstance(payload.get("search_and_crawl"), list), payload
    assert len(payload["search_and_crawl"]) == 2, payload

    batched_read = payload.get("read_page")
    assert isinstance(batched_read, dict), payload
    assert batched_read["returned_pages"] == 2, batched_read
    assert len(batched_read["requested_page_ids"]) == 2, batched_read
    assert len(batched_read["pages"]) == 2, batched_read
    assert not batched_read["errors"], batched_read


def test_generate_query_variants_adds_retrieval_expansion():
    tool = Tools()

    variants = tool._generate_query_variants(
        "How do I find a free gold price api with no key and historical data"
    )

    assert (
        variants[0]
        == "How do I find a free gold price api with no key and historical data"
    )
    assert len(variants) >= 2, variants
    assert "how" not in variants[1].lower(), variants
    assert any(
        variant.startswith("api historical free")
        or variant.startswith("api historical free gold")
        for variant in variants[1:]
    ), variants
    assert not any("site:reddit.com" in variant for variant in variants), variants


def test_generate_query_variants_adds_reddit_only_when_requested():
    tool = Tools()

    variants = tool._generate_query_variants(
        "reddit discussion about a free gold price api with no key"
    )

    assert any("site:reddit.com" in variant for variant in variants), variants


def test_pre_crawl_reranker_demotes_generic_homepages():
    tool = Tools()
    query = "free gold price api no key historical data"

    candidates = [
        tool._build_candidate(
            "https://broker.example/",
            title="Broker Example Home",
            snippet="Enterprise trading platform pricing and brokerage accounts.",
            search_rank=1,
            retrieved_by_queries=[query],
        ),
        tool._build_candidate(
            "https://docs.gold.example/api/historical",
            title="Free gold price API historical endpoint",
            snippet="Historical gold prices with JSON output and no API key.",
            search_rank=4,
            retrieved_by_queries=[query],
        ),
        tool._build_candidate(
            "https://vendor.example/pricing",
            title="Gold API pricing",
            snippet="Plans, signup, and paid tiers for commodity market data.",
            search_rank=2,
            retrieved_by_queries=[query],
        ),
    ]

    for candidate in candidates:
        candidate["pre_crawl_score"] = tool._score_search_candidate(candidate, query)

    ranked = tool._rank_candidates(candidates, max_per_domain=3)

    assert ranked[0]["url"] == "https://docs.gold.example/api/historical", ranked
    assert ranked[-1]["url"] == "https://broker.example/", ranked


def test_search_and_crawl_uses_multi_query_reranking(monkeypatch):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False
        seen_queries = []
        crawl_calls = []

        async def fake_search(query_variant, __event_emitter__=None):
            seen_queries.append(query_variant)
            results = [
                tool._build_candidate(
                    "https://broker.example/",
                    title="Broker Example Home",
                    snippet="Trade commodities with enterprise plans and market access.",
                    search_rank=1,
                    retrieved_by_queries=[query_variant],
                ),
                tool._build_candidate(
                    "https://techtarget.example/definition/gold-api",
                    title="What is a gold API?",
                    snippet="Generic explanation of market data feeds for enterprises.",
                    search_rank=2,
                    retrieved_by_queries=[query_variant],
                ),
                tool._build_candidate(
                    "https://docs.gold.example/api/historical",
                    title="Free gold price API historical endpoint",
                    snippet="Historical gold prices with JSON output and no API key.",
                    search_rank=3,
                    retrieved_by_queries=[query_variant],
                ),
                tool._build_candidate(
                    "https://vendor.example/pricing",
                    title="Gold API pricing",
                    snippet="Plans, signup, and paid tiers for commodity market data.",
                    search_rank=4,
                    retrieved_by_queries=[query_variant],
                ),
                tool._build_candidate(
                    "https://github.example/open-gold-api",
                    title="Open gold API historical dataset",
                    snippet="Free historical gold data and sample endpoints without auth.",
                    search_rank=5,
                    retrieved_by_queries=[query_variant],
                ),
            ]
            if "free gold price api no key historical data" != query_variant:
                results = results[1:] + [
                    tool._build_candidate(
                        "https://docs.gold.example/api/quickstart",
                        title="Gold API quickstart and historical guide",
                        snippet="Quickstart for free historical gold API requests with JSON examples.",
                        search_rank=2,
                        retrieved_by_queries=[query_variant],
                    )
                ]
            return results

        async def fake_crawl(urls, query=None, timeout_s=None, __event_emitter__=None):
            crawl_calls.extend(urls)
            page_bodies = {
                "https://broker.example/": (
                    "Broker Example",
                    "# Broker Example\n\nEnterprise brokerage homepage with pricing, accounts, and trading tools.",
                ),
                "https://techtarget.example/definition/gold-api": (
                    "What is a gold API?",
                    "# What is a gold API?\n\nA short definition of market data APIs for enterprises.",
                ),
                "https://docs.gold.example/api/historical": (
                    "Free gold price API historical endpoint",
                    "# Historical endpoint\n\nThis endpoint returns historical gold prices in JSON and does not require an API key. "
                    "Free access includes time series data, historical daily values, and simple REST examples."
                    "\n\n## Example\n\nGET /historical?symbol=XAUUSD",
                ),
                "https://vendor.example/pricing": (
                    "Gold API pricing",
                    "# Pricing\n\nContact sales for enterprise pricing.",
                ),
                "https://github.example/open-gold-api": (
                    "Open gold API historical dataset",
                    "# Open gold API\n\nA free historical gold dataset with example endpoints and no auth requirement."
                    "\n\nThe repository documents CSV downloads, JSON mirrors, and sample integrations.",
                ),
                "https://docs.gold.example/api/quickstart": (
                    "Gold API quickstart and historical guide",
                    "# Quickstart\n\nFree historical gold API examples, quickstart steps, and endpoint documentation.",
                ),
            }
            results = []
            for url in urls:
                title, content = page_bodies[url]
                page_id = tool._page_store.put(url, title, content)
                compact = tool._build_compact_summary(content, query or "")
                results.append(
                    {
                        "url": url,
                        "title": title,
                        "page_id": page_id,
                        "summary": compact["summary"],
                        "key_points": compact["key_points"],
                        "content_length": len(content),
                        "images": [],
                        "content_type": "html",
                        "_content": content,
                    }
                )
            return {"content": results, "images": []}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_and_crawl(
            query="free gold price api no key historical data",
            depth="quick",
            max_results=2,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert len(results) == 2, results
        assert len(seen_queries) >= 2, seen_queries
        assert not any("site:reddit.com" in query for query in seen_queries), (
            seen_queries
        )
        assert len(crawl_calls) == 4, crawl_calls
        assert "https://broker.example/" not in crawl_calls, crawl_calls

        top_urls = [result["url"] for result in results]
        assert "https://docs.gold.example/api/historical" in top_urls, results
        assert any(
            "github.example" in url or "quickstart" in url for url in top_urls
        ), results

        for result in results:
            for key in (
                "source_type",
                "content_type",
                "search_rank",
                "search_snippet",
                "pre_crawl_score",
                "post_crawl_score",
                "discovered_from",
            ):
                assert key in result, result
            assert result["post_crawl_score"] >= result["pre_crawl_score"] * 0.2, result

    asyncio.run(scenario())


def test_search_and_crawl_honors_requested_max_results_above_default(monkeypatch):
    async def scenario():
        tool = Tools()
        tool._cache.enabled = False

        async def fake_search(query_variant, __event_emitter__=None):
            return [
                tool._build_candidate(
                    f"https://docs{idx}.example/api/{idx}",
                    title=f"Historical gold API page {idx}",
                    snippet="Free historical gold API docs and examples without an API key.",
                    search_rank=idx,
                    retrieved_by_queries=[query_variant],
                )
                for idx in range(1, 13)
            ]

        async def fake_crawl(urls, query=None, timeout_s=None, __event_emitter__=None):
            results = []
            for url in urls:
                idx = int(url.rsplit("/", 1)[-1])
                content = (
                    f"# Historical gold API page {idx}\n\n"
                    "Free historical gold API docs, JSON examples, and endpoint notes without an API key."
                )
                page_id = tool._page_store.put(
                    url, f"Historical gold API page {idx}", content
                )
                compact = tool._build_compact_summary(content, query or "")
                results.append(
                    {
                        "url": url,
                        "title": f"Historical gold API page {idx}",
                        "page_id": page_id,
                        "summary": compact["summary"],
                        "key_points": compact["key_points"],
                        "content_length": len(content),
                        "images": [],
                        "content_type": "html",
                        "_content": content,
                    }
                )
            return {"content": results, "images": []}

        monkeypatch.setattr(tool, "_search_searxng", fake_search)
        monkeypatch.setattr(tool, "_crawl_url", fake_crawl)

        results = await tool.search_and_crawl(
            query="free gold price api no key historical data",
            depth="normal",
            max_results=8,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert len(results) == 8, results

    asyncio.run(scenario())


def test_crawl_url_handles_missing_status_code(monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        async def json(self):
            return self.payload

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
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

    async def scenario():
        tool = Tools()
        negative_cache_keys = []

        async def fake_setex(key, ttl_s, value):
            negative_cache_keys.append((key, ttl_s, value))

        monkeypatch.setattr(
            "sourceweave_web_search.tool.aiohttp.ClientSession", FakeSession
        )
        monkeypatch.setattr(tool._cache, "setex", fake_setex)

        result = await tool._crawl_url(["https://bad.example/"], query="gold api")

        assert result == {"content": [], "images": []}, result
        assert negative_cache_keys, negative_cache_keys

    asyncio.run(scenario())


def test_build_tools_syncs_cache_and_normalizes_searxng_runtime_overrides():
    tool = build_tools(
        valve_overrides={
            "SEARXNG_BASE_URL": "http://search.example/search?lang=en",
            "CACHE_REDIS_URL": "redis://cache.example:6379/9",
            "CACHE_ENABLED": False,
        }
    )

    assert (
        tool.valves.SEARXNG_BASE_URL
        == "http://search.example/search?lang=en&format=json&q=<query>"
    )
    assert tool._cache.url == "redis://cache.example:6379/9"
    assert tool._cache.enabled is False
    assert tool._cache._redis is None
    assert tool._cache._unavailable_until == 0.0


def test_runtime_overrides_reset_cache_state_and_replace_fixed_searxng_query():
    tool = Tools()
    tool._cache._unavailable_until = 99.0

    RuntimeOverrides(
        valve_overrides={
            "SEARXNG_BASE_URL": "http://search.example/search?q=stale&lang=en&format=html",
        }
    ).apply(tool)

    assert (
        tool.valves.SEARXNG_BASE_URL
        == "http://search.example/search?q=<query>&lang=en&format=json"
    )
    assert tool._cache.url == tool.valves.CACHE_REDIS_URL
    assert tool._cache.enabled is tool.valves.CACHE_ENABLED
    assert tool._cache._redis is None
    assert tool._cache._unavailable_until == 0.0
