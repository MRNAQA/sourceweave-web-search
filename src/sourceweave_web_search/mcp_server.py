import argparse
import os
from typing import Sequence

from mcp.server.fastmcp import FastMCP

from sourceweave_web_search.config import build_tools
from sourceweave_web_search.tool import Tools


def _mcp_host() -> str:
    return os.getenv("FASTMCP_HOST", "127.0.0.1")


def _mcp_port() -> int:
    return int(os.getenv("FASTMCP_PORT", "8000"))


def build_mcp_server(tool: Tools | None = None) -> FastMCP:
    tool_instance = tool or build_tools()
    server = FastMCP(
        "sourceweave-web-search",
        host=_mcp_host(),
        port=_mcp_port(),
    )

    @server.tool(
        name="search_and_crawl",
        description=(
            "Search the web for relevant sources and crawl the selected pages. "
            "Returns compact summaries plus page_ids. Use read_page next when you need full content."
        ),
    )
    async def search_and_crawl(
        query: str,
        urls: list[str] | None = None,
        depth: str = "normal",
        max_results: int | None = None,
        fresh: bool = False,
    ):
        return await tool_instance.search_and_crawl(
            query=query,
            urls=urls,
            depth=depth,
            max_results=max_results,
            fresh=fresh,
        )

    @server.tool(
        name="read_page",
        description=(
            "Retrieve the full cleaned content for one or more previously returned page_ids. "
            "Prefer batching related page_ids in a single call."
        ),
    )
    async def read_page(
        page_ids: list[str],
        focus: str = "",
        max_chars: int = 8000,
    ):
        return await tool_instance.read_page(
            page_ids,
            focus=focus,
            max_chars=max_chars,
        )

    return server


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the SourceWeave Web Search MCP server."
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport to run. stdio is the default for uvx-based MCP clients.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    build_mcp_server().run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
