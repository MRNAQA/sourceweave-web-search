# Web Research Studio

Rebranded development repo for the OpenWebUI search and crawl tool.

## What It Includes

- `web_research_tool.py`: the OpenWebUI tool implementation
- `docker-compose.yml`: local stack with `openwebui`, `redis`, `crawl4ai`, and `searxng`
- `docker-compose.source.yml`: optional override to run against a sibling `../open-webui` checkout
- `tests/`: endpoint-level integration checks for SearXNG and Crawl4AI

## Local Setup

1. Install dependencies:

```bash
uv sync
```

2. Start the local stack:

```bash
docker compose up -d
```

3. Open OpenWebUI at `http://localhost:3300`.

The stack bootstraps the `web_research_tool` tool into OpenWebUI automatically after startup.

## Source Debug Mode

Clone the official OpenWebUI repo as a sibling directory:

```bash
git clone https://github.com/open-webui/open-webui ../open-webui
```

Then run:

```bash
docker compose -f docker-compose.yml -f docker-compose.source.yml up -d --build
```

Use this mode when you need to inspect or patch OpenWebUI internals while iterating on the tool.

## Tests

Run the endpoint integration checks after the containers are healthy:

```bash
uv run python tests/test_tool.py
uv run python tests/test_phase4.py
```

Default host ports used by this repo:

- OpenWebUI: `3300`
- SearXNG: `19080`
- Crawl4AI: `19235`

The local OpenWebUI stack is configured to use SearXNG for native web search by default.

## Notes

- The tool defaults SearXNG to `http://searxng:8080/...` to match the local compose stack.
- Native OpenWebUI search remains enabled in the tool when OpenWebUI internals are importable.
