import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_research_tool import Tools


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call Web Research Studio directly from the local harness."
    )
    parser.add_argument("--query", default="", help="Query for search_and_crawl")
    parser.add_argument(
        "--url",
        dest="urls",
        action="append",
        default=[],
        help="Optional URL to crawl alongside search results. Repeatable.",
    )
    parser.add_argument(
        "--depth",
        choices=["quick", "normal", "deep"],
        default="normal",
        help="search_and_crawl depth",
    )
    parser.add_argument("--max-results", type=int, default=None)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--read-first-page", action="store_true")
    parser.add_argument(
        "--read-first-pages",
        type=int,
        default=0,
        help="After search, batch-read the first N returned page_ids in a single read_page call.",
    )
    parser.add_argument(
        "--read-page-id",
        dest="read_page_ids",
        action="append",
        default=[],
        help="Read one or more page_ids. Repeat this flag to batch them into a single read_page call.",
    )
    parser.add_argument("--focus", default="")
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def _page_ids_from_results(results: Any, count: int) -> list[str]:
    if not isinstance(results, list) or count <= 0:
        return []

    return [
        result.get("page_id", "")
        for result in results[:count]
        if result.get("page_id", "")
    ]


async def _read_pages(
    tool: Tools, page_ids: list[str], focus: str, max_chars: int
) -> Any:
    if not page_ids:
        return None

    return await tool.read_page(
        page_ids[0] if len(page_ids) == 1 else page_ids,
        focus=focus,
        max_chars=max_chars,
    )


async def main() -> None:
    args = parse_args()
    if not args.query and not args.read_page_ids:
        raise SystemExit("Provide --query or --read-page-id")

    tool = Tools()
    payload: dict[str, Any] = {}

    if args.query:
        results = await tool.search_and_crawl(
            query=args.query,
            urls=args.urls or None,
            depth=args.depth,
            max_results=args.max_results,
            fresh=args.fresh,
        )
        payload["search_and_crawl"] = results

        read_first_count = max(args.read_first_pages, 1 if args.read_first_page else 0)
        page_ids = _page_ids_from_results(results, read_first_count)
        read_payload = await _read_pages(tool, page_ids, args.focus, args.max_chars)
        if read_payload is not None:
            payload["read_page"] = read_payload

    if args.read_page_ids:
        requested_page_ids = [page_id for page_id in args.read_page_ids if page_id]
        payload["read_page"] = await _read_pages(
            tool,
            requested_page_ids,
            args.focus,
            args.max_chars,
        )

    print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
