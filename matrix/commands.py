from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
import sys
import tempfile
import re
import uuid
from urllib.parse import parse_qs, urlparse

from SYS.cmdlet_spec import Cmdlet, CmdletArg
from SYS.config import load_config, save_config
from SYS.logger import log, debug
from SYS.result_table import Table
from SYS.item_accessors import get_sha256_hex
from SYS.utils import extract_hydrus_hash_from_url
from SYS.command_parsing import (
    extract_arg_value,
    extract_piped_value as _extract_piped_value,
    extract_value_arg as _extract_value_arg,
    has_flag as _has_flag,
    normalize_to_list as _normalize_to_list,
)
from SYS import pipeline as ctx
from PluginCore.registry import get_plugin, get_plugin_for_url

_MATRIX_PENDING_ITEMS_KEY = "matrix_pending_items"
_MATRIX_PENDING_TEXT_KEY = "matrix_pending_text"
_MATRIX_MENU_STATE_KEY = "matrix_menu_state"
_MATRIX_SELECTED_SETTING_KEY_KEY = "matrix_selected_setting_key"


def _get_matrix_provider(config: Dict[str, Any]) -> Any:
    provider = get_plugin("matrix", config)
    if provider is None:
        raise RuntimeError("Matrix plugin is not registered")
    return provider


def _resolve_plugin_url(url: str, config: Dict[str, Any]) -> str:
    target = str(url or "").strip()
    if not target:
        return target

    provider = get_plugin_for_url(target, config)
    if provider is None:
        return target

    try:
        resolved = provider.resolve_url(target)
    except Exception:
        return target
    return str(resolved or target)


def _extract_set_value_arg(args: Sequence[str]) -> Optional[str]:
    """Extract the value from -set-value flag."""
    return extract_arg_value(args, flags={"-set-value"})


def _get_matrix_config_block(config: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    try:
        provider = _get_matrix_provider(config)
        _name, cfg = provider.resolve_plugin_instance(None)
        if isinstance(cfg, dict) and cfg:
            return cfg
    except Exception:
        pass
    plugins = config.get("plugin")
    if not isinstance(plugins, dict):
        return {}
    matrix_cfg = plugins.get("matrix")
    if not isinstance(matrix_cfg, dict):
        return {}
    if matrix_cfg.get("homeserver") or matrix_cfg.get("access_token"):
        return matrix_cfg
    for value in matrix_cfg.values():
        if isinstance(value, dict) and (value.get("homeserver") or value.get("access_token")):
            return value
    return matrix_cfg


def _ensure_matrix_config_block(config: Dict[str, Any]) -> Dict[str, Any]:
    plugins = config.setdefault("plugin", {})
    if not isinstance(plugins, dict):
        plugins = {}
        config["plugin"] = plugins
    matrix_cfg = plugins.setdefault("matrix", {})
    if not isinstance(matrix_cfg, dict):
        matrix_cfg = {}
        plugins["matrix"] = matrix_cfg
    return matrix_cfg


def _update_matrix_config(config: Dict[str, Any], key: str, value: Any) -> bool:
    """Update the Matrix plugin block in the shared config.

    This method writes to the unified config store so changes persist between
    sessions.
    """
    try:
        if not isinstance(config, dict):
            return False

        value_str = str(value)

        current_cfg = load_config() or {}
        matrix_cfg = _ensure_matrix_config_block(current_cfg)
        matrix_cfg[key] = value_str

        save_config(current_cfg)

        # Keep the supplied config dict in sync for the running CLI
        target_matrix = _ensure_matrix_config_block(config)
        target_matrix[key] = value_str
        return True
    except Exception as exc:
        debug(f"[matrix] Failed to update Matrix config: {exc}")
        return False


def _parse_config_room_filter_ids(config: Dict[str, Any]) -> List[str]:
    try:
        matrix_conf = _get_matrix_config_block(config)
        if not matrix_conf:
            return []
        raw = None
        # Support a few common spellings; `room` is the documented key.
        for key in ("room", "room_id", "rooms", "room_ids"):
            if key in matrix_conf:
                raw = matrix_conf.get(key)
                break
        if raw is None:
            return []

        # Allow either a string or a list-like value.
        if isinstance(raw, (list, tuple, set)):
            items = [str(v).strip() for v in raw if str(v).strip()]
            return items

        text = str(raw or "").strip()
        if not text:
            return []
        # Comma-separated list of room IDs, but be tolerant of whitespace/newlines.
        items = [p.strip() for p in re.split(r"[,\s]+", text) if p and p.strip()]
        return items
    except Exception:
        return []


def _get_matrix_size_limit_bytes(config: Dict[str, Any]) -> Optional[int]:
    """Return max allowed per-file size in bytes for Matrix uploads.

    Config: [plugin=matrix] size_limit=50   # MB
    """
    try:
        matrix_conf = _get_matrix_config_block(config)
        if not matrix_conf:
            return None

        raw = None
        for key in ("size_limit", "size_limit_mb", "max_mb"):
            if key in matrix_conf:
                raw = matrix_conf.get(key)
                break
        if raw is None:
            return None

        mb: Optional[float] = None
        if isinstance(raw, (int, float)):
            mb = float(raw)
        else:
            text = str(raw or "").strip().lower()
            if not text:
                return None
            m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(mb|mib|m)?", text)
            if not m:
                return None
            mb = float(m.group(1))

        if mb is None or mb <= 0:
            return None

        # Use MiB semantics for predictable limits.
        return int(mb * 1024 * 1024)
    except Exception:
        return None


def _room_id_matches_filter(room_id: str, allowed_ids_canon: set[str]) -> bool:
    rid = str(room_id or "").strip()
    if not rid or not allowed_ids_canon:
        return False

    rid_canon = rid.casefold()
    if rid_canon in allowed_ids_canon:
        return True

    # Allow matching when config omits the homeserver part: "!abc" matches "!abc:server".
    base = rid.split(":", 1)[0].strip().casefold()
    return bool(base) and base in allowed_ids_canon


def _extract_room_arg(args: Sequence[str]) -> Optional[str]:
    """Extract the `-room <value>` argument from a cmdnat args list."""
    if not args:
        return None
    try:
        tokens = list(args)
    except Exception:
        return None
    for i, tok in enumerate(tokens):
        try:
            low = str(tok or "").strip()
            if not low:
                continue
            low_lower = low.lower()
            if low_lower in ("-room", "--room") and i + 1 < len(tokens):
                return str(tokens[i + 1] or "").strip()
            if low_lower.startswith("-room=") or low_lower.startswith("--room="):
                parts = low.split("=", 1)
                if len(parts) > 1:
                    return parts[1].strip()
        except Exception:
            continue
    return None


def _resolve_room_identifier(value: str, config: Dict[str, Any]) -> Optional[str]:
    """Resolve a user-provided room identifier (display name or id) to a Matrix room_id.

    Returns the canonical room_id string on success, otherwise None.
    """
    try:
        cand = str(value or "").strip()
        if not cand:
            return None

        # If looks like an id already (starts with '!'), accept it as-is
        if cand.startswith("!"):
            return cand

        # First try to resolve against configured default rooms (fast, local)
        conf_ids = _parse_config_room_filter_ids(config)
        if conf_ids:
            # Attempt to fetch names for the configured IDs
            block = _get_matrix_config_block(config)
            if block and block.get("homeserver") and block.get("access_token"):
                try:
                    m = _get_matrix_provider(config)
                    rooms = m.list_rooms(room_ids=conf_ids)
                    for room in rooms or []:
                        name = str(room.get("name") or "").strip()
                        rid = str(room.get("room_id") or "").strip()
                        if name and name.lower() == cand.lower():
                            return rid
                        if name and cand.lower() in name.lower():
                            return rid
                except Exception:
                    pass

        # Last resort: attempt to ask the server for matching rooms (if possible)
        block = _get_matrix_config_block(config)
        if block and block.get("homeserver") and block.get("access_token"):
            try:
                m = _get_matrix_provider(config)
                rooms = m.list_rooms()
                for room in rooms or []:
                    name = str(room.get("name") or "").strip()
                    rid = str(room.get("room_id") or "").strip()
                    if name and name.lower() == cand.lower():
                        return rid
                    if name and cand.lower() in name.lower():
                        return rid
            except Exception:
                pass

        return None
    except Exception:
        return None


def _send_pending_to_rooms(config: Dict[str, Any], room_ids: List[str], args: Sequence[str]) -> int:
    """Send currently pending items to the specified list of room_ids.

    This function mirrors the existing -send behavior but accepts explicit
    room_ids so the same logic can be reused for -room direct invocation.
    """
    pending_items = ctx.load_value(_MATRIX_PENDING_ITEMS_KEY, default=[])
    items = _normalize_to_list(pending_items)
    if not items:
        log("No pending items to upload (use: @N | .matrix)", file=sys.stderr)
        return 1

    try:
        provider = _get_matrix_provider(config)
    except Exception as exc:
        log(f"Matrix not available: {exc}", file=sys.stderr)
        return 1

    text_value = _extract_text_arg(args)
    if not text_value:
        try:
            text_value = str(ctx.load_value(_MATRIX_PENDING_TEXT_KEY, default="") or "").strip()
        except Exception:
            text_value = ""

    size_limit_bytes = _get_matrix_size_limit_bytes(config)
    size_limit_mb = (size_limit_bytes / (1024 * 1024)) if size_limit_bytes else None

    # Resolve upload paths once
    upload_jobs: List[Dict[str, Any]] = []
    any_failed = False
    for item in items:
        file_path = _resolve_upload_path(item, config)
        if not file_path:
            any_failed = True
            log("Matrix upload requires a local file (path) or a direct URL on the selected item", file=sys.stderr)
            continue

        media_path = Path(file_path)
        if not media_path.exists():
            any_failed = True
            log(f"Matrix upload file missing: {file_path}", file=sys.stderr)
            continue

        if size_limit_bytes is not None:
            try:
                byte_size = int(media_path.stat().st_size)
            except Exception:
                byte_size = -1
            if byte_size >= 0 and byte_size > size_limit_bytes:
                any_failed = True
                actual_mb = byte_size / (1024 * 1024)
                lim = float(size_limit_mb or 0)
                log(f"ERROR: file is too big, skipping: {media_path.name} ({actual_mb:.1f} MB > {lim:.1f} MB)", file=sys.stderr)
                continue

        upload_jobs.append({
            "path": str(media_path),
            "pipe_obj": item
        })

    for rid in room_ids:
        sent_any_for_room = False
        for job in upload_jobs:
            file_path = str(job.get("path") or "")
            if not file_path:
                continue
            try:
                link = provider.upload_to_room(file_path, rid, pipe_obj=job.get("pipe_obj"))
                debug(f"✓ Sent {Path(file_path).name} -> {rid}")
                if link:
                    log(link)
                sent_any_for_room = True
            except Exception as exc:
                any_failed = True
                log(f"Matrix send failed for {Path(file_path).name}: {exc}", file=sys.stderr)

        if text_value and sent_any_for_room:
            try:
                provider.send_text_to_room(text_value, rid)
            except Exception as exc:
                any_failed = True
                log(f"Matrix text send failed: {exc}", file=sys.stderr)

    # Clear pending items once attempted
    ctx.store_value(_MATRIX_PENDING_ITEMS_KEY, [])
    try:
        ctx.store_value(_MATRIX_PENDING_TEXT_KEY, "")
    except Exception:
        pass

    return 1 if any_failed else 0


def _extract_text_arg(args: Sequence[str]) -> str:
    """Extract a `-text <value>` argument from a cmdnat args list."""
    if not args:
        return ""
    try:
        tokens = list(args)
    except Exception:
        return ""
    for i, tok in enumerate(tokens):
        try:
            if str(tok).lower() == "-text" and i + 1 < len(tokens):
                return str(tokens[i + 1] or "").strip()
        except Exception:
            continue
    return ""


def _extract_room_id(room_obj: Any) -> Optional[str]:
    try:
        # PipeObject stores unknown fields in .extra
        if hasattr(room_obj, "extra"):
            extra = getattr(room_obj, "extra")
            if isinstance(extra, dict):
                rid = extra.get("room_id")
                if isinstance(rid, str) and rid.strip():
                    return rid.strip()
        # Dict fallback
        if isinstance(room_obj, dict):
            rid = room_obj.get("room_id")
            if isinstance(rid, str) and rid.strip():
                return rid.strip()
    except Exception:
        pass
    return None


def _extract_file_path(item: Any) -> Optional[str]:
    """Best-effort local file path extraction.

    Returns a filesystem path string only if it exists.
    """

    def _maybe_local_path(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, Path):
            candidate_path = value
        else:
            text = str(value).strip()
            if not text:
                return None
            # Treat URLs as not-local.
            if text.startswith("http://") or text.startswith("https://"):
                return None
            candidate_path = Path(text).expanduser()
        try:
            if candidate_path.exists():
                return str(candidate_path)
        except Exception:
            return None
        return None

    try:
        if hasattr(item, "path"):
            found = _maybe_local_path(getattr(item, "path"))
            if found:
                return found
        if hasattr(item, "file_path"):
            found = _maybe_local_path(getattr(item, "file_path"))
            if found:
                return found
        if isinstance(item, dict):
            for key in ("path", "file_path", "target"):
                found = _maybe_local_path(item.get(key))
                if found:
                    return found
    except Exception:
        pass
    return None


def _extract_url(item: Any) -> Optional[str]:
    try:
        if hasattr(item, "url"):
            raw = getattr(item, "url")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            if isinstance(raw, (list, tuple)):
                for v in raw:
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        if hasattr(item, "source_url"):
            raw = getattr(item, "source_url")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        if isinstance(item, dict):
            for key in ("url", "source_url", "path", "target"):
                raw = item.get(key)
                if (isinstance(raw,
                               str) and raw.strip() and raw.strip().startswith(
                                   ("http://",
                                    "https://"))):
                    return raw.strip()
    except Exception:
        pass
    return None


def _extract_sha256_hex(item: Any) -> Optional[str]:
    return get_sha256_hex(item, "hash")


def _extract_hash_from_hydrus_file_url(url: str) -> Optional[str]:
    """Extract hash from Hydrus URL using centralized utility."""
    return extract_hydrus_hash_from_url(url)


def _maybe_download_hydrus_file(item: Any,
                                config: Dict[str,
                                             Any],
                                output_dir: Path) -> Optional[str]:
    """If the item looks like a Hydrus file (hash + Hydrus URL), download it using Hydrus access key headers.

    This avoids 401 from Hydrus when the URL is /get_files/file?hash=... without headers.
    """
    try:
        from SYS.config import get_plugin_instance_value
        from plugins.hydrusnetwork.api import HydrusNetwork as HydrusClient, download_hydrus_file

        # Prefer per-item Hydrus instance name when it matches a configured instance.
        store_name = None
        if isinstance(item, dict):
            store_name = item.get("store")
        else:
            store_name = getattr(item, "store", None)
        store_name = str(store_name).strip() if store_name else ""

        # Try the store name as instance key first; fallback to "home".
        instance_candidates = [s for s in [store_name.lower(), "home"] if s]
        hydrus_url = None
        access_key = None
        for inst in instance_candidates:
            access_key = (get_plugin_instance_value(config, "hydrusnetwork", "API", inst) or "").strip() or None
            hydrus_url = (get_plugin_instance_value(config, "hydrusnetwork", "URL", inst) or "").strip() or None
            if access_key and hydrus_url:
                break

        if not access_key or not hydrus_url:
            return None

        url = _extract_url(item)
        file_hash = _extract_sha256_hex(item)
        if url and not file_hash:
            file_hash = _extract_hash_from_hydrus_file_url(url)

        # If it doesn't look like a Hydrus file, skip.
        if not file_hash:
            return None

        # Only treat it as Hydrus when we have a matching /get_files/file URL OR the item store suggests it.
        is_hydrus_url = False
        if url:
            parsed = urlparse(url)
            is_hydrus_url = (parsed.path or "").endswith(
                "/get_files/file"
            ) and _extract_hash_from_hydrus_file_url(url) == file_hash
        hydrus_instances: set[str] = set()
        try:
            store_cfg = (config
                         or {}).get("store") if isinstance(config,
                                                           dict) else None
            if isinstance(store_cfg, dict):
                hydrus_cfg = store_cfg.get("hydrusnetwork")
                if isinstance(hydrus_cfg, dict):
                    hydrus_instances = {
                        str(k).strip().lower()
                        for k in hydrus_cfg.keys() if str(k).strip()
                    }
        except Exception:
            hydrus_instances = set()

        store_hint = store_name.lower() in {
            "hydrus",
            "hydrusnetwork"
        } or (store_name.lower() in hydrus_instances)
        if not (is_hydrus_url or store_hint):
            return None

        client = HydrusClient(url=hydrus_url, access_key=access_key, timeout=30.0)
        file_url = url if (url and is_hydrus_url) else client.file_url(file_hash)

        # Best-effort extension from Hydrus metadata.
        suffix = ".hydrus"
        try:
            meta_response = client.fetch_file_metadata(
                hashes=[file_hash],
                include_mime=True
            )
            entries = meta_response.get("metadata"
                                        ) if isinstance(meta_response,
                                                        dict) else None
            if isinstance(entries, list) and entries:
                entry = entries[0]
                if isinstance(entry, dict):
                    ext = entry.get("ext")
                    if isinstance(ext, str) and ext.strip():
                        cleaned = ext.strip()
                        if not cleaned.startswith("."):
                            cleaned = "." + cleaned.lstrip(".")
                        if len(cleaned) <= 12:
                            suffix = cleaned
        except Exception:
            pass

        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / f"{file_hash}{suffix}"
        if dest.exists():
            # Avoid clobbering; pick a unique name.
            dest = output_dir / f"{file_hash}_{uuid.uuid4().hex[:10]}{suffix}"

        headers = {
            "Hydrus-Client-API-Access-Key": access_key
        }
        download_hydrus_file(file_url, headers, dest, timeout=30.0)
        if dest.exists():
            return str(dest)
    except Exception as exc:
        debug(f"[matrix] Hydrus export failed: {exc}")
    return None


def _resolve_upload_path(item: Any, config: Dict[str, Any]) -> Optional[str]:
    """Resolve a usable local file path for uploading.

    - Prefer existing local file paths.
    - Otherwise, if the item has an http(s) URL, download it to a temp directory.
    """
    local = _extract_file_path(item)
    if local:
        return local

    # If this is a Hydrus-backed item (e.g. /get_files/file?hash=...), download it with Hydrus headers.
    try:
        base_tmp = None
        if isinstance(config, dict):
            base_tmp = config.get("temp")
        output_dir = (
            Path(str(base_tmp)).expanduser() if base_tmp else
            (Path(tempfile.gettempdir()) / "Medios-Macina")
        )
        output_dir = output_dir / "matrix" / "hydrus"
        hydrus_path = _maybe_download_hydrus_file(item, config, output_dir)
        if hydrus_path:
            return hydrus_path
    except Exception:
        pass

    url = _extract_url(item)
    if not url:
        return None

    # Best-effort: unlock AllDebrid file links (they require auth and aren't directly downloadable).
    url = _resolve_plugin_url(url, config)

    try:
        from API.HTTP import download_direct_file

        base_tmp = None
        if isinstance(config, dict):
            base_tmp = config.get("temp")
        output_dir = (
            Path(str(base_tmp)).expanduser() if base_tmp else
            (Path(tempfile.gettempdir()) / "Medios-Macina")
        )
        output_dir = output_dir / "matrix"
        output_dir.mkdir(parents=True, exist_ok=True)
        result = download_direct_file(url, output_dir, quiet=True)
        if (result and hasattr(result,
                               "path") and isinstance(result.path,
                                                      Path) and result.path.exists()):
            return str(result.path)
    except Exception as exc:
        debug(f"[matrix] Failed to download URL for upload: {exc}")

    return None


def _show_main_menu() -> int:
    """Display main menu: Rooms or Settings."""
    table = Table("Matrix (select with @N)")
    table.set_table("matrix")
    table.set_source_command(".matrix", [])

    menu_items = [
        {
            "title": "Rooms",
            "description": "List and select rooms for uploads",
            "action": "rooms",
        },
        {
            "title": "Settings",
            "description": "View and modify Matrix configuration",
            "action": "settings",
        },
    ]

    for item in menu_items:
        row = table.add_row()
        row.add_column("Action", item["title"])
        row.add_column("Description", item["description"])

    ctx.set_last_result_table_overlay(table, menu_items)
    ctx.set_current_stage_table(table)
    ctx.set_pending_pipeline_tail([[".matrix", "-menu-select"]], ".matrix")
    return 0


def _show_settings_table(config: Dict[str, Any]) -> int:
    """Display Matrix configuration settings as a modifiable table."""
    table = Table("Matrix Settings (select with @N to modify)")
    table.set_table("matrix")
    table.set_source_command(".matrix", ["-settings"])

    matrix_conf = {}
    try:
        matrix_conf = _get_matrix_config_block(config)
    except Exception:
        pass

    settings_items = []
    if isinstance(matrix_conf, dict):
        sensitive_keys = {"access_token", "password"}
        for key in sorted(matrix_conf.keys()):
            value = matrix_conf[key]
            display_value = "***" if key in sensitive_keys else str(value)
            settings_items.append({
                "key": key,
                "label": key,
                "value": display_value,
                "original_value": value,
            })

    if not settings_items:
        log("No Matrix settings configured. Edit config.conf manually.", file=sys.stderr)
        return 0

    settings_items.append({
        "action": "test",
        "label": "Test connection",
        "value": "Verify the homeserver and token before picking rooms",
        "description": "Runs a health check and then prompts for default rooms",
    })

    for item in settings_items:
        row = table.add_row()
        label = item.get("label") or item.get("key") or "Setting"
        value_text = item.get("value") or item.get("description") or ""
        row.add_column("Key", label)
        row.add_column("Value", value_text)

    ctx.set_last_result_table_overlay(table, settings_items)
    ctx.set_current_stage_table(table)
    ctx.set_pending_pipeline_tail([[".matrix", "-settings-edit"]], ".matrix")
    return 0


def _handle_menu_selection(selected: Any, config: Dict[str, Any]) -> int:
    """Handle main menu selection (rooms or settings)."""
    items = _normalize_to_list(selected)
    if not items:
        return 1

    item = items[0]  # Only consider first selection
    action = None
    if isinstance(item, dict):
        action = item.get("action")
    else:
        action = getattr(item, "action", None)

    if action == "settings":
        return _show_settings_table(config)
    else:
        return _show_rooms_table(config)


def _handle_settings_edit(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Handle settings modification from selected rows.
    
    Two-stage flow:
    1. User selects @4 from settings table (@4 stores selection index in context)
    2. User pipes value (@4 | "30" → result becomes "30")
    3. Handler uses both: 
       - get_last_selection() → get the row index
       - result → the piped value
       - Retrieve the setting key from stored items
       - Update config
    
    Usage: @4 | "30"
    """
    # Get the last selected indices (@4 would give [3])
    selection_indices = []
    try:
        selection_indices = ctx.get_last_selection() or []
    except Exception:
        pass
    
    # Get the last result items (the settings table items)
    last_items = []
    try:
        last_items = ctx.get_last_result_items() or []
    except Exception:
        pass
    
    if not selection_indices or not last_items:
        log("No setting selected. Use @N to select a setting first.", file=sys.stderr)
        return 1
    
    # Get the selected settings item
    idx = selection_indices[0]
    if idx < 0 or idx >= len(last_items):
        log("Invalid selection", file=sys.stderr)
        return 1
    
    selected_item = last_items[idx]
    selected_action = None
    if isinstance(selected_item, dict):
        selected_action = selected_item.get("action")
    else:
        selected_action = getattr(selected_item, "action", None)

    if selected_action == "test":
        return _handle_settings_test(config)

    key = None
    if isinstance(selected_item, dict):
        key = selected_item.get("key")
    else:
        key = getattr(selected_item, "key", None)
    
    if not key:
        log("Invalid settings selection", file=sys.stderr)
        return 1
    
    # Prevent modifying sensitive settings
    if key in ("password", "access_token"):
        log(f"Cannot modify sensitive setting: {key}", file=sys.stderr)
        return 1
    
    # Extract the piped value or fallback to CLI args
    new_value = _extract_piped_value(result) or _extract_value_arg(args)
    if new_value is None:
        log(f"To modify '{key}', pipe a literal value (e.g. @N | '30') or pass it as an arg: .matrix -settings-edit 30", file=sys.stderr)
        return 1
    
    # Update the config with the new value
    if _update_matrix_config(config, str(key), new_value):
        log(f"✓ Updated {key} = {new_value}")
        return 0
    else:
        log(f"✗ Failed to update {key}", file=sys.stderr)
        return 1


def _handle_settings_test(config: Dict[str, Any]) -> int:
    """Test Matrix credentials and prompt for default rooms upon success."""
    try:
        provider = _get_matrix_provider(config)
    except Exception as exc:
        log(f"Matrix test failed: {exc}", file=sys.stderr)
        return 1

    log("Matrix configuration validated. Select default rooms to share.")
    return _show_default_room_picker(config, provider=provider)


def _show_default_room_picker(config: Dict[str, Any], *, provider: Optional[Any] = None) -> int:
    """Display joined rooms so the user can select defaults for sharing."""
    try:
        if provider is None:
            provider = _get_matrix_provider(config)
    except Exception as exc:
        log(f"Matrix not available: {exc}", file=sys.stderr)
        return 1

    try:
        rooms = provider.list_rooms()
    except Exception as exc:
        log(f"Failed to list Matrix rooms: {exc}", file=sys.stderr)
        return 1

    if not rooms:
        log("No joined rooms found.", file=sys.stderr)
        return 0

    default_ids = {
        str(v).strip()
        for v in _parse_config_room_filter_ids(config)
        if str(v).strip()
    }

    table = Table("Matrix Rooms (select defaults with @N)")
    table.set_table("matrix")
    table.set_source_command(".matrix", ["-settings"])

    room_items: List[Dict[str, Any]] = []
    for room in rooms:
        if isinstance(room, dict):
            room_id = str(room.get("room_id") or "").strip()
            name = str(room.get("name") or "").strip()
        else:
            room_id = ""
            name = ""

        row = table.add_row()
        row.add_column("Name", name)
        row.add_column("Room", room_id)
        row.add_column("Default", "✓" if room_id and room_id in default_ids else "")

        room_items.append({
            **(room if isinstance(room, dict) else {}),
            "room_id": room_id,
            "name": name,
            "store": "matrix",
            "title": name or room_id or "Matrix Room",
        })

    ctx.set_last_result_table_overlay(table, room_items)
    ctx.set_current_stage_table(table)
    ctx.set_pending_pipeline_tail([[".matrix", "-settings-rooms"]], ".matrix")
    log("Select default rooms to share (used by @N and autocomplete).")
    return 0


def _handle_settings_rooms(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Store the selected rooms list as the default sharing target."""
    selection_indices = []
    try:
        selection_indices = ctx.get_last_selection() or []
    except Exception:
        pass

    last_items = []
    try:
        last_items = ctx.get_last_result_items() or []
    except Exception:
        pass

    if not selection_indices or not last_items:
        log("No Matrix room selected. Use @N on the rooms table.", file=sys.stderr)
        return 1

    room_ids: List[str] = []
    for idx in selection_indices:
        if not isinstance(idx, int):
            continue
        if idx < 0 or idx >= len(last_items):
            continue
        item = last_items[idx]
        candidate = None
        if isinstance(item, dict):
            candidate = item.get("room_id") or item.get("id")
        else:
            candidate = getattr(item, "room_id", None) or getattr(item, "id", None)
        if candidate:
            room_ids.append(str(candidate).strip())

    cleaned: List[str] = []
    for rid in room_ids:
        clean = str(rid or "").strip()
        if clean and clean not in cleaned:
            cleaned.append(clean)

    if not cleaned:
        log("No valid Matrix room selected.", file=sys.stderr)
        return 1

    value = ", ".join(cleaned)
    if not _update_matrix_config(config, "rooms", value):
        log("✗ Failed to save default rooms", file=sys.stderr)
        return 1

    log(f"✓ Default rooms saved: {value}")
    return _show_settings_table(config)


def _show_rooms_table(config: Dict[str, Any]) -> int:
    """Display rooms (refactored original behavior)."""
    try:
        provider = _get_matrix_provider(config)
    except Exception as exc:
        log(f"Matrix not available: {exc}", file=sys.stderr)
        return 1

    try:
        configured_ids = None
        # Use `-all` flag to override room filter
        all_rooms_flag = False
        if hasattr(ctx, "get_last_args"):
            try:
                last_args = ctx.get_last_args() or []
                all_rooms_flag = any(str(a).lower() == "-all" for a in last_args)
            except Exception:
                all_rooms_flag = False

        if not all_rooms_flag:
            ids = [
                str(v).strip() for v in _parse_config_room_filter_ids(config)
                if str(v).strip()
            ]
            if ids:
                configured_ids = ids

        rooms = provider.list_rooms(room_ids=configured_ids)
    except Exception as exc:
        log(f"Failed to list Matrix rooms: {exc}", file=sys.stderr)
        return 1

    # Diagnostics if a configured filter yields no rows
    if not rooms and not all_rooms_flag:
        configured_ids_dbg = [
            str(v).strip() for v in _parse_config_room_filter_ids(config)
            if str(v).strip()
        ]
        if configured_ids_dbg:
            try:
                joined_ids = provider.list_joined_room_ids()
                debug(f"[matrix] Configured room filter IDs: {configured_ids_dbg}")
                debug(f"[matrix] Joined room IDs (from Matrix): {joined_ids}")
            except Exception:
                pass

    if not rooms:
        if _parse_config_room_filter_ids(config) and not all_rooms_flag:
            log(
                "No joined rooms matched the configured Matrix room filter (use: .matrix -all)",
                file=sys.stderr,
            )
        else:
            log("No joined rooms found.", file=sys.stderr)
        return 0

    table = Table("Matrix Rooms (select with @N)")
    table.set_table("matrix")
    table.set_source_command(".matrix", [])

    for room in rooms:
        row = table.add_row()
        name = str(room.get("name") or "").strip() if isinstance(room, dict) else ""
        room_id = str(room.get("room_id") or ""
                      ).strip() if isinstance(room,
                                              dict) else ""
        row.add_column("Name", name)
        row.add_column("Room", room_id)

    # Make selection results clearer
    room_items: List[Dict[str, Any]] = []
    for room in rooms:
        if not isinstance(room, dict):
            continue
        room_id = str(room.get("room_id") or "").strip()
        name = str(room.get("name") or "").strip()
        room_items.append(
            {
                **room,
                "store": "matrix",
                "title": name or room_id or "Matrix Room",
            }
        )

    ctx.set_last_result_table_overlay(table, room_items)
    ctx.set_current_stage_table(table)
    ctx.set_pending_pipeline_tail([[".matrix", "-send"]], ".matrix")
    return 0


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Main Matrix cmdlet execution.
    
    Flow:
    1. First call: Show main menu (Rooms or Settings)
    2. User selects @1 (rooms) or @2 (settings)
    3. -menu-select: Route to appropriate handler
    4. -send: Send files to selected room(s) (when uploading)
    5. -settings-edit: Handle settings modification
    """
    # Handle menu selection routing
    if _has_flag(args, "-menu-select"):
        return _handle_menu_selection(result, config)
    
    # Handle settings view/edit
    if _has_flag(args, "-settings"):
        return _show_settings_table(config)
    
    if _has_flag(args, "-settings-edit"):
        return _handle_settings_edit(result, args, config)

    if _has_flag(args, "-settings-rooms"):
        return _handle_settings_rooms(result, args, config)

    # Internal stage: send previously selected items to selected rooms.
    if _has_flag(args, "-send"):
        # Ensure we don't re-print the rooms picker table on the send stage.
        try:
            if hasattr(ctx, "set_last_result_table_overlay"):
                ctx.set_last_result_table_overlay(None, None, None)
        except Exception:
            pass
        try:
            if hasattr(ctx, "set_current_stage_table"):
                ctx.set_current_stage_table(None)
        except Exception:
            pass

        rooms = _normalize_to_list(result)
        room_ids: List[str] = []
        for r in rooms:
            rid = _extract_room_id(r)
            if rid:
                room_ids.append(rid)
        if not room_ids:
            log("No Matrix room selected (use @N on the rooms table)", file=sys.stderr)
            return 1

        pending_items = ctx.load_value(_MATRIX_PENDING_ITEMS_KEY, default=[])
        items = _normalize_to_list(pending_items)
        if not items:
            log("No pending items to upload (use: @N | .matrix)", file=sys.stderr)
            return 1

        try:
            provider = _get_matrix_provider(config)
        except Exception as exc:
            log(f"Matrix not available: {exc}", file=sys.stderr)
            return 1

        text_value = _extract_text_arg(args)
        if not text_value:
            try:
                text_value = str(
                    ctx.load_value(_MATRIX_PENDING_TEXT_KEY,
                                   default="") or ""
                ).strip()
            except Exception:
                text_value = ""

        size_limit_bytes = _get_matrix_size_limit_bytes(config)
        size_limit_mb = (size_limit_bytes / (1024 * 1024)) if size_limit_bytes else None

        # Resolve upload paths once (also avoids repeated downloads when sending to multiple rooms).
        upload_jobs: List[Dict[str, Any]] = []
        any_failed = False
        for item in items:
            file_path = _resolve_upload_path(item, config)
            if not file_path:
                any_failed = True
                log(
                    "Matrix upload requires a local file (path) or a direct URL on the selected item",
                    file=sys.stderr,
                )
                continue

            media_path = Path(file_path)
            if not media_path.exists():
                any_failed = True
                log(f"Matrix upload file missing: {file_path}", file=sys.stderr)
                continue

            if size_limit_bytes is not None:
                try:
                    byte_size = int(media_path.stat().st_size)
                except Exception:
                    byte_size = -1
                if byte_size >= 0 and byte_size > size_limit_bytes:
                    any_failed = True
                    actual_mb = byte_size / (1024 * 1024)
                    lim = float(size_limit_mb or 0)
                    log(
                        f"ERROR: file is too big, skipping: {media_path.name} ({actual_mb:.1f} MB > {lim:.1f} MB)",
                        file=sys.stderr,
                    )
                    continue

            upload_jobs.append({
                "path": str(media_path),
                "pipe_obj": item
            })

        for rid in room_ids:
            sent_any_for_room = False
            for job in upload_jobs:
                file_path = str(job.get("path") or "")
                if not file_path:
                    continue
                try:
                    link = provider.upload_to_room(
                        file_path,
                        rid,
                        pipe_obj=job.get("pipe_obj")
                    )
                    debug(f"✓ Sent {Path(file_path).name} -> {rid}")
                    if link:
                        log(link)
                    sent_any_for_room = True
                except Exception as exc:
                    any_failed = True
                    log(
                        f"Matrix send failed for {Path(file_path).name}: {exc}",
                        file=sys.stderr
                    )

            # Optional caption-like follow-up message (sent once per room).
            if text_value and sent_any_for_room:
                try:
                    provider.send_text_to_room(text_value, rid)
                except Exception as exc:
                    any_failed = True
                    log(f"Matrix text send failed: {exc}", file=sys.stderr)

        # Clear pending items once we've attempted to send.
        ctx.store_value(_MATRIX_PENDING_ITEMS_KEY, [])
        try:
            ctx.store_value(_MATRIX_PENDING_TEXT_KEY, "")
        except Exception:
            pass
        return 1 if any_failed else 0

    # Default stage: handle piped vs non-piped behavior
    selected_items = _normalize_to_list(result)
    ctx.store_value(_MATRIX_PENDING_ITEMS_KEY, selected_items)
    try:
        ctx.store_value(_MATRIX_PENDING_TEXT_KEY, _extract_text_arg(args))
    except Exception:
        pass
    
    # When piped (result has data), either send directly (if -room given)
    # or show rooms for interactive selection.
    if selected_items:
        # If user provided a -room argument, resolve it and send to the target(s)
        room_arg = _extract_room_arg(args)
        if room_arg:
            # Support comma-separated list of room names/ids
            requested = [r.strip() for r in room_arg.split(",") if r.strip()]
            resolved_ids: List[str] = []
            for req in requested:
                try:
                    rid = _resolve_room_identifier(req, config)
                    if rid:
                        resolved_ids.append(rid)
                except Exception:
                    # Ignore unresolved entries
                    continue

            if not resolved_ids:
                log("Could not resolve specified room(s). Opening room selection table instead.", file=sys.stderr)
                return _show_rooms_table(config)

            # Proceed to send the pending items directly
            return _send_pending_to_rooms(config, resolved_ids, args)

        return _show_rooms_table(config)
    else:
        return _show_main_menu()


CMDLET = Cmdlet(
    name=".matrix",
    alias=["matrix",
           "rooms"],
    summary="Send selected items to a Matrix room or manage settings",
    usage="@N | .matrix",
    arg=[
        CmdletArg(
            name="send",
            type="bool",
            description="(internal) Send to selected room(s)",
            required=False,
        ),
        CmdletArg(
            name="menu-select",
            type="bool",
            description="(internal) Handle menu selection (rooms/settings)",
            required=False,
        ),
        CmdletArg(
            name="settings",
            type="bool",
            description="(internal) Show settings table",
            required=False,
        ),
        CmdletArg(
            name="settings-edit",
            type="bool",
            description="(internal) Handle settings modification",
            required=False,
        ),
        CmdletArg(
            name="settings-rooms",
            type="bool",
            description="(internal) Save selected default rooms",
            required=False,
        ),
        CmdletArg(
            name="set-value",
            type="string",
            description="New value for selected setting (use with -settings-edit)",
            required=False,
        ),
        CmdletArg(
            name="all",
            type="bool",
            description="Ignore config room filter and show all joined rooms",
            required=False,
        ),
        CmdletArg(
            name="room",
            type="string",
            description="Target room (name or id). Comma-separated values supported. Autocomplete uses configured defaults.",
            required=False,
        ),
        CmdletArg(
            name="text",
            type="string",
            description="Send a follow-up text message after each upload (caption-like)",
            required=False,
        ),
    ],
    exec=_run,
)

COMMANDS = [CMDLET]
