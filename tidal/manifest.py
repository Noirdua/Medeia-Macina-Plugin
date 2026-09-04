"""Tidal/HIFI manifest helpers.

This module lives with the Tidal plugin (not cmdlets).
It contains best-effort helpers for turning proxy-provided Tidal "manifest"
values into a playable input reference:
- A local MPD file path (persisted to temp)
- Or a direct URL (when the manifest is JSON with `urls`)

Callers may pass either a SearchResult-like object (with `.full_metadata`) or
pipeline dicts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from API.httpx_shared import get_shared_httpx_client
from SYS.logger import log


_DEFAULT_TIDAL_TRACK_API_BASES = (
    "https://triton.squid.wtf",
    "https://wolf.qqdl.site",
    "https://maus.qqdl.site",
    "https://vogel.qqdl.site",
    "https://katze.qqdl.site",
    "https://hund.qqdl.site",
    "https://tidal.kinoplus.online",
    "https://tidal-api.binimum.org",
)


def resolve_tidal_manifest_path(item: Any) -> Optional[str]:
    """Persist the Tidal manifest (MPD) and return a local path or URL.

    Resolution order:
    1) `_tidal_manifest_path` (existing local file)
    2) `_tidal_manifest_url` (existing remote URL)
    3) decode `manifest` and:
       - if JSON with `urls`: return the first URL
       - if MPD XML: persist under `%TEMP%/medeia/tidal/` and return path

    If `manifest` is missing but a track id exists, the function will attempt a
    best-effort fetch from the public proxy endpoints to populate `manifest`.
    """

    metadata: Any = None
    if isinstance(item, dict):
        metadata = item.get("full_metadata") or item.get("metadata")
    else:
        metadata = getattr(item, "full_metadata", None) or getattr(item, "metadata", None)

    if not isinstance(metadata, dict):
        return None

    existing_path = metadata.get("_tidal_manifest_path")
    if existing_path:
        try:
            resolved = Path(str(existing_path))
            if resolved.is_file():
                return str(resolved)
        except Exception:
            pass

    existing_url = metadata.get("_tidal_manifest_url")
    if existing_url and isinstance(existing_url, str):
        candidate = existing_url.strip()
        if candidate:
            return candidate

    raw_manifest = metadata.get("manifest")
    if not raw_manifest:
        _maybe_fetch_track_manifest(item, metadata)
        raw_manifest = metadata.get("manifest")
        if not raw_manifest:
            return None

    manifest_str = "".join(str(raw_manifest or "").split())
    if not manifest_str:
        return None

    manifest_bytes: bytes
    try:
        manifest_bytes = base64.b64decode(manifest_str, validate=True)
    except Exception:
        try:
            manifest_bytes = base64.b64decode(manifest_str, validate=False)
        except Exception:
            try:
                manifest_bytes = manifest_str.encode("utf-8")
            except Exception:
                return None

    if not manifest_bytes:
        return None

    head = (manifest_bytes[:1024] or b"").lstrip()
    if head.startswith((b"{", b"[")):
        return _resolve_json_manifest_urls(metadata, manifest_bytes)

    looks_like_mpd = head.startswith((b"<?xml", b"<MPD")) or (b"<MPD" in head)
    if not looks_like_mpd:
        manifest_mime = str(metadata.get("manifestMimeType") or "").strip().lower()
        try:
            metadata["_tidal_manifest_error"] = (
                f"Decoded manifest is not an MPD XML (mime: {manifest_mime or 'unknown'})"
            )
        except Exception:
            pass
        try:
            log(
                f"[tidal] Decoded manifest is not an MPD XML for track {metadata.get('trackId') or metadata.get('id')} (mime {manifest_mime or 'unknown'})",
                file=sys.stderr,
            )
        except Exception:
            pass
        return None

    return _persist_mpd_bytes(item, metadata, manifest_bytes)


def _normalize_api_base(candidate: Any) -> Optional[str]:
    text = str(candidate or "").strip()
    if not text:
        return None
    if not re.match(r"^https?://", text, flags=re.IGNORECASE):
        return None
    return text.rstrip("/")


def _iter_track_api_bases(metadata: Dict[str, Any]) -> list[str]:
    bases: list[str] = []
    seen: set[str] = set()

    dynamic_candidates = [
        metadata.get("_tidal_api_base"),
        metadata.get("_api_base"),
        metadata.get("api_base"),
        metadata.get("base_url"),
    ]

    for candidate in dynamic_candidates:
        normalized = _normalize_api_base(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            bases.append(normalized)

    for candidate in _DEFAULT_TIDAL_TRACK_API_BASES:
        normalized = _normalize_api_base(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            bases.append(normalized)

    return bases


def _maybe_fetch_track_manifest(item: Any, metadata: Dict[str, Any]) -> None:
    """If we only have a track id, fetch details from the proxy to populate `manifest`."""

    try:
        already = bool(metadata.get("_tidal_track_details_fetched"))
    except Exception:
        already = False

    track_id = metadata.get("trackId") or metadata.get("id")

    if track_id is None:
        try:
            if isinstance(item, dict):
                candidate_path = item.get("path") or item.get("url")
            else:
                candidate_path = getattr(item, "path", None) or getattr(item, "url", None)
        except Exception:
            candidate_path = None

        if candidate_path:
            m = re.search(
                r"(tidal|hifi):(?://)?track[\\/](\d+)",
                str(candidate_path),
                flags=re.IGNORECASE,
            )
            if m:
                track_id = m.group(2)

    if already or track_id is None:
        return

    try:
        track_int = int(track_id)
    except Exception:
        track_int = None

    if not track_int or track_int <= 0:
        return

    try:
        client = get_shared_httpx_client()
    except Exception:
        return

    attempted = False
    for base in _iter_track_api_bases(metadata):
        attempted = True

        track_data: Optional[Dict[str, Any]] = None
        for params in ({"id": str(track_int)}, {"id": str(track_int), "quality": "LOSSLESS"}):
            try:
                resp = client.get(
                    f"{base}/track/",
                    params=params,
                    timeout=10.0,
                )
                resp.raise_for_status()
                payload = resp.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict) and data:
                    track_data = data
                    break
            except Exception:
                continue

        if isinstance(track_data, dict) and track_data:
            try:
                metadata.update(track_data)
            except Exception:
                pass

        if not metadata.get("manifest") or not metadata.get("url"):
            try:
                resp_info = client.get(
                    f"{base}/info/",
                    params={"id": str(track_int)},
                    timeout=10.0,
                )
                resp_info.raise_for_status()
                info_payload = resp_info.json()
                info_data = info_payload.get("data") if isinstance(info_payload, dict) else None
                if isinstance(info_data, dict) and info_data:
                    try:
                        for key, value in info_data.items():
                            if key not in metadata or not metadata.get(key):
                                metadata[key] = value
                    except Exception:
                        pass
            except Exception:
                pass

        if metadata.get("manifest"):
            break

    if attempted:
        try:
            metadata["_tidal_track_details_fetched"] = True
        except Exception:
            pass


def _resolve_json_manifest_urls(metadata: Dict[str, Any], manifest_bytes: bytes) -> Optional[str]:
    try:
        text = manifest_bytes.decode("utf-8", errors="ignore")
        payload = json.loads(text)
        urls = payload.get("urls") or []
        selected_url = None
        for candidate in urls:
            if isinstance(candidate, str):
                candidate = candidate.strip()
                if candidate:
                    selected_url = candidate
                    break
        if selected_url:
            try:
                metadata["_tidal_manifest_url"] = selected_url
            except Exception:
                pass
            return selected_url
        try:
            metadata["_tidal_manifest_error"] = "JSON manifest contained no urls"
        except Exception:
            pass
        log(
            f"[tidal] JSON manifest for track {metadata.get('trackId') or metadata.get('id')} had no playable urls",
            file=sys.stderr,
        )
    except Exception as exc:
        try:
            metadata["_tidal_manifest_error"] = f"Failed to parse JSON manifest: {exc}"
        except Exception:
            pass
        log(
            f"[tidal] Failed to parse JSON manifest for track {metadata.get('trackId') or metadata.get('id')}: {exc}",
            file=sys.stderr,
        )
    return None


def _persist_mpd_bytes(item: Any, metadata: Dict[str, Any], manifest_bytes: bytes) -> Optional[str]:
    manifest_hash = str(metadata.get("manifestHash") or "").strip()
    track_id = metadata.get("trackId") or metadata.get("id")

    identifier = manifest_hash or hashlib.sha256(manifest_bytes).hexdigest()
    identifier_safe = re.sub(r"[^A-Za-z0-9_-]+", "_", identifier)[:64]
    if not identifier_safe:
        identifier_safe = hashlib.sha256(manifest_bytes).hexdigest()[:12]

    track_safe = "tidal"
    if track_id is not None:
        track_safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(track_id))[:32] or "tidal"

    manifest_dir = Path(tempfile.gettempdir()) / "medeia" / "tidal"
    try:
        manifest_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    filename = f"tidal-{track_safe}-{identifier_safe[:24]}.mpd"
    target_path = manifest_dir / filename

    try:
        with open(target_path, "wb") as fh:
            fh.write(manifest_bytes)
        metadata["_tidal_manifest_path"] = str(target_path)

        # Best-effort: propagate back into the caller object/dict.
        if isinstance(item, dict):
            if item.get("full_metadata") is metadata:
                item["full_metadata"] = metadata
            elif item.get("metadata") is metadata:
                item["metadata"] = metadata
        else:
            extra = getattr(item, "extra", None)
            if isinstance(extra, dict):
                extra["_tidal_manifest_path"] = str(target_path)

        return str(target_path)
    except Exception:
        return None
