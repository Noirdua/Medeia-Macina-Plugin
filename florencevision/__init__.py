"""FlorenceVision support module under the plugin namespace."""

from __future__ import annotations

__all__ = [
    "FlorenceVisionTool",
    "FlorenceVisionDefaults",
    "config_schema",
]

_MODULE_ATTRS = {
    "FlorenceVisionTool": ".runtime",
    "FlorenceVisionDefaults": ".runtime",
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