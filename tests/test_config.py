from pathlib import Path
from urllib.parse import parse_qs, urlparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sourceweave_web_search.config import RuntimeOverrides, build_tools
from sourceweave_web_search.tool import Tools


def test_runtime_overrides_apply_updates_cache_state():
    tool = Tools()
    tool._cache._unavailable_until = 99.0

    RuntimeOverrides(
        valve_overrides={
            "CACHE_REDIS_URL": "redis://custom-cache:6379/9",
            "CACHE_ENABLED": False,
        }
    ).apply(tool)

    assert tool.valves.CACHE_REDIS_URL == "redis://custom-cache:6379/9"
    assert tool._cache.url == "redis://custom-cache:6379/9"
    assert tool._cache.enabled is False
    assert tool._cache._redis is None
    assert tool._cache._unavailable_until == 0.0


def test_build_tools_valve_overrides_update_cache_and_normalize_searxng():
    tool = build_tools(
        valve_overrides={
            "CACHE_REDIS_URL": "redis://override-cache:6379/3",
            "SEARXNG_BASE_URL": "http://search.internal/search",
        }
    )

    parsed = urlparse(tool.valves.SEARXNG_BASE_URL)
    parsed_query = parse_qs(parsed.query)

    assert tool._cache.url == "redis://override-cache:6379/3"
    assert parsed.netloc == "search.internal"
    assert parsed_query["q"] == ["<query>"]
    assert parsed_query["format"] == ["json"]


def test_build_tools_cli_overrides_take_precedence_over_runtime_overrides():
    tool = build_tools(
        runtime_overrides=RuntimeOverrides(
            valve_overrides={
                "CACHE_REDIS_URL": "redis://runtime-cache:6379/0",
                "SEARXNG_BASE_URL": "http://runtime.internal/search?format=xml",
            }
        ),
        valve_overrides={
            "CACHE_REDIS_URL": "redis://cli-cache:6379/1",
            "SEARXNG_BASE_URL": "http://cli.internal/search",
        },
    )

    parsed = urlparse(tool.valves.SEARXNG_BASE_URL)
    parsed_query = parse_qs(parsed.query)

    assert tool._cache.url == "redis://cli-cache:6379/1"
    assert parsed.netloc == "cli.internal"
    assert parsed_query["q"] == ["<query>"]
    assert parsed_query["format"] == ["json"]


def test_build_tools_ignores_none_valve_overrides():
    tool = build_tools(
        runtime_overrides=RuntimeOverrides(
            valve_overrides={"CACHE_REDIS_URL": "redis://runtime-cache:6379/4"}
        ),
        valve_overrides={"CACHE_REDIS_URL": None},
    )

    assert tool._cache.url == "redis://runtime-cache:6379/4"
    assert tool.valves.CACHE_REDIS_URL == "redis://runtime-cache:6379/4"


def test_runtime_overrides_keep_searxng_disabled_while_syncing_url_state():
    tool = Tools()

    RuntimeOverrides(
        valve_overrides={
            "SEARCH_WITH_SEARXNG": False,
            "SEARXNG_BASE_URL": "http://search.internal/search",
        }
    ).apply(tool)

    parsed = urlparse(tool.valves.SEARXNG_BASE_URL)
    parsed_query = parse_qs(parsed.query)

    assert tool.valves.SEARCH_WITH_SEARXNG is False
    assert parsed.netloc == "search.internal"
    assert parsed_query["q"] == ["<query>"]
    assert parsed_query["format"] == ["json"]
