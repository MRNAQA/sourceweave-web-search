import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = os.environ.get("OPENWEBUI_BASE_URL", "http://openwebui:8080").rstrip("/")
TOOL_ID = os.environ.get("OPENWEBUI_TOOL_ID", "web_research_tool")
TOOL_NAME = os.environ.get("OPENWEBUI_TOOL_NAME", "Web Research Studio")
FUNCTION_PATH = Path("/app/web_research_tool.py")
ADMIN_EMAIL = os.environ.get("OPENWEBUI_ADMIN_EMAIL", "admin@localhost")
ADMIN_PASSWORD = os.environ.get("OPENWEBUI_ADMIN_PASSWORD", "admin")


def request_json(
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict | list | str]:
    body = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload).encode()
    request = Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, raw
    except URLError as exc:
        raise RuntimeError(f"Request to {path} failed: {exc}") from exc


def wait_for_openwebui() -> None:
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            status, _ = request_json("GET", "/health")
            if status == 200:
                return
        except RuntimeError:
            pass
        time.sleep(2)
    raise RuntimeError("OpenWebUI did not become ready in time")


def authenticate() -> str:
    status, body = request_json(
        "POST",
        "/api/v1/auths/signin",
        {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    if status != 200 or not isinstance(body, dict) or not body.get("token"):
        raise RuntimeError(f"Failed to authenticate bootstrap admin: {status} {body}")
    return body["token"]


def ensure_tool(token: str) -> None:
    content = FUNCTION_PATH.read_text()
    payload = {
        "id": TOOL_ID,
        "name": TOOL_NAME,
        "content": content,
        "meta": {},
        "access_grants": [],
    }

    status, existing = request_json("GET", f"/api/v1/tools/id/{TOOL_ID}", token=token)
    if status == 200:
        status, body = request_json(
            "POST",
            f"/api/v1/tools/id/{TOOL_ID}/update",
            payload,
            token,
        )
        if status != 200:
            raise RuntimeError(f"Failed to update tool: {status} {body}")
    elif status == 404:
        status, body = request_json("POST", "/api/v1/tools/create", payload, token)
        if status != 200:
            raise RuntimeError(f"Failed to create tool: {status} {body}")
    else:
        raise RuntimeError(f"Unexpected tool lookup response: {status} {existing}")

    status, body = request_json("GET", f"/api/v1/tools/id/{TOOL_ID}", token=token)
    if status != 200:
        raise RuntimeError(f"Failed to re-read tool after upsert: {status} {body}")


def main() -> None:
    wait_for_openwebui()
    token = authenticate()
    ensure_tool(token)
    print(f"Registered OpenWebUI tool '{TOOL_ID}'")


if __name__ == "__main__":
    main()
