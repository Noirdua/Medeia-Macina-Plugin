"""yt-dlp search and download plugin.

This plugin owns all yt-dlp-specific search, picker, and download behavior so
cmdlets can treat it as a generic URL-handling plugin.
"""

from __future__ import annotations

import re
import sys
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from PluginCore.base import Plugin, SearchResult, parse_inline_query_arguments
from SYS.plugin_helpers import TablePluginMixin
from SYS.logger import debug, log
from SYS.models import DownloadError, DownloadMediaResult, DownloadOptions
from SYS.payload_builders import build_file_result_payload, build_table_result_payload
from SYS.pipeline_progress import PipelineProgress
from SYS.result_table import Table
from SYS.rich_display import stderr_console as get_stderr_console
from SYS import pipeline as pipeline_context
from SYS.utils import sha256_file
from .tooling import (
    YtDlpTool,
    _best_chat_sidecar,
    _best_subtitle_sidecar,
    _format_live_chat_note,
    _SUBTITLE_EXTS,
    _download_with_timeout,
    _format_chapters_note,
    config_schema as _ytdlp_config_schema,
    _read_text_file,
    collapse_picker_formats,
    format_for_table_selection,
    get_display_format_id,
    get_selection_format_id,
    is_browseable_format,
    is_url_supported_by_ytdlp,
    list_formats,
    probe_url,
)

_FORMAT_INDEX_RE = re.compile(r"^\s*#?\d+\s*$")


def _parse_keyed_csv_spec(spec: str, *, default_key: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    text = str(spec or "").strip()
    if not text:
        return out

    active = str(default_key or "").strip().lower() or "clip"
    key_pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")

    for raw_piece in text.split(","):
        piece = raw_piece.strip()
        if not piece:
            continue

        match = key_pattern.match(piece)
        if match:
            active = (match.group(1) or "").strip().lower() or active
            value = (match.group(2) or "").strip()
            if value:
                out.setdefault(active, []).append(value)
            continue

        out.setdefault(active, []).append(piece)

    return out


def _parse_query_keyed_spec(query_spec: Optional[str]) -> Dict[str, List[str]]:
    if not query_spec:
        return {}
    keyed = _parse_keyed_csv_spec(str(query_spec), default_key="hash")
    if not keyed:
        return {}

    def _alias(src: str, dest: str) -> None:
        values = keyed.get(src)
        if not values:
            return
        keyed.setdefault(dest, []).extend(list(values))
        keyed.pop(src, None)

    for src in ("range", "ranges", "section", "sections"):
        _alias(src, "clip")
    for src in ("fmt", "f"):
        _alias(src, "format")
    for src in ("aud", "a"):
        _alias(src, "audio")

    return keyed


def _to_seconds(ts: str) -> Optional[int]:
    text = str(ts or "").strip()
    if not text:
        return None

    unit_match = re.fullmatch(
        r"(?i)\s*(?:(?P<h>\d+)h)?\s*(?:(?P<m>\d+)m)?\s*(?:(?P<s>\d+(?:\.\d+)?)s)?\s*",
        text,
    )
    if unit_match and unit_match.group(0).strip() and any(unit_match.group(g) for g in ("h", "m", "s")):
        try:
            hours = int(unit_match.group("h") or 0)
            minutes = int(unit_match.group("m") or 0)
            seconds = float(unit_match.group("s") or 0)
            return int((hours * 3600) + (minutes * 60) + seconds)
        except Exception:
            return None

    if ":" in text:
        parts = [p.strip() for p in text.split(":")]
        if len(parts) == 2:
            hh_s = "0"
            mm_s, ss_s = parts
        elif len(parts) == 3:
            hh_s, mm_s, ss_s = parts
        else:
            return None
        try:
            hours = int(hh_s)
            minutes = int(mm_s)
            seconds = float(ss_s)
            return int((hours * 3600) + (minutes * 60) + seconds)
        except Exception:
            return None

    try:
        return int(float(text))
    except Exception:
        return None


def _parse_time_ranges(spec: str) -> List[tuple[int, int]]:
    ranges: List[tuple[int, int]] = []
    if not spec:
        return ranges

    for piece in str(spec).split(","):
        piece = piece.strip()
        if not piece or "-" not in piece:
            return []
        start_s, end_s = [p.strip() for p in piece.split("-", 1)]
        start = _to_seconds(start_s)
        end = _to_seconds(end_s)
        if start is None or end is None or start >= end:
            return []
        ranges.append((start, end))
    return ranges


def _build_clip_sections_spec(clip_ranges: Optional[List[tuple[int, int]]]) -> Optional[str]:
    if not clip_ranges:
        return None
    return ",".join(f"{start_s}-{end_s}" for start_s, end_s in clip_ranges)


def _format_timecode(seconds: int, *, force_hours: bool) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if force_hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _rebase_subtitle_timestamp_text(text: str, offset_seconds: int) -> str:
    if not text:
        return text
    try:
        offset_value = float(offset_seconds)
    except Exception:
        return text
    if offset_value <= 0:
        return text

    timestamp_re = re.compile(r"(?<!\d)(?P<ts>(?:\d{2}:)?\d{2}:\d{2}(?:[\.,]\d{1,3})?)(?!\d)")

    def _shift(match: re.Match[str]) -> str:
        original = str(match.group("ts") or "")
        if not original:
            return original

        frac_sep = "."
        frac_digits = 0
        base = original
        frac_seconds = 0.0
        if "." in original:
            base, frac = original.split(".", 1)
            frac_sep = "."
            frac_digits = len(frac)
            frac_seconds = float(f"0.{frac}") if frac else 0.0
        elif "," in original:
            base, frac = original.split(",", 1)
            frac_sep = ","
            frac_digits = len(frac)
            frac_seconds = float(f"0.{frac}") if frac else 0.0

        parts = base.split(":")
        if len(parts) == 3:
            hours_s, minutes_s, seconds_s = parts
            include_hours = True
        elif len(parts) == 2:
            hours_s = "0"
            minutes_s, seconds_s = parts
            include_hours = False
        else:
            return original

        total = (
            (int(hours_s) * 3600)
            + (int(minutes_s) * 60)
            + int(seconds_s)
            + frac_seconds
            + offset_value
        )
        total = max(0.0, total)
        whole_seconds = int(total)
        fraction = total - whole_seconds
        hours, remainder = divmod(whole_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        if frac_digits > 0:
            scale = 10 ** frac_digits
            frac_value = int(round(fraction * scale))
            if frac_value >= scale:
                frac_value = 0
                seconds += 1
                if seconds >= 60:
                    seconds = 0
                    minutes += 1
                    if minutes >= 60:
                        minutes = 0
                        hours += 1
            frac_text = f"{frac_value:0{frac_digits}d}"
            if include_hours or hours > 0:
                return f"{hours:02d}:{minutes:02d}:{seconds:02d}{frac_sep}{frac_text}"
            return f"{minutes:02d}:{seconds:02d}{frac_sep}{frac_text}"

        if include_hours or hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    try:
        return timestamp_re.sub(_shift, str(text))
    except Exception:
        return text


def _format_clip_range(start_s: int, end_s: int) -> str:
    force_hours = bool(start_s >= 3600 or end_s >= 3600)
    return f"{_format_timecode(start_s, force_hours=force_hours)}-{_format_timecode(end_s, force_hours=force_hours)}"


def _apply_clip_decorations(pipe_objects: List[Dict[str, Any]], clip_ranges: List[tuple[int, int]]) -> None:
    if not pipe_objects or len(pipe_objects) != len(clip_ranges):
        return

    for po, (start_s, end_s) in zip(pipe_objects, clip_ranges):
        clip_range = _format_clip_range(start_s, end_s)
        clip_tag = f"clip:{clip_range}"
        po["title"] = clip_tag

        tags = po.get("tag")
        if not isinstance(tags, list):
            tags = []
        tags = [t for t in tags if not str(t).strip().lower().startswith("title:")]
        tags = [t for t in tags if not str(t).strip().lower().startswith("relationship:")]
        tags.insert(0, f"title:{clip_tag}")
        if clip_tag not in tags:
            tags.append(clip_tag)
        po["tag"] = tags

        notes = po.get("notes")
        if isinstance(notes, dict):
            sub_text = notes.get("sub")
            if isinstance(sub_text, str) and sub_text.strip():
                notes["sub"] = _rebase_subtitle_timestamp_text(sub_text, start_s)
                po["notes"] = notes

    if len(pipe_objects) < 2:
        return

    hashes: List[str] = []
    for po in pipe_objects:
        try:
            hashes.append(str(po.get("hash") or "").strip().lower())
        except Exception:
            hashes.append("")
    king_hash = hashes[0] if hashes and hashes[0] else None
    if not king_hash:
        return
    alt_hashes = [h for h in hashes if h and h != king_hash]
    if not alt_hashes:
        return
    for po in pipe_objects:
        po["relationships"] = {"king": [king_hash], "alt": list(alt_hashes)}


def _cookiefile_str(ytdlp_tool: YtDlpTool) -> Optional[str]:
    try:
        cookie_path = ytdlp_tool.resolve_cookiefile()
        if cookie_path is not None and cookie_path.is_file():
            return str(cookie_path)
    except Exception:
        pass
    return None


def _list_formats_cached(
    url: str,
    *,
    playlist_items_value: Optional[str],
    formats_cache: Dict[str, Optional[List[Dict[str, Any]]]],
    ytdlp_tool: YtDlpTool,
) -> Optional[List[Dict[str, Any]]]:
    key = f"{url}||{playlist_items_value or ''}"
    if key in formats_cache:
        return formats_cache[key]
    fmts = list_formats(
        url,
        no_playlist=False,
        playlist_items=playlist_items_value,
        cookiefile=_cookiefile_str(ytdlp_tool),
    )
    formats_cache[key] = fmts
    return fmts


def _format_id_for_query_index(
    query_format: str,
    url: str,
    formats_cache: Dict[str, Optional[List[Dict[str, Any]]]],
    ytdlp_tool: YtDlpTool,
) -> Optional[str]:
    if not query_format or not _FORMAT_INDEX_RE.match(str(query_format)):
        return None

    s_val = str(query_format).strip()
    idx = int(s_val.lstrip("#"))
    fmts = _list_formats_cached(
        url,
        playlist_items_value=None,
        formats_cache=formats_cache,
        ytdlp_tool=ytdlp_tool,
    )
    if not fmts:
        raise ValueError("Unable to list formats for the URL")

    if s_val and not s_val.startswith("#"):
        for item in fmts:
            if str(item.get("format_id", "")) == s_val:
                normalized = get_selection_format_id(item, video_audio_suffix="bestaudio")
                return normalized or s_val

    candidate_formats = collapse_picker_formats(fmts, video_audio_suffix="bestaudio")
    if s_val and not s_val.startswith("#"):
        for item in candidate_formats:
            if get_display_format_id(item) == s_val:
                normalized = get_selection_format_id(item, video_audio_suffix="bestaudio")
                return normalized or s_val

    filtered_formats = candidate_formats if candidate_formats else list(fmts)
    if idx <= 0 or idx > len(filtered_formats):
        raise ValueError(f"Format index {idx} out of range")

    chosen = filtered_formats[idx - 1]
    selection_format_id = get_selection_format_id(chosen, video_audio_suffix="bestaudio")
    if not selection_format_id:
        raise ValueError("Selected format has no format_id")
    return selection_format_id


def _merge_query_args(selection_args: List[str], query_value: str) -> List[str]:
    if not query_value:
        return selection_args
    merged = list(selection_args or [])
    if "-query" in merged:
        idx_query = merged.index("-query")
        if idx_query + 1 < len(merged):
            existing = str(merged[idx_query + 1] or "").strip()
            merged[idx_query + 1] = f"{existing},{query_value}" if existing else query_value
        else:
            merged.append(query_value)
    else:
        merged.extend(["-query", query_value])
    return merged


def _build_pipe_objects(
    result_obj: Any,
    *,
    url: str,
    opts: DownloadOptions,
    embed_chapters: bool,
    write_sub: bool,
) -> List[Dict[str, Any]]:
    results_to_emit: List[Any]
    if isinstance(result_obj, list):
        results_to_emit = list(result_obj)
    else:
        paths = getattr(result_obj, "paths", None)
        if isinstance(paths, list) and paths:
            results_to_emit = []
            for p in paths:
                try:
                    p_path = Path(p)
                except Exception:
                    continue
                try:
                    if p_path.suffix.lower() in _SUBTITLE_EXTS:
                        continue
                except Exception:
                    pass
                if not p_path.exists() or p_path.is_dir():
                    continue
                try:
                    hv = sha256_file(p_path)
                except Exception:
                    hv = None
                results_to_emit.append(
                    DownloadMediaResult(
                        path=p_path,
                        info=getattr(result_obj, "info", {}) or {},
                        tag=list(getattr(result_obj, "tag", []) or []),
                        source_url=getattr(result_obj, "source_url", None) or opts.url,
                        hash_value=hv,
                    )
                )
        else:
            results_to_emit = [result_obj]

    pipe_objects: List[Dict[str, Any]] = []
    pipe_seq = 0
    for downloaded in results_to_emit:
        info: Dict[str, Any] = downloaded.info if isinstance(getattr(downloaded, "info", None), dict) else {}
        media_path = Path(downloaded.path)
        hash_value = getattr(downloaded, "hash_value", None) or sha256_file(media_path)
        title = info.get("title") or media_path.stem
        tag = list(getattr(downloaded, "tag", []) or [])
        if title and f"title:{title}" not in tag:
            tag.insert(0, f"title:{title}")

        final_url = None
        try:
            page_url = info.get("webpage_url") or info.get("original_url") or info.get("url")
            if page_url:
                final_url = str(page_url)
        except Exception:
            final_url = None
        if not final_url:
            final_url = str(url)

        po = build_file_result_payload(
            title=title,
            path=str(media_path),
            hash_value=hash_value,
            url=final_url,
            tag=tag,
            store=getattr(opts, "storage_name", None) or getattr(opts, "storage_location", None) or "PATH",
            action="cmdlet:download-file",
            is_temp=True,
            ytdl_format=getattr(opts, "ytdl_format", None),
            media_kind="video" if opts.mode == "video" else "audio",
        )
        pipe_seq += 1
        po.setdefault("pipe_index", pipe_seq)

        if embed_chapters:
            chapters_text = _format_chapters_note(info)
            if chapters_text:
                notes = po.get("notes")
                if not isinstance(notes, dict):
                    notes = {}
                notes.setdefault("chapters", chapters_text)
                po["notes"] = notes

        if write_sub:
            try:
                sub_path = _best_subtitle_sidecar(media_path)
            except Exception:
                sub_path = None
            if sub_path is not None:
                sub_text = _read_text_file(sub_path)
                if sub_text:
                    notes = po.get("notes")
                    if not isinstance(notes, dict):
                        notes = {}
                    notes["sub"] = sub_text
                    po["notes"] = notes
                try:
                    sub_path.unlink()
                except Exception:
                    pass

        try:
            chat_path = _best_chat_sidecar(media_path)
        except Exception:
            chat_path = None
        if chat_path is not None:
            chat_text = _format_live_chat_note(chat_path)
            if chat_text:
                notes = po.get("notes")
                if not isinstance(notes, dict):
                    notes = {}
                notes["chat"] = chat_text
                po["notes"] = notes
            try:
                chat_path.unlink()
            except Exception:
                pass

        pipe_objects.append(po)
    return pipe_objects


class ytdlp(TablePluginMixin, Plugin):
    """yt-dlp-backed search and direct download plugin."""

    PLUGIN_NAME = "ytdlp"
    PLUGIN_SYSTEM_REQUIRES = ("deno",)
    PLUGIN_ALIASES = ("youtube",)
    SEARCH_QUERY_KEYS = ("search", "q")
    SUPPORTED_CMDLETS = frozenset({"download-file", "search-file"})

    QUERY_ARG_CHOICES = {
        "format": ["audio", "1080", "720", "480", "360", "best", "worst", "bestvideo+bestaudio"],
        "clip": ["0:00-1:00"],
        "audio": ["true", "false", "1", "0"],
        "item": ["1"],
    }
    INLINE_QUERY_FIELD_CHOICES = QUERY_ARG_CHOICES

    @staticmethod
    def config_schema() -> List[Dict[str, Any]]:
        return _ytdlp_config_schema()

    @classmethod
    def url_patterns(cls) -> Tuple[str, ...]:
        try:
            import yt_dlp

            domains = set(cls._fallback_domains)
            try:
                extractors = yt_dlp.gen_extractors()
                for extractor_class in extractors:
                    name = getattr(extractor_class, "IE_NAME", "")
                    if name and name not in ("generic", "http"):
                        name_lower = name.lower().replace("ie", "").strip()
                        if name_lower and len(name_lower) > 2:
                            domains.add(f"{name_lower}.com")
            except Exception:
                pass
            return tuple(domains) if domains else tuple(cls._fallback_domains)
        except Exception:
            return tuple(cls._fallback_domains)

    _fallback_domains = [
        "youtube.com", "youtu.be",
        "bandcamp.com",
        "vimeo.com",
        "twitch.tv",
        "dailymotion.com",
        "rumble.com",
        "odysee.com",
    ]

    TABLE_AUTO_STAGES = {
        "ytdlp.formatlist": ["download-file"],
        "youtube": ["download-file"],
        "bandcamp": ["download-file"],
    }
    AUTO_STAGE_USE_SELECTION_ARGS = True

    @staticmethod
    def _playlist_entry_to_url(entry: Any, *, extractor_name: str) -> Optional[str]:
        if not isinstance(entry, dict):
            return None
        for key in ("webpage_url", "original_url", "url"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                cleaned = value.strip()
                try:
                    if urlparse(cleaned).scheme in {"http", "https"}:
                        return cleaned
                except Exception:
                    return cleaned
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id.strip() and "youtube" in extractor_name:
            return f"https://www.youtube.com/watch?v={entry_id.strip()}"
        return None

    def resolve_preflight_items(self, url: str, **kwargs: Any) -> Optional[List[Dict[str, Any]]]:
        url_str = str(url or "").strip()
        if not url_str or not is_url_supported_by_ytdlp(url_str):
            return None

        parsed = kwargs.get("parsed") if isinstance(kwargs.get("parsed"), dict) else {}
        query_spec = parsed.get("query")
        query_keyed = _parse_query_keyed_spec(str(query_spec) if query_spec is not None else None)

        playlist_items = str(parsed.get("item")) if parsed.get("item") else None
        item_values: List[str] = []
        if isinstance(query_keyed, dict):
            item_values.extend(query_keyed.get("item", []) or [])
        if item_values and not playlist_items:
            playlist_items = ",".join([value for value in item_values if value])

        ytdlp_tool = YtDlpTool(self.config)
        try:
            probe = probe_url(
                url_str,
                no_playlist=False,
                playlist_items=playlist_items,
                timeout_seconds=15,
                cookiefile=_cookiefile_str(ytdlp_tool),
            )
        except Exception:
            probe = None

        if not isinstance(probe, dict):
            return None

        entries = probe.get("entries")
        if not isinstance(entries, list) or not entries:
            return None

        extractor_name = str(probe.get("extractor") or probe.get("extractor_key") or "").strip().lower()
        items: List[Dict[str, Any]] = []
        for idx, entry in enumerate(entries, 1):
            entry_url = self._playlist_entry_to_url(entry, extractor_name=extractor_name)
            if not entry_url:
                continue
            playlist_index = None
            if isinstance(entry, dict):
                playlist_index = entry.get("playlist_index")
            try:
                playlist_index_value = int(playlist_index)
            except Exception:
                playlist_index_value = idx
            items.append(
                {
                    "url": entry_url,
                    "playlist_index": playlist_index_value,
                }
            )

        return items or None

    def extract_query_arguments(self, query: str) -> Tuple[str, Dict[str, Any]]:
        normalized_query, inline_args = parse_inline_query_arguments(query)
        search_parts: List[str] = []

        for key in self.SEARCH_QUERY_KEYS:
            value = str(inline_args.pop(key, "") or "").strip()
            if value:
                search_parts.append(value)

        if normalized_query:
            search_parts.append(normalized_query)

        resolved_query = " ".join(part for part in search_parts if part).strip()
        if not resolved_query:
            resolved_query = str(query or "").strip()

        filters: Dict[str, Any] = dict(inline_args or {})
        filters.setdefault("search_provider", "youtube")
        return resolved_query, filters

    def get_table_type(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
    ) -> str:
        _ = query, filters
        return "youtube"

    def get_table_title(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
    ) -> str:
        _ = filters
        q = str(query or "").strip() or "*"
        return f"YouTube: {q}"

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        _ = filters
        _ = kwargs
        try:
            import yt_dlp  # type: ignore

            ydl_opts: Dict[str, Any] = {
                "quiet": True,
                "skip_download": True,
                "extract_flat": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                search_query = f"ytsearch{limit}:{query}"
                info = ydl.extract_info(search_query, download=False)
                entries = info.get("entries") or []
                results: List[SearchResult] = []
                for video_data in entries[:limit]:
                    title = video_data.get("title", "Unknown")
                    video_id = video_data.get("id", "")
                    url = video_data.get("url") or f"https://youtube.com/watch?v={video_id}"
                    uploader = video_data.get("uploader", "Unknown")
                    duration = video_data.get("duration", 0)
                    view_count = video_data.get("view_count", 0)
                    duration_str = f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else ""
                    views_str = f"{view_count:,}" if view_count else ""

                    results.append(
                        SearchResult(
                            table="youtube",
                            title=title,
                            path=url,
                            detail=f"By: {uploader}",
                            annotations=[duration_str, f"{views_str} views"],
                            media_kind="video",
                            columns=[
                                ("Title", title),
                                ("Uploader", uploader),
                                ("Duration", duration_str),
                                ("Views", views_str),
                            ],
                            full_metadata={
                                "video_id": video_id,
                                "uploader": uploader,
                                "duration": duration,
                                "view_count": view_count,
                                "_selection_args": ["-url", url],
                            },
                        )
                    )
                return results
        except Exception:
            debug("[ytdlp] yt_dlp import or search failed")
            return []

    def validate(self) -> bool:
        return True

    def list_url_formats(self, url: str, **kwargs: Any) -> Optional[List[Dict[str, Any]]]:
        url_str = str(url or "").strip()
        if not url_str:
            return None

        no_playlist = bool(kwargs.get("no_playlist", True))
        timeout_seconds = kwargs.get("timeout_seconds")
        playlist_items = kwargs.get("playlist_items")

        ytdlp_tool = YtDlpTool(self.config)
        cookiefile = _cookiefile_str(ytdlp_tool)

        call_kwargs: Dict[str, Any] = {
            "no_playlist": no_playlist,
            "playlist_items": playlist_items,
            "cookiefile": cookiefile,
        }
        if timeout_seconds is not None:
            call_kwargs["timeout_seconds"] = timeout_seconds

        try:
            formats = list_formats(url_str, **call_kwargs)
        except TypeError:
            call_kwargs.pop("timeout_seconds", None)
            formats = list_formats(url_str, **call_kwargs)
        return formats if isinstance(formats, list) else None

    def filter_picker_formats(
        self,
        formats: List[Dict[str, Any]],
        **_kwargs: Any,
    ) -> List[Dict[str, Any]]:
        if not isinstance(formats, list):
            return []
        browseable = collapse_picker_formats(formats, video_audio_suffix="ba")
        return browseable if browseable else list(formats)

    def enrich_playlist_entries(
        self,
        entries: List[Dict[str, Any]],
        **_kwargs: Any,
    ) -> Optional[List[Dict[str, Any]]]:
        if not entries:
            return []

        enriched: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            entry_url = entry.get("url")
            if not isinstance(entry_url, str) or not entry_url.strip():
                enriched.append(entry)
                continue

            try:
                import yt_dlp

                ydl_opts: Dict[str, Any] = {
                    "quiet": True,
                    "no_warnings": True,
                    "skip_download": True,
                    "noprogress": True,
                    "socket_timeout": 5,
                    "retries": 1,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    full_info = ydl.extract_info(entry_url, download=False)
                if isinstance(full_info, dict):
                    enriched.append(full_info)
                    continue
            except Exception:
                debug(f"[ytdlp] failed to fetch full metadata for entry URL: {entry_url}")

            enriched.append(entry)

        return enriched

    def _show_playlist_table(self, *, url: str, ytdlp_tool: YtDlpTool) -> bool:
        ctx = pipeline_context.get_stage_context()
        if ctx is not None and getattr(ctx, "total_stages", 0) > 1:
            return False

        try:
            pr = probe_url(url, no_playlist=False, timeout_seconds=15, cookiefile=_cookiefile_str(ytdlp_tool))
        except Exception:
            pr = None
        if not isinstance(pr, dict):
            return False

        entries = pr.get("entries")
        if not isinstance(entries, list) or len(entries) <= 1:
            return False

        extractor_name = str(pr.get("extractor") or pr.get("extractor_key") or "").strip().lower()
        table_type: Optional[str] = None
        if "bandcamp" in extractor_name:
            table_type = "bandcamp"
        elif "youtube" in extractor_name:
            table_type = "youtube"

        table = Table(preserve_order=True)
        safe_url = str(url or "").strip()
        table.title = f'download-file -url "{safe_url}"' if safe_url else "download-file"
        if table_type:
            try:
                table.set_table(table_type)
            except Exception:
                table.table = table_type
        table.set_source_command("download-file", [])
        try:
            table._perseverance(True)
        except Exception:
            pass

        results_list: List[Dict[str, Any]] = []
        for idx, entry in enumerate(entries[:200], 1):
            title = entry.get("title") if isinstance(entry, dict) else None
            uploader = entry.get("uploader") if isinstance(entry, dict) else None
            duration = entry.get("duration") if isinstance(entry, dict) else None
            entry_url = self._playlist_entry_to_url(entry, extractor_name=extractor_name)
            row = build_table_result_payload(
                table="download-file",
                title=str(title or f"Item {idx}"),
                detail=str(uploader or ""),
                columns=[
                    ("#", str(idx)),
                    ("Title", str(title or "")),
                    ("Duration", str(duration or "")),
                    ("Uploader", str(uploader or "")),
                ],
                selection_args=(
                    ["-url", str(entry_url)] if entry_url else ["-url", str(url), "-item", str(idx)]
                ),
                selection_action=(
                    ["download-file", "-url", str(entry_url)]
                    if entry_url
                    else ["download-file", "-url", str(url), "-item", str(idx)]
                ),
                media_kind="playlist-item",
                playlist_index=idx,
                url=entry_url,
                target=entry_url,
            )
            results_list.append(row)
            table.add_result(row)

        pipeline_context.set_current_stage_table(table)
        pipeline_context.set_last_result_table(table, results_list)
        try:
            suspend = getattr(pipeline_context, "suspend_live_progress", None)
            cm: AbstractContextManager[Any] = nullcontext()
            if callable(suspend):
                maybe_cm = suspend()
                if maybe_cm is not None:
                    cm = maybe_cm  # type: ignore[assignment]
            with cm:
                get_stderr_console().print(table)
        except Exception:
            pass
        setattr(table, "_rendered_by_cmdlet", True)
        return True

    def _show_format_table(
        self,
        *,
        url: str,
        args: Sequence[str],
        clip_spec: Optional[str],
        clip_values: Sequence[str],
        ytdlp_tool: YtDlpTool,
        formats_cache: Dict[str, Optional[List[Dict[str, Any]]]],
    ) -> bool:
        ctx = pipeline_context.get_stage_context()
        if ctx is not None and getattr(ctx, "total_stages", 0) > 1:
            return False

        formats = _list_formats_cached(
            url,
            playlist_items_value=None,
            formats_cache=formats_cache,
            ytdlp_tool=ytdlp_tool,
        )
        if not formats or len(formats) <= 1:
            return False

        candidate_formats = collapse_picker_formats(formats, video_audio_suffix="bestaudio")
        filtered_formats = candidate_formats if candidate_formats else list(formats)
        base_cmd = f'download-file "{url}"'
        remaining_args = [arg for arg in args if arg not in [url] and not str(arg).startswith("-")]
        if remaining_args:
            base_cmd += " " + " ".join(remaining_args)

        table = Table(title=f"Available formats for {url}", max_columns=10, preserve_order=True)
        table.set_table("ytdlp.formatlist")
        table.set_source_command("download-file", [url])

        results_list: List[Dict[str, Any]] = []
        for idx, fmt in enumerate(filtered_formats, 1):
            selection_format_id = get_selection_format_id(fmt, video_audio_suffix="bestaudio")

            format_dict = format_for_table_selection(
                fmt,
                url,
                idx,
                selection_format_id=selection_format_id,
            )
            format_dict["cmd"] = base_cmd
            selection_args: List[str] = list(format_dict.get("_selection_args") or [])
            if (not clip_spec) and clip_values:
                clip_query = f"clip:{','.join([v for v in clip_values if v])}"
                selection_args = _merge_query_args(selection_args, clip_query)
            format_dict["_selection_args"] = selection_args
            format_dict.setdefault("full_metadata", {})["_selection_args"] = selection_args
            results_list.append(format_dict)
            table.add_result(format_dict)

        try:
            suspend = getattr(pipeline_context, "suspend_live_progress", None)
            cm: AbstractContextManager[Any] = nullcontext()
            if callable(suspend):
                maybe_cm = suspend()
                if maybe_cm is not None:
                    cm = maybe_cm  # type: ignore[assignment]
            with cm:
                get_stderr_console().print(table)
        except Exception:
            pass

        setattr(table, "_rendered_by_cmdlet", True)
        pipeline_context.set_current_stage_table(table)
        pipeline_context.set_last_result_table(table, results_list)
        return True

    def download_url(
        self,
        url: str,
        output_dir: Path,
        **kwargs: Any,
    ) -> Optional[Any]:
        url_str = str(url or "").strip()
        if not url_str or not is_url_supported_by_ytdlp(url_str):
            return None

        parsed = kwargs.get("parsed") if isinstance(kwargs.get("parsed"), dict) else {}
        args = kwargs.get("args") if isinstance(kwargs.get("args"), list) else []
        progress = kwargs.get("progress")
        quiet_mode = bool(kwargs.get("quiet_mode"))

        if progress is None:
            try:
                progress = self.config.get("_pipeline_progress") if isinstance(self.config, dict) else None
            except Exception:
                progress = None
        if progress is None:
            progress = PipelineProgress(pipeline_context)

        query_spec = parsed.get("query")
        clip_spec = parsed.get("clip")
        query_keyed = _parse_query_keyed_spec(str(query_spec) if query_spec is not None else None)
        clip_values: List[str] = []
        item_values: List[str] = []
        if clip_spec:
            keyed = _parse_keyed_csv_spec(str(clip_spec), default_key="clip")
            clip_values.extend(keyed.get("clip", []) or [])
            item_values.extend(keyed.get("item", []) or [])
        if query_keyed:
            clip_values.extend(query_keyed.get("clip", []) or [])
            item_values.extend(query_keyed.get("item", []) or [])

        if item_values and not parsed.get("item"):
            parsed["item"] = ",".join([v for v in item_values if v])

        clip_ranges = None
        if clip_values:
            clip_ranges = _parse_time_ranges(",".join([v for v in clip_values if v]))
            if not clip_ranges:
                log(f"Invalid clip format: {clip_spec or query_spec}", file=sys.stderr)
                return {"action": "handled", "exit_code": 1}

        ytdlp_tool = YtDlpTool(self.config)
        formats_cache: Dict[str, Optional[List[Dict[str, Any]]]] = {}
        playlist_items = str(parsed.get("item")) if parsed.get("item") else None
        query_format: Optional[str] = None
        try:
            fmt_values = query_keyed.get("format", []) if isinstance(query_keyed, dict) else []
            fmt_candidate = fmt_values[-1] if fmt_values else None
            if fmt_candidate is not None:
                query_format = str(fmt_candidate).strip()
        except Exception:
            query_format = None

        query_audio: Optional[bool] = None
        try:
            audio_values = query_keyed.get("audio", []) if isinstance(query_keyed, dict) else []
            audio_candidate = audio_values[-1] if audio_values else None
            if audio_candidate is not None:
                s_val = str(audio_candidate).strip().lower()
                if s_val in {"1", "true", "t", "yes", "y", "on"}:
                    query_audio = True
                elif s_val in {"0", "false", "f", "no", "n", "off"}:
                    query_audio = False
                elif s_val:
                    query_audio = True
        except Exception:
            query_audio = None

        query_wants_audio = bool(query_format and str(query_format).strip().lower() == "audio")
        wants_audio = bool(query_audio) if query_audio is not None else bool(query_wants_audio)
        try:
            host = urlparse(url_str).netloc.lower()
            path = urlparse(url_str).path.lower()
            if "bandcamp.com" in host and ("/track/" in path or "/album/" in path):
                wants_audio = True
        except Exception:
            pass
        mode = "audio" if wants_audio else "video"

        ytdl_format: Optional[str] = None
        height_selector = None
        if query_format and not query_wants_audio:
            try:
                height_selector = ytdlp_tool.resolve_height_selector(query_format)
            except Exception:
                height_selector = None
        if query_wants_audio:
            # bestaudio alone can fail on some YouTube clients; allow best muxed fallback.
            ytdl_format = "bestaudio/best"
        elif height_selector:
            ytdl_format = height_selector
        elif query_format:
            ytdl_format = query_format

        if not playlist_items:
            if query_format and not query_wants_audio and not ytdl_format:
                try:
                    idx_fmt = _format_id_for_query_index(query_format, url_str, formats_cache, ytdlp_tool)
                    if idx_fmt:
                        ytdl_format = idx_fmt
                except ValueError as exc:
                    debug(f"[ytdlp] Format resolution for '{query_format}' failed ({exc}); treating as literal")
                    ytdl_format = query_format

            if not ytdl_format and self._show_playlist_table(url=url_str, ytdlp_tool=ytdlp_tool):
                return {"action": "handled", "exit_code": 0}

            if (
                mode != "audio"
                and not clip_spec
                and not clip_values
                and not playlist_items
                and not ytdl_format
                and self._show_format_table(
                    url=url_str,
                    args=args,
                    clip_spec=str(clip_spec) if clip_spec is not None else None,
                    clip_values=clip_values,
                    ytdlp_tool=ytdlp_tool,
                    formats_cache=formats_cache,
                )
            ):
                return {"action": "handled", "exit_code": 0}

        if mode == "video" and not ytdl_format and not query_format and not query_wants_audio:
            try:
                fmts = _list_formats_cached(
                    url_str,
                    playlist_items_value=playlist_items,
                    formats_cache=formats_cache,
                    ytdlp_tool=ytdlp_tool,
                )
                if fmts:
                    has_video = any(str(f.get("vcodec", "none")) != "none" for f in fmts if isinstance(f, dict))
                    has_audio = any(str(f.get("acodec", "none")) != "none" for f in fmts if isinstance(f, dict))
                    if has_audio and not has_video:
                        mode = "audio"
                        ytdl_format = ytdlp_tool.default_format("audio")
                elif "bandcamp.com/album/" in url_str:
                    mode = "audio"
                    ytdl_format = ytdlp_tool.default_format("audio")
            except Exception as exc:
                debug(f"[ytdlp] Audio-only detection error: {exc}")

        if mode == "audio" and not ytdl_format:
            ytdl_format = "bestaudio/best"
        if mode == "video" and not ytdl_format:
            configured = (ytdlp_tool.default_format("video") or "").strip()
            if configured and configured != "bestvideo+bestaudio/best":
                resolved = ytdlp_tool.resolve_height_selector(configured)
                ytdl_format = resolved or configured

        clip_sections_spec = _build_clip_sections_spec(clip_ranges)
        if clip_sections_spec and mode != "audio":
            clip_format_basis = ytdl_format
            if not clip_format_basis or str(clip_format_basis).strip().lower() in {
                "bestvideo+bestaudio/best",
                "bestvideo+bestaudio",
                "best",
                "best/b",
                "best/best",
                "b",
            }:
                preferred_clip_format = str(getattr(ytdlp_tool.defaults, "format", "") or "").strip()
                if preferred_clip_format and preferred_clip_format.lower() != "audio":
                    clip_format_basis = preferred_clip_format
                else:
                    clip_format_basis = ytdlp_tool.default_format("video")
            clip_safe_format = ytdlp_tool.resolve_clip_safe_format(clip_format_basis)
            if clip_safe_format:
                ytdl_format = clip_safe_format

        timeout_seconds = 300
        try:
            override = self.config.get("_pipeobject_timeout_seconds") if isinstance(self.config, dict) else None
            if override is not None:
                timeout_seconds = max(1, int(override))
        except Exception:
            timeout_seconds = 300

        actual_format = ytdl_format
        actual_playlist_items = playlist_items
        if playlist_items and not ytdl_format and re.search(r"[^0-9,-]", playlist_items):
            actual_format = playlist_items
            actual_playlist_items = None

        attempted_single_format_fallback = False
        attempted_audio_fallback_specific = False
        attempted_audio_fallback_generic = False
        while True:
            try:
                opts = DownloadOptions(
                    url=url_str,
                    mode=mode,
                    output_dir=output_dir,
                    ytdl_format=actual_format,
                    cookies_path=ytdlp_tool.resolve_cookiefile(),
                    clip_sections=clip_sections_spec,
                    playlist_items=actual_playlist_items,
                    quiet=quiet_mode,
                    no_playlist=False,
                    embed_chapters=True,
                    write_sub=True,
                )
                result_obj = _download_with_timeout(opts, timeout_seconds=timeout_seconds, config=self.config)
                break
            except DownloadError as exc:
                cause = getattr(exc, "__cause__", None)
                detail = str(cause or "")
                msg_lc = str(exc or "").lower()
                detail_lc = detail.lower()
                requested_format_unavailable = (
                    "requested format is not available" in detail_lc
                    or "requested format is not available" in msg_lc
                )

                if requested_format_unavailable and mode == "audio":
                    if not attempted_audio_fallback_specific:
                        attempted_audio_fallback_specific = True
                        audio_format_id = None
                        try:
                            formats = _list_formats_cached(
                                url_str,
                                playlist_items_value=actual_playlist_items,
                                formats_cache=formats_cache,
                                ytdlp_tool=ytdlp_tool,
                            )
                            if formats:
                                audio_candidates = []
                                for fmt in formats:
                                    if not isinstance(fmt, dict):
                                        continue
                                    vcodec = str(fmt.get("vcodec", "none"))
                                    acodec = str(fmt.get("acodec", "none"))
                                    if acodec != "none" and vcodec == "none":
                                        audio_candidates.append(fmt)
                                if audio_candidates:
                                    def _score_audio(fmt: Dict[str, Any]) -> float:
                                        score = 0.0
                                        fid = str(fmt.get("format_id") or "").lower()
                                        if "drc" in fid:
                                            score -= 1000.0
                                        for key in ("abr", "tbr", "filesize", "filesize_approx"):
                                            val = fmt.get(key)
                                            if isinstance(val, (int, float)):
                                                score += float(val)
                                                break
                                            if isinstance(val, str) and val.strip().isdigit():
                                                score += float(val)
                                                break
                                        return score
                                    audio_candidates.sort(key=_score_audio, reverse=True)
                                    audio_format_id = str(audio_candidates[0].get("format_id") or "").strip() or None
                        except Exception:
                            audio_format_id = None
                        if audio_format_id:
                            actual_format = audio_format_id
                            continue

                    if not attempted_audio_fallback_generic and actual_format != "bestaudio/best":
                        attempted_audio_fallback_generic = True
                        actual_format = "bestaudio/best"
                        continue

                if requested_format_unavailable and mode != "audio":
                    formats = _list_formats_cached(
                        url_str,
                        playlist_items_value=actual_playlist_items,
                        formats_cache=formats_cache,
                        ytdlp_tool=ytdlp_tool,
                    )
                    if (
                        (not attempted_single_format_fallback)
                        and isinstance(formats, list)
                        and len(formats) == 1
                        and isinstance(formats[0], dict)
                    ):
                        only = formats[0]
                        fallback_format = str(only.get("format_id") or "").strip()
                        selection_format_id = fallback_format
                        try:
                            vcodec = str(only.get("vcodec", "none"))
                            acodec = str(only.get("acodec", "none"))
                            if not clip_sections_spec and vcodec != "none" and acodec == "none" and fallback_format:
                                selection_format_id = f"{fallback_format}+bestaudio"
                        except Exception:
                            selection_format_id = fallback_format
                        if selection_format_id:
                            attempted_single_format_fallback = True
                            actual_format = selection_format_id
                            continue

                    if isinstance(formats, list) and formats:
                        table = Table(title=f"Available formats for {url_str}", max_columns=10, preserve_order=True)
                        table.set_table("ytdlp.formatlist")
                        table.set_source_command("download-file", [url_str])
                        results_list: List[Dict[str, Any]] = []
                        for idx, fmt in enumerate(formats, 1):
                            format_id = str(fmt.get("format_id") or "")
                            selection_format_id = format_id
                            try:
                                if str(fmt.get("vcodec", "none")) != "none" and str(fmt.get("acodec", "none")) == "none" and format_id:
                                    selection_format_id = f"{format_id}+bestaudio"
                            except Exception:
                                selection_format_id = format_id
                            size_str = ""
                            size_bytes = fmt.get("filesize") or fmt.get("filesize_approx")
                            try:
                                if isinstance(size_bytes, (int, float)) and size_bytes > 0:
                                    size_str = f"{float(size_bytes) / (1024 * 1024):.1f}MB"
                            except Exception:
                                size_str = ""
                            format_dict = build_table_result_payload(
                                table="download-file",
                                title=f"Format {format_id}",
                                detail=" | ".join([part for part in [fmt.get("resolution", ""), fmt.get("ext", ""), size_str] if part]),
                                columns=[
                                    ("ID", format_id),
                                    ("Resolution", str(fmt.get("resolution") or "N/A")),
                                    ("Ext", str(fmt.get("ext") or "")),
                                    ("Size", size_str),
                                    ("Video", str(fmt.get("vcodec") or "none")),
                                    ("Audio", str(fmt.get("acodec") or "none")),
                                ],
                                selection_args=["-query", f"format:{selection_format_id}"],
                                url=url_str,
                                target=url_str,
                                media_kind="format",
                                full_metadata={
                                    "format_id": format_id,
                                    "url": url_str,
                                    "item_selector": selection_format_id,
                                },
                            )
                            results_list.append(format_dict)
                            table.add_result(format_dict)

                        pipeline_context.set_current_stage_table(table)
                        pipeline_context.set_last_result_table(table, results_list)
                        try:
                            suspend = getattr(pipeline_context, "suspend_live_progress", None)
                            cm: AbstractContextManager[Any] = nullcontext()
                            if callable(suspend):
                                maybe_cm = suspend()
                                if maybe_cm is not None:
                                    cm = maybe_cm  # type: ignore[assignment]
                            with cm:
                                get_stderr_console().print(table)
                        except Exception:
                            pass
                        log("Requested format is not available; select a working format with @N", file=sys.stderr)
                        return {"action": "handled", "exit_code": 1}

                log(f"Download failed for {url_str}: {exc}", file=sys.stderr)
                return {"action": "handled", "exit_code": 1}
            except Exception as exc:
                log(f"Error processing {url_str}: {exc}", file=sys.stderr)
                return {"action": "handled", "exit_code": 1}

        pipe_objects = _build_pipe_objects(
            result_obj,
            url=url_str,
            opts=opts,
            embed_chapters=True,
            write_sub=True,
        )
        if clip_ranges and len(pipe_objects) == len(clip_ranges):
            _apply_clip_decorations(pipe_objects, clip_ranges)
        return {"action": "emit_pipe_objects", "items": pipe_objects, "exit_code": 0}

    def download_url_as_pipe_objects(
        self,
        url: str,
        *,
        output_dir: Optional[Path] = None,
        mode_hint: Optional[str] = None,
        ytdl_format_hint: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        url_str = str(url or "").strip()
        if not url_str or not is_url_supported_by_ytdlp(url_str):
            return []

        out_dir = output_dir
        if out_dir is None:
            try:
                from SYS.config import resolve_output_dir
                out_dir = resolve_output_dir(self.config)
            except Exception:
                out_dir = None
        if out_dir is None:
            return []

        mode = str(mode_hint or "").strip().lower() if mode_hint else ""
        if mode not in {"audio", "video"}:
            mode = "video"
            try:
                fmts_probe = list_formats(
                    url_str,
                    no_playlist=False,
                    playlist_items=None,
                    cookiefile=_cookiefile_str(YtDlpTool(self.config)),
                )
                if isinstance(fmts_probe, list) and fmts_probe:
                    has_video = any(
                        str(f.get("vcodec", "none") or "none").strip().lower() != "none"
                        for f in fmts_probe
                        if isinstance(f, dict)
                    )
                    mode = "video" if has_video else "audio"
            except Exception:
                mode = "video"

        chosen_format = str(ytdl_format_hint).strip() if ytdl_format_hint else None
        if not chosen_format and mode == "audio":
            chosen_format = "bestaudio"

        quiet_download = False
        try:
            quiet_download = bool((self.config or {}).get("_quiet_background_output"))
        except Exception:
            quiet_download = False

        opts = DownloadOptions(
            url=url_str,
            mode=mode,
            output_dir=Path(out_dir),
            cookies_path=YtDlpTool(self.config).resolve_cookiefile(),
            ytdl_format=chosen_format,
            quiet=quiet_download,
            embed_chapters=True,
            write_sub=True,
        )
        try:
            result_obj = _download_with_timeout(opts, timeout_seconds=300, config=self.config)
        except Exception as exc:
            log(f"[ytdlp] Download failed for {url_str}: {exc}", file=sys.stderr)
            return []
        return _build_pipe_objects(
            result_obj,
            url=url_str,
            opts=opts,
            embed_chapters=True,
            write_sub=True,
        )
