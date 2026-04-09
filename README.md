# SourceWeave Web Search

Web search and crawl tool that ships in two forms:

- a generated standalone OpenWebUI tool file for copy/paste
- installable CLI and MCP entry points for `uv run` and `uvx`

## What It Includes

- `src/web_research_studio/tool.py`: canonical tool implementation used by the package, CLI, and MCP server
- `web_research_tool.py`: generated standalone OpenWebUI artifact copied from `src/web_research_studio/tool.py`
- `src/web_research_studio/cli.py`: package CLI for direct tool calls
- `src/web_research_studio/mcp_server.py`: MCP server entry point for `uvx` and MCP clients
- `src/web_research_studio/build_openwebui.py`: build/check logic for the OpenWebUI artifact
- `docker-compose.yml`: local stack with `redis`, `crawl4ai`, `searxng`, and a `tester` container
- `scripts/run_tool_call.py`: thin wrapper around the package CLI for the existing local harness workflow
- `scripts/build_openwebui_tool.py`: thin wrapper that regenerates or checks the standalone OpenWebUI file
- `tests/`: standalone integration checks that call the tool directly
- `skills/web-research-tool-testing/`: repo-local skill describing the local testing workflow

## Architecture

The code under `src/` is the source of truth.

- `src/web_research_studio/tool.py` contains the real implementation.
- `web_research_tool.py` is an artifact that stays byte-for-byte in sync with it.
- `search_and_crawl` and batched `read_page` are exposed through both the CLI and MCP server.

Keep edits in the main module and then verify the OpenWebUI artifact is still in sync:

```bash
uv run web-research-build-openwebui --check
```

## Local Setup

1. Start the local dependency stack:

```bash
docker compose up -d redis searxng crawl4ai
```

2. Run a direct tool call the same way a model would use it:

```bash
docker compose run --rm tester uv run web-research-studio \
  --query "python programming" \
  --depth quick \
  --read-first-pages 2 \
  --pretty
```

That command instantiates `Tools()`, calls `search_and_crawl(...)`, and optionally follows up with `read_page(page_ids=[...])` using the first returned page IDs in one batch.

The wrapper script still works too:

```bash
docker compose run --rm tester uv run python scripts/run_tool_call.py \
  --query "python programming" \
  --depth quick \
  --read-first-pages 2 \
  --pretty
```

## MCP And uvx

Run the MCP server from the repo:

```bash
uvx --from . web-research-mcp
```

Run the package CLI from the repo:

```bash
uvx --from . web-research-studio --query "python programming" --read-first-pages 2
```

For host-side use, prefer explicit runtime overrides instead of changing code:

```bash
WEB_RESEARCH_SEARXNG_BASE_URL="http://localhost:19080/search?format=json&q=<query>" \
WEB_RESEARCH_CRAWL4AI_BASE_URL="http://localhost:19235" \
WEB_RESEARCH_CACHE_REDIS_URL="redis://localhost:6379/2" \
uvx --from . web-research-studio --query "python programming" --read-first-pages 2
```

## Automated Checks

Run the standalone runtime checks inside the `tester` container:

```bash
docker compose run --rm tester uv run pytest tests/test_packaging.py
docker compose run --rm tester uv run pytest tests/test_tool.py
docker compose run --rm tester uv run python tests/test_phase4.py
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

The standalone OpenWebUI artifact keeps deployment-style container defaults so you do not need to change valves before deployment:

- SearXNG: `http://searxng:8080/search?format=json&q=<query>`
- Crawl4AI: `http://crawl4ai:11235`
- Redis: `redis://redis:6379/2`

The compose file keeps those same service names on the internal Docker network. Host ports are exposed only for optional manual debugging.

Default host ports used by this repo:

- SearXNG: `19080`
- Crawl4AI: `19235`

## Notes

- The repo now supports both copy/paste OpenWebUI delivery and installable CLI/MCP delivery from the same codebase.
- Keep manual edits in `src/web_research_studio/tool.py`, then regenerate or check `web_research_tool.py`.
- Paste `web_research_tool.py` into OpenWebUI when you are ready to deploy the standalone tool file.
- `read_page(page_ids=[...])` now supports batched page reads and still falls back to Redis-backed cached page content, so follow-up reads are more stable across fresh tool instances.
