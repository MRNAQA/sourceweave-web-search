import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_research_studio.build_openwebui import canonical_tool_path, default_output_path
from web_research_studio.mcp_server import build_mcp_server


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_openwebui_artifact_is_in_sync() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/build_openwebui_tool.py", "--check"],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert default_output_path().read_text(
        encoding="utf-8"
    ) == canonical_tool_path().read_text(encoding="utf-8")


def test_cli_module_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "web_research_studio.cli", "--help"],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "--read-first-pages" in result.stdout, result.stdout


def test_mcp_server_exposes_expected_tools() -> None:
    async def scenario() -> None:
        server = build_mcp_server()
        tool_names = sorted(tool.name for tool in await server.list_tools())
        assert tool_names == ["read_page", "search_and_crawl"], tool_names

    asyncio.run(scenario())
