FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

LABEL io.modelcontextprotocol.server.name="io.github.mrnaqa/sourceweave-web-search"

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
