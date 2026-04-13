# SourceWeave Web Search

SourceWeave Web Search is an MCP server and CLI for web search plus follow-up page reading.

It keeps discovery and full-page retrieval separate so agents can stay token-efficient:

- `search_web`: discover relevant sources and return compact summaries plus `page_id` handles
- `read_pages`: fetch one or more stored pages for full content, focused extraction, and optional related links

You provide the runtime services: SearXNG for search, Crawl4AI for HTML extraction, and Redis/Valkey for page and search caching.

This repo ships two delivery paths:

- an installable package that exposes `sourceweave-search` and `sourceweave-search-mcp`
- a repo-generated standalone OpenWebUI artifact at `artifacts/sourceweave_web_search.py`

## Choose The Right Workflow

### Published package users

Use the installed entry points and your own runtime services.

- install with `pip install sourceweave-web-search`
- or, once published, run with `uvx --from sourceweave-web-search ...`
- use `sourceweave-search` for direct CLI calls
- use `sourceweave-search-mcp` for MCP clients

The published wheel is intentionally lean. It does **not** install repo-only helpers like `docker-compose.yml`, `infrastructure/`, `scripts/`, `tests/`, `skills/`, or the checked-in OpenWebUI artifact under `artifacts/`.

### Repo checkout users

Use a git checkout when you need any of the following:

- the checked-in `artifacts/sourceweave_web_search.py` file for copy/paste deployment
- the local Docker Compose stack in `docker-compose.yml` and `infrastructure/*.yml`
- the wrapper scripts in `scripts/`
- the packaging and runtime tests in `tests/`

## What The Repo Includes

Shared package source:

- `src/sourceweave_web_search/tool.py`: canonical tool implementation used by the package, CLI, and MCP server
- `src/sourceweave_web_search/cli.py`: package CLI for direct tool calls
- `src/sourceweave_web_search/mcp_server.py`: MCP server entry point for `uvx` and MCP clients
- `src/sourceweave_web_search/build_openwebui.py`: build/check logic for the standalone OpenWebUI artifact

Repo-local helpers:

- `artifacts/sourceweave_web_search.py`: checked-in standalone OpenWebUI artifact copied from `src/sourceweave_web_search/tool.py`
- `docker-compose.yml`: root compose entrypoint with the `mcp` service and include-based dependency fragments
- `infrastructure/*.yml`: per-service compose fragments for `redis`, `crawl4ai`, and `searxng`
- `infrastructure/searxng-settings.yml`: tracked SearXNG tuning used by the local compose stack
- `scripts/run_tool_call.py`: thin wrapper around the package CLI for the local harness workflow
- `scripts/build_openwebui_tool.py`: thin wrapper that validates or regenerates the standalone OpenWebUI file from a repo checkout
- `tests/`: local integration and packaging checks
- `skills/sourceweave-search-tool-testing/`: repo-local skill describing the local testing workflow

## Architecture

The code under `src/` is the source of truth.

- `src/sourceweave_web_search/tool.py` contains the real implementation.
- `artifacts/sourceweave_web_search.py` is a generated artifact checked into the repo.
- `search_web` and `read_pages` are exposed through both the CLI and MCP server.

Current runtime behavior:

- `search_web` makes one SearXNG request per query and preserves that search order.
- Crawl4AI enriches HTML pages one URL at a time instead of batch-reranking results.
- document conversion is explicit per URL via `{"url": "...pdf", "convert_document": true}`; document hits are not auto-converted from normal search results.
- `search_web` stays lean and does not include related-link expansions in discovery results.
- `read_pages` returns stored full content plus state fields such as `content_type`, `source_type`, `content_source`, `full_content_available`, `focus_applied`, and `truncated`, and can include up to `related_links_limit` stored related links per page.

From a repo checkout, verify artifact drift without rewriting the artifact:

```bash
uv run sourceweave-build-openwebui --check
```

## Published Package Usage

After installing the package, call the CLI directly:

```bash
sourceweave-search --query "python programming" --read-first-pages 2
```

Read a discovered page and include a capped number of stored related links:

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

Run the MCP server from the installed package:

```bash
sourceweave-search-mcp
```

For a shareable local HTTP endpoint instead of stdio:

```bash
sourceweave-search-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

Or, once the package is published, use `uvx` without cloning the repo:

```bash
uvx --from sourceweave-web-search sourceweave-search --query "python programming" --read-first-pages 2
uvx --from sourceweave-web-search sourceweave-search-mcp
```

For host-side use, point the package at your own services with environment variables:

```bash
SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://localhost:19080/search?format=json&q=<query>" \
SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://localhost:19235" \
SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://localhost:16379/2" \
sourceweave-search --query "python programming" --read-first-pages 2
```

The published package does not start Redis, Crawl4AI, or SearXNG for you; it only exposes the Python entry points.

### Containerized MCP deployment

The repo also includes a production-style `Dockerfile` that installs the package into an image instead of bind-mounting the checkout:

```bash
docker build -t sourceweave-web-search .
docker run --rm -p 18000:8000 \
  -e SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://host.docker.internal:19080/search?format=json&q=<query>" \
  -e SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://host.docker.internal:19235" \
  -e SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://host.docker.internal:16379/2" \
  sourceweave-web-search
```

That image only packages the Python service. You still need to provide reachable SearXNG, Crawl4AI, and Redis endpoints through environment variables.

## OpenCode And VS Code Copilot

The easiest integration path for both clients is local `stdio` using the published package entry point. Use remote HTTP only when you already want a long-running shared MCP endpoint.

### OpenCode

OpenCode uses `mcp` entries in `opencode.json`, `opencode.jsonc`, or `~/.config/opencode/opencode.json`.

Published package via `uvx`:

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

If you are running from this repo checkout instead of an installed package, replace the command with `[
  "uv",
  "run",
  "sourceweave-search-mcp"
]` from the repo root.

If you already have the repo-local compose stack running and want OpenCode to connect to the shared HTTP endpoint instead:

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

VS Code and GitHub Copilot use `.vscode/mcp.json` or a user-profile `mcp.json` with a `servers` root key.

Recommended local `stdio` setup:

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

If you already started the repo-local MCP endpoint with Docker Compose or `sourceweave-search-mcp --transport streamable-http`, VS Code can also connect over HTTP:

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

Notes:

- `stdio` is the best default for local single-user use because it does not require a long-running server.
- HTTP is useful when you want to share one already-running MCP endpoint across multiple tools.
- VS Code requires the `type` field on each server entry.
- OpenCode uses `command` as an array and `environment`; VS Code uses `command` plus `args` and `env`.

## Repo-Local Setup

Optionally copy `.env.example` to `.env` before starting the local stack. The compose fragments use pinned image tags and local-only placeholder secrets so you can override them without editing tracked files. The local SearXNG service also mounts the tracked `infrastructure/searxng-settings.yml` file so engine tuning lives in the repo instead of an anonymous container volume.

```bash
cp .env.example .env
```

Start the MCP stack:

```bash
docker compose up -d mcp
```

That starts the `mcp` service and its included dependencies. The HTTP MCP endpoint is exposed at `http://localhost:18000/mcp`.

Run a direct tool call in a one-off container that uses the same service definition:

```bash
docker compose run --rm mcp uv run sourceweave-search \
  --query "python programming" \
  --depth quick \
  --read-first-pages 2 \
  --pretty
```

That command instantiates `Tools()`, calls `search_web(...)`, and optionally follows up with `read_pages(page_ids=[...])` using the first returned page IDs in one batch.

The wrapper script still works too:

```bash
docker compose run --rm mcp uv run python scripts/run_tool_call.py \
  --query "python programming" \
  --depth quick \
  --read-first-pages 2 \
  --pretty
```

From a repo checkout, `uvx --from .` still works for the package entry points:

```bash
uvx --from . sourceweave-search-mcp
uvx --from . sourceweave-search --query "python programming" --read-first-pages 2
```

## Defaults

The canonical tool and generated OpenWebUI artifact default to the published host ports:

- SearXNG: `http://127.0.0.1:19080/search?format=json&q=<query>`
- Crawl4AI: `http://127.0.0.1:19235`
- Redis: `redis://127.0.0.1:16379/2`

The Docker Compose `mcp` service overrides those values to the internal container names:

- SearXNG: `http://searxng:8080/search?format=json&q=<query>`
- Crawl4AI: `http://crawl4ai:11235`
- Redis: `redis://redis:6379/2`

Default host ports used by this repo:

- SearXNG: `19080`
- Crawl4AI: `19235`
- Redis: `16379`
- MCP: `18000` at `/mcp`

For safer checked-in deployment defaults, the compose fragments avoid floating image tags and expose overridable environment variables for the local-only SearXNG secret and Crawl4AI token. Replace those placeholders before exposing the stack beyond localhost.

## Automated Checks

Local release-gate checks:

```bash
uv run sourceweave-build-openwebui --check
uv run pytest tests/test_packaging.py
uv build --no-sources --sdist --wheel --out-dir dist/release-gate --clear --no-create-gitignore
```

Additional local runtime checks:

```bash
uv run pytest tests/test_tool.py
docker compose run --rm mcp uv run python tests/test_phase4.py
```

`tests/test_packaging.py` verifies that:

- the checked-in OpenWebUI artifact is validated in strict `--check` mode without being rewritten
- the package CLI can be invoked
- the MCP server exposes `search_web` and `read_pages`
- publishable wheels and sdists include the README metadata and LICENSE file while keeping repo-only files out of the wheel

`tests/test_tool.py` verifies the main tool contract:

- `search_web(query=..., depth=...)` returns results with `page_id` plus content-state fields such as `content_type`, `source_type`, `content_source`, and `full_content_available`
- `read_pages(page_ids=[...])` batches full content reads across one or more pages, reports `focus_applied` / `truncated`, and returns up to `related_links_limit` stored related links per page
- explicit per-URL document conversion works through the public tool contract
- cached `page_id` lookups still work from a fresh `Tools()` instance

`tests/test_phase4.py` keeps the lower-level Crawl4AI BM25 checks.

## Notes

- Keep manual edits in `src/sourceweave_web_search/tool.py`, then regenerate or check `artifacts/sourceweave_web_search.py` from a repo checkout.
- Paste `artifacts/sourceweave_web_search.py` into OpenWebUI when you are ready to deploy the standalone tool file.
- `read_pages(page_ids=[...])` supports batched page reads and still falls back to Redis-backed cached page content, so follow-up reads are more stable across fresh tool instances.
