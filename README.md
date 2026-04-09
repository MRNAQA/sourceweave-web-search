# Web Research Studio

Standalone dev repo for a deployment-ready web search and crawl tool.

## What It Includes

- `web_research_tool.py`: the tool file you can paste into your target app
- `docker-compose.yml`: local stack with `redis`, `crawl4ai`, `searxng`, and a `tester` container
- `scripts/run_tool_call.py`: direct model-style invocation harness for `search_and_crawl` and batched `read_page`
- `tests/`: standalone integration checks that call the tool directly
- `skills/web-research-tool-testing/`: repo-local skill describing the local testing workflow

## Local Setup

1. Start the local dependency stack:

```bash
docker compose up -d redis searxng crawl4ai
```

2. Run a direct tool call the same way a model would use it:

```bash
docker compose run --rm tester uv run python scripts/run_tool_call.py \
  --query "python programming" \
  --depth quick \
  --read-first-pages 2 \
  --pretty
```

That command instantiates `Tools()`, calls `search_and_crawl(...)`, and optionally follows up with `read_page(page_ids=[...])` using the first returned page IDs in one batch.

## Automated Checks

Run the standalone runtime checks inside the `tester` container:

```bash
docker compose run --rm tester uv run pytest tests/test_tool.py
docker compose run --rm tester uv run python tests/test_phase4.py
```

`tests/test_tool.py` verifies the main tool contract:

- `search_and_crawl(query=..., depth=...)` returns results with `page_id`
- `read_page(page_ids=[...])` batches full content reads across one or more pages
- cached `page_id` lookups still work from a fresh `Tools()` instance

`tests/test_phase4.py` keeps the lower-level Crawl4AI BM25 checks.

## Defaults

The tool defaults are intentionally aligned with deployment-style container names and ports so you do not need to change valves before deployment:

- SearXNG: `http://searxng:8080/search?format=json&q=<query>`
- Crawl4AI: `http://crawl4ai:11235`
- Redis: `redis://redis:6379/2`

The compose file keeps those same service names on the internal Docker network. Host ports are exposed only for optional manual debugging.

Default host ports used by this repo:

- SearXNG: `19080`
- Crawl4AI: `19235`

## Notes

- The repo is now a standalone local test harness.
- Paste `web_research_tool.py` into your target app manually when you are ready to deploy.
- `read_page(page_ids=[...])` now supports batched page reads and still falls back to Redis-backed cached page content, so follow-up reads are more stable across fresh tool instances.
