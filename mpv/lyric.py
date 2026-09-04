r"""Timed lyric overlay for mpv via JSON IPC.

This is intentionally implemented from scratch (no vendored/copied code) while
providing the same *kind* of functionality as popular mpv lyric scripts:
- Parse LRC (timestamped lyrics)
- Track mpv playback time via IPC
- Show the current line on mpv's OSD

Primary intended usage in this repo:
- Auto mode (no stdin / no --lrc): loads lyrics from store notes.
    A lyric note is stored under the note name 'lyric'.
- If the lyric note is missing, auto mode will attempt to auto-fetch synced lyrics
    from a public API (LRCLIB) and store it into the 'lyric' note.
    You can disable this by setting config key `lyric_autofetch` to false.
- You can still pipe LRC into this script (stdin) and it will render lyrics in mpv.

Example (PowerShell):
    Get-Content .\song.lrc | python -m plugins.mpv.lyric

If you want to connect to a non-default mpv IPC server:
    Get-Content .\song.lrc | python -m plugins.mpv.lyric --ipc "\\.\pipe\mpv-custom"
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO
from urllib.parse import parse_qs, unquote, urlencode
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from plugins.mpv.mpv_ipc import MPV, MPVIPCClient

_TIMESTAMP_RE = re.compile(r"\[(?P<m>\d+):(?P<s>\d{2})(?:\.(?P<frac>\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"^\[offset:(?P<ms>[+-]?\d+)\]$", re.IGNORECASE)
_HASH_RE = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
_HYDRUS_HASH_QS_RE = re.compile(r"hash=([0-9a-f]{64})", re.IGNORECASE)

_WIN_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")
_WIN_UNC_RE = re.compile(r"^\\\\")

_LOG_FH: Optional[TextIO] = None
_SINGLE_INSTANCE_LOCK_FH: Optional[TextIO] = None

_LYRIC_VISIBLE_PROP = "user-data/medeia-lyric-visible"

# Optional overrides set by the playlist controller (.pipe/.mpv) so the lyric
# helper can resolve notes even when the local file path cannot be mapped back
# to a store via the store DB.
_ITEM_STORE_PROP = "user-data/medeia-item-store"
_ITEM_HASH_PROP = "user-data/medeia-item-hash"
_LEGACY_SUB_TRACK_TITLES = ("medeia-note-sub", "medeia-lyric-sub", "medeia-sub")
_NOTE_SUB_TRACK_TITLE = "medeia-note-sub"

# Note: We previously used `osd-overlay`, but some mpv builds return
# error='invalid parameter' for that command. We now use `show-text`, which is
# widely supported across mpv versions.

_OSD_STYLE_SAVED: Optional[Dict[str, Any]] = None
_OSD_STYLE_APPLIED: bool = False
_NOTES_CACHE_VERSION = 1
_DEFAULT_NOTES_CACHE_TTL_S = 900.0
_DEFAULT_NOTES_CACHE_WAIT_S = 1.5
_DEFAULT_NOTES_PENDING_WAIT_S = 12.0
_SUBTITLE_NOTE_ALIASES = ("subtitle", "subtitles", "transcript", "transcription")


def _single_instance_lock_path(ipc_path: str) -> Path:
    # Key the lock to the mpv IPC target so multiple mpv instances with different
    # IPC servers can still run independent lyric helpers.
    key = hashlib.sha1((ipc_path or "").encode("utf-8", errors="ignore")).hexdigest()
    tmp_dir = Path(tempfile.gettempdir())
    return (tmp_dir / f"medeia-mpv-lyric-{key}.lock").resolve()


def _acquire_single_instance_lock(ipc_path: str) -> bool:
    """Ensure only one MPV lyric process runs per IPC server.

    This prevents duplicate overlays (e.g. multiple lyric helpers racing to update OSD).
    """
    global _SINGLE_INSTANCE_LOCK_FH

    if _SINGLE_INSTANCE_LOCK_FH is not None:
        return True

    lock_path = _single_instance_lock_path(ipc_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        fh = open(lock_path, "a", encoding="utf-8", errors="replace")
    except Exception:
        # If we can't create the lock file, don't block playback; just proceed.
        return True

    try:
        if os.name == "nt":
            import msvcrt

            # Lock the first byte (non-blocking).
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        _SINGLE_INSTANCE_LOCK_FH = fh
        try:
            fh.write(f"pid={os.getpid()} ipc={ipc_path}\n")
            fh.flush()
        except Exception:
            pass
        return True
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return False


def _ass_escape(text: str) -> str:
    # Escape braces/backslashes so lyric text can't break ASS formatting.
    t = str(text or "")
    t = t.replace("\\", "\\\\")
    t = t.replace("{", "\\{")
    t = t.replace("}", "\\}")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\n", "\\N")
    return t


def _osd_set_text(client: MPVIPCClient, text: str, *, duration_ms: int = 1000) -> Optional[dict]:
    # Signature: show-text <string> [<duration-ms>] [<level>]
    # Duration 0 clears immediately; we generally set it to cover until next update.
    try:
        d = int(duration_ms)
    except Exception:
        d = 1000
    if d < 0:
        d = 0
    return client.send_command({
        "command": [
            "show-text",
            str(text or ""),
            d,
        ]
    })


def _osd_clear(client: MPVIPCClient) -> None:
    try:
        _osd_set_text(client, "", duration_ms=0)
    except Exception:
        return


def _log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    try:
        if _LOG_FH is not None:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
            return
    except Exception:
        pass

    print(line, file=sys.stderr, flush=True)


def _ipc_get_property(
    client: MPVIPCClient,
    name: str,
    default: object = None,
    *,
    raise_on_disconnect: bool = False,
) -> object:
    try:
        resp = client.send_command({
            "command": ["get_property",
                        name]
        })
    except Exception as exc:
        if raise_on_disconnect:
            raise ConnectionError(f"Lost mpv IPC connection: {exc}") from exc
        return default
    if resp is None:
        if raise_on_disconnect:
            raise ConnectionError("Lost mpv IPC connection")
        return default
    if resp and resp.get("error") == "success":
        return resp.get("data", default)
    return default


def _ipc_set_property(client: MPVIPCClient, name: str, value: Any) -> bool:
    resp = client.send_command({
        "command": ["set_property",
                    name,
                    value]
    })
    return bool(resp and resp.get("error") == "success")


def _osd_capture_style(client: MPVIPCClient) -> Dict[str, Any]:
    keys = [
        "osd-align-x",
        "osd-align-y",
        "osd-font-size",
        "osd-margin-y",
    ]
    out: Dict[str, Any] = {}
    for k in keys:
        try:
            out[k] = _ipc_get_property(client, k, None)
        except Exception:
            out[k] = None
    return out


def _osd_apply_lyric_style(client: MPVIPCClient, *, config: Dict[str, Any]) -> None:
    """Apply bottom-center + larger font for lyric show-text messages.

    This modifies mpv's global OSD settings, so we save and restore them.
    """
    global _OSD_STYLE_SAVED, _OSD_STYLE_APPLIED

    if not _OSD_STYLE_APPLIED:
        if _OSD_STYLE_SAVED is None:
            _OSD_STYLE_SAVED = _osd_capture_style(client)

        try:
            _ipc_set_property(client, "osd-align-x", "center")
            _ipc_set_property(client, "osd-align-y", "bottom")

            scale = config.get("lyric_osd_font_scale", 1.15)
            try:
                scale_f = float(scale)
            except Exception:
                scale_f = 1.15
            if scale_f < 1.0:
                scale_f = 1.0

            old_size = None
            try:
                if _OSD_STYLE_SAVED is not None:
                    old_size = _OSD_STYLE_SAVED.get("osd-font-size")
            except Exception:
                old_size = None
            if isinstance(old_size, (int, float)):
                new_size = int(max(10, round(float(old_size) * scale_f)))
            else:
                # mpv default is typically ~55; choose a conservative readable size.
                new_size = int(config.get("lyric_osd_font_size", 64))

            _ipc_set_property(client, "osd-font-size", new_size)

            min_margin_y = int(config.get("lyric_osd_min_margin_y", 60))
            old_margin_y = None
            try:
                if _OSD_STYLE_SAVED is not None:
                    old_margin_y = _OSD_STYLE_SAVED.get("osd-margin-y")
            except Exception:
                old_margin_y = None
            if isinstance(old_margin_y, (int, float)):
                _ipc_set_property(client, "osd-margin-y", int(max(old_margin_y, min_margin_y)))
            else:
                _ipc_set_property(client, "osd-margin-y", min_margin_y)
        except Exception:
            return

        _OSD_STYLE_APPLIED = True


def _osd_restore_style(client: MPVIPCClient) -> None:
    global _OSD_STYLE_SAVED, _OSD_STYLE_APPLIED

    if not _OSD_STYLE_APPLIED:
        return

    try:
        saved = _OSD_STYLE_SAVED or {}
        for k, v in saved.items():
            if v is None:
                continue
            try:
                _ipc_set_property(client, k, v)
            except Exception:
                pass
    finally:
        _OSD_STYLE_APPLIED = False


def _osd_clear_and_restore(client: MPVIPCClient) -> None:
    """Clear OSD text and restore any saved OSD style in a single call."""
    _osd_clear(client)
    _osd_restore_style(client)


def _http_get_json_raw(url: str, *, timeout_s: float = 10.0) -> Optional[Any]:
    """HTTP GET and JSON-decode; returns the parsed value (dict, list, etc.) or None on any failure."""
    try:
        req = Request(
            url,
            headers={
                "User-Agent": "medeia-macina/lyric",
                "Accept": "application/json",
            },
            method="GET",
        )
        with urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
        import json
        return json.loads(data.decode("utf-8", errors="replace"))
    except Exception as exc:
        _log(f"HTTP JSON failed: {exc} ({url})")
        return None


def _http_get_json(url: str, *, timeout_s: float = 10.0) -> Optional[dict]:
    """HTTP GET returning a JSON object (dict), or None."""
    obj = _http_get_json_raw(url, timeout_s=timeout_s)
    return obj if isinstance(obj, dict) else None


def _http_get_json_list(url: str, *, timeout_s: float = 10.0) -> Optional[list]:
    """HTTP GET returning a JSON array (list), or None."""
    obj = _http_get_json_raw(url, timeout_s=timeout_s)
    return obj if isinstance(obj, list) else None


def _sanitize_query(s: Optional[str]) -> Optional[str]:
    if not isinstance(s, str):
        return None
    t = s.strip().strip("\ufeff")
    return t if t else None


def _infer_artist_title_from_tags(
    tags: List[str]
) -> tuple[Optional[str],
           Optional[str]]:
    artist = None
    title = None
    for t in tags or []:
        ts = str(t)
        low = ts.lower()
        if low.startswith("artist:") and artist is None:
            artist = ts.split(":", 1)[1].strip() or None
        elif low.startswith("title:") and title is None:
            title = ts.split(":", 1)[1].strip() or None
        if artist and title:
            break
    return _sanitize_query(artist), _sanitize_query(title)


def _wrap_plain_lyrics_as_lrc(text: str) -> str:
    # Fallback: create a crude LRC that advances every 4 seconds.
    # This is intentionally simple and deterministic.
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    out: List[str] = []
    t_s = 0
    for ln in lines:
        mm = t_s // 60
        ss = t_s % 60
        out.append(f"[{mm:02d}:{ss:02d}.00]{ln}")
        t_s += 4
    return "\n".join(out) + "\n"


def _fetch_lrclib(
    *,
    artist: Optional[str],
    title: Optional[str],
    duration_s: Optional[float] = None
) -> Optional[str]:
    base = "https://lrclib.net/api"

    # Require both artist and title; title-only lookups cause frequent mismatches.
    if not artist or not title:
        return None

    # Try direct get.
    q: Dict[str,
            str] = {
                "artist_name": artist,
                "track_name": title,
            }
    if isinstance(duration_s, (int, float)) and duration_s and duration_s > 0:
        q["duration"] = str(int(duration_s))
    url = f"{base}/get?{urlencode(q)}"
    obj = _http_get_json(url)
    if isinstance(obj, dict):
        synced = obj.get("syncedLyrics")
        if isinstance(synced, str) and synced.strip():
            _log("LRCLIB: got syncedLyrics")
            return synced
        plain = obj.get("plainLyrics")
        if isinstance(plain, str) and plain.strip():
            _log("LRCLIB: only plainLyrics; wrapping")
            wrapped = _wrap_plain_lyrics_as_lrc(plain)
            return wrapped if wrapped.strip() else None

    # Fallback: search using artist+title only.
    q_text = f"{artist} {title}"
    url = f"{base}/search?{urlencode({'q': q_text})}"
    items = _http_get_json_list(url) or []
    for item in items:
        if not isinstance(item, dict):
            continue
        synced = item.get("syncedLyrics")
        if isinstance(synced, str) and synced.strip():
            _log("LRCLIB: search hit with syncedLyrics")
            return synced
    # Plain lyrics fallback from search if available
    for item in items:
        if not isinstance(item, dict):
            continue
        plain = item.get("plainLyrics")
        if isinstance(plain, str) and plain.strip():
            _log("LRCLIB: search hit only plainLyrics; wrapping")
            wrapped = _wrap_plain_lyrics_as_lrc(plain)
            return wrapped if wrapped.strip() else None

    return None


def _fetch_lyrics_ovh(*, artist: Optional[str], title: Optional[str]) -> Optional[str]:
    # Public, no-auth lyrics provider (typically plain lyrics, not time-synced).
    if not artist or not title:
        return None
    try:
        # Endpoint uses path segments, so we urlencode each part.
        from urllib.parse import quote

        url = f"https://api.lyrics.ovh/v1/{quote(artist)}/{quote(title)}"
        obj = _http_get_json(url)
        if not isinstance(obj, dict):
            return None
        lyr = obj.get("lyrics")
        if isinstance(lyr, str) and lyr.strip():
            _log("lyrics.ovh: got plain lyrics; wrapping")
            wrapped = _wrap_plain_lyrics_as_lrc(lyr)
            return wrapped if wrapped.strip() else None
    except Exception as exc:
        _log(f"lyrics.ovh failed: {exc}")
    return None


@dataclass(frozen=True)
class LrcLine:
    time_s: float
    text: str


def _frac_to_ms(frac: str) -> int:
    # LRC commonly uses centiseconds (2 digits), but can be 1–3 digits.
    if not frac:
        return 0
    if len(frac) == 3:
        return int(frac)
    if len(frac) == 2:
        return int(frac) * 10
    return int(frac) * 100


def parse_lrc(text: str) -> List[LrcLine]:
    """Parse LRC into sorted timestamped lines."""
    offset_ms = 0
    lines: List[LrcLine] = []

    for raw_line in text.splitlines():
        line = raw_line.strip("\ufeff\r\n")
        if not line:
            continue

        # Optional global offset.
        off_m = _OFFSET_RE.match(line)
        if off_m:
            try:
                offset_ms = int(off_m.group("ms"))
            except Exception:
                offset_ms = 0
            continue

        matches = list(_TIMESTAMP_RE.finditer(line))
        if not matches:
            # Ignore non-timestamp metadata lines like [ar:], [ti:], etc.
            continue

        lyric_text = line[matches[-1].end():].strip()
        for m in matches:
            mm = int(m.group("m"))
            ss = int(m.group("s"))
            frac = m.group("frac") or ""
            ts_ms = (mm * 60 + ss) * 1000 + _frac_to_ms(frac) + offset_ms
            if ts_ms < 0:
                continue
            lines.append(LrcLine(time_s=ts_ms / 1000.0, text=lyric_text))

    # Sort and de-dupe by timestamp (prefer last non-empty text).
    lines.sort(key=lambda x: x.time_s)
    deduped: List[LrcLine] = []
    for item in lines:
        if deduped and abs(deduped[-1].time_s - item.time_s) < 1e-6:
            if item.text:
                deduped[-1] = item
        else:
            deduped.append(item)
    return deduped


def _read_all_stdin() -> str:
    return sys.stdin.read()


def _current_index(time_s: float, times: List[float]) -> int:
    # Index of last timestamp <= time_s
    return bisect.bisect_right(times, time_s) - 1


def _lyric_duration_ms(idx: int, times: List[float], current_t: float) -> int:
    """Duration in ms to display the lyric at *idx* — until the next timestamp or a safe maximum."""
    try:
        if idx + 1 < len(times):
            return int(max(250, min(8000, (times[idx + 1] - current_t) * 1000)))
    except Exception:
        pass
    return 1200


def _format_vtt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds or 0.0) * 1000.0)))
    hours = total_ms // 3600000
    minutes = (total_ms // 60000) % 60
    secs = (total_ms // 1000) % 60
    millis = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _lrc_entries_to_vtt_text(entries: List[LrcLine]) -> str:
    if not entries:
        return "WEBVTT\n\n"

    lines: List[str] = ["WEBVTT", ""]
    times = [entry.time_s for entry in entries]
    for idx, entry in enumerate(entries, start=1):
        start_s = max(0.0, float(entry.time_s or 0.0))
        if idx < len(entries):
            end_s = max(start_s + 0.25, float(times[idx]))
        else:
            end_s = start_s + 1.2

        text = str(entry.text or "").replace("\r\n", "\n").replace("\r", "\n")
        cue_text = text if text.strip() else " "

        lines.append(str(idx))
        lines.append(f"{_format_vtt_timestamp(start_s)} --> {_format_vtt_timestamp(end_s)}")
        lines.extend(cue_text.split("\n"))
        lines.append("")

    return "\n".join(lines)


def _unwrap_memory_m3u(text: Optional[str]) -> Optional[str]:
    """Extract the real target URL/path from a memory:// M3U payload."""
    if not isinstance(text, str) or not text.startswith("memory://"):
        return text
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("memory://"):
            continue
        return s
    return text


def _extract_hash_from_target(target: str) -> Optional[str]:
    if not isinstance(target, str):
        return None
    m = _HYDRUS_HASH_QS_RE.search(target)
    if m:
        return m.group(1).lower()

    # Fallback: plain hash string
    s = target.strip().lower()
    if _HASH_RE.fullmatch(s):
        return s
    return None


def _load_config_best_effort() -> dict:
    try:
        from SYS.config import load_config

        cfg = load_config()

        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _cache_float_config(config: Optional[dict], key: str, default: float) -> float:
    try:
        raw = (config or {}).get(key)
        if raw is None:
            return float(default)
        value = float(raw)
        if value < 0:
            return 0.0
        return value
    except Exception:
        return float(default)


def _notes_cache_root() -> Path:
    root = Path(tempfile.gettempdir()) / "medeia-mpv-notes" / "cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _generated_sub_root() -> Path:
    root = Path(tempfile.gettempdir()) / "medeia-mpv-notes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _notes_cache_key(store: str, file_hash: str) -> str:
    return hashlib.sha1(
        f"{str(store or '').strip().lower()}:{str(file_hash or '').strip().lower()}".encode(
            "utf-8",
            errors="ignore",
        )
    ).hexdigest()


def _notes_cache_path(store: str, file_hash: str) -> Path:
    return (_notes_cache_root() / f"notes-{_notes_cache_key(store, file_hash)}.json").resolve()


def _notes_pending_path(store: str, file_hash: str) -> Path:
    return (_notes_cache_root() / f"notes-{_notes_cache_key(store, file_hash)}.pending").resolve()


def _normalize_notes_payload(notes: Any) -> Dict[str, str]:
    if not isinstance(notes, dict):
        return {}
    return {
        str(k): str(v or "")
        for k, v in notes.items()
        if str(k).strip()
    }


def load_cached_notes(
    store: Optional[str],
    file_hash: Optional[str],
    *,
    config: Optional[dict] = None,
) -> Optional[Dict[str, str]]:
    if not store or not file_hash:
        return None

    path = _notes_cache_path(str(store), str(file_hash))
    if not path.exists():
        return None

    ttl_s = _cache_float_config(config, "lyric_notes_cache_ttl_seconds", _DEFAULT_NOTES_CACHE_TTL_S)
    if ttl_s > 0:
        try:
            age_s = max(0.0, time.time() - float(path.stat().st_mtime))
            if age_s > ttl_s:
                return None
        except Exception:
            return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    if int(payload.get("version") or 0) != _NOTES_CACHE_VERSION:
        return None

    return _normalize_notes_payload(payload.get("notes"))


def store_cached_notes(
    store: Optional[str],
    file_hash: Optional[str],
    notes: Any,
) -> bool:
    if not store or not file_hash:
        return False

    normalized = _normalize_notes_payload(notes)
    path = _notes_cache_path(str(store), str(file_hash))
    tmp_path = path.with_suffix(".tmp")
    payload = {
        "version": _NOTES_CACHE_VERSION,
        "saved_at": time.time(),
        "store": str(store),
        "hash": str(file_hash),
        "notes": normalized,
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            errors="replace",
        )
        tmp_path.replace(path)
        return True
    except Exception:
        return False


def set_notes_prefetch_pending(
    store: Optional[str],
    file_hash: Optional[str],
    pending: bool,
) -> None:
    if not store or not file_hash:
        return

    path = _notes_pending_path(str(store), str(file_hash))
    if pending:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(time.time()), encoding="utf-8", errors="replace")
        except Exception:
            return
        return

    try:
        if path.exists():
            path.unlink()
    except Exception:
        return


def is_notes_prefetch_pending(
    store: Optional[str],
    file_hash: Optional[str],
    *,
    stale_after_s: float = 60.0,
) -> bool:
    if not store or not file_hash:
        return False

    path = _notes_pending_path(str(store), str(file_hash))
    if not path.exists():
        return False

    try:
        age_s = max(0.0, time.time() - float(path.stat().st_mtime))
        if stale_after_s > 0 and age_s > stale_after_s:
            path.unlink(missing_ok=True)
            return False
    except Exception:
        return False

    return True


def _infer_artist_title_from_mpv(client: MPVIPCClient) -> tuple[Optional[str], Optional[str]]:
    artist = None
    title = None

    artist_keys = [
        "metadata/by-key/artist",
        "metadata/by-key/Artist",
        "metadata/by-key/album_artist",
        "metadata/by-key/ALBUMARTIST",
    ]
    title_keys = [
        "metadata/by-key/title",
        "metadata/by-key/Title",
        "media-title",
    ]

    for key in artist_keys:
        try:
            value = _ipc_get_property(client, key, None)
        except Exception:
            value = None
        artist = _sanitize_query(str(value) if isinstance(value, str) else None)
        if artist:
            break

    for key in title_keys:
        try:
            value = _ipc_get_property(client, key, None)
        except Exception:
            value = None
        title = _sanitize_query(str(value) if isinstance(value, str) else None)
        if title:
            break

    return artist, title


def _extract_note_text(notes: Dict[str, str], name: str) -> Optional[str]:
    """Return stripped text from the note named *name*, or None if absent or blank."""
    if not isinstance(notes, dict) or not notes:
        return None
    raw = None
    for k, v in notes.items():
        if isinstance(k, str) and k.strip() == name:
            raw = v
            break
    if not isinstance(raw, str):
        return None
    text = raw.strip("\ufeff\r\n")
    return text if text.strip() else None


def _extract_first_note_text(
    notes: Dict[str, str],
    names: List[str],
    *,
    predicate: Optional[Any] = None,
) -> tuple[Optional[str], Optional[str]]:
    for name in names:
        candidate = _extract_note_text(notes, name)
        if not candidate:
            continue
        if predicate is not None:
            try:
                if not bool(predicate(candidate)):
                    continue
            except Exception:
                continue
        return name, candidate
    return None, None


def _extract_lrc_from_notes(notes: Dict[str, str]) -> Optional[str]:
    """Return raw LRC text from the note named 'lyric'."""
    return _extract_note_text(notes, "lyric")


def _looks_like_subtitle_text(text: str) -> bool:
    t = (text or "").lstrip("\ufeff\r\n").lstrip()
    if not t:
        return False
    upper = t.upper()
    if upper.startswith("WEBVTT"):
        return True
    if upper.startswith("[SCRIPT INFO]"):
        return True
    if "-->" in t:
        return True
    if re.search(r"(?m)^Dialogue:\s*", t):
        return True
    return False


def _extract_sub_from_notes(notes: Dict[str, str]) -> tuple[Optional[str], Optional[str]]:
    """Return (note_name, subtitle_text) from note-backed subtitle/transcript keys."""
    primary = _extract_note_text(notes, "sub")
    if primary:
        return "sub", primary
    return _extract_first_note_text(
        notes,
        list(_SUBTITLE_NOTE_ALIASES),
        predicate=_looks_like_subtitle_text,
    )


def _display_note_name(note_name: Optional[str]) -> str:
    text = re.sub(r"\s+", " ", str(note_name or "").replace("_", " ")).strip()
    if not text:
        return "subtitle"
    lowered = text.casefold()
    if lowered == "lyric":
        return "lyrics"
    if lowered == "sub":
        return "subtitles"
    return text


def _display_media_title(client: MPVIPCClient) -> Optional[str]:
    for key in ("metadata/by-key/title", "metadata/by-key/Title", "media-title"):
        try:
            value = _ipc_get_property(client, key, None)
        except Exception:
            value = None
        if isinstance(value, str):
            text = re.sub(r"\s+", " ", value).strip()
            if text:
                return text
    return None


def _generated_subtitle_title(client: MPVIPCClient, *, note_name: Optional[str]) -> str:
    note_label = _display_note_name(note_name)
    media_title = _display_media_title(client)
    if media_title:
        title = f"{note_label}: {media_title}"
    else:
        title = note_label
    title = re.sub(r"\s+", " ", title).strip()
    return title[:96] if len(title) > 96 else title


def _filename_slug(text: Optional[str], *, default: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._ -]+", " ", str(text or ""))
    value = re.sub(r"\s+", "-", value).strip("- ._")
    value = value[:48]
    return value or default


def _infer_sub_extension(text: str) -> str:
    # Best-effort: mpv generally understands SRT/VTT; choose based on content.
    t = (text or "").lstrip("\ufeff\r\n").lstrip()
    upper = t.upper()
    if upper.startswith("WEBVTT"):
        return ".vtt"
    if upper.startswith("[SCRIPT INFO]") or re.search(r"(?m)^Dialogue:\s*", t):
        return ".ass"
    if "-->" in t:
        # SRT typically uses commas for milliseconds, VTT uses dots.
        if re.search(r"\d\d:\d\d:\d\d,\d\d\d\s*-->\s*\d\d:\d\d:\d\d,\d\d\d", t):
            return ".srt"
        return ".vtt"
    return ".vtt"


def _write_temp_sub_file(*, key: str, text: str, label: Optional[str] = None) -> Path:
    # Write to a content-addressed temp path so updates force mpv reload.
    tmp_dir = _generated_sub_root()

    ext = _infer_sub_extension(text)
    digest = hashlib.sha1((key + "\n" + (text or "")).encode("utf-8",
                                                             errors="ignore")
                          ).hexdigest()[:16]
    prefix = _filename_slug(label, default="subtitle")
    path = (tmp_dir / f"{prefix}-{digest}{ext}").resolve()
    path.write_text(text or "", encoding="utf-8", errors="replace")
    return path


def _subtitle_track_snapshot(client: MPVIPCClient) -> List[Dict[str, Any]]:
    raw = _ipc_get_property(client, "track-list", [])
    return raw if isinstance(raw, list) else []


def _track_external_sub_path(track: Dict[str, Any]) -> Optional[Path]:
    if not isinstance(track, dict):
        return None
    for key in ("external-filename", "external_filename", "demux-filename", "demux_filename"):
        raw = track.get(key)
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue
        try:
            return Path(text).expanduser().resolve()
        except Exception:
            return Path(text)
    return None


def _is_medeia_generated_sub_track(track: Dict[str, Any]) -> bool:
    if not isinstance(track, dict):
        return False
    title = str(track.get("title") or "").strip()
    if title in _LEGACY_SUB_TRACK_TITLES:
        return True
    path = _track_external_sub_path(track)
    if path is None:
        return False
    try:
        path.relative_to(_generated_sub_root().resolve())
        return True
    except Exception:
        return False


def _find_medeia_sub_track_ids(client: MPVIPCClient) -> List[int]:
    out: List[int] = []
    for track in _subtitle_track_snapshot(client):
        if not isinstance(track, dict):
            continue
        if str(track.get("type") or "") != "sub":
            continue
        if not _is_medeia_generated_sub_track(track):
            continue
        try:
            track_id = int(track.get("id"))
        except Exception:
            continue
        out.append(track_id)
    return out


def _log_medeia_sub_tracks(client: MPVIPCClient, reason: str) -> None:
    parts: List[str] = []
    for track in _subtitle_track_snapshot(client):
        if not isinstance(track, dict):
            continue
        if str(track.get("type") or "") != "sub":
            continue
        if not _is_medeia_generated_sub_track(track):
            continue
        title = str(track.get("title") or "").strip()
        source = _track_external_sub_path(track)
        parts.append(
            f"id={track.get('id')}"
            f" title={title!r}"
            f" selected={bool(track.get('selected'))}"
            f" external={bool(track.get('external'))}"
            f" source={source.name if source is not None else '<none>'}"
        )
    if parts:
        _log(f"Medeia subtitle tracks {reason}: " + " | ".join(parts))
    else:
        _log(f"Medeia subtitle tracks {reason}: <none>")


def _remove_medeia_external_subs(client: MPVIPCClient, *, reason: str = "") -> None:
    track_ids = _find_medeia_sub_track_ids(client)
    if not track_ids:
        return
    _log(f"Removing Medeia subtitle tracks reason={reason or 'unknown'} ids={track_ids}")
    for track_id in track_ids:
        try:
            client.send_command({
                "command": ["sub-remove", int(track_id)]
            })
        except Exception:
            continue
    _log_medeia_sub_tracks(client, f"after-remove:{reason or 'unknown'}")


def _try_add_external_sub(client: MPVIPCClient, path: Path, *, title: str) -> None:
    try:
        client.send_command(
            {
                "command": ["sub-add",
                            str(path),
                            "select",
                            str(title or _NOTE_SUB_TRACK_TITLE)]
            }
        )
    except Exception:
        return


def _is_stream_target(target: str) -> bool:
    """Return True when mpv's 'path' is not a local filesystem file.

    We intentionally treat any URL/streaming scheme as invalid for lyrics in auto mode.
    """
    if not isinstance(target, str):
        return False
    s = target.strip()
    if not s:
        return False

    # Windows local paths: drive letter or UNC.
    if _WIN_DRIVE_RE.match(s) or _WIN_UNC_RE.match(s):
        return False

    # Common streaming prefixes.
    if s.startswith("http://") or s.startswith("https://"):
        return True

    # Generic scheme:// (e.g. ytdl://, edl://, rtmp://, etc.).
    if "://" in s:
        try:
            parsed = urlparse(s)
            scheme = (parsed.scheme or "").lower()
            if scheme and scheme not in {"file"}:
                return True
        except Exception:
            return True

    return False


def _normalize_file_uri_target(target: str) -> str:
    """Convert file:// URIs to a local filesystem path string when possible."""
    if not isinstance(target, str):
        return target
    s = target.strip()
    if not s:
        return target
    if not s.lower().startswith("file://"):
        return target

    try:
        parsed = urlparse(s)
        path = unquote(parsed.path or "")

        if os.name == "nt":
            # UNC: file://server/share/path -> \\server\share\path
            if parsed.netloc:
                p = path.replace("/", "\\")
                if p.startswith("\\"):
                    p = p.lstrip("\\")
                return f"\\\\{parsed.netloc}\\{p}" if p else f"\\\\{parsed.netloc}"

            # Drive letter: file:///C:/path -> C:/path
            if path.startswith("/") and len(path) >= 3 and path[2] == ":":
                path = path[1:]

        return path or target
    except Exception:
        return target


def _extract_store_from_url_target(target: str) -> Optional[str]:
    """Extract explicit store name from a URL query param `store=...` (if present)."""
    if not isinstance(target, str):
        return None
    s = target.strip()
    if not (s.startswith("http://") or s.startswith("https://")):
        return None
    try:
        parsed = urlparse(s)
        if not parsed.query:
            return None
        qs = parse_qs(parsed.query)
        raw = qs.get("store", [None])[0]
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    except Exception:
        return None
    return None


def _infer_hydrus_store_from_url_target(*, target: str, config: dict) -> Optional[str]:
    """Infer a Hydrus store backend by matching the URL prefix to the backend base URL."""
    if not isinstance(target, str):
        return None
    s = target.strip()
    if not (s.startswith("http://") or s.startswith("https://")):
        return None

    try:
        from PluginCore.backend_registry import BackendRegistry

        backend_registry = BackendRegistry(config, suppress_debug=True)
        backends = [(name, backend_registry[name]) for name in backend_registry.list_backends()]
    except Exception:
        return None

    matches: List[str] = []
    for name, backend in backends:
        backend_type = str(getattr(backend, "STORE_TYPE", "") or "").strip().lower()
        backend_class = type(backend).__name__.strip().lower()
        is_hydrus_backend = backend_type == "hydrusnetwork" or backend_class == "hydrusnetwork"
        if not is_hydrus_backend:
            try:
                from PluginCore.registry import get_plugin

                hydrus_provider = get_plugin("hydrusnetwork", config)
                checker = getattr(hydrus_provider, "is_backend", None) if hydrus_provider is not None else None
                if callable(checker):
                    is_hydrus_backend = bool(checker(backend, name))
            except Exception:
                is_hydrus_backend = False
        if not is_hydrus_backend:
            continue
        base_url = getattr(backend, "_url", None)
        if not base_url:
            client = getattr(backend, "_client", None)
            base_url = getattr(client, "url", None) if client else None
        if not base_url:
            continue
        base = str(base_url).rstrip("/")
        if s.startswith(base):
            matches.append(name)

    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_store_backend_for_target(
    *,
    target: str,
    file_hash: str,
    config: dict,
) -> tuple[Optional[str],
           Any]:
    """Resolve a store backend for a local mpv target using the store DB.

    A target is considered valid only when:
    - target is a local filesystem file
    - a backend's get_file(hash) returns a local file path
    - that path resolves to the same target path
    """
    try:
        p = Path(target)
        if not p.exists() or not p.is_file():
            return None, None
        target_resolved = p.resolve()
    except Exception:
        return None, None

    try:
        from PluginCore.backend_registry import BackendRegistry

        backend_registry = BackendRegistry(config, suppress_debug=True)
        backend_names = list(backend_registry.list_backends())
    except Exception:
        return None, None



    for name in backend_names:
        try:
            backend = backend_registry[name]
        except Exception:
            continue

        store_file = None
        try:
            store_file = backend.get_file(file_hash, config=config)
        except TypeError:
            try:
                store_file = backend.get_file(file_hash)
            except Exception:
                store_file = None
        except Exception:
            store_file = None

        if not store_file:
            continue

        # Only accept local files; if the backend returns a URL, it's not valid for lyrics.
        try:
            store_path = Path(str(store_file)).expanduser()
            if not store_path.exists() or not store_path.is_file():
                continue
            if store_path.resolve() != target_resolved:
                continue
        except Exception:
            continue

        return name, backend

    return None, None


def _infer_hash_for_target(target: str) -> Optional[str]:
    """Infer SHA256 hash from Hydrus URL query, hash-named local files, or by hashing local file content."""
    h = _extract_hash_from_target(target)
    if h:
        return h

    try:
        p = Path(target)
        if not p.exists() or not p.is_file():
            return None
        stem = p.stem
        if isinstance(stem, str) and _HASH_RE.fullmatch(stem.strip()):
            return stem.strip().lower()
        from SYS.utils import sha256_file

        return sha256_file(p)
    except Exception:
        return None


@dataclass
class _PlaybackState:
    """Mutable per-track resolution state for the auto overlay loop.

    Centralising these variables in one object eliminates the repeated
    15-line 'reset everything + clear OSD + remove sub' block that
    previously appeared five times inside run_auto_overlay.
    """

    store_name: Optional[str] = None
    file_hash: Optional[str] = None
    key: Optional[str] = None
    backend: Optional[Any] = None
    entries: List[LrcLine] = field(default_factory=list)
    times: List[float] = field(default_factory=list)
    loaded_key: Optional[str] = None
    loaded_mode: Optional[str] = None          # 'lyric' | 'sub' | 'lyric-sub' | None
    loaded_sub_path: Optional[Path] = None
    last_target: Optional[str] = None
    fetch_attempt_key: Optional[str] = None
    fetch_attempt_at: float = 0.0
    cache_wait_key: Optional[str] = None
    cache_wait_started_at: float = 0.0
    cache_wait_next_probe_at: float = 0.0

    def clear(self, client: MPVIPCClient, *, clear_hash: bool = True) -> None:
        """Reset backend resolution and clean up any active OSD / external subtitle.

        Pass ``clear_hash=False`` to preserve *file_hash* when the hash is
        still valid but the store lookup failed (e.g. store temporarily
        unavailable), so the late-arriving-context fallback can retry later.
        """
        self.store_name = None
        self.backend = None
        self.key = None
        if clear_hash:
            self.file_hash = None
        self.entries = []
        self.times = []
        self.cache_wait_key = None
        self.cache_wait_started_at = 0.0
        self.cache_wait_next_probe_at = 0.0
        if self.loaded_key is not None:
            _osd_clear_and_restore(client)
        self.loaded_key = None
        self.loaded_mode = None
        if self.loaded_sub_path is not None:
            _remove_medeia_external_subs(client, reason="state-clear")
            self.loaded_sub_path = None


def run_auto_overlay(
    *,
    mpv: MPV,
    poll_s: float = 0.15,
    config: Optional[dict] = None
) -> int:
    """Auto mode: track mpv's current file and render lyrics (note: 'lyric') or subtitles (note: 'sub').

    State is managed via :class:`_PlaybackState` to eliminate the repeated
    15-line reset blocks of the previous implementation.
    """
    cfg = config or {}

    client = mpv.client()
    if not client.connect():
        _log("mpv IPC is not reachable (is mpv running with --input-ipc-server?).")
        return 3

    _log(f"Auto overlay connected (ipc={getattr(mpv, 'ipc_path', None)})")
    _remove_medeia_external_subs(client, reason="startup-sweep")

    state = _PlaybackState()
    last_idx: Optional[int] = None
    last_text: Optional[str] = None
    last_visible: Optional[bool] = None

    global _OSD_STYLE_SAVED, _OSD_STYLE_APPLIED

    # Import the Store registry once so each track change doesn't re-import the module.
    try:
        from PluginCore.backend_registry import BackendRegistry  # noqa: PLC0415
        _backend_registry_cls: Any = BackendRegistry
    except Exception:
        _backend_registry_cls = None

    def _make_registry() -> Optional[Any]:
        if _backend_registry_cls is None:
            return None
        try:
            return _backend_registry_cls(cfg, suppress_debug=True)
        except Exception:
            return None

    while True:
        # ----------------------------------------------------------------
        # 1. Read IPC properties; reconnect on disconnect.
        # ----------------------------------------------------------------
        try:
            visible_raw = _ipc_get_property(
                client, _LYRIC_VISIBLE_PROP, True, raise_on_disconnect=True
            )
            raw_path = _ipc_get_property(client, "path", None, raise_on_disconnect=True)
        except ConnectionError:
            _osd_clear_and_restore(client)
            try:
                client.disconnect()
            except Exception:
                pass
            _OSD_STYLE_SAVED = None
            _OSD_STYLE_APPLIED = False
            if not client.connect():
                _log("mpv IPC disconnected; exiting lyric helper")
                return 4
            _remove_medeia_external_subs(client, reason="reconnect-sweep:path")
            state.clear(client)
            state.last_target = None
            last_idx = None
            last_text = None
            last_visible = None
            time.sleep(poll_s)
            continue

        # ----------------------------------------------------------------
        # 2. Visibility toggle support.
        # ----------------------------------------------------------------
        visible = bool(visible_raw) if isinstance(visible_raw, (bool, int)) else True
        if last_visible is None:
            last_visible = visible
        elif last_visible is True and visible is False:
            _osd_clear_and_restore(client)
            _remove_medeia_external_subs(client, reason="visibility-off")
            state.loaded_sub_path = None
            last_idx = None
            last_text = None
            last_visible = visible
        elif last_visible is False and visible is True:
            if state.loaded_mode in {"sub", "lyric-sub"} and state.loaded_sub_path is None:
                state.loaded_key = None
            last_idx = None
            last_text = None
            last_visible = visible
        else:
            last_visible = visible

        # ----------------------------------------------------------------
        # 3. Normalise the current playback target.
        # ----------------------------------------------------------------
        target = _unwrap_memory_m3u(str(raw_path)) if isinstance(raw_path, str) else None
        if isinstance(target, str):
            target = _normalize_file_uri_target(target)

        if not isinstance(target, str) or not target:
            time.sleep(poll_s)
            continue

        is_http = target.startswith("http://") or target.startswith("https://")

        # Non-HTTP streams (ytdl://, edl://, rtmp://, etc.) are never valid for lyrics.
        if (not is_http) and _is_stream_target(target):
            state.clear(client)
            state.last_target = target
            time.sleep(poll_s)
            continue

        # ----------------------------------------------------------------
        # 4. Read user-data overrides from the playlist controller.
        # ----------------------------------------------------------------
        store_override: Optional[str] = None
        hash_override: Optional[str] = None
        try:
            raw_so = _ipc_get_property(client, _ITEM_STORE_PROP, None)
            raw_ho = _ipc_get_property(client, _ITEM_HASH_PROP, None)
            store_override = str(raw_so).strip() if raw_so else None
            hash_override = str(raw_ho).strip().lower() if raw_ho else None
        except Exception:
            pass

        # ----------------------------------------------------------------
        # 5. Resolve store / hash on target change.
        # ----------------------------------------------------------------
        if target != state.last_target:
            state.last_target = target
            last_idx = None
            last_text = None

            _log(f"Target changed: {target}")

            state.file_hash = _infer_hash_for_target(target)
            if not state.file_hash:
                state.clear(client, clear_hash=False)
                time.sleep(poll_s)
                continue

            # Reset backend state; user-data override may supply it right away.
            state.store_name = None
            state.backend = None
            state.key = None
            state.cache_wait_key = None
            state.cache_wait_started_at = 0.0
            state.cache_wait_next_probe_at = 0.0

            if store_override and (not hash_override or hash_override == state.file_hash):
                reg = _make_registry()
                if reg is not None:
                    try:
                        state.backend = reg[store_override]
                        state.store_name = store_override
                        state.key = f"{state.store_name}:{state.file_hash}"
                        _log(
                            f"Resolved via mpv override"
                            f" store={state.store_name!r} hash={state.file_hash!r} valid=True"
                        )
                    except Exception:
                        state.backend = None
                        state.store_name = None
                        state.key = None

            if is_http:
                store_from_url = _extract_store_from_url_target(target)
                store_name = store_from_url or _infer_hydrus_store_from_url_target(
                    target=target, config=cfg
                )
                if not store_name:
                    _log("HTTP target has no store mapping; lyrics disabled")
                    state.clear(client, clear_hash=False)
                    time.sleep(poll_s)
                    continue

                reg = _make_registry()
                if reg is None:
                    _log(f"HTTP target store {store_name!r} not available; lyrics disabled")
                    state.clear(client, clear_hash=False)
                    time.sleep(poll_s)
                    continue

                try:
                    state.backend = reg[store_name]
                    state.store_name = store_name
                except Exception:
                    _log(f"HTTP target store {store_name!r} not available; lyrics disabled")
                    state.clear(client, clear_hash=False)
                    time.sleep(poll_s)
                    continue

                # Existence check only when store was inferred (not explicit in ?store=…).
                # When ?store= is in the URL mpv is already streaming — the file provably exists.
                if not store_from_url:
                    try:
                        meta = state.backend.get_metadata(state.file_hash, config=cfg)
                    except Exception:
                        meta = None
                    if meta is None:
                        _log(
                            f"HTTP target not found in store DB"
                            f" (store={store_name!r} hash={state.file_hash}); lyrics disabled"
                        )
                        state.clear(client, clear_hash=False)
                        time.sleep(poll_s)
                        continue

                state.key = f"{state.store_name}:{state.file_hash}"
                _log(f"Resolved store={state.store_name!r} hash={state.file_hash!r} valid=True")

            else:
                # Local file: resolve via store DB (skip if user-data already resolved it).
                if not state.key or not state.backend:
                    state.store_name, state.backend = _resolve_store_backend_for_target(
                        target=target,
                        file_hash=state.file_hash,
                        config=cfg,
                    )
                    state.key = (
                        f"{state.store_name}:{state.file_hash}"
                        if state.store_name and state.file_hash else None
                    )

                _log(
                    f"Resolved store={state.store_name!r} hash={state.file_hash!r}"
                    f" valid={bool(state.key)}"
                )

                if not state.key or not state.backend:
                    state.clear(client, clear_hash=False)
                    time.sleep(poll_s)
                    continue

        # ----------------------------------------------------------------
        # 6. Late-arriving context fallback: user-data override published
        #    after the track change was already processed without a backend.
        # ----------------------------------------------------------------
        if (not is_http) and target and (not state.key or not state.backend):
            try:
                state.file_hash = _infer_hash_for_target(target) or state.file_hash
            except Exception:
                pass

            if (
                store_override
                and state.file_hash
                and (not hash_override or hash_override == state.file_hash)
            ):
                reg = _make_registry()
                if reg is not None:
                    try:
                        state.backend = reg[store_override]
                        state.store_name = store_override
                        state.key = f"{state.store_name}:{state.file_hash}"
                        _log(
                            f"Resolved via mpv override"
                            f" store={state.store_name!r} hash={state.file_hash!r} valid=True"
                        )
                    except Exception:
                        pass

        # ----------------------------------------------------------------
        # 7. Load / reload content when the resolved key changes.
        # ----------------------------------------------------------------
        if (
            state.key
            and state.key != state.loaded_key
            and state.store_name
            and state.file_hash
            and state.backend
        ):
            notes: Optional[Dict[str, str]] = None
            cache_wait_s = _cache_float_config(
                cfg,
                "lyric_notes_cache_wait_seconds",
                _DEFAULT_NOTES_CACHE_WAIT_S,
            )
            pending_wait_s = _cache_float_config(
                cfg,
                "lyric_notes_pending_wait_seconds",
                _DEFAULT_NOTES_PENDING_WAIT_S,
            )

            try:
                notes = load_cached_notes(state.store_name, state.file_hash, config=cfg)
            except Exception:
                notes = None

            if notes is None:
                now = time.time()
                if state.cache_wait_key != state.key:
                    state.cache_wait_key = state.key
                    state.cache_wait_started_at = now
                    state.cache_wait_next_probe_at = 0.0
                elif state.cache_wait_next_probe_at > now:
                    time.sleep(max(0.05, min(0.5, state.cache_wait_next_probe_at - now)))
                    continue
                pending = is_notes_prefetch_pending(state.store_name, state.file_hash)
                waited_s = max(0.0, now - float(state.cache_wait_started_at or now))

                if pending and waited_s < pending_wait_s:
                    state.cache_wait_next_probe_at = now + max(0.2, min(0.5, pending_wait_s - waited_s))
                    time.sleep(max(0.05, min(0.5, state.cache_wait_next_probe_at - now)))
                    continue

                if waited_s < cache_wait_s:
                    state.cache_wait_next_probe_at = now + max(0.2, min(0.5, cache_wait_s - waited_s))
                    time.sleep(max(0.05, min(0.5, state.cache_wait_next_probe_at - now)))
                    continue

                try:
                    notes = state.backend.get_note(state.file_hash, config=cfg) or {}
                except Exception:
                    notes = {}

                try:
                    store_cached_notes(state.store_name, state.file_hash, notes)
                except Exception:
                    pass

            state.cache_wait_key = None
            state.cache_wait_started_at = 0.0
            state.cache_wait_next_probe_at = 0.0

            try:
                _log(
                    f"Loaded notes keys:"
                    f" {sorted(str(k) for k in notes) if isinstance(notes, dict) else 'N/A'}"
                )
            except Exception:
                _log("Loaded notes keys: <error>")

            sub_note_name, sub_text = _extract_sub_from_notes(notes)
            if sub_text:
                # Hand subtitles to mpv's track subsystem; suppress OSD lyric overlay.
                _osd_clear_and_restore(client)
                sub_path: Optional[Path] = None
                sub_title = _generated_subtitle_title(client, note_name=sub_note_name)
                try:
                    sub_path = _write_temp_sub_file(key=state.key, text=sub_text, label=sub_title)
                except Exception as exc:
                    _log(f"Failed to write sub note temp file: {exc}")

                if sub_path is not None:
                    _remove_medeia_external_subs(client, reason="load-note-sub")
                    _try_add_external_sub(client, sub_path, title=sub_title)
                    state.loaded_sub_path = sub_path
                    _log(
                        f"Loaded note-backed native subtitle track"
                        f" note={sub_note_name!r} title={sub_title!r} path={sub_path}"
                    )
                    _log_medeia_sub_tracks(client, "after-add-note-sub")

                state.entries = []
                state.times = []
                state.loaded_key = state.key
                state.loaded_mode = "sub"

            else:
                # Switching away from native subtitle mode: unload the external subtitle.
                if state.loaded_sub_path is not None:
                    _remove_medeia_external_subs(client, reason="switch-away-native-sub")
                    state.loaded_sub_path = None

                lrc_text = _extract_lrc_from_notes(notes)
                if not lrc_text:
                    _log("No lyric note found (note name: 'lyric')")

                # Auto-fetch: throttled per key to avoid hammering APIs.
                autofetch_enabled = bool(cfg.get("lyric_autofetch", True))
                now = time.time()
                if (
                    not lrc_text
                    and
                    autofetch_enabled
                    and state.key != state.fetch_attempt_key
                    and (now - state.fetch_attempt_at) > 2.0
                ):
                    state.fetch_attempt_key = state.key
                    state.fetch_attempt_at = now

                    artist, title = _infer_artist_title_from_mpv(client)
                    duration_s: Optional[float] = None
                    try:
                        duration_s = _ipc_get_property(client, "duration", None)
                    except Exception:
                        pass
                    if not artist or not title:
                        try:
                            tags, _src = state.backend.get_tag(state.file_hash, config=cfg)
                            if isinstance(tags, list):
                                artist, title = _infer_artist_title_from_tags(
                                    [str(x) for x in tags]
                                )
                        except Exception:
                            pass

                    _log(
                        f"Autofetch query artist={artist!r} title={title!r}"
                        f" duration={duration_s!r}"
                    )

                    if not artist or not title:
                        _log("Autofetch skipped: requires both artist and title")
                        fetched: Optional[str] = None
                    else:
                        fetched = _fetch_lrclib(
                            artist=artist,
                            title=title,
                            duration_s=(
                                float(duration_s)
                                if isinstance(duration_s, (int, float)) else None
                            ),
                        )
                        if not fetched or not fetched.strip():
                            fetched = _fetch_lyrics_ovh(artist=artist, title=title)

                    if fetched and fetched.strip():
                        try:
                            ok = bool(
                                state.backend.set_note(
                                    state.file_hash, "lyric", fetched, config=cfg
                                )
                            )
                            _log(f"Autofetch stored lyric note ok={ok}")
                        except Exception as exc:
                            _log(f"Autofetch failed to store lyric note: {exc}")
                    else:
                        _log("Autofetch: no lyrics found")

                    state.entries = []
                    state.times = []
                    _osd_clear_and_restore(client)
                    state.loaded_key = None
                    state.loaded_mode = None

                elif not lrc_text:
                    # No lyric and autofetch is throttled or disabled for this key.
                    _osd_clear_and_restore(client)
                    state.entries = []
                    state.times = []
                    state.loaded_key = state.key
                    state.loaded_mode = None

                else:
                    _log(f"Loaded lyric note ({len(lrc_text)} chars)")
                    parsed = parse_lrc(lrc_text)
                    if not parsed:
                        _log("Lyric note contained no timestamped entries")
                        _osd_clear_and_restore(client)
                        state.entries = []
                        state.times = []
                        state.loaded_key = state.key
                        state.loaded_mode = None
                    else:
                        lyric_sub_path: Optional[Path] = None
                        lyric_sub_title = _generated_subtitle_title(client, note_name="lyric")
                        try:
                            lyric_sub_text = _lrc_entries_to_vtt_text(parsed)
                            lyric_sub_path = _write_temp_sub_file(
                                key=f"{state.key}:lyric",
                                text=lyric_sub_text,
                                label=lyric_sub_title,
                            )
                        except Exception as exc:
                            _log(f"Failed to write lyric note temp subtitle: {exc}")

                        if lyric_sub_path is None:
                            _osd_clear_and_restore(client)
                            state.entries = []
                            state.times = []
                            state.loaded_key = state.key
                            state.loaded_mode = None
                        else:
                            _osd_clear_and_restore(client)
                            _remove_medeia_external_subs(client, reason="load-lyric-sub")
                            _try_add_external_sub(client, lyric_sub_path, title=lyric_sub_title)
                            state.loaded_sub_path = lyric_sub_path
                            state.entries = []
                            state.times = []
                            state.loaded_key = state.key
                            state.loaded_mode = "lyric-sub"
                            _log(
                                f"Loaded lyric note as native subtitle track"
                                f" title={lyric_sub_title!r} entries={len(parsed)}"
                                f" path={lyric_sub_path}"
                            )
                            _log_medeia_sub_tracks(client, "after-add-lyric-sub")

        # ----------------------------------------------------------------
        # 8. Render the current lyric line.
        # ----------------------------------------------------------------
        try:
            t = _ipc_get_property(client, "time-pos", None, raise_on_disconnect=True)
        except ConnectionError:
            _osd_clear_and_restore(client)
            try:
                client.disconnect()
            except Exception:
                pass
            _OSD_STYLE_SAVED = None
            _OSD_STYLE_APPLIED = False
            if not client.connect():
                _log("mpv IPC disconnected; exiting lyric helper")
                return 4
            _remove_medeia_external_subs(client, reason="reconnect-sweep:time")
            state.clear(client)
            state.last_target = None
            last_idx = None
            last_text = None
            last_visible = None
            time.sleep(poll_s)
            continue

        if not isinstance(t, (int, float)):
            time.sleep(poll_s)
            continue

        if not state.entries:
            if last_text is not None:
                _osd_clear_and_restore(client)
                last_text = None
                last_idx = None
            time.sleep(poll_s)
            continue

        if not visible:
            if last_text is not None:
                _osd_clear_and_restore(client)
                last_text = None
                last_idx = None
            time.sleep(poll_s)
            continue

        idx = _current_index(float(t), state.times)
        if idx < 0:
            time.sleep(poll_s)
            continue

        line = state.entries[idx]
        if idx != last_idx or line.text != last_text:
            if state.loaded_mode == "lyric":
                try:
                    _osd_apply_lyric_style(client, config=cfg)
                except Exception:
                    pass

            dur_ms = _lyric_duration_ms(idx, state.times, float(t))
            resp = _osd_set_text(client, line.text, duration_ms=dur_ms)
            if resp is None:
                client.disconnect()
                if not client.connect():
                    print("Lost mpv IPC connection.", file=sys.stderr)
                    return 4
            elif isinstance(resp, dict) and resp.get("error") not in (None, "success"):
                _log(f"mpv show-text returned error={resp.get('error')!r}")
            last_idx = idx
            last_text = line.text

        time.sleep(poll_s)

def run_overlay(*, mpv: MPV, entries: List[LrcLine], poll_s: float = 0.15) -> int:
    if not entries:
        print("No timestamped LRC lines found.", file=sys.stderr)
        return 2

    times = [e.time_s for e in entries]
    last_idx: Optional[int] = None
    last_text: Optional[str] = None

    client = mpv.client()
    if not client.connect():
        print(
            "mpv IPC is not reachable (is mpv running with --input-ipc-server?).",
            file=sys.stderr,
        )
        return 3

    while True:
        try:
            # mpv returns None when idle/no file.
            t = _ipc_get_property(client, "time-pos", None, raise_on_disconnect=True)
        except ConnectionError:
            _osd_clear(client)
            try:
                client.disconnect()
            except Exception:
                pass
            if not client.connect():
                print("Lost mpv IPC connection.", file=sys.stderr)
                return 4
            time.sleep(poll_s)
            continue

        if not isinstance(t, (int, float)):
            time.sleep(poll_s)
            continue

        idx = _current_index(float(t), times)
        if idx < 0:
            # Before first lyric timestamp.
            time.sleep(poll_s)
            continue

        line = entries[idx]
        if idx != last_idx or line.text != last_text:
            dur_ms = _lyric_duration_ms(idx, times, float(t))
            resp = _osd_set_text(client, line.text, duration_ms=dur_ms)
            if resp is None:
                client.disconnect()
                if not client.connect():
                    print("Lost mpv IPC connection.", file=sys.stderr)
                    return 4
            elif isinstance(resp, dict) and resp.get("error") not in (None, "success"):
                _log(f"mpv show-text returned error={resp.get('error')!r}")
            last_idx = idx
            last_text = line.text

        time.sleep(poll_s)

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plugins.mpv.lyric", add_help=True)
    parser.add_argument(
        "--ipc",
        default=None,
        help="mpv IPC path. Defaults to the repo's fixed IPC pipe name.",
    )
    parser.add_argument(
        "--lrc",
        default=None,
        help="Path to an .lrc file. If omitted, reads LRC from stdin.",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=0.15,
        help="Polling interval in seconds for time-pos updates.",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Optional path to a log file for diagnostics.",
    )

    args = parser.parse_args(argv)

    # Configure logging early.
    global _LOG_FH
    if args.log:
        try:
            log_path = Path(str(args.log)).expanduser().resolve()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _LOG_FH = open(log_path, "a", encoding="utf-8", errors="replace")
            _log("plugins.mpv.lyric starting")
        except Exception:
            _LOG_FH = None

    mpv = MPV(ipc_path=args.ipc) if args.ipc else MPV()

    # Prevent multiple lyric helpers from running at once for the same mpv IPC.
    if not _acquire_single_instance_lock(getattr(mpv, "ipc_path", "") or ""):
        _log("Another lyric helper instance is already running for this IPC; exiting.")
        return 0

    # If --lrc is provided, use it.
    if args.lrc:
        with open(args.lrc, "r", encoding="utf-8", errors="replace") as f:
            lrc_text = f.read()
        entries = parse_lrc(lrc_text)
        try:
            return run_overlay(mpv=mpv, entries=entries, poll_s=float(args.poll))
        except KeyboardInterrupt:
            return 0

    # Otherwise: if stdin has content, treat it as LRC; if stdin is empty/TTY, auto-discover.
    lrc_text = ""
    try:
        if not sys.stdin.isatty():
            lrc_text = _read_all_stdin() or ""
    except Exception:
        lrc_text = ""

    if lrc_text.strip():
        entries = parse_lrc(lrc_text)
        try:
            return run_overlay(mpv=mpv, entries=entries, poll_s=float(args.poll))
        except KeyboardInterrupt:
            return 0

    cfg = _load_config_best_effort()
    try:
        return run_auto_overlay(mpv=mpv, poll_s=float(args.poll), config=cfg)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
