from __future__ import annotations

import posixpath
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote, urlparse

from SYS.utils import coerce_bool, coerce_int, unique_path


def format_epoch(raw_value: Any) -> str:
    try:
        stamp = int(raw_value)
    except Exception:
        return ""
    try:
        return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(raw_value or "")


def safe_filename(name: Any) -> str:
    raw = str(name or "").strip()
    if not raw:
        raw = "download"
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", ".", " "} else "_" for ch in raw)
    cleaned = cleaned.strip(" ._")
    return cleaned or "download"


def normalize_remote_path(value: Any, *, default: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        text = default
    elif text.startswith(("scp://", "sftp://")):
        try:
            text = unquote(urlparse(text).path or "/")
        except Exception:
            text = default
    elif not text.startswith("/"):
        text = posixpath.join(default, text)

    normalized = posixpath.normpath(text)
    normalized = "/" + normalized.lstrip("/")
    return normalized or "/"


def join_remote_path(parent: Any, child: Any, base_path: str) -> str:
    left = normalize_remote_path(parent, default=base_path)
    right = str(child or "").strip().replace("\\", "/")
    if not right:
        return left
    return normalize_remote_path(posixpath.join(left, right), default="/")


def build_url(
    normalized_path: str,
    default_host: str,
    default_port: int,
    *,
    settings: Optional[Dict[str, Any]] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    scheme: str = "scp",
) -> str:
    resolved = dict(settings or {})
    host_text = str(host or resolved.get("host") or default_host).strip()
    port_value = int(port or resolved.get("port") or default_port)
    port_suffix = f":{port_value}" if port_value and port_value != 22 else ""
    _qsafe = "/-._~!$&'()*+,;=:@"
    return f"{scheme}://{host_text}{port_suffix}{quote(normalized_path, safe=_qsafe)}"


def connection_settings_for_url(
    url: str,
    settings: Dict[str, Any],
    default_host: str,
    default_port: int,
    default_username: str,
    default_password: str,
    default_key_path: str,
    default_allow_agent: bool,
    default_look_for_keys: bool,
    default_timeout: int,
    default_base_path: str,
) -> Dict[str, Any]:
    parsed = urlparse(str(url or "").strip())
    scheme = (parsed.scheme or "scp").strip().lower()
    host = parsed.hostname or settings.get("host") or default_host
    port = parsed.port or settings.get("port") or default_port
    username = parsed.username or settings.get("username") or default_username
    password = parsed.password or settings.get("password") or default_password
    path_text = normalize_remote_path(unquote(parsed.path or "/"), default=str(settings.get("base_path") or "/"))
    return {
        "instance": settings.get("instance"),
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "key_path": settings.get("key_path") or default_key_path,
        "allow_agent": settings.get("allow_agent", default_allow_agent),
        "look_for_keys": settings.get("look_for_keys", default_look_for_keys),
        "path": path_text,
        "timeout": settings.get("timeout", default_timeout),
        "base_path": settings.get("base_path", default_base_path),
    }


def item_metadata(
    item: Any,
    pipe_obj: Any,
    base_path: str,
    default_host: str,
    default_port: int,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for source in (item, pipe_obj):
        if isinstance(source, dict):
            for key in ("title", "path", "url"):
                if source.get(key) is not None and key not in metadata:
                    metadata[key] = source.get(key)
            nested = source.get("full_metadata") or source.get("metadata")
            if isinstance(nested, dict):
                metadata.update(nested)
        elif source is not None:
            for attr in ("title", "path", "url"):
                try:
                    value = getattr(source, attr, None)
                except Exception:
                    value = None
                if value is not None and attr not in metadata:
                    metadata[attr] = value
            try:
                nested = getattr(source, "full_metadata", None) or getattr(source, "metadata", None)
            except Exception:
                nested = None
            if isinstance(nested, dict):
                metadata.update(nested)

    scp_path = metadata.get("scp_path") or metadata.get("selection_path")
    if not scp_path:
        path_value = metadata.get("path") or metadata.get("url") or metadata.get("scp_url")
        path_text = str(path_value or "").strip()
        if path_text.startswith(("scp://", "sftp://")):
            scp_path = normalize_remote_path(path_text, default=base_path)
    if scp_path:
        base = str(metadata.get("base_path") or base_path)
        metadata["scp_path"] = normalize_remote_path(scp_path, default=base)
        metadata.setdefault("selection_path", metadata["scp_path"])

    if metadata.get("scp_path") and not metadata.get("scp_url"):
        metadata["scp_url"] = build_url(
            metadata["scp_path"],
            default_host,
            default_port,
            settings={
                "host": metadata.get("host") or default_host,
                "instance": metadata.get("instance"),
            },
        )
    if metadata.get("scp_url") and not metadata.get("selection_url"):
        metadata["selection_url"] = metadata["scp_url"]

    is_dir = metadata.get("is_dir")
    if is_dir is None and metadata.get("media_kind"):
        is_dir = str(metadata.get("media_kind") or "").strip().lower() == "folder"
    metadata["is_dir"] = bool(is_dir)
    return metadata
