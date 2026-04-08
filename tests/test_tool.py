"""Standalone integration checks for the Web Research Studio stack."""

import sys

import requests


SEARXNG_URL = "http://localhost:19080"
CRAWL4AI_URL = "http://localhost:19235"


def test_searxng_health():
    print("=== SearXNG Health ===")
    try:
        response = requests.get(f"{SEARXNG_URL}/healthz", timeout=5)
        print(f"  Status: {response.status_code}")
        print("  PASS" if response.status_code == 200 else "  FAIL")
        return response.status_code == 200
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def test_searxng_json_search():
    print("\n=== SearXNG JSON Search ===")
    try:
        response = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": "python programming", "format": "json"},
            timeout=30,
        )
        print(f"  Status: {response.status_code}")
        if response.status_code != 200:
            print(f"  FAIL: HTTP {response.status_code}")
            print(f"  Body: {response.text[:200]}")
            return False

        data = response.json()
        results = data.get("results", [])
        print(f"  Query: {data.get('query')}")
        print(f"  Results: {len(results)}")
        for idx, result in enumerate(results[:3], start=1):
            print(f"    [{idx}] {result.get('title', 'N/A')[:60]}")
            print(f"        {result.get('url', 'N/A')[:80]}")
        print("  PASS" if results else "  FAIL: no results")
        return bool(results)
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def test_crawl4ai_health():
    print("\n=== Crawl4AI Health ===")
    try:
        response = requests.get(f"{CRAWL4AI_URL}/health", timeout=10)
        print(f"  Status: {response.status_code}")
        print(f"  Body: {response.text[:200]}")
        print("  PASS" if response.status_code == 200 else "  FAIL")
        return response.status_code == 200
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def test_crawl4ai_crawl():
    print("\n=== Crawl4AI Crawl ===")
    try:
        payload = {
            "urls": ["https://example.com"],
            "crawler_config": {
                "cache_mode": "bypass",
                "page_timeout": 30000,
                "stream": False,
            },
        }
        response = requests.post(
            f"{CRAWL4AI_URL}/crawl",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        print(f"  Status: {response.status_code}")
        if response.status_code != 200:
            print(f"  FAIL: HTTP {response.status_code}")
            print(f"  Body: {response.text[:300]}")
            return False

        data = response.json()
        results = data.get("results", [])
        print(f"  Results count: {len(results)}")
        for result in results:
            print(f"    URL: {result.get('url', 'N/A')}")
            print(f"    Title: {result.get('metadata', {}).get('title', 'N/A')}")
            print(f"    Success: {result.get('success', False)}")
        passed = len(results) > 0 and results[0].get("success", False)
        print("  PASS" if passed else "  FAIL")
        return passed
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def test_end_to_end():
    print("\n=== End-to-End: Search + Crawl ===")
    try:
        search_response = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": "what is docker compose", "format": "json"},
            timeout=30,
        )
        if search_response.status_code != 200:
            print(f"  FAIL: Search returned {search_response.status_code}")
            return False
        results = search_response.json().get("results", [])
        if not results:
            print("  FAIL: No search results")
            return False

        urls = [result.get("url") for result in results[:3] if result.get("url")]
        print(f"  Found {len(urls)} URLs")
        crawl_response = requests.post(
            f"{CRAWL4AI_URL}/crawl",
            json={
                "urls": [urls[0]],
                "crawler_config": {
                    "cache_mode": "bypass",
                    "page_timeout": 30000,
                    "stream": False,
                    "word_count_threshold": 200,
                },
            },
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if crawl_response.status_code != 200:
            print(f"  FAIL: Crawl returned {crawl_response.status_code}")
            return False

        crawl_results = crawl_response.json().get("results", [])
        passed = bool(crawl_results) and crawl_results[0].get("success", False)
        print("  PASS" if passed else "  FAIL")
        return passed
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def main():
    print("Web Research Studio - Integration Tests")
    print("=" * 50)
    print(f"SearXNG: {SEARXNG_URL}")
    print(f"Crawl4AI: {CRAWL4AI_URL}")
    print()

    results = {
        "searxng_health": test_searxng_health(),
        "searxng_search": test_searxng_json_search(),
        "crawl4ai_health": test_crawl4ai_health(),
        "crawl4ai_crawl": test_crawl4ai_crawl(),
        "end_to_end": test_end_to_end(),
    }

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_pass = False
    print(f"\n{'All tests passed!' if all_pass else 'Some tests FAILED.'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
