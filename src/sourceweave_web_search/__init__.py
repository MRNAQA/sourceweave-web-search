from typing import Any

__all__ = ["Tools", "main"]


def __getattr__(name: str) -> Any:
    if name == "Tools":
        from sourceweave_web_search.tool import Tools

        return Tools
    if name == "main":
        from sourceweave_web_search.cli import main

        return main
    raise AttributeError(name)
