# Changelog

## 0.2.2

- fix MCP Registry publishing by aligning the registry namespace with the GitHub OIDC publisher namespace
- simplify `server.json` to the supported PyPI package metadata path for registry publication
- make the release metadata sync script dependency-free so lightweight release workflows can run it before installing runtime dependencies
- align the publishable MCP container name and OCI labels across Dockerfile, release automation, docs, and local compose

## 0.2.1

- add direct URL support to `read_pages`, including explicit per-URL document conversion for direct reads
- make `focus` explicitly optional for `read_pages`, with empty focus performing a normal cleaned read
- improve MCP and OpenWebUI tool descriptions so agents understand when to search first versus read directly by URL
- add MCP Registry metadata and publishing workflow, including `server.json` and registry verification markers
- add optional release publishing to PyPI, GHCR, and Docker Hub from the manual GitHub release workflow
- improve README deployment guidance with `uvx`, local service containers, and container-compose examples
- tighten release hygiene with explicit local ignore rules, stronger packaging metadata, and release metadata sync checks
