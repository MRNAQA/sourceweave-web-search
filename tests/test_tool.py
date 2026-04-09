"""Standalone runtime checks that call the tool directly."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sourceweave_web_search.tool import Tools


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def test_explicit_url_crawl_without_relying_on_search_ranking():
    async def scenario():
        tool = Tools()
        results = await tool.search_and_crawl(
            query="python about page",
            urls=["https://www.python.org/about/"],
            depth="quick",
            max_results=1,
            fresh=True,
        )

        assert isinstance(results, list), results
        assert results, "explicit URL crawl returned no results"
        assert results[0]["url"].startswith("https://www.python.org/about"), results[0]

    asyncio.run(scenario())


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

    assert variants[0] == "How do I find a free gold price api with no key and historical data"
    assert len(variants) >= 2, variants
    assert "how" not in variants[1].lower(), variants
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
            if "site:reddit.com" in query_variant:
                return [
                    tool._build_candidate(
                        "https://www.reddit.com/r/algotrading/comments/free_gold_api/",
                        title="Free gold price API without API key?",
                        snippet="Community thread comparing historical endpoints and no-key options.",
                        search_rank=1,
                        retrieved_by_queries=[query_variant],
                    )
                ]

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
                results = results[1:]
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
                "https://www.reddit.com/r/algotrading/comments/free_gold_api/": (
                    "Free gold price API without API key?",
                    "# Free gold price API without API key?\n\nSeveral users suggest historical gold APIs, public datasets, "
                    "and docs pages that do not require a key for basic use.",
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
        assert any("site:reddit.com" in query for query in seen_queries), seen_queries
        assert len(crawl_calls) == 4, crawl_calls
        assert "https://broker.example/" not in crawl_calls, crawl_calls

        top_urls = [result["url"] for result in results]
        assert "https://docs.gold.example/api/historical" in top_urls, results
        assert any("reddit.com" in url or "github.example" in url for url in top_urls), results

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

        monkeypatch.setattr("sourceweave_web_search.tool.aiohttp.ClientSession", FakeSession)
        monkeypatch.setattr(tool._cache, "setex", fake_setex)

        result = await tool._crawl_url(["https://bad.example/"], query="gold api")

        assert result == {"content": [], "images": []}, result
        assert negative_cache_keys, negative_cache_keys

    asyncio.run(scenario())
