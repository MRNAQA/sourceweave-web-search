import importlib.util
import sys
from pathlib import Path


def _load_release_metadata_main() -> object:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "sourceweave_web_search"
        / "release_metadata.py"
    )
    spec = importlib.util.spec_from_file_location(
        "sourceweave_release_metadata", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load release metadata module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


main = _load_release_metadata_main()


if __name__ == "__main__":
    raise SystemExit(main())
