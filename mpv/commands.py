# pyright: reportUnusedFunction=false
from typing import Any, Dict, Sequence, List, Optional
import atexit
import os
import sys
import json
import socket
import re
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from SYS.cmdlet_spec import Cmdlet, CmdletArg, parse_cmdlet_args
from PluginCore.registry import get_plugin, get_plugin_for_url
from SYS.logger import debug, get_thread_stream, is_debug_enabled, set_debug, set_thread_stream
from SYS.result_table import Table
from plugins.mpv.mpv_ipc import MPV
from SYS import pipeline as ctx
from SYS.models import PipeObject

from SYS.config import get_plugin_instance_value, resolve_cookies_path

_NOTES_PREFETCH_INFLIGHT: set[str] = set()
_NOTES_PREFETCH_LOCK = threading.Lock()
_PREFETCH_THREADS: set[threading.Thread] = set()
_PREFETCH_THREADS_LOCK = threading.Lock()
_PLAYLIST_STORE_CACHE: Optional[Dict[str, Any]] = None
_PLAYLIST_STORE_MTIME_NS: Optional[int] = None
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SHA256_FULL_RE = re.compile(r"^[0-9a-f]{64}$")
_EXTINF_TITLE_RE = re.compile(r"#EXTINF:-1,(.*?)(?:\n|\r|$)")
_WINDOWS_PATH_RE = re.compile(r"^[a-z]:[\\/]", flags=re.IGNORECASE)
_HASH_QUERY_RE = re.compile(r"hash=([0-9a-f]{64})")
_IPV4_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
_MPD_PATH_RE = re.compile(r"\.mpd($|\?)")


def _repo_root() -> Path:
    try:
        return Path(__file__).resolve().parent.parent.parent
    except Exception:
        return Path(os.getcwd())


def _playlist_store_path() -> Path:
    return _repo_root() / "mpv_playlists.json"


def _new_playlist_store() -> Dict[str, Any]:
    return {"next_id": 1, "playlists": []}


def _normalize_playlist_store(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return _new_playlist_store()

    normalized = dict(data)
    try:
        next_id = int(normalized.get("next_id") or 1)
    except Exception:
        next_id = 1
    normalized["next_id"] = max(next_id, 1)

    playlists = normalized.get("playlists")
    normalized["playlists"] = playlists if isinstance(playlists, list) else []
    return normalized


def _load_playlist_store(path: Path) -> Dict[str, Any]:
    global _PLAYLIST_STORE_CACHE, _PLAYLIST_STORE_MTIME_NS
    if not path.exists():
        _PLAYLIST_STORE_CACHE = _new_playlist_store()
        _PLAYLIST_STORE_MTIME_NS = None
        return _PLAYLIST_STORE_CACHE
    try:
        current_mtime_ns = path.stat().st_mtime_ns
        if (_PLAYLIST_STORE_CACHE is not None and
                _PLAYLIST_STORE_MTIME_NS == current_mtime_ns):
            return _PLAYLIST_STORE_CACHE

        data = _normalize_playlist_store(json.loads(path.read_text(encoding="utf-8")))
        _PLAYLIST_STORE_CACHE = data
        _PLAYLIST_STORE_MTIME_NS = current_mtime_ns
        return data
    except Exception:
        _PLAYLIST_STORE_CACHE = _new_playlist_store()
        _PLAYLIST_STORE_MTIME_NS = None
        return _PLAYLIST_STORE_CACHE


def _save_playlist_store(path: Path, data: Dict[str, Any]) -> bool:
    global _PLAYLIST_STORE_CACHE, _PLAYLIST_STORE_MTIME_NS
    try:
        normalized = _normalize_playlist_store(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
        _PLAYLIST_STORE_CACHE = normalized
        _PLAYLIST_STORE_MTIME_NS = path.stat().st_mtime_ns
        return True
    except Exception:
        return False


def _save_playlist(name: str, items: List[Any]) -> bool:
    path = _playlist_store_path()
    data = _load_playlist_store(path)
    playlists = data.get("playlists", [])
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    for pl in playlists:
        if str(pl.get("name")).strip().lower() == str(name).strip().lower():
            pl["items"] = list(items)
            pl["updated_at"] = now
            return _save_playlist_store(path, data)

    new_id = int(data.get("next_id") or 1)
    data["next_id"] = new_id + 1
    playlists.append({
        "id": new_id,
        "name": name,
        "items": list(items),
        "updated_at": now,
    })
    data["playlists"] = playlists
    return _save_playlist_store(path, data)


def _get_playlist_by_id(playlist_id: int) -> Optional[tuple[str, List[Any]]]:
    data = _load_playlist_store(_playlist_store_path())
    for pl in data.get("playlists", []):
        try:
            if int(pl.get("id")) == int(playlist_id):
                return str(pl.get("name") or ""), list(pl.get("items") or [])
        except Exception:
            continue
    return None


def _delete_playlist(playlist_id: int) -> bool:
    path = _playlist_store_path()
    data = _load_playlist_store(path)
    playlists = data.get("playlists", [])
    kept = []
    removed = False
    for pl in playlists:
        try:
            if int(pl.get("id")) == int(playlist_id):
                removed = True
                continue
        except Exception:
            pass
        kept.append(pl)
    data["playlists"] = kept
    return _save_playlist_store(path, data) if removed else False


def _get_playlists() -> List[Dict[str, Any]]:
    data = _load_playlist_store(_playlist_store_path())
    playlists = data.get("playlists", [])
    return [dict(pl) for pl in playlists if isinstance(pl, dict)]


def _repo_log_dir() -> Path:
    d = _repo_root() / "Log"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# pyright: ignore[reportUnusedFunction]
def _helper_log_file() -> Path:
    return _repo_log_dir() / "medeia-mpv-helper.log"


# pyright: ignore[reportUnusedFunction]
def _lua_log_file() -> Path:
    return _repo_log_dir() / "medeia-mpv-lua.log"


def _extract_log_filter(args: Sequence[str]) -> tuple[List[str], Optional[str]]:
    normalized: List[str] = []
    log_filter: Optional[str] = None
    i = 0
    while i < len(args):
        token = str(args[i])
        token_lower = token.lower()
        if token_lower in {"-log", "--log"}:
            normalized.append(token)
            if log_filter is None and i + 1 < len(args):
                candidate = str(args[i + 1])
                if candidate and not candidate.startswith("-"):
                    log_filter = candidate
                    i += 2
                    continue
            i += 1
            continue
        normalized.append(token)
        i += 1
    return normalized, log_filter


def _apply_log_filter(lines: Sequence[str], filter_text: Optional[str]) -> List[str]:
    if not filter_text:
        return list(lines)
    needle = filter_text.lower()
    filtered: List[str] = []
    for line in lines:
        text = str(line)
        if needle in text.lower():
            filtered.append(text)
    return filtered


def _collapse_repeated_log_lines(lines: Sequence[str]) -> List[str]:
    collapsed: List[str] = []
    last_line: Optional[str] = None
    repeat_count = 0

    def flush() -> None:
        nonlocal last_line, repeat_count
        if last_line is None:
            return
        if repeat_count > 1:
            collapsed.append(f"{last_line} [repeated x{repeat_count}]")
        else:
            collapsed.append(last_line)
        last_line = None
        repeat_count = 0

    for raw in lines:
        line = str(raw or "")
        if line == last_line:
            repeat_count += 1
            continue
        flush()
        last_line = line
        repeat_count = 1

    flush()
    return collapsed


def _is_noisy_mpv_log_line(line: str) -> bool:
    text = str(line or "")
    lower = text.lower()
    noisy_tokens = (
        "client connected",
        "client disconnected",
        "destroying client handle",
        "set property: options/log-file",
        "set property: options/msg-level",
        "run command: script-message-to, flags=64, args=[target=\"console\", args=\"log\"",
        "run command: script-message-to, flags=64, args=[target=\"console\", args=\"print\"",
    )
    if any(token in lower for token in noisy_tokens):
        return True

    startup_prefixes = (
        "mpv v",
        " built on ",
        "libplacebo version:",
        "ffmpeg version:",
        "ffmpeg library versions:",
        "   libavcodec",
        "   libavdevice",
        "   libavfilter",
        "   libavformat",
        "   libavutil",
        "   libswresample",
        "   libswscale",
        "configuration:",
        "list of enabled features:",
        "built with ndebug.",
    )
    # Log lines look like [timestamp][level][module] content — strip 3 brackets.
    stripped = lower.split(']', 3)
    payload = stripped[-1].strip() if len(stripped) > 1 else lower.strip()
    return any(prefix in payload for prefix in startup_prefixes)


def _focus_mpv_log_lines(lines: Sequence[str]) -> List[str]:
    focused = [str(line) for line in lines if not _is_noisy_mpv_log_line(str(line))]
    return _collapse_repeated_log_lines(focused)


def _focus_db_log_rows(
    rows: Sequence[tuple[Any, Any, Any, Any]],
) -> List[str]:
    rendered: List[str] = []
    for timestamp, level, _module, message in rows:
        ts = str(timestamp or "").strip()
        prefix = f"[{ts}] [{level}] " if ts else f"[{level}] "
        rendered.append(prefix + str(message or ""))
    return _collapse_repeated_log_lines(rendered)


def _slice_log_to_latest_marker(
    lines: Sequence[str],
    patterns: Sequence[re.Pattern[str]],
) -> List[str]:
    collected = list(lines)
    if not collected:
        return []
    for idx in range(len(collected) - 1, -1, -1):
        text = str(collected[idx] or "")
        if any(pattern.search(text) for pattern in patterns):
            return collected[idx:]
    return collected


def _slice_mpv_log_to_latest_run(lines: Sequence[str]) -> List[str]:
    return _slice_log_to_latest_marker(
        lines,
        [re.compile(r"\bmpv v\d", re.IGNORECASE)],
    )


def _slice_lua_log_to_latest_run(lines: Sequence[str]) -> List[str]:
    return _slice_log_to_latest_marker(
        lines,
        [re.compile(r"medeia[- ]lua loaded version=", re.IGNORECASE)],
    )


def _slice_helper_log_to_latest_run(lines: Sequence[str]) -> List[str]:
    return _slice_log_to_latest_marker(
        lines,
        [re.compile(r"\[helper\] version=.* started ipc=", re.IGNORECASE)],
    )


def _get_mpv_property(prop_name: str) -> Optional[Any]:
    try:
        resp = _send_ipc_command(
            {
                "command": ["get_property", prop_name],
            },
            silent=True,
        )
        if resp and resp.get("error") == "success":
            return resp.get("data")
    except Exception:
        pass
    return None


def _get_latest_mpv_run_marker(log_db_path: str) -> Optional[tuple[str, str]]:
    try:
        import sqlite3

        with sqlite3.connect(log_db_path, timeout=5.0) as conn:
            cur = conn.cursor()
            cur.execute(
                (
                    "SELECT timestamp, message FROM logs "
                    "WHERE module = 'mpv' AND ("
                    "message LIKE '[helper] version=% started ipc=%' "
                    "OR message LIKE 'medeia lua loaded version=%'"
                    ") "
                    "ORDER BY timestamp DESC LIMIT 1"
                )
            )
            row = cur.fetchone()
            cur.close()
        if row and row[0]:
            return str(row[0]), str(row[1] or "")
    except Exception:
        pass
    return None


def _get_mpv_logs_for_latest_run(
    log_db_path: str,
    *,
    log_filter_text: Optional[str],
    limit: int = 200,
) -> tuple[Optional[str], Optional[str], List[tuple[Any, Any, Any, Any]]]:
    marker = _get_latest_mpv_run_marker(log_db_path)
    marker_ts = marker[0] if marker else None
    marker_msg = marker[1] if marker else None
    try:
        import sqlite3

        with sqlite3.connect(log_db_path, timeout=5.0) as conn:
            cur = conn.cursor()
            query = "SELECT timestamp, level, module, message FROM logs WHERE module = 'mpv'"
            params: List[str] = []
            if marker_ts:
                query += " AND timestamp >= ?"
                params.append(marker_ts)
            else:
                cutoff = (datetime.utcnow() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
                query += " AND timestamp >= ?"
                params.append(cutoff)
            if log_filter_text:
                query += " AND LOWER(message) LIKE ?"
                params.append(f"%{log_filter_text.lower()}%")
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(str(max(1, int(limit))))
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            cur.close()
        rows.reverse()
        return marker_ts, marker_msg, rows
    except Exception:
        return marker_ts, marker_msg, []


def _try_enable_mpv_file_logging(mpv_log_path: str, *, attempts: int = 3) -> bool:
    """Best-effort enable mpv log-file + verbose level on a running instance.

    Note: mpv may not honor changing log-file at runtime on all builds/platforms.
    We still try; if it fails, callers can fall back to restart-on-demand.
    """
    if not isinstance(mpv_log_path, str) or not mpv_log_path.strip():
        return False
    mpv_log_path = mpv_log_path.strip()

    try:
        Path(mpv_log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(mpv_log_path, "a", encoding="utf-8", errors="replace"):
            pass
    except Exception:
        pass

    ok = False
    for _ in range(max(1, int(attempts))):
        try:
            # Try to set log-file and verbose level.
            r1 = _send_ipc_command(
                {
                    "command": ["set_property",
                                "options/log-file",
                                mpv_log_path]
                }
            )
            r2 = _send_ipc_command(
                {
                    "command": ["set_property",
                                "options/msg-level",
                                "cplayer=info,ffmpeg=error,ipc=warn"]
                }
            )
            ok = bool(
                (r1 and r1.get("error") == "success")
                or (r2 and r2.get("error") == "success")
            )

            # Emit a predictable line so the file isn't empty if logging is active.
            _send_ipc_command(
                {
                    "command": ["print-text",
                                f"medeia: log enabled -> {mpv_log_path}"]
                },
                silent=True
            )
        except Exception:
            ok = False

        # If mpv has opened the log file, it should have content shortly.
        try:
            p = Path(mpv_log_path)
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                return True
        except Exception:
            pass

        try:
            import time

            time.sleep(0.15)
        except Exception:
            break

    return bool(ok)


def _iter_plugin_targets(item: Any) -> List[str]:
    values: List[str] = []
    seen: set[str] = set()

    def _add(candidate: Any) -> None:
        text = str(candidate or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        values.append(text)

    try:
        if isinstance(item, dict):
            _add(item.get("path"))
            _add(item.get("url"))
            _add(item.get("source_url"))
            _add(item.get("target"))
            metadata = item.get("full_metadata") or item.get("metadata")
        else:
            _add(getattr(item, "path", None))
            _add(getattr(item, "url", None))
            _add(getattr(item, "source_url", None))
            _add(getattr(item, "target", None))
            metadata = getattr(item, "full_metadata", None) or getattr(item, "metadata", None)
        if isinstance(metadata, dict):
            _add(metadata.get("url"))
            _add(metadata.get("webpage_url"))
            _add(metadata.get("source_url"))
        extra = item.get("extra") if isinstance(item, dict) else getattr(item, "extra", None)
        if isinstance(extra, dict):
            _add(extra.get("url"))
            _add(extra.get("source_url"))
    except Exception:
        return values

    return values


def _resolve_plugin_url(url: str, config: Optional[Dict[str, Any]]) -> str:
    target = str(url or "").strip()
    if not target:
        return target

    try:
        plugin = get_plugin_for_url(target, config or {})
    except Exception:
        plugin = None
    if plugin is None:
        return target

    try:
        resolved = plugin.resolve_url(target)
    except Exception as exc:
        debug(f"Plugin URL resolution failed for {target}: {exc}", file=sys.stderr)
        return target

    return str(resolved or target)


def _resolve_plugin_playback_path(item: Any, config: Optional[Dict[str, Any]]) -> Optional[str]:
    for candidate in _iter_plugin_targets(item):
        try:
            plugin = get_plugin_for_url(candidate, config or {})
        except Exception:
            plugin = None
        if plugin is None:
            continue

        try:
            resolved = plugin.resolve_playback_path(item)
        except Exception as exc:
            debug(f"Plugin playback-path resolution failed for {candidate}: {exc}", file=sys.stderr)
            continue

        text = str(resolved or "").strip()
        if text:
            return text

    return None


def _iter_provider_hook_candidates(
    capability: str,
    *,
    config: Optional[Dict[str, Any]] = None,
    targets: Optional[Sequence[str]] = None,
) -> List[Any]:
    providers: List[Any] = []
    seen: set[str] = set()

    def _append_provider(provider: Any) -> None:
        if provider is None:
            return
        name = str(getattr(provider, "name", "") or "").strip().lower()
        if name and name not in seen:
            seen.add(name)
            providers.append(provider)

    for target in targets or ():
        try:
            provider = get_plugin_for_url(str(target or ""), config or {})
        except Exception:
            provider = None
        _append_provider(provider)

    fallback_provider_names = {
        "pipe-item-context": ("hydrusnetwork",),
        "playlist-store": ("hydrusnetwork",),
    }.get(str(capability or "").strip().lower(), ())

    for provider_name in fallback_provider_names:
        try:
            provider = get_plugin(provider_name, config or {})
        except Exception:
            provider = None
        _append_provider(provider)

    return providers


def _resolve_provider_item_context(
    item: Any,
    *,
    metadata: Optional[Dict[str, Any]],
    store: Optional[str],
    file_hash: Optional[str],
    targets: Sequence[str],
    config: Optional[Dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str]]:
    resolved_store = store
    resolved_hash = file_hash

    for provider in _iter_provider_hook_candidates(
        "pipe-item-context",
        config=config,
        targets=targets,
    ):
        try:
            result = provider.resolve_pipe_item_context(
                item,
                metadata=metadata,
                store=resolved_store,
                file_hash=resolved_hash,
                targets=targets,
            )
        except Exception:
            continue
        if not result or not isinstance(result, tuple) or len(result) != 2:
            continue
        next_store, next_hash = result
        if next_store:
            resolved_store = str(next_store).strip()
        if next_hash:
            resolved_hash = str(next_hash).strip().lower()

    return resolved_store, resolved_hash


def _infer_provider_playlist_store(
    item: Any,
    *,
    target: str,
    file_storage: Any = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    for provider in _iter_provider_hook_candidates(
        "playlist-store",
        config=config,
        targets=[target],
    ):
        try:
            resolved = provider.infer_playlist_store(
                item,
                target=target,
                file_storage=file_storage,
            )
        except Exception:
            continue
        text = str(resolved or "").strip()
        if text:
            return text
    return None


def _ensure_lyric_overlay(mpv: MPV) -> None:
    try:
        mpv.ensure_lyric_loader_running()
    except Exception:
        pass


def _send_ipc_command(command: Dict[str, Any], silent: bool = False, wait: bool = True) -> Optional[Any]:
    """Send a command to the MPV IPC pipe and return the response."""
    try:
        mpv = MPV()
        return mpv.send(command, silent=silent, wait=wait)
    except Exception as e:
        if not silent:
            debug(f"IPC Error: {e}", file=sys.stderr)
        return None


def _extract_store_and_hash(
    item: Any,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str]]:
    store: Optional[str] = None
    file_hash: Optional[str] = None
    targets: list[str] = []

    def _add_target(value: Any) -> None:
        text = str(value or "").strip()
        if text:
            targets.append(text)

    try:
        if isinstance(item, dict):
            store = item.get("store")
            file_hash = item.get("hash") or item.get("file_hash")
            _add_target(item.get("path"))
            _add_target(item.get("url"))
            _add_target(item.get("filename"))
            metadata = item.get("full_metadata") or item.get("metadata")
        else:
            store = getattr(item, "store", None)
            file_hash = getattr(item, "hash", None) or getattr(item, "file_hash", None)
            _add_target(getattr(item, "path", None))
            _add_target(getattr(item, "url", None))
            metadata = getattr(item, "full_metadata", None) or getattr(item, "metadata", None)
    except Exception:
        store = None
        file_hash = None
        metadata = None

    if isinstance(metadata, dict):
        try:
            if not store:
                store = metadata.get("store")
            if not file_hash or str(file_hash).strip().lower() in {"unknown", "none", "n/a", "na"}:
                file_hash = metadata.get("hash") or metadata.get("hash_hex")
            _add_target(metadata.get("path"))
            _add_target(metadata.get("url"))
            _add_target(metadata.get("selection_url"))
            _add_target(metadata.get("hydrus_url"))
        except Exception:
            pass

    try:
        store = str(store).strip() if store else None
    except Exception:
        store = None

    try:
        file_hash = str(file_hash).strip().lower() if file_hash else None
    except Exception:
        file_hash = None

    store, file_hash = _resolve_provider_item_context(
        item,
        metadata=metadata if isinstance(metadata, dict) else None,
        store=store,
        file_hash=file_hash,
        targets=targets,
        config=config,
    )

    if store and store.upper() in {"PATH", "LOCAL", "UNKNOWN"}:
        store = None

    if not file_hash:
        try:
            for text in targets:
                m = _SHA256_RE.search(str(text).lower())
                if m:
                    file_hash = m.group(0)
                    break
        except Exception:
            pass

    return store, file_hash


def _set_mpv_item_context(store: Optional[str], file_hash: Optional[str]) -> None:
    # Properties consumed by MPV.lyric
    try:
        _send_ipc_command(
            {
                "command": ["set_property", "user-data/medeia-item-store", store or ""],
                "request_id": 901,
            },
            silent=True,
        )
        _send_ipc_command(
            {
                "command": ["set_property", "user-data/medeia-item-hash", file_hash or ""],
                "request_id": 902,
            },
            silent=True,
        )
    except Exception:
        pass


def _get_lyric_prefetch_limit(config: Optional[Dict[str, Any]]) -> int:
    try:
        raw = (config or {}).get("lyric_prefetch_limit")
        if raw is None:
            return 5
        value = int(raw)
    except Exception:
        return 5
    return max(0, min(20, value))


def _prefetch_notes_async(
    store: Optional[str],
    file_hash: Optional[str],
    config: Optional[Dict[str, Any]],
) -> None:
    if not store or not file_hash:
        return

    key = f"{str(store).strip().lower()}:{str(file_hash).strip().lower()}"
    with _NOTES_PREFETCH_LOCK:
        if key in _NOTES_PREFETCH_INFLIGHT:
            return
        _NOTES_PREFETCH_INFLIGHT.add(key)

    cfg = dict(config or {})

    def _worker() -> None:
        try:
            from plugins.mpv.lyric import (
                load_cached_notes,
                set_notes_prefetch_pending,
                store_cached_notes,
            )
            from PluginCore.backend_registry import BackendRegistry

            cached = load_cached_notes(store, file_hash, config=cfg)
            if cached is not None:
                return

            set_notes_prefetch_pending(store, file_hash, True)

            registry = BackendRegistry(cfg, suppress_debug=True)
            if not registry.is_available(str(store)):
                return
            backend = registry[str(store)]
            notes = backend.get_note(str(file_hash), config=cfg) or {}
            store_cached_notes(store, file_hash, notes)
            try:
                debug(
                    f"Prefetched MPV notes cache for {key} keys={sorted(str(k) for k in notes)}"
                )
            except Exception:
                debug(f"Prefetched MPV notes cache for {key}")
        except Exception as exc:
            debug(f"MPV note prefetch failed for {key}: {exc}", file=sys.stderr)
        finally:
            try:
                from plugins.mpv.lyric import set_notes_prefetch_pending

                set_notes_prefetch_pending(store, file_hash, False)
            except Exception:
                pass
            with _NOTES_PREFETCH_LOCK:
                _NOTES_PREFETCH_INFLIGHT.discard(key)

    thread = threading.Thread(
        target=_worker,
        name=f"mpv-notes-prefetch-{file_hash[:8]}",
    )
    with _PREFETCH_THREADS_LOCK:
        _PREFETCH_THREADS.add(thread)
    thread.start()


def _shutdown_prefetch_threads() -> None:
    with _PREFETCH_THREADS_LOCK:
        threads = list(_PREFETCH_THREADS)
        _PREFETCH_THREADS.clear()
    for t in threads:
        try:
            t.join(timeout=1.0)
        except Exception:
            pass


atexit.register(_shutdown_prefetch_threads)


def _schedule_notes_prefetch(items: Sequence[Any], config: Optional[Dict[str, Any]]) -> None:
    limit = _get_lyric_prefetch_limit(config)
    if limit <= 0:
        return

    seen: set[str] = set()
    scheduled = 0
    for item in items or []:
        store, file_hash = _extract_store_and_hash(item, config=config)
        if not store or not file_hash:
            continue
        key = f"{store.lower()}:{file_hash}"
        if key in seen:
            continue
        seen.add(key)
        _prefetch_notes_async(store, file_hash, config)
        scheduled += 1
        if scheduled >= limit:
            break


def _get_playlist(silent: bool = False) -> Optional[List[Dict[str, Any]]]:
    """Get the current playlist from MPV. Returns None if MPV is not running."""
    cmd = {
        "command": ["get_property",
                    "playlist"],
        "request_id": 100
    }
    resp = _send_ipc_command(cmd, silent=silent)
    if resp is None:
        return None
    if resp.get("error") == "success":
        return resp.get("data", [])
    return []


def _extract_title_from_item(item: Dict[str, Any]) -> str:
    """Extract a clean title from an MPV playlist item, handling memory:// M3U hacks."""
    title = item.get("title")
    filename = item.get("filename") or item.get("playlist-path") or ""

    # Special handling for memory:// M3U playlists (used to pass titles via IPC)
    if "memory://" in filename and "#EXTINF:" in filename:
        try:
            # Extract title from #EXTINF:-1,Title
            # Use regex to find title between #EXTINF:-1, and newline
            match = _EXTINF_TITLE_RE.search(filename)
            if match:
                extracted_title = match.group(1).strip()
                if not title or title == "memory://":
                    title = extracted_title

            # If we still don't have a title, try to find the URL in the M3U content
            if not title:
                lines = filename.splitlines()
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith(
                            "memory://"):
                        # Found the URL, use it as title
                        return line
        except Exception:
            pass

    return title or filename or "Unknown"


def _looks_like_raw_playlist_title(
    title: Optional[str],
    target: Optional[str],
) -> bool:
    text = str(title or "").strip()
    if not text or text == "Unknown":
        return True

    target_text = str(target or "").strip()
    if target_text and text == target_text:
        return True

    lower = text.lower()
    if lower.startswith(("http://", "https://", "hydrus://", "file://", "memory://")):
        return True
    if _WINDOWS_PATH_RE.match(text) or text.startswith("\\\\"):
        return True
    return False


def _resolve_hydrus_playlist_title(
    target: str,
    *,
    store_name: Optional[str],
    file_hash: Optional[str],
    config: Optional[Dict[str, Any]],
) -> Optional[str]:
    raw_target = str(target or "").strip()
    if not raw_target:
        return None

    resolved_store = str(store_name or "").strip() or None
    resolved_hash = str(file_hash or "").strip().lower() or None
    looks_hydrus = bool(resolved_store) or bool(
        _SHA256_FULL_RE.fullmatch(raw_target.lower())
    ) or raw_target.lower().startswith("hydrus://") or _is_hydrus_path(raw_target, None)
    if not looks_hydrus:
        return None

    try:
        hydrus_plugin = get_plugin("hydrusnetwork", config or {})
    except Exception:
        hydrus_plugin = None
    if hydrus_plugin is None:
        return None

    try:
        parsed_store, parsed_hash = hydrus_plugin.parse_hydrus_url(raw_target)
    except Exception:
        parsed_store, parsed_hash = None, ""

    if not resolved_store and parsed_store:
        resolved_store = str(parsed_store).strip() or None
    if not resolved_hash and parsed_hash:
        resolved_hash = str(parsed_hash).strip().lower() or None

    if not resolved_store:
        try:
            inferred_store = hydrus_plugin.infer_playlist_store(
                None,
                target=raw_target,
                file_storage=None,
            )
        except Exception:
            inferred_store = None
        if inferred_store:
            resolved_store = str(inferred_store).strip() or None

    if not resolved_hash:
        try:
            hashes = hydrus_plugin.find_hashes_by_url(
                raw_target,
                store_name=resolved_store,
            )
        except TypeError:
            try:
                hashes = hydrus_plugin.find_hashes_by_url(raw_target)
            except Exception:
                hashes = []
        except Exception:
            hashes = []
        if isinstance(hashes, list) and hashes:
            resolved_hash = str(hashes[0] or "").strip().lower() or None

    if not resolved_hash:
        return None

    try:
        resolved_title = hydrus_plugin.get_title(
            resolved_hash,
            store_name=resolved_store,
        )
    except Exception:
        resolved_title = ""

    title_text = str(resolved_title or "").strip()
    if not title_text:
        return None

    if title_text.lower() in {resolved_hash, resolved_hash[:16] + "..."}:
        return None
    return title_text


def _resolve_playlist_display_title(
    item: Dict[str, Any],
    *,
    config: Optional[Dict[str, Any]] = None,
    file_storage: Optional[Any] = None,
    store_name: Optional[str] = None,
    file_hash: Optional[str] = None,
    title_cache: Optional[Dict[tuple[str, str, str], Optional[str]]] = None,
) -> str:
    title = _extract_title_from_item(item)
    filename = item.get("filename") or item.get("playlist-path") or ""
    real_path = _extract_target_from_memory_uri(filename) or filename
    if not _looks_like_raw_playlist_title(title, real_path):
        return title

    resolved_store = str(store_name or "").strip() or None
    resolved_hash = str(file_hash or "").strip().lower() or None
    if not resolved_store or not resolved_hash:
        extracted_store, extracted_hash = _extract_store_and_hash(
            {
                "store": resolved_store,
                "hash": resolved_hash,
                "path": real_path,
                "filename": filename,
                "title": title,
            },
            config=config,
        )
        if not resolved_store and extracted_store:
            resolved_store = str(extracted_store).strip() or None
        if not resolved_hash and extracted_hash:
            resolved_hash = str(extracted_hash).strip().lower() or None

    cache_key = (
        str(real_path or "").strip().lower(),
        str(resolved_store or "").strip().lower(),
        str(resolved_hash or "").strip().lower(),
    )
    if title_cache is not None and cache_key in title_cache:
        cached_title = title_cache[cache_key]
        return cached_title or title

    resolved_title = _resolve_hydrus_playlist_title(
        real_path,
        store_name=resolved_store,
        file_hash=resolved_hash,
        config=config,
    )
    if title_cache is not None:
        title_cache[cache_key] = resolved_title
    return resolved_title or title


def _extract_target_from_memory_uri(text: str) -> Optional[str]:
    """Extract the real target URL/path from a memory:// M3U payload."""
    if not isinstance(text, str) or not text.startswith("memory://"):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("memory://"):
            continue
        return line
    return None


def _normalize_playlist_path(text: Optional[str]) -> Optional[str]:
    """Normalize playlist entry paths for dedupe comparisons."""
    if not text:
        return None
    real = _extract_target_from_memory_uri(text) or text
    real = real.strip()
    if not real:
        return None
    # If it's already a bare hydrus hash, use it directly
    lower_real = real.lower()
    if _SHA256_FULL_RE.fullmatch(lower_real):
        return lower_real

    # If it's a hydrus file URL, normalize to the hash for dedupe
    try:
        parsed = urlparse(real)
        if parsed.scheme in {"http",
                             "https",
                             "hydrus"}:
            if parsed.path.endswith("/get_files/file"):
                qs = parse_qs(parsed.query)
                h = qs.get("hash", [None])[0]
                if h and _SHA256_FULL_RE.fullmatch(h.lower()):
                    return h.lower()
    except Exception:
        pass

    # Normalize slashes for Windows paths and lowercase for comparison
    real = real.replace("\\", "/")
    return real.lower()


def _infer_store_from_playlist_item(
    item: Dict[str,
               Any],
    file_storage: Optional[Any] = None,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Infer a friendly store label from an MPV playlist entry.

    Args:
        item: MPV playlist item dict
        file_storage: Optional FileStorage instance for querying specific backend instances

    Returns:
        Store label (e.g., 'home', 'work', 'local', 'youtube', etc.)
    """
    name = item.get("filename") if isinstance(item, dict) else None
    target = str(name or "")

    # Unwrap memory:// M3U wrapper
    memory_target = _extract_target_from_memory_uri(target)
    if memory_target:
        target = memory_target

    provider_store = _infer_provider_playlist_store(
        item,
        target=target,
        file_storage=file_storage,
        config=config,
    )
    if provider_store:
        return provider_store

    lower = target.lower()
    if lower.startswith("magnet:"):
        return "magnet"

    # Windows / UNC paths
    if _WINDOWS_PATH_RE.match(target) or target.startswith("\\\\"):
        return "local"

    # file:// url
    if lower.startswith("file://"):
        return "local"

    parsed = urlparse(target)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if not host:
        return ""

    host_no_port = host.split(":", 1)[0]
    host_stripped = host_no_port[4:] if host_no_port.startswith(
        "www."
    ) else host_no_port

    if "youtube" in host_stripped or "youtu.be" in target.lower():
        return "youtube"
    if "soundcloud" in host_stripped:
        return "soundcloud"
    if "bandcamp" in host_stripped:
        return "bandcamp"

    parts = host_stripped.split(".")
    if len(parts) >= 2:
        return parts[-2] or host_stripped
    return host_stripped


def _build_hydrus_header(config: Dict[str, Any]) -> Optional[str]:
    """Return header string for Hydrus auth if configured."""
    try:
        key = get_plugin_instance_value(config, "hydrusnetwork", "API")
    except Exception:
        key = None
    if not key:
        return None
    return f"Hydrus-Client-API-Access-Key: {key}"


def _build_ytdl_options(config: Optional[Dict[str,
                                              Any]],
                        hydrus_header: Optional[str]) -> Optional[str]:
    """Compose mpv ytdl-raw-options for playback.

    Keep this limited to options that are truly required for MPV playback.
    In particular, do not pass the repo cookiefile via `cookies=...` here:
    mpv's ytdl-hook can fail to resolve some YouTube/Shorts URLs when a global
    cookiefile is forced, even though the same URLs work with MPV defaults.
    """
    opts: List[str] = []

    if hydrus_header:
        opts.append(f"add-header={hydrus_header}")
    return ",".join(opts) if opts else None


def _is_hydrus_path(path: str, hydrus_url: Optional[str]) -> bool:
    if not path:
        return False
    lower = path.lower()
    if "hydrus://" in lower:
        return True
    parsed = urlparse(path)
    host = (parsed.netloc or "").lower()
    path_part = parsed.path or ""
    if hydrus_url:
        try:
            hydrus_host = urlparse(hydrus_url).netloc.lower()
            if hydrus_host and hydrus_host in host:
                return True
        except Exception:
            pass
    if "get_files" in path_part or "file?hash=" in path_part:
        return True
    if _IPV4_RE.match(host) and "get_files" in path_part:
        return True
    return False


def _is_probable_ytdl_url(url: str) -> bool:
    """Check if the URL is likely meant to be handled by MPV's ytdl-hook.

    We use this to avoid wrapping these URLs in memory:// M3U payloads,
    since the wrapper can sometimes prevent the ytdl-hook from triggering.
    """
    if not isinstance(url, str):
        return False

    lower = url.lower().strip()
    if not lower.startswith(("http://", "https://")):
        return False

    # Exclude Hydrus API file links (we handle headers for these separately)
    if "/get_files/file" in lower:
        return False

    # Exclude Tidal manifest redirects if they've been resolved already
    if "tidal.com" in lower and "/manifest" in lower:
        return False

    # Exclude Tidal CDN direct media URLs (these are already-resolved streams and do
    # not need ytdl-hook; wrapping them in memory:// M3U is safe and lets us carry
    # per-item titles into MPV's playlist UI).
    if "audio.tidal.com" in lower or "/mediatracks/" in lower:
        return False

    # Exclude AllDebrid protected links
    if "alldebrid.com/f/" in lower:
        return False

    # Most other HTTP links (YouTube, Bandcamp, etc) are candidates for yt-dlp resolution in MPV
    return True


def _ensure_ytdl_cookies(config: Optional[Dict[str, Any]] = None) -> None:
    """Ensure yt-dlp options are set correctly for this session."""
    from pathlib import Path

    cookies_path = None
    try:
        cookiefile = resolve_cookies_path(config or {})
        if cookiefile is not None:
            cookies_path = str(cookiefile)
    except Exception:
        cookies_path = None
    if cookies_path:
        # Check if file exists and has content (use forward slashes for path checking)
        check_path = cookies_path.replace("\\", "/")
        file_obj = Path(cookies_path)
        if file_obj.exists():
            file_size = file_obj.stat().st_size
            debug(f"Cookies file verified: {check_path} ({file_size} bytes)")
        else:
            debug(
                f"WARNING: Cookies file does not exist: {check_path}",
                file=sys.stderr
            )
    else:
        debug("No cookies file configured")


def _monitor_mpv_logs(duration: float = 3.0) -> None:
    """Monitor MPV logs for a short duration to capture errors."""
    try:
        mpv = MPV()
        client = mpv.client()
        if not client.connect():
            debug("Failed to connect to MPV for log monitoring", file=sys.stderr)
            return

        # Request log messages
        client.send_command({
            "command": ["request_log_messages",
                        "warn"]
        })

        # On Windows named pipes, avoid blocking the CLI; skip log read entirely
        if client.is_windows:
            client.disconnect()
            return

        import time

        start_time = time.time()

        # Unix sockets already have timeouts set; read until duration expires
        sock_obj = client.sock
        if not isinstance(sock_obj, socket.socket):
            client.disconnect()
            return

        while time.time() - start_time < duration:
            try:
                chunk = sock_obj.recv(4096)
            except socket.timeout:
                continue
            except Exception:
                break
            if not chunk:
                break
            for line in chunk.decode("utf-8", errors="ignore").splitlines():
                try:
                    msg = json.loads(line)
                    if msg.get("event") == "log-message":
                        text = msg.get("text", "").strip()
                        prefix = msg.get("prefix", "")
                        level = msg.get("level", "")
                        if "ytdl" in prefix or level == "error":
                            debug(f"[MPV {prefix}] {text}", file=sys.stderr)
                except json.JSONDecodeError:
                    continue

        client.disconnect()
    except Exception:
        pass


def _tail_text_file(path: str,
                    *,
                    max_lines: int = 120,
                    max_bytes: int = 65536) -> List[str]:
    try:
        p = Path(str(path))
        if not p.exists() or not p.is_file():
            return []
    except Exception:
        return []
    try:
        with open(p, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                start = max(0, end - int(max_bytes))
                f.seek(start, os.SEEK_SET)
            except Exception:
                pass
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > max_lines:
            return lines[-max_lines:]
        return lines
    except Exception:
        return []


def _extract_tidal_stream_fallback_url(item: Any) -> Optional[str]:
    """Best-effort HTTP streaming fallback for unresolved tidal:// placeholders."""

    def _http_candidate(value: Any) -> Optional[str]:
        if isinstance(value, list):
            for entry in value:
                candidate = _http_candidate(entry)
                if candidate:
                    return candidate
            return None
        text = str(value or "").strip()
        if not text:
            return None
        if text.lower().startswith(("http://", "https://")):
            return text
        return None

    metadata: Optional[Dict[str, Any]] = None

    if isinstance(item, dict):
        metadata = item.get("full_metadata") or item.get("metadata")
        for key in ("url", "source_url", "target"):
            candidate = _http_candidate(item.get(key))
            if candidate:
                return candidate
    else:
        try:
            metadata = getattr(item, "full_metadata", None) or getattr(item, "metadata", None)
        except Exception:
            metadata = None
        for key in ("url", "source_url", "target"):
            try:
                candidate = _http_candidate(getattr(item, key, None))
            except Exception:
                candidate = None
            if candidate:
                return candidate

    if not isinstance(metadata, dict):
        return None

    for key in (
        "_tidal_manifest_url",
        "streamUrl",
        "audioUrl",
        "assetUrl",
        "playbackUrl",
        "manifestUrl",
        "manifestURL",
        "url",
    ):
        candidate = _http_candidate(metadata.get(key))
        if candidate:
            return candidate

    return None


def _get_playable_path(
    item: Any,
    file_storage: Optional[Any],
    config: Optional[Dict[str,
                          Any]]
) -> Optional[tuple[str,
                    Optional[str]]]:
    """Extract a playable path/URL from an item, handling different store types.

    Args:
        item: Item to extract path from (dict, PipeObject, or string)
        file_storage: FileStorage instance for querying backends
        config: Config dict for Hydrus URL

    Returns:
        Tuple of (path, title) or None if no valid path found
    """
    path: Optional[str] = None
    title: Optional[str] = None
    store: Optional[str] = None
    file_hash: Optional[str] = None

    # Extract fields from item - prefer a disk path ('path'), but accept 'url' as fallback for providers
    if isinstance(item, dict):
        path = item.get("path")
        # Fallbacks for provider-style entries where URL is stored in 'url' or 'source_url' or 'target'
        if not path:
            path = item.get("url") or item.get("source_url") or item.get("target")
            if not path:
                known = item.get("url") or item.get("url") or []
                if known and isinstance(known, list):
                    path = known[0]
        title = item.get("title") or item.get("file_title")
        store = item.get("store")
        file_hash = item.get("hash")
    elif (hasattr(item,
                  "path") or hasattr(item,
                                     "url") or hasattr(item,
                                                       "source_url") or hasattr(item,
                                                                                "store")
          or hasattr(item,
                     "hash")):
        # Handle PipeObject / dataclass objects - prefer path, but fall back to url/source_url attributes
        path = getattr(item, "path", None)
        if not path:
            path = (
                getattr(item,
                        "url",
                        None) or getattr(item,
                                         "source_url",
                                         None) or getattr(item,
                                                          "target",
                                                          None)
            )
            if not path:
                known = getattr(item,
                                "url",
                                None) or (getattr(item,
                                                  "extra",
                                                  None) or {}).get("url")
                if known and isinstance(known, list):
                    path = known[0]
        title = getattr(item, "title", None) or getattr(item, "file_title", None)
        store = getattr(item, "store", None)
        file_hash = getattr(item, "hash", None)
    elif isinstance(item, str):
        path = item

    if not store or not file_hash or str(file_hash).strip().lower() == "unknown":
        try:
            extracted_store, extracted_hash = _extract_store_and_hash(item, config=config)
        except Exception:
            extracted_store, extracted_hash = None, None
        if extracted_store and not store:
            store = extracted_store
        if extracted_hash and (not file_hash or str(file_hash).strip().lower() == "unknown"):
            file_hash = extracted_hash

    # Debug: show incoming values
    try:
        debug(f"_get_playable_path: store={store}, path={path}, hash={file_hash}")
    except Exception:
        pass

    # Treat common placeholders as missing.
    if isinstance(path,
                  str) and path.strip().lower() in {"",
                                                    "n/a",
                                                    "na",
                                                    "none"}:
        path = None

    hydrus_store = False
    if store:
        try:
            hydrus_provider = get_plugin("hydrusnetwork", config or {})
            hydrus_store = bool(
                hydrus_provider is not None
                and hydrus_provider.is_store_name(str(store))
            )
        except Exception:
            hydrus_store = False

    hydrus_file = bool(
        hydrus_store
        and file_hash
        and str(file_hash).strip().lower() not in {"", "unknown"}
    )
    if hydrus_file and isinstance(path, str):
        lowered = path.replace("\\", "/").lower()
        if path.startswith("hydrus://") or (
            path.startswith(("http://", "https://"))
            and "get_files/file" not in lowered
        ):
            path = None

    manifest_path = None if hydrus_file else _resolve_plugin_playback_path(item, config)
    if manifest_path:
        path = manifest_path
    else:
        # If this is a tidal:// placeholder and we couldn't resolve a manifest, do not fall back —
        # UNLESS the item has already been stored in a backend (store+hash present), in which case
        # we clear the tidal:// path so the store-resolution logic below can build a playable URL.
        try:
            if isinstance(path, str) and path.strip().lower().startswith("tidal:"):
                if store and file_hash and str(file_hash).strip().lower() not in ("", "unknown"):
                    # Item is stored in a backend — clear the tidal:// placeholder and let
                    # the hash+store resolution further below build the real playable URL.
                    path = None
                else:
                    fallback_stream_url = _extract_tidal_stream_fallback_url(item)
                    if fallback_stream_url:
                        path = fallback_stream_url
                        try:
                            debug(
                                f"_get_playable_path: using fallback Tidal stream URL {fallback_stream_url}"
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            meta = None
                            if isinstance(item, dict):
                                meta = item.get("full_metadata") or item.get("metadata")
                            else:
                                meta = getattr(item, "full_metadata", None) or getattr(item, "metadata", None)
                            if isinstance(meta, dict) and meta.get("_tidal_manifest_error"):
                                print(str(meta.get("_tidal_manifest_error")), file=sys.stderr)
                        except Exception:
                            pass
                        print("Tidal selection has no playable DASH MPD manifest.", file=sys.stderr)
                        return None
        except Exception:
            pass

    if title is not None and not isinstance(title, str):
        title = str(title)

    if isinstance(file_hash, str):
        file_hash = file_hash.strip().lower()

    # Resolve hash+store into a playable target (file path or URL).
    # This is unrelated to MPV's IPC pipe and keeps "pipe" terminology reserved for:
    # - MPV IPC pipe (transport)
    # - PipeObject (pipeline data)
    backend_target_resolved = False
    if store and file_hash and file_hash != "unknown":
        try:
            resolved_path = _resolve_plugin_playback_path(
                {
                    "store": str(store),
                    "hash": str(file_hash),
                    "path": path,
                    "url": path,
                },
                config,
            )
        except Exception as e:
            debug(
                f"Error resolving playback path from store '{store}': {e}",
                file=sys.stderr,
            )
            resolved_path = None

        if resolved_path:
            backend_target_resolved = True
            path = resolved_path

    if isinstance(path, str) and path.startswith(("http://", "https://")) and not backend_target_resolved:
        return (path, title)

    if not path:
        # As a last resort, if we have a hash and no path/url, return the hash.
        # _queue_items will convert it to a Hydrus file URL when possible.
        if store and file_hash and file_hash != "unknown":
            return (str(file_hash), title)
        return None

    if not isinstance(path, str):
        path = str(path)

    return (path, title)


def _queue_items(
    items: List[Any],
    clear_first: bool = False,
    config: Optional[Dict[str,
                          Any]] = None,
    start_opts: Optional[Dict[str,
                              Any]] = None,
    wait: bool = True,
) -> bool:
    """Queue items to MPV, starting it if necessary.

    Args:
        items: List of items to queue
        clear_first: If True, the first item will replace the current playlist
        wait: If True, wait for MPV to acknowledge the loadfile command.

    Returns:
        True if MPV was started, False if items were queued via IPC.
    """
    # Debug: print incoming items
    try:
        debug(
            f"_queue_items: count={len(items)} types={[type(i).__name__ for i in items]}"
        )
    except Exception:
        pass

    # Batched queue operations should not block on one IPC reply per item.
    # On Windows named pipes, waiting for every `loadfile`/`loadlist` ack can
    # make multi-select `.mpv` calls stall for several seconds.
    command_wait = bool(wait)
    if len(items) > 1:
        command_wait = False

    # Just verify cookies are configured, don't try to set via IPC
    _ensure_ytdl_cookies(config)

    hydrus_header = _build_hydrus_header(config or {})
    ytdl_opts = _build_ytdl_options(config, hydrus_header)
    hydrus_url = None
    try:
        hydrus_url = get_plugin_instance_value(config, "hydrusnetwork", "URL") if config is not None else None
    except Exception:
        hydrus_url = None

    # Initialize backend registry for path resolution
    file_storage = None
    try:
        from PluginCore.backend_registry import BackendRegistry

        file_storage = BackendRegistry(config or {})
    except Exception as e:
        debug(f"Warning: Could not initialize backend registry: {e}", file=sys.stderr)

    _schedule_notes_prefetch(items, config)

    # Dedupe existing playlist before adding more (unless we're replacing it)
    existing_targets: set[str] = set()
    if not clear_first:
        playlist = _get_playlist(silent=True) or []
        dup_indexes: List[int] = []
        for idx, pl_item in enumerate(playlist):
            fname = pl_item.get("filename") if isinstance(pl_item,
                                                          dict) else str(pl_item)
            alt = pl_item.get("playlist-path") if isinstance(pl_item, dict) else None
            norm = _normalize_playlist_path(fname) or _normalize_playlist_path(alt)
            if not norm:
                continue
            if norm in existing_targets:
                dup_indexes.append(idx)
            else:
                existing_targets.add(norm)

        # Remove duplicates from playlist starting from the end to keep indices valid
        # Use wait=False for better performance, especially over slow IPC
        for idx in reversed(dup_indexes):
            try:
                _send_ipc_command(
                    {
                        "command": ["playlist-remove",
                                    idx],
                        "request_id": 106
                    },
                    silent=True,
                    wait=False
                )
            except Exception:
                pass

    new_targets: set[str] = set()

    for i, item in enumerate(items):
        # Debug: show the item being processed
        try:
            debug(
                f"_queue_items: processing idx={i} type={type(item)} repr={repr(item)[:200]}"
            )
        except Exception:
            pass
        # Extract URL/Path using store-aware logic
        result = _get_playable_path(item, file_storage, config)
        if not result:
            debug(f"_queue_items: item idx={i} produced no playable path")
            continue

        target, title = result

        # MPD/DASH playback requires ffmpeg protocol whitelist (file + https + crypto etc).
        # Set it via IPC before loadfile so the currently running MPV can play the manifest.
        try:
            target_str = str(target or "")
            if _MPD_PATH_RE.search(target_str.lower()):
                _send_ipc_command(
                    {
                        "command": [
                            "set_property",
                            "options/demuxer-lavf-o",
                            "protocol_whitelist=file,https,tcp,tls,crypto,data",
                        ],
                        "request_id": 198,
                    },
                    silent=True,
                    wait=False
                )
        except Exception:
            pass

        # If the target is an AllDebrid protected file URL, unlock it to a direct link for MPV.
        try:
            if isinstance(target, str):
                target = _resolve_plugin_url(target, config)
        except Exception:
            pass

        # Prefer per-item Hydrus instance credentials when the item belongs to a Hydrus store.
        effective_hydrus_url = hydrus_url
        effective_hydrus_header = hydrus_header
        effective_ytdl_opts = ytdl_opts
        item_store_name: Optional[str] = None
        try:
            item_store = None
            if isinstance(item, dict):
                item_store = item.get("store")
                metadata = item.get("full_metadata") or item.get("metadata")
            else:
                item_store = getattr(item, "store", None)
                metadata = getattr(item, "full_metadata", None) or getattr(item, "metadata", None)

            if not item_store and isinstance(metadata, dict):
                item_store = metadata.get("store")

            if not item_store:
                item_store, _ = _extract_store_and_hash(item, config=config)

            if item_store:
                item_store_name = str(item_store).strip() or None

            if item_store_name and file_storage:
                hydrus_provider = get_plugin("hydrusnetwork", config or {})
                if hydrus_provider is not None:
                    try:
                        client = hydrus_provider.get_client(
                            store_name=item_store_name,
                            allow_default=False,
                        )
                    except Exception:
                        client = None
                    if client is not None:
                        base_url = getattr(client, "url", None)
                        key = getattr(client, "access_key", None)
                        if base_url:
                            effective_hydrus_url = str(base_url).rstrip("/")
                        if key:
                            effective_hydrus_header = (
                                f"Hydrus-Client-API-Access-Key: {str(key).strip()}"
                            )
                            effective_ytdl_opts = _build_ytdl_options(
                                config,
                                effective_hydrus_header
                            )
        except Exception:
            pass

        if target:
            # If we just have a hydrus hash, build a direct file URL for MPV
            if _SHA256_FULL_RE.fullmatch(
                str(target).strip().lower()
            ) and effective_hydrus_url:
                target = (
                    f"{effective_hydrus_url.rstrip('/')}/get_files/file?hash={str(target).strip()}"
                )

            hydrus_target = bool(
                effective_hydrus_header and _is_hydrus_path(str(target), effective_hydrus_url)
            )

            norm_key = _normalize_playlist_path(target) or str(target).strip().lower()
            if norm_key in existing_targets or norm_key in new_targets:
                debug(f"Skipping duplicate playlist entry: {title or target}")
                continue
            new_targets.add(norm_key)

            # Use memory:// M3U to preserve titles in MPV's playlist UI.
            # Avoid this only for probable ytdl URLs because it can prevent the hook from triggering.
            safe_title = title.replace("\n", " ").replace("\r", "") if title else None
            if title and not _is_probable_ytdl_url(target):
                # Sanitize title for M3U (remove newlines)
                # Carry the store name for hash URLs so MPV.lyric can resolve the backend.
                # This is especially important for local file-server URLs like /get_files/file?hash=...
                target_for_m3u = target
                try:
                    if (item_store_name and isinstance(target_for_m3u,
                                                       str)
                            and target_for_m3u.startswith("http")):
                        if "get_files/file" in target_for_m3u and "store=" not in target_for_m3u:
                            sep = "&" if "?" in target_for_m3u else "?"
                            target_for_m3u = f"{target_for_m3u}{sep}store={item_store_name}"
                except Exception:
                    target_for_m3u = target

                m3u_content = f"#EXTM3U\n#EXTINF:-1,{safe_title}\n{target_for_m3u}"
                target_to_send = f"memory://{m3u_content}"
            else:
                target_to_send = target

            mode = "append"
            if clear_first and i == 0:
                mode = "replace"

            # If we're replacing, this will start playing immediately: set store/hash context
            # so MPV.lyric can resolve the correct backend for notes.
            if mode == "replace":
                try:
                    s, h = _extract_store_and_hash(item, config=config)
                    _set_mpv_item_context(s, h)
                except Exception:
                    pass

            # If this is a Hydrus path, set header property and yt-dlp headers before loading.
            # Use the real target (not the memory:// wrapper) for detection.
            if hydrus_target:
                header_cmd = {
                    "command":
                    ["set_property",
                     "http-header-fields",
                     effective_hydrus_header],
                    "request_id":
                    199,
                }
                _send_ipc_command(header_cmd, silent=True, wait=True)
                if effective_ytdl_opts:
                    ytdl_cmd = {
                        "command":
                        ["set_property",
                         "ytdl-raw-options",
                         effective_ytdl_opts],
                        "request_id": 197,
                    }
                    _send_ipc_command(ytdl_cmd, silent=True, wait=True)

            # For memory:// M3U payloads (used to carry titles), use loadlist so mpv parses
            # the content as a playlist and does not expose #EXTINF lines as entries.
            command_name = "loadfile"
            try:
                if isinstance(target_to_send, str) and target_to_send.startswith("memory://") and "#EXTM3U" in target_to_send:
                    command_name = "loadlist"
            except Exception:
                pass

            load_options: Dict[str, str] = {}
            if hydrus_target and command_name == "loadfile":
                load_options["ytdl"] = "no"
                if safe_title:
                    load_options["force-media-title"] = safe_title

            command_args: List[Any] = [command_name, target_to_send, mode]
            if load_options:
                command_args.extend([-1, load_options])

            cmd = {
                "command": command_args,
                "request_id": 200
            }
            try:
                debug(f"Sending MPV {command_name}: {target_to_send} mode={mode} wait={command_wait}")
                resp = _send_ipc_command(cmd, silent=True, wait=command_wait)
                debug(f"MPV {command_name} response: {resp}")
            except Exception as e:
                debug(f"Exception sending {command_name} to MPV: {e}", file=sys.stderr)
                resp = None

            if resp is None:
                # MPV not running (or died)
                # Start MPV with remaining items
                debug(
                    f"MPV not running/died while queuing, starting MPV with remaining items: {items[i:]}"
                )
                _start_mpv(items[i:], config=config, start_opts=start_opts)
                return True
            elif resp.get("error") == "success":
                # Do not set `force-media-title` when queueing items. It's a global property and
                # would change the MPV window title even if the item isn't currently playing.
                debug(f"Queued: {title or target}")
            else:
                error_msg = str(resp.get("error"))
                debug(f"Failed to queue item: {error_msg}", file=sys.stderr)
    return False


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Manage and play items in the MPV playlist via IPC."""

    log_filter_text: Optional[str] = None
    args_for_parse, log_filter_text = _extract_log_filter(args)
    parsed = parse_cmdlet_args(args_for_parse, CMDLET)
    if log_filter_text:
        log_filter_text = log_filter_text.strip()
        if not log_filter_text:
            log_filter_text = None

    log_requested = bool(parsed.get("log"))
    borderless = bool(parsed.get("borderless"))

    prev_debug = is_debug_enabled()
    prev_stream = get_thread_stream()
    devnull_fh = None

    mpv_log_path: Optional[str] = None

    try:
        # Default: keep `.pipe` quiet even if debug is enabled.
        # With -log: keep transport chatter quiet and print an explicit focused
        # report later in this function.
        if log_requested:
            try:
                log_dir = _repo_log_dir()
                mpv_log_path = str((log_dir / "medeia-mpv.log").resolve())
            except Exception:
                mpv_log_path = str(
                    (
                        Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") /
                        "medeia-mpv.log"
                    ).resolve()
                )
            # Ensure file exists early so we can tail it even if mpv writes later.
            try:
                Path(mpv_log_path).parent.mkdir(parents=True, exist_ok=True)
                with open(mpv_log_path, "a", encoding="utf-8", errors="replace"):
                    pass
            except Exception:
                pass
            # Try to enable mpv file logging on the currently running instance.
            # (If mpv wasn't started with --log-file, this may not work everywhere.)
            try:
                _try_enable_mpv_file_logging(mpv_log_path, attempts=3)
            except Exception:
                pass

            # If mpv is already running, set log options live via IPC.
            try:
                mpv_live = MPV()
                if mpv_live.is_running():
                    mpv_live.set_property("options/log-file", mpv_log_path)
                    mpv_live.set_property("options/msg-level", "cplayer=info,ffmpeg=error,ipc=warn")
            except Exception:
                pass
        else:
            if prev_debug:
                try:
                    devnull_fh = open(
                        os.devnull,
                        "w",
                        encoding="utf-8",
                        errors="replace"
                    )
                    set_thread_stream(devnull_fh)
                except Exception:
                    pass

        start_opts: Dict[str,
                         Any] = {
                             "borderless": borderless,
                             "mpv_log_path": mpv_log_path
                         }

        # Store registry is only needed for certain playlist listing/inference paths.
        # Keep it lazy so a simple `.pipe <url> -play` doesn't trigger Hydrus/API calls.
        file_storage = None

        # Initialize mpv_started flag
        mpv_started = False

        # Handle positional index argument if provided
        index_arg = parsed.get("index")
        url_arg = parsed.get("url")

        # If index_arg is provided but is not an integer, treat it as a URL
        # This allows .pipe "http://..." without -url flag
        if index_arg is not None:
            # Avoid exception-based check to prevent debugger breaks on caught exceptions
            index_str = str(index_arg).strip()
            is_int = False
            if index_str:
                if index_str.isdigit():
                    is_int = True
                elif index_str.startswith("-") and index_str[1:].isdigit():
                    is_int = True

            if not is_int:
                # Not an integer, treat as URL if url_arg is not set
                if not url_arg:
                    url_arg = index_arg
                index_arg = None

        clear_mode = parsed.get("clear")
        list_mode = parsed.get("list")
        play_mode = parsed.get("play")
        pause_mode = parsed.get("pause")
        replace_mode = parsed.get("replace")
        save_mode = parsed.get("save")
        load_mode = parsed.get("load")
        current_mode = parsed.get("current")

        # Pure log mode: `.pipe -log` should not run any playlist actions and
        # should not print the playlist table. It should only enable/tail logs
        # (handled in the `finally` block).
        only_log = bool(
            log_requested and not url_arg and index_arg is None and not clear_mode
            and not list_mode and not play_mode and not pause_mode and not save_mode
            and not load_mode and not current_mode and not replace_mode
        )
        if only_log:
            return 0

        # Handle --current flag: emit currently playing item to pipeline
        if current_mode:
            items = _get_playlist()
            if items is None:
                debug("MPV is not running or not accessible.", file=sys.stderr)
                return 1

            # Find the currently playing item
            current_item = None
            for item in items:
                if item.get("current", False):
                    current_item = item
                    break

            if current_item is None:
                debug("No item is currently playing.", file=sys.stderr)
                return 1

            # Build result object with file info
            title = _resolve_playlist_display_title(current_item, config=config)
            filename = current_item.get("filename", "")

            # Emit the current item to pipeline
            result_obj = {
                "path": filename,
                "title": title,
                "cmdlet_name": ".mpv",
                "source": "pipe",
                "__pipe_index": items.index(current_item),
            }

            ctx.emit(result_obj)
            debug(f"Emitted current item: {title}")
            return 0

        # Handle URL queuing
        mpv_started = False
        if url_arg:
            # If -replace is used, or if we have a URL and -play is requested, 
            # we prefer 'replace' mode which starts playback immediately and avoids IPC overhead.
            # NOTE: Use wait=False for URLs because yt-dlp resolution can be slow and 
            # would cause the calling Lua script to timeout.
            queue_replace = bool(replace_mode)
            if play_mode and not replace_mode:
                # If -play is used with a URL, treat it as "play this now".
                # For better UX, we'll replace the current playlist.
                queue_replace = True

            mpv_started = _queue_items([url_arg], clear_first=queue_replace, config=config, start_opts=start_opts, wait=False)

            ctx.emit({"path": url_arg, "title": url_arg, "source": "load-url", "queued": True})

            if not (clear_mode or play_mode or pause_mode or save_mode or load_mode or replace_mode):
                play_mode = True
                if mpv_started:
                    import time
                    time.sleep(0.5)
                    index_arg = "1"
                else:
                    # If already running, we want to play the item we just added (last one).
                    # We need to fetch the current playlist to find the count.
                    current_playlist = _get_playlist(silent=True) or []
                    if current_playlist:
                        index_arg = str(len(current_playlist))

            if queue_replace:
                play_mode = False
                index_arg = None

            try:
                mpv = MPV()
                _ensure_lyric_overlay(mpv)
            except Exception:
                pass

        # Handle Save Playlist
        if save_mode:
            # Avoid `shell=True` / `date /t` on Windows (can flash a cmd.exe window).
            # Use Python's datetime instead.
            playlist_name = index_arg or f"Playlist {datetime.now().strftime('%Y-%m-%d')}"
            # If index_arg was used for name, clear it so it doesn't trigger index logic
            if index_arg:
                index_arg = None

            items = _get_playlist()
            if not items:
                debug("Cannot save: MPV playlist is empty or MPV is not running.")
                return 1

            # Clean up items for saving (remove current flag, etc)
            clean_items = []
            for item in items:
                # If title was extracted from memory://, we should probably save the original filename
                # if it's a URL, or reconstruct a clean object.
                # Actually, _extract_title_from_item handles the display title.
                # But for playback, we need the 'filename' (which might be memory://...)
                # If we save 'memory://...', it will work when loaded back.
                clean_items.append(item)

            if _save_playlist(playlist_name, clean_items):
                debug(f"Playlist saved as '{playlist_name}'")
                return 0
            debug(f"Failed to save playlist '{playlist_name}'")
            return 1

        # Handle Load Playlist
        current_playlist_name = None
        if load_mode:
            if index_arg:
                try:
                    pl_id = int(index_arg)

                    # Handle Delete Playlist (if -clear is also passed)
                    if clear_mode:
                        if _delete_playlist(pl_id):
                            debug(f"Playlist ID {pl_id} deleted.")
                            # Clear index_arg so we fall through to list mode and show updated list
                            index_arg = None
                            # Don't return, let it list the remaining playlists
                        else:
                            debug(f"Failed to delete playlist ID {pl_id}.")
                            return 1
                    else:
                        # Handle Load Playlist
                        result = _get_playlist_by_id(pl_id)
                        if result is None:
                            debug(f"Playlist ID {pl_id} not found.")
                            return 1

                        name, items = result
                        current_playlist_name = name

                        # Queue items (replacing current playlist)
                        if items:
                            _queue_items(
                                items,
                                clear_first=True,
                                config=config,
                                start_opts=start_opts
                            )
                        else:
                            # Empty playlist, just clear
                            _send_ipc_command(
                                {
                                    "command": ["playlist-clear"]
                                },
                                silent=True
                            )

                        # Switch to list mode to show the result
                        list_mode = True
                        index_arg = None
                        # Fall through to list logic

                except ValueError:
                    debug(f"Invalid playlist ID: {index_arg}")
                    return 1

            # If we deleted or didn't have an index, list playlists
            if not index_arg:
                playlists = _get_playlists()

                if not playlists:
                    debug("No saved playlists found.")
                    return 0

                table = Table("Saved Playlists")
                for i, pl in enumerate(playlists):
                    item_count = len(pl.get("items", []))
                    row = table.add_row()
                    # row.add_column("ID", str(pl['id'])) # Hidden as per user request
                    row.add_column("Name", pl["name"])
                    row.add_column("Items", str(item_count))
                    row.add_column("Updated", pl.get("updated_at") or "")

                    # Set the playlist items as the result object for this row
                    # When user selects @N, they get the list of items
                    # We also set the source command to .pipe -load <ID> so it loads it
                    table.set_row_selection_args(i, ["-load", str(pl["id"])])

                table.set_source_command(".mpv")

                # Register results
                ctx.set_last_result_table_overlay(
                    table,
                    [p["items"] for p in playlists]
                )
                ctx.set_current_stage_table(table)

                # Do not print directly here.
                # Both CmdletExecutor and PipelineExecutor render the current-stage/overlay table,
                # so printing here would duplicate output.
                return 0

        # Everything below was originally outside a try block; keep it inside so `start_opts` is in scope.

        # Handle Play/Pause commands (but skip if we have index_arg to play a specific item)
        if play_mode and index_arg is None:
            cmd = {
                "command": ["set_property",
                            "pause",
                            False],
                "request_id": 103
            }
            resp = _send_ipc_command(cmd)
            if resp and resp.get("error") == "success":
                debug("Resumed playback")
                return 0
            else:
                debug("Failed to resume playback (MPV not running?)", file=sys.stderr)
                return 1

        if pause_mode:
            cmd = {
                "command": ["set_property",
                            "pause",
                            True],
                "request_id": 104
            }
            resp = _send_ipc_command(cmd)
            if resp and resp.get("error") == "success":
                debug("Paused playback")
                return 0
            else:
                debug("Failed to pause playback (MPV not running?)", file=sys.stderr)
                return 1

        # Handle Clear All command (no index provided)
        if clear_mode and index_arg is None:
            cmd = {
                "command": ["playlist-clear"],
                "request_id": 105
            }
            resp = _send_ipc_command(cmd)
            if resp and resp.get("error") == "success":
                debug("Playlist cleared")
                return 0
            else:
                debug("Failed to clear playlist (MPV not running?)", file=sys.stderr)
                return 1

        # Handle piped input (add to playlist)
        # Skip adding if -list is specified (user just wants to see current playlist)
        if result and not list_mode and not url_arg:
            playlist_before = _get_playlist(silent=True)
            idle_before = None
            try:
                idle_resp = _send_ipc_command(
                    {
                        "command": ["get_property",
                                    "idle-active"],
                        "request_id": 111
                    },
                    silent=True
                )
                if idle_resp and idle_resp.get("error") == "success":
                    idle_before = bool(idle_resp.get("data"))
            except Exception:
                idle_before = None

            # If result is a list of items, add them to playlist
            items_to_add = []
            if isinstance(result, list):
                items_to_add = result
            elif isinstance(result, dict):
                items_to_add = [result]
            else:
                # Handle PipeObject or any other object type
                items_to_add = [result]

            # Debug: inspect incoming result and attributes
            try:
                debug(
                    f"pipe._run: received result type={type(result)} repr={repr(result)[:200]}"
                )
                debug(
                    f"pipe._run: attrs path={getattr(result, 'path', None)} url={getattr(result, 'url', None)} store={getattr(result, 'store', None)} hash={getattr(result, 'hash', None)}"
                )
            except Exception:
                pass

            queued_started_mpv = False
            if items_to_add and _queue_items(items_to_add,
                                             config=config,
                                             start_opts=start_opts):
                mpv_started = True
                queued_started_mpv = True

            # Ensure lyric overlay is running when we queue anything via .pipe.
            if items_to_add and not queued_started_mpv:
                try:
                    mpv = MPV()
                    _ensure_lyric_overlay(mpv)
                except Exception:
                    pass

            # Auto-play when a single item is piped and mpv was idle/empty.
            if items_to_add and len(items_to_add) == 1 and not queued_started_mpv:
                try:
                    playlist_after = _get_playlist(silent=True)
                    before_len = len(playlist_before
                                     ) if isinstance(playlist_before,
                                                     list) else 0
                    after_len = len(playlist_after
                                    ) if isinstance(playlist_after,
                                                    list) else 0

                    idx_to_play: Optional[int] = None
                    if after_len > before_len:
                        idx_to_play = min(max(0, before_len), after_len - 1)
                    elif idle_before is True and after_len > 0:
                        idx_to_play = after_len - 1
                    elif isinstance(playlist_before,
                                    list) and len(playlist_before) == 0 and after_len > 0:
                        idx_to_play = after_len - 1

                    if idx_to_play is not None:
                        # Prefer the store/hash from the piped item when auto-playing.
                        try:
                            s, h = _extract_store_and_hash(items_to_add[0], config=config)
                            _set_mpv_item_context(s, h)
                        except Exception:
                            pass
                        play_resp = _send_ipc_command(
                            {
                                "command": ["playlist-play-index",
                                            idx_to_play],
                                "request_id": 112
                            },
                            silent=True,
                        )
                        _send_ipc_command(
                            {
                                "command": ["set_property",
                                            "pause",
                                            False],
                                "request_id": 113
                            },
                            silent=True,
                        )
                        if play_resp and play_resp.get("error") == "success":
                            debug("Auto-playing piped item")

                        # Start lyric overlay (auto-discovery handled by MPV.lyric).
                        try:
                            mpv = MPV()
                            _ensure_lyric_overlay(mpv)
                        except Exception:
                            pass
                except Exception:
                    pass

        # Get playlist from MPV (silent: we handle MPV-not-running gracefully below)
        items = _get_playlist(silent=True)

        if items is None:
            mpv_process_alive = False
            try:
                mpv_process_alive = MPV().has_process_owner()
            except Exception:
                mpv_process_alive = False

            if mpv_started:
                # MPV was just started, retry getting playlist after a brief delay
                import time

                time.sleep(0.3)
                items = _get_playlist(silent=True)

                if items is None:
                    # Still can't connect, but MPV is starting
                    debug("MPV is starting up...")
                    return 0
            else:
                if mpv_process_alive:
                    debug("MPV is already running, but the Windows IPC pipe is busy. Not starting a second instance.")
                    return 0

                # Do not auto-launch MPV when no action/inputs were provided; avoid surprise startups
                no_inputs = not any(
                    [
                        result,
                        url_arg,
                        index_arg,
                        clear_mode,
                        play_mode,
                        pause_mode,
                        save_mode,
                        load_mode,
                        current_mode,
                        list_mode,
                    ]
                )

                if no_inputs:
                    # User invoked `.pipe` with no args: treat this as an intent to open MPV.
                    debug("MPV is not running. Starting new instance...")
                    _start_mpv([], config=config, start_opts=start_opts)

                    # Re-check playlist after startup; if IPC still isn't ready, just exit cleanly.
                    try:
                        import time

                        time.sleep(0.3)
                    except Exception:
                        pass
                    items = _get_playlist(silent=True)
                    if items is None:
                        debug("MPV is starting up...")
                        return 0

                    # IPC is ready; continue without restarting MPV again.
                else:
                    debug("MPV is not running. Starting new instance...")
                    _start_mpv([], config=config, start_opts=start_opts)
                    return 0

        if not items:
            debug("MPV playlist is empty.")
            return 0

        # If index is provided, perform action (Play or Clear)
        if index_arg is not None:
            try:
                # Handle 1-based index
                idx = int(index_arg) - 1

                if idx < 0 or idx >= len(items):
                    debug(f"Index {index_arg} out of range (1-{len(items)}).")
                    return 1

                item = items[idx]
                title = _resolve_playlist_display_title(
                    item,
                    config=config,
                    file_storage=file_storage,
                )
                filename = item.get("filename", "") if isinstance(item, dict) else ""
                hydrus_header = _build_hydrus_header(config or {})
                hydrus_url = None
                try:
                    hydrus_url = get_plugin_instance_value(config, "hydrusnetwork", "URL") if config is not None else None
                except Exception:
                    hydrus_url = None

                if clear_mode:
                    # Remove item
                    cmd = {
                        "command": ["playlist-remove",
                                    idx],
                        "request_id": 101
                    }
                    resp = _send_ipc_command(cmd)
                    if resp and resp.get("error") == "success":
                        debug(f"Removed: {title}")
                        # Refresh items for listing
                        items = _get_playlist() or []
                        list_mode = True
                        index_arg = None
                    else:
                        debug(
                            f"Failed to remove item: {resp.get('error') if resp else 'No response'}"
                        )
                        return 1
                else:
                    # Play item
                    try:
                        s, h = _extract_store_and_hash(item, config=config)
                        _set_mpv_item_context(s, h)
                    except Exception:
                        pass
                    if hydrus_header and _is_hydrus_path(filename, hydrus_url):
                        header_cmd = {
                            "command":
                            ["set_property",
                             "http-header-fields",
                             hydrus_header],
                            "request_id": 198,
                        }
                        _send_ipc_command(header_cmd, silent=True)
                    cmd = {
                        "command": ["playlist-play-index",
                                    idx],
                        "request_id": 102
                    }
                    resp = _send_ipc_command(cmd)
                    if resp and resp.get("error") == "success":
                        # Ensure playback starts (unpause)
                        unpause_cmd = {
                            "command": ["set_property",
                                        "pause",
                                        False],
                            "request_id": 103,
                        }
                        _send_ipc_command(unpause_cmd)

                        debug(f"Playing: {title}")

                        # Monitor logs briefly for errors (e.g. ytdl failures)
                        _monitor_mpv_logs(3.0)

                        # Refresh playlist view so the user sees the new current item immediately
                        items = _get_playlist(silent=True) or items
                        list_mode = True
                        index_arg = None
                    else:
                        debug(
                            f"Failed to play item: {resp.get('error') if resp else 'No response'}"
                        )
                        return 1
            except ValueError:
                debug(f"Invalid index: {index_arg}")
                return 1

        # List items (Default action or after clear)
        if list_mode or (index_arg is None and not url_arg):
            if not items:
                debug("MPV playlist is empty.")
                return 0

            if file_storage is None:
                try:
                    from PluginCore.backend_registry import BackendRegistry

                    file_storage = BackendRegistry(config)
                except Exception as e:
                    debug(
                        f"Warning: Could not initialize backend registry: {e}",
                        file=sys.stderr
                    )

            # Use the loaded playlist name if available, otherwise default
            # Note: current_playlist_name is defined in the load_mode block if a playlist was loaded
            try:
                table_title = current_playlist_name or "MPV Playlist"
            except NameError:
                table_title = "MPV Playlist"

            table = Table(table_title, preserve_order=True)

            # Convert MPV items to PipeObjects with proper hash and store
            pipe_objects = []
            title_cache: Dict[tuple[str, str, str], Optional[str]] = {}
            for i, item in enumerate(items):
                is_current = item.get("current", False)
                filename = item.get("filename", "")

                # Extract the real path/URL from memory:// wrapper if present
                real_path = _extract_target_from_memory_uri(filename) or filename

                # Try to extract hash/store from the path/URL via provider-aware normalization.
                store_name, file_hash = _extract_store_and_hash(
                    {
                        "path": real_path,
                        "filename": filename,
                    },
                    config=config,
                )

                # Check if it's a hash-based local file
                if not file_hash and real_path:
                    # Try to extract hash from filename (e.g., C:\path\1e8c46...a1b2.mp4)
                    path_obj = Path(real_path)
                    stem = path_obj.stem  # filename without extension
                    if len(stem) == 64 and all(c in "0123456789abcdef"
                                               for c in stem.lower()):
                        file_hash = stem.lower()

                # Fallback to inferred store if we couldn't find it
                if not store_name:
                    store_name = _infer_store_from_playlist_item(
                        item,
                        file_storage=file_storage,
                        config=config,
                    )

                title = _resolve_playlist_display_title(
                    item,
                    config=config,
                    file_storage=file_storage,
                    store_name=store_name,
                    file_hash=file_hash,
                    title_cache=title_cache,
                )

                # Build PipeObject with proper metadata
                pipe_obj = PipeObject(
                    hash=file_hash or "unknown",
                    store=store_name or "unknown",
                    title=title,
                    path=real_path,
                )
                pipe_objects.append(pipe_obj)

                # Truncate title for display
                display_title = title
                if len(display_title) > 80:
                    display_title = display_title[:77] + "..."

                row = table.add_row()
                row.add_column("Current", "*" if is_current else "")
                row.add_column("Instance", store_name or "unknown")
                row.add_column("Title", display_title)

                table.set_row_selection_args(i, [str(i + 1)])

            table.set_source_command(".mpv")

            # Register PipeObjects (not raw MPV items) with pipeline context
            ctx.set_last_result_table_overlay(table, pipe_objects)
            ctx.set_current_stage_table(table)

            # Do not print directly here.
            # Both CmdletExecutor and PipelineExecutor render the current-stage/overlay table,
            # so printing here would duplicate output.

        return 0
    finally:
        if log_requested and isinstance(mpv_log_path, str) and mpv_log_path.strip():
            try:
                # Give mpv a short moment to flush logs, then print a tail that is easy to copy.
                print(f"MPV log file: {mpv_log_path}")

                # Best-effort: re-try enabling file logging at the end too (mpv may have
                # been unreachable at the start).
                try:
                    _try_enable_mpv_file_logging(mpv_log_path, attempts=2)
                except Exception:
                    pass

                tail_lines: List[str] = []
                for _ in range(8):
                    tail_lines = _tail_text_file(mpv_log_path, max_lines=400, max_bytes=262144)
                    if tail_lines:
                        break
                    try:
                        import time

                        time.sleep(0.25)
                    except Exception:
                        break

                latest_run_tail = _slice_mpv_log_to_latest_run(tail_lines)
                filtered_tail = _focus_mpv_log_lines(
                    _apply_log_filter(latest_run_tail, log_filter_text)
                )

                helper_heartbeat = _get_mpv_property("user-data/medeia-pipeline-ready")
                helper_status = "not running"
                if helper_heartbeat not in (None, "", "0", False):
                    helper_status = f"running ({helper_heartbeat})"
                else:
                    try:
                        mpv_live = MPV()
                        if mpv_live.has_process_owner():
                            helper_status = "ipc busy or helper-owned connection"
                    except Exception:
                        pass

                print(f"Pipeline helper: {helper_status}")

                lua_version = _get_mpv_property("user-data/medeia-lua-version")
                lua_script = _get_mpv_property("user-data/medeia-lua-script")
                print(f"Medeia Lua: {lua_version or 'not loaded'}")
                if lua_script:
                    print(f"Medeia Lua script: {lua_script}")

                # Print database logs for mpv module (helper + lua output)
                try:
                    log_db_path = str(_repo_root() / "logs.db")
                    marker_ts, marker_msg, mpv_logs = _get_mpv_logs_for_latest_run(
                        log_db_path,
                        log_filter_text=log_filter_text,
                        limit=250,
                    )
                    if log_filter_text:
                        print(
                            f"MPV logs from database (latest run, filtered by '{log_filter_text}', chronological):"
                        )
                    else:
                        print("MPV logs from database (latest run, chronological):")
                    if marker_ts:
                        print(f"Latest run started: {marker_ts}")
                        if marker_msg:
                            print(f"Run marker: {marker_msg}")
                    if mpv_logs:
                        print("Medios MPV logs (latest run, focused):")
                        for line in _focus_db_log_rows(mpv_logs):
                            print(line)
                    else:
                        if log_filter_text:
                            print(f"(no latest-run mpv logs found matching '{log_filter_text}')")
                        else:
                            print("(no latest-run mpv logs found)")
                except Exception as e:
                    debug(f"Could not fetch database logs: {e}")
                    pass

                if filtered_tail:
                    title = "MPV core log (latest run, filtered)"
                    if log_filter_text:
                        title += f" for '{log_filter_text}'"
                    title += ":"
                    print(title)
                    for ln in filtered_tail:
                        print(ln)
                else:
                    if log_filter_text:
                        print(f"MPV core log: <no focused entries match filter '{log_filter_text}'>")
                    else:
                        print("MPV core log: <no focused entries>")

                try:
                    lua_tail = _tail_text_file(str(_lua_log_file()), max_lines=400, max_bytes=262144)
                except Exception:
                    lua_tail = []
                lua_tail = _slice_lua_log_to_latest_run(lua_tail)
                lua_tail = _collapse_repeated_log_lines(_apply_log_filter(lua_tail, log_filter_text))
                if lua_tail:
                    title = "Medeia Lua log (latest run)"
                    if log_filter_text:
                        title += f" filtered by '{log_filter_text}'"
                    title += ":"
                    print(title)
                    for line in lua_tail:
                        print(line)

                fallback_logs = [
                    ("Medeia helper log file tail", str(_helper_log_file())),
                ]
                for title, path in fallback_logs:
                    try:
                        lines = _tail_text_file(path, max_lines=120)
                    except Exception:
                        lines = []
                    if path == str(_helper_log_file()):
                        lines = _slice_helper_log_to_latest_run(lines)
                    lines = _collapse_repeated_log_lines(_apply_log_filter(lines, log_filter_text))
                    if not lines:
                        continue
                    print(f"{title}:")
                    for line in lines:
                        print(line)
            except Exception:
                pass
        try:
            set_thread_stream(prev_stream)
        except Exception:
            pass
        try:
            set_debug(prev_debug)
        except Exception:
            pass
        try:
            if devnull_fh is not None:
                devnull_fh.close()
        except Exception:
            pass


def _start_mpv(
    items: List[Any],
    config: Optional[Dict[str,
                          Any]] = None,
    start_opts: Optional[Dict[str,
                              Any]] = None,
) -> None:
    """Start MPV with a list of items."""
    import time as _time_module

    mpv = MPV()
    mpv.kill_existing_windows()
    _time_module.sleep(0.5)  # Wait for process to die

    hydrus_header = _build_hydrus_header(config or {})
    ytdl_opts = _build_ytdl_options(config, hydrus_header)

    _schedule_notes_prefetch(items[:1], config)

    if ytdl_opts:
        debug(f"Starting MPV with ytdl raw options: {ytdl_opts}")
    else:
        debug("Starting MPV with default yt-dlp raw options")

    try:
        extra_args: List[str] = []

        # If we are going to play a DASH MPD, allow ffmpeg to fetch https segments referenced by the manifest.
        try:
            needs_mpd_whitelist = False
            for it in items or []:
                mpd = _resolve_plugin_playback_path(it, config)
                candidate = mpd
                if not candidate:
                    if isinstance(it, dict):
                        candidate = it.get("path") or it.get("url")
                    else:
                        candidate = getattr(it, "path", None) or getattr(it, "url", None)
                if candidate and _MPD_PATH_RE.search(str(candidate).lower()):
                    needs_mpd_whitelist = True
                    break
            if needs_mpd_whitelist:
                extra_args.append(
                    "--demuxer-lavf-o=protocol_whitelist=file,https,tcp,tls,crypto,data"
                )
        except Exception:
            pass

        # Optional: borderless window (useful for uosc-like overlay UI without fullscreen).
        if start_opts and start_opts.get("borderless"):
            extra_args.append("--border=no")

        # Optional: mpv logging to file.
        mpv_log_path = (start_opts or {}).get("mpv_log_path")
        if isinstance(mpv_log_path, str) and mpv_log_path.strip():
            extra_args.append(f"--log-file={mpv_log_path}")
            extra_args.append("--msg-level=cplayer=info,ffmpeg=error,ipc=warn")

        # Always start MPV with the bundled Lua script via MPV class.
        mpv.start(
            extra_args=extra_args,
            ytdl_raw_options=ytdl_opts,
            http_header_fields=hydrus_header,
            detached=True,
        )
        debug("Started MPV process")

        # Wait for IPC pipe to be ready
        if not mpv.wait_for_ipc(retries=20, delay_seconds=0.2):
            debug("Timed out waiting for MPV IPC connection", file=sys.stderr)
            return

        # Publish context early so the lyric helper can resolve notes on the first
        # target change (the helper may start before playback begins).
        try:
            if items:
                s, h = _extract_store_and_hash(items[0], config=config)
                _set_mpv_item_context(s, h)
        except Exception:
            pass

        # main.lua is loaded at startup via --script; don't reload it here.

        # Ensure lyric overlay is running (auto-discovery handled by MPV.lyric).
        _ensure_lyric_overlay(mpv)

        # Queue items via IPC
        if items:
            _queue_items(items, config=config, start_opts=start_opts)

            # Auto-play the first item
            import time

            time.sleep(0.3)  # Give MPV a moment to process the queued items

            # Play the first item (index 0) and unpause
            play_cmd = {
                "command": ["playlist-play-index",
                            0],
                "request_id": 102
            }
            play_resp = _send_ipc_command(play_cmd, silent=True)

            if play_resp and play_resp.get("error") == "success":
                # Ensure playback starts (unpause)
                unpause_cmd = {
                    "command": ["set_property",
                                "pause",
                                False],
                    "request_id": 103
                }
                _send_ipc_command(unpause_cmd, silent=True)
                debug("Auto-playing first item")

            # Overlay already started above; it will follow track changes automatically.

    except Exception as e:
        debug(f"Error starting MPV: {e}", file=sys.stderr)

# pyright: ignore[reportCallIssue]
CMDLET = Cmdlet(
    name=".mpv",
    alias=[".pipe", "pipe", "playlist", "queue", "ls-pipe"],
    summary="Manage and play items in the MPV playlist via IPC",
    usage=".mpv [index|url] [-current] [-clear] [-list] [-url URL] [-log [filter text]] [-borderless]",
    arg=[
        CmdletArg(
            name="index",
            type="string",  # Changed to string to allow URL detection
            description="Index of item to play/clear, or URL to queue",
            required=False,
        ),
        CmdletArg(name="url", type="string", description="URL to queue", required=False),
        CmdletArg(
            name="clear",
            type="flag",
            description="Remove the selected item, or clear entire playlist if no index provided",
        ),
        CmdletArg(name="list", type="flag", description="List items (default)"),
        CmdletArg(name="play", type="flag", description="Resume playback or play specific index/URL"),
        CmdletArg(name="pause", type="flag", description="Pause playback"),
        CmdletArg(
            name="replace",
            type="flag",
            description="Replace current playlist when adding index or URL",
        ),
        CmdletArg(
            name="save",
            type="flag",
            description="Save current playlist to database",
            requires_db=True,
        ),
        CmdletArg(
            name="load",
            type="flag",
            description="List saved playlists",
            requires_db=True,
        ),
        CmdletArg(
            name="current",
            type="flag",
            description="Emit the currently playing item to pipeline for further processing",
        ),
        CmdletArg(
            name="log",
            type="flag",
            description="Enable pipeable debug output, write an mpv log file, and optionally specify a filter string right after -log to search stored mpv logs from the latest observed run",
        ),
        CmdletArg(
            name="borderless",
            type="flag",
            description="Start mpv with no window border (uosc-like overlay feel without fullscreen)",
        ),
    ],
    exec=_run,
)

COMMANDS = [CMDLET]
