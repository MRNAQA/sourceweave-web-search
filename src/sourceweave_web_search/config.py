import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from sourceweave_web_search.tool import Tools


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
            env_name = f"SOURCEWEAVE_SEARCH_{field_name}"
            raw_value = os.getenv(env_name)
            if raw_value is None:
                continue
            overrides[field_name] = _coerce_env_value(
                raw_value,
                getattr(defaults, field_name),
            )
        return cls(valve_overrides=overrides)

    def apply(self, tool: Tools) -> Tools:
        return tool.apply_valve_overrides(self.valve_overrides)


def _sync_runtime_state(tool: Tools) -> None:
    tool._sync_runtime_state()


def build_tools(
    *,
    runtime_overrides: RuntimeOverrides | None = None,
    valve_overrides: Mapping[str, Any] | None = None,
) -> Tools:
    tool = Tools()
    merged_overrides = dict(
        (runtime_overrides or RuntimeOverrides.from_env()).valve_overrides
    )
    for field_name, value in (valve_overrides or {}).items():
        if value is None or not hasattr(tool.valves, field_name):
            continue
        merged_overrides[field_name] = value

    return tool.apply_valve_overrides(merged_overrides)
