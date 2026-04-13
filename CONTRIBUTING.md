# Contributing

## Development Setup

```bash
git clone https://github.com/MRNAQA/sourceweave-web-search.git
cd sourceweave-web-search
uv sync --locked --group dev
```

Optional repo-local runtime stack:

```bash
cp .env.example .env
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

## Verification

Recommended local checks:

```bash
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
- `skills/`
- the checked-in OpenWebUI artifact under `artifacts/`

`tests/test_packaging.py` verifies that:

- the checked-in OpenWebUI artifact is validated in strict `--check` mode without being rewritten
- the package CLI can be invoked
- the MCP server exposes `search_web` and `read_pages`
- publishable wheels and sdists include README metadata and the LICENSE file while keeping repo-only files out of the wheel

## Releasing

This repo includes a manual GitHub Actions workflow at `.github/workflows/release.yml`.

Inputs:

- `tag`: release tag, for example `v0.2.0`
- `release_name`: optional GitHub release title
- `target_ref`: branch or commit to release, usually `main`
- `prerelease`: whether to mark the release as a prerelease
- `changelog`: markdown release notes for the GitHub release body

The workflow:

- verifies the tag matches `pyproject.toml` version
- reruns artifact, lint, type, and deterministic test checks
- builds the wheel and sdist
- generates `SHA256SUMS.txt`
- creates a GitHub release with the built distributions and `artifacts/sourceweave_web_search.py` attached

Example trigger from the CLI:

```bash
gh workflow run release.yml \
  --ref main \
  -f tag=v0.2.0 \
  -f target_ref=main \
  -f prerelease=false \
  -f changelog="$(cat CHANGELOG.md)"
```
