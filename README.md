# SourceWeave Web Search

SourceWeave Web Search is an MCP server and CLI for web search plus follow-up page reading.

It is built around a simple two-step workflow:

- `search_web`: find relevant sources and return compact summaries plus `page_id` handles
- `read_pages`: read one or more returned pages in full, with optional focused extraction and related links

It uses SearXNG for search, Crawl4AI for HTML extraction, and Redis or Valkey for caching.

## Key Features

- MCP server with `stdio`, `sse`, and `streamable-http` transports
- two-step workflow that keeps discovery lean and full-page reads explicit
- explicit per-URL document conversion for PDFs and other supported documents
- focused reads, related-link limits, image metadata, and page-quality hints
- installable package, Docker image, and generated OpenWebUI artifact
- compatible with OpenCode, VS Code Copilot, and other MCP clients

## Requirements

- Python `3.12+`
- a reachable SearXNG endpoint
- a reachable Crawl4AI endpoint
- a reachable Redis or Valkey instance

Optional:

- Docker and Docker Compose for the repo-local stack

## Getting Started

### Install from PyPI

```bash
pip install sourceweave-web-search
```

Or run it without a global install:

```bash
uvx --from sourceweave-web-search sourceweave-search-mcp
uvx --from sourceweave-web-search sourceweave-search --query "python programming"
```

### Configure runtime services

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

### CLI

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

### MCP server

Run over stdio:

```bash
sourceweave-search-mcp
```

Run as a local HTTP endpoint:

```bash
sourceweave-search-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

## Tools

### `search_web`

Use `search_web` to discover sources and get compact summaries plus `page_id` handles.

Inputs:

- `query`: concise retrieval-style query; quote exact errors and use `site:` when domain preference matters
- `urls`: optional specific URLs to crawl in addition to search results; each item may be a string or `{ "url": "...", "convert_document": true }`
- `depth`: `quick`, `normal`, or `deep`
- `max_results`: optional result cap
- `fresh`: bypass cached search and page results for that call

Returns compact records including fields such as `page_id`, `summary`, `key_points`, `content_type`, `content_source`, `source_type`, `full_content_available`, optional `images`, and optional `page_quality` when a page looks blocked or challenge-like.

### `read_pages`

Use `read_pages` after `search_web` when you want the stored content for one or more `page_id` values.

Inputs:

- `page_ids`: one or more page IDs returned by `search_web`
- `focus`: optional phrase for focused extraction
- `related_links_limit`: maximum related links per page; use `0` to omit the `related_links` array while keeping totals and hints
- `max_chars`: maximum returned characters per page

Returns batched page content with fields such as `content`, `focus_applied`, `truncated`, `related_links_total`, `related_links_more_available`, optional `related_links`, optional `images`, and optional `page_quality`.

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

## Deployment Options

### Docker image

```bash
docker build -t sourceweave-web-search .
docker run --rm -p 18000:8000 \
  -e SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://host.docker.internal:19080/search?format=json&q=<query>" \
  -e SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://host.docker.internal:19235" \
  -e SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://host.docker.internal:16379/2" \
  sourceweave-web-search
```

### Repo-local Compose stack

```bash
git clone https://github.com/MRNAQA/sourceweave-web-search.git
cd sourceweave-web-search
cp .env.example .env
docker compose up -d mcp
```

That starts the MCP server plus SearXNG, Crawl4AI, and Redis. The HTTP MCP endpoint is exposed at `http://localhost:18000/mcp`.

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
- MCP: `18000` at `/mcp`

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local development, verification, packaging notes, and release workflow details.

## License

This project is licensed under the MIT License. See [`LICENSE`](LICENSE).
