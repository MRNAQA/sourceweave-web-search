# Changelog

## Unreleased

## 0.3.0

- simplify the implementation back to the minimal public search/read behavior after reviewing the hidden internal feature paths with a KISS/YAGNI lens
- keep the default `fit_markdown`-first cleaned output path and compact `search_web` surface aligned across code, tests, docs, and the OpenWebUI artifact
- preserve Crawl4AI-extracted `tables` by default through crawl and cache so `read_pages` returns them without adding new public flags
- preserve Crawl4AI image `desc` values by default alongside `url` and `alt`, and document the verified CLI behavior in README and tool descriptions

## 0.2.3

- sharpen `search_web` and `read_pages` descriptions so MCP clients understand when SourceWeave is a better fit than generic fetch tools for search-first, batched web research
- remove the in-process page store and make Redis or Valkey the canonical persisted page cache with richer stored crawl representations
- integrate Crawl4AI `CacheMode` intentionally so direct reads bypass upstream cache while fresh searches force a new fetch and still warm Crawl4AI's cache
- fix direct URL and document reads to return immediately from the freshly fetched record even when cache persistence is unavailable in the same call
- refresh README, contributing notes, and repo skills to reflect the Redis-only cache design, current quality gate, and release workflow expectations

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
