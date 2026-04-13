FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

LABEL io.modelcontextprotocol.server.name="io.github.MRNAQA/sourceweave-web-search" \
      org.opencontainers.image.title="sourceweave-web-search-mcp" \
      org.opencontainers.image.description="MCP server and CLI for web search and page reading with SearXNG, Crawl4AI, and Redis" \
      org.opencontainers.image.source="https://github.com/MRNAQA/sourceweave-web-search" \
      org.opencontainers.image.url="https://github.com/MRNAQA/sourceweave-web-search" \
      org.opencontainers.image.version="0.2.1" \
      org.opencontainers.image.vendor="Mohammad ElNaqa" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

ENV HOME=/tmp \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    FASTMCP_HOST=0.0.0.0 \
    FASTMCP_PORT=8000

COPY LICENSE README.md pyproject.toml uv.lock /app/
COPY src /app/src

RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["uv", "run", "sourceweave-search-mcp", "--transport", "streamable-http"]
