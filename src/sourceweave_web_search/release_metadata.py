from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path
from typing import Sequence


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def pyproject_path() -> Path:
    return repo_root() / "pyproject.toml"


def tool_path() -> Path:
    return Path(__file__).resolve().with_name("tool.py")


def server_json_path() -> Path:
    return repo_root() / "server.json"


def dockerfile_path() -> Path:
    return repo_root() / "Dockerfile"


def project_version() -> str:
    data = tomllib.loads(pyproject_path().read_text(encoding="utf-8"))
    return data["project"]["version"]


def _sync_tool_header(version: str, check: bool) -> bool:
    path = tool_path()
    original = path.read_text(encoding="utf-8")
    updated, replacements = re.subn(
        r"(?m)^version:\s*.+$",
        f"version: {version}",
        original,
        count=1,
    )
    if replacements != 1:
        raise RuntimeError(f"Could not find a single version header in {path}")

    if check:
        return original == updated

    if original != updated:
        path.write_text(updated, encoding="utf-8")
    return True


def _sync_server_json(version: str, check: bool) -> bool:
    path = server_json_path()
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)

    data["version"] = version
    for package in data.get("packages", []):
        registry_type = package.get("registryType")
        if registry_type == "pypi":
            package["version"] = version
        elif registry_type == "oci":
            identifier = package.get("identifier", "")
            package["identifier"] = re.sub(r":[^:]+$", f":{version}", identifier)

    updated = json.dumps(data, indent=2) + "\n"

    if check:
        return original == updated

    if original != updated:
        path.write_text(updated, encoding="utf-8")
    return True


def _sync_dockerfile_labels(version: str, check: bool) -> bool:
    path = dockerfile_path()
    original = path.read_text(encoding="utf-8")
    updated, replacements = re.subn(
        r'org\.opencontainers\.image\.version="[^"]+"',
        f'org.opencontainers.image.version="{version}"',
        original,
        count=1,
    )
    if replacements != 1:
        raise RuntimeError(f"Could not find a single OCI version label in {path}")

    if check:
        return original == updated

    if original != updated:
        path.write_text(updated, encoding="utf-8")
    return True


def sync_release_metadata(check: bool = False) -> bool:
    version = project_version()
    checks = [
        _sync_tool_header(version, check=check),
        _sync_server_json(version, check=check),
        _sync_dockerfile_labels(version, check=check),
    ]
    return all(checks)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync release metadata files from pyproject.toml version."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if release metadata files are out of sync with pyproject.toml.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    in_sync = sync_release_metadata(check=args.check)

    if args.check:
        if not in_sync:
            print("Release metadata is out of sync with pyproject.toml")
            return 1
        print("Release metadata is in sync with pyproject.toml")
        return 0

    print(f"Synced release metadata to version {project_version()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
