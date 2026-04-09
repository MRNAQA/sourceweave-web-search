# SourceWeave Web Search

Web search and crawl tool that ships in two forms:

- a generated standalone OpenWebUI tool file for copy/paste
- installable CLI and MCP entry points for `uv run` and `uvx`

## What It Includes

- `src/sourceweave_web_search/tool.py`: canonical tool implementation used by the package, CLI, and MCP server
- `artifacts/sourceweave_web_search.py`: generated standalone OpenWebUI artifact copied from `src/sourceweave_web_search/tool.py`
- `src/sourceweave_web_search/cli.py`: package CLI for direct tool calls
- `src/sourceweave_web_search/mcp_server.py`: MCP server entry point for `uvx` and MCP clients
- `src/sourceweave_web_search/build_openwebui.py`: build/check logic for the OpenWebUI artifact
- `docker-compose.yml`: root compose entrypoint with the `mcp` service and include-based dependency fragments
- `infrastructure/*.yml`: per-service compose fragments for `redis`, `crawl4ai`, and `searxng`
- `scripts/run_tool_call.py`: thin wrapper around the package CLI for the existing local harness workflow
- `scripts/build_openwebui_tool.py`: thin wrapper that regenerates or checks the standalone OpenWebUI file
- `tests/`: standalone integration checks that call the tool directly
- `skills/sourceweave-search-tool-testing/`: repo-local skill describing the local testing workflow

## Architecture

The code under `src/` is the source of truth.

- `src/sourceweave_web_search/tool.py` contains the real implementation.
- `artifacts/sourceweave_web_search.py` is an artifact generated from it.
- `search_and_crawl` and batched `read_page` are exposed through both the CLI and MCP server.

Keep edits in the main module and then verify the OpenWebUI artifact is still in sync:

```bash
uv run sourceweave-build-openwebui --check
```

## Local Setup

1. Start the MCP stack:

```bash
docker compose up -d mcp
```

That starts the `mcp` service and its included dependencies. The HTTP MCP endpoint is exposed at `http://localhost:18000/mcp`.

2. Run a direct tool call in a one-off container that uses the same service definition:

```bash
docker compose run --rm mcp uv run sourceweave-search \
  --query "python programming" \
  --depth quick \
  --read-first-pages 2 \
  --pretty
```

That command instantiates `Tools()`, calls `search_and_crawl(...)`, and optionally follows up with `read_page(page_ids=[...])` using the first returned page IDs in one batch.

The wrapper script still works too:

```bash
docker compose run --rm mcp uv run python scripts/run_tool_call.py \
  --query "python programming" \
  --depth quick \
  --read-first-pages 2 \
  --pretty
```

## MCP And uvx

Run the Docker-managed MCP server:

```bash
docker compose up -d mcp
```

Then connect a client to:

```text
http://localhost:18000/mcp
```

Run the MCP server from the repo:

```bash
uvx --from . sourceweave-search-mcp
```

Run the package CLI from the repo:

```bash
uvx --from . sourceweave-search --query "python programming" --read-first-pages 2
```

For host-side use, prefer explicit runtime overrides instead of changing code:

```bash
SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://localhost:19080/search?format=json&q=<query>" \
SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://localhost:19235" \
SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://localhost:16379/2" \
uvx --from . sourceweave-search --query "python programming" --read-first-pages 2
```

## Automated Checks

Run the standalone runtime checks inside a one-off `mcp` container:

```bash
docker compose run --rm mcp uv run pytest tests/test_packaging.py
docker compose run --rm mcp uv run pytest tests/test_tool.py
docker compose run --rm mcp uv run python tests/test_phase4.py
```

`tests/test_packaging.py` verifies the package surfaces:

- the generated OpenWebUI artifact is in sync with the package source
- the package CLI can be invoked
- the MCP server exposes `search_and_crawl` and `read_page`

`tests/test_tool.py` verifies the main tool contract:

- `search_and_crawl(query=..., depth=...)` returns results with `page_id`
- `read_page(page_ids=[...])` batches full content reads across one or more pages
- cached `page_id` lookups still work from a fresh `Tools()` instance

`tests/test_phase4.py` keeps the lower-level Crawl4AI BM25 checks.

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

## Notes

- The repo now supports both copy/paste OpenWebUI delivery and installable CLI/MCP delivery from the same codebase.
- Keep manual edits in `src/sourceweave_web_search/tool.py`, then regenerate or check `artifacts/sourceweave_web_search.py`.
- Paste `artifacts/sourceweave_web_search.py` into OpenWebUI when you are ready to deploy the standalone tool file.
- `read_page(page_ids=[...])` now supports batched page reads and still falls back to Redis-backed cached page content, so follow-up reads are more stable across fresh tool instances.
