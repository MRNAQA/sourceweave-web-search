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
            "and use site: when a specific domain matters."
        )
    ),
]

SearchUrls = Annotated[
    list[str | UrlTarget] | None,
    Field(
        description=(
            "Optional specific URLs to crawl in addition to search results. Each item may be "
            "either a plain URL string or an object with per-URL options like convert_document."
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
    Field(description="If true, bypass cached search and page results for this call."),
]

ReadPageIds = Annotated[
    list[str],
    Field(
        description=(
            "One or more page_ids returned by search_and_crawl. Batch related pages into one "
            "call when comparing or synthesizing multiple sources."
        )
    ),
]

ReadFocus = Annotated[
    str,
    Field(
        description=(
            "Optional focus phrase used to extract the most relevant sections from stored page "
            "content. Use short topic phrases, exact errors, function names, or concepts."
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
            "Use concise retrieval-style queries, quote exact errors, and add site: filters when domain preference matters. "
            "Returns compact summaries plus page_ids. Use read_page next when you need full content. "
            "If you already know an important URL, pass it in urls; use convert_document for explicit document URLs like PDFs. "
            "Low-utility crawled pages may include page_quality such as challenge or blocked."
        ),
    )
    async def search_and_crawl(
        query: SearchQuery,
        urls: SearchUrls = None,
        depth: SearchDepth = "normal",
        max_results: SearchMaxResults = None,
        fresh: SearchFresh = False,
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
            "Prefer batching related page_ids in a single call. Use focus to extract the most relevant sections. "
            "Use related_links_limit=0 when you only want page content without page-adjacent links. "
            "Returned pages may include page_quality when a page looks challenge-like or blocked."
        ),
    )
    async def read_page(
        page_ids: ReadPageIds,
        focus: ReadFocus = "",
        related_links_limit: ReadRelatedLinksLimit = 3,
        max_chars: ReadMaxChars = 8000,
    ):
        return await tool_instance.read_page(
            page_ids,
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    build_mcp_server().run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
