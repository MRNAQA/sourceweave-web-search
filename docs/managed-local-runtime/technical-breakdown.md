# Managed Local Runtime Technical Breakdown

## Purpose

This document translates the BRD and SRS into the concrete runtime flow, state transitions, file layout, and implementation boundaries for Managed Local Runtime.

## Runtime Modes

`sourceweave-search-mcp` has three effective modes:managed Docker Compose orchestration for SearXNG, Crawl4AI, and Redis or Valkey


1. Explicit endpoint mode
2. Reused external stack mode
3. Managed stack mode

Mode selection order:

1. Parse CLI args.
2. If any `SOURCEWEAVE_SEARCH_*` endpoint env var is present, run in explicit mode.
3. Otherwise discover an existing SourceWeave-managed stack for the current state directory by Docker Compose project identity.
4. If a discovered managed stack is healthy, join it.
5. If active managed sessions exist but the stack is missing, restart it from persisted managed ports.
6. If no managed stack is active, probe the canonical host endpoints for a healthy external stack.
7. If all canonical external services are healthy, reuse them without ownership.
8. Otherwise start a new managed stack, preferring canonical ports when available and allocating free ports when they are not.

## State Machine

### Explicit mode

- Trigger: any explicit endpoint env var is present.
- Effective endpoints: caller-provided endpoints.
- Ownership: none.
- Teardown: none.

### Reused mode

- Trigger: no managed stack is active for the state directory and canonical external endpoints probe healthy.
- Effective endpoints: canonical host endpoints.
- Ownership: none.
- Teardown: none.

### Managed mode

- Trigger: a managed stack is discovered for the state directory, or a managed stack is started or restarted by SourceWeave.
- Effective endpoints: managed host endpoints reconstructed from Docker discovery or persisted state.
- Ownership: shared across active SourceWeave sessions for the same state directory.
- Teardown: only last active SourceWeave-managed session runs `docker compose down`.

## Cross-Process Coordination

State directory target:

- `~/.sourceweave-local/managed-runtime/`

Files in the state directory:

- `compose.yaml`
- `searxng-settings.yml`
- `managed-runtime.lock`
- `managed-runtime-state.json`

State file fields:

- `version`
- `stack_started_by_sourceweave`
- `managed_ports`
- `sessions`
- `updated_at`

Session fields:

- `session_id`
- `pid`
- `started_at`
- `last_seen_at`

Coordination rules:

- Every state mutation happens under the lock.
- Stale sessions are removed before join, start, restart, and teardown decisions.
- A process joins the managed session set only after the stack is confirmed healthy.
- Managed ports are persisted so later processes can restart the stack after crashes.
- Last-owner teardown preserves named volumes.

## Discovery Rules

### Managed stack discovery

- Discovery target: Docker containers labeled with `com.docker.compose.project=<derived project name>`.
- Project name derivation: SHA1-based suffix from the resolved state-directory path.
- Source of truth for managed ports: Docker inspect data first, persisted `managed_ports` second.
- Healthy discovered stack: all three managed services found and effective endpoint probes succeed.

### Canonical external reuse probe

- Probe target: `Tools.Valves()` default host endpoints.
- Purpose: detect an already-running compatible local stack SourceWeave did not start.
- Healthy: all three services respond as expected.
- Missing or incompatible: not fatal by itself; SourceWeave may still start its own managed stack on other ports.

## Probe Rules

### SearXNG

- Probe target: effective search URL with a harmless encoded probe query.
- Healthy: HTTP 200 with JSON containing a `results` list.
- Missing: connection refused or timeout.
- Incompatible: non-200 response or unexpected body shape.

### Crawl4AI

- Probe target: `GET /health` on the effective host port.
- Healthy: HTTP 200.
- Missing: connection refused or timeout.
- Incompatible: non-200 response.

### Redis or Valkey

- Probe target: raw TCP `PING` against the effective Redis URL.
- Healthy: `+PONG` response.
- Missing: connection refused or timeout.
- Incompatible: any other response.

## Docker Orchestration Rules

Command shape:

```text
docker compose -p <derived-project-name> -f <state-dir>/compose.yaml ...
```

Required operations:

- `docker compose version`
- `docker compose up -d`
- `docker compose down`
- `docker ps -aq --filter label=com.docker.compose.project=<project>`
- `docker inspect <containers...>`

Behavioral rules:

- never shell out through a string command
- always capture subprocess output
- never pass `-v` on teardown
- keep image overrides environment-driven but optional
- inject managed host ports through internal compose environment variables

## Port Allocation Rules

- Canonical ports remain the preferred defaults for a new managed stack.
- If a preferred port is unavailable, the runtime allocates a free localhost port.
- One managed stack uses one host port per service.
- Processes sharing the same state directory share the same managed ports.
- Different state directories may produce different Compose project names and different managed ports.

## File Touchpoints

### New runtime module

- `src/sourceweave_web_search/managed_runtime.py`
- responsibilities:
  - explicit endpoint detection
  - managed-stack discovery
  - canonical external reuse probe
  - packaged asset materialization
  - Docker command construction
  - dynamic port selection
  - session-state persistence
  - last-owner teardown

### MCP entrypoint

- `src/sourceweave_web_search/mcp_server.py`
- responsibilities:
  - parse CLI args
  - resolve runtime mode
  - build tools with effective overrides
  - start FastMCP

### Packaged assets

- `src/sourceweave_web_search/managed_runtime_assets/compose.yaml`
- `src/sourceweave_web_search/managed_runtime_assets/searxng-settings.yml`

### Tests

- `tests/test_managed_runtime.py`
- `tests/test_packaging.py`

## Failure Paths

### Docker unavailable

- show a concise error explaining Docker Compose is required for managed mode
- mention explicit endpoints as the bypass path

### Corrupt state file

- fail clearly
- avoid silent resets that could hide ownership mistakes

### Active sessions but missing managed-port state

- fail clearly because SourceWeave cannot safely restart the tracked stack

### Managed stack health timeout

- fail with the effective endpoints and last probe state
- do not proceed to FastMCP startup

### Canonical endpoints occupied by unrelated services

- do not fail solely because canonical defaults are busy
- allocate alternate managed ports when starting a new stack

## Minimality Rules

- Keep orchestration out of `tool.py`.
- Keep `build_mcp_server()` thin.
- Do not add support for `sourceweave-search` managed mode in this change.
- Do not alter OpenWebUI artifact generation.
