import argparse
import os
from typing import Annotated
from typing import Literal
from typing import Sequence

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel
from pydantic import Field

from sourceweave_web_search.config import build_tools
from sourceweave_web_search.tool import Tools


class UrlTarget(BaseModel):
    url: str = Field(description="Absolute URL to crawl or convert.")
    convert_document: bool = Field(
        default=False,
        description="Force document conversion for this URL when it points to a document such as a PDF.",
    )


SearchQuery = Annotated[
    str,
    Field(
        description=(
            "Search query. Prefer concise retrieval-style queries, quote exact errors, "
            "error codes, or function names, and use site: when a specific domain matters."
        )
    ),
]

SearchUrls = Annotated[
    list[str | UrlTarget] | None,
    Field(
        description=(
            "Optional specific URLs to crawl in addition to search results. Each item may be "
            "either a plain URL string or an object with per-URL options like convert_document. "
            "Use this when you already know a must-read page but still want it returned inside the same research pass."
        )
    ),
]

SearchDepth = Annotated[
    Literal["quick", "normal", "deep"],
    Field(
        description=(
            "How much search and crawl effort to spend. quick is fastest, normal is balanced, "
            "and deep explores more candidates."
        )
    ),
]

SearchMaxResults = Annotated[
    int | None,
    Field(description="Optional cap on how many summarized results to return."),
]

SearchFresh = Annotated[
    bool,
    Field(
        description=(
            "If true, bypass SourceWeave's cached search and page results for this call and force a fresh upstream fetch. "
            "Use when freshness matters more than latency."
        )
    ),
]

ReadPageIds = Annotated[
    list[str] | None,
    Field(
        description=(
            "Optional page_ids returned by search_web. Batch related pages into one "
            "call when comparing or synthesizing multiple sources. Prefer this over repeated single-page fetches."
        )
    ),
]

ReadUrls = Annotated[
    list[str | UrlTarget] | None,
    Field(
        description=(
            "Optional direct URLs to read without running search_web first. Each item may be "
            "either a plain URL string or an object with per-URL options like convert_document. "
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

ReadRelatedLinksLimit = Annotated[
    int,
    Field(
        description=(
            "Maximum number of stored related links to return per page. Use 0 to omit the links "
            "array while still returning related_links_total and related_links_more_available."
        ),
        ge=0,
    ),
]

ReadMaxChars = Annotated[
    int,
    Field(description="Maximum number of characters to return per page."),
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
            "Search the web for relevant sources and crawl the most useful pages into a reusable research set. "
            "Returns compact summaries, key points, metadata, and stable page_ids for follow-up reading. "
            "Prefer this over generic web search when you need source discovery plus structured follow-up reads. "
            "Use concise retrieval-style queries, quote exact errors, and add site: filters when domain preference matters. "
            "If you already know an important URL, pass it in urls; use convert_document for explicit document URLs like PDFs. "
            "Use read_pages next when summaries are not enough or when you want to batch full reads across multiple sources."
        ),
    )
    async def search_web(
        query: SearchQuery,
        urls: SearchUrls = None,
        depth: SearchDepth = "normal",
        max_results: SearchMaxResults = None,
        fresh: SearchFresh = False,
    ):
        return await tool_instance.search_web(
            query=query,
            urls=urls,
            depth=depth,
            max_results=max_results,
            fresh=fresh,
        )

    @server.tool(
        name="read_pages",
        description=(
            "Retrieve cleaned, synthesis-ready content for one or more pages. Use it either after search_web with page_ids "
            "or as a standalone direct-URL reader when you already know what to read and do not need a search step first. "
            "Prefer batching related page_ids or URLs in a single call. Use this instead of a generic webfetch-style tool "
            "when you want cleaned extraction, focused reads, related links, or page-quality hints. "
            "Leave focus empty for a normal cleaned read. Use related_links_limit=0 when you only want page content without page-adjacent links."
        ),
    )
    async def read_pages(
        page_ids: ReadPageIds = None,
        urls: ReadUrls = None,
        focus: ReadFocus = "",
        related_links_limit: ReadRelatedLinksLimit = 3,
        max_chars: ReadMaxChars = 8000,
    ):
        return await tool_instance.read_pages(
            page_ids=page_ids,
            urls=urls,
            focus=focus,
            related_links_limit=related_links_limit,
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
    build_mcp_server(host=args.host, port=args.port).run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
