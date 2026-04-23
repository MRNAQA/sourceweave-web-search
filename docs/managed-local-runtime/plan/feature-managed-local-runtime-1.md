---
goal: Managed local Docker runtime for sourceweave-search-mcp
version: 1.1
date_created: 2026-04-23
last_updated: 2026-04-23
owner: OpenCode
status: Completed
tags: [feature, runtime, mcp, docker]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

This plan implemented the Managed Local Runtime feature so `sourceweave-search-mcp` can automatically reuse or start the local Docker-backed SourceWeave dependency stack when explicit `SOURCEWEAVE_SEARCH_*` endpoints are absent.

## 1. Requirements & Constraints

- **REQ-001**: Preserve the public MCP tool contract and keep the command surface at `sourceweave-search-mcp`.
- **REQ-002**: Treat explicit `SOURCEWEAVE_SEARCH_*` endpoint configuration as authoritative and bypass managed runtime when present.
- **REQ-003**: Discover an existing SourceWeave-managed stack by Docker Compose project identity for the current state directory before considering new startup.
- **REQ-004**: Reuse a healthy pre-existing compatible stack on canonical host endpoints without teardown ownership.
- **REQ-005**: Start a managed local stack with `docker compose` when no reusable stack exists.
- **REQ-006**: Prefer canonical host ports for a new managed stack but fall back to free local ports when they are occupied.
- **REQ-007**: Coordinate multiple local processes with persisted session state, persisted managed ports, and stale-session cleanup.
- **REQ-008**: Recover a missing managed stack using persisted managed ports when active sessions still exist.
- **REQ-009**: Tear down managed containers only when the last active SourceWeave-managed owner exits.
- **REQ-010**: Preserve named volumes across teardown.
- **REQ-011**: Keep `build_mcp_server()` as a thin constructor and keep orchestration outside `tool.py`.
- **REQ-012**: Ship runtime assets inside the Python package rather than relying on repo-root compose files.
- **CON-001**: Do not change `Tools.Valves` default endpoint constants.
- **CON-002**: Do not change the OpenWebUI artifact behavior.
- **CON-003**: Do not disturb unrelated worktree changes, especially the existing README timeout edits.
- **GUD-001**: Prefer subprocess-based Docker orchestration over the Docker SDK.

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: Add reference docs and packaged runtime assets.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Create `docs/managed-local-runtime/plan/feature-managed-local-runtime-1.md` with atomic implementation phases and validation targets. | ✅ | 2026-04-23 |
| TASK-002 | Create `docs/managed-local-runtime/technical-breakdown.md` describing runtime modes, discovery flow, state machine, and file touchpoints. | ✅ | 2026-04-23 |
| TASK-003 | Create `docs/managed-local-runtime/test-strategy.md` focused on lifecycle, concurrency, recovery, and packaging verification. | ✅ | 2026-04-23 |
| TASK-004 | Add packaged runtime assets under `src/sourceweave_web_search/managed_runtime_assets/`. | ✅ | 2026-04-23 |

### Implementation Phase 2

- GOAL-002: Implement managed runtime orchestration and MCP integration.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | Add `src/sourceweave_web_search/managed_runtime.py` with discovery, probes, dynamic port selection, Docker command construction, session-state handling, and lifecycle cleanup. | ✅ | 2026-04-23 |
| TASK-006 | Update `src/sourceweave_web_search/mcp_server.py` to resolve runtime mode before building tools, while keeping `build_mcp_server()` simple. | ✅ | 2026-04-23 |
| TASK-007 | Update `server.json` and `README.md` for optional endpoint variables and default managed local behavior. | ✅ | 2026-04-23 |

### Implementation Phase 3

- GOAL-003: Add deterministic tests and verification coverage.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-008 | Add unit tests for explicit bypass, external canonical reuse, managed discovery, dynamic-port startup, recovery from persisted managed ports, stale-session cleanup, and last-owner teardown. | ✅ | 2026-04-23 |
| TASK-009 | Extend packaging tests to assert managed-runtime assets ship in the wheel and help output stays side-effect free. | ✅ | 2026-04-23 |
| TASK-010 | Run release-gate-adjacent verification commands and fix any failures. | ✅ | 2026-04-23 |

## 3. Alternatives

- **ALT-001**: Embed SearXNG, Crawl4AI, and Redis replacements directly in Python. Rejected because it is much larger than the approved scope.
- **ALT-002**: Add a separate launcher command. Rejected because the approved user-facing surface is `sourceweave-search-mcp`.
- **ALT-003**: Use only canonical fixed host ports. Rejected because unrelated services may legitimately occupy those ports and the managed runtime must still function.

## 4. Dependencies

- **DEP-001**: Docker CLI with Compose plugin available on the host.
- **DEP-002**: Existing `build_tools()` override path in `src/sourceweave_web_search/config.py`.
- **DEP-003**: Current `Tools.Valves` canonical host endpoint defaults in `src/sourceweave_web_search/tool.py`.

## 5. Files

- **FILE-001**: `src/sourceweave_web_search/managed_runtime.py`
- **FILE-002**: `src/sourceweave_web_search/managed_runtime_assets/compose.yaml`
- **FILE-003**: `src/sourceweave_web_search/managed_runtime_assets/searxng-settings.yml`
- **FILE-004**: `src/sourceweave_web_search/mcp_server.py`
- **FILE-005**: `README.md`
- **FILE-006**: `server.json`
- **FILE-007**: `tests/test_managed_runtime.py`
- **FILE-008**: `tests/test_packaging.py`

## 6. Testing

- **TEST-001**: Explicit env vars bypass managed runtime.
- **TEST-002**: Healthy canonical stack is reused without ownership.
- **TEST-003**: Missing stack triggers `docker compose up` with managed host-port env injection.
- **TEST-004**: Managed stack discovery by Docker project identity reuses an existing stack.
- **TEST-005**: Persisted managed ports enable recovery after a prior owner dies.
- **TEST-006**: Last-owner teardown runs `docker compose down` without volume removal.
- **TEST-007**: Stale sessions are cleaned before join/start decisions.
- **TEST-008**: Managed runtime assets are present in built distributions.
- **TEST-009**: `sourceweave-search-mcp --help` does not create the managed runtime state directory.

## 7. Risks & Assumptions

- **RISK-001**: Docker startup latency may make concurrent local starts race-prone if locking is incorrect.
- **RISK-002**: Packaging non-Python runtime assets could be missed if they are placed outside the package tree.
- **RISK-003**: Multiple state directories can lead to multiple managed stacks, increasing local resource usage if users intentionally isolate them.
- **ASSUMPTION-001**: Linux/macOS-style workstation flows remain the primary target for this feature.
- **ASSUMPTION-002**: Canonical host ports remain a useful preferred default and external-reuse probe target even though they are no longer required for managed startup.

## 8. Related Specifications / Further Reading

- `docs/managed-local-runtime/brd.md`
- `docs/managed-local-runtime/srs.md`
- `docs/managed-local-runtime/technical-breakdown.md`
- `docs/managed-local-runtime/test-strategy.md`
