---
name: sourceweave-publishing
description: Use this whenever the user wants to publish, release, ship, cut a version, push a package, publish a container, publish to PyPI, publish to GHCR, publish to Docker Hub, publish to the MCP Registry, prepare release notes, bump the SourceWeave version, or verify that SourceWeave is release-ready. Use it even if the user only mentions one destination such as PyPI or GHCR, because the right workflow depends on coordinated checks across pyproject versioning, server.json, Docker metadata, GitHub releases, and registry publishing.
---

# SourceWeave Publishing

This skill is for publishing `sourceweave-web-search` autonomously and safely.

The repo already has a concrete release shape. Do not invent a new one unless the user explicitly asks. The core job is to make release metadata consistent, verify the release surface, publish the package/container artifacts, and only then publish MCP Registry metadata.

## Goal

Publish SourceWeave with the least surprising flow:

1. bump the version in `pyproject.toml`
2. sync derived release metadata
3. verify the package, artifact, tests, and container surface
4. run the GitHub release workflow
5. confirm PyPI and container artifacts are live
6. publish `server.json` to the MCP Registry

## Source Of Truth

Treat `pyproject.toml` as the release version source of truth.

Derived files are synchronized by:

```bash
uv run python scripts/sync_release_metadata.py
```

The sync/check path covers:

- `src/sourceweave_web_search/tool.py` header version
- `server.json` version and package versions
- OCI package tag in `server.json`
- Dockerfile OCI version label

Do not hand-edit those version fields unless the user explicitly asks for a one-off repair.

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

1. GitHub release workflow
2. confirm PyPI package is live
3. confirm GHCR image is live
4. run MCP Registry publish workflow

This order matters because MCP Registry metadata should point at already-published artifacts.

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

- `io.github.mrnaqa/sourceweave-web-search`

Publish only after PyPI and GHCR artifacts are live.

Workflow:

- `.github/workflows/publish-mcp-registry.yml`

Typical invocation:

```bash
gh workflow run publish-mcp-registry.yml --ref main -f target_ref=main
```

## What To Check After Publishing

Confirm the release itself:

```bash
gh release view vX.Y.Z --json url,tagName,name,isPrerelease,assets
```

Confirm the PyPI package is installable:

```bash
uvx --from sourceweave-web-search==X.Y.Z sourceweave-search --help
```

Confirm GHCR image exists:

```bash
docker manifest inspect ghcr.io/mrnaqa/sourceweave-web-search-mcp:X.Y.Z
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

## Response Style

When doing a publish task, report in this order:

1. readiness status
2. exact action taken or next action required
3. verification results
4. any blocker with the minimal fix

Keep it operational and specific. The user should be able to tell at a glance whether the release is ready, running, blocked, or complete.

## Examples

**Example 1:**
Input: `bump to 0.2.2, update the changelog, and publish to pypi and ghcr`
Output: bump `pyproject.toml`, run metadata sync, update `CHANGELOG.md`, run verification, trigger `release.yml` with `publish_pypi=true` and `publish_ghcr=true`, verify PyPI install and GHCR manifest, then recommend running MCP Registry publish.

**Example 2:**
Input: `the pypi release failed with invalid-publisher, figure it out`
Output: inspect the workflow identity from the error, compare it against the expected trusted-publisher config for `MRNAQA/sourceweave-web-search` and `release.yml`, explain the mismatch, and tell the user exactly what to fix in PyPI.

**Example 3:**
Input: `publish this release everywhere and tell me the next command`
Output: verify release readiness, run or instruct `release.yml`, confirm PyPI and GHCR artifacts, then give the exact `gh workflow run publish-mcp-registry.yml --ref main -f target_ref=main` command.
