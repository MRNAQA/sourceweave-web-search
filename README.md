# SourceWeave Web Search

SourceWeave Web Search is an MCP server and CLI for web search plus follow-up page reading.

It uses SearXNG for search, Crawl4AI for HTML extraction, and Redis or Valkey for caching.

For most users, the setup is simple:

1. run the supporting services locally in containers, or point at existing external endpoints
2. start the MCP server with `uvx`
3. connect your MCP client to the running server over `stdio` or local HTTP

## Key Features

- MCP server with `stdio`, `sse`, and `streamable-http` transports
- lean search plus follow-up page reading for MCP clients
- explicit per-URL document conversion for PDFs and other supported documents
- focused reads, related-link limits, image metadata, and page-quality hints
- publishable Python package, container image, and generated OpenWebUI artifact
- compatible with OpenCode, VS Code Copilot, and other MCP clients

## Requirements

- Python `3.12+`
- a reachable SearXNG endpoint
- a reachable Crawl4AI endpoint
- a reachable Redis or Valkey instance

Optional:

- Docker and Docker Compose for the repo-local stack

## Recommended Local Deployment

Start the supporting services locally:

```bash
git clone https://github.com/MRNAQA/sourceweave-web-search.git
cd sourceweave-web-search
cp .env.example .env
docker compose up -d redis crawl4ai searxng
```

Then start the MCP server from the published package with `uvx` and point it at those local endpoints:

```bash
SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://127.0.0.1:19080/search?format=json&q=<query>" \
SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://127.0.0.1:19235" \
SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://127.0.0.1:16379/2" \
uvx --from sourceweave-web-search sourceweave-search-mcp
```

For a local HTTP MCP endpoint instead of `stdio`:

```bash
SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://127.0.0.1:19080/search?format=json&q=<query>" \
SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://127.0.0.1:19235" \
SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://127.0.0.1:16379/2" \
uvx --from sourceweave-web-search sourceweave-search-mcp \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8000
```

You can also point the same `uvx` command at externally hosted SearXNG, Crawl4AI, and Redis or Valkey endpoints by changing the environment variables.

## Installation Options

### Python package

Published releases can be installed from PyPI:

```bash
pip install sourceweave-web-search
```

Or run directly without a global install:

```bash
uvx --from sourceweave-web-search sourceweave-search-mcp
uvx --from sourceweave-web-search sourceweave-search --query "python programming"
```

### Repo checkout

For local development or source-based runs:

```bash
git clone https://github.com/MRNAQA/sourceweave-web-search.git
cd sourceweave-web-search
uv sync --locked --group dev
uv run sourceweave-search-mcp
```

### Container image

The release workflow can publish a container image to:

- `ghcr.io/mrnaqa/sourceweave-web-search`
- optionally `docker.io/mrnaqa/sourceweave-web-search` when Docker Hub publishing is configured

Example runtime:

```bash
docker run --rm -p 8000:8000 \
  -e SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://host.docker.internal:19080/search?format=json&q=<query>" \
  -e SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://host.docker.internal:19235" \
  -e SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://host.docker.internal:16379/2" \
  ghcr.io/mrnaqa/sourceweave-web-search:latest
```

## Runtime Configuration

Set these environment variables:

| Variable | Purpose |
| --- | --- |
| `SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL` | SearXNG URL template. Must contain `<query>`. |
| `SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL` | Crawl4AI base URL. |
| `SOURCEWEAVE_SEARCH_CACHE_REDIS_URL` | Redis or Valkey URL used for caching. |
| `FASTMCP_HOST` | Host for `sse` or `streamable-http` transport. |
| `FASTMCP_PORT` | Port for `sse` or `streamable-http` transport. |

Example:

```bash
SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://127.0.0.1:19080/search?format=json&q=<query>" \
SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://127.0.0.1:19235" \
SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://127.0.0.1:16379/2" \
sourceweave-search --query "python programming" --read-first-pages 2
```

## Quick Start

The CLI is useful for smoke testing the runtime outside an MCP client.

Search and immediately read the first results:

```bash
sourceweave-search --query "python programming" --read-first-pages 2
```

Read a discovered page and include stored related links:

```bash
sourceweave-search \
  --query "react useEffect cleanup example" \
  --read-first-page \
  --related-links-limit 3
```

Force document conversion for an explicit URL:

```bash
sourceweave-search \
  --query "guide pdf" \
  --url '{"url": "https://example.com/guide.pdf", "convert_document": true}'
```

## MCP Server

Run over stdio:

```bash
sourceweave-search-mcp
```

Run as a local HTTP endpoint:

```bash
sourceweave-search-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

## What MCP Clients Get

MCP clients receive a simple two-step flow:

- a search step that returns compact results plus `page_id` handles
- a follow-up page-read step that returns stored content, focused excerpts, related-link summaries, image metadata, and page-quality hints when relevant

Human operators usually only need to know how to run the server and where to point the runtime endpoints. MCP clients handle the exact tool parameters.

## MCP Client Setup

### OpenCode

Example `opencode.json` / `opencode.jsonc` / `~/.config/opencode/opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "sourceweave": {
      "type": "local",
      "command": [
        "uvx",
        "--from",
        "sourceweave-web-search",
        "sourceweave-search-mcp"
      ],
      "environment": {
        "SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL": "http://127.0.0.1:19080/search?format=json&q=<query>",
        "SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL": "http://127.0.0.1:19235",
        "SOURCEWEAVE_SEARCH_CACHE_REDIS_URL": "redis://127.0.0.1:16379/2"
      },
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

For a shared HTTP endpoint instead:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "sourceweave": {
      "type": "remote",
      "url": "http://127.0.0.1:18000/mcp",
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

### VS Code Copilot

Example `.vscode/mcp.json`:

```json
{
  "servers": {
    "sourceweave": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "sourceweave-web-search",
        "sourceweave-search-mcp"
      ],
      "env": {
        "SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL": "http://127.0.0.1:19080/search?format=json&q=<query>",
        "SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL": "http://127.0.0.1:19235",
        "SOURCEWEAVE_SEARCH_CACHE_REDIS_URL": "redis://127.0.0.1:16379/2"
      }
    }
  }
}
```

For a shared HTTP endpoint instead:

```json
{
  "servers": {
    "sourceweave": {
      "type": "http",
      "url": "http://127.0.0.1:18000/mcp"
    }
  }
}
```

## Publishing

The manual release workflow at `.github/workflows/release.yml` accepts a changelog and can optionally:

- publish the wheel and sdist to PyPI
- publish the container image to GHCR
- mirror the container image to Docker Hub when Docker Hub credentials are configured

Releases always attach the built distributions and `artifacts/sourceweave_web_search.py` to the GitHub release.

For contributor setup and publishing requirements, see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## OpenWebUI

This repo also ships a generated standalone OpenWebUI tool file at `artifacts/sourceweave_web_search.py`.

From a repo checkout, verify it is in sync with the canonical implementation:

```bash
uv run sourceweave-build-openwebui --check
```

Paste that artifact into OpenWebUI when you want the standalone tool-file deployment path.

## Defaults

Default host-side endpoints used by the package:

- SearXNG: `http://127.0.0.1:19080/search?format=json&q=<query>`
- Crawl4AI: `http://127.0.0.1:19235`
- Redis: `redis://127.0.0.1:16379/2`

Default repo-local ports:

- SearXNG: `19080`
- Crawl4AI: `19235`
- Redis: `16379`
- MCP: `8000` when run directly with `uvx`; `18000` at `/mcp` when using the repo's `mcp` compose service

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local development, verification, packaging notes, and release workflow details.

## License

This project is licensed under the MIT License. See [`LICENSE`](LICENSE).
