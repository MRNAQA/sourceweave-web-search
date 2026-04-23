from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from importlib import resources
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus, urlparse

from sourceweave_web_search.tool import Tools

_RUNTIME_ENV_KEYS = (
    "SOURCEWEAVE_SEARCH_SEARXNG_BASE_URL",
    "SOURCEWEAVE_SEARCH_CRAWL4AI_BASE_URL",
    "SOURCEWEAVE_SEARCH_CACHE_REDIS_URL",
)
_CANONICAL_VALVES = Tools.Valves()
_CANONICAL_VALVE_OVERRIDES = {
    "SEARXNG_BASE_URL": _CANONICAL_VALVES.SEARXNG_BASE_URL,
    "CRAWL4AI_BASE_URL": _CANONICAL_VALVES.CRAWL4AI_BASE_URL,
    "CACHE_REDIS_URL": _CANONICAL_VALVES.CACHE_REDIS_URL,
}
_CANONICAL_SEARXNG_BASE_URL = _CANONICAL_VALVE_OVERRIDES["SEARXNG_BASE_URL"]
_CANONICAL_CRAWL4AI_BASE_URL = _CANONICAL_VALVE_OVERRIDES["CRAWL4AI_BASE_URL"]
_CANONICAL_REDIS_URL = _CANONICAL_VALVE_OVERRIDES["CACHE_REDIS_URL"]
_CANONICAL_SERVICE_PORTS = {
    "searxng": urlparse(_CANONICAL_SEARXNG_BASE_URL).port or 80,
    "crawl4ai": urlparse(_CANONICAL_CRAWL4AI_BASE_URL).port or 80,
    "redis": urlparse(_CANONICAL_REDIS_URL).port or 6379,
}
_SERVICE_CONTAINER_PORTS = {
    "searxng": "8080/tcp",
    "crawl4ai": "11235/tcp",
    "redis": "6379/tcp",
}
_PORT_ENV_KEYS = {
    "searxng": "SOURCEWEAVE_MANAGED_RUNTIME_SEARXNG_HOST_PORT",
    "crawl4ai": "SOURCEWEAVE_MANAGED_RUNTIME_CRAWL4AI_HOST_PORT",
    "redis": "SOURCEWEAVE_MANAGED_RUNTIME_REDIS_HOST_PORT",
}
_LOCK_FILENAME = "managed-runtime.lock"
_STATE_FILENAME = "managed-runtime-state.json"
_COMPOSE_FILENAME = "compose.yaml"
_SEARXNG_SETTINGS_FILENAME = "searxng-settings.yml"
_STATE_VERSION = 2
_COMPOSE_PROJECT_PREFIX = "sourceweave-web-search-managed"
_PROBE_TIMEOUT_S = 2.0
_STARTUP_TIMEOUT_S = 120.0


class ManagedRuntimeError(RuntimeError):
    pass


@dataclass(slots=True)
class ServiceProbe:
    status: Literal["healthy", "missing", "incompatible"]
    detail: str = ""


@dataclass(slots=True)
class ProbeResult:
    searxng: ServiceProbe
    crawl4ai: ServiceProbe
    redis: ServiceProbe

    @property
    def all_healthy(self) -> bool:
        return all(service.status == "healthy" for service in self.services.values())

    @property
    def all_missing(self) -> bool:
        return all(service.status == "missing" for service in self.services.values())

    @property
    def services(self) -> dict[str, ServiceProbe]:
        return {
            "SearXNG": self.searxng,
            "Crawl4AI": self.crawl4ai,
            "Redis": self.redis,
        }

    def describe(self) -> str:
        return "; ".join(
            f"{name}: {service.status}{_format_probe_detail(service.detail)}"
            for name, service in self.services.items()
        )


@dataclass(slots=True)
class ManagedStack:
    allocated_ports: dict[str, int]
    valve_overrides: dict[str, str]


@dataclass(slots=True)
class ManagedRuntimeSession:
    mode: Literal["explicit", "reused", "managed"]
    valve_overrides: dict[str, str]
    state_dir: Path | None = None
    session_id: str | None = None

    def __enter__(self) -> "ManagedRuntimeSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if self.mode != "managed" or self.state_dir is None or self.session_id is None:
            return
        _release_managed_session(self.state_dir, self.session_id)


def default_state_dir() -> Path:
    return Path.home() / ".sourceweave-local" / "managed-runtime"


def explicit_runtime_endpoints_configured(
    env: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if env is None else env
    return any(key in source for key in _RUNTIME_ENV_KEYS)


def materialize_runtime_assets(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    asset_root = resources.files("sourceweave_web_search.managed_runtime_assets")
    for asset_name in (_COMPOSE_FILENAME, _SEARXNG_SETTINGS_FILENAME):
        target_path = state_dir / asset_name
        asset_text = asset_root.joinpath(asset_name).read_text(encoding="utf-8")
        if target_path.exists() and target_path.read_text(encoding="utf-8") == asset_text:
            continue
        target_path.write_text(asset_text, encoding="utf-8")


def compose_project_name(state_dir: Path) -> str:
    normalized = str(state_dir.expanduser().resolve())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{_COMPOSE_PROJECT_PREFIX}-{digest}"


def compose_command(state_dir: Path, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        compose_project_name(state_dir),
        "-f",
        str(state_dir / _COMPOSE_FILENAME),
        *args,
    ]


def resolve_managed_runtime(
    *,
    env: Mapping[str, str] | None = None,
    state_dir: Path | None = None,
    startup_timeout_s: float = _STARTUP_TIMEOUT_S,
) -> ManagedRuntimeSession:
    runtime_env = os.environ if env is None else env
    if explicit_runtime_endpoints_configured(runtime_env):
        return ManagedRuntimeSession(mode="explicit", valve_overrides={})

    runtime_state_dir = state_dir or default_state_dir()
    materialize_runtime_assets(runtime_state_dir)

    with _state_lock(runtime_state_dir):
        state = _cleanup_stale_sessions(_load_state(runtime_state_dir))
        discovered_stack = _discover_managed_stack(runtime_state_dir)
        state_stack = _stack_from_state(state)

        if discovered_stack is not None:
            probe = probe_runtime(discovered_stack.valve_overrides)
            if probe.all_healthy:
                _record_managed_stack(state, discovered_stack)
                session_id = _register_session(state)
                _write_state(runtime_state_dir, state)
                return ManagedRuntimeSession(
                    mode="managed",
                    valve_overrides=dict(discovered_stack.valve_overrides),
                    state_dir=runtime_state_dir,
                    session_id=session_id,
                )

        if state["sessions"]:
            recovery_stack = discovered_stack or state_stack
            if recovery_stack is None:
                raise ManagedRuntimeError(
                    "Managed runtime session state is missing the tracked service ports "
                    "for active local sessions. Stop the other SourceWeave MCP "
                    "processes or remove the managed runtime state directory before retrying."
                )
            return _start_managed_session(
                runtime_state_dir,
                state,
                recovery_stack,
                startup_timeout_s=startup_timeout_s,
            )

        if discovered_stack is not None:
            recovery_stack = _select_managed_stack(
                preferred_ports=discovered_stack.allocated_ports
            )
            return _start_managed_session(
                runtime_state_dir,
                state,
                recovery_stack,
                startup_timeout_s=startup_timeout_s,
            )

        canonical_probe = probe_canonical_runtime()
        if canonical_probe.all_healthy:
            if _state_has_managed_stack(state):
                _clear_managed_stack(state)
                _write_state(runtime_state_dir, state)
            return ManagedRuntimeSession(
                mode="reused",
                valve_overrides=dict(_CANONICAL_VALVE_OVERRIDES),
            )

        preferred_ports = None
        if state_stack is not None:
            preferred_ports = state_stack.allocated_ports
        recovery_stack = _select_managed_stack(preferred_ports=preferred_ports)
        return _start_managed_session(
            runtime_state_dir,
            state,
            recovery_stack,
            startup_timeout_s=startup_timeout_s,
        )

    raise AssertionError("unreachable managed runtime resolution state")


def probe_canonical_runtime(timeout_s: float = _PROBE_TIMEOUT_S) -> ProbeResult:
    return probe_runtime(_CANONICAL_VALVE_OVERRIDES, timeout_s=timeout_s)


def probe_runtime(
    valve_overrides: Mapping[str, str],
    *,
    timeout_s: float = _PROBE_TIMEOUT_S,
) -> ProbeResult:
    effective = dict(_CANONICAL_VALVE_OVERRIDES)
    effective.update(valve_overrides)
    return ProbeResult(
        searxng=_probe_searxng(effective["SEARXNG_BASE_URL"], timeout_s),
        crawl4ai=_probe_crawl4ai(effective["CRAWL4AI_BASE_URL"], timeout_s),
        redis=_probe_redis(effective["CACHE_REDIS_URL"], timeout_s),
    )


def _probe_searxng(base_url: str, timeout_s: float) -> ServiceProbe:
    parsed = urlparse(base_url)
    path = parsed.path or "/search"
    query = parsed.query.replace("<query>", quote_plus("sourceweave runtime probe"))
    request_path = f"{path}?{query}" if query else path
    return _probe_http_service(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 80,
        path=request_path,
        timeout_s=timeout_s,
        validator=_validate_searxng_response,
    )


def _probe_crawl4ai(base_url: str, timeout_s: float) -> ServiceProbe:
    parsed = urlparse(base_url)
    return _probe_http_service(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 80,
        path="/health",
        timeout_s=timeout_s,
    )


def _probe_redis(redis_url: str, timeout_s: float) -> ServiceProbe:
    parsed = urlparse(redis_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as connection:
            connection.settimeout(timeout_s)
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            response = connection.recv(32)
    except OSError as exc:
        status = "missing" if _is_missing_socket_error(exc) else "incompatible"
        return ServiceProbe(status=status, detail=str(exc))

    if response.startswith(b"+PONG"):
        return ServiceProbe(status="healthy")
    return ServiceProbe(
        status="incompatible",
        detail=f"unexpected ping response {response!r}",
    )


def _probe_http_service(
    *,
    host: str,
    port: int,
    path: str,
    timeout_s: float,
    validator: Callable[[bytes], None] | None = None,
) -> ServiceProbe:
    connection = HTTPConnection(host, port, timeout=timeout_s)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
    except OSError as exc:
        status = "missing" if _is_missing_socket_error(exc) else "incompatible"
        return ServiceProbe(status=status, detail=str(exc))
    except HTTPException as exc:
        return ServiceProbe(status="incompatible", detail=str(exc))
    finally:
        connection.close()

    if response.status != 200:
        return ServiceProbe(
            status="incompatible",
            detail=f"unexpected status {response.status}",
        )

    if validator is None:
        return ServiceProbe(status="healthy")

    try:
        validator(body)
    except ValueError as exc:
        return ServiceProbe(status="incompatible", detail=str(exc))
    return ServiceProbe(status="healthy")


def _validate_searxng_response(body: bytes) -> None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("response did not contain a results list")


def _start_managed_session(
    state_dir: Path,
    state: dict[str, Any],
    stack: ManagedStack,
    *,
    startup_timeout_s: float,
) -> ManagedRuntimeSession:
    _ensure_compose_available()
    _run_compose_command(
        state_dir,
        "up",
        "-d",
        timeout_s=startup_timeout_s,
        env_overrides=_compose_env(stack.allocated_ports),
    )
    _wait_for_healthy_stack(stack.valve_overrides, startup_timeout_s)
    _record_managed_stack(state, stack)
    session_id = _register_session(state)
    _write_state(state_dir, state)
    return ManagedRuntimeSession(
        mode="managed",
        valve_overrides=dict(stack.valve_overrides),
        state_dir=state_dir,
        session_id=session_id,
    )


def _wait_for_healthy_stack(
    valve_overrides: Mapping[str, str],
    timeout_s: float,
) -> None:
    deadline = time.time() + timeout_s
    last_probe: ProbeResult | None = None
    while time.time() < deadline:
        last_probe = probe_runtime(valve_overrides)
        if last_probe.all_healthy:
            return
        time.sleep(1.0)
    detail = last_probe.describe() if last_probe is not None else "no probe data"
    raise ManagedRuntimeError(
        "Timed out waiting for the managed local runtime to become healthy. "
        f"Effective endpoints: {_describe_endpoints(valve_overrides)}. "
        f"Last probe: {detail}. Set explicit SOURCEWEAVE_SEARCH_* endpoints "
        "to bypass managed mode if you want to use external services instead."
    )


def _ensure_compose_available() -> None:
    try:
        _run_docker_command("compose", "version", timeout_s=20.0)
    except ManagedRuntimeError as exc:
        raise ManagedRuntimeError(
            "Docker with Compose support is required for the managed local runtime. "
            "Install Docker Desktop or Docker Engine with the compose plugin, or set "
            "explicit SOURCEWEAVE_SEARCH_* endpoints to bypass managed mode. "
            f"Details: {exc}"
        ) from exc


def _discover_managed_stack(state_dir: Path) -> ManagedStack | None:
    project = compose_project_name(state_dir)
    try:
        result = _run_docker_command(
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
            timeout_s=20.0,
        )
    except ManagedRuntimeError:
        return None

    container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not container_ids:
        return None

    try:
        inspect_result = _run_docker_command(
            "inspect",
            *container_ids,
            timeout_s=20.0,
        )
    except ManagedRuntimeError:
        return None

    try:
        payload = json.loads(inspect_result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None

    ports: dict[str, int] = {}
    for container in payload:
        if not isinstance(container, dict):
            continue
        labels = container.get("Config", {}).get("Labels", {})
        if not isinstance(labels, dict):
            continue
        if labels.get("com.docker.compose.project") != project:
            continue
        service = labels.get("com.docker.compose.service")
        if service not in _SERVICE_CONTAINER_PORTS:
            continue
        host_port = _inspect_host_port(container, _SERVICE_CONTAINER_PORTS[service])
        if host_port is None:
            continue
        ports[service] = host_port

    if set(ports) != set(_SERVICE_CONTAINER_PORTS):
        return None
    return _stack_from_ports(ports)


def _inspect_host_port(container: Mapping[str, Any], port_key: str) -> int | None:
    network_bindings = container.get("NetworkSettings", {}).get("Ports", {})
    if isinstance(network_bindings, dict):
        bindings = network_bindings.get(port_key)
        if isinstance(bindings, list) and bindings:
            host_port = bindings[0].get("HostPort")
            if host_port is not None:
                try:
                    return int(host_port)
                except (TypeError, ValueError):
                    return None

    host_config_bindings = container.get("HostConfig", {}).get("PortBindings", {})
    if isinstance(host_config_bindings, dict):
        bindings = host_config_bindings.get(port_key)
        if isinstance(bindings, list) and bindings:
            host_port = bindings[0].get("HostPort")
            if host_port is not None:
                try:
                    return int(host_port)
                except (TypeError, ValueError):
                    return None
    return None


def _run_docker_command(
    *args: str,
    timeout_s: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_subprocess(
        ["docker", *args],
        timeout_s=timeout_s,
    )


def _run_compose_command(
    state_dir: Path,
    *args: str,
    timeout_s: float | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_subprocess(
        compose_command(state_dir, *args),
        cwd=state_dir,
        timeout_s=timeout_s,
        env_overrides=env_overrides,
    )


def _run_subprocess(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_s: float | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = None
    if env_overrides is not None:
        env = dict(os.environ)
        env.update({key: str(value) for key, value in env_overrides.items()})

    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ManagedRuntimeError(f"`{command[0]}` executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        timeout_label = (
            f"{timeout_s:.0f}s" if timeout_s is not None else "the configured timeout"
        )
        raise ManagedRuntimeError(
            f"`{' '.join(command)}` timed out after {timeout_label}"
        ) from exc

    if result.returncode == 0:
        return result

    output = (result.stderr or result.stdout or "").strip()
    detail = f": {output}" if output else ""
    raise ManagedRuntimeError(f"`{' '.join(command)}` failed{detail}")


def _release_managed_session(state_dir: Path, session_id: str) -> None:
    with _state_lock(state_dir):
        state = _cleanup_stale_sessions(_load_state(state_dir))
        state["sessions"] = [
            session
            for session in state["sessions"]
            if session.get("session_id") != session_id
        ]
        state["updated_at"] = time.time()
        if state["sessions"] or not state["stack_started_by_sourceweave"]:
            _write_state(state_dir, state)
            return

        state_stack = _stack_from_state(state)
        env_overrides = None
        if state_stack is not None:
            env_overrides = _compose_env(state_stack.allocated_ports)
        _run_compose_command(
            state_dir,
            "down",
            timeout_s=60.0,
            env_overrides=env_overrides,
        )
        _clear_managed_stack(state)
        _write_state(state_dir, state)


def _register_session(state: dict[str, Any]) -> str:
    session_id = uuid.uuid4().hex
    now = time.time()
    state["sessions"] = [
        session
        for session in state.get("sessions", [])
        if session.get("session_id") != session_id
    ]
    state["sessions"].append(
        {
            "session_id": session_id,
            "pid": os.getpid(),
            "started_at": now,
            "last_seen_at": now,
        }
    )
    state["updated_at"] = now
    return session_id


def _cleanup_stale_sessions(state: dict[str, Any]) -> dict[str, Any]:
    live_sessions = [
        session
        for session in state.get("sessions", [])
        if _session_is_alive(session)
    ]
    if len(live_sessions) != len(state.get("sessions", [])):
        state["sessions"] = live_sessions
        state["updated_at"] = time.time()
    return state


def _session_is_alive(session: Mapping[str, Any]) -> bool:
    try:
        pid = int(session.get("pid", -1))
    except (TypeError, ValueError):
        return False
    return _pid_is_alive(pid)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _state_path(state_dir: Path) -> Path:
    return state_dir / _STATE_FILENAME


def _load_state(state_dir: Path) -> dict[str, Any]:
    path = _state_path(state_dir)
    if not path.exists():
        return _default_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedRuntimeError(
            f"Managed runtime state file is unreadable: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ManagedRuntimeError(f"Managed runtime state file is invalid: {path}")
    sessions = payload.get("sessions", [])
    if not isinstance(sessions, list):
        raise ManagedRuntimeError(f"Managed runtime state file is invalid: {path}")
    managed_ports = payload.get("managed_ports", {})
    if not isinstance(managed_ports, dict):
        raise ManagedRuntimeError(f"Managed runtime state file is invalid: {path}")
    return {
        "version": int(payload.get("version", _STATE_VERSION)),
        "stack_started_by_sourceweave": bool(
            payload.get("stack_started_by_sourceweave", False)
        ),
        "managed_ports": managed_ports,
        "sessions": sessions,
        "updated_at": float(payload.get("updated_at", 0.0)),
    }


def _write_state(state_dir: Path, state: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(state_dir)
    path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _default_state() -> dict[str, Any]:
    return {
        "version": _STATE_VERSION,
        "stack_started_by_sourceweave": False,
        "managed_ports": {},
        "sessions": [],
        "updated_at": 0.0,
    }


def _state_has_managed_stack(state: Mapping[str, Any]) -> bool:
    return bool(
        state.get("stack_started_by_sourceweave")
        or state.get("managed_ports")
        or state.get("sessions")
    )


def _record_managed_stack(state: dict[str, Any], stack: ManagedStack) -> None:
    state["stack_started_by_sourceweave"] = True
    state["managed_ports"] = dict(stack.allocated_ports)
    state["updated_at"] = time.time()


def _clear_managed_stack(state: dict[str, Any]) -> None:
    state["stack_started_by_sourceweave"] = False
    state["managed_ports"] = {}
    state["updated_at"] = time.time()


def _stack_from_state(state: Mapping[str, Any]) -> ManagedStack | None:
    managed_ports = state.get("managed_ports", {})
    if not isinstance(managed_ports, Mapping) or not managed_ports:
        return None
    ports: dict[str, int] = {}
    for service in _SERVICE_CONTAINER_PORTS:
        raw_port = managed_ports.get(service)
        if raw_port is None:
            return None
        try:
            ports[service] = int(raw_port)
        except (TypeError, ValueError):
            return None
    return _stack_from_ports(ports)


def _stack_from_ports(ports: Mapping[str, int]) -> ManagedStack:
    allocated_ports = {service: int(ports[service]) for service in _SERVICE_CONTAINER_PORTS}
    return ManagedStack(
        allocated_ports=allocated_ports,
        valve_overrides={
            "SEARXNG_BASE_URL": _searxng_base_url_for_port(allocated_ports["searxng"]),
            "CRAWL4AI_BASE_URL": f"http://127.0.0.1:{allocated_ports['crawl4ai']}",
            "CACHE_REDIS_URL": _redis_url_for_port(allocated_ports["redis"]),
        },
    )


def _searxng_base_url_for_port(port: int) -> str:
    parsed = urlparse(_CANONICAL_SEARXNG_BASE_URL)
    path = parsed.path or "/search"
    query = parsed.query
    base = f"{parsed.scheme or 'http'}://127.0.0.1:{port}{path}"
    return f"{base}?{query}" if query else base


def _redis_url_for_port(port: int) -> str:
    parsed = urlparse(_CANONICAL_REDIS_URL)
    path = parsed.path or ""
    return f"{parsed.scheme or 'redis'}://127.0.0.1:{port}{path}"


def _select_managed_stack(
    *,
    preferred_ports: Mapping[str, int] | None = None,
) -> ManagedStack:
    allocated_ports: dict[str, int] = {}
    used_ports: set[int] = set()
    for service, canonical_port in _CANONICAL_SERVICE_PORTS.items():
        preferred_port = canonical_port
        if preferred_ports is not None and service in preferred_ports:
            candidate_port = preferred_ports[service]
            preferred_port = int(candidate_port)
        allocated_ports[service] = _select_host_port(preferred_port, used_ports)
        used_ports.add(allocated_ports[service])
    return _stack_from_ports(allocated_ports)


def _select_host_port(preferred_port: int, used_ports: set[int]) -> int:
    if preferred_port not in used_ports and _port_is_available(preferred_port):
        return preferred_port
    return _find_free_port(used_ports)


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _find_free_port(used_ports: set[int]) -> int:
    for _ in range(32):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(("127.0.0.1", 0))
            port = int(candidate.getsockname()[1])
        if port not in used_ports:
            return port
    raise ManagedRuntimeError(
        "Unable to allocate a free local port for the managed runtime. "
        "Set explicit SOURCEWEAVE_SEARCH_* endpoints to bypass managed mode."
    )


def _compose_env(ports: Mapping[str, int]) -> dict[str, str]:
    return {
        _PORT_ENV_KEYS[service]: str(int(port))
        for service, port in ports.items()
        if service in _PORT_ENV_KEYS
    }


def _describe_endpoints(valve_overrides: Mapping[str, str]) -> str:
    return ", ".join(
        [
            valve_overrides["SEARXNG_BASE_URL"],
            valve_overrides["CRAWL4AI_BASE_URL"],
            valve_overrides["CACHE_REDIS_URL"],
        ]
    )


def _is_missing_socket_error(exc: OSError) -> bool:
    if isinstance(exc, (ConnectionRefusedError, TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, socket.gaierror):
        return exc.errno == getattr(socket, "EAI_NONAME", None)
    return getattr(exc, "errno", None) in {
        errno.ECONNREFUSED,
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
    }


def _format_probe_detail(detail: str) -> str:
    if not detail:
        return ""
    return f" ({detail})"


class _StateLock:
    def __init__(self, state_dir: Path) -> None:
        self._lock_path = state_dir / _LOCK_FILENAME
        self._file: Any | None = None

    def __enter__(self) -> "_StateLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
        return False


def _state_lock(state_dir: Path) -> _StateLock:
    return _StateLock(state_dir)
