from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

_CONFIG_KEYS = (
    "archive.org",
    "archiveorg",
    "openlibrary",
    "internetarchive",
    "ia",
)


def plugin_config_entry(config: Any) -> Dict[str, Any]:
    from PluginCore.base import plugin_config_block

    block = plugin_config_block(config, *_CONFIG_KEYS)
    if block:
        return block
    try:
        from SYS.config import get_plugin_block
    except Exception:
        return {}
    for key in _CONFIG_KEYS:
        try:
            found = get_plugin_block(config, key)
        except Exception:
            found = {}
        if isinstance(found, dict) and found:
            return found
    return {}


def archive_credentials(config: Any) -> Tuple[Optional[str], Optional[str]]:
    entry = plugin_config_entry(config)
    email = (
        entry.get("email")
        or entry.get("username")
        or entry.get("user")
    )
    password = entry.get("password")
    email_text = str(email).strip() if email else ""
    password_text = str(password).strip() if password else ""
    if email_text and password_text:
        return email_text, password_text
    return None, None
