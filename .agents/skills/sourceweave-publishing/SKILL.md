---
name: sourceweave-publishing
description: Use this whenever the user wants to publish, release, ship, cut a version, push a package, publish a container, publish to PyPI, publish to GHCR, publish to Docker Hub, publish to the MCP Registry, prepare release notes, bump the SourceWeave version, or verify that SourceWeave is release-ready. Use it even if the user only mentions one destination such as PyPI or GHCR, because the right workflow depends on coordinated checks across pyproject versioning, server.json, Docker metadata, GitHub releases, and registry publishing.
---

# SourceWeave Publishing

This skill is for publishing `sourceweave-web-search` autonomously and safely.

The repo already has a concrete release shape. Do not invent a new one unless the user explicitly asks. The core job is to make release metadata consistent, verify the release surface, publish the package/container artifacts, and only then publish MCP Registry metadata.

Apply KISS/YAGNI/DRY/SOLID when reviewing or editing release flow:

- do not add new knobs or alternate publish paths unless the repo already uses them
- do not duplicate release logic across docs, scripts, and skill text when one source of truth already exists
- prefer small repairs to the existing workflow over new helper scripts
- stop and ask only when a real release blocker exists, not for routine operational steps

## Goal

Publish SourceWeave with least surprising flow:

1. bump the version in `pyproject.toml`
2. update `CHANGELOG.md` so released changes move out of `Unreleased`
3. review public docs and refresh `README.md` if release changes user-facing behavior
4. sync derived release metadata
5. verify package, artifact, tests, and container surface
6. commit and push release-ready state to target branch
7. run GitHub release workflow
8. confirm PyPI and container artifacts are live
9. publish `server.json` to MCP Registry

## Source Of Truth

Treat `pyproject.toml` as the release version source of truth.

Use this as the canonical release-metadata sync command:

```bash
uv run python scripts/sync_release_metadata.py
```

The packaged script entry point is an equivalent alternative when that is more convenient:

```bash
uv run sourceweave-sync-release-metadata
```

The sync/check path covers:

- `src/sourceweave_web_search/tool.py` header version
- `server.json` version and package versions
- Dockerfile OCI version label
- `docker-compose.yml` publishable image tag

Do not hand-edit those version fields unless the user explicitly asks for a one-off repair.

The sync script does not cover every literal version occurrence in repo.

It does not update:

- `artifacts/sourceweave_web_search.py`
- `CHANGELOG.md`
- skill docs, eval fixtures, or other prose that may mention old release numbers

For README work, do not hand-wave. If public behavior, install/run steps, or release surfaces changed enough that README may now be stale, explicitly load global `create-readme` skill and use it to review/update `README.md` before release verification.

Recent architectural context that matters for release validation:

- Redis or Valkey is now the canonical persisted page store
- SourceWeave stores richer page representations in cache instead of relying on an in-process page store
- direct URL reads should still succeed even if same-call persistence is unavailable
- public MCP contract is now lean three-tool split:
  - `search_web(query, domains?, urls?)`
  - `read_pages(page_ids, focus?)`
  - `read_urls(urls, focus?)`
- `read_pages` is follow-up read by `page_id`; direct URL reading belongs to `read_urls`
- Crawl4AI `CacheMode` is intentionally used for search freshness and direct-read semantics, so release testing should include at least one normal search path and one direct URL read path when behavior changed

The generated OpenWebUI artifact is a separate derived surface. Keep it aligned with:

```bash
uv run sourceweave-build-openwebui
uv run sourceweave-build-openwebui --check
```

For release work, treat the combined version-alignment flow as:

```bash
uv run python scripts/sync_release_metadata.py
uv run sourceweave-build-openwebui
```

And the combined verification flow as:

```bash
uv run python scripts/sync_release_metadata.py --check
uv run sourceweave-build-openwebui --check
```

For autonomous releases, use this full local prep flow unless user explicitly narrows scope:

```bash
uv run python scripts/sync_release_metadata.py
uv run sourceweave-build-openwebui
uv run python scripts/sync_release_metadata.py --check
uv run sourceweave-build-openwebui --check
uv run pytest tests/test_tool.py tests/test_config.py tests/test_packaging.py -q -p no:cacheprovider
uv run ruff check src tests
uv run mypy
uv build --no-sources --sdist --wheel --out-dir dist/release-check --clear --no-create-gitignore
docker build -t sourceweave-web-search-mcp:release-check .
```

## Canonical Release Surfaces

Release workflow:

- `.github/workflows/release.yml`

Registry workflow:

- `.github/workflows/publish-mcp-registry.yml`

Release gate:

- `.github/workflows/release-gate.yml`

Key metadata files:

- `pyproject.toml`
- `CHANGELOG.md`
- `server.json`
- `Dockerfile`
- `README.md`

## What To Verify Before Publishing

Run these checks unless the user explicitly tells you to skip them:

```bash
uv run python scripts/sync_release_metadata.py --check
uv run sourceweave-build-openwebui --check
uv run pytest tests/test_tool.py tests/test_config.py tests/test_packaging.py -q -p no:cacheprovider
uv run ruff check src tests
uv run mypy
uv build --no-sources --sdist --wheel --out-dir dist/release-check --clear --no-create-gitignore
```

If the user asks whether the sync script covers "all occurrences," answer precisely: no. It covers the intended release metadata surfaces, while the OpenWebUI artifact has its own build/check path and changelog text is still maintained separately.

If the container story changed, also verify the local MCP image path:

```bash
docker compose build mcp
docker compose up -d --force-recreate mcp
```

When the user cares about the running container identity, inspect labels with:

```bash
docker inspect sourceweave-web-search-mcp-1 --format '{{.Config.Image}} {{json .Config.Labels}}'
```

## Publish Order

Use this order unless the user explicitly changes it:

1. local version/changelog/README/update pass
2. local verification pass
3. commit and push target branch
4. GitHub release workflow
5. confirm PyPI package is live
6. confirm GHCR image is live
7. run MCP Registry publish workflow

This order matters because MCP Registry metadata should point at already-published artifacts.

Do not skip commit/push in autonomous release mode. `workflow_dispatch` releases remote Git state, not local uncommitted edits.

## GitHub Release Workflow

Use `.github/workflows/release.yml`.

Important inputs:

- `tag`: must match `v{project.version}`
- `target_ref`: usually `main`
- `prerelease`: `false` for normal releases
- `publish_pypi`: `true` when publishing the package
- `publish_ghcr`: `true` for the primary container publish path
- `publish_dockerhub`: only if the user wants the extra mirror
- `changelog`: markdown release notes body

Recommended default invocation:

```bash
VERSION=$(python - <<'PY'
import tomllib
from pathlib import Path

data = tomllib.loads(Path("pyproject.toml").read_text())
print(data["project"]["version"])
PY
)

gh workflow run release.yml \
  --ref main \
  -f tag="v${VERSION}" \
  -f target_ref=main \
  -f prerelease=false \
  -f publish_pypi=true \
  -f publish_ghcr=true \
  -f publish_dockerhub=false \
  -f changelog="$(cat CHANGELOG.md)"
```

After dispatch, wait for result and inspect logs:

```bash
gh run watch --exit-status
gh run list --workflow release.yml --limit 1
```

## Registry Targets

### PyPI

Package name:

- `sourceweave-web-search`

Use trusted publishing via the existing workflow. If PyPI trusted publishing fails, suspect a publisher-configuration mismatch first, not a build problem.

Expected trusted-publisher identity:

- owner: `MRNAQA`
- repo: `sourceweave-web-search`
- workflow: `release.yml`

### GHCR

Primary image:

- `ghcr.io/mrnaqa/sourceweave-web-search-mcp`

Prefer GHCR as the default public container registry. It is the cheapest and simplest fit for this repo's GitHub-centered workflow.

### Docker Hub

Optional mirror only.

Default repository if enabled:

- `mrnaqa/sourceweave-web-search-mcp`

Requires:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

### MCP Registry

Registry server name:

- `io.github.MRNAQA/sourceweave-web-search`

Publish only after PyPI and GHCR artifacts are live.

Workflow:

- `.github/workflows/publish-mcp-registry.yml`

Typical invocation:

```bash
gh workflow run publish-mcp-registry.yml --ref main -f target_ref=main
```

After dispatch, wait for result and inspect logs:

```bash
gh run watch --exit-status
gh run list --workflow publish-mcp-registry.yml --limit 1
```

## What To Check After Publishing

Confirm the release itself:

```bash
VERSION=$(python - <<'PY'
import tomllib
from pathlib import Path

data = tomllib.loads(Path("pyproject.toml").read_text())
print(data["project"]["version"])
PY
)

gh release view "v${VERSION}" --json url,tagName,name,isPrerelease,assets
```

Confirm the PyPI package is installable:

```bash
VERSION=$(python - <<'PY'
import tomllib
from pathlib import Path

data = tomllib.loads(Path("pyproject.toml").read_text())
print(data["project"]["version"])
PY
)

uvx --from "sourceweave-web-search==${VERSION}" sourceweave-search --help
```

Confirm GHCR image exists:

```bash
VERSION=$(python - <<'PY'
import tomllib
from pathlib import Path

data = tomllib.loads(Path("pyproject.toml").read_text())
print(data["project"]["version"])
PY
)

docker manifest inspect "ghcr.io/mrnaqa/sourceweave-web-search-mcp:${VERSION}"
```

If the user wants extra confidence, also confirm `latest` is updated when that tag was published.

## Changelog Expectations

Prefer `CHANGELOG.md` as the source for the release notes body unless the user gives a custom changelog.

Good release notes for this repo should answer:

- what changed for MCP users
- what changed for deployment or publishing
- whether registry metadata, package names, or image names changed
- whether there are any migration or operator notes

## When To Stop And Ask

Pause and ask the user before proceeding if:

- the worktree is dirty in ways unrelated to the release
- the version is not bumped yet and the user has not confirmed the target version
- the release workflow inputs imply publishing to Docker Hub but secrets are missing
- PyPI trusted publishing fails with an identity mismatch
- the user asks to publish MCP Registry metadata before PyPI or GHCR artifacts are live

If untracked temp eval files or scratch outputs exist, do not include them in release commit unless user explicitly wants them shipped.

## Response Style

When doing a publish task, report in this order:

1. readiness status
2. exact action taken or next action required
3. verification results
4. any blocker with the minimal fix

Keep it operational and specific. The user should be able to tell at a glance whether the release is ready, running, blocked, or complete.

## Examples

**Example 1:**
Input: `bump the version, update the changelog, and publish to pypi and ghcr`
Output: bump `pyproject.toml`, run metadata sync, update `CHANGELOG.md`, run verification, trigger `release.yml` with `publish_pypi=true` and `publish_ghcr=true`, verify PyPI install and GHCR manifest, then recommend running MCP Registry publish.

**Example 2:**
Input: `the pypi release failed with invalid-publisher, figure it out`
Output: inspect the workflow identity from the error, compare it against the expected trusted-publisher config for `MRNAQA/sourceweave-web-search` and `release.yml`, explain the mismatch, and tell the user exactly what to fix in PyPI.

**Example 3:**
Input: `publish this release everywhere and tell me the next command`
Output: verify release readiness, run or instruct `release.yml`, confirm PyPI and GHCR artifacts, then give the exact `gh workflow run publish-mcp-registry.yml --ref main -f target_ref=main` command.
