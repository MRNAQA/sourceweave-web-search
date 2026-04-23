# Managed Local Runtime SRS

## Document Control

- Feature: Managed Local Runtime
- Product: SourceWeave Web Search
- Status: Implemented and verified
- Related BRD: `docs/managed-local-runtime/brd.md`

## 1. Purpose

This Software Requirements Specification defines the technical behavior, interfaces, constraints, validation strategy, and implementation scope for the Managed Local Runtime feature in SourceWeave Web Search.

The feature makes `sourceweave-search-mcp` automatically reuse or manage the local Docker-backed dependency stack when explicit runtime endpoints are not provided.
managed Docker Compose orchestration for SearXNG, Crawl4AI, and Redis or Valkey

## 2. Scope

The implementation scope covers:

- runtime-mode selection in `sourceweave-search-mcp`
- managed-stack discovery by Docker Compose project identity for the current state directory
- compatibility probing of canonical host-side endpoints for external stack reuse
- managed Docker Compose orchestration for SearXNG, Crawl4AI, and Redis or Valkey
- cross-process lifecycle coordination for a shared local stack
- persisted managed-port tracking for restart and recovery
- endpoint override injection into `build_tools()`
- docs and metadata updates required by the changed startup behavior

The implementation does not cover:

- changes to the public MCP API
- managed runtime support for `sourceweave-search`
- in-process replacement of Docker-backed dependencies

## 3. System Context

Current runtime components:

- `src/sourceweave_web_search/tool.py`: source of truth for tool behavior and endpoint defaults
- `src/sourceweave_web_search/config.py`: runtime/env override handling and tool construction
- `src/sourceweave_web_search/mcp_server.py`: MCP entrypoint and transport setup
- `src/sourceweave_web_search/managed_runtime.py`: managed runtime mode selection, discovery, orchestration, and state handling
- external services: SearXNG, Crawl4AI, Redis or Valkey

Managed Local Runtime adds an orchestration layer between CLI argument parsing and `build_tools()`.

## 4. Definitions

- Explicit endpoints: any supplied `SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL`, `SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL`, or `SOURCEWEAVE_SEARCH_CACHE_REDIS_URL` values.
- Canonical host endpoints: `http://127.0.0.1:19080/search?format=json&q=<query>`, `http://127.0.0.1:19235`, and `redis://127.0.0.1:16379/2`.
- Managed stack discovery: lookup of SourceWeave-managed containers by Docker Compose project label derived from the current state directory.
- External reuse probe: a health check against the canonical host endpoints to determine whether an unrelated but compatible local stack can be reused.
- Managed owner: a SourceWeave process that participates in shared ownership tracking for a stack SourceWeave manages.
- Reused stack: a healthy local stack found on the canonical host endpoints that SourceWeave did not start and must not tear down.
- Managed ports: persisted host-port bindings used by the SourceWeave-managed stack for the current state directory.

## 5. High-Level Design

### 5.1 Runtime modes

`sourceweave-search-mcp` shall support three effective startup paths:

1. Explicit endpoint mode
2. Reuse-existing-local-stack mode
3. Managed-stack mode

Mode selection order:

1. Parse CLI args.
2. Check for explicit endpoint env vars.
3. If explicit endpoints are present, bypass managed logic.
4. If explicit endpoints are absent, discover an existing SourceWeave-managed stack for the current state directory.
5. If a discovered managed stack is healthy, join it and use its actual persisted endpoints.
6. If active managed sessions exist but the stack is missing, restart it using the persisted managed ports.
7. If no managed stack is active, probe the canonical host endpoints for a healthy external stack.
8. If the canonical endpoints are healthy, reuse them without ownership.
9. Otherwise start a new managed stack, preferring canonical ports when available and falling back to free ports when they are not.

### 5.2 Integration point

Managed runtime logic shall be invoked from `src/sourceweave_web_search/mcp_server.py` in `main()`.

`build_mcp_server()` shall remain a simple constructor over a provided or default `Tools` instance. This preserves current tests and keeps server registration logic isolated from runtime orchestration.

### 5.3 Module structure

`src/sourceweave_web_search/managed_runtime.py` shall encapsulate:

- mode selection helpers
- canonical external reuse probes
- managed-stack discovery by Docker Compose project label
- Docker command construction and subprocess execution
- runtime asset materialization
- dynamic host-port selection
- cross-process lock and state management
- lifecycle context management

## 6. Detailed Functional Requirements

### 6.1 Explicit endpoint bypass

FR-1. The system shall treat explicit `SOURCEWEAVE_SEARCH_*` endpoint configuration as authoritative.

FR-2. If explicit endpoints are present, the system shall not run discovery, compatibility probes, or Docker Compose orchestration.

FR-3. Existing precedence behavior in `build_tools(runtime_overrides=..., valve_overrides=...)` shall remain unchanged.

### 6.2 Managed-stack discovery and external reuse probe

FR-4. If explicit endpoints are absent, the system shall first attempt to discover a SourceWeave-managed stack for the current state directory by Docker Compose project identity.

FR-5. Discovery shall inspect Docker containers labeled with the Compose project name derived from the resolved state directory path.

FR-6. If a complete discovered managed stack is found, the system shall derive host ports from Docker inspect data and probe those effective endpoints.

FR-7. If the discovered managed stack is healthy, the system shall join it as managed and persist its host-port bindings into local state.

FR-8. If explicit endpoints are absent and no healthy managed stack is found, the system shall probe the canonical host endpoints for a healthy reusable external stack.

FR-9. The probe shall validate:

- SearXNG endpoint returns an expected search response shape.
- Crawl4AI endpoint responds successfully on its health route.
- Redis or Valkey endpoint is reachable and responds to a minimal connectivity check.

FR-10. If all canonical services are healthy, the system shall return canonical endpoint overrides and mark the runtime as reused, not owned.

FR-11. If canonical endpoints are partially occupied, incompatible, or absent, the system shall not fail solely because those ports are unavailable; it shall proceed to managed stack selection unless a stricter managed-state recovery requirement blocks it.

### 6.3 Managed runtime asset materialization

FR-12. The system shall not depend on repo-root `docker-compose.yml` or `infrastructure/` files at runtime.

FR-13. The system shall materialize its own managed runtime assets into a user-local state directory before invoking Docker Compose.

FR-14. The managed runtime assets shall include:

- a Compose specification for SearXNG, Crawl4AI, and Redis or Valkey
- the SearXNG settings file required by that Compose specification

FR-15. The Compose specification shall allow host-port injection through environment variables while keeping canonical host ports as defaults.

FR-16. The managed runtime assets shall preserve named-volume persistence for Redis or Valkey and SearXNG cache data.

FR-17. The managed runtime assets shall omit `restart: unless-stopped` because lifecycle is owned by the SourceWeave supervisor.

### 6.4 Managed startup and health wait

FR-18. The system shall start the managed local stack via `docker compose` subprocess calls.

FR-19. The system shall capture subprocess output instead of forwarding it to MCP stdio transport.

FR-20. For a new managed stack, the system shall prefer canonical host ports when they are available.

FR-21. If a preferred port is unavailable, the system shall allocate a free local port for that service.

FR-22. When restarting a previously managed stack, the system shall reuse the persisted managed ports when possible.

FR-23. The system shall wait until all required services are healthy on the effective endpoints before starting the MCP server.

FR-24. The system shall fail with a bounded timeout and a clear error if the stack does not become healthy.

### 6.5 Session coordination and teardown

FR-25. The system shall support multiple local processes sharing one managed stack per local state directory.

FR-26. The system shall use a cross-process lock during session-state mutation.

FR-27. The system shall persist runtime session state in a local file under the managed runtime state directory.

FR-28. The session state shall record at minimum:

- active session identifiers
- owning process identifiers
- stack ownership state
- managed host ports
- timestamps sufficient for stale-session cleanup

FR-29. On startup, the system shall remove stale sessions whose owning processes are no longer alive.

FR-30. If active sessions exist but the managed stack is missing, the system shall recover by restarting the stack with the persisted managed ports.

FR-31. If active sessions exist but the managed-port data is missing or invalid, the system shall fail with a clear state-recovery error.

FR-32. On shutdown, the system shall remove the current process from session state.

FR-33. The system shall call `docker compose down` only when the current process is the last active owner of a stack that SourceWeave started.

FR-34. Shutdown shall not remove named volumes.

FR-35. The system shall never tear down a reused external stack discovered by the canonical external-reuse probe.

### 6.6 Tool construction and MCP startup

FR-36. After selecting runtime mode, the system shall build tools using `build_tools(valve_overrides=...)` with the effective endpoints.

FR-37. In managed mode, the effective endpoints shall match the actual managed host ports.

FR-38. In reused mode, the effective endpoints shall be the canonical host endpoints.

FR-39. The system shall then start the existing FastMCP server with the chosen transport, host, and port.

### 6.7 Help and no-op flows

FR-40. CLI help output for `sourceweave-search-mcp --help` shall not trigger discovery, probes, or Docker startup.

FR-41. Argument validation failures shall occur before managed runtime orchestration where feasible.

## 7. Non-Functional Requirements

NFR-1. The implementation shall preserve the public MCP tool names and input schemas.

NFR-2. The implementation shall preserve `Tools.Valves` default endpoint constants.

NFR-3. Managed runtime behavior shall be deterministic under concurrent local starts against the same state directory.

NFR-4. The implementation shall avoid modifying unrelated runtime surfaces such as the standalone CLI or OpenWebUI artifact generation.

NFR-5. The implementation shall use minimal new dependencies; Docker control should rely on subprocesses rather than the Docker Python SDK.

NFR-6. Diagnostic errors shall be concise and actionable.

## 8. Data And State Requirements

### 8.1 Managed runtime state directory

The implementation shall define a dedicated local state directory for managed runtime artifacts and coordination state.

Expected contents:

- materialized `compose.yaml`
- materialized `searxng-settings.yml`
- lock file or lock primitive target
- session-state JSON file

### 8.2 Session-state schema

Minimum fields:

- `version`
- `stack_started_by_sourceweave`
- `managed_ports`
- `sessions`
- `updated_at`

Each session record should contain:

- `session_id`
- `pid`
- `started_at`
- `last_seen_at`

The implementation may include additional internal fields if needed.

## 9. Interfaces

### 9.1 Existing environment variables

These remain supported and authoritative:

- `SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL`
- `SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL`
- `SOURCEWEAVE_SEARCH_CACHE_REDIS_URL`
- `FASTMCP_HOST`
- `FASTMCP_PORT`

### 9.2 Managed runtime port injection

The managed Compose file shall accept host-port overrides through:

- `SOURCEWEAVE_MANAGED_RUNTIME_SEARXNG_HOST_PORT`
- `SOURCEWEAVE_MANAGED_RUNTIME_CRAWL4AI_HOST_PORT`
- `SOURCEWEAVE_MANAGED_RUNTIME_REDIS_HOST_PORT`

These are internal orchestration variables, not part of the public MCP user interface.

### 9.3 Docker interface

The implementation shall use Docker subprocesses against the materialized Compose file and against Docker inspect/ps APIs via CLI.

Expected operations:

- `docker compose version`
- `docker compose up -d`
- `docker compose down`
- `docker ps -aq --filter label=com.docker.compose.project=<project>`
- `docker inspect <container ids...>`

The exact subprocess command sequence may vary as long as behavior matches this specification.

## 10. Error Handling Requirements

The implementation shall distinguish at least these failure classes:

- Docker executable missing
- Docker Compose support unavailable
- managed stack startup failure
- managed stack health timeout
- session-state corruption or missing managed-port recovery data
- dynamic port allocation failure

Each error shall produce a user-readable message explaining:

- what failed
- whether explicit endpoints can bypass the problem
- what corrective action is likely needed

## 11. Logging And Observability

The implementation may log runtime decisions internally, but shall avoid polluting stdio transport output used by MCP clients.

Useful decision points to log include:

- explicit endpoint bypass
- discovered managed stack reuse or restart
- canonical external stack reuse
- managed stack startup
- managed stack teardown
- stale-session cleanup
- dynamic port allocation decisions

## 12. Security And Safety Requirements

SR-1. The feature shall not execute arbitrary shell fragments; Docker subprocesses must use explicit argument vectors.

SR-2. The feature shall not delete named volumes during routine teardown.

SR-3. The feature shall not override explicit user-specified endpoints.

SR-4. The feature shall not tear down services it did not start.

## 13. Test Requirements

### 13.1 Deterministic unit tests

The implementation shall add deterministic tests for:

- explicit endpoint detection bypasses managed runtime
- asset materialization produces the managed Compose and settings files
- compose project naming is state-directory specific
- healthy canonical external stack reuse returns reused mode without ownership
- managed startup allocates dynamic ports when canonical defaults are unavailable
- discovered managed stack reuse by Docker project identity
- recovery startup using persisted managed ports after prior owner death
- stale-session cleanup
- last-owner teardown behavior
- Docker inspect parsing for managed-stack discovery
- `sourceweave-search-mcp --help` remains side-effect free

### 13.2 Packaging tests

Packaging tests shall continue to assert:

- default host-side endpoint values in `Tools.Valves`
- built wheels exclude repo-only paths such as `infrastructure/` and `docker-compose.yml`

Additional packaging tests should assert that any packaged managed-runtime assets are available if the implementation chooses to ship them inside the package.

### 13.3 Verification gate

If runtime, packaging, or release surfaces are changed, verification shall follow:

1. `uv run python scripts/sync_release_metadata.py --check`
2. `uv run sourceweave-build-openwebui --check`
3. `uv run ruff check src tests`
4. `uv run pyright src tests`
5. `uv run pytest tests/test_config.py tests/test_packaging.py tests/test_tool.py tests/test_managed_runtime.py -m "not integration"`

## 14. Documentation Requirements

The implementation shall update:

- `README.md` to describe the new default local startup path
- MCP client configuration examples for OpenCode and VS Code
- `server.json` so endpoint variables are no longer marked required for the package story

Documentation shall clearly state:

- Docker is still required for the managed local runtime path
- explicit endpoints remain supported and override auto-management
- canonical endpoints are preferred defaults and external-reuse probes, not a mandatory managed binding
- managed stacks are discovered by per-state-dir Docker project identity and may use alternate host ports

## 15. Backward Compatibility

The following must remain backward compatible:

- public MCP tool names and schemas
- explicit endpoint deployments
- default host endpoint constants in `Tools.Valves`
- repo-local compose deployment with the published image

## 16. Implementation Notes

- Keep the change surgical and centered on the MCP entrypoint.
- Avoid moving orchestration into `tool.py`.
- Prefer generated runtime assets or packaged in-module assets over repo-root runtime file dependencies.
- Preserve current config-merging behavior by injecting effective endpoints through `build_tools()`.

## 17. Acceptance Tests

AT-1. Package-based stdio launch with no endpoint env vars and healthy Docker support starts successfully.

AT-2. Package-based stdio launch with no endpoint env vars and an already-running compatible stack on canonical endpoints reuses that stack without teardown ownership.

AT-3. Package-based launch with explicit endpoint env vars bypasses managed runtime.

AT-4. Two local processes can share a managed stack discovered by state-directory project identity without premature teardown.

AT-5. If a prior managed owner dies, a later process can recover the stack using persisted managed ports.

AT-6. Cache volumes persist after all managed processes exit and the stack is later restarted.

AT-7. Help output remains side-effect free.

## 18. Deferred Items

- Extending the same behavior to `sourceweave-search`
- Adding explicit status or cleanup subcommands
- Supporting non-Docker local orchestration modes
