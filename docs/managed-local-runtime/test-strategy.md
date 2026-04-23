# Managed Local Runtime Test Strategy

## Goal

Validate that Managed Local Runtime meets the approved requirements for mode selection, Docker orchestration, shared ownership, stale-owner recovery, packaging, and user-facing documentation updates without regressing the MCP contract.

## Quality Priorities

Highest-risk areas:

1. Multi-process lifecycle coordination
2. Safe reuse versus ownership mistakes
3. Recovery after the original managed owner dies
4. Dynamic host-port allocation and persistence
5. Packaged runtime asset availability
6. Side-effect-free help behavior

## Test Scope

In scope:

- explicit endpoint bypass
- managed-stack discovery by Docker project identity
- canonical external stack reuse
- managed startup path with dynamic ports
- join and rejoin path after prior SourceWeave ownership
- stale-session cleanup
- last-owner-only teardown
- asset materialization
- package wheel contents
- README and metadata behavior reflected in packaging tests where practical

Out of scope for deterministic tests:

- live Docker startup in CI
- live SearXNG/Crawl4AI/Redis integration from unit tests
- OpenWebUI artifact behavior changes

## Test Layers

### Unit tests

Target file: `tests/test_managed_runtime.py`

Required cases:

- explicit env vars return explicit mode and skip managed startup
- canonical healthy probe returns reused mode without ownership
- missing canonical stack triggers managed `docker compose up`
- managed stack starts on dynamic ports when canonical defaults are unavailable
- discovered managed stack is reused by state-directory project identity
- persisted managed ports are reused for recovery startup after prior owner death
- asset materialization writes packaged compose/settings files
- compose command builder emits a state-directory-specific project name and expected argument vector
- stale sessions are removed before join/start decisions
- last-owner teardown runs `docker compose down`
- non-last-owner exit does not run teardown
- Docker inspect parsing reconstructs host ports correctly

### Packaging tests

Target file: `tests/test_packaging.py`

Required cases:

- wheel still excludes repo-only runtime files like `infrastructure/` and `docker-compose.yml`
- wheel includes `sourceweave_web_search/managed_runtime_assets/compose.yaml`
- wheel includes `sourceweave_web_search/managed_runtime_assets/searxng-settings.yml`
- `python -m sourceweave_web_search.mcp_server --help` remains successful and side-effect free

## Risk-Based Matrix

| Risk | Failure | Test Response |
|------|---------|---------------|
| Shared-stack coordination bug | One process tears down while another still runs | Unit test session join/leave and last-owner-only teardown |
| Stale owner after crash | New process cannot recover stack | Unit test persisted managed-port recovery |
| Fixed-port collision | Managed startup fails when canonical defaults are busy | Unit test dynamic-port fallback path |
| Asset packaging miss | Managed mode works in repo but fails from wheel | Packaging test asserts asset files in wheel |
| Discovery bug | Existing SourceWeave-managed stack is not adopted | Unit test Docker inspect-based discovery |
| Help side effects | MCP clients trigger Docker startup during capability checks | Subprocess help smoke with temporary HOME |

## Verification Commands

Primary verification sequence:

1. `uv run python scripts/sync_release_metadata.py --check`
2. `uv run sourceweave-build-openwebui --check`
3. `uv run ruff check src tests`
4. `uv run pyright src tests`
5. `uv run pytest tests/test_config.py tests/test_packaging.py tests/test_tool.py tests/test_managed_runtime.py -m "not integration"`

## Exit Criteria

- All deterministic managed-runtime tests pass.
- Existing config and packaging tests still pass.
- MCP tool contract remains unchanged.
- Help output remains side-effect free.
- Packaged assets are present in the built wheel.
- Dynamic-port allocation and recovery paths are covered by deterministic tests.
