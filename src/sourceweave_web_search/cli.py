import argparse
import asyncio
import json
from typing import Any, Sequence

from sourceweave_web_search.config import build_tools


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call the SourceWeave Web Search tool directly from the package CLI."
    )
    parser.add_argument("--query", default="", help="Query for search_web")
    parser.add_argument(
        "--domain",
        dest="domains",
        action="append",
        default=[],
        help="Optional domain constraint for search_web. Repeatable.",
    )
    parser.add_argument(
        "--url",
        dest="urls",
        action="append",
        default=[],
        help=(
            "Optional URL to crawl alongside search results. Repeatable. "
            "Supported document URLs such as PDFs are converted automatically when detected."
        ),
    )
    parser.add_argument("--read-first-page", action="store_true")
    parser.add_argument(
        "--read-first-pages",
        type=int,
        default=0,
        help="After search, batch-read the first N returned page_ids in a single read_pages call.",
    )
    parser.add_argument(
        "--read-page-id",
        dest="read_page_ids",
        action="append",
        default=[],
        help="Read one or more page_ids. Repeat this flag to batch them into a single read_pages call.",
    )
    parser.add_argument(
        "--read-url",
        dest="read_urls",
        action="append",
        default=[],
        help=(
            "Read one or more direct URLs without running search_web first. Repeatable. "
            "Supported document URLs such as PDFs are converted automatically when detected."
        ),
    )
    parser.add_argument("--focus", default="")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--include-metadata",
        action="store_true",
        help="Include per-query debug metadata in CLI output.",
    )
    parser.add_argument(
        "--searxng-base-url",
        default=None,
        help="Optional override for SEARXNG_BASE_URL. The SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL env var works too.",
    )
    parser.add_argument(
        "--crawl4ai-base-url",
        default=None,
        help="Optional override for CRAWL4AI_BASE_URL. The SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL env var works too.",
    )
    parser.add_argument(
        "--cache-redis-url",
        default=None,
        help="Optional override for CACHE_REDIS_URL. The SOURCEWEAVE_SEARCH_CACHE_REDIS_URL env var works too.",
    )
    return parser.parse_args(argv)


def _page_ids_from_results(results: Any, count: int) -> list[str]:
    if not isinstance(results, list) or count <= 0:
        return []

    return [
        result.get("page_id", "")
        for result in results[:count]
        if result.get("page_id", "")
    ]


def _targets_from_raw_args(raw_values: Sequence[str]) -> list[str] | None:
    normalized_urls: list[str] = []
    for raw_value in raw_values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        normalized_urls.append(value)
    return normalized_urls or None


def _urls_from_args(args: argparse.Namespace) -> list[str] | None:
    return _targets_from_raw_args(args.urls)


def _read_urls_from_args(args: argparse.Namespace) -> list[str] | None:
    return _targets_from_raw_args(args.read_urls)


def _valve_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "SEARXNG_BASE_URL": args.searxng_base_url,
        "CRAWL4AI_BASE_URL": args.crawl4ai_base_url,
        "CACHE_REDIS_URL": args.cache_redis_url,
    }


async def _read_pages(tool: Any, page_ids: list[str], focus: str) -> Any:
    if not page_ids:
        return None

    return await tool.mcp_read_pages(page_ids=page_ids, focus=focus)


async def _read_urls(tool: Any, urls: list[str] | None, focus: str) -> Any:
    if not urls:
        return None

    return await tool.mcp_read_urls(urls=urls, focus=focus)


async def run_cli(args: argparse.Namespace) -> dict[str, Any]:
    if not args.query and not args.read_page_ids and not args.read_urls:
        raise SystemExit("Provide --query, --read-page-id, or --read-url")

    tool = build_tools(valve_overrides=_valve_overrides_from_args(args))
    payload: dict[str, Any] = {}

    if args.query:
        results = await tool.mcp_search_web(
            query=args.query,
            domains=args.domains or None,
            urls=_urls_from_args(args),
        )
        payload["search_web"] = results
        if args.include_metadata:
            payload["search_metadata"] = tool.last_query_metadata

        read_first_count = max(args.read_first_pages, 1 if args.read_first_page else 0)
        page_ids = _page_ids_from_results(results, read_first_count)
        read_payload = await _read_pages(
            tool,
            page_ids,
            args.focus,
        )
        if read_payload is not None:
            payload["read_pages"] = read_payload

    if args.read_page_ids:
        requested_page_ids = [page_id for page_id in args.read_page_ids if page_id]
        payload["read_pages"] = await _read_pages(
            tool,
            requested_page_ids,
            args.focus,
        )

    if args.read_urls:
        payload["read_urls"] = await _read_urls(
            tool, _read_urls_from_args(args), args.focus
        )

    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    payload = asyncio.run(run_cli(args))
    print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
