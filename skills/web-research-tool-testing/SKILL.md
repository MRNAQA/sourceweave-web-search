---
name: web-research-tool-testing
description: Use this whenever the user wants to validate `web_research_tool.py` locally before deployment, especially when they mention testing a tool without the target app, simulating model tool calls, checking `search_and_crawl` and `read_page` outputs, or confirming deployment-aligned defaults and valves. This skill should also trigger when the user wants to debug whether the tool will work in deployment without changing service URLs or ports.
---

# Web Research Tool Testing

Use this repo as a standalone harness. Do not add the target app back into the local workflow unless the user explicitly asks for it.

The canonical source now lives under `src/web_research_studio/tool.py`; `web_research_tool.py` is the generated standalone OpenWebUI artifact.

## Goal

Validate the pasted-tool behavior directly:

- instantiate `Tools()`
- call `search_and_crawl(...)`
- inspect the returned list shape and summaries
- call `read_page(page_ids=[...], ...)`
- confirm the default service URLs match deployment-style container names and ports

## Default assumptions

Assume the tool should work with these defaults unless the user explicitly says deployment changed:

- `SEARXNG_BASE_URL = http://searxng:8080/search?format=json&q=<query>`
- `CRAWL4AI_BASE_URL = http://crawl4ai:11235`
- `CACHE_REDIS_URL = redis://redis:6379/2`
- `SEARCH_WITH_SEARXNG = true`

These are deployment-aligned internal Docker network values, not localhost values.

## Workflow

1. Bring up the dependency stack.

```bash
docker compose up -d redis searxng crawl4ai
```

2. Run a direct model-style tool call from the `tester` container.

```bash
docker compose run --rm tester uv run web-research-studio \
  --query "python programming" \
  --depth quick \
  --read-first-pages 2 \
  --pretty
```

3. Check that `search_and_crawl` returns:

- a JSON list
- at least one result for a healthy stack
- `url`, `title`, `page_id`, `summary`, and `key_points`

4. Check that `read_page` returns:

- no `error`
- the expected titles/urls
- enough cleaned content to be useful

5. If needed, run the automated checks.

```bash
docker compose run --rm tester uv run pytest tests/test_packaging.py
docker compose run --rm tester uv run pytest tests/test_tool.py
docker compose run --rm tester uv run python tests/test_phase4.py
```

## What to look for

Prioritize functional risks over cosmetic issues:

- empty search result sets
- Crawl4AI request failures or timeouts
- page IDs that cannot be read back
- broken summaries or missing required keys
- deployment drift in service names, ports, or URLs

## Response style

Report findings first. Keep the summary brief. If the tool works, say that clearly and mention any remaining deployment-only risks separately.
