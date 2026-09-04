"""Playwright plugin: browser automation and file -screenshot."""

from __future__ import annotations

from typing import Any, Dict, List

from PluginCore.base import Plugin

from .runtime import USER_AGENT

__all__ = [
    "USER_AGENT",
    "PlaywrightTimeoutError",
    "PlaywrightTool",
    "PlaywrightDefaults",
    "PlaywrightDownloadResult",
    "config_schema",
    "Playwright",
]

_MODULE_ATTRS = {
    "PlaywrightTimeoutError": ".runtime",
    "PlaywrightTool": ".runtime",
    "PlaywrightDefaults": ".runtime",
    "PlaywrightDownloadResult": ".runtime",
    "config_schema": ".runtime",
}


def __getattr__(name: str) -> object:
    submod = _MODULE_ATTRS.get(name)
    if submod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    mod = import_module(submod, package=__name__)
    obj = getattr(mod, name)
    globals()[name] = obj
    return obj


class Playwright(Plugin):
    PLUGIN_NAME = "playwright"
    SUPPORTED_CMDLETS = frozenset({"screen-shot"})
    FILE_ACTIONS: Dict[str, Dict[str, Any]] = {
        "screenshot": {
            "flags": (
                "-screenshot",
                "--screenshot",
                "-screen-shot",
                "--screen-shot",
                "-shot",
                "--shot",
            ),
            "module": "plugins.playwright.screenshot",
            "cmdlet": "screen-shot",
            "description": "Capture a screenshot",
            "alias": "shot",
        }
    }

    def validate(self) -> bool:
        return True

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        from .runtime import config_schema as runtime_schema

        return list(runtime_schema() or [])
