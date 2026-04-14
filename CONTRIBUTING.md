# Contributing

## Development Setup

```bash
git clone https://github.com/MRNAQA/sourceweave-web-search.git
cd sourceweave-web-search
uv sync --locked --group dev
```

Recommended repo-local runtime stack:

```bash
cp .env.example .env
docker compose up -d redis crawl4ai searxng
```

Then run the MCP server locally against those endpoints:

```bash
SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL="http://127.0.0.1:19080/search?format=json&q=<query>" \
SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL="http://127.0.0.1:19235" \
SOURCEWEAVE_SEARCH_CACHE_REDIS_URL="redis://127.0.0.1:16379/2" \
uv run sourceweave-search-mcp
```

Optional fully containerized repo-local stack:

```bash
docker compose up -d mcp
```

That starts the repo-local MCP service plus SearXNG, Crawl4AI, and Redis.

## Repository Layout

Shared package source:

- `src/sourceweave_web_search/tool.py`: canonical implementation used by the package, CLI, and MCP server
- `src/sourceweave_web_search/cli.py`: CLI entry point
- `src/sourceweave_web_search/mcp_server.py`: MCP server entry point
- `src/sourceweave_web_search/build_openwebui.py`: build/check logic for the OpenWebUI artifact

Repo-local helpers:

- `artifacts/sourceweave_web_search.py`: generated standalone OpenWebUI artifact
- `docker-compose.yml`: local compose entrypoint
- `infrastructure/*.yml`: service fragments for Redis, Crawl4AI, and SearXNG
- `infrastructure/searxng-settings.yml`: tracked local SearXNG tuning
- `scripts/run_tool_call.py`: local tool-call wrapper
- `scripts/build_openwebui_tool.py`: local artifact rebuild/check wrapper
- `tests/`: runtime, packaging, and integration checks

## Working On The Tool

- edit `src/sourceweave_web_search/tool.py` as the source of truth
- regenerate or check the standalone OpenWebUI artifact with `sourceweave-build-openwebui`
- keep public tool names and docs aligned with `search_web` and `read_pages`
- treat Redis or Valkey as the canonical persisted page store; direct same-call reads should still work when persistence is temporarily unavailable

## Verification

Recommended local checks:

```bash
uv run python scripts/sync_release_metadata.py --check
uv run sourceweave-build-openwebui --check
uv run pytest tests/test_tool.py tests/test_config.py tests/test_packaging.py -q -p no:cacheprovider
uv run ruff check src tests
uv run mypy
```

Additional repo-local runtime check:

```bash
docker compose run --rm mcp uv run python tests/test_phase4.py
```

## Packaging Notes

The published wheel is intentionally lean. It does not include repo-only helpers such as:

- `docker-compose.yml`
- `infrastructure/`
- `scripts/`
- `tests/`
- `.agents/skills/`
- the checked-in OpenWebUI artifact under `artifacts/`

`tests/test_packaging.py` verifies that:

- the checked-in OpenWebUI artifact is validated in strict `--check` mode without being rewritten
- the package CLI can be invoked
- the MCP server exposes `search_web` and `read_pages`
- publishable wheels and sdists include README metadata and the LICENSE file while keeping repo-only files out of the wheel

## Releasing

This repo includes a manual GitHub Actions workflow at `.github/workflows/release.yml`.

Inputs:

- `tag`: release tag, for example `vX.Y.Z`
- `release_name`: optional GitHub release title
- `target_ref`: branch or commit to release, usually `main`
- `prerelease`: whether to mark the release as a prerelease
- `publish_pypi`: publish the built wheel and sdist to PyPI using trusted publishing
- `publish_ghcr`: publish the container image to GitHub Container Registry
- `publish_dockerhub`: publish the container image to Docker Hub
- `changelog`: markdown release notes for the GitHub release body

The workflow:

- verifies the tag matches `pyproject.toml` version
- reruns artifact, lint, type, and deterministic test checks
- smoke builds the Docker image
- builds the wheel and sdist
- optionally publishes the package to PyPI
- optionally publishes the container image to GHCR and Docker Hub
- generates `SHA256SUMS.txt`
- creates a GitHub release with the built distributions and `artifacts/sourceweave_web_search.py` attached

External setup required before enabling publishing:

- PyPI: configure trusted publishing for the `sourceweave-web-search` project to allow this GitHub workflow to publish
- Docker Hub: add `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` secrets, and optionally set `DOCKERHUB_REPOSITORY` as a repository variable if you do not want the default `mrnaqa/sourceweave-web-search`

Example trigger from the CLI:

```bash
gh workflow run release.yml \
  --ref main \
  -f tag=vX.Y.Z \
  -f target_ref=main \
  -f prerelease=false \
  -f publish_pypi=true \
  -f publish_ghcr=true \
  -f publish_dockerhub=false \
  -f changelog="$(cat CHANGELOG.md)"
```
