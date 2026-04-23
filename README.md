# SourceWeave Web Search

![Python 3.12+](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![MIT License](https://img.shields.io/badge/License-MIT-111111?logo=opensourceinitiative&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-stdio%20%7C%20HTTP-0EA5E9)
![Docker managed runtime](https://img.shields.io/badge/Docker-managed%20runtime-2496ED?logo=docker&logoColor=white)

Search-first MCP server and CLI for web research.

<!-- mcp-name: io.github.MRNAQA/sourceweave-web-search -->

> [!NOTE]
> `sourceweave-search-mcp` is the default local entrypoint. When explicit `SOURCEWEAVE_SEARCH_*` endpoint variables are absent, it discovers or starts the local Docker-backed stack automatically. If you already run the services yourself, set explicit endpoints and it will use them instead.

[Overview](#overview) • [Getting started](#getting-started) • [Managed local runtime](#managed-local-runtime) • [MCP client setup](#mcp-client-setup) • [CLI](#cli) • [Container deployments](#container-deployments) • [OpenWebUI](#openwebui) • [Runtime configuration](#runtime-configuration) • [Development](#development)

## Overview

SourceWeave Web Search gives MCP clients a compact three-tool contract for web research:

- `search_web(query, domains?, urls?, effort?)` discovers sources and returns compact results with stable `page_id` handles.
- `read_pages(page_ids, focus?)` reads stored pages by `page_id`.
- `read_urls(urls, focus?)` reads direct URLs without searching first.

It combines:

| Component | Role |
| --- | --- |
| SearXNG | Search discovery |
| Crawl4AI | Clean HTML extraction |
| Redis or Valkey | Persisted page cache and `page_id` store |
| MarkItDown | Document conversion for PDFs and other supported files |

## Getting started

### Requirements

- Python `3.12+`
- Docker with Compose support for the default managed local runtime
- Explicit `SOURCEWEAVE_SEARCH_*` endpoints only if you want hosted or self-managed services

### Managed local runtime

Run the server from the published package:

```bash
uvx --from sourceweave-web-search sourceweave-search-mcp
```

Or start the MCP server over HTTP:

```bash
uvx --from sourceweave-web-search sourceweave-search-mcp \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8000
```

When no endpoint env vars are set, `sourceweave-search-mcp`:

| Mode | What happens |
| --- | --- |
| Managed stack found | Join the existing SourceWeave-managed stack for the current runtime state directory |
| Healthy external stack found | Reuse the canonical local ports `19080`, `19235`, and `16379` without ownership |
| No reusable stack | Start and supervise a Docker-backed stack on canonical or free local ports |

Managed state lives under `~/.sourceweave-local/managed-runtime`. Multiple MCP processes on the same machine share one managed stack per state directory.

> [!IMPORTANT]
> Managed runtime removes containers only when the last active SourceWeave-managed process exits. Named volumes are preserved, so cache data survives restarts. If the original owning process dies, a later process can recover the same stack from Docker project identity and persisted runtime state.

### Explicit endpoint mode

If you already run SearXNG, Crawl4AI, and Redis or Valkey yourself, or want to point at hosted services, set explicit endpoints and the MCP entrypoint will bypass managed Docker startup:

```bash
SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://127.0.0.1:19080/search?format=json&q=<query>" \
SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://127.0.0.1:19235" \
SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://127.0.0.1:16379/2" \
uvx --from sourceweave-web-search sourceweave-search-mcp
```

### Direct CLI

`sourceweave-search` runs the tool directly. Use it when the supporting services are already available or when you provide explicit endpoints. It does not start Docker.

```bash
sourceweave-search --query "python programming" --read-first-pages 2
sourceweave-search --read-url "https://packaging.python.org/en/latest/"
```

> [!TIP]
> The direct CLI also accepts `--searxng-base-url`, `--crawl4ai-base-url`, and `--cache-redis-url` overrides.

## MCP client setup

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
      "enabled": true,
      "timeout": 300000
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
      "timeout": 300000
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
      ]
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

### Claude Code

Example `.mcp.json`:

```json
{
  "mcpServers": {
    "sourceweave": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "sourceweave-web-search",
        "sourceweave-search-mcp"
      ]
    }
  }
}
```

For a project-scoped shared config, place the same block in `.mcp.json` at the repo root.

## CLI

The direct CLI is useful once the supporting services are already reachable. It gives you the same search-first workflow without the MCP wrapper.

```bash
sourceweave-search --query "react useEffect cleanup example" --read-first-page
sourceweave-search --query "HTTP overview" --domain developer.mozilla.org --read-first-page
sourceweave-search --read-url "https://packaging.python.org/en/latest/"
```

## Container deployments

The managed local runtime is for host-side `uvx` or `uv run` launches. Containerized deployments still use explicit endpoint wiring.

- Image: `ghcr.io/mrnaqa/sourceweave-web-search-mcp`
- Repo-local compose entrypoint: `docker compose up -d --build mcp`

Example container run:

```bash
docker run --rm -p 8000:8000 \
  -e SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://host.docker.internal:19080/search?format=json&q=<query>" \
  -e SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://host.docker.internal:19235" \
  -e SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://host.docker.internal:16379/2" \
  ghcr.io/mrnaqa/sourceweave-web-search-mcp:latest
```

## OpenWebUI

This repo also ships a generated standalone OpenWebUI tool file at `artifacts/sourceweave_web_search.py`.

From a repo checkout, verify it is in sync with the canonical implementation:

```bash
uv run sourceweave-build-openwebui --check
```

Paste that artifact into OpenWebUI when you want the standalone tool-file deployment path. The generated file rewrites the default endpoints to the repo-local compose service names so it matches the container deployment path out of the box.

## Runtime configuration

Optional environment variables:

| Variable | Purpose |
| --- | --- |
| `SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL` | SearXNG URL template. Must contain `<query>`. |
| `SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL` | Crawl4AI base URL. |
| `SOURCEWEAVE_SEARCH_CACHE_REDIS_URL` | Redis or Valkey URL used for caching. |
| `FASTMCP_HOST` | Host for `sse` or `streamable-http` transport. |
| `FASTMCP_PORT` | Port for `sse` or `streamable-http` transport. |

If the endpoint variables are unset, `sourceweave-search-mcp` defaults to managed local runtime.

- Canonical host endpoints remain the preferred defaults and the external-reuse probe targets.
- A SourceWeave-managed stack may use different free host ports when the canonical defaults are already occupied.
- Multiple MCP processes on the same machine share one managed stack per local runtime state directory.

Default endpoint values:

- SearXNG: `http://127.0.0.1:19080/search?format=json&q=<query>`
- Crawl4AI: `http://127.0.0.1:19235`
- Redis: `redis://127.0.0.1:16379/2`

Default preferred host ports for managed startup:

- SearXNG: `19080`
- Crawl4AI: `19235`
- Redis: `16379`
- MCP: `8000` when run directly with `uvx`; `18000` at `/mcp` when using the repo's `mcp` compose service

## Development

```bash
git clone https://github.com/MRNAQA/sourceweave-web-search.git
cd sourceweave-web-search
uv sync --locked --group dev
uv run sourceweave-search-mcp
```

Useful checks:

```bash
uv run sourceweave-build-openwebui --check
uv run sourceweave-search-mcp --help
uv run pytest tests/test_config.py tests/test_packaging.py tests/test_tool.py tests/test_managed_runtime.py -m "not integration"
```
