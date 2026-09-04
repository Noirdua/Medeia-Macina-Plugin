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
    if not isinstance(config, dict):
        return {}
    plugin = config.get("plugin")
    if not isinstance(plugin, dict):
        plugin = {}
    for key in _CONFIG_KEYS:
        block = plugin.get(key)
        if isinstance(block, dict) and block:
            return block
    try:
        from SYS.config import get_plugin_block
    except Exception:
        return {}
    for key in _CONFIG_KEYS:
        try:
            block = get_plugin_block(config, key)
        except Exception:
            block = {}
        if isinstance(block, dict) and block:
            return block
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
