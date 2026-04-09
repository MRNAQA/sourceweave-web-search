"""Standalone runtime checks that call the tool directly."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_research_tool import Tools


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
