import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sourceweave_web_search import managed_runtime as runtime


def _healthy_probe() -> runtime.ProbeResult:
    return runtime.ProbeResult(
        searxng=runtime.ServiceProbe("healthy"),
        crawl4ai=runtime.ServiceProbe("healthy"),
        redis=runtime.ServiceProbe("healthy"),
    )


def _missing_probe() -> runtime.ProbeResult:
    return runtime.ProbeResult(
        searxng=runtime.ServiceProbe("missing"),
        crawl4ai=runtime.ServiceProbe("missing"),
        redis=runtime.ServiceProbe("missing"),
    )


def test_explicit_runtime_endpoints_configured_detects_any_endpoint() -> None:
    assert runtime.explicit_runtime_endpoints_configured(
        {"SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL": "http://example.test"}
    )
    assert not runtime.explicit_runtime_endpoints_configured({})


def test_materialize_runtime_assets_writes_packaged_files(tmp_path: Path) -> None:
    runtime.materialize_runtime_assets(tmp_path)

    compose_path = tmp_path / "compose.yaml"
    settings_path = tmp_path / "searxng-settings.yml"

    assert compose_path.exists()
    assert settings_path.exists()
    compose_text = compose_path.read_text(encoding="utf-8")
    assert "SOURCEWEAVE_MANAGED_RUNTIME_SEARXNG_HOST_PORT" in compose_text
    assert "SOURCEWEAVE_MANAGED_RUNTIME_CRAWL4AI_HOST_PORT" in compose_text
    assert "SOURCEWEAVE_MANAGED_RUNTIME_REDIS_HOST_PORT" in compose_text
    assert "keep_only:" in settings_path.read_text(encoding="utf-8")


def test_compose_command_uses_state_dir_specific_project_name(tmp_path: Path) -> None:
    command = runtime.compose_command(tmp_path, "up", "-d")

    assert command[:3] == ["docker", "compose", "-p"]
    assert command[3].startswith("sourceweave-web-search-managed-")
    assert command[4:7] == ["-f", str(tmp_path / "compose.yaml"), "up"]
    assert command[7] == "-d"


def test_resolve_managed_runtime_bypasses_when_explicit_env_present(
    tmp_path: Path,
) -> None:
    session = runtime.resolve_managed_runtime(
        env={"SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL": "http://search.internal/search"},
        state_dir=tmp_path,
    )

    assert session.mode == "explicit"
    assert session.valve_overrides == {}
    assert not (tmp_path / "compose.yaml").exists()


def test_resolve_managed_runtime_reuses_healthy_external_canonical_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "_discover_managed_stack", lambda state_dir: None)
    monkeypatch.setattr(runtime, "probe_canonical_runtime", lambda timeout_s=2.0: _healthy_probe())

    session = runtime.resolve_managed_runtime(env={}, state_dir=tmp_path)

    assert session.mode == "reused"
    assert session.valve_overrides == {
        "SEARXNG_BASE_URL": "http://127.0.0.1:19080/search?format=json&q=<query>",
        "CRAWL4AI_BASE_URL": "http://127.0.0.1:19235",
        "CACHE_REDIS_URL": "redis://127.0.0.1:16379/2",
    }
    state = runtime._load_state(tmp_path)
    assert state["managed_ports"] == {}
    assert state["sessions"] == []


def test_resolve_managed_runtime_starts_managed_stack_on_dynamic_ports_when_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[tuple[str, ...], dict[str, str] | None]] = []
    selected_stack = runtime._stack_from_ports(
        {"searxng": 29180, "crawl4ai": 29235, "redis": 26379}
    )

    monkeypatch.setattr(runtime, "_discover_managed_stack", lambda state_dir: None)
    monkeypatch.setattr(runtime, "probe_canonical_runtime", lambda timeout_s=2.0: _missing_probe())
    monkeypatch.setattr(runtime, "_ensure_compose_available", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_select_managed_stack",
        lambda preferred_ports=None: selected_stack,
    )
    monkeypatch.setattr(runtime, "_wait_for_healthy_stack", lambda valve_overrides, timeout_s: None)
    monkeypatch.setattr(runtime, "_pid_is_alive", lambda pid: True)

    def fake_run_compose_command(
        state_dir: Path,
        *args: str,
        timeout_s: float | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        _ = timeout_s
        commands.append((tuple(args), env_overrides))

    monkeypatch.setattr(runtime, "_run_compose_command", fake_run_compose_command)

    with runtime.resolve_managed_runtime(env={}, state_dir=tmp_path) as session:
        assert session.mode == "managed"
        assert session.valve_overrides == selected_stack.valve_overrides
        state = json.loads(
            (tmp_path / "managed-runtime-state.json").read_text(encoding="utf-8")
        )
        assert state["stack_started_by_sourceweave"] is True
        assert state["managed_ports"] == {
            "searxng": 29180,
            "crawl4ai": 29235,
            "redis": 26379,
        }
        assert len(state["sessions"]) == 1

    assert commands == [
        (
            ("up", "-d"),
            {
                "SOURCEWEAVE_MANAGED_RUNTIME_SEARXNG_HOST_PORT": "29180",
                "SOURCEWEAVE_MANAGED_RUNTIME_CRAWL4AI_HOST_PORT": "29235",
                "SOURCEWEAVE_MANAGED_RUNTIME_REDIS_HOST_PORT": "26379",
            },
        ),
        (
            ("down",),
            {
                "SOURCEWEAVE_MANAGED_RUNTIME_SEARXNG_HOST_PORT": "29180",
                "SOURCEWEAVE_MANAGED_RUNTIME_CRAWL4AI_HOST_PORT": "29235",
                "SOURCEWEAVE_MANAGED_RUNTIME_REDIS_HOST_PORT": "26379",
            },
        ),
    ]


def test_resolve_managed_runtime_discovers_existing_managed_stack_by_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discovered_stack = runtime._stack_from_ports(
        {"searxng": 30180, "crawl4ai": 30235, "redis": 30379}
    )

    monkeypatch.setattr(
        runtime,
        "_discover_managed_stack",
        lambda state_dir: discovered_stack,
    )
    monkeypatch.setattr(runtime, "probe_runtime", lambda valve_overrides, timeout_s=2.0: _healthy_probe())
    monkeypatch.setattr(runtime, "_pid_is_alive", lambda pid: True)

    with runtime.resolve_managed_runtime(env={}, state_dir=tmp_path) as session:
        assert session.mode == "managed"
        assert session.valve_overrides == discovered_stack.valve_overrides
        state = runtime._load_state(tmp_path)
        assert state["managed_ports"] == discovered_stack.allocated_ports


def test_resolve_managed_runtime_recovers_missing_managed_stack_using_state_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous_stack = runtime._stack_from_ports(
        {"searxng": 31180, "crawl4ai": 31235, "redis": 31379}
    )
    runtime._write_state(
        tmp_path,
        {
            "version": 2,
            "stack_started_by_sourceweave": True,
            "managed_ports": previous_stack.allocated_ports,
            "sessions": [
                {
                    "session_id": "live-session",
                    "pid": 9999,
                    "started_at": 1.0,
                    "last_seen_at": 1.0,
                }
            ],
            "updated_at": 1.0,
        },
    )
    commands: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    monkeypatch.setattr(runtime, "_discover_managed_stack", lambda state_dir: None)
    monkeypatch.setattr(runtime, "_ensure_compose_available", lambda: None)
    monkeypatch.setattr(runtime, "_wait_for_healthy_stack", lambda valve_overrides, timeout_s: None)
    monkeypatch.setattr(runtime, "_pid_is_alive", lambda pid: True)

    def fake_run_compose_command(
        state_dir: Path,
        *args: str,
        timeout_s: float | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        _ = timeout_s
        commands.append((tuple(args), env_overrides))

    monkeypatch.setattr(runtime, "_run_compose_command", fake_run_compose_command)

    with runtime.resolve_managed_runtime(env={}, state_dir=tmp_path) as session:
        assert session.mode == "managed"
        assert session.valve_overrides == previous_stack.valve_overrides

    assert commands[0] == (
        ("up", "-d"),
        {
            "SOURCEWEAVE_MANAGED_RUNTIME_SEARXNG_HOST_PORT": "31180",
            "SOURCEWEAVE_MANAGED_RUNTIME_CRAWL4AI_HOST_PORT": "31235",
            "SOURCEWEAVE_MANAGED_RUNTIME_REDIS_HOST_PORT": "31379",
        },
    )


def test_select_managed_stack_prefers_canonical_ports_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_port_is_available", lambda port: True)
    stack = runtime._select_managed_stack()

    assert stack.allocated_ports == {"searxng": 19080, "crawl4ai": 19235, "redis": 16379}


def test_select_managed_stack_falls_back_to_free_ports_when_canonical_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    free_ports = iter([40180, 40235, 46379])
    monkeypatch.setattr(runtime, "_port_is_available", lambda port: False)
    monkeypatch.setattr(runtime, "_find_free_port", lambda used_ports: next(free_ports))

    stack = runtime._select_managed_stack()

    assert stack.allocated_ports == {"searxng": 40180, "crawl4ai": 40235, "redis": 46379}


def test_cleanup_stale_sessions_removes_dead_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "version": 2,
        "stack_started_by_sourceweave": True,
        "managed_ports": {"searxng": 19080, "crawl4ai": 19235, "redis": 16379},
        "sessions": [
            {"session_id": "dead", "pid": 99999, "started_at": 1.0, "last_seen_at": 1.0},
            {"session_id": "live", "pid": 12345, "started_at": 2.0, "last_seen_at": 2.0},
        ],
        "updated_at": 0.0,
    }

    monkeypatch.setattr(runtime, "_pid_is_alive", lambda pid: pid == 12345)
    cleaned = runtime._cleanup_stale_sessions(state)

    assert [session["session_id"] for session in cleaned["sessions"]] == ["live"]


def test_release_managed_session_only_last_owner_stops_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {
        "version": 2,
        "stack_started_by_sourceweave": True,
        "managed_ports": {"searxng": 29180, "crawl4ai": 29235, "redis": 26379},
        "sessions": [
            {"session_id": "one", "pid": 1, "started_at": 1.0, "last_seen_at": 1.0},
            {"session_id": "two", "pid": 2, "started_at": 2.0, "last_seen_at": 2.0},
        ],
        "updated_at": 0.0,
    }
    commands: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    runtime._write_state(tmp_path, state)
    monkeypatch.setattr(runtime, "_pid_is_alive", lambda pid: True)

    def fake_run_compose_command(
        state_dir: Path,
        *args: str,
        timeout_s: float | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        _ = timeout_s
        commands.append((tuple(args), env_overrides))

    monkeypatch.setattr(runtime, "_run_compose_command", fake_run_compose_command)

    runtime._release_managed_session(tmp_path, "one")
    assert commands == []
    intermediate = runtime._load_state(tmp_path)
    assert [session["session_id"] for session in intermediate["sessions"]] == ["two"]

    runtime._release_managed_session(tmp_path, "two")
    assert commands == [
        (
            ("down",),
            {
                "SOURCEWEAVE_MANAGED_RUNTIME_SEARXNG_HOST_PORT": "29180",
                "SOURCEWEAVE_MANAGED_RUNTIME_CRAWL4AI_HOST_PORT": "29235",
                "SOURCEWEAVE_MANAGED_RUNTIME_REDIS_HOST_PORT": "26379",
            },
        )
    ]
    final_state = runtime._load_state(tmp_path)
    assert final_state["stack_started_by_sourceweave"] is False
    assert final_state["managed_ports"] == {}
    assert final_state["sessions"] == []


def test_discover_managed_stack_reads_docker_project_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = runtime.compose_project_name(tmp_path)

    class Result:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run_docker_command(*args: str, timeout_s: float | None = None) -> Result:
        _ = timeout_s
        if args[0] == "ps":
            assert args[-1] == f"label=com.docker.compose.project={project}"
            return Result("abc123\ndef456\n")
        if args[0] == "inspect":
            payload = [
                {
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": project,
                            "com.docker.compose.service": "searxng",
                        }
                    },
                    "NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": "50180"}]}},
                },
                {
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": project,
                            "com.docker.compose.service": "crawl4ai",
                        }
                    },
                    "NetworkSettings": {"Ports": {"11235/tcp": [{"HostPort": "50235"}]}},
                },
                {
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": project,
                            "com.docker.compose.service": "redis",
                        }
                    },
                    "NetworkSettings": {"Ports": {"6379/tcp": [{"HostPort": "56379"}]}},
                },
            ]
            return Result(json.dumps(payload))
        raise AssertionError(args)

    monkeypatch.setattr(runtime, "_run_docker_command", fake_run_docker_command)

    stack = runtime._discover_managed_stack(tmp_path)

    assert stack is not None
    assert stack.allocated_ports == {"searxng": 50180, "crawl4ai": 50235, "redis": 56379}
