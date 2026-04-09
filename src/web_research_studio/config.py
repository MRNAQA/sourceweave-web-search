import os
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from web_research_studio.tool import Tools


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _coerce_env_value(raw_value: str, current_value: Any) -> Any:
    if isinstance(current_value, bool):
        return _parse_bool(raw_value)
    if isinstance(current_value, int) and not isinstance(current_value, bool):
        return int(raw_value)
    if isinstance(current_value, float):
        return float(raw_value)
    return raw_value


@dataclass(slots=True)
class RuntimeOverrides:
    valve_overrides: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "RuntimeOverrides":
        defaults = Tools.Valves()
        overrides: dict[str, Any] = {}
        for field_name in Tools.Valves.model_fields:
            env_name = f"WEB_RESEARCH_{field_name}"
            raw_value = os.getenv(env_name)
            if raw_value is None:
                continue
            overrides[field_name] = _coerce_env_value(
                raw_value,
                getattr(defaults, field_name),
            )
        return cls(valve_overrides=overrides)

    def apply(self, tool: Tools) -> Tools:
        for field_name, value in self.valve_overrides.items():
            if hasattr(tool.valves, field_name):
                setattr(tool.valves, field_name, value)

        tool._cache.url = tool.valves.CACHE_REDIS_URL
        tool._cache.enabled = tool.valves.CACHE_ENABLED

        if tool.valves.SEARCH_WITH_SEARXNG and tool.valves.SEARXNG_BASE_URL:
            tool.valves.SEARXNG_BASE_URL = _normalize_searxng_base_url(
                tool.valves.SEARXNG_BASE_URL
            )

        return tool


def _normalize_searxng_base_url(base_url: str) -> str:
    parsed_url = urlparse(base_url)
    parsed_query = parse_qs(parsed_url.query)
    if "q" not in parsed_query:
        parsed_query["q"] = ["<query>"]
    if "format" in parsed_query and parsed_query["format"][0] != "json":
        parsed_query["format"][0] = "json"

    reconstructed_query = "&".join(
        f"{key}={value[0]}" for key, value in parsed_query.items()
    )
    return (
        f"{parsed_url.scheme}://{parsed_url.netloc}"
        f"{parsed_url.path}?{reconstructed_query}"
    )


def build_tools(
    *,
    runtime_overrides: RuntimeOverrides | None = None,
    valve_overrides: Mapping[str, Any] | None = None,
) -> Tools:
    tool = Tools()
    (runtime_overrides or RuntimeOverrides.from_env()).apply(tool)

    for field_name, value in (valve_overrides or {}).items():
        if value is None or not hasattr(tool.valves, field_name):
            continue
        setattr(tool.valves, field_name, value)

    return tool
