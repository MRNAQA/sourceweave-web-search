import argparse
import os
from typing import Annotated
from typing import Literal
from typing import Sequence

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from sourceweave_web_search.config import build_tools
from sourceweave_web_search.managed_runtime import ManagedRuntimeError
from sourceweave_web_search.managed_runtime import resolve_managed_runtime
from sourceweave_web_search.tool import Tools


SearchQuery = Annotated[
    str,
    Field(
        description=(
            "Search query. Prefer concise retrieval-style queries and quote exact errors, error codes, or function names when relevant."
        )
    ),
]

SearchDomains = Annotated[
    list[str] | None,
    Field(
        description=(
            "Optional domains to constrain results to, such as docs.python.org or developer.mozilla.org."
        )
    ),
]

SearchUrls = Annotated[
    list[str] | None,
    Field(
        description=(
            "Optional specific URLs to crawl in addition to search results. Pass plain URL strings. "
            "Supported document URLs such as PDFs are converted automatically when detected. "
            "Use this when you already know a must-read page but still want it returned inside the same research pass."
        )
    ),
]

SearchEffort = Annotated[
    Literal["quick", "normal", "deep"],
    Field(
        description=(
            "Optional search effort. Use quick for narrow, time-sensitive, or single-answer lookups, or when urls already identify the must-read page; "
            "examples: weather forecast, stock price, today's exchange rate, or reading one known URL. "
            "Use normal for most docs lookup, troubleshooting, and focused research; examples: Python requests timeout error, React useEffect cleanup, API docs for OAuth refresh tokens. "
            "Use deep for broad, ambiguous, or synthesis-heavy research; examples: compare vector databases, research browser automation tools, summarize the current landscape of local RAG stacks. "
            "Avoid deep for simple weather-like lookups."
        )
    ),
]

ReadPageIds = Annotated[
    list[str],
    Field(
        description=(
            "Page_ids returned by search_web. Batch related pages into one "
            "call when comparing or synthesizing multiple sources. Prefer this over repeated single-page fetches."
        )
    ),
]

ReadUrls = Annotated[
    list[str],
    Field(
        description=(
            "Direct URLs to read without running search_web first. Pass plain URL strings. "
            "Supported document URLs such as PDFs are converted automatically when detected. "
            "Use this when discovery is unnecessary and you want cleaned content immediately."
        )
    ),
]

ReadFocus = Annotated[
    str,
    Field(
        description=(
            "Optional focus phrase used to extract the most relevant sections from page content. "
            "Leave it empty for a normal cleaned read. Use short topic phrases, exact errors, "
            "function names, or concepts when you want a focused excerpt."
        )
    ),
]


def _mcp_host() -> str:
    return os.getenv("FASTMCP_HOST", "127.0.0.1")


def _mcp_port() -> int:
    return int(os.getenv("FASTMCP_PORT", "8000"))


def build_mcp_server(
    tool: Tools | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> FastMCP:
    tool_instance = tool or build_tools()
    server = FastMCP(
        "sourceweave-web-search",
        host=host or _mcp_host(),
        port=port if port is not None else _mcp_port(),
    )

    @server.tool(
        name="search_web",
        description=(
            "Search the web for relevant sources and return compact summaries with stable page_ids for follow-up reads. "
            "Choose effort deliberately: quick for narrow current-fact lookups or explicit URLs, normal for most docs and troubleshooting, and deep for broad comparisons or synthesis-heavy research. "
            "Use domains when you want to constrain results to specific hosts."
        ),
    )
    async def search_web(
        query: SearchQuery,
        domains: SearchDomains = None,
        urls: SearchUrls = None,
        effort: SearchEffort = "normal",
    ):
        return await tool_instance.search_web(
            query=query,
            domains=domains,
            urls=urls,
            effort=effort,
        )

    @server.tool(
        name="read_pages",
        description=(
            "Retrieve cleaned content for one or more stored pages using page_ids returned by search_web. "
            "Batch related page_ids in one call when comparing multiple sources."
        ),
    )
    async def read_pages(
        page_ids: ReadPageIds,
        focus: ReadFocus = "",
    ):
        return await tool_instance.read_pages(
            page_ids=page_ids,
            focus=focus,
        )

    @server.tool(
        name="read_urls",
        description=(
            "Retrieve cleaned content for one or more direct URLs without running search_web first. "
            "Supported document URLs such as PDFs are converted automatically when detected."
        ),
    )
    async def read_urls(
        urls: ReadUrls,
        focus: ReadFocus = "",
    ):
        return await tool_instance.read_urls(
            urls=urls,
            focus=focus,
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
    parser.add_argument(
        "--host",
        help=(
            "Host to bind for sse or streamable-http transport. "
            "Ignored for stdio. Defaults to FASTMCP_HOST or 127.0.0.1."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        help=(
            "Port to bind for sse or streamable-http transport. "
            "Ignored for stdio. Defaults to FASTMCP_PORT or 8000."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with resolve_managed_runtime() as runtime:
            tool = build_tools(valve_overrides=runtime.valve_overrides)
            build_mcp_server(tool=tool, host=args.host, port=args.port).run(
                transport=args.transport
            )
    except ManagedRuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
