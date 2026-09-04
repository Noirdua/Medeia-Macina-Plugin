"""Playwright support module under the plugin namespace.

This package provides shared browser automation defaults/helpers for cmdlets and
plugins. It is intentionally lightweight at import time so plugin discovery can
import `plugins.playwright` even when Playwright itself is not installed.
"""

from __future__ import annotations

from .runtime import USER_AGENT

__all__ = [
    "USER_AGENT",
    "PlaywrightTimeoutError",
    "PlaywrightTool",
    "PlaywrightDefaults",
    "PlaywrightDownloadResult",
    "config_schema",
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