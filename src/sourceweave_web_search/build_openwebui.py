import argparse
from pathlib import Path
from typing import Sequence


def canonical_tool_path() -> Path:
    return Path(__file__).resolve().with_name("tool.py")


def default_output_path() -> Path:
    return canonical_tool_path().parents[2] / "artifacts" / "sourceweave_web_search.py"


def build_openwebui_artifact(
    output_path: Path | None = None, check: bool = False
) -> bool:
    source = canonical_tool_path().read_text(encoding="utf-8")
    target = output_path or default_output_path()

    if check:
        return target.exists() and target.read_text(encoding="utf-8") == source

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build or validate the standalone OpenWebUI tool artifact."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if artifacts/sourceweave_web_search.py is out of sync with src/sourceweave_web_search/tool.py.",
    )
    parser.add_argument(
        "--output",
        default=str(default_output_path()),
        help="Output path for the generated OpenWebUI tool file.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output_path = Path(args.output).resolve()
    in_sync = build_openwebui_artifact(output_path=output_path, check=args.check)

    if args.check:
        if not in_sync:
            print(
                f"OpenWebUI artifact is out of sync: {output_path} != {canonical_tool_path()}"
            )
            return 1
        print(f"OpenWebUI artifact is in sync: {output_path}")
        return 0

    print(f"Wrote OpenWebUI artifact to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
