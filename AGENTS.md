# Agent Notes

## Canonical Files
- If docs conflict, trust `pyproject.toml`, `src/sourceweave_web_search/*.py`, and `.github/workflows/*.yml` over prose. `CONTRIBUTING.md` and some repo-local skill text lag current code.
- `src/sourceweave_web_search/tool.py` is the source of truth. `cli.py` and `mcp_server.py` are thin wrappers around it.
- `artifacts/sourceweave_web_search.py` is generated from `src/sourceweave_web_search/tool.py`. Rebuild with `uv run sourceweave-build-openwebui`; verify drift with `uv run sourceweave-build-openwebui --check`.
- The public MCP contract is exactly `search_web(query, domains?, urls?)`, `read_pages(page_ids, focus?)`, and `read_urls(urls, focus?)`. Some older repo prose still mentions only two tools; trust `src/sourceweave_web_search/mcp_server.py` and `tests/test_packaging.py`.

## Runtime Defaults
- Dev install: `uv sync --locked --group dev`.
- Package and test defaults target host-side services: `http://127.0.0.1:19080/search?format=json&q=<query>`, `http://127.0.0.1:19235`, and `redis://127.0.0.1:16379/2`.
- The compose `mcp` service uses container-network endpoints instead: `http://searxng:8080/search?format=json&q=<query>`, `http://crawl4ai:11235`, and `redis://redis:6379/2`. Do not call those the defaults unless you are inside the compose container.
- `SEARXNG_BASE_URL` is normalized to a template with `format=json` and `q=<query>`; preserve that behavior when touching config handling.
- Usual local dependency stack: `cp .env.example .env && docker compose up -d redis crawl4ai searxng`.
- `docker compose up -d mcp` builds and runs the publishable image and exposes `http://127.0.0.1:18000/mcp`. Running `uv run sourceweave-search-mcp --transport streamable-http` uses port `8000`.

## Verification
- If you touch runtime, packaging, or release surfaces, mirror the `release-gate` order:
1. `uv run python scripts/sync_release_metadata.py --check`
2. `uv run sourceweave-build-openwebui --check`
3. `uv run ruff check src tests`
4. `uv run mypy`
5. `uv run pytest tests/test_config.py tests/test_packaging.py tests/test_tool.py -m "not integration"`
- `tests/test_phase4.py` is a separate manual Crawl4AI HTTP check; it is excluded from `mypy` and the default deterministic CI gate.

## Fast Checks
- Runtime smoke: `uv run sourceweave-search --query "python programming" --read-first-pages 2 --pretty`
- Focused tests: `uv run pytest tests/test_tool.py -m "not integration"`

## Packaging And Release
- `pyproject.toml` is the version source of truth.
- Run `uv run python scripts/sync_release_metadata.py` instead of hand-editing version stamps in `src/sourceweave_web_search/tool.py`, `server.json`, `Dockerfile`, or `docker-compose.yml`.
- Packaging tests expect the wheel to exclude repo-only paths such as `.agents/`, `artifacts/`, `infrastructure/`, `scripts/`, `tests/`, and `docker-compose.yml`.
- `.github/workflows/release.yml` rejects tags that do not equal `v{project.version}`.

## API And CLI Gotchas
- Internally, `Tools.read_pages()` can read `page_ids` or direct URLs, but the public MCP API keeps direct URL reads in `read_urls`. Do not collapse that split unless you intend to change the external contract.
- Trust `src/sourceweave_web_search/cli.py` for current CLI flags. Current CLI supports `--query`, repeatable `--domain` and `--url`, `--read-first-page`, `--read-first-pages`, repeatable `--read-page-id`, repeatable `--read-url`, `--focus`, `--include-metadata`, and endpoint overrides.
- Some repo-local prose and skills still reference the older 2-tool contract or removed CLI flags such as `--depth` and `--related-links-limit`; verify against code and tests before copying examples.
