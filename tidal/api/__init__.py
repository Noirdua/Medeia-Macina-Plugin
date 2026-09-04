from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from API.base import API, ApiError
from SYS.logger import debug, debug_panel

DEFAULT_BASE_URL = "https://tidal-api.binimum.org"


def _debug_payload_summary(title: str, payload: Any) -> None:
    try:
        keys = list(payload.keys()) if isinstance(payload, dict) else []
    except Exception:
        keys = []

    preview = ", ".join(str(key) for key in keys[:8]) if keys else "<none>"
    if keys and len(keys) > 8:
        preview = f"{preview}, ..."

    try:
        payload_size = len(str(payload))
    except Exception:
        payload_size = "<unknown>"

    debug_panel(
        title,
        [
            ("type", type(payload).__name__),
            ("keys", preview),
            ("size", payload_size),
        ],
        border_style="cyan",
    )


def stringify(value: Any) -> str:
    """Helper to ensure we have a stripped string or empty."""
    return str(value or "").strip()


def extract_artists(item: Dict[str, Any]) -> List[str]:
    """Extract list of artist names from a Tidal-style metadata dict."""
    names: List[str] = []
    artists = item.get("artists")
    if isinstance(artists, list):
        for artist in artists:
            if isinstance(artist, dict):
                name = stringify(artist.get("name"))
                if name and name not in names:
                    names.append(name)
    if not names:
        primary = item.get("artist")
        if isinstance(primary, dict):
            name = stringify(primary.get("name"))
            if name:
                names.append(name)
    return names


def build_track_tags(metadata: Dict[str, Any]) -> Set[str]:
    """Create a set of searchable tags from track metadata."""
    tags: Set[str] = {"tidal"}

    audio_quality = stringify(metadata.get("audioQuality"))
    if audio_quality:
        tags.add(f"quality:{audio_quality.lower()}")

    media_md = metadata.get("mediaMetadata")
    if isinstance(media_md, dict):
        tag_values = media_md.get("tags") or []
        for tag in tag_values:
            if isinstance(tag, str):
                candidate = tag.strip()
                if candidate:
                    tags.add(candidate.lower())

    title_text = stringify(metadata.get("title"))
    if title_text:
        tags.add(f"title:{title_text}")

    artists = extract_artists(metadata)
    for artist in artists:
        artist_clean = stringify(artist)
        if artist_clean:
            tags.add(f"artist:{artist_clean}")

    album_title = ""
    album_obj = metadata.get("album")
    if isinstance(album_obj, dict):
        album_title = stringify(album_obj.get("title"))
    else:
        album_title = stringify(metadata.get("album"))
    if album_title:
        tags.add(f"album:{album_title}")

    track_no_val = metadata.get("trackNumber") or metadata.get("track_number")
    if track_no_val is not None:
        try:
            track_int = int(track_no_val)
            if track_int > 0:
                tags.add(f"track:{track_int}")
        except Exception:
            track_text = stringify(track_no_val)
            if track_text:
                tags.add(f"track:{track_text}")

    return tags


def parse_track_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Parse raw Tidal track data into a clean, flat dictionary.
    
    Extracts core fields: id, title, duration, Track:, url, artist name, and album title.
    """
    if not isinstance(item, dict):
        return {}

    # Handle the "data" wrapper if present
    data = item.get("data") if isinstance(item.get("data"), dict) else item

    artist_name = ""
    artist_obj = data.get("artist")
    if isinstance(artist_obj, dict):
        artist_name = stringify(artist_obj.get("name"))
    if not artist_name:
        artists = extract_artists(data)
        if artists:
            artist_name = artists[0]

    album_title = ""
    album_obj = data.get("album")
    if isinstance(album_obj, dict):
        album_title = stringify(album_obj.get("title"))
    if not album_title and isinstance(data.get("album"), str):
        album_title = stringify(data.get("album"))

    return {
        "id": data.get("id"),
        "title": stringify(data.get("title")),
        "duration": data.get("duration"),
        "Track:": data.get("trackNumber"),
        "url": stringify(data.get("url")),
        "artist": artist_name,
        "album": album_title,
    }


def coerce_duration_seconds(value: Any) -> Optional[int]:
    """Attempt to extracts seconds from various Tidal duration formats."""
    candidates = [value]
    try:
        if isinstance(value, dict):
            for key in (
                "duration",
                "durationSeconds",
                "duration_sec",
                "duration_ms",
                "durationMillis",
            ):
                if key in value:
                    candidates.append(value.get(key))
    except Exception:
        pass

    for cand in candidates:
        try:
            if cand is None:
                continue
            text = str(cand).strip()
            if text.lower().endswith("ms"):
                text = text[:-2].strip()
            num = float(text)
            if num <= 0:
                continue
            if num > 10_000:
                # Suspect milliseconds
                num = num / 1000.0
            return int(round(num))
        except Exception:
            continue
    return None


class TidalApiError(ApiError):
    """Raised when the Tidal API returns an error or malformed response."""


class Tidal(API):
    """Client for the Tidal (Tidal) API endpoints.
    
    This client communicates with the configured Tidal backend to retrieve
    track metadata, manifests, search results, and lyrics.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 10.0) -> None:
        super().__init__(base_url, timeout)

    def search(self, params: Dict[str, str]) -> Dict[str, Any]:
        usable = {k: v for k, v in (params or {}).items() if v}
        search_keys = [key for key in ("s", "a", "v", "p") if usable.get(key)]
        if not search_keys:
            raise TidalApiError("One of s/a/v/p is required for /search/")
        if len(search_keys) > 1:
            first = search_keys[0]
            usable = {first: usable[first]}
        return self._get_json("search/", params=usable)

    def track(self, track_id: int, *, quality: Optional[str] = None) -> Dict[str, Any]:
        try:
            track_int = int(track_id)
        except Exception as exc:
            raise TidalApiError(f"track_id must be int-compatible: {exc}") from exc
        if track_int <= 0:
            raise TidalApiError("track_id must be positive")

        p: Dict[str, Any] = {"id": track_int}
        if quality:
            p["quality"] = str(quality)
        return self._get_json("track/", params=p)

    def info(self, track_id: int) -> Dict[str, Any]:
        """Fetch and parse core track metadata (id, title, artist, album, duration, etc)."""
        try:
            track_int = int(track_id)
        except Exception as exc:
            raise TidalApiError(f"track_id must be int-compatible: {exc}") from exc
        if track_int <= 0:
            raise TidalApiError("track_id must be positive")

        raw = self._get_json("info/", params={"id": track_int})
        return parse_track_item(raw)

    def album(self, album_id: int) -> Dict[str, Any]:
        """Fetch album details, including track list when provided by the backend."""
        try:
            album_int = int(album_id)
        except Exception as exc:
            raise TidalApiError(f"album_id must be int-compatible: {exc}") from exc
        if album_int <= 0:
            raise TidalApiError("album_id must be positive")

        return self._get_json("album/", params={"id": album_int})

    def lyrics(self, track_id: int) -> Dict[str, Any]:
        """Fetch lyrics (including subtitles/LRC) for a track."""
        try:
            track_int = int(track_id)
        except Exception as exc:
            raise TidalApiError(f"track_id must be int-compatible: {exc}") from exc
        if track_int <= 0:
            raise TidalApiError("track_id must be positive")

        return self._get_json("lyrics/", params={"id": track_int})

    def get_full_track_metadata(self, track_id: int) -> Dict[str, Any]:
        """
        Orchestrate fetching all details for a track:
        1. Base info (/info/)
        2. Playback/Quality info (/track/)
        3. Lyrics (/lyrics/)
        4. Derived tags
        """
        try:
            track_int = int(track_id)
        except Exception as exc:
            raise TidalApiError(f"track_id must be int-compatible: {exc}") from exc

        # 1. Fetch info (metadata) - fetch raw to ensure all fields are available for merging
        info_resp = self._get_json("info/", params={"id": track_int})
        _debug_payload_summary("plugins.tidal.api info", info_resp)
        info_data = info_resp.get("data") if isinstance(info_resp, dict) else info_resp
        if not isinstance(info_data, dict) or "id" not in info_data:
            info_data = info_resp if isinstance(info_resp, dict) and "id" in info_resp else {}

        # 2. Fetch track (manifest/bit depth)
        track_resp = self.track(track_id)
        _debug_payload_summary("plugins.tidal.api track", track_resp)
        # Note: track() method in this class currently returns raw JSON, so we handle it similarly.
        track_data = track_resp.get("data") if isinstance(track_resp, dict) else track_resp
        if not isinstance(track_data, dict):
            track_data = track_resp if isinstance(track_resp, dict) else {}

        # 3. Fetch lyrics
        lyrics_data = {}
        try:
            lyr_resp = self.lyrics(track_id)
            _debug_payload_summary("plugins.tidal.api lyrics", lyr_resp)
            lyrics_data = lyr_resp.get("lyrics") or lyr_resp if isinstance(lyr_resp, dict) else {}
        except Exception:
            pass

        # Merged data for tags and parsing
        merged_md = {}
        if isinstance(info_data, dict):
            merged_md.update(info_data)
        if isinstance(track_data, dict):
            merged_md.update(track_data)

        # Derived tags and normalized/parsed info
        tags = build_track_tags(merged_md)
        parsed_info = parse_track_item(merged_md)

        # Structure for return
        res = {
            "metadata": merged_md,
            "parsed": parsed_info,
            "tags": list(tags),
            "lyrics": lyrics_data,
        }
        debug_panel(
            "plugins.tidal.api full track metadata",
            [
                ("track_id", track_int),
                ("metadata_keys", len(merged_md)),
                ("tags", len(tags)),
                ("has_lyrics", bool(lyrics_data)),
                ("result_keys", ", ".join(res.keys())),
            ],
            border_style="cyan",
        )
        return res
