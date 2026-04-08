"""BM25 content filtering checks via the Crawl4AI HTTP API."""

import sys
import time

import requests
from crawl4ai import (
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    DefaultMarkdownGenerator,
    DefaultTableExtraction,
)
from crawl4ai.content_filter_strategy import BM25ContentFilter, PruningContentFilter


CRAWL4AI_URL = "http://localhost:19235"


def build_payload(url, query=None, bm25_threshold=1.0):
    if query and bm25_threshold > 0:
        content_filter = BM25ContentFilter(
            user_query=query, bm25_threshold=bm25_threshold
        )
    else:
        content_filter = PruningContentFilter()

    markdown_generator = DefaultMarkdownGenerator(
        content_filter=content_filter,
        options={"ignore_links": True, "escape_html": False, "body_width": 80},
    )
    crawler_config = CrawlerRunConfig(
        markdown_generator=markdown_generator,
        table_extraction=DefaultTableExtraction(),
        cache_mode=CacheMode.BYPASS,
        page_timeout=60000,
        word_count_threshold=200,
    )
    browser_config = BrowserConfig(headless=True, light_mode=True)
    return {
        "urls": [url],
        "browser_config": browser_config.dump(),
        "crawler_config": crawler_config.dump(),
    }


def extract_content(result):
    markdown = result.get("markdown", {})
    if isinstance(markdown, dict):
        return markdown.get("fit_markdown", "") or markdown.get("raw_markdown", "")
    return str(markdown)


def test_bm25_vs_pruning():
    url = "https://docs.docker.com/compose/install/"
    query = "docker compose installation linux"

    print("=== Test: BM25 vs Pruning on relevant page ===")
    payload = build_payload(url, query=None)
    start = time.time()
    response = requests.post(f"{CRAWL4AI_URL}/crawl", json=payload, timeout=60)
    pruning_time = time.time() - start
    pruning_content = extract_content(response.json()["results"][0])
    print(f"Pruning:  {len(pruning_content):>6} chars  ({pruning_time:.1f}s)")

    payload = build_payload(url, query=query, bm25_threshold=1.0)
    start = time.time()
    response = requests.post(f"{CRAWL4AI_URL}/crawl", json=payload, timeout=60)
    bm25_time = time.time() - start
    bm25_content = extract_content(response.json()["results"][0])
    reduction = round((1 - len(bm25_content) / max(len(pruning_content), 1)) * 100, 1)
    print(
        f"BM25@1.0: {len(bm25_content):>6} chars  ({bm25_time:.1f}s)  reduction: {reduction}%"
    )

    payload = build_payload(url, query=query, bm25_threshold=0.5)
    response = requests.post(f"{CRAWL4AI_URL}/crawl", json=payload, timeout=60)
    bm25_loose = extract_content(response.json()["results"][0])
    print(f"BM25@0.5: {len(bm25_loose):>6} chars  (loose)")


def test_bm25_irrelevant_query():
    payload = build_payload(
        "https://en.wikipedia.org/wiki/Docker_(software)",
        query="chocolate cake recipe",
        bm25_threshold=1.0,
    )
    response = requests.post(f"{CRAWL4AI_URL}/crawl", json=payload, timeout=60)
    content = extract_content(response.json()["results"][0])
    print(f"Docker page + 'chocolate cake' query: {len(content)} chars")


def test_bm25_disabled():
    payload = build_payload("https://example.com", query="test", bm25_threshold=0)
    response = requests.post(f"{CRAWL4AI_URL}/crawl", json=payload, timeout=60)
    content = extract_content(response.json()["results"][0])
    print(f"Content: {len(content)} chars")


if __name__ == "__main__":
    try:
        requests.get(f"{CRAWL4AI_URL}/health", timeout=5)
    except Exception:
        print(f"ERROR: Crawl4AI not reachable at {CRAWL4AI_URL}")
        sys.exit(1)

    test_bm25_vs_pruning()
    test_bm25_irrelevant_query()
    test_bm25_disabled()
