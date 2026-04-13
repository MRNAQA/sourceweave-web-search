import asyncio
import shutil
import subprocess
import sys
import tarfile
import zipfile
from email import message_from_bytes
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sourceweave_web_search.build_openwebui import (
    canonical_tool_path,
    default_output_path,
)
from sourceweave_web_search.mcp_server import build_mcp_server
from sourceweave_web_search.release_metadata import project_version, server_json_path
from sourceweave_web_search.tool import Tools


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _build_distributions() -> tuple[Path, Path]:
    out_dir = _repo_root() / "dist" / "packaging-tests"
    if out_dir.exists():
        shutil.rmtree(out_dir)

    result = subprocess.run(
        [
            "uv",
            "build",
            "--no-sources",
            "--sdist",
            "--wheel",
            "--out-dir",
            str(out_dir),
            "--clear",
            "--no-create-gitignore",
        ],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    return next(out_dir.glob("*.whl")), next(out_dir.glob("*.tar.gz"))


def test_openwebui_artifact_is_in_sync() -> None:
    artifact_path = default_output_path()
    artifact_before = artifact_path.read_text(encoding="utf-8")
    mtime_before = artifact_path.stat().st_mtime_ns

    result = subprocess.run(
        [sys.executable, "scripts/build_openwebui_tool.py", "--check"],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "in sync" in result.stdout.lower(), result.stdout
    assert artifact_path.read_text(encoding="utf-8") == artifact_before
    assert artifact_path.stat().st_mtime_ns == mtime_before
    assert artifact_before == canonical_tool_path().read_text(encoding="utf-8")


def test_release_metadata_is_in_sync() -> None:
    version = project_version()
    tool_source = canonical_tool_path().read_text(encoding="utf-8")
    server_json = server_json_path().read_text(encoding="utf-8")
    dockerfile = _repo_root().joinpath("Dockerfile").read_text(encoding="utf-8")

    assert f'version = "{version}"' in _repo_root().joinpath(
        "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert f"version: {version}" in tool_source
    assert f'"version": "{version}"' in server_json
    assert '"registryType": "pypi"' in server_json
    assert '"identifier": "sourceweave-web-search"' in server_json
    assert 'org.opencontainers.image.title="sourceweave-web-search-mcp"' in dockerfile
    assert f'org.opencontainers.image.version="{version}"' in dockerfile
    assert (
        'io.modelcontextprotocol.server.name="io.github.MRNAQA/sourceweave-web-search"'
        in dockerfile
    )


def test_openwebui_build_check_reports_drift_and_recovers(tmp_path: Path) -> None:
    artifact_path = tmp_path / "sourceweave_web_search.py"

    missing_check = subprocess.run(
        [
            sys.executable,
            "-m",
            "sourceweave_web_search.build_openwebui",
            "--check",
            "--output",
            str(artifact_path),
        ],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_check.returncode == 1, missing_check.stderr or missing_check.stdout

    build_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sourceweave_web_search.build_openwebui",
            "--output",
            str(artifact_path),
        ],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert build_result.returncode == 0, build_result.stderr or build_result.stdout

    in_sync_check = subprocess.run(
        [
            sys.executable,
            "-m",
            "sourceweave_web_search.build_openwebui",
            "--check",
            "--output",
            str(artifact_path),
        ],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert in_sync_check.returncode == 0, in_sync_check.stderr or in_sync_check.stdout
    assert artifact_path.read_text(encoding="utf-8") == canonical_tool_path().read_text(
        encoding="utf-8"
    )


def test_built_distributions_ship_publishable_metadata() -> None:
    wheel_path, sdist_path = _build_distributions()

    with zipfile.ZipFile(wheel_path) as wheel_archive:
        wheel_names = set(wheel_archive.namelist())
        metadata_path = next(
            name for name in wheel_names if name.endswith(".dist-info/METADATA")
        )
        metadata = message_from_bytes(wheel_archive.read(metadata_path))

    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_names), (
        wheel_names
    )
    assert metadata["Name"] == "sourceweave-web-search"
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert metadata["Requires-Python"] == ">=3.12"
    assert metadata["License-Expression"] == "MIT"
    assert metadata.get_all("License-File") == ["LICENSE"]
    assert "crawl4ai" in (metadata.get("Keywords") or "")
    project_urls = metadata.get_all("Project-URL") or []

    classifiers = metadata.get_all("Classifier") or []
    assert "License :: OSI Approved :: MIT License" in classifiers
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert "Homepage, https://github.com/MRNAQA/sourceweave-web-search" in project_urls
    assert (
        "Repository, https://github.com/MRNAQA/sourceweave-web-search" in project_urls
    )
    assert (
        "Issues, https://github.com/MRNAQA/sourceweave-web-search/issues"
        in project_urls
    )

    for repo_only_prefix in (
        "artifacts/",
        "infrastructure/",
        "scripts/",
        "skills/",
        "tests/",
    ):
        assert not any(name.startswith(repo_only_prefix) for name in wheel_names), (
            wheel_names
        )
    assert "docker-compose.yml" not in wheel_names, wheel_names

    sdist_root = sdist_path.name.removesuffix(".tar.gz")
    with tarfile.open(sdist_path) as sdist_archive:
        sdist_names = set(sdist_archive.getnames())

    assert f"{sdist_root}/README.md" in sdist_names, sdist_names
    assert f"{sdist_root}/LICENSE" in sdist_names, sdist_names


def test_cli_module_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sourceweave_web_search.cli", "--help"],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "--read-first-pages" in result.stdout, result.stdout


def test_mcp_module_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sourceweave_web_search.mcp_server", "--help"],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "--transport" in result.stdout, result.stdout
    assert "--host" in result.stdout, result.stdout
    assert "--port" in result.stdout, result.stdout


def test_mcp_server_exposes_expected_tools() -> None:
    async def scenario() -> None:
        server = build_mcp_server()
        tool_names = sorted(tool.name for tool in await server.list_tools())
        assert tool_names == ["read_pages", "search_web"], tool_names

    asyncio.run(scenario())


def test_default_tool_endpoints_target_host_ports() -> None:
    valves = Tools.Valves()

    assert (
        valves.SEARXNG_BASE_URL == "http://127.0.0.1:19080/search?format=json&q=<query>"
    )
    assert valves.CRAWL4AI_BASE_URL == "http://127.0.0.1:19235"
    assert valves.CACHE_REDIS_URL == "redis://127.0.0.1:16379/2"
