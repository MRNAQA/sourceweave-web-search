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
    parser.add_argument("--read-page-id", default="")
    parser.add_argument("--focus", default="")
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if not args.query and not args.read_page_id:
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

        if args.read_first_page and isinstance(results, list) and results:
            page_id = results[0].get("page_id", "")
            if page_id:
                payload["read_page"] = await tool.read_page(
                    page_id,
                    focus=args.focus,
                    max_chars=args.max_chars,
                )

    if args.read_page_id:
        payload["read_page"] = await tool.read_page(
            args.read_page_id,
            focus=args.focus,
            max_chars=args.max_chars,
        )

    print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
