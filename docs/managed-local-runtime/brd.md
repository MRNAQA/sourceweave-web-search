# Managed Local Runtime BRD

## Document Control

- Feature: Managed Local Runtime
- Product: SourceWeave Web Search
- Primary entrypoint: `sourceweave-search-mcp`
- Status: Implemented and verified
- Scope type: New runtime behavior for package-based MCP usage

## Executive Summary

SourceWeave Web Search currently requires operators to provision and manage SearXNG, Crawl4AI, and Redis or Valkey separately before starting the MCP server. This creates friction for MCP client adoption, especially in tools such as OpenCode and VS Code where users expect a single local command to work.

Managed Local Runtime removes that friction by making `sourceweave-search-mcp` automatically manage the local Docker-backed dependency stack when explicit runtime endpoints are not provided. The MCP server will continue to honor user-supplied `SOURCEWEAVE_SEARCH_*` endpoints exactly as it does today. When those endpoints are absent, the entrypoint will first try to discover an existing SourceWeave-managed stack for the current local state directory, then try to reuse a healthy compatible stack on the canonical default ports, and otherwise start and supervise its own Docker-backed stack on ports it allocates safely.

This feature preserves the current architecture and public MCP contract while improving first-run usability, avoiding port conflicts with unrelated services, and keeping persistent cache volumes across sessions.

## Problem Statement

The current package story has a gap between installation and successful use:

- Users can install and launch `sourceweave-search-mcp` easily.
- Users cannot use it successfully without separately provisioning three supporting services.
- Documentation currently asks users to know and export three endpoint variables before the MCP server can function.
- MCP clients that favor simple local server registration make this setup feel heavier than competing single-command tools.
- Fixed-port assumptions create failure modes when `19080`, `19235`, or `16379` are already used by unrelated services.

As a result, package installation is simple but actual adoption is operationally complex.

## Background And Current State

The runtime architecture today is intentionally split:

- SearXNG provides search result discovery.
- Crawl4AI provides cleaned HTML extraction.
- Redis or Valkey provides the canonical page cache and page-id persistence layer.
- `src/sourceweave_web_search/tool.py` consumes those services through configured endpoints.
- `src/sourceweave_web_search/mcp_server.py` is currently a thin server entrypoint with no dependency orchestration.

Current default package endpoints target host-side local ports:

- SearXNG: `http://127.0.0.1:19080/search?format=json&q=<query>`
- Crawl4AI: `http://127.0.0.1:19235`
- Redis: `redis://127.0.0.1:16379/2`

Those defaults remain important for compatibility and external reuse, but managed runtime itself must not require those ports to be free.

## Business Goal

Make the published package feel self-starting for local MCP users without changing the core search architecture or requiring a rewrite of SearXNG, Crawl4AI, or the caching layer.

## Product Goal

Enable a zero-endpoint local startup path where `sourceweave-search-mcp` works out of the box for users who have Docker and Docker Compose available, while still preserving explicit endpoint control for advanced or externally hosted deployments.

## Success Metrics

Primary success indicators:

- Users can run `uvx --from sourceweave-web-search sourceweave-search-mcp` without manually setting `SOURCEWEAVE_SEARCH_*` variables.
- MCP client configuration for the common local case no longer needs explicit runtime endpoint environment variables.
- Existing explicit-endpoint deployments continue to function unchanged.
- Local cache volumes persist across managed runtime restarts.
- Multiple local MCP clients can coexist without tearing down the shared stack prematurely.
- Managed runtime can start successfully even when the canonical default ports are already occupied by unrelated services.

Secondary success indicators:

- Fewer setup steps in README and MCP client examples.
- Clear runtime status and failure messaging when Docker or dependencies are unavailable.
- No regression in the public MCP tool contract.

## Target Users

Primary users:

- Developers using SourceWeave from OpenCode, VS Code, or similar MCP clients on a local workstation.
- Users evaluating the package directly from PyPI with `uvx`.

Secondary users:

- Developers running from a repo checkout who want the same local convenience.
- Operators pointing at externally hosted SearXNG, Crawl4AI, or Redis endpoints.

## User Needs

Users need:

- A single package-based startup command for the local default case.
- Reliable reuse of an already-running compatible local stack.
- Persistent cache behavior across restarts.
- The ability to override with explicit endpoints when needed.
- Predictable behavior in multi-client scenarios.
- Clear failure messages when Docker is missing, unhealthy, or blocked.
- Safe behavior when default host ports are already in use by something else.

## In Scope

This feature includes:

- Managed runtime behavior in `sourceweave-search-mcp` when explicit `SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL`, `SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL`, and `SOURCEWEAVE_SEARCH_CACHE_REDIS_URL` are absent.
- Discovery of an existing SourceWeave-managed stack by Docker Compose project identity for the current local state directory.
- Reuse of a healthy pre-existing compatible stack listening on the canonical default ports.
- Local Docker Compose orchestration for SearXNG, Crawl4AI, and Redis or Valkey.
- Dynamic managed host-port allocation when canonical ports are unavailable.
- Managed lifecycle ownership only when SourceWeave starts the stack.
- Shared-stack coordination across multiple local client sessions.
- Persisted data volumes for cache continuity.
- Updated docs and MCP package metadata reflecting optional endpoint variables.

## Out Of Scope

This feature does not include:

- Rewriting SearXNG, Crawl4AI, or Redis functionality into a single Python process.
- Changing the public MCP tool contract.
- Changing the default endpoint values in `Tools.Valves`.
- Automatic managed runtime for the direct CLI `sourceweave-search` in this phase.
- One isolated managed Docker stack per MCP process.
- Support for Dockerless embedded service implementations.
- New external cloud services.
- Changes to the generated OpenWebUI artifact behavior.

## Product Principles

- Preserve the current architecture.
- Prefer the smallest operational change that materially improves user experience.
- Keep explicit configuration authoritative.
- Reuse healthy local infrastructure when already present.
- Avoid surprising teardown of services another client is still using.
- Keep local state durable where durability already matters.
- Do not require canonical ports for managed startup when safe alternatives exist.

## Core User Journeys

### Journey 1: First-time package user

1. User configures an MCP client to run `sourceweave-search-mcp` from the published package.
2. User does not set `SOURCEWEAVE_SEARCH_*` endpoint variables.
3. SourceWeave checks for an existing managed stack for the current state directory.
4. If none is found, SourceWeave probes the canonical default ports for a healthy reusable stack.
5. If no reusable stack is found, SourceWeave starts the managed local Docker stack on preferred or dynamically allocated host ports.
6. SourceWeave waits for the stack to become healthy.
7. SourceWeave starts the MCP server and serves requests normally.

Desired outcome: first-run success with a single local command plus Docker availability.

### Journey 2: User already runs local supporting services manually

1. User already has SearXNG, Crawl4AI, and Redis or Valkey running on the canonical default ports.
2. User launches `sourceweave-search-mcp` without endpoint env vars.
3. SourceWeave probes those ports, finds healthy services, and reuses them.
4. No new containers are started and no lifecycle ownership is assumed.

Desired outcome: compatibility with existing manual workflows and no duplicate stack startup.

### Journey 3: Explicit hosted deployment

1. User sets one or more `SOURCEWEAVE_SEARCH_*` endpoint variables.
2. SourceWeave bypasses managed mode entirely.
3. MCP server runs exactly against the supplied endpoints.

Desired outcome: preserve all current explicit configuration behavior.

### Journey 4: Multiple local MCP clients

1. One client starts the managed runtime for a local state directory.
2. SourceWeave records the allocated host ports and active session information in the local runtime state.
3. A second client starts later with no endpoint env vars.
4. The second client discovers the same managed Docker project for that state directory and joins the shared runtime.
5. If the original owning process exits unexpectedly, a later client discovers the existing project or restarts it using the persisted managed ports.
6. When the last active owner exits, the stack is torn down without removing named volumes.

Desired outcome: safe shared local usage with recovery after crashed owners.

## Functional Requirements

### Runtime selection

- The system must preserve current explicit-endpoint behavior when endpoint env vars are present.
- The system must enter managed local runtime mode only when the endpoint env vars are absent.
- The system must discover an existing SourceWeave-managed stack for the current state directory before probing canonical default ports.
- The system must reuse a healthy compatible stack found on the canonical default ports without taking ownership.

### Managed stack startup

- The system must be able to materialize all required runtime assets without depending on repo checkout files.
- The system must start SearXNG, Crawl4AI, and Redis or Valkey using Docker Compose subprocesses.
- The system must wait for services to become healthy before launching the MCP server.
- The system must inject the actual managed effective endpoints into the tool build path once the managed stack is ready.
- The system must prefer canonical default ports for a new managed stack when they are available.
- The system must fall back to safe free host ports when canonical defaults are occupied.

### Lifecycle management

- The system must distinguish between a reused external stack and a stack started by this managed runtime.
- The system must track active managed-runtime sessions across processes.
- The system must persist the managed stack's allocated ports for restart and recovery.
- The system must not tear down the stack while other active sessions remain.
- The system must perform stale-session cleanup when previous owners exited unexpectedly.
- The system must preserve named volumes when shutting down managed containers.

### Failure handling

- The system must fail with a clear message when Docker or Docker Compose is unavailable.
- The system must fail with a clear message when the managed stack cannot become healthy.
- The system must fail with a clear message when runtime state is corrupt or missing required managed-port information for active sessions.
- The system must not emit noisy Docker logs into stdio MCP transport output.

### Documentation and metadata

- The README must document the new default local behavior.
- MCP client setup examples must show the simpler local configuration.
- `server.json` must reflect that endpoint variables are optional overrides rather than required configuration.

## Non-Functional Requirements

- Runtime startup behavior must be deterministic and observable.
- The feature must preserve current public MCP tool names and parameter schemas.
- The feature must remain compatible with packaged wheels that exclude repo-only runtime files.
- The implementation must avoid destructive teardown of persistent cache volumes.
- The implementation must be testable with deterministic unit tests.

## Assumptions

- Docker and Docker Compose are available on the host for managed local runtime users.
- Explicit endpoint users want no change in behavior.
- The repo-local Docker topology is already the correct architecture to package and reuse.
- One managed Docker project per local state directory is sufficient for the default package workflow.

## Constraints

- `tool.py` remains the source of truth for runtime behavior and endpoint defaults.
- Packaging tests currently exclude `infrastructure/` and `docker-compose.yml` from wheels.
- The published image path should continue to rely on explicit endpoint injection, not self-managed Docker.
- The public MCP contract must remain `search_web`, `read_pages`, and `read_urls`.

## Risks

- Docker availability varies by host environment.
- Shared runtime lifecycle coordination can become error-prone if session state is not robust.
- Managed runtime assets can drift from repo-local compose definitions if not validated.
- Users may misinterpret the feature as meaning Docker is no longer required.
- Multiple state directories can intentionally create multiple managed stacks, increasing local resource usage.

## Mitigations

- Discover managed stacks by Docker project identity and persisted runtime state.
- Keep explicit endpoints authoritative.
- Use lock-based session coordination with stale owner cleanup.
- Persist managed ports so later processes can restart the stack after crashes.
- Preserve named volumes and remove only containers on last-owner shutdown.
- Add tests comparing packaged assets and runtime discovery behavior to expected managed semantics.
- Document Docker as still required for the auto-managed local path.

## Dependencies

- Docker CLI
- Docker Compose support
- Existing container images for SearXNG, Crawl4AI, and Redis or Valkey
- Current SourceWeave runtime config and MCP server entrypoint

## Release Impact

This feature changes:

- local package onboarding behavior
- `sourceweave-search-mcp` startup semantics when endpoint env vars are absent
- README setup instructions
- `server.json` environment variable metadata

This feature does not change:

- tool names
- tool schemas
- package default endpoint constants
- explicit endpoint deployments

## Acceptance Criteria

- Running `uvx --from sourceweave-web-search sourceweave-search-mcp` without endpoint env vars starts or reuses a healthy local runtime and serves MCP successfully.
- Running with explicit `SOURCEWEAVE_SEARCH_*` endpoints bypasses all managed-runtime orchestration.
- A healthy pre-existing stack on the canonical default ports is reused without being torn down by SourceWeave.
- If canonical defaults are occupied by unrelated services, SourceWeave can still start its own managed stack on alternate free ports.
- A stack started by SourceWeave is torn down only when the last active owning session exits.
- Redis or Valkey-backed cached data survives managed runtime restarts.
- README and MCP metadata reflect the new default behavior accurately.

## Open Decisions Resolved

- Managed behavior is the default when endpoint env vars are absent.
- The feature includes external-reuse probing on the canonical default endpoints.
- The implementation targets `sourceweave-search-mcp`, not a new launcher.
- Managed runtime discovery is based on per-state-dir Docker project identity plus persisted managed ports.
- Shutdown removes containers but preserves volumes.

## Future Considerations

- Extending managed runtime behavior to the direct CLI.
- Adding a user-visible status command for the managed stack.
- Adding telemetry or diagnostics around startup timing and failures.
- Supporting alternate local runtimes in environments without Docker.
