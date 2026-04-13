# Changelog

## 0.2.1

- add direct URL support to `read_pages`, including explicit per-URL document conversion for direct reads
- make `focus` explicitly optional for `read_pages`, with empty focus performing a normal cleaned read
- improve MCP and OpenWebUI tool descriptions so agents understand when to search first versus read directly by URL
- add MCP Registry metadata and publishing workflow, including `server.json` and registry verification markers
- add optional release publishing to PyPI, GHCR, and Docker Hub from the manual GitHub release workflow
- improve README deployment guidance with `uvx`, local service containers, and container-compose examples
- tighten release hygiene with explicit local ignore rules, stronger packaging metadata, and release metadata sync checks
