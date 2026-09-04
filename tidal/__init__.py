from __future__ import annotations

import re
import shutil
import subprocess
import time
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from plugins.tidal.api import (
    Tidal as TidalApi,
    build_track_tags,
    coerce_duration_seconds,
    extract_artists,
    stringify,
)
from PluginCore.base import Plugin, SearchResult
from SYS.field_access import get_field
from plugins.tidal.manifest import resolve_tidal_manifest_path
from SYS import pipeline as pipeline_context
from SYS.logger import debug, log

URL_API = (
    "https://triton.squid.wtf",
    "https://wolf.qqdl.site",
    "https://maus.qqdl.site",
    "https://vogel.qqdl.site",
    "https://katze.qqdl.site",
    "https://hund.qqdl.site",
    "https://tidal.kinoplus.online",
    "https://tidal-api.binimum.org",
    "https://tidal-api.binimum.org",
)




_KEY_TO_PARAM: Dict[str, str] = {
    "album": "al",
    "artist": "a",
    "playlist": "p",
    "video": "v",
    "song": "s",
    "track": "s",
    "title": "s",
}

_DELIMITERS_RE = re.compile(r"[;,]")
_SEGMENT_BOUNDARY_RE = re.compile(r"(?=\b\w+\s*:)")


def _format_total_seconds(seconds: Any) -> str:
    try:
        total = int(seconds)
    except Exception:
        return ""
    if total <= 0:
        return ""
    mins = total // 60
    secs = total % 60
    return f"{mins}:{secs:02d}"


class Tidal(Plugin):
    PLUGIN_NAME = "tidal"
    SUPPORTED_CMDLETS = frozenset({"download-file", "search-file"})

    TABLE_AUTO_STAGES = {
        "tidal.track": ["download-file"],
    }
    QUERY_ARG_CHOICES = {
        "artist": (),
        "album": (),
        "track": (),
        "title": (),
        "playlist": (),
        "video": (),
    }
    INLINE_QUERY_FIELD_CHOICES = QUERY_ARG_CHOICES
    URL_DOMAINS = (
        "tidal.com",
        "listen.tidal.com",
    )
    URL = URL_DOMAINS + ("tidal:",)
    """Provider that targets the Tidal search endpoint.

    The CLI can supply a list of fail-over URLs via ``provider.tidal.api_urls`` or
    ``provider.tidal.api_url`` in the config. When not configured, it defaults to
    https://tidal-api.binimum.org.
    """

    _stringify = staticmethod(stringify)
    _extract_artists = staticmethod(extract_artists)
    _build_track_tags = staticmethod(build_track_tags)
    _coerce_duration_seconds = staticmethod(coerce_duration_seconds)

    @property
    def prefers_transfer_progress(self) -> bool:
        return True

    @staticmethod
    def _normalize_query_filters(filters: Optional[Dict[str, Any]]) -> Dict[str, str]:
        """Normalize cmdlet-provided filters / inline args.

        The search-file cmdlet calls `provider.extract_query_arguments()` and then
        passes the extracted args back in as `filters=`. We treat those as
        first-class query arguments.
        """

        out: Dict[str, str] = {}
        if not isinstance(filters, dict):
            return out
        for k, v in filters.items():
            key = str(k or "").strip().lower()
            if not key:
                continue
            val = str(v or "").strip()
            if not val:
                continue
            out[key] = val
        return out

    def _get_view(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        # If filters/inline args specify the view, that wins.
        inline_args = self._normalize_query_filters(filters)
        if inline_args:
            return self._determine_view(str(query or "").strip(), inline_args)

        text = str(query or "").strip()
        if not text:
            return "track"
        if re.search(r"\balbum\s*:", text, flags=re.IGNORECASE):
            return "album"
        if re.search(r"\bartist\s*:", text, flags=re.IGNORECASE):
            return "artist"
        return "track"

    def get_table_type(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        view = self._get_view(query, filters)
        return f"{self.PLUGIN_NAME}.{view}"

    def get_table_metadata(self, query: str, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        meta = super().get_table_metadata(query, filters)
        meta["view"] = self._get_view(query, filters)
        return meta

    def postprocess_search_results(
        self,
        *,
        query: str,
        results: List[SearchResult],
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 50,
        table_type: str = "",
        table_meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[SearchResult], Optional[str], Optional[Dict[str, Any]]]:
        _ = query
        _ = filters
        _ = table_type

        # Provider-specific UX: if an artist search yields exactly one artist row,
        # auto-expand directly to albums (preserves historical cmdlet behavior).
        try:
            view = (table_meta or {}).get("view") if isinstance(table_meta, dict) else None
            if str(view or "").strip().lower() != "artist":
                return results, None, None
        except Exception:
            return results, None, None

        if not isinstance(results, list) or len(results) != 1:
            return results, None, None

        artist_res = results[0]
        artist_name = str(getattr(artist_res, "title", "") or "").strip()
        artist_md = getattr(artist_res, "full_metadata", None)

        artist_id: Optional[int] = None
        if isinstance(artist_md, dict):
            raw_id = artist_md.get("artistId") or artist_md.get("id")
            try:
                artist_id = int(raw_id) if raw_id is not None else None
            except Exception:
                artist_id = None

        # Use a floor of 200 to keep the expanded album list useful.
        want = max(int(limit or 0), 200)
        try:
            album_results = self._albums_for_artist(
                artist_id=artist_id,
                artist_name=artist_name,
                limit=want,
            )
        except Exception:
            album_results = []

        if not album_results:
            return results, None, None

        meta_out: Dict[str, Any] = dict(table_meta or {}) if isinstance(table_meta, dict) else {}
        meta_out["view"] = "album"
        return album_results, f"{self.PLUGIN_NAME}.album", meta_out

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.api_urls = self._resolve_api_urls()
        try:
            self.api_timeout = float(self.config.get("timeout", 10.0))
        except Exception:
            self.api_timeout = 10.0
        self.api_clients = [TidalApi(base_url=url, timeout=self.api_timeout) for url in self.api_urls]

    def resolve_playback_path(self, item: Any, **_kwargs: Any) -> Optional[str]:
        return resolve_tidal_manifest_path(item)

    def extract_query_arguments(self, query: str) -> Tuple[str, Dict[str, Any]]:
        """Parse inline `key:value` query arguments.

        Unlike the generic parser in PluginCore, this supports multi-word
        values (e.g. `artist:elliott smith`).

        Returns:
          (normalized_free_text_query, parsed_args)
        """

        cleaned = str(query or "").strip()
        if not cleaned:
            return "", {}

        segments: List[str] = []
        for chunk in _DELIMITERS_RE.split(cleaned):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                for sub in _SEGMENT_BOUNDARY_RE.split(chunk):
                    part = sub.strip()
                    if part:
                        segments.append(part)
            else:
                segments.append(chunk)

        parsed_args: Dict[str, Any] = {}
        free_text: List[str] = []
        for segment in segments:
            # Support both key:value and key=value.
            sep_index = segment.find(":")
            if sep_index < 0:
                sep_index = segment.find("=")

            if sep_index <= 0:
                free_text.append(segment)
                continue

            key = segment[:sep_index].strip().lower()
            value = segment[sep_index + 1 :].strip().strip('"').strip("'")
            if not key or not value:
                free_text.append(segment)
                continue

            if key in self.QUERY_ARG_CHOICES:
                parsed_args[key] = value
            else:
                # Unknown key: keep it in the free text so it isn't silently lost.
                free_text.append(segment)

        normalized = " ".join(part for part in free_text if part).strip()

        # If the query was *only* structured args (no free text), provide a
        # human-friendly query string for table titles (avoid falling back to '*').
        if not normalized and parsed_args:
            for preferred in ("artist", "album", "track", "title", "playlist", "video"):
                val = str(parsed_args.get(preferred) or "").strip()
                if val:
                    normalized = val
                    break
            if not normalized:
                # Last resort: join all values.
                normalized = " ".join(str(v) for v in parsed_args.values() if str(v).strip()).strip()

        return normalized, parsed_args

    def validate(self) -> bool:
        return bool(self.api_urls)

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> List[SearchResult]:
        if limit <= 0:
            return []
        normalized_query, inline_args = self.extract_query_arguments(query)
        raw_query = str(query or "").strip()
        search_query = (normalized_query or raw_query).strip()

        # Merge cmdlet-provided filters with inline args.
        merged_args: Dict[str, str] = {}
        merged_args.update(self._normalize_query_filters(filters))
        for k, v in (inline_args or {}).items():
            key = str(k or "").strip().lower()
            val = str(v or "").strip()
            if key and val:
                merged_args[key] = val

        # Best-effort: if the cmdlet split a multi-word value (e.g. artist:elliott smith
        # -> filters={'artist': 'elliott'}, query='smith'), stitch it back together.
        if merged_args.get("artist") and search_query and search_query not in {"*", ""}:
            candidate = merged_args.get("artist", "")
            if candidate:
                low_candidate = candidate.lower()
                low_query = search_query.lower()
                if low_query and low_query not in low_candidate:
                    # Only append when it looks like plain text (not another structured segment).
                    if ":" not in search_query and "=" not in search_query:
                        merged_args["artist"] = f"{candidate} {search_query}".strip()

        # Determine view from merged args (preferred), otherwise from the query text.
        view = self._determine_view(search_query, merged_args) if merged_args else self._determine_view(search_query, inline_args)

        # Build API params. Prefer structured args when present; the backend only accepts
        # one of s/a/v/p.
        structured_query = " ".join(
            f"{k}:{v}" for k, v in merged_args.items() if k in self.QUERY_ARG_CHOICES and str(v).strip()
        ).strip()
        params_source = structured_query or search_query
        if not params_source and merged_args:
            params_source = " ".join(f"{k}:{v}" for k, v in merged_args.items() if str(v).strip()).strip()
        if not params_source:
            return []

        params = self._build_search_params(params_source)
        if not params:
            return []

        payload: Optional[Dict[str, Any]] = None
        for base in self.api_urls:
            endpoint = f"{base.rstrip('/')}/search/"
            try:
                client = self._get_api_client_for_base(base)
                payload = client.search(params) if client else None
                if payload is not None:
                    break
            except Exception as exc:
                log(f"[tidal] Search failed for {endpoint}: {exc}", file=sys.stderr)
                continue

        if not payload:
            return []

        data = payload.get("data") or {}
        if view == "artist":
            items = self._extract_artist_items(data)
        else:
            items = self._extract_track_items(data)
        results: List[SearchResult] = []
        for item in items:
            if limit and len(results) >= limit:
                break
            if view == "artist":
                result = self._artist_item_to_result(item)
            else:
                result = self._item_to_result(item)
            if result is not None:
                results.append(result)

        return results[:limit]

    @staticmethod
    def _get_view_from_query(query: str) -> str:
        text = str(query or "").strip()
        if not text:
            return "track"
        if re.search(r"\bartist\s*:", text, flags=re.IGNORECASE):
            return "artist"
        if re.search(r"\balbum\s*:", text, flags=re.IGNORECASE):
            return "album"
        return "track"

    def _determine_view(self, query: str, inline_args: Dict[str, Any]) -> str:
        if inline_args:
            if "artist" in inline_args:
                return "artist"
            if "album" in inline_args:
                return "album"
            if "track" in inline_args or "title" in inline_args:
                return "track"
            if "video" in inline_args or "playlist" in inline_args:
                return "track"
        return self._get_view_from_query(query)

    @staticmethod
    def _safe_filename(value: Any, *, fallback: str = "tidal") -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", text)
        text = re.sub(r"\s+", " ", text).strip().strip(". ")
        return text[:120] if text else fallback

    @staticmethod
    def _parse_track_id(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            track_id = int(value)
        except Exception:
            return None
        return track_id if track_id > 0 else None

    def _extract_track_id_from_result(self, result: SearchResult) -> Optional[int]:
        md = getattr(result, "full_metadata", None)
        if isinstance(md, dict):
            track_id = self._parse_track_id(md.get("trackId") or md.get("id"))
            if track_id:
                return track_id

        path = str(getattr(result, "path", "") or "").strip()
        if path:
            m = re.search(r"tidal:(?://)?track[\\/](\d+)", path, flags=re.IGNORECASE)
            if m:
                return self._parse_track_id(m.group(1))
        return None

    @staticmethod
    def _parse_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            num = int(value)
        except Exception:
            return None
        return num if num > 0 else None

    def _parse_tidal_url(self, url: str) -> Tuple[str, Optional[int]]:
        try:
            parsed = urlparse(str(url))
        except Exception:
            return "", None

        scheme = str(parsed.scheme or "").lower().strip()
        if scheme == "tidal":
            # Handle tidal://view/id
            view = str(parsed.netloc or "").lower().strip()
            path_parts = [p for p in (parsed.path or "").split("/") if p]
            identifier = None
            if path_parts:
                identifier = self._parse_int(path_parts[0])
            return view, identifier

        parts = [segment for segment in (parsed.path or "").split("/") if segment]
        if not parts:
            return "", None

        idx = 0
        if parts[0].lower() == "browse":
            idx = 1
        if idx >= len(parts):
            return "", None

        # Scan ALL (view, id) pairs in the path, e.g.
        #   /album/634516/track/634519  →  [("album", 634516), ("track", 634519)]
        # When multiple views are present, prefer the more specific one:
        #   track > album > artist
        _VIEW_PRIORITY = {"track": 2, "album": 1, "artist": 0}
        _VALID_VIEWS = set(_VIEW_PRIORITY)
        found: dict[str, int] = {}
        i = idx
        while i < len(parts):
            v = parts[i].lower()
            if v in _VALID_VIEWS:
                # Look ahead for the first integer following this view keyword
                for j in range(i + 1, len(parts)):
                    cand = self._parse_int(parts[j])
                    if cand is not None:
                        found[v] = cand
                        i = j  # advance past the id
                        break
            i += 1

        if not found:
            return "", None

        # Return the highest-priority view that was found
        best_view = max(found, key=lambda v: _VIEW_PRIORITY.get(v, -1))
        return best_view, found[best_view]

    def _track_detail_to_result(self, detail: Optional[Dict[str, Any]], track_id: int) -> SearchResult:
        if isinstance(detail, dict):
            candidate = self._item_to_result(detail)
            if candidate is not None:
                try:
                    candidate.full_metadata = dict(detail)
                except Exception:
                    pass
                return candidate

        title = f"Track {track_id}"
        if isinstance(detail, dict):
            title = stringify(detail.get("title")) or title

        return SearchResult(
            table="tidal.track",
            title=title,
            path=f"tidal://track/{track_id}",
            detail=f"id:{track_id}",
            annotations=["tidal", "track"],
            media_kind="audio",
            full_metadata=dict(detail) if isinstance(detail, dict) else {},
            selection_args=["-url", f"tidal://track/{track_id}"],
        )

    def _extract_artist_selection_context(self, selected_items: List[Any]) -> List[Tuple[int, str]]:
        contexts: List[Tuple[int, str]] = []
        seen: set[int] = set()

        for item in selected_items or []:
            payload: Dict[str, Any] = {}
            if isinstance(item, dict):
                payload = item
            else:
                try:
                    payload = item.to_dict() if hasattr(item, "to_dict") and callable(getattr(item, "to_dict")) else {}
                except Exception:
                    payload = {}
                if not payload:
                    try:
                        payload = {
                            "title": getattr(item, "title", None),
                            "path": getattr(item, "path", None),
                            "full_metadata": getattr(item, "full_metadata", None),
                        }
                    except Exception:
                        payload = {}

            meta = payload.get("full_metadata") if isinstance(payload.get("full_metadata"), dict) else payload
            if not isinstance(meta, dict):
                meta = {}

            artist_id = self._parse_int(meta.get("artistId") or meta.get("id") or payload.get("artistId") or payload.get("id"))
            if not artist_id:
                # Try to parse from path.
                raw_path = str(payload.get("path") or "").strip()
                if raw_path:
                    m = re.search(r"tidal:(?://)?artist[\\/](\d+)", raw_path, flags=re.IGNORECASE)
                    if m:
                        artist_id = self._parse_int(m.group(1))

            if not artist_id or artist_id in seen:
                continue
            seen.add(artist_id)

            name = (
                payload.get("title")
                or meta.get("name")
                or meta.get("title")
                or payload.get("name")
            )
            name_text = str(name or "").strip() or f"Artist {artist_id}"
            contexts.append((artist_id, name_text))

        return contexts

    def _extract_album_selection_context(self, selected_items: List[Any]) -> List[Tuple[Optional[int], str, str]]:
        """Return (album_id, album_title, artist_name) for selected album rows."""

        contexts: List[Tuple[Optional[int], str, str]] = []
        seen_ids: set[int] = set()
        seen_keys: set[str] = set()

        for item in selected_items or []:
            payload: Dict[str, Any] = {}
            if isinstance(item, dict):
                payload = item
            else:
                try:
                    payload = item.to_dict() if hasattr(item, "to_dict") and callable(getattr(item, "to_dict")) else {}
                except Exception:
                    payload = {}
                if not payload:
                    try:
                        payload = {
                            "title": getattr(item, "title", None),
                            "path": getattr(item, "path", None),
                            "full_metadata": getattr(item, "full_metadata", None),
                        }
                    except Exception:
                        payload = {}

            meta = payload.get("full_metadata") if isinstance(payload.get("full_metadata"), dict) else payload
            if not isinstance(meta, dict):
                meta = {}

            album_title = stringify(payload.get("title") or meta.get("title") or meta.get("name"))
            if not album_title:
                album_title = stringify(meta.get("album") or meta.get("albumTitle"))
            if not album_title:
                continue

            artist_name = stringify(meta.get("_artist_name") or meta.get("artist") or meta.get("artistName"))
            if not artist_name:
                # Some album payloads include nested artist objects.
                artist_obj = meta.get("artist")
                if isinstance(artist_obj, dict):
                    artist_name = stringify(artist_obj.get("name"))

            # Prefer albumId when available; some payloads carry both id/albumId.
            album_id = self._parse_int(meta.get("albumId") or meta.get("id"))

            if not album_id:
                raw_path = stringify(payload.get("path"))
                if raw_path:
                    m = re.search(r"tidal:(?://)?album[\\/](\d+)", raw_path, flags=re.IGNORECASE)
                    if m:
                        album_id = self._parse_int(m.group(1))

            if album_id:
                if album_id in seen_ids:
                    continue
                seen_ids.add(album_id)
            else:
                key = f"{album_title.lower()}::{artist_name.lower()}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

            contexts.append((album_id, album_title, artist_name))

        return contexts

    def _track_matches_artist(self, track: Dict[str, Any], *, artist_id: Optional[int], artist_name: str) -> bool:
        if not isinstance(track, dict):
            return False
        wanted = str(artist_name or "").strip().lower()

        primary = track.get("artist")
        if isinstance(primary, dict):
            if artist_id and self._parse_int(primary.get("id")) == artist_id:
                return True
            name = str(primary.get("name") or "").strip().lower()
            if wanted and name == wanted:
                return True

        artists = track.get("artists")
        if isinstance(artists, list):
            for a in artists:
                if not isinstance(a, dict):
                    continue
                if artist_id and self._parse_int(a.get("id")) == artist_id:
                    return True
                name = str(a.get("name") or "").strip().lower()
                if wanted and name == wanted:
                    return True

        # Fallback: string-match extracted display.
        if wanted:
            try:
                names = [n.lower() for n in extract_artists(track)]
            except Exception:
                names = []
            return wanted in names

        return False

    def _albums_for_artist(self, *, artist_id: Optional[int], artist_name: str, limit: int = 200) -> List[SearchResult]:
        name = str(artist_name or "").strip()
        if not name:
            return []

        payload: Optional[Dict[str, Any]] = None
        for base in self.api_urls:
            endpoint = f"{base.rstrip('/')}/search/"
            try:
                client = self._get_api_client_for_base(base)
                payload = client.search({"s": name}) if client else None
                if payload is not None:
                    break
            except Exception as exc:
                log(f"[tidal] Album lookup failed for {endpoint}: {exc}", file=sys.stderr)
                continue

        if not payload:
            return []

        data = payload.get("data") or {}
        tracks = self._extract_track_items(data)
        if not tracks:
            return []

        albums_by_id: Dict[int, Dict[str, Any]] = {}
        albums_by_key: Dict[str, Dict[str, Any]] = {}
        for track in tracks:
            if not self._track_matches_artist(track, artist_id=artist_id, artist_name=name):
                continue
            album = track.get("album")
            if not isinstance(album, dict):
                continue
            # Prefer albumId when available; some payloads carry both id/albumId.
            album_id = self._parse_int(album.get("albumId") or album.get("id"))
            title = stringify(album.get("title"))
            if not title:
                continue
            if album_id:
                albums_by_id.setdefault(album_id, album)
                continue
            key = f"{title.lower()}::{name.lower()}"
            albums_by_key.setdefault(key, album)

        album_items: List[Dict[str, Any]] = list(albums_by_id.values()) + list(albums_by_key.values())
        results: List[SearchResult] = []
        for album in album_items:
            if limit and len(results) >= limit:
                break
            res = self._album_item_to_result(album, artist_name=name)
            if res is not None:
                results.append(res)
        return results

    def _tracks_for_album(self, *, album_id: Optional[int], album_title: str, artist_name: str = "", limit: int = 200) -> List[SearchResult]:
        title = str(album_title or "").strip()
        # When album_id is provided the /album/ endpoint can resolve tracks directly —
        # no title is required. Only bail out early when we have neither.
        if not title and not album_id:
            return []

        def _norm_album(text: str) -> str:
            # Normalize album titles for matching across punctuation/case/spacing.
            # Example: "either/or" vs "Either Or" or "Either/Or (Expanded Edition)".
            s = str(text or "").strip().lower()
            if not s:
                return ""
            s = re.sub(r"&", " and ", s)
            s = re.sub(r"[^a-z0-9]+", "", s)
            return s

        search_text = title
        artist_text = str(artist_name or "").strip()
        if artist_text:
            # The proxy only supports s/a/v/p. Use a combined s= query to bias results
            # toward the target album's tracks.
            search_text = f"{artist_text} {title}".strip()

        # Prefer /album when we have a numeric album id.
        # The proxy returns the album payload including a full track list in `data.items`.
        # When this endpoint is available, it is authoritative for an album id, so we do
        # not apply additional title/artist filtering.
        if album_id:
            for base in self.api_urls:
                endpoint = f"{base.rstrip('/')}/album/"
                try:
                    client = self._get_api_client_for_base(base)
                    album_payload = client.album(int(album_id)) if client else None
                except Exception as exc:
                    log(f"[tidal] Album lookup failed for {endpoint}: {exc}", file=sys.stderr)
                    continue

                if not isinstance(album_payload, dict) or not album_payload:
                    continue

                try:
                    album_data = album_payload.get("data")
                    album_tracks = self._extract_track_items(album_data if album_data is not None else album_payload)
                except Exception:
                    album_tracks = []

                if not album_tracks:
                    # Try the next configured base URL (some backends return an error-shaped
                    # JSON object with 200, or omit tracks for certain ids).
                    continue

                ordered: List[Tuple[int, int, Dict[str, Any]]] = []
                for tr in album_tracks:
                    if not isinstance(tr, dict):
                        continue
                    disc_val = self._parse_int(tr.get("volumeNumber") or tr.get("discNumber") or 0) or 0
                    track_val = self._parse_int(tr.get("trackNumber") or 0) or 0
                    ordered.append((disc_val, track_val, tr))

                ordered.sort(key=lambda t: (t[0], t[1]))
                try:
                    debug(f"tidal album endpoint tracks: album_id={album_id} extracted={len(album_tracks)}")
                except Exception:
                    pass

                results: List[SearchResult] = []
                for _disc, _track, tr in ordered:
                    if limit and len(results) >= limit:
                        break
                    res = self._item_to_result(tr)
                    if res is not None:
                        results.append(res)
                if results:
                    return results

        # Reduce punctuation in the raw search string to improve /search/ recall.
        try:
            search_text = re.sub(r"[/\\]+", " ", search_text)
            search_text = re.sub(r"\s+", " ", search_text).strip()
        except Exception:
            pass

        payload: Optional[Dict[str, Any]] = None
        for base in self.api_urls:
            endpoint = f"{base.rstrip('/')}/search/"
            try:
                client = self._get_api_client_for_base(base)
                payload = client.search({"s": search_text}) if client else None
                if payload is not None:
                    break
            except Exception as exc:
                log(f"[tidal] Track lookup failed for {endpoint}: {exc}", file=sys.stderr)
                continue

        if not payload:
            return []

        data = payload.get("data") or {}
        tracks = self._extract_track_items(data)
        if not tracks:
            return []

        try:
            debug(f"tidal album search tracks: album_id={album_id} extracted={len(tracks)} query={repr(search_text)}")
        except Exception:
            pass

        wanted_album = title.lower()
        wanted_album_norm = _norm_album(title)
        wanted_artist = artist_text.lower()
        seen_ids: set[int] = set()
        candidates: List[Tuple[int, int, Dict[str, Any]]] = []

        for track in tracks:
            if not isinstance(track, dict):
                continue
            tid = self._parse_int(track.get("id") or track.get("trackId"))
            if not tid or tid in seen_ids:
                continue

            album = track.get("album")
            album_ok = False
            if isinstance(album, dict):
                if album_id and self._parse_int(album.get("albumId") or album.get("id")) == album_id:
                    album_ok = True
                else:
                    at = stringify(album.get("title")).lower()
                    if at:
                        if at == wanted_album:
                            album_ok = True
                        else:
                            at_norm = _norm_album(at)
                            if wanted_album_norm and at_norm and (
                                    at_norm == wanted_album_norm
                                    or wanted_album_norm in at_norm
                                    or at_norm in wanted_album_norm):
                                album_ok = True
            else:
                # If album is not a dict, fall back to string compare.
                at = stringify(track.get("album")).lower()
                if at:
                    if at == wanted_album:
                        album_ok = True
                    else:
                        at_norm = _norm_album(at)
                        if wanted_album_norm and at_norm and (
                                at_norm == wanted_album_norm
                                or wanted_album_norm in at_norm
                                or at_norm in wanted_album_norm):
                            album_ok = True
            if not album_ok:
                continue

            if wanted_artist:
                if not self._track_matches_artist(track, artist_id=None, artist_name=artist_name):
                    continue
            seen_ids.add(tid)

            disc_val = self._parse_int(track.get("volumeNumber") or track.get("discNumber") or 0) or 0
            track_val = self._parse_int(track.get("trackNumber") or 0) or 0
            candidates.append((disc_val, track_val, track))

        candidates.sort(key=lambda t: (t[0], t[1]))

        # If strict matching found nothing, relax title matching (substring) while still
        # keeping artist filtering when available.
        if not candidates:
            for track in tracks:
                if not isinstance(track, dict):
                    continue
                tid = self._parse_int(track.get("id") or track.get("trackId"))
                if not tid or tid in seen_ids:
                    continue

                album = track.get("album")
                if isinstance(album, dict):
                    at = stringify(album.get("title")).lower()
                else:
                    at = stringify(track.get("album")).lower()

                if not at:
                    continue
                at_norm = _norm_album(at)
                if wanted_album_norm and at_norm:
                    if not (wanted_album_norm in at_norm or at_norm in wanted_album_norm):
                        continue
                else:
                    if wanted_album not in at:
                        continue
                if wanted_artist:
                    if not self._track_matches_artist(track, artist_id=None, artist_name=artist_name):
                        continue

                seen_ids.add(tid)
                disc_val = self._parse_int(track.get("volumeNumber") or track.get("discNumber") or 0) or 0
                track_val = self._parse_int(track.get("trackNumber") or 0) or 0
                candidates.append((disc_val, track_val, track))

            candidates.sort(key=lambda t: (t[0], t[1]))

        try:
            debug(f"tidal album search tracks: album_id={album_id} matched={len(candidates)} title={repr(title)} artist={repr(artist_name)}")
        except Exception:
            pass

        results: List[SearchResult] = []
        for _disc, _track, track in candidates:
            if limit and len(results) >= limit:
                break
            res = self._item_to_result(track)
            if res is not None:
                results.append(res)

        return results

    def _present_album_tracks(
        self,
        track_results: List[SearchResult],
        *,
        album_id: Optional[int],
        album_title: str,
        artist_name: str,
    ) -> None:
        if not track_results:
            return

        try:
            from SYS.rich_display import stdout_console
            from SYS.result_table import Table
        except Exception:
            return

        label = album_title or "Album"
        if artist_name:
            label = f"{artist_name} - {label}"

        table = Table(f"Tidal Tracks: {label}")._perseverance(True)
        table.set_table("tidal.track")
        try:
            table.set_table_metadata(
                {
                    "plugin": "tidal",
                    "view": "track",
                    "album_id": album_id,
                    "album_title": album_title,
                    "artist_name": artist_name,
                }
            )
        except Exception:
            pass

        results_payload: List[Dict[str, Any]] = []
        for result in track_results:
            table.add_result(result)
            try:
                results_payload.append(result.to_dict())
            except Exception:
                results_payload.append(
                    {
                        "table": getattr(result, "table", "tidal.track"),
                        "title": getattr(result, "title", ""),
                        "path": getattr(result, "path", ""),
                    }
                )

        pipeline_context.set_last_result_table(table, results_payload)
        pipeline_context.set_current_stage_table(table)

        try:
            stdout_console().print()
            stdout_console().print(table)
        except Exception:
            pass

    def _album_item_to_result(self, album: Dict[str, Any], *, artist_name: str) -> Optional[SearchResult]:
        if not isinstance(album, dict):
            return None
        title = stringify(album.get("title"))
        if not title:
            return None
        # Prefer albumId when available; some payloads carry both id/albumId.
        album_id = self._parse_int(album.get("albumId") or album.get("id"))
        path = f"tidal://album/{album_id}" if album_id else f"tidal://album/{self._safe_filename(title)}"

        columns: List[tuple[str, str]] = [("Album", title)]
        if artist_name:
            columns.append(("Artist", str(artist_name)))

        # Album stats (best-effort): show track count and total duration when available.
        track_count = self._parse_int(album.get("numberOfTracks") or album.get("trackCount") or album.get("tracks") or 0)
        if track_count:
            columns.append(("Tracks", str(track_count)))
        total_time = _format_total_seconds(album.get("duration") or album.get("durationSeconds") or album.get("duration_sec") or 0)
        if total_time:
            columns.append(("Total", total_time))

        release_date = stringify(album.get("releaseDate") or album.get("release_date") or album.get("date"))
        if release_date:
            columns.append(("Release", release_date))

        # Preserve the original album payload but add a hint for downstream.
        md: Dict[str, Any] = dict(album)
        if artist_name and "_artist_name" not in md:
            md["_artist_name"] = artist_name

        return SearchResult(
            table="tidal.album",
            title=title,
            path=path,
            detail="album",
            annotations=["tidal", "album"],
            media_kind="audio",
            columns=columns,
            full_metadata=md,
        )

    @staticmethod
    def url_patterns() -> List[str]:
        """Return URL prefixes handled by this provider."""
        return ["tidal://", "tidal.com"]

    @staticmethod
    def _find_ffmpeg() -> Optional[str]:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        try:
            repo_root = Path(__file__).resolve().parents[1]
            bundled = repo_root / "MPV" / "ffmpeg" / "bin" / "ffmpeg.exe"
            if bundled.is_file():
                return str(bundled)
        except Exception:
            pass
        return None

    @staticmethod
    def _find_ffprobe() -> Optional[str]:
        exe = shutil.which("ffprobe")
        if exe:
            return exe
        try:
            repo_root = Path(__file__).resolve().parents[1]
            bundled = repo_root / "MPV" / "ffmpeg" / "bin" / "ffprobe.exe"
            if bundled.is_file():
                return str(bundled)
        except Exception:
            pass
        return None

    def _probe_audio_codec(self, input_ref: str) -> Optional[str]:
        """Best-effort probe for primary audio codec name (lowercase)."""

        candidate = str(input_ref or "").strip()
        if not candidate:
            return None

        ffprobe_path = self._find_ffprobe()
        if ffprobe_path:
            cmd = [
                ffprobe_path,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=nw=1:nk=1",
                candidate,
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode == 0:
                    codec = str(proc.stdout or "").strip().lower()
                    if codec:
                        return codec
            except Exception:
                pass

        # Fallback: parse `ffmpeg -i` stream info.
        ffmpeg_path = self._find_ffmpeg()
        if not ffmpeg_path:
            return None
        try:
            proc = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-i", candidate],
                capture_output=True,
                text=True,
                check=False,
            )
            text = (proc.stderr or "") + "\n" + (proc.stdout or "")
            m = re.search(r"Audio:\s*([A-Za-z0-9_]+)", text)
            if m:
                return str(m.group(1)).strip().lower()
        except Exception:
            pass
        return None

    @staticmethod
    def _preferred_audio_suffix(codec: Optional[str], metadata: Optional[Dict[str, Any]] = None) -> str:
        c = str(codec or "").strip().lower()
        if c == "flac":
            return ".flac"
        if c in {"aac", "alac"}:
            return ".m4a"
        # Default to Matroska Audio for unknown / uncommon codecs.
        return ".mka"

    @staticmethod
    def _has_nonempty_file(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except Exception:
            return False

    def _ffmpeg_demux_to_audio(
        self,
        *,
        input_ref: str,
        output_path: Path,
        lossless_fallback: bool = True,
        progress: Optional[Any] = None,
        transfer_label: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        audio_quality: Optional[str] = None,
    ) -> Optional[Path]:
        ffmpeg_path = self._find_ffmpeg()
        if not ffmpeg_path:
            debug("[tidal] ffmpeg not found; cannot materialize audio from MPD")
            return None

        if self._has_nonempty_file(output_path):
            return output_path

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        protocol_whitelist = "file,https,http,tcp,tls,crypto,data"

        label = str(transfer_label or output_path.name or "tidal")

        def _estimate_total_bytes() -> Optional[int]:
            try:
                dur = int(duration_seconds) if duration_seconds is not None else None
            except Exception:
                dur = None
            if not dur or dur <=     0:
                return None 

            qual = str(audio_quality or "").strip().lower()
            # Rough per-quality bitrate guess (bytes/sec).
            if qual in {"hi_res",
                        "hi_res_lossless",
                        "hires",
                        "hi-res",
                        "master",
                        "mqa"}:
                bps = 4_608_000  # ~24-bit/96k stereo
            elif qual in {"lossless",
                          "flac"}:
                bps = 1_411_200  # 16-bit/44.1k stereo
            else:
                bps = 320_000  # kbps for compressed

            try:
                return int((bps / 8.0) * dur)
            except Exception:
                return None

        est_total_bytes = _estimate_total_bytes()

        def _update_transfer(total_bytes_val: Optional[int]) -> None:
            if progress is None:
                return
            try:
                progress.update_transfer(
                    label=label,
                    completed=int(total_bytes_val) if total_bytes_val is not None else None,
                    total=est_total_bytes,
                )
            except Exception:
                pass

        def _run(cmd: List[str], *, target_path: Optional[Path] = None) -> bool:
            cmd_progress = list(cmd)
            # Enable ffmpeg progress output for live byte updates.
            cmd_progress.insert(1, "-progress")
            cmd_progress.insert(2, "pipe:1")
            cmd_progress.insert(3, "-nostats")

            try:
                proc = subprocess.Popen(
                    cmd_progress,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except Exception as exc:
                debug(f"[tidal] ffmpeg invocation failed: {exc}")
                return False

            last_bytes = None
            try:
                while True:
                    line = proc.stdout.readline() if proc.stdout else ""
                    if not line:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.05)
                        continue

                    if "=" not in line:
                        continue
                    key, val = line.strip().split("=", 1)
                    if key == "total_size":
                        try:
                            last_bytes = int(val)
                            _update_transfer(last_bytes)
                        except Exception:
                            pass
                    elif key == "out_time_ms":
                        # Map out_time_ms to byte estimate when total_size missing.
                        try:
                            if est_total_bytes and val.isdigit():
                                ms = int(val)
                                dur_ms = (duration_seconds or 0) * 1000
                                if dur_ms > 0:
                                    pct = min(1.0, max(0.0, ms / dur_ms))
                                    approx = int(est_total_bytes * pct)
                                    _update_transfer(approx)
                        except Exception:
                            pass

                proc.wait()
            finally:
                if last_bytes is not None:
                    _update_transfer(last_bytes)

            check_path = target_path or output_path
            if proc.returncode == 0 and self._has_nonempty_file(check_path):
                return True

            try:
                stderr_text = proc.stderr.read() if proc.stderr else ""
                if stderr_text:
                    debug(f"[tidal] ffmpeg failed: {stderr_text.strip()}")
            except Exception:
                pass
            return False

        # Prefer remux (fast, no transcode).
        cmd_copy = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            protocol_whitelist,
            "-i",
            str(input_ref),
            "-vn",
            "-c",
            "copy",
            str(output_path),
        ]
        if _run(cmd_copy):
            return output_path

        if not lossless_fallback:
            return None

        # Fallback: decode/transcode to FLAC to guarantee a supported file.
        flac_path = (
            output_path
            if output_path.suffix.lower() == ".flac"
            else output_path.with_suffix(".flac")
        )
        if self._has_nonempty_file(flac_path):
            return flac_path

        # Avoid leaving a partial FLAC behind if we're transcoding into the final name.
        tmp_flac_path = flac_path
        if flac_path == output_path:
            tmp_flac_path = output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")

        cmd_flac = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            protocol_whitelist,
            "-i",
            str(input_ref),
            "-vn",
            "-c:a",
            "flac",
            str(tmp_flac_path),
        ]
        if _run(cmd_flac, target_path=tmp_flac_path) and self._has_nonempty_file(tmp_flac_path):
            if tmp_flac_path != flac_path:
                try:
                    tmp_flac_path.replace(flac_path)
                except Exception:
                    # If rename fails, still return the temp file.
                    return tmp_flac_path
            return flac_path
        return None

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        """Materialize a playable audio file from a Tidal DASH manifest."""

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        raw_path = str(getattr(result, "path", "") or "").strip()

        md: Dict[str, Any] = {}
        if isinstance(getattr(result, "full_metadata", None), dict):
            md = dict(getattr(result, "full_metadata") or {})

        track_id = self._extract_track_id_from_result(result)
        if track_id:
            # Multi-part enrichment from API: metadata, tags, and lyrics.
            full_data = self._fetch_all_track_data(track_id)
            if isinstance(full_data, dict):
                # 1. Update metadata
                api_md = full_data.get("metadata")
                if isinstance(api_md, dict):
                    md.update(api_md)
                
                # 2. Update tags (re-sync result.tag so cmdlet sees them)
                api_tags = full_data.get("tags")
                if isinstance(api_tags, list) and api_tags:
                    result.tag = set(api_tags)

                # 3. Handle lyrics
                lyrics = full_data.get("lyrics")
                if isinstance(lyrics, dict) and lyrics:
                    md.setdefault("lyrics", lyrics)
                    subtitles = lyrics.get("subtitles")
                    if isinstance(subtitles, str) and subtitles.strip():
                        md["_tidal_lyrics_subtitles"] = subtitles.strip()
                        # Generic key consumed by download-file._emit_local_file to
                        # persist lyrics as a store note without provider-specific logic.
                        md["_notes"] = {"lyric": subtitles.strip()}

        # Ensure downstream cmdlets see our enriched metadata.
        try:
            if isinstance(getattr(result, "full_metadata", None), dict):
                result.full_metadata.update(md)
            else:
                result.full_metadata = md
        except Exception:
            pass

        resolved = resolve_tidal_manifest_path({"full_metadata": md, "path": raw_path, "title": getattr(result, "title", "")})
        if not resolved:
            return None

        resolved_text = str(resolved).strip()
        if not resolved_text:
            return None

        track_id = self._extract_track_id_from_result(result)
        title_part = self._safe_filename(getattr(result, "title", None), fallback="tidal")
        hash_part = self._safe_filename(md.get("manifestHash"), fallback="")

        stem_parts = ["tidal"]
        if track_id:
            stem_parts.append(str(track_id))
        if hash_part:
            stem_parts.append(hash_part[:12])
        if title_part:
            stem_parts.append(title_part)
        stem = "-".join([p for p in stem_parts if p])[:180].rstrip("- ")

        codec = self._probe_audio_codec(resolved_text)
        suffix = self._preferred_audio_suffix(codec, md)

        # If resolve_tidal_manifest_path returned a URL, prefer feeding it directly to ffmpeg.
        if resolved_text.lower().startswith("http"):
            out_file = output_dir / f"{stem}{suffix}"
            materialized = self._ffmpeg_demux_to_audio(
                input_ref=resolved_text,
                output_path=out_file,
                progress=self.config.get("_pipeline_progress") if isinstance(self.config, dict) else None,
                transfer_label=title_part or getattr(result, "title", None),
                duration_seconds=coerce_duration_seconds(md),
                audio_quality=md.get("audioQuality") if isinstance(md, dict) else None,
            )
            if materialized is not None:
                return materialized

            # As a fallback, try downloading the URL directly if it looks like a file.
            try:
                from API.httpx_shared import get_shared_httpx_client

                timeout_val = float(getattr(self, "api_timeout", 10.0))
                client = get_shared_httpx_client()
                resp = client.get(resolved_text, timeout=timeout_val)
                resp.raise_for_status()
                content = resp.content
                direct_path = output_dir / f"{stem}.bin"
                with open(direct_path, "wb") as fh:
                    fh.write(content)
                return direct_path
            except Exception:
                return None

        try:
            source_path = Path(resolved_text)
        except Exception:
            return None

        if source_path.is_file() and source_path.suffix.lower() == ".mpd":
            # Materialize audio from the local MPD.
            out_file = output_dir / f"{stem}{suffix}"
            materialized = self._ffmpeg_demux_to_audio(
                input_ref=str(source_path),
                output_path=out_file,
                progress=self.config.get("_pipeline_progress") if isinstance(self.config, dict) else None,
                transfer_label=title_part or getattr(result, "title", None),
                duration_seconds=coerce_duration_seconds(md),
                audio_quality=md.get("audioQuality") if isinstance(md, dict) else None,
            )
            if materialized is not None:
                return materialized
            return None

        # If we somehow got a local audio file already, copy it to output_dir.
        if source_path.is_file() and source_path.suffix.lower() in {".m4a", ".mp3", ".flac", ".wav", ".mka", ".mp4"}:
            dest = output_dir / f"{stem}{source_path.suffix.lower()}"
            if self._has_nonempty_file(dest):
                return dest
            try:
                shutil.copyfile(source_path, dest)
                return dest
            except Exception:
                return None

        # As a last resort, attempt to treat the local path as an ffmpeg input.
        out_file = output_dir / f"{stem}{suffix}"
        materialized = self._ffmpeg_demux_to_audio(
            input_ref=resolved_text,
            output_path=out_file,
            progress=self.config.get("_pipeline_progress") if isinstance(self.config, dict) else None,
            transfer_label=title_part or getattr(result, "title", None),
            duration_seconds=coerce_duration_seconds(md),
            audio_quality=md.get("audioQuality") if isinstance(md, dict) else None,
        )
        return materialized

    def handle_url(self, url: str, *, output_dir: Optional[Path] = None) -> Tuple[bool, Optional[Path | Dict[str, Any]]]:
        view, identifier = self._parse_tidal_url(url)
        if not view:
            return False, None

        if view == "track":
            if not identifier or output_dir is None:
                return False, None

            try:
                detail = self._fetch_track_details(identifier)
            except Exception:
                detail = None

            result = self._track_detail_to_result(detail, identifier)
            try:
                downloaded = self.download(result, output_dir)
            except Exception:
                return False, None

            if downloaded:
                meta = None
                try:
                    if isinstance(getattr(result, "full_metadata", None), dict):
                        meta = dict(result.full_metadata)
                except Exception:
                    meta = None
                if meta is None and isinstance(detail, dict):
                    meta = dict(detail)

                title_hint = None
                try:
                    title_hint = str(getattr(result, "title", "") or "").strip()
                except Exception:
                    title_hint = None
                if not title_hint or title_hint.lower().startswith("track "):
                    if isinstance(meta, dict):
                        title_hint = stringify(meta.get("title")) or title_hint

                tags_hint: Optional[List[str]] = None
                try:
                    tags_val = getattr(result, "tag", None)
                    if isinstance(tags_val, (set, list, tuple)):
                        tags_hint = [str(t) for t in tags_val if t]
                except Exception:
                    tags_hint = None
                if not tags_hint and isinstance(meta, dict):
                    try:
                        tags_hint = [str(t) for t in build_track_tags(meta) if t]
                    except Exception:
                        tags_hint = None

                return True, {
                    "path": str(downloaded),
                    "title": title_hint,
                    "tags": tags_hint,
                    "full_metadata": meta,
                    "media_kind": "audio",
                }
            return False, None

        if view == "album":
            if not identifier:
                return False, None

            # In download-file flows, return a provider action so the cmdlet can
            # invoke this provider's bulk download hook and emit each track.
            if output_dir is not None:
                return True, {
                    "action": "download_items",
                    "path": f"tidal://album/{identifier}",
                    "title": f"Album {identifier}",
                    "metadata": {
                        "album_id": identifier,
                    },
                    "media_kind": "audio",
                }

            try:
                track_results = self._tracks_for_album(
                    album_id=identifier,
                    album_title="",
                    artist_name="",
                    limit=200,
                )
            except Exception:
                return False, None

            if not track_results:
                return False, None

            album_title = ""
            artist_name = ""
            metadata = getattr(track_results[0], "full_metadata", None)
            if isinstance(metadata, dict):
                album_obj = metadata.get("album")
                if isinstance(album_obj, dict):
                    album_title = stringify(album_obj.get("title"))
                else:
                    album_title = stringify(album_obj or metadata.get("album"))
                artists = extract_artists(metadata)
                if artists:
                    artist_name = artists[0]

            if not album_title:
                album_title = f"Album {identifier}"

            self._present_album_tracks(
                track_results,
                album_id=identifier,
                album_title=album_title,
                artist_name=artist_name,
            )
            return True, None

        return False, None

    def download_items(
        self,
        result: SearchResult,
        output_dir: Path,
        *,
        emit: Callable[[Path, str, str, Dict[str, Any]], None],
        progress: Any,
        quiet_mode: bool,
        path_from_result: Callable[[Any], Path],
        config: Optional[Dict[str, Any]] = None,
    ) -> int:
        _ = progress
        _ = quiet_mode
        _ = path_from_result
        _ = config

        metadata = getattr(result, "full_metadata", None)
        md: Dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}

        album_id = self._parse_int(md.get("album_id") or md.get("albumId") or md.get("id"))
        album_title = stringify(md.get("album_title") or md.get("title") or md.get("album"))

        artist_name = stringify(md.get("artist_name") or md.get("_artist_name") or md.get("artist"))
        if not artist_name:
            artist_obj = md.get("artist")
            if isinstance(artist_obj, dict):
                artist_name = stringify(artist_obj.get("name"))

        path_text = stringify(getattr(result, "path", ""))
        if path_text:
            view, identifier = self._parse_tidal_url(path_text)
            if view == "album" and not album_id:
                album_id = identifier

        if not album_id:
            return 0

        try:
            track_results = self._tracks_for_album(
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                limit=500,
            )
        except Exception:
            return 0

        if not track_results:
            return 0

        downloaded_count = 0
        for track_result in track_results:
            try:
                downloaded = self.download(track_result, output_dir)
            except Exception:
                downloaded = None

            if not downloaded:
                continue

            tr_md_raw = getattr(track_result, "full_metadata", None)
            tr_md = dict(tr_md_raw) if isinstance(tr_md_raw, dict) else {}
            source = stringify(tr_md.get("url") or getattr(track_result, "path", ""))
            relpath = str(downloaded.name)

            emit(downloaded, source, relpath, tr_md)
            downloaded_count += 1

        return downloaded_count

    def _get_api_client_for_base(self, base_url: str) -> Optional[Tidal]:
        base = base_url.rstrip("/")
        for client in self.api_clients:
            if getattr(client, "base_url", "").rstrip("/") == base:
                return client
        return None

    def _extract_track_items(self, data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            items: List[Dict[str, Any]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                # Some endpoints return wrapper objects like {"item": {...}}.
                nested = item.get("item")
                if isinstance(nested, dict):
                    items.append(nested)
                    continue
                nested = item.get("track")
                if isinstance(nested, dict):
                    items.append(nested)
                    continue
                items.append(item)
            return items
        if not isinstance(data, dict):
            return []

        items: List[Dict[str, Any]] = []
        direct = data.get("items")
        if isinstance(direct, list):
            for item in direct:
                if not isinstance(item, dict):
                    continue
                nested = item.get("item")
                if isinstance(nested, dict):
                    items.append(nested)
                    continue
                nested = item.get("track")
                if isinstance(nested, dict):
                    items.append(nested)
                    continue
                items.append(item)

        tracks_section = data.get("tracks")
        if isinstance(tracks_section, dict):
            track_items = tracks_section.get("items")
            if isinstance(track_items, list):
                for item in track_items:
                    if not isinstance(item, dict):
                        continue
                    nested = item.get("item")
                    if isinstance(nested, dict):
                        items.append(nested)
                        continue
                    nested = item.get("track")
                    if isinstance(nested, dict):
                        items.append(nested)
                        continue
                    items.append(item)

        top_hits = data.get("topHits")
        if isinstance(top_hits, list):
            for hit in top_hits:
                if not isinstance(hit, dict):
                    continue
                hit_type = str(hit.get("type") or "").upper()
                if hit_type != "TRACKS" and hit_type != "TRACK":
                    continue
                value = hit.get("value")
                if isinstance(value, dict):
                    items.append(value)

        seen: set[int] = set()
        deduped: List[Dict[str, Any]] = []
        for item in items:
            track_id = item.get("id") or item.get("trackId")
            if track_id is None:
                continue
            try:
                track_int = int(track_id)
            except Exception:
                track_int = None
            if track_int is None or track_int in seen:
                continue
            seen.add(track_int)
            deduped.append(item)

        return deduped

    def _resolve_api_urls(self) -> List[str]:
        urls: List[str] = []
        raw = self.config.get("api_urls")
        if raw is None:
            raw = self.config.get("api_url")
        if isinstance(raw, (list, tuple)):
            urls.extend(str(item).strip() for item in raw if isinstance(item, str))
        elif isinstance(raw, str):
            urls.append(raw.strip())
        cleaned = [u.rstrip("/") for u in urls if isinstance(u, str) and u.strip()]
        if not cleaned:
            cleaned = [URL_API[0]]
        return cleaned

    def _build_search_params(self, query: str) -> Dict[str, str]:
        cleaned = str(query or "").strip()
        if not cleaned:
            return {}

        segments: List[str] = []
        for chunk in _DELIMITERS_RE.split(cleaned):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                for sub in _SEGMENT_BOUNDARY_RE.split(chunk):
                    part = sub.strip()
                    if part:
                        segments.append(part)
            else:
                segments.append(chunk)

        key_values: Dict[str, str] = {}
        free_text: List[str] = []
        for segment in segments:
            if ":" not in segment:
                free_text.append(segment)
                continue
            key, value = segment.split(":", 1)
            key = key.strip().lower()
            value = value.strip().strip('"').strip("'")
            if value:
                key_values[key] = value

        # The proxy API only accepts exactly one of s/a/v/p. If the user mixes
        # free text with a structured key (e.g. artist:foo bar), treat the free
        # text as part of the same query instead of creating an additional key.
        mapped_values: Dict[str, List[str]] = {}
        for key, value in key_values.items():
            if not value:
                continue
            mapped = _KEY_TO_PARAM.get(key)
            if not mapped:
                continue
            mapped_values.setdefault(mapped, []).append(value)

        # Choose the search key in priority order.
        chosen_key = None
        for candidate in ("a", "v", "p", "s"):
            if mapped_values.get(candidate):
                chosen_key = candidate
                break
        if chosen_key is None:
            chosen_key = "s"

        chosen_parts: List[str] = []
        chosen_parts.extend(mapped_values.get(chosen_key, []))

        # If the user provided free text and a structured key (like artist:),
        # fold it into the chosen key instead of forcing a second key.
        extra = " ".join(part for part in free_text if part).strip()
        if extra:
            chosen_parts.append(extra)

        chosen_value = " ".join(p for p in chosen_parts if p).strip()
        if not chosen_value:
            chosen_value = cleaned

        return {chosen_key: chosen_value} if chosen_value else {}

    def _extract_artist_items(self, data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []

        items: List[Dict[str, Any]] = []
        direct = data.get("items")
        if isinstance(direct, list):
            items.extend(item for item in direct if isinstance(item, dict))

        artists_section = data.get("artists")
        if isinstance(artists_section, dict):
            artist_items = artists_section.get("items")
            if isinstance(artist_items, list):
                items.extend(item for item in artist_items if isinstance(item, dict))

        top_hits = data.get("topHits")
        if isinstance(top_hits, list):
            for hit in top_hits:
                if not isinstance(hit, dict):
                    continue
                hit_type = str(hit.get("type") or "").upper()
                if hit_type != "ARTISTS" and hit_type != "ARTIST":
                    continue
                value = hit.get("value")
                if isinstance(value, dict):
                    items.append(value)

        seen: set[int] = set()
        deduped: List[Dict[str, Any]] = []
        for item in items:
            raw_id = item.get("id") or item.get("artistId")
            if raw_id is None:
                continue
            try:
                artist_int = int(raw_id)
            except Exception:
                artist_int = None
            if artist_int is None or artist_int in seen:
                continue
            seen.add(artist_int)
            deduped.append(item)

        return deduped

    def _artist_item_to_result(self, item: Dict[str, Any]) -> Optional[SearchResult]:
        if not isinstance(item, dict):
            return None

        name = str(item.get("name") or item.get("title") or "").strip()
        if not name:
            return None

        raw_id = item.get("id") or item.get("artistId")
        if raw_id is None:
            return None
        try:
            artist_id = int(raw_id)
        except (TypeError, ValueError):
            return None

        path = f"tidal://artist/{artist_id}"

        columns: List[tuple[str, str]] = [("Artist", name), ("Artist ID", str(artist_id))]
        popularity = stringify(item.get("popularity"))
        if popularity:
            columns.append(("Popularity", popularity))

        return SearchResult(
            table="tidal.artist",
            title=name,
            path=path,
            detail="tidal.artist",
            annotations=["tidal", "artist"],
            media_kind="audio",
            columns=columns,
            full_metadata=item,
        )

    @staticmethod
    def _format_duration(seconds: Any) -> str:
        try:
            total = int(seconds)
            if total < 0:
                return ""
        except Exception:
            return ""
        minutes, secs = divmod(total, 60)
        return f"{minutes}:{secs:02d}"

    def _item_to_result(self, item: Dict[str, Any]) -> Optional[SearchResult]:
        if not isinstance(item, dict):
            return None

        title = str(item.get("title") or "").strip()
        if not title:
            return None

        identifier = item.get("id")
        if identifier is None:
            return None
        try:
            track_id = int(identifier)
        except (TypeError, ValueError):
            return None

        # Avoid tidal.com URLs entirely; selection will resolve to a decoded MPD.
        path = f"tidal://track/{track_id}"

        artists = extract_artists(item)
        artist_display = ", ".join(artists)

        album = item.get("album")
        album_title = ""
        if isinstance(album, dict):
            album_title = str(album.get("title") or "").strip()

        detail_parts: List[str] = []
        if artist_display:
            detail_parts.append(artist_display)
        if album_title:
            detail_parts.append(album_title)
        detail = " | ".join(detail_parts)

        columns: List[tuple[str, str]] = []
        if title:
            columns.append(("Title", title))
        disc_no = stringify(item.get("volumeNumber") or item.get("discNumber") or item.get("disc_number"))
        track_no = stringify(item.get("trackNumber") or item.get("track_number"))
        if disc_no:
            columns.append(("Disc #", disc_no))
        if track_no:
            columns.append(("Track #", track_no))
        if album_title:
            columns.append(("Album", album_title))
        if artist_display:
            columns.append(("Artist", artist_display))
        duration_text = self._format_duration(item.get("duration"))
        if duration_text:
            columns.append(("Duration", duration_text))
        audio_quality = str(item.get("audioQuality") or "").strip()
        if audio_quality:
            columns.append(("Quality", audio_quality))

        # IMPORTANT: do not retain a shared reference to the raw API dict.
        # Downstream playback (MPV) mutates metadata to cache the decoded Tidal
        # manifest path/URL. If multiple results share the same dict reference,
        # they can incorrectly collapse to a single playable target.
        full_md: Dict[str, Any] = dict(item)
        url_value = stringify(full_md.get("url"))
        if url_value:
            full_md["url"] = url_value

        tags = build_track_tags(full_md)

        result = SearchResult(
            table="tidal.track",
            title=title,
            path=path,
            detail="tidal.track",
            annotations=["tidal", "track"],
            media_kind="audio",
            tag=tags,
            columns=columns,
            full_metadata=full_md,
            selection_args=["-url", path],
        )
        if url_value:
            try:
                result.url = url_value
            except Exception:
                pass
        return result

    def _extract_track_selection_context(
        self, selected_items: List[Any]
    ) -> List[Tuple[int, str, str]]:
        contexts: List[Tuple[int, str, str]] = []
        seen_ids: set[int] = set()
        for item in selected_items or []:
            payload: Dict[str, Any] = {}
            if isinstance(item, dict):
                payload = item
            else:
                try:
                    payload = (
                        item.to_dict()
                        if hasattr(item, "to_dict")
                        and callable(getattr(item, "to_dict"))
                        else {}
                    )
                except Exception:
                    payload = {}
                if not payload:
                    try:
                        payload = {
                            "title": getattr(item, "title", None),
                            "path": getattr(item, "path", None),
                            "url": getattr(item, "url", None),
                            "full_metadata": getattr(item, "full_metadata", None),
                        }
                    except Exception:
                        payload = {}

            meta = (
                payload.get("full_metadata")
                if isinstance(payload.get("full_metadata"), dict)
                else payload
            )
            if not isinstance(meta, dict):
                meta = {}
            raw_id = meta.get("trackId") or meta.get("id") or payload.get("id")
            if raw_id is None:
                continue
            try:
                track_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if track_id in seen_ids:
                continue
            seen_ids.add(track_id)

            title = (
                payload.get("title")
                or meta.get("title")
                or payload.get("name")
                or payload.get("path")
                or payload.get("url")
            )
            if not title:
                title = f"Track {track_id}"
            path = (
                payload.get("path")
                or payload.get("url")
                or f"tidal://track/{track_id}"
            )
            contexts.append((track_id, str(title).strip(), str(path).strip()))
        return contexts

    def _fetch_track_details(self, track_id: int) -> Optional[Dict[str, Any]]:
        """Legacy wrapper returning just metadata from the consolidated API call."""
        res = self._fetch_all_track_data(track_id)
        return res.get("metadata") if res else None

    def _fetch_all_track_data(self, track_id: int) -> Optional[Dict[str, Any]]:
        """Fetch full track details including metadata, tags, and lyrics from the API."""
        if track_id <= 0:
            return None
        for base in self.api_urls:
            try:
                client = self._get_api_client_for_base(base)
                if not client:
                    continue
                # This method in the API client handles merging info+track and building tags.
                return client.get_full_track_metadata(track_id)
            except Exception as exc:
                debug(f"[tidal] Full track fetch failed for {base}: {exc}")
                continue
        return None

    def _fetch_track_lyrics(self, track_id: int) -> Optional[Dict[str, Any]]:
        """Legacy wrapper returning just lyrics from the consolidated API call."""
        res = self._fetch_all_track_data(track_id)
        return res.get("lyrics") if res else None

    def _build_track_columns(self, detail: Dict[str, Any], track_id: int) -> List[Tuple[str, str]]:
        values: List[Tuple[str, str]] = [
            ("Track ID", str(track_id)),
            ("Quality", stringify(detail.get("audioQuality"))),
            ("Mode", stringify(detail.get("audioMode"))),
            ("Asset", stringify(detail.get("assetPresentation"))),
            ("Manifest Type", stringify(detail.get("manifestMimeType"))),
            ("Manifest Hash", stringify(detail.get("manifestHash"))),
            ("Bit Depth", stringify(detail.get("bitDepth"))),
            ("Sample Rate", stringify(detail.get("sampleRate"))),
        ]
        return [(name, value) for name, value in values if value]

    @staticmethod
    def selection_auto_stage(
        table_type: str,
        stage_args: Optional[Sequence[str]] = None,
    ) -> Optional[List[str]]:
        """Determine if selection should auto-run download-file."""
        t = str(table_type or "").strip().lower()
        
        # Explicit track tables always auto-download.
        if t == "tidal.track":
            return ["download-file"]
            
        # For the generic "tidal" table (first-stage search results), 
        # only auto-download if we're selecting track items.
        # Otherwise, let selector() handle navigation (artist -> album -> track).
        if t == "tidal":
            # If we can't see the items yet, we have to guess. 
            # Default to None so selector() gets a chance to run first.
            return None
            
        return super().selection_auto_stage(table_type, stage_args)

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any,
    ) -> bool:
        if not stage_is_last:
            return False

        try:
            current_table = ctx.get_current_stage_table()
        except Exception:
            current_table = None
        if current_table is None:
            try:
                current_table = ctx.get_last_result_table()
            except Exception:
                current_table = None
        table_type = str(
            current_table.table
            if current_table and hasattr(current_table, "table")
            else ""
        ).strip().lower()

        try:
            debug(
                f"[tidal.selector] table_type={table_type} stage_is_last={stage_is_last} selected_count={len(selected_items) if selected_items else 0}"
            )
        except Exception:
            pass

        # Unified selection logic: detect artist/album/track by inspecting path or metadata
        # when the table name is just the generic "tidal" (from search-file).
        is_generic_tidal = (table_type == "tidal")
        
        # Artist selection: selecting @N should open an albums list.
        if table_type == "tidal.artist" or (is_generic_tidal and any(str(get_field(i, "path")).startswith("tidal://artist/") for i in selected_items)):
            contexts = self._extract_artist_selection_context(selected_items)
            try:
                debug(f"[tidal.selector] artist contexts={len(contexts)}")
            except Exception:
                pass
            if not contexts:
                return False

            artist_id, artist_name = contexts[0]
            album_results = self._albums_for_artist(artist_id=artist_id, artist_name=artist_name, limit=200)
            if not album_results:
                try:
                    from SYS.rich_display import stdout_console
                    stdout_console().print(f"[bold yellow][tidal] No albums found for {artist_name}[/]")
                except Exception:
                    log(f"[tidal] No albums found for {artist_name}")
                return True

            try:
                from SYS.rich_display import stdout_console
                from SYS.result_table import Table
            except Exception:
                return False

            table = Table(f"Tidal Albums: {artist_name}")._perseverance(False)
            table.set_table("tidal.album")
            try:
                table.set_table_metadata({"plugin": "tidal", "view": "album", "artist_id": artist_id, "artist_name": artist_name})
            except Exception:
                pass

            results_payload: List[Dict[str, Any]] = []
            for res in album_results:
                table.add_result(res)
                try:
                    results_payload.append(res.to_dict())
                except Exception:
                    results_payload.append({"table": "tidal", "title": getattr(res, "title", ""), "path": getattr(res, "path", "")})

            try:
                ctx.set_last_result_table(table, results_payload)
                ctx.set_current_stage_table(table)
            except Exception:
                pass

            try:
                suppress = bool(getattr(ctx, "_suppress_provider_selector_print", False))
            except Exception:
                suppress = False

            if not suppress:
                try:
                    stdout_console().print()
                    stdout_console().print(table)
                except Exception:
                    pass

            return True

        # Album selection: selecting @N should open the track list for that album.
        if table_type == "tidal.album" or (is_generic_tidal and any(str(get_field(i, "path")).startswith("tidal://album/") for i in selected_items)):
            contexts = self._extract_album_selection_context(selected_items)
            try:
                debug(f"[tidal.selector] album contexts={len(contexts)}")
            except Exception:
                pass
            if not contexts:
                return False

            album_id, album_title, artist_name = contexts[0]
            track_results = self._tracks_for_album(album_id=album_id, album_title=album_title, artist_name=artist_name, limit=200)
            if not track_results:
                return False

            try:
                from SYS.rich_display import stdout_console
                from SYS.result_table import Table
            except Exception:
                return False

            label = album_title
            if artist_name:
                label = f"{artist_name} - {album_title}"
            # Preserve album order (disc/track) rather than sorting by title.
            table = Table(f"Tidal Tracks: {label}")._perseverance(True)
            table.set_table("tidal.track")
            try:
                table.set_table_metadata(
                    {
                        "plugin": "tidal",
                        "view": "track",
                        "album_id": album_id,
                        "album_title": album_title,
                        "artist_name": artist_name,
                    }
                )
            except Exception:
                pass

            results_payload: List[Dict[str, Any]] = []
            for res in track_results:
                table.add_result(res)
                try:
                    results_payload.append(res.to_dict())
                except Exception:
                    results_payload.append({"table": "tidal", "title": getattr(res, "title", ""), "path": getattr(res, "path", "")})

            try:
                ctx.set_last_result_table(table, results_payload)
                ctx.set_current_stage_table(table)
            except Exception:
                pass

            try:
                suppress = bool(getattr(ctx, "_suppress_provider_selector_print", False))
            except Exception:
                suppress = False

            if not suppress:
                try:
                    stdout_console().print()
                    stdout_console().print(table)
                except Exception:
                    pass

            return True

        # Optimization: If we are selecting tracks, do NOT force a "Detail View" (resolving manifest) here.
        # This allows batch selection to flow immediately to `download-file` (via TABLE_AUTO_STAGES)
        # or other downstream cmdlets. The download logic (tidal.download) handles manifest resolution locally.
        if table_type == "tidal.track" or (is_generic_tidal and any(str(get_field(i, "path")).startswith("tidal://track/") for i in selected_items)):
             return False

        contexts = self._extract_track_selection_context(selected_items)
        try:
            debug(f"[tidal.selector] track contexts={len(contexts)}")
        except Exception:
            pass
        if not contexts:
            return False

        track_details: List[Tuple[int, str, str, Dict[str, Any]]] = []
        for track_id, title, path in contexts:
            detail = self._fetch_track_details(track_id)
            if detail:
                track_details.append((track_id, title, path, detail))

        if not track_details:
            return False

        try:
            from SYS.rich_display import stdout_console
            from SYS.result_table import Table
        except Exception:
            return False

        table = Table("Tidal Track")._perseverance(True)
        table.set_table("tidal.track")
        try:
            table.set_table_metadata({"plugin": "tidal", "view": "track", "resolved_manifest": True})
        except Exception:
            pass
        results_payload: List[Dict[str, Any]] = []
        for track_id, title, path, detail in track_details:
            resolved_path = f"tidal://track/{track_id}"

            artists = extract_artists(detail)
            artist_display = ", ".join(artists) if artists else ""
            columns = self._build_track_columns(detail, track_id)
            if artist_display:
                columns.insert(1, ("Artist", artist_display))
            album = detail.get("album")
            if isinstance(album, dict):
                album_title = stringify(album.get("title"))
            else:
                album_title = stringify(detail.get("album"))
            if album_title:
                insert_pos = 2 if artist_display else 1
                columns.insert(insert_pos, ("Album", album_title))

            tags = build_track_tags(detail)
            url_value = stringify(detail.get("url"))

            result = SearchResult(
                table="tidal.track",
                title=title,
                path=resolved_path,
                detail=f"id:{track_id}",
                annotations=["tidal", "track"],
                media_kind="audio",
                columns=columns,
                full_metadata=detail,
                tag=tags,
                selection_args=["-url", resolved_path],
            )
            if url_value:
                try:
                    result.url = url_value
                except Exception:
                    pass
            table.add_result(result)
            try:
                results_payload.append(result.to_dict())
            except Exception:
                results_payload.append({
                    "table": "tidal.track",
                    "title": result.title,
                    "path": result.path,
                })

        try:
            ctx.set_last_result_table(table, results_payload)
            ctx.set_current_stage_table(table)
        except Exception:
            pass

        try:
            stdout_console().print()
            stdout_console().print(table)
        except Exception:
            pass

        return True

    def expand_selection(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        table_type: str = "",
        **_kwargs: Any,
    ) -> Optional[List[Any]]:
        _ = ctx
        if stage_is_last:
            return None

        normalized_table = str(table_type or "").strip().lower()
        if normalized_table != "tidal.album":
            return None

        try:
            contexts = self._extract_album_selection_context(selected_items)
        except Exception:
            return None
        if not contexts:
            return None

        track_items: List[Any] = []
        seen_track_ids: set[int] = set()
        for album_id, album_title, artist_name in contexts:
            try:
                track_results = self._tracks_for_album(
                    album_id=album_id,
                    album_title=album_title,
                    artist_name=artist_name,
                    limit=500,
                )
            except Exception:
                track_results = []
            for track in track_results or []:
                try:
                    metadata = getattr(track, "full_metadata", None)
                    track_id = None
                    if isinstance(metadata, dict):
                        raw_id = metadata.get("trackId") or metadata.get("id")
                        track_id = int(raw_id) if raw_id is not None else None
                    if track_id is not None:
                        if track_id in seen_track_ids:
                            continue
                        seen_track_ids.add(track_id)
                except Exception:
                    pass
                track_items.append(track)

        return track_items or None