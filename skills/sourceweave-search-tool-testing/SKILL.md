---
name: sourceweave-search-tool-testing
description: Use this whenever the user wants to validate SourceWeave Web Search locally before deployment or after runtime changes, especially when they mention smoke-testing `search_web` and `read_pages`, checking whether `src/sourceweave_web_search/tool.py` and `artifacts/sourceweave_web_search.py` are still aligned, confirming host-side package defaults versus compose `mcp` container defaults, testing direct URL or document reads, or debugging whether the tool will work in deployment without wiring it back into a separate app.
---

# SourceWeave Web Search Tool Testing

Use this repo as a standalone harness. Do not add a target app back into the workflow unless the user explicitly asks for it.

The canonical source lives under `src/sourceweave_web_search/tool.py`. `artifacts/sourceweave_web_search.py` is a generated standalone OpenWebUI artifact and should be validated with the build/check command instead of treated as the source of truth.

## Goal

Validate the current standalone behavior directly:

- confirm the OpenWebUI artifact is still in sync with the canonical source
- call `search_web(...)` through the repo's package CLI or direct tool runtime
- inspect the returned list shape, summaries, and runtime metadata when useful
- call `read_pages(...)` with batched `page_ids` or direct `urls`
- distinguish package host defaults from compose `mcp` container-network defaults
- verify direct URL and document-conversion behavior when the user is exercising those paths
- remember that Redis or Valkey now backs persisted `page_id` reuse; only same-call direct reads should work without persistence

## Default assumptions

There are two valid default contexts in this repo. Do not blur them together.

Package and test defaults use host-side endpoints:

- `SEARXNG_BASE_URL = http://127.0.0.1:19080/search?format=json&q=<query>`
- `CRAWL4AI_BASE_URL = http://127.0.0.1:19235`
- `CACHE_REDIS_URL = redis://127.0.0.1:16379/2`
- `SEARCH_WITH_SEARXNG = true`

The repo's compose `mcp` service injects deployment-aligned container-network values:

- `SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL = http://searxng:8080/search?format=json&q=<query>`
- `SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL = http://crawl4ai:11235`
- `SOURCEWEAVE_SEARCH_CACHE_REDIS_URL = redis://redis:6379/2`

Only describe the container-name values as the defaults when the check is running inside the compose `mcp` service.

## Workflow

1. Verify the generated artifact is still aligned with the canonical source.

```bash
uv run sourceweave-build-openwebui --check
```

2. Bring up the repo-local dependency stack for package-level checks.

```bash
docker compose up -d redis crawl4ai searxng
```

3. Run the package CLI as the primary smoke-test harness.

```bash
uv run sourceweave-search \
  --query "python programming" \
  --depth quick \
  --read-first-pages 2 \
  --include-metadata \
  --pretty
```

4. Check that `search_web` returns:

- a JSON list
- at least one result for a healthy stack
- `url`, `title`, `page_id`, `summary`, `key_points`, `content_type`, `source_type`, `content_source`, and `full_content_available`
- optional `images` or `page_quality` only when the crawled page warrants them
- descriptions and behavior that still reinforce the intended workflow: discover with `search_web`, then batch follow-up reads with `read_pages`, while also making it clear that `read_pages` can be used standalone with direct URLs when discovery is unnecessary

5. Check that batched `read_pages` returns:

- an object with `requested_page_ids`, `returned_pages`, `pages`, and `errors`
- no `errors` for a healthy stack
- enough cleaned content to be useful
- `related_links` omitted when not requested in search results, but available on page reads when stored
- descriptions and behavior that position `read_pages` as the preferred alternative to generic `webfetch`-style tools when cleaned extraction, focused reads, batching, related links, or page-quality hints matter

6. When the user is testing direct URL reads, use the explicit read path instead of forcing everything through `search_web`.

Read an HTML page directly:

```bash
uv run sourceweave-search \
  --read-url "https://packaging.python.org/en/latest/" \
  --related-links-limit 1 \
  --pretty
```

Read a document URL only when `convert_document` is set:

```bash
uv run sourceweave-search \
  --read-url '{"url": "https://example.com/guide.pdf", "convert_document": true}' \
  --pretty
```

If the same document URL is passed without `convert_document`, expect an error telling you to reissue the request with `convert_document: true`.

7. Use the compose `mcp` service only when you specifically need deployment-aligned container-network behavior without overriding service URLs.

```bash
docker compose up -d mcp
docker compose run --rm mcp uv run sourceweave-search \
  --query "python programming" \
  --depth quick \
  --read-first-page \
  --include-metadata \
  --pretty
```

8. If needed, run the automated checks.

```bash
uv run pytest tests/test_tool.py tests/test_config.py tests/test_packaging.py -q -p no:cacheprovider
uv run ruff check src tests
uv run mypy
docker compose run --rm mcp uv run python tests/test_phase4.py
```

## What to look for

Prioritize functional risks over cosmetic issues:

- empty search result sets
- Crawl4AI request failures or timeouts
- page IDs that cannot be read back, especially in batched reads or fresh follow-up reads
- direct URL reads that fail when HTML should work
- document reads that do not enforce `convert_document` correctly
- broken summaries, missing required keys, or missing `search_metadata` when the CLI is asked to include it
- drift between host defaults and compose `mcp` service defaults
- regressions where `fresh=True` still behaves like an upstream cached read
- `page_quality` values that indicate an upstream challenge or block page; report those separately from tool/runtime drift

## Response style

Report findings first. Keep the summary brief. If the tool works, say that clearly.

Separate conclusions into:

- package-level behavior with host-side defaults
- compose `mcp` behavior with container-network defaults
- upstream-site issues such as challenge or blocked pages
