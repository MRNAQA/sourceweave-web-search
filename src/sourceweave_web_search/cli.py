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
        "--url",
        dest="urls",
        action="append",
        default=[],
        help=(
            "Optional URL to crawl alongside search results. Repeatable. "
            "May be a plain URL string or a JSON object like "
            '\'{"url": "https://example.com/file.pdf", "convert_document": true}\'.'
        ),
    )
    parser.add_argument(
        "--related-links-limit",
        type=int,
        default=3,
        help="Maximum number of stored related links to include per read_pages result. Use 0 to omit them.",
    )
    parser.add_argument(
        "--depth",
        choices=["quick", "normal", "deep"],
        default="normal",
        help="search_web depth",
    )
    parser.add_argument("--max-results", type=int, default=None)
    parser.add_argument("--fresh", action="store_true")
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
            "May be a plain URL string or a JSON object like "
            '\'{"url": "https://example.com/file.pdf", "convert_document": true}\'.'
        ),
    )
    parser.add_argument("--focus", default="")
    parser.add_argument("--max-chars", type=int, default=1200)
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


def _targets_from_raw_args(
    raw_values: Sequence[str], flag_name: str
) -> list[Any] | None:
    normalized_urls: list[Any] = []
    for raw_value in raw_values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        if value.startswith("{"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSON passed to {flag_name}: {exc}") from exc
            if not isinstance(parsed, dict) or not parsed.get("url"):
                raise SystemExit(
                    f"JSON passed to {flag_name} must be an object with at least a 'url' field"
                )
            normalized_urls.append(parsed)
            continue
        normalized_urls.append(value)
    return normalized_urls or None


def _urls_from_args(args: argparse.Namespace) -> list[Any] | None:
    return _targets_from_raw_args(args.urls, "--url")


def _read_urls_from_args(args: argparse.Namespace) -> list[Any] | None:
    return _targets_from_raw_args(args.read_urls, "--read-url")


def _valve_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "SEARXNG_BASE_URL": args.searxng_base_url,
        "CRAWL4AI_BASE_URL": args.crawl4ai_base_url,
        "CACHE_REDIS_URL": args.cache_redis_url,
    }


async def _read_pages(
    tool: Any,
    page_ids: list[str],
    urls: list[Any] | None,
    focus: str,
    related_links_limit: int,
    max_chars: int,
) -> Any:
    if not page_ids and not urls:
        return None

    return await tool.read_pages(
        page_ids=page_ids[0] if len(page_ids) == 1 else page_ids or None,
        urls=urls,
        focus=focus,
        related_links_limit=related_links_limit,
        max_chars=max_chars,
    )


async def run_cli(args: argparse.Namespace) -> dict[str, Any]:
    if not args.query and not args.read_page_ids and not args.read_urls:
        raise SystemExit("Provide --query, --read-page-id, or --read-url")

    tool = build_tools(valve_overrides=_valve_overrides_from_args(args))
    payload: dict[str, Any] = {}

    if args.query:
        results = await tool.search_web(
            query=args.query,
            urls=_urls_from_args(args),
            depth=args.depth,
            max_results=args.max_results,
            fresh=args.fresh,
        )
        payload["search_web"] = results
        if args.include_metadata:
            payload["search_metadata"] = tool.last_query_metadata

        read_first_count = max(args.read_first_pages, 1 if args.read_first_page else 0)
        page_ids = _page_ids_from_results(results, read_first_count)
        read_payload = await _read_pages(
            tool,
            page_ids,
            None,
            args.focus,
            args.related_links_limit,
            args.max_chars,
        )
        if read_payload is not None:
            payload["read_pages"] = read_payload

    if args.read_page_ids:
        requested_page_ids = [page_id for page_id in args.read_page_ids if page_id]
        payload["read_pages"] = await _read_pages(
            tool,
            requested_page_ids,
            None,
            args.focus,
            args.related_links_limit,
            args.max_chars,
        )

    if args.read_urls:
        payload["read_pages"] = await _read_pages(
            tool,
            [],
            _read_urls_from_args(args),
            args.focus,
            args.related_links_limit,
            args.max_chars,
        )

    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    payload = asyncio.run(run_cli(args))
    print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
