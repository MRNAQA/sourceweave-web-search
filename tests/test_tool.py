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
