# pyright: reportUnusedFunction=false
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, cast
from urllib.parse import urlparse

from SYS import pipeline as pipeline_context
from SYS.config import get_nested_config_value as _get_nested
from SYS.logger import debug, debug_panel, log
from SYS.models import (
    DebugLogger,
    DownloadError,
    DownloadMediaResult,
    DownloadOptions,
    ProgressBar,
)
from SYS.pipeline_progress import PipelineProgress
from SYS.utils import ensure_directory, safe_output_dir, sanitize_filename, sha256_file
from SYS.yt_metadata import extract_ytdlp_tags

_YTDLP_TRANSFER_STATE: Dict[str, Dict[str, Any]] = {}
_YTDLP_PLAYLIST_STEPS_BEGUN: bool = False
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _http_headers_for_url(url: str) -> Dict[str, str]:
    try:
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        netloc = str(parsed.netloc or "").lower()
    except Exception:
        host = ""
        netloc = ""
    if "youtube.com" in netloc or "youtu.be" in netloc:
        referer = "https://www.youtube.com/"
    else:
        referer = (host + "/") if host else url
    return {
        "User-Agent": _BROWSER_UA,
        "Referer": referer,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _is_bandcamp_url(url: str) -> bool:
    try:
        return "bandcamp.com" in (urlparse(url).netloc or "").lower()
    except Exception:
        return "bandcamp.com" in str(url or "").lower()


_YTDLP_IMPERSONATE_WARNED = False


def _ytdlp_cache_dir() -> Path:
    from SYS.utils import default_staging_dir, is_unsafe_output_dir

    path = default_staging_dir() / "ytdlp"
    if is_unsafe_output_dir(path.parent):
        path = default_staging_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _apply_ytdlp_impersonate(ydl_opts: Dict[str, Any]) -> None:
    global _YTDLP_IMPERSONATE_WARNED
    if ydl_opts.get("impersonate"):
        return
    target: Any = None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget  # type: ignore

        try:
            target = ImpersonateTarget.from_str("chrome")
        except Exception:
            target = ImpersonateTarget("chrome")
    except Exception:
        target = "chrome"
    ydl_opts["impersonate"] = target
    ydl_opts.setdefault("cachedir", str(_ytdlp_cache_dir()))
    ydl_opts.setdefault("no_color", True)
    try:
        import curl_cffi  # noqa: F401
    except Exception:
        ydl_opts.pop("impersonate", None)
        if not _YTDLP_IMPERSONATE_WARNED:
            _YTDLP_IMPERSONATE_WARNED = True
            debug(
                "[ytdlp] curl_cffi not installed; browser impersonate disabled. "
                "pip install 'Medeia-Macina[impersonate]' or curl_cffi"
            )


try:
    import yt_dlp  # type: ignore
    from yt_dlp.extractor import gen_extractors  # type: ignore
except Exception as exc:  # pragma: no cover - handled at runtime
    yt_dlp = None  # type: ignore
    gen_extractors = None  # type: ignore
    YTDLP_IMPORT_ERROR: Optional[Exception] = exc
else:
    YTDLP_IMPORT_ERROR = None

_EXTRACTOR_CACHE: List[Any] | None = None

# Patterns for domain extraction from yt-dlp regexes
# 1) Alternation group followed by \.tld  e.g. (?:youtube|youtu|youtube-nocookie)\.com
ALT_GROUP_TLD = re.compile(r'\((?:\?:)?([^\)]+)\)\\\.(?P<tld>[A-Za-z0-9.+-]+)')
# 2) Literal domain pieces like youtube\.com or youtu\.be (not preceded by a group)
LITERAL_DOMAIN = re.compile(r'(?<!\()(?<!\|)(?<!:)([A-Za-z0-9][A-Za-z0-9_-]{0,})\\\.([A-Za-z0-9.+-]+)')
# 3) Partial domain tokens that appear alone (e.g., zhihu) — treat as zhihu.com fallback
PARTIAL_TOKEN = re.compile(r'(?<![A-Za-z0-9_-])([A-Za-z0-9][A-Za-z0-9_-]{1,})(?=(?:\\?[/\)\$]|\\\.|$))')

_SUPPORTED_DOMAINS: set[str] | None = None


def normalize_patterns(valid_url) -> List[str]:
    if not valid_url:
        return []
    if isinstance(valid_url, str):
        return [valid_url]
    if isinstance(valid_url, (list, tuple)):
        return [p for p in valid_url if isinstance(p, str)]
    return []


def extract_from_pattern(pat: str) -> set[str]:
    domains = set()

    # 1) Alternation groups followed by .tld
    for alt_group, tld in ALT_GROUP_TLD.findall(pat):
        # alt_group like "youtube|youtu|youtube-nocookie"
        for alt in alt_group.split('|'):
            alt = alt.strip()
            # remove any non-domain tokens like (?:www\.)? if present inside alt (rare)
            alt = re.sub(r'\(\?:www\\\.\)\?', '', alt)
            if alt:
                domains.add(f"{alt}.{tld}".lower())

    # 2) Literal domain matches (youtube\.com)
    for name, tld in LITERAL_DOMAIN.findall(pat):
        domains.add(f"{name}.{tld}".lower())

    # 3) Partial tokens fallback (only if we didn't already capture domains)
    # This helps when regexes contain plain tokens like 'zhihu' or 'vimeo' without .com
    if not domains:
        for token in PARTIAL_TOKEN.findall(pat):
            # ignore common regex words that are not domains
            if len(token) <= 2:
                continue
            # avoid tokens that are clearly regex constructs
            if token.lower() in {"https", "http", "www", "com", "net", "org"}:
                continue
            domains.add(f"{token.lower()}.com")

    return domains


def extract_domains(valid_url) -> set[str]:
    patterns = normalize_patterns(valid_url)
    all_domains = set()
    for pat in patterns:
        all_domains |= extract_from_pattern(pat)
    # final cleanup: remove obvious junk like 'com.com' if present
    cleaned = set()
    for d in all_domains:
        # drop duplicates where left side equals tld (e.g., com.com)
        parts = d.split('.')
        if len(parts) >= 2 and parts[-2] == parts[-1]:
            continue
        cleaned.add(d)
    return cleaned


def _build_supported_domains() -> set[str]:
    global _SUPPORTED_DOMAINS
    if _SUPPORTED_DOMAINS is not None:
        return _SUPPORTED_DOMAINS

    _SUPPORTED_DOMAINS = set()
    if gen_extractors is None:
        return _SUPPORTED_DOMAINS

    try:
        for e in gen_extractors():
            name = getattr(e, "IE_NAME", "").lower()
            if name == "generic":
                continue
            regex = getattr(e, "_VALID_URL", None)
            domains = extract_domains(regex)
            _SUPPORTED_DOMAINS.update(domains)
    except Exception:
        from SYS.logger import logger
        logger.exception("Failed to build supported domains from yt-dlp extractors")
    return _SUPPORTED_DOMAINS


def _parse_csv_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            s = str(item).strip()
            if s:
                out.append(s)
        return out or None
    s = str(value).strip()
    if not s:
        return None
    # allow either JSON-ish list strings or simple comma-separated values
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    parts = [p.strip() for p in s.split(",")]
    parts = [p for p in parts if p]
    return parts or None


_BROWSER_COOKIES_AVAILABLE: Optional[bool] = None
_BROWSER_COOKIE_WARNING_EMITTED = False


def _browser_cookie_candidate_paths() -> List[Path]:
    try:
        home = Path.home()
    except Exception:
        home = Path.cwd()

    candidates: List[Path] = []
    if os.name == "nt":
        for env_value in (os.getenv("LOCALAPPDATA"), os.getenv("APPDATA")):
            if not env_value:
                continue
            base_path = Path(env_value)
            if not base_path:
                continue
            candidates.extend([
                base_path / "Google" / "Chrome" / "User Data" / "Default" / "Cookies",
                base_path / "Chromium" / "User Data" / "Default" / "Cookies",
                base_path / "BraveSoftware" / "Brave-Browser" / "User Data" / "Default" / "Cookies",
            ])
    else:
        candidates.extend([
            home / ".config" / "google-chrome" / "Default" / "Cookies",
            home / ".config" / "chromium" / "Default" / "Cookies",
            home / ".config" / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies",
        ])
        if sys.platform == "darwin":
            candidates.extend([
                home / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Cookies",
                home / "Library" / "Application Support" / "Chromium" / "Default" / "Cookies",
                home / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies",
            ])
    return candidates


def _has_browser_cookie_database() -> bool:
    global _BROWSER_COOKIES_AVAILABLE
    if _BROWSER_COOKIES_AVAILABLE is not None:
        return _BROWSER_COOKIES_AVAILABLE

    for path in _browser_cookie_candidate_paths():
        try:
            if path.is_file():
                _BROWSER_COOKIES_AVAILABLE = True
                return True
        except Exception:
            continue

    _BROWSER_COOKIES_AVAILABLE = False
    return False


def _browser_cookie_path_for(browser_name: str) -> Optional[Path]:
    """Return the cookie DB Path for a specific browser if present, else None.

    Supported browsers (case-insensitive): "chrome", "chromium", "brave".
    """
    name = str(browser_name or "").strip().lower()
    if not name:
        return None

    try:
        home = Path.home()
    except Exception:
        home = Path.cwd()

    # Windows
    if os.name == "nt":
        for env_value in (os.getenv("LOCALAPPDATA"), os.getenv("APPDATA")):
            if not env_value:
                continue
            base = Path(env_value)
            if name in ("chrome", "google-chrome"):
                p = base / "Google" / "Chrome" / "User Data" / "Default" / "Cookies"
                if p.is_file():
                    return p
            if name == "chromium":
                p = base / "Chromium" / "User Data" / "Default" / "Cookies"
                if p.is_file():
                    return p
            if name in ("brave", "brave-browser"):
                p = base / "BraveSoftware" / "Brave-Browser" / "User Data" / "Default" / "Cookies"
                if p.is_file():
                    return p

    # *nix and macOS
    if sys.platform == "darwin":
        if name in ("chrome", "google-chrome"):
            p = home / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Cookies"
            if p.is_file():
                return p
        if name == "chromium":
            p = home / "Library" / "Application Support" / "Chromium" / "Default" / "Cookies"
            if p.is_file():
                return p
        if name in ("brave", "brave-browser"):
            p = home / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies"
            if p.is_file():
                return p

    # Linux and other
    if name in ("chrome", "google-chrome"):
        p = home / ".config" / "google-chrome" / "Default" / "Cookies"
        if p.is_file():
            return p
    if name == "chromium":
        p = home / ".config" / "chromium" / "Default" / "Cookies"
        if p.is_file():
            return p
    if name in ("brave", "brave-browser"):
        p = home / ".config" / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies"
        if p.is_file():
            return p

    return None


def _add_browser_cookies_if_available(options: Dict[str, Any], preferred_browser: Optional[str] = None) -> None:
    global _BROWSER_COOKIE_WARNING_EMITTED

    # If a preferred browser is specified, try to use it if available
    if preferred_browser:
        try:
            if _browser_cookie_path_for(preferred_browser) is not None:
                options["cookiesfrombrowser"] = [preferred_browser]
                return
            else:
                if not _BROWSER_COOKIE_WARNING_EMITTED:
                    log(f"Requested browser cookie DB '{preferred_browser}' not found; falling back to autodetect.")
                    _BROWSER_COOKIE_WARNING_EMITTED = True
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to check browser cookie path for preferred browser '%s'", preferred_browser)

    # Auto-detect in common order (chrome/chromium/brave)
    for candidate in ("chrome", "chromium", "brave"):
        try:
            if _browser_cookie_path_for(candidate) is not None:
                options["cookiesfrombrowser"] = [candidate]
                return
        except Exception:
            from SYS.logger import logger
            logger.exception("Error while checking cookie path for candidate browser '%s'", candidate)
            continue

    if not _BROWSER_COOKIE_WARNING_EMITTED:
        log(
            "Browser cookie extraction skipped because no Chrome-compatible cookie database was found. "
            "Place a Netscape cookies.txt in plugins/ytdlp/ if authentication is required."
        )
        _BROWSER_COOKIE_WARNING_EMITTED = True


def ensure_yt_dlp_ready() -> None:
    """Verify yt-dlp is importable, raising DownloadError if missing."""

    if yt_dlp is not None:
        return

    detail = str(YTDLP_IMPORT_ERROR or "yt-dlp is not installed")
    raise DownloadError(f"yt-dlp module not available: {detail}")


def _get_extractors() -> List[Any]:
    global _EXTRACTOR_CACHE

    if _EXTRACTOR_CACHE is not None:
        return _EXTRACTOR_CACHE

    ensure_yt_dlp_ready()

    if gen_extractors is None:
        _EXTRACTOR_CACHE = []
        return _EXTRACTOR_CACHE

    try:
        _EXTRACTOR_CACHE = [ie for ie in gen_extractors()]
    except Exception:
        _EXTRACTOR_CACHE = []

    return _EXTRACTOR_CACHE


def _yt_dlp_cli_prefix() -> List[str]:
    """Return a subprocess argv prefix that uses the same yt-dlp implementation as this process."""
    python_exe = str(sys.executable or "").strip()
    if python_exe:
        return [python_exe, "-m", "yt_dlp"]
    return ["yt-dlp"]


def is_url_supported_by_ytdlp(url: str) -> bool:
    """Return True if yt-dlp has a non-generic extractor for the URL."""

    if not url or not isinstance(url, str):
        return False

    if YTDLP_IMPORT_ERROR is not None:
        return False

    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
    except Exception:
        return False

    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if not domain:
            return False
        supported = _build_supported_domains()
        for base in supported:
            if domain == base or domain.endswith("." + base):
                return True
    except Exception:
        return False

    return False


_FORMATS_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}


def _audio_variant_base_selector(fmt: Any) -> Optional[str]:
    if not isinstance(fmt, dict):
        return None

    format_id = str(fmt.get("format_id") or "").strip()
    if not format_id:
        return None

    vcodec = str(fmt.get("vcodec", "none"))
    acodec = str(fmt.get("acodec", "none"))
    if vcodec != "none" or acodec == "none":
        return None

    match = re.fullmatch(r"(?P<base>\d+)-[A-Za-z0-9]+", format_id)
    if not match:
        return None
    return match.group("base")

def list_formats(
    url: str,
    *,
    no_playlist: bool = False,
    playlist_items: Optional[str] = None,
    cookiefile: Optional[str] = None,
    timeout_seconds: int = 20,
) -> Optional[List[Dict[str, Any]]]:
    """Get available formats for a URL.

    Returns a list of format dicts or None if unsupported or probing fails.
    """

    if not is_url_supported_by_ytdlp(url):
        return None

    # Cache format probes to avoid redundant network hits
    cache_key = hashlib.md5(f"{url}|{no_playlist}|{playlist_items}|{cookiefile}".encode()).hexdigest()
    now = time.monotonic()
    if cache_key in _FORMATS_CACHE:
        ts, result = _FORMATS_CACHE[cache_key]
        if now - ts < 300: # 5 minute cache for formats
            return result

    result_container: List[Optional[Any]] = [None, None]  # [result, error]

    def _do_list() -> None:
        try:
            ensure_yt_dlp_ready()
            assert yt_dlp is not None

            headers = _http_headers_for_url(url)
            ydl_opts: Dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noprogress": True,
                "socket_timeout": min(10, max(1, int(timeout_seconds))),
                "retries": 2,
                "user_agent": headers["User-Agent"],
                "referer": headers["Referer"],
                "http_headers": headers,
            }
            _apply_ytdlp_impersonate(ydl_opts)

            if cookiefile:
                ydl_opts["cookiefile"] = str(cookiefile)
            else:
                # Best effort attempt to use browser cookies if no file is explicitly passed
                _add_browser_cookies_if_available(ydl_opts)

            if no_playlist:
                ydl_opts["noplaylist"] = True
            if playlist_items:
                ydl_opts["playlist_items"] = str(playlist_items)

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                info = ydl.extract_info(url, download=False)

            if not isinstance(info, dict):
                result_container[0] = None
                return

            formats = info.get("formats")
            if not isinstance(formats, list):
                result_container[0] = None
                return

            selector_cache: Dict[str, bool] = {}

            def _selector_has_matches(selector: str) -> bool:
                cached = selector_cache.get(selector)
                if cached is not None:
                    return cached
                try:
                    matches = ydl.build_format_selector(selector)(info)
                    cached = any(True for _ in matches)
                except Exception:
                    cached = False
                selector_cache[selector] = cached
                return cached

            out: List[Dict[str, Any]] = []
            for fmt in formats:
                if isinstance(fmt, dict):
                    base_selector = _audio_variant_base_selector(fmt)
                    if base_selector and not _selector_has_matches(base_selector):
                        fmt["_medios_selector_valid"] = False
                    out.append(fmt)
            result_container[0] = out
        except Exception as exc:
            debug(f"yt-dlp format probe failed for {url}: {exc}")
            result_container[1] = exc

    # Use daemon=True so a hung thread doesn't block process exit
    thread = threading.Thread(target=_do_list, daemon=True)
    thread.start()
    thread.join(timeout=max(1, int(timeout_seconds)))

    if thread.is_alive():
        debug(f"yt-dlp format probe timed out for {url} (>={timeout_seconds}s)")
        return None

    if result_container[1] is not None:
        return None

    if result_container[0] is not None:
        _FORMATS_CACHE[cache_key] = (now, cast(List[Dict[str, Any]], result_container[0]))

    return cast(Optional[List[Dict[str, Any]]], result_container[0])


_PROBE_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}

def probe_url(
    url: str,
    no_playlist: bool = False,
    timeout_seconds: int = 15,
    *,
    cookiefile: Optional[str] = None,
    playlist_items: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Probe URL metadata without downloading.

    Returns None if unsupported, errors, or times out.
    """

    if not is_url_supported_by_ytdlp(url):
        return None

    playlist_items_key = str(playlist_items or "").strip() or None

    # Simple in-memory cache to avoid duplicate probes for the same URL/options in a short window.
    cache_key = hashlib.md5(
        f"{url}|{no_playlist}|{cookiefile}|{playlist_items_key or ''}".encode()
    ).hexdigest()
    now = time.monotonic()
    if cache_key in _PROBE_CACHE:
        ts, result = _PROBE_CACHE[cache_key]
        if now - ts < 60: # 60 second cache
            return result

    result_container: List[Optional[Any]] = [None, None]  # [result, error]

    def _do_probe() -> None:
        try:
            ensure_yt_dlp_ready()

            assert yt_dlp is not None
            headers = _http_headers_for_url(url)
            ydl_opts: Dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 10,
                "retries": 2,
                "skip_download": True,
                "extract_flat": "in_playlist",
                "noprogress": True,
                "user_agent": headers["User-Agent"],
                "referer": headers["Referer"],
                "http_headers": headers,
            }
            _apply_ytdlp_impersonate(ydl_opts)

            if cookiefile:
                ydl_opts["cookiefile"] = str(cookiefile)
            else:
                # Best effort fallback
                _add_browser_cookies_if_available(ydl_opts)

            if no_playlist:
                ydl_opts["noplaylist"] = True
            elif playlist_items_key:
                ydl_opts["playlist_items"] = playlist_items_key

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                info = ydl.extract_info(url, download=False)

            if not isinstance(info, dict):
                result_container[0] = None
                return

            webpage_url = info.get("webpage_url") or info.get("original_url") or info.get("url")

            result_container[0] = {
                "extractor": info.get("extractor", ""),
                "title": info.get("title", ""),
                "entries": info.get("entries", []),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
                "description": info.get("description"),
                "requested_url": url,
                "webpage_url": webpage_url,
                "url": webpage_url or url,
            }
            debug_panel(
                "yt-dlp probe",
                [
                    ("url", url),
                    ("extractor", info.get("extractor", "")),
                    ("title", info.get("title", "")),
                    ("playlist", bool(info.get("entries"))),
                ],
                border_style="green",
            )
        except Exception as exc:
            debug(f"Probe error for {url}: {exc}")
            result_container[1] = exc

    # Use daemon=True so a hung probe doesn't block the process
    thread = threading.Thread(target=_do_probe, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        debug(f"Probe timeout for {url} (>={timeout_seconds}s), proceeding without probe")
        return None

    if result_container[1] is not None:
        return None

    if result_container[0] is not None:
        _PROBE_CACHE[cache_key] = (now, cast(Dict[str, Any], result_container[0]))

    return cast(Optional[Dict[str, Any]], result_container[0])


def is_browseable_format(fmt: Any) -> bool:
    """Check if a format is user-browseable (not storyboard, metadata, etc).
    
    Used by the ytdlp format selector to filter out non-downloadable formats.
    Returns False for:
    - MHTML, JSON sidecar metadata
    - Storyboard/thumbnail formats
    - Audio-only or video-only when both available
    
    Args:
        fmt: Format dict from yt-dlp with keys like format_id, ext, vcodec, acodec, format_note
        
    Returns:
        bool: True if format is suitable for browsing/selection
    """
    if not isinstance(fmt, dict):
        return False

    if fmt.get("_medios_selector_valid") is False:
        return False
    
    format_id = str(fmt.get("format_id") or "").strip()
    if not format_id:
        return False
    
    # Filter out metadata/sidecar formats
    ext = str(fmt.get("ext") or "").strip().lower()
    if ext in {"mhtml", "json"}:
        return False
    
    # Filter out storyboard/thumbnail formats
    note = str(fmt.get("format_note") or "").lower()
    if "storyboard" in note:
        return False
    
    if format_id.lower().startswith("sb"):
        return False

    protocol = str(fmt.get("protocol") or "").strip().lower()
    size_bytes = fmt.get("filesize") or fmt.get("filesize_approx")
    if (
        protocol in {"m3u8", "m3u8_native"}
        and re.fullmatch(r"\d+-\d+", format_id)
        and not size_bytes
    ):
        vcodec = str(fmt.get("vcodec", "none"))
        acodec = str(fmt.get("acodec", "none"))
        if vcodec != "none" and acodec != "none":
            return False
    
    # Filter out formats with no audio and no video
    vcodec = str(fmt.get("vcodec", "none"))
    acodec = str(fmt.get("acodec", "none"))
    if vcodec == "none" and acodec == "none":
        return False

    # Audio-only rows are redundant in the video format picker (every video-only
    # row already gets "+ba" auto-muxed) and their collapsed DRC-variant selector
    # ids (e.g. "250-20" -> "250") are frequently not resolvable by yt-dlp on
    # their own, which makes picking them fail with "format not available".
    if vcodec == "none" and acodec != "none":
        return False

    return True


def get_selection_format_id(
    fmt: Dict[str, Any],
    *,
    video_audio_suffix: str = "ba",
) -> str:
    format_id = str(fmt.get("format_id") or "").strip()
    if not format_id:
        return ""

    vcodec = str(fmt.get("vcodec", "none"))
    acodec = str(fmt.get("acodec", "none"))
    selector_id = format_id

    match = re.fullmatch(r"(?P<base>\d+)-[A-Za-z0-9]+", format_id)
    if match and vcodec == "none" and acodec != "none":
        selector_id = match.group("base")

    if selector_id and vcodec != "none" and acodec == "none" and video_audio_suffix:
        selector_id = f"{selector_id}+{video_audio_suffix}"

    return selector_id


def get_display_format_id(fmt: Dict[str, Any]) -> str:
    format_id = str(fmt.get("format_id") or "").strip()
    if not format_id:
        return ""
    selector_id = get_selection_format_id(fmt, video_audio_suffix="")
    return selector_id or format_id


def _picker_format_score(fmt: Dict[str, Any]) -> tuple[int, int, float]:
    note = str(fmt.get("format_note") or fmt.get("format") or "").strip().lower()
    format_id = str(fmt.get("format_id") or "").strip().lower()
    prefers_original = 1 if ("original" in note or "default" in note) else 0
    avoids_drc = 0 if ("-drc" in format_id or "drc" in note) else 1
    magnitude = 0.0
    for key in ("filesize", "filesize_approx", "abr", "tbr"):
        value = fmt.get(key)
        if isinstance(value, (int, float)):
            magnitude = float(value)
            break
        if isinstance(value, str):
            try:
                magnitude = float(value.strip())
                break
            except Exception:
                pass
    return (prefers_original, avoids_drc, magnitude)


def collapse_picker_formats(
    formats: Sequence[Dict[str, Any]],
    *,
    video_audio_suffix: str = "ba",
) -> List[Dict[str, Any]]:
    collapsed: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for fmt in formats:
        if not isinstance(fmt, dict) or not is_browseable_format(fmt):
            continue
        selector_id = get_selection_format_id(fmt, video_audio_suffix=video_audio_suffix)
        if not selector_id:
            continue
        current = collapsed.get(selector_id)
        if current is None:
            collapsed[selector_id] = fmt
            order.append(selector_id)
            continue
        if _picker_format_score(fmt) > _picker_format_score(current):
            collapsed[selector_id] = fmt
    return [collapsed[key] for key in order if key in collapsed]


def format_for_table_selection(
    fmt: Dict[str, Any],
    url: str,
    index: int,
    *,
    selection_format_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Format a yt-dlp format dict into a table result row for selection.
    
    This helper formats a single format from list_formats() into the shape
    expected by the ResultTable system, ready for user selection and routing
    to download-file with -query "format:<id>".
    
    Args:
        fmt: Format dict from yt-dlp
        url: The URL this format came from
        index: Row number for display (1-indexed)
        selection_format_id: Override format_id for selection (e.g., with +ba suffix)
        
    Returns:
        dict: Format result row with _selection_args for table system
        
    Example:
        fmts = list_formats("https://youtube.com/watch?v=abc")
        browseable = [f for f in fmts if is_browseable_format(f)]
        results = [format_for_table_selection(f, url, i+1) for i, f in enumerate(browseable)]
    """
    format_id = fmt.get("format_id", "")
    display_format_id = get_display_format_id(fmt)
    resolution = fmt.get("resolution", "")
    ext = fmt.get("ext", "")
    vcodec = fmt.get("vcodec", "none")
    acodec = fmt.get("acodec", "none")
    filesize = fmt.get("filesize")
    filesize_approx = fmt.get("filesize_approx")
    
    # If not provided, compute selection format ID (add +ba for video-only)
    if selection_format_id is None:
        selection_format_id = get_selection_format_id(fmt, video_audio_suffix="ba")
        try:
            if not selection_format_id and format_id:
                selection_format_id = format_id
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to compute selection_format_id for format: %s", fmt)
    
    # Format file size
    size_str = ""
    size_prefix = ""
    size_bytes = filesize or filesize_approx
    try:
        if isinstance(size_bytes, (int, float)) and size_bytes > 0:
            size_mb = float(size_bytes) / (1024 * 1024)
            size_str = f"{size_prefix}{size_mb:.1f}MB"
    except Exception:
        from SYS.logger import logger
        logger.exception("Failed to compute size string for format: %s", fmt)
    
    # Build description
    desc_parts: List[str] = []
    if resolution and resolution != "audio only":
        desc_parts.append(resolution)
    if ext:
        desc_parts.append(str(ext).upper())
    if vcodec != "none":
        desc_parts.append(f"v:{vcodec}")
    if acodec != "none":
        desc_parts.append(f"a:{acodec}")
    if size_str:
        desc_parts.append(size_str)
    format_desc = " | ".join(desc_parts)
    
    # Build table row
    return {
        "table": "download-file",
        "title": f"Format {display_format_id or format_id}",
        "url": url,
        "target": url,
        "detail": format_desc,
        "annotations": [ext, resolution] if resolution else [ext],
        "media_kind": "format",
        "columns": [
            ("ID", display_format_id or format_id),
            ("Resolution", resolution or "N/A"),
            ("Ext", ext),
            ("Size", size_str or ""),
            ("Video", vcodec),
            ("Audio", acodec),
        ],
        "full_metadata": {
            "format_id": format_id,
            "url": url,
            "item_selector": selection_format_id,
            "_selection_args": ["-query", f"format:{selection_format_id}"],
        },
        "_selection_args": ["-query", f"format:{selection_format_id}"],
    }


@dataclass(slots=True)
class YtDlpDefaults:
    """User-tunable defaults for yt-dlp behavior.

    Recommended config.conf keys (top-level dotted keys):
      - format="best|1080|720|640|audio"
      - ytdlp.format_sort="res:2160,res:1440,res:1080,res:720,res"

    Cookies:
      - Drop Netscape cookie files into ``plugins/ytdlp/`` (prefer ``cookies.txt``)
      - cookies_from_browser="auto|none|chrome|brave|chromium" when no file is present
    """

    format: str = "best"
    video_format: str = "bestvideo+bestaudio/best"
    # Prefer pure audio, then fall back to best overall (some clients omit audio-only).
    audio_format: str = "bestaudio/best"
    format_sort: Optional[List[str]] = None
    cookies_from_browser: Optional[str] = None


class YtDlpTool:
    """Centralizes yt-dlp defaults and translation helpers.

    This is intentionally small and dependency-light so cmdlets can use it without
    forcing a full refactor.
    """

    def __init__(
        self,
        config: Optional[Dict[str,
                              Any]] = None,
        *,
        script_dir: Optional[Path] = None
    ) -> None:
        self._config: Dict[str,
                           Any] = dict(config or {})
        # App root for cookie resolution under plugins/ytdlp/.
        self._script_dir = script_dir or Path(__file__).resolve().parent.parent.parent
        self.defaults = self._load_defaults()
        self._cookiefile: Optional[Path] = self._init_cookiefile()

    def _init_cookiefile(self) -> Optional[Path]:
        """Resolve cookies from plugins/ytdlp/ (no config path override)."""
        try:
            from SYS.config import resolve_cookies_path

            resolved = resolve_cookies_path(self._config, script_dir=self._script_dir)
            if resolved is not None and resolved.is_file():
                return resolved
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to initialize cookiefile using resolve_cookies_path")
        return None

    def resolve_height_selector(self, format_str: Optional[str]) -> Optional[str]:
        """Resolve numeric heights (720, 1080p) to yt-dlp height selectors.
        
        Examples:
          "720" -> "bestvideo[height<=720]+bestaudio/best[height<=720]"
          "1080p" -> "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
        """
        if not format_str or not isinstance(format_str, str):
            return None
        
        raw = format_str.strip()
        s = raw.lower()
        if not s:
            return None
            
        # Strip trailing 'p' if present (e.g. 720p -> 720)
        explicit_height = s.endswith('p')
        if explicit_height:
            s = s[:-1]
        
        # Heuristic: 240/360/480/720/1080/1440/2160 are common height inputs
        # But small IDs like 18, 22, 137 are format IDs. 
        # YouTube Format IDs are usually 2-3 digits. 
        # Heights are also 3-4 digits.
        # "240" is ambiguous (Format 240 vs Height 240).
        # We assume common video heights are intended as heights.
        if s.isdigit():
            val = int(s)
            
            # Common video heights that overlap with format IDs:
            # - None currently overlap with common legacy itag IDs (17,18,22,34-38,43-46)
            #   or dash video IDs (133-137, 160, 242-248, 264, 271, 278, 298-315...).
            # - 240, 360, 480, 720, 1080, 1440, 2160
            #
            # Format 240 is likely not a thing (242, 243, ... exist).
            # Format 480 ... none in common lists.
            # Format 720 ... none.
            # So if it looks like a standard resolution, treat as height constraint.
            common_heights = {144, 240, 360, 480, 540, 720, 1080, 1440, 2160, 2880, 4320}
            if explicit_height or val in common_heights:
                return f"bestvideo[height<={val}]+bestaudio/best[height<={val}]"

        return None

    def resolve_clip_safe_format(self, format_str: Optional[str]) -> Optional[str]:
        """Normalize clip downloads to a single-stream selector yt-dlp can section-download reliably."""
        if format_str is None or not isinstance(format_str, str):
            return format_str

        raw = format_str.strip()
        if not raw:
            return raw

        low = raw.lower()

        if low in {"bestvideo+bestaudio/best", "bestvideo+bestaudio", "bv+ba/b", "bv+ba"}:
            return "best/b"

        if low in {"best", "best/b", "best/best", "b"}:
            return "best/b"

        # Height preferences like 720/1080p should prefer a progressive single-file stream for clips.
        height_match = re.fullmatch(r"(?i)\s*(\d{3,4})p?\s*", raw)
        if height_match:
            try:
                height = int(height_match.group(1))
            except Exception:
                height = 0
            if height > 0:
                return f"best[height<={height}]/best"

        merged_height_match = re.fullmatch(
            r"bestvideo(?:\[height<=?(\d+)\])?\+bestaudio(?:/best(?:\[height<=?(\d+)\])?)?",
            low,
        )
        if merged_height_match:
            height = merged_height_match.group(1) or merged_height_match.group(2)
            if height:
                return f"best[height<={height}]/best"
            return "best/b"

        return raw

    def _load_defaults(self) -> YtDlpDefaults:
        cfg = self._config

        # NOTE: `YtDlpDefaults` is a slots dataclass. Referencing defaults via
        # `YtDlpDefaults.video_format` yields a `member_descriptor`, not the
        # default string value. Use an instance for fallback defaults.
        _fallback_defaults = YtDlpDefaults()

        ytdlp_block = _get_nested(cfg, "plugin", "ytdlp")
        if not isinstance(ytdlp_block, dict):
            ytdlp_block = {}

        video_format = (
            ytdlp_block.get("video_format") or ytdlp_block.get("video")
            or ytdlp_block.get("format_video")
        )
        audio_format = (
            ytdlp_block.get("audio_format") or ytdlp_block.get("audio")
            or ytdlp_block.get("format_audio")
        )

        nested_video = _get_nested(cfg, "plugin", "ytdlp", "format", "video")
        nested_audio = _get_nested(cfg, "plugin", "ytdlp", "format", "audio")

        fmt_sort_val = (
            ytdlp_block.get("format_sort")
            or ytdlp_block.get("formatSort")
            or _get_nested(cfg,
                           "plugin",
                           "ytdlp",
                           "format",
                           "sort")
        )
        fmt_sort = _parse_csv_list(fmt_sort_val)

        cookies_pref = (
            ytdlp_block.get("cookies_from_browser")
            or ytdlp_block.get("cookiesfrombrowser")
            or _get_nested(cfg, "plugin", "ytdlp", "cookies_from_browser")
        )

        format_pref = (
            ytdlp_block.get("format")
            or ytdlp_block.get("video_format")
            or _get_nested(cfg, "plugin", "ytdlp", "format")
        )

        defaults = YtDlpDefaults(
            format=str(format_pref).strip() if format_pref else "best",
            video_format=str(
                nested_video or video_format or _fallback_defaults.video_format
            ),
            audio_format=str(
                nested_audio or audio_format or _fallback_defaults.audio_format
            ),
            format_sort=fmt_sort,
            cookies_from_browser=(str(cookies_pref).strip() if cookies_pref else None),
        )

        return defaults



    def resolve_cookiefile(self) -> Optional[Path]:
        return self._cookiefile

    def default_format(self, mode: str) -> str:
        """Determine the final yt-dlp format string.

        Priority:
        - If caller explicitly requested audio mode (mode == 'audio'), return audio format.
        - If configured default format is 'audio', return audio format.
        - If configured default is 'best' or blank, return video_format.
        - Otherwise return the configured format value (e.g., '720').
        """
        m = str(mode or "").lower().strip()
        if m == "audio":
            return self.defaults.audio_format

        cfg = (str(self.defaults.format or "")).strip()
        lc = cfg.lower()
        if lc == "audio":
            return self.defaults.audio_format
        if not cfg or lc == "best":
            return self.defaults.video_format
        return cfg

    def build_ytdlp_options(self, opts: DownloadOptions) -> Dict[str, Any]:
        """Translate DownloadOptions into yt-dlp API options."""
        out_dir = safe_output_dir(opts.output_dir)
        ensure_directory(out_dir)
        outtmpl = str((out_dir / "%(title)s.%(ext)s").resolve())
        base_options: Dict[str,
                           Any] = {
                               "outtmpl": outtmpl,
                               "quiet": True,
                               "no_warnings": True,
                               "noprogress": True,
                               "socket_timeout": 30,
                               "retries": 10,
                               "fragment_retries": 10,
                               "http_chunk_size": 10_485_760,
                               "restrictfilenames": True,
                           }
        headers = _http_headers_for_url(opts.url)
        base_options["user_agent"] = headers["User-Agent"]
        base_options["referer"] = headers["Referer"]
        base_options.setdefault("http_headers", headers)
        _apply_ytdlp_impersonate(base_options)

        try:
            repo_root = Path(__file__).resolve().parents[2]
            bundled_ffmpeg_dir = repo_root / "MPV" / "ffmpeg" / "bin"
            if bundled_ffmpeg_dir.exists():
                base_options.setdefault("ffmpeg_location", str(bundled_ffmpeg_dir))
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to inspect bundled ffmpeg directory")

        try:
            if os.name == "nt":
                base_options.setdefault("file_access_retries", 40)
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to set Windows-specific yt-dlp options")

        if opts.cookies_path and opts.cookies_path.is_file():
            base_options["cookiefile"] = str(opts.cookies_path)
        else:
            cookiefile = self.resolve_cookiefile()
            if cookiefile is not None and cookiefile.is_file():
                base_options["cookiefile"] = str(cookiefile)
            else:
                # Respect configured browser cookie preference if provided; otherwise fall back to auto-detect.
                pref = (self.defaults.cookies_from_browser or "").lower().strip()
                if pref:
                    if pref in {"none", "off", "false"}:
                        # Explicitly disabled
                        pass
                    elif pref in {"auto", "detect"}:
                        _add_browser_cookies_if_available(base_options)
                    else:
                        # Try the preferred browser first; fall back to auto-detect if not present
                        _add_browser_cookies_if_available(base_options, preferred_browser=pref)
                else:
                    # Add browser cookies support "just in case" if no file found (best effort)
                    _add_browser_cookies_if_available(base_options)

        # YouTube hardening: prefer browser cookies when available.
        try:
            netloc = urlparse(opts.url).netloc.lower()
        except Exception:
            netloc = ""
        is_youtube = ("youtube.com" in netloc) or ("youtu.be" in netloc)
        if is_youtube:
            # Prefer browser cookies over a potentially stale cookiefile when available
            pref = (self.defaults.cookies_from_browser or "").lower().strip()
            if pref not in {"none", "off", "false"} and _has_browser_cookie_database():
                _add_browser_cookies_if_available(
                    base_options,
                    preferred_browser=None if pref in {"", "auto", "detect"} else pref,
                )
                if base_options.get("cookiesfrombrowser"):
                    base_options.pop("cookiefile", None)
                    debug("[ytdlp] Using browser cookies for YouTube; ignoring cookiefile")

        # Special handling for format keywords explicitly passed in via options
        if opts.ytdl_format == "audio":
            try:
                import dataclasses as _dc

                opts = _dc.replace(opts, mode="audio", ytdl_format=None)
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to set opts mode to audio via dataclasses.replace")
        elif opts.ytdl_format == "video":
            try:
                import dataclasses as _dc

                opts = _dc.replace(opts, mode="video", ytdl_format=None)
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to set opts mode to video via dataclasses.replace")

        if opts.no_playlist:
            base_options["noplaylist"] = True

        # If no explicit format was provided, honor the configured default format
        ytdl_format = opts.ytdl_format
        if not ytdl_format:
            configured_format = (str(self.defaults.format or "")).strip()
            if configured_format:
                if configured_format.lower() == "audio":
                    # Default to audio-only downloads
                    try:
                        import dataclasses as _dc

                        opts = _dc.replace(opts, mode="audio")
                    except Exception:
                        from SYS.logger import logger
                        logger.exception("Failed to set opts mode to audio via dataclasses.replace (configured default)")
                    ytdl_format = None
                else:
                    # Leave ytdl_format None so that default_format(opts.mode)
                    # returns the configured format literally (e.g., '720') and
                    # we don't auto-convert it to an internal selector.
                    pass

        if ytdl_format and opts.mode != "audio":
            # Don't resolve 3-digit format IDs (like 251, 249, 140 from YouTube format tables) as heights
            # YouTube format IDs are typically 2-3 digits representing specific codec/quality combinations
            # Height selectors come from user input like "720" or "1080p"
            is_likely_format_id = (
                isinstance(ytdl_format, str) and
                len(ytdl_format.strip()) == 3 and 
                ytdl_format.strip().isdigit()
            )
            if not is_likely_format_id:
                resolved = self.resolve_height_selector(ytdl_format)
                if resolved:
                    ytdl_format = resolved

        default_fmt = self.default_format(opts.mode)
        fmt = ytdl_format or default_fmt
        if opts.clip_sections and opts.mode != "audio":
            clip_basis = fmt
            if isinstance(ytdl_format, str):
                incoming = ytdl_format.strip().lower()
                if incoming in {
                    "bestvideo+bestaudio/best",
                    "bestvideo+bestaudio",
                    "best",
                    "best/b",
                    "best/best",
                    "b",
                } and default_fmt:
                    clip_basis = default_fmt
            clip_safe_fmt = self.resolve_clip_safe_format(clip_basis)
            if clip_safe_fmt:
                if clip_safe_fmt != fmt:
                    debug(f"[ytdlp] clip format normalized: {fmt} -> {clip_safe_fmt}")
                fmt = clip_safe_fmt
        base_options["format"] = fmt

        if opts.mode == "audio":
            base_options["postprocessors"] = [{
                "key": "FFmpegExtractAudio"
            }]

        if opts.mode != "audio":
            format_sort = self.defaults.format_sort or [
                "res:4320",
                "res:2880",
                "res:2160",
                "res:1440",
                "res:1080",
                "res:720",
                "res",
            ]
            base_options["format_sort"] = format_sort

        if getattr(opts, "embed_chapters", False):
            pps = base_options.get("postprocessors")
            if not isinstance(pps, list):
                pps = []
            already_has_metadata = any(
                isinstance(pp,
                           dict) and str(pp.get("key") or "") == "FFmpegMetadata"
                for pp in pps
            )
            if not already_has_metadata:
                pps.append(
                    {
                        "key": "FFmpegMetadata",
                        "add_metadata": True,
                        "add_chapters": True,
                        "add_infojson": "if_exists",
                    }
                )
            base_options["postprocessors"] = pps

            if opts.mode != "audio":
                base_options.setdefault("merge_output_format", "mkv")

        if getattr(opts, "write_sub", False):
            base_options["writesubtitles"] = True
            base_options["writeautomaticsub"] = True
            base_options["subtitlesformat"] = "vtt"

        if opts.clip_sections:
            sections: List[str] = []

            def _secs_to_hms(seconds: float) -> str:
                total = max(0, int(seconds))
                minutes, secs = divmod(total, 60)
                hours, minutes = divmod(minutes, 60)
                return f"{hours:02d}:{minutes:02d}:{secs:02d}"

            for section_range in str(opts.clip_sections).split(","):
                section_range = section_range.strip()
                if not section_range:
                    continue
                try:
                    start_s_raw, end_s_raw = section_range.split("-", 1)
                    start_s = float(start_s_raw.strip())
                    end_s = float(end_s_raw.strip())
                    if start_s >= end_s:
                        continue
                    sections.append(f"*{_secs_to_hms(start_s)}-{_secs_to_hms(end_s)}")
                except (ValueError, AttributeError):
                    continue

            if sections:
                base_options["download_sections"] = sections
                # Clipped outputs should begin with a keyframe; otherwise players (notably mpv)
                # can show audio before video or a black screen until the next keyframe.
                # yt-dlp implements this by forcing keyframes at cut points.
                base_options["force_keyframes_at_cuts"] = True
                debug(f"Download sections configured: {', '.join(sections)}")

        if opts.playlist_items:
            base_options["playlist_items"] = opts.playlist_items

        # YouTube often needs alternate player clients when web/cookies flake.
        try:
            netloc = urlparse(opts.url).netloc.lower()
        except Exception:
            netloc = ""
        if ("youtube.com" in netloc) or ("youtu.be" in netloc):
            extractor_args = base_options.get("extractor_args")
            if not isinstance(extractor_args, dict):
                extractor_args = {}
            youtube_args = extractor_args.get("youtube")
            if not isinstance(youtube_args, dict):
                youtube_args = {}
            if not youtube_args.get("player_client"):
                # android/ios expose stable audio formats; web is last resort.
                youtube_args["player_client"] = ["android", "ios", "web"]
            extractor_args["youtube"] = youtube_args
            base_options["extractor_args"] = extractor_args

        return base_options

    def build_yt_dlp_cli_args(
        self,
        *,
        url: str,
        output_dir: Optional[Path] = None,
        ytdl_format: Optional[str] = None,
        playlist_items: Optional[str] = None,
        no_playlist: bool = False,
        quiet: bool = True,
        extra_args: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Build a yt-dlp command line (argv list).

        This is primarily for debug output or subprocess execution.
        """
        argv: List[str] = _yt_dlp_cli_prefix()
        if quiet:
            argv.extend(["--quiet", "--no-warnings"])
        argv.append("--no-progress")

        cookiefile = self.resolve_cookiefile()
        if cookiefile is not None:
            argv.extend(["--cookies", str(cookiefile)])

        if no_playlist:
            argv.append("--no-playlist")
        if playlist_items:
            argv.extend(["--playlist-items", str(playlist_items)])

        fmt = (ytdl_format or "").strip()
        if fmt:
            # Use long form to avoid confusion with app-level flags.
            argv.extend(["--format", fmt])

        if self.defaults.format_sort:
            for sort_key in self.defaults.format_sort:
                argv.extend(["-S", sort_key])

        if output_dir is not None:
            outtmpl = str((output_dir / "%(title)s.%(ext)s").resolve())
            argv.extend(["-o", outtmpl])

        if extra_args:
            argv.extend([str(a) for a in extra_args if str(a).strip()])

        argv.append(str(url))
        return argv

    def debug_print_cli(self, argv: Sequence[str]) -> None:
        try:
            debug("yt-dlp argv: " + " ".join(str(a) for a in argv))
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to debug-print yt-dlp CLI arguments")


def config_schema() -> List[Dict[str, Any]]:
    """Return a schema describing editable YT-DLP tool defaults for the config UI."""
    format_choices = [
        "best",
        "1080",
        "720",
        "640",
        "audio",
    ]

    # Offer browser choices depending on what's present on the host system
    browser_choices = ["auto", "none"]
    for b in ("chrome", "chromium", "brave"):
        try:
            if _browser_cookie_path_for(b) is not None:
                browser_choices.append(b)
        except Exception:
            from SYS.logger import logger
            logger.exception("Error while checking cookie path for browser '%s'", b)
            continue

    return [
        {
            "key": "format",
            "label": "Default format",
            "default": YtDlpDefaults.format,
            "choices": format_choices,
        },
        {
            "key": "cookies_from_browser",
            "label": "Browser cookies (if no file in plugins/ytdlp/)",
            "default": "auto",
            "choices": browser_choices,
        },
    ]

# Progress + utility helpers for yt-dlp driven downloads (previously in cmdlet/download_media).
_YTDLP_PROGRESS_BAR = ProgressBar()
_YTDLP_PROGRESS_ACTIVITY_LOCK = threading.Lock()
_YTDLP_PROGRESS_LAST_ACTIVITY = 0.0
_SUBTITLE_EXTS = (".vtt", ".srt", ".ass", ".ssa", ".lrc")


def _progress_label(status: Optional[Dict[str, Any]]) -> str:
    if not status:
        return "unknown"
    raw_info = status.get("info_dict")
    info_dict = raw_info if isinstance(raw_info, dict) else {}

    candidates = [
        status.get("filename"),
        info_dict.get("_filename"),
        info_dict.get("filepath"),
        info_dict.get("title"),
        info_dict.get("id"),
    ]

    for cand in candidates:
        if not cand:
            continue
        try:
            name = Path(str(cand)).name
        except Exception:
            name = str(cand)
        label = str(name or "").strip()
        if label:
            return label

    return "download"


def _record_progress_activity(timestamp: Optional[float] = None) -> None:
    global _YTDLP_PROGRESS_LAST_ACTIVITY
    with _YTDLP_PROGRESS_ACTIVITY_LOCK:
        _YTDLP_PROGRESS_LAST_ACTIVITY = timestamp if timestamp is not None else time.monotonic()


def _get_last_progress_activity() -> float:
    with _YTDLP_PROGRESS_ACTIVITY_LOCK:
        return _YTDLP_PROGRESS_LAST_ACTIVITY


def _clear_progress_activity() -> None:
    _record_progress_activity(0.0)


def _live_ui_and_pipe_index() -> tuple[Optional[Any], int]:
    ui = None
    try:
        ui = pipeline_context.get_live_progress() if hasattr(pipeline_context, "get_live_progress") else None
    except Exception:
        ui = None

    pipe_idx: int = 0
    try:
        stage_ctx = pipeline_context.get_stage_context() if hasattr(pipeline_context, "get_stage_context") else None
        maybe_idx = getattr(stage_ctx, "pipe_index", None) if stage_ctx is not None else None
        if isinstance(maybe_idx, int):
            pipe_idx = int(maybe_idx)
    except Exception:
        pipe_idx = 0

    return ui, pipe_idx


def _begin_live_steps(total_steps: int) -> None:
    ui, pipe_idx = _live_ui_and_pipe_index()
    if ui is None:
        return
    try:
        begin = getattr(ui, "begin_pipe_steps", None)
        if callable(begin):
            begin(int(pipe_idx), total_steps=int(total_steps))
    except Exception:
        return


def _step(text: str) -> None:
    ui, pipe_idx = _live_ui_and_pipe_index()
    if ui is None:
        return
    try:
        adv = getattr(ui, "advance_pipe_step", None)
        if callable(adv):
            adv(int(pipe_idx), str(text))
    except Exception:
        return


def _set_pipe_percent(percent: int) -> None:
    ui, pipe_idx = _live_ui_and_pipe_index()
    if ui is None:
        return
    try:
        set_pct = getattr(ui, "set_pipe_percent", None)
        if callable(set_pct):
            set_pct(int(pipe_idx), int(percent))
    except Exception:
        return


def _format_chapters_note(info: Dict[str, Any]) -> Optional[str]:
    """Format yt-dlp chapter metadata into a stable, note-friendly text."""
    try:
        chapters = info.get("chapters")
    except Exception:
        chapters = None

    if not isinstance(chapters, list) or not chapters:
        return None

    rows: List[tuple[int, Optional[int], str]] = []
    max_t = 0
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        start_raw = ch.get("start_time")
        end_raw = ch.get("end_time")
        title_raw = ch.get("title") or ch.get("name") or ch.get("chapter")

        try:
            if start_raw is None:
                continue
            start_s = int(float(start_raw))
        except Exception:
            continue

        end_s: Optional[int] = None
        try:
            if end_raw is not None:
                end_s = int(float(end_raw))
        except Exception:
            end_s = None

        title = str(title_raw).strip() if title_raw is not None else ""
        rows.append((start_s, end_s, title))
        try:
            max_t = max(max_t, start_s, end_s or 0)
        except Exception:
            max_t = max(max_t, start_s)

    if not rows:
        return None

    force_hours = bool(max_t >= 3600)

    def _tc(seconds: int) -> str:
        total = max(0, int(seconds))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if force_hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    lines: List[str] = []
    for start_s, end_s, title in sorted(
        rows, key=lambda r: (r[0], r[1] if r[1] is not None else 10**9, r[2])
    ):
        if end_s is not None and end_s > start_s:
            prefix = f"{_tc(start_s)}-{_tc(end_s)}"
        else:
            prefix = _tc(start_s)
        line = f"{prefix} {title}".strip()
        if line:
            lines.append(line)

    text = "\n".join(lines).strip()
    return text or None


def _best_subtitle_sidecar(media_path: Path) -> Optional[Path]:
    """Find the most likely subtitle sidecar file for a downloaded media file."""
    try:
        base_dir = media_path.parent
        stem = media_path.stem
        if not stem:
            return None

        candidates: List[Path] = []
        for p in base_dir.glob(stem + ".*"):
            try:
                if not p.is_file():
                    continue
            except Exception:
                continue
            if p.suffix.lower() in _SUBTITLE_EXTS:
                candidates.append(p)

        preferred_order = [".vtt", ".srt", ".ass", ".ssa", ".lrc"]
        for ext in preferred_order:
            for p in candidates:
                if p.suffix.lower() == ext:
                    return p

        return candidates[0] if candidates else None
    except Exception:
        return None


def _best_chat_sidecar(media_path: Path) -> Optional[Path]:
    try:
        base_dir = media_path.parent
        stem = media_path.stem
        if not stem:
            return None
        direct = base_dir / f"{stem}.live_chat.json"
        if direct.is_file():
            return direct
        matches = [
            path for path in base_dir.glob(f"{stem}*.live_chat.json")
            if path.is_file()
        ]
        return matches[0] if matches else None
    except Exception:
        return None


def _live_chat_message_text(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    renderer = (
        ((payload.get("replayChatItemAction") or {}).get("actions") or [None])[0]
        if "replayChatItemAction" in payload
        else payload
    )
    if not isinstance(renderer, dict):
        return None
    item = (
        ((renderer.get("addChatItemAction") or {}).get("item"))
        or renderer.get("liveChatTextMessageRenderer")
        or renderer
    )
    if not isinstance(item, dict):
        return None
    msg = item.get("liveChatTextMessageRenderer") or item
    if not isinstance(msg, dict):
        return None
    author = ""
    name = msg.get("authorName")
    if isinstance(name, dict):
        author = str(name.get("simpleText") or "").strip()
    elif isinstance(name, str):
        author = name.strip()
    runs = ((msg.get("message") or {}).get("runs") or []) if isinstance(msg.get("message"), dict) else []
    pieces = []
    for run in runs:
        if isinstance(run, dict) and run.get("text"):
            pieces.append(str(run.get("text")))
    text = "".join(pieces).strip()
    if not text:
        return None
    if author:
        return f"{author}: {text}"
    return text


def _format_live_chat_note(path: Path) -> Optional[str]:
    raw = _read_text_file(path)
    if not raw:
        return None
    messages: List[str] = []

    def _consume(obj: Any) -> None:
        line = _live_chat_message_text(obj)
        if line:
            messages.append(line)
        elif isinstance(obj, list):
            for item in obj:
                _consume(item)
        elif isinstance(obj, dict):
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    _consume(value)

    try:
        parsed = json.loads(raw)
        _consume(parsed)
    except Exception:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                _consume(json.loads(line))
            except Exception:
                continue
    text = "\n".join(messages).strip()
    if not text:
        text = raw.strip()
    if len(text) > 1_000_000:
        text = text[:1_000_000]
    return text or None


def _read_text_file(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def _download_with_sections_via_cli(
    url: str,
    ytdl_options: Dict[str, Any],
    sections: List[str],
    quiet: bool = False,
) -> tuple[Optional[str], Dict[str, Any]]:
    sections_list = ytdl_options.get("download_sections", [])
    if not sections_list:
        return "", {}

    pipeline = PipelineProgress(pipeline_context)

    section_hms_re = re.compile(r"\*?(\d{2}:\d{2}:\d{2})-(\d{2}:\d{2}:\d{2})")
    ytdlp_pct_re = re.compile(r"(?P<pct>\d{1,3}(?:\.\d+)?)%")
    ffmpeg_time_re = re.compile(r"time=(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}(?:\.\d+)?)")

    def _hms_to_seconds(hms: str) -> Optional[float]:
        try:
            hh, mm, ss = str(hms).split(":", 2)
            return float(int(hh) * 3600 + int(mm) * 60 + float(ss))
        except Exception:
            return None

    def _section_duration_seconds(section_text: str) -> Optional[float]:
        match = section_hms_re.search(str(section_text or "").strip())
        if not match:
            return None
        start_sec = _hms_to_seconds(match.group(1))
        end_sec = _hms_to_seconds(match.group(2))
        if start_sec is None or end_sec is None:
            return None
        duration = float(end_sec) - float(start_sec)
        if duration <= 0:
            return None
        return duration

    def _safe_set_percent(percent: int) -> None:
        try:
            _set_pipe_percent(max(0, min(int(percent), 99)))
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to set pipeline percent to %s", percent)

    def _run_section_command(
        cmd: List[str],
        *,
        section_idx: int,
        total_sections: int,
        section_label: str,
        section_duration: Optional[float],
    ) -> None:
        section_start_pct = int(((section_idx - 1) / max(1, total_sections)) * 99)
        section_end_pct = int((section_idx / max(1, total_sections)) * 99)
        if section_end_pct <= section_start_pct:
            section_end_pct = min(99, section_start_pct + 1)
        section_span = max(1, section_end_pct - section_start_pct)

        observed_progress = 0.0

        def _apply_progress(progress01: float, phase: str) -> None:
            nonlocal observed_progress
            p = max(0.0, min(float(progress01), 1.0))
            if p <= observed_progress:
                return
            observed_progress = p
            mapped = section_start_pct + int(round(section_span * p))
            _safe_set_percent(mapped)
            try:
                if phase == "clip":
                    pipeline.set_status(f"Clipping section {section_idx}/{total_sections} ({int(round(p * 100))}%)")
                else:
                    pipeline.set_status(f"Downloading section {section_idx}/{total_sections} ({int(round(p * 100))}%)")
            except Exception:
                pass

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        assert proc.stdout is not None
        tail_lines: List[str] = []
        try:
            _record_progress_activity()
            for raw_line in proc.stdout:
                line = str(raw_line or "").strip()
                if not line:
                    continue

                _record_progress_activity()

                if len(tail_lines) >= 120:
                    tail_lines.pop(0)
                tail_lines.append(line)

                m_pct = ytdlp_pct_re.search(line)
                if m_pct is not None:
                    try:
                        pct_value = float(m_pct.group("pct"))
                    except Exception:
                        pct_value = 0.0
                    # Reserve final 15% for ffmpeg clipping phase if present.
                    _apply_progress(min(0.85, max(0.0, pct_value / 100.0) * 0.85), "download")
                    continue

                m_time = ffmpeg_time_re.search(line)
                if m_time is not None and section_duration and section_duration > 0:
                    try:
                        elapsed = (
                            int(m_time.group("h")) * 3600
                            + int(m_time.group("m")) * 60
                            + float(m_time.group("s"))
                        )
                    except Exception:
                        elapsed = 0.0
                    clip_progress = min(1.0, max(0.0, float(elapsed) / float(section_duration)))
                    _apply_progress(0.85 + (clip_progress * 0.15), "clip")
                    continue

                # Non-empty output line from yt-dlp/ffmpeg still means the process is active.
                _apply_progress(min(0.98, observed_progress + 0.002), "download")

            return_code = proc.wait()
            if return_code != 0:
                tail = "\n".join(tail_lines[-12:]).strip()
                details = f"\n{tail}" if tail else ""
                raise DownloadError(
                    f"yt-dlp failed for section {section_label} (exit {return_code}){details}"
                )
            _apply_progress(1.0, "clip")
            _record_progress_activity()
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    session_id = hashlib.md5((url + str(time.time()) + "".join(random.choices(string.ascii_letters, k=10))).encode()).hexdigest()[:12]
    first_section_info = None

    total_sections = len(sections_list)
    try:
        for section_idx, section in enumerate(sections_list, 1):
            pipeline.set_status(f"Downloading & clipping clip section {section_idx}/{total_sections}")
            section_duration = _section_duration_seconds(section)

            base_outtmpl = ytdl_options.get("outtmpl", "%(title)s.%(ext)s")
            output_dir_path = Path(base_outtmpl).parent
            filename_tmpl = f"{session_id}_{section_idx}"
            if base_outtmpl.endswith(".%(ext)s"):
                filename_tmpl += ".%(ext)s"
            section_outtmpl = str(output_dir_path / filename_tmpl)

            if section_idx == 1:
                metadata_cmd = _yt_dlp_cli_prefix() + ["--dump-json", "--skip-download"]
                if ytdl_options.get("cookiefile"):
                    cookies_path = ytdl_options["cookiefile"].replace("\\", "/")
                    metadata_cmd.extend(["--cookies", cookies_path])
                if ytdl_options.get("noplaylist"):
                    metadata_cmd.append("--no-playlist")
                metadata_cmd.append(url)
                try:
                    meta_result = subprocess.run(metadata_cmd, capture_output=True, text=True)
                    if meta_result.returncode == 0 and meta_result.stdout:
                        try:
                            info_dict = json.loads(meta_result.stdout.strip())
                            first_section_info = info_dict
                            if not quiet:
                                debug(f"Extracted title from metadata: {info_dict.get('title')}")
                        except json.JSONDecodeError:
                            if not quiet:
                                debug("Could not parse JSON metadata")
                except Exception as exc:
                    if not quiet:
                        debug(f"Error extracting metadata: {exc}")

            cmd = _yt_dlp_cli_prefix()
            if quiet:
                cmd.append("--no-warnings")
            # Emit line-oriented progress and capture it for real clip activity tracking.
            cmd.append("--newline")
            cmd.extend(["--postprocessor-args", "ffmpeg:-hide_banner -nostdin -stats"])
            if ytdl_options.get("ffmpeg_location"):
                try:
                    cmd.extend(["--ffmpeg-location", str(ytdl_options["ffmpeg_location"])])
                except Exception:
                    from SYS.logger import logger
                    logger.exception("Failed to append ffmpeg_location CLI option")
            if ytdl_options.get("format"):
                cmd.extend(["-f", ytdl_options["format"]])
            if ytdl_options.get("merge_output_format"):
                cmd.extend(["--merge-output-format", str(ytdl_options["merge_output_format"])])

            postprocessors = ytdl_options.get("postprocessors")
            want_add_metadata = bool(ytdl_options.get("addmetadata"))
            want_embed_chapters = bool(ytdl_options.get("embedchapters"))
            if isinstance(postprocessors, list):
                for pp in postprocessors:
                    if not isinstance(pp, dict):
                        continue
                    if str(pp.get("key") or "") == "FFmpegMetadata":
                        want_add_metadata = True
                        if bool(pp.get("add_chapters", True)):
                            want_embed_chapters = True

            if want_add_metadata:
                cmd.append("--add-metadata")
            if want_embed_chapters:
                cmd.append("--embed-chapters")
            if ytdl_options.get("writesubtitles"):
                cmd.append("--write-sub")
                cmd.append("--write-auto-sub")
                cmd.extend(["--sub-format", "vtt"])
            if ytdl_options.get("force_keyframes_at_cuts"):
                cmd.append("--force-keyframes-at-cuts")
            cmd.extend(["-o", section_outtmpl])
            if ytdl_options.get("cookiefile"):
                cookies_path = ytdl_options["cookiefile"].replace("\\", "/")
                cmd.extend(["--cookies", cookies_path])
            if ytdl_options.get("noplaylist"):
                cmd.append("--no-playlist")

            cmd.extend(["--download-sections", section])
            cmd.append(url)
            if not quiet:
                debug(f"Running yt-dlp for section: {section}")

            _run_section_command(
                cmd,
                section_idx=section_idx,
                total_sections=total_sections,
                section_label=section,
                section_duration=section_duration,
            )
    finally:
        pipeline.clear_status()

    try:
        _set_pipe_percent(99)
    except Exception:
        from SYS.logger import logger
        logger.exception("Failed to set pipeline percent to 99 at end of multi-section job")

    return session_id, first_section_info or {}


def _is_remote_section_ffmpeg_failure(exc: Exception) -> bool:
    parts: List[str] = []
    for candidate in (exc, getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if candidate is None:
            continue
        try:
            parts.append(str(candidate))
        except Exception:
            continue
    detail = "\n".join(parts)
    return (
        "Error opening input file" in detail
        or "Error opening input files" in detail
        or "Connection to tcp://" in detail
        or "Error number -138 occurred" in detail
    )


def _resolve_ffmpeg_binary(ytdl_options: Dict[str, Any]) -> Optional[str]:
    location = str(ytdl_options.get("ffmpeg_location") or "").strip()
    if location:
        ffmpeg_path = Path(location)
        candidates = [ffmpeg_path]
        if ffmpeg_path.is_dir():
            candidates = [ffmpeg_path / "ffmpeg.exe", ffmpeg_path / "ffmpeg"]
        for candidate in candidates:
            try:
                if candidate.exists() and candidate.is_file():
                    return str(candidate)
            except Exception:
                continue

    for name in ("ffmpeg", "ffmpeg.exe"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def _parse_section_bounds(section_text: str) -> Optional[tuple[float, float]]:
    match = re.search(r"\*?(\d{2}:\d{2}:\d{2})-(\d{2}:\d{2}:\d{2})", str(section_text or "").strip())
    if not match:
        return None

    def _to_seconds(value: str) -> float:
        hours, minutes, seconds = value.split(":", 2)
        return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)

    start = _to_seconds(match.group(1))
    end = _to_seconds(match.group(2))
    if end <= start:
        return None
    return start, end


def _trim_sections_from_local_file(
    *,
    source_path: Path,
    output_dir: Path,
    sections: List[str],
    session_id: str,
    ytdl_options: Dict[str, Any],
    quiet: bool,
) -> List[Path]:
    ffmpeg_bin = _resolve_ffmpeg_binary(ytdl_options)
    if not ffmpeg_bin:
        raise DownloadError("ffmpeg not found for local clip fallback")

    generated: List[Path] = []
    for index, section_text in enumerate(sections, 1):
        bounds = _parse_section_bounds(section_text)
        if bounds is None:
            raise DownloadError(f"Invalid clip section: {section_text}")
        start_seconds, end_seconds = bounds
        duration_seconds = end_seconds - start_seconds
        output_path = output_dir / f"{session_id}_{index}{source_path.suffix or '.mp4'}"

        cmd = [
            ffmpeg_bin,
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{duration_seconds:.3f}",
            "-map_metadata",
            "0",
        ]

        if source_path.suffix.lower() in {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}:
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend([
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
            ])

        cmd.append(str(output_path))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr_text = str(result.stderr or "").strip()
            raise DownloadError(
                f"ffmpeg local clip fallback failed for section {section_text}: {stderr_text}"
            )
        if not quiet:
            debug(f"Local clip fallback wrote: {output_path.name}")
        generated.append(output_path)

    return generated


def _download_with_sections_via_local_trim(
    url: str,
    ytdl_options: Dict[str, Any],
    sections: List[str],
    quiet: bool = False,
) -> tuple[Optional[str], Dict[str, Any]]:
    download_options = dict(ytdl_options)
    download_options.pop("download_sections", None)
    download_options.pop("force_keyframes_at_cuts", None)

    assert yt_dlp is not None
    with yt_dlp.YoutubeDL(download_options) as ydl:  # type: ignore[arg-type]
        info = cast(Dict[str, Any], ydl.extract_info(url, download=True))

    output_dir = Path(str(download_options.get("outtmpl") or "%(_title)s.%(ext)s")).parent
    entry, media_path = _resolve_entry_and_path(info, output_dir)
    session_id = hashlib.md5((url + str(time.time()) + "localtrim").encode()).hexdigest()[:12]
    generated = _trim_sections_from_local_file(
        source_path=media_path,
        output_dir=output_dir,
        sections=sections,
        session_id=session_id,
        ytdl_options=ytdl_options,
        quiet=quiet,
    )

    try:
        media_path.unlink()
    except Exception:
        pass

    first_section_info = entry if isinstance(entry, dict) else info
    return session_id, first_section_info or {}


def _iter_download_entries(info: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    queue: List[Dict[str, Any]] = [info]
    seen: set[int] = set()
    while queue:
        current = queue.pop(0)
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        entries = current.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                queue.append(entry)
        if current.get("requested_downloads") or not entries:
            yield current


def _candidate_paths(entry: Dict[str, Any], output_dir: Path) -> Iterator[Path]:
    requested = entry.get("requested_downloads")
    if isinstance(requested, list):
        for item in requested:
            if isinstance(item, dict):
                fp = item.get("filepath") or item.get("_filename")
                if fp:
                    yield Path(fp)
    for key in ("filepath", "_filename", "filename"):
        value = entry.get(key)
        if value:
            yield Path(value)
    if entry.get("filename"):
        yield output_dir / entry["filename"]


def _resolve_entry_and_path(info: Dict[str, Any], output_dir: Path) -> tuple[Dict[str, Any], Path]:
    for entry in _iter_download_entries(info):
        for candidate in _candidate_paths(entry, output_dir):
            if candidate.is_file():
                return entry, candidate
            if not candidate.is_absolute():
                maybe = output_dir / candidate
                if maybe.is_file():
                    return entry, maybe
    raise FileNotFoundError("yt-dlp did not report a downloaded media file")


def _resolve_entries_and_paths(info: Dict[str, Any], output_dir: Path) -> List[tuple[Dict[str, Any], Path]]:
    resolved: List[tuple[Dict[str, Any], Path]] = []
    seen: set[str] = set()
    for entry in _iter_download_entries(info):
        chosen: Optional[Path] = None
        for candidate in _candidate_paths(entry, output_dir):
            if candidate.is_file():
                chosen = candidate
                break
            if not candidate.is_absolute():
                maybe = output_dir / candidate
                if maybe.is_file():
                    chosen = maybe
                    break
        if chosen is None:
            continue
        key = str(chosen.resolve())
        if key in seen:
            continue
        seen.add(key)
        resolved.append((entry, chosen))
    return resolved


def _extract_sha256(info: Dict[str, Any]) -> Optional[str]:
    for payload in [info] + info.get("entries", []):
        if not isinstance(payload, dict):
            continue
        hashes = payload.get("hashes")
        if isinstance(hashes, dict):
            for key in ("sha256", "sha-256", "sha_256"):
                if key in hashes and isinstance(hashes[key], str) and hashes[key].strip():
                    return hashes[key].strip()
        for key in ("sha256", "sha-256", "sha_256"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _is_sidecar_progress(status: Dict[str, Any]) -> bool:
    label = _progress_label(status).lower()
    if "live_chat" in label or label.endswith(".json") or ".info.json" in label:
        return True
    if any(label.endswith(ext) for ext in _SUBTITLE_EXTS):
        return True
    return False


def _progress_callback(status: Dict[str, Any]) -> None:
    global _YTDLP_PLAYLIST_STEPS_BEGUN
    label = _progress_label(status)
    event = status.get("status")
    downloaded = status.get("downloaded_bytes")
    total = status.get("total_bytes") or status.get("total_bytes_estimate")
    sidecar = _is_sidecar_progress(status)

    info_dict = status.get("info_dict", None)
    if isinstance(info_dict, dict):
        playlist_index = info_dict.get("playlist_index")
        playlist_count = info_dict.get("playlist_count")
        video_title = info_dict.get("title")
    else:
        playlist_index = None
        playlist_count = None
        video_title = None

    if event == "downloading" and not sidecar:
        _record_progress_activity()

    pipeline = PipelineProgress(pipeline_context)
    live_ui, _ = pipeline.ui_and_pipe_index()
    use_live = live_ui is not None

    def _total_bytes(value: Any) -> Optional[int]:
        try:
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        except Exception:
            from SYS.logger import logger
            logger.exception("Failed to interpret total bytes value: %r", value)
        return None

    if event == "downloading":
        if use_live and playlist_count and playlist_count > 1 and playlist_index is not None and not _YTDLP_PLAYLIST_STEPS_BEGUN:
            _begin_live_steps(int(playlist_count))
            _YTDLP_PLAYLIST_STEPS_BEGUN = True
        if use_live and playlist_index is not None and playlist_count and int(playlist_count) > 1:
            step_text = f"downloading {playlist_index}/{playlist_count}: {video_title or label}"
            try:
                pipeline.set_step(int(playlist_index), step_text)
            except Exception:
                _step(step_text)
        if sidecar:
            return
        if use_live:
            try:
                if not _YTDLP_TRANSFER_STATE.get(label, {}).get("started"):
                    pipeline.begin_transfer(label=label, total=_total_bytes(total))
                    _YTDLP_TRANSFER_STATE[label] = {"started": True}
                pipeline.update_transfer(
                    label=label,
                    completed=int(downloaded) if downloaded is not None else None,
                    total=_total_bytes(total),
                )
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to update pipeline transfer for label '%s'", label)
        else:
            _YTDLP_PROGRESS_BAR.update(
                downloaded=int(downloaded) if downloaded is not None else None,
                total=int(total) if total is not None else None,
                label=label,
                file=sys.stderr,
            )
    elif event == "finished":
        if sidecar:
            if use_live:
                _YTDLP_TRANSFER_STATE.pop(label, None)
            return
        if use_live:
            try:
                if _YTDLP_TRANSFER_STATE.get(label, {}).get("started"):
                    pipeline.finish_transfer(label=label)
            except Exception:
                from SYS.logger import logger
                logger.exception("Failed to finish pipeline transfer for label '%s'", label)
            _YTDLP_TRANSFER_STATE.pop(label, None)
            if (
                playlist_index is not None
                and playlist_count
                and int(playlist_index) >= int(playlist_count)
            ):
                _YTDLP_PLAYLIST_STEPS_BEGUN = False
        else:
            _YTDLP_PROGRESS_BAR.finish()
    elif event in ("postprocessing", "processing"):
        return


try:
    from SYS.metadata import extract_ytdlp_tags
except ImportError:
    extract_ytdlp_tags = None  # type: ignore


def _exception_message_blob(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    depth = 0
    while current is not None and depth < 6 and id(current) not in seen:
        seen.add(id(current))
        try:
            parts.append(str(current))
        except Exception:
            pass
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        depth += 1
    return "\n".join(parts).lower()


def _is_http_403(exc: Exception) -> bool:
    msg = _exception_message_blob(exc)
    return (
        "http error 403" in msg
        or "403: forbidden" in msg
        or "403 forbidden" in msg
    )


def _is_youtube_access_error(exc: Exception) -> bool:
    """True when YouTube/yt-dlp rejected access or format selection in a retryable way."""
    msg = _exception_message_blob(exc)
    markers = (
        "video unavailable",
        "this video is not available",
        "this video is unavailable",
        "sign in to confirm",
        "confirm your age",
        "private video",
        "login required",
        "requested format is not available",
        "only images are available",
        "http error 403",
        "403: forbidden",
        "403 forbidden",
        "the page needs to be reloaded",
    )
    if "unable to extract" in msg and ("youtube" in msg or "sign in" in msg):
        return True
    return any(marker in msg for marker in markers)


def _with_youtube_player_clients(options: Dict[str, Any], clients: Sequence[str]) -> Dict[str, Any]:
    updated = dict(options)
    extractor_args = dict(updated.get("extractor_args") or {})
    youtube_args = dict(extractor_args.get("youtube") or {})
    youtube_args["player_client"] = list(clients)
    extractor_args["youtube"] = youtube_args
    updated["extractor_args"] = extractor_args
    return updated


def _youtube_retry_option_sets(base_options: Dict[str, Any], *, mode: str) -> List[Dict[str, Any]]:
    """Ordered fallback configs when YouTube returns sign-in / unavailable / 403.

    Only two retries beyond the primary attempt — keeps the download fast:
    1) browser cookies if available, android/ios clients
    2) cookies.txt from plugins/ytdlp/ if present, android/ios clients
    """
    _ = mode
    variants: List[Dict[str, Any]] = []

    def _add(opts: Dict[str, Any]) -> None:
        key = (
            str(opts.get("format")),
            str(opts.get("cookiefile")),
            str(opts.get("cookiesfrombrowser")),
        )
        if any(
            (
                str(e.get("format")),
                str(e.get("cookiefile")),
                str(e.get("cookiesfrombrowser")),
            )
            == key
            for e in variants
        ):
            return
        variants.append(opts)

    # 1) Cookiefile from plugins/ytdlp/ — explicit Netscape export.
    cookiefile = base_options.get("cookiefile")
    if cookiefile:
        file_opts = dict(base_options)
        file_opts.pop("cookiesfrombrowser", None)
        file_opts["cookiefile"] = cookiefile
        _add(_with_youtube_player_clients(file_opts, ["android", "ios"]))

    # 2) Browser cookies (chrome/brave) — most reliable for logged-in users.
    browser_opts = dict(base_options)
    browser_opts.pop("cookiefile", None)
    _add_browser_cookies_if_available(browser_opts)
    _add(_with_youtube_player_clients(browser_opts, ["android", "ios"]))

    return variants


def _parse_bandcamp_tralbum(html_text: str) -> Optional[Dict[str, Any]]:
    import html as html_lib

    match = re.search(r'data-tralbum="([^"]*)"', html_text)
    if not match:
        match = re.search(r"data-tralbum='([^']*)'", html_text)
    if not match:
        return None
    raw = html_lib.unescape(match.group(1))
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _bandcamp_stream_url(tralbum: Dict[str, Any]) -> Optional[str]:
    tracks = tralbum.get("trackinfo")
    if not isinstance(tracks, list):
        return None
    for track in tracks:
        if not isinstance(track, dict):
            continue
        files = track.get("file")
        if not isinstance(files, dict):
            continue
        for key in ("mp3-v0", "mp3-320", "mp3-128"):
            value = files.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        for value in files.values():
            if isinstance(value, str) and value.startswith("http"):
                return value
    return None


def _html_is_bot_challenge(html_text: str) -> bool:
    text = str(html_text or "").lower()
    return (
        "client challenge" in text
        or "/_fs-ch-" in text
        or "enable javascript to proceed" in text
    )


def _fetch_bandcamp_html_http(url: str) -> str:
    from API.HTTP import HTTPClient

    headers = _http_headers_for_url(url)
    try:
        with HTTPClient(
            timeout=30.0,
            user_agent=headers.get("User-Agent") or _BROWSER_UA,
            headers=headers,
        ) as client:
            response = client.get(url, headers=headers, allow_redirects=True)
            html_text = getattr(response, "text", None) or ""
            if not html_text and getattr(response, "content", None):
                html_text = bytes(response.content).decode("utf-8", errors="replace")
            return html_text
    except Exception as exc:
        debug(f"[ytdlp] bandcamp page fetch failed: {exc}")
        return ""


def _fetch_bandcamp_html_playwright(url: str) -> str:
    try:
        from plugins.playwright import PlaywrightTool
    except Exception as exc:
        debug(f"[ytdlp] playwright unavailable for bandcamp: {exc}")
        return ""
    try:
        tool = PlaywrightTool({})
        tool.require()
        with tool.open_page(headless=True) as page:
            tool.goto(page, url)
            try:
                page.wait_for_selector("[data-tralbum]", timeout=45_000)
            except Exception:
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
            return str(page.content() or "")
    except Exception as exc:
        debug(f"[ytdlp] bandcamp playwright fetch failed: {exc}")
        return ""


def _fetch_bandcamp_html(url: str) -> str:
    html_text = _fetch_bandcamp_html_http(url)
    if html_text and "data-tralbum" in html_text and not _html_is_bot_challenge(html_text):
        return html_text
    if html_text and _html_is_bot_challenge(html_text):
        debug("[ytdlp] bandcamp returned a JS client challenge; retrying with playwright")
    pw_html = _fetch_bandcamp_html_playwright(url)
    if pw_html:
        return pw_html
    return html_text


def _download_bandcamp_fallback(opts: DownloadOptions) -> Optional[DownloadMediaResult]:
    from API.HTTP import download_direct_file

    html_text = _fetch_bandcamp_html(opts.url)
    tralbum = _parse_bandcamp_tralbum(html_text)
    if not tralbum:
        debug("[ytdlp] bandcamp page had no data-tralbum")
        return None
    stream_url = _bandcamp_stream_url(tralbum)
    if not stream_url:
        debug("[ytdlp] bandcamp tralbum had no stream url")
        return None
    current = tralbum.get("current") if isinstance(tralbum.get("current"), dict) else {}
    title = str((current or {}).get("title") or "bandcamp")
    artist = str(tralbum.get("artist") or "").strip()
    if isinstance(tralbum.get("trackinfo"), list) and tralbum["trackinfo"]:
        first = tralbum["trackinfo"][0]
        if isinstance(first, dict) and first.get("title"):
            title = str(first.get("title") or title)
    suggested = sanitize_filename(f"{artist} - {title}".strip(" -") if artist else title) + ".mp3"
    debug(f"[ytdlp] bandcamp stream fallback: {stream_url}")
    result = download_direct_file(
        stream_url,
        opts.output_dir,
        quiet=bool(opts.quiet),
        suggested_filename=suggested,
    )
    info = {
        "title": title,
        "artist": artist,
        "album": str(tralbum.get("album_title") or (current or {}).get("title") or ""),
        "webpage_url": opts.url,
        "extractor": "bandcamp",
        "ext": "mp3",
    }
    tags = []
    if extract_ytdlp_tags is not None:
        try:
            tags = extract_ytdlp_tags(info)
        except Exception:
            tags = []
    return DownloadMediaResult(
        path=result.path,
        info=info,
        tag=tags or list(result.tag or []),
        source_url=opts.url,
        hash_value=result.hash_value,
    )


def download_media(opts: DownloadOptions, *, config: Optional[Dict[str, Any]] = None, debug_logger: Optional[DebugLogger] = None) -> Any:
    """Download streaming media exclusively via yt-dlp.

    Optional `config` dict may be provided so tool defaults (e.g., cookies, default
    format) are applied when constructing the YtDlpTool instance.
    """

    try:
        netloc = urlparse(opts.url).netloc.lower()
    except Exception:
        netloc = ""
    if "gofile.io" in netloc:
        msg = "GoFile links are currently unsupported"
        if not opts.quiet:
            debug(msg)
        if debug_logger is not None:
            debug_logger.write_record("gofile-unsupported", {"url": opts.url})
        raise DownloadError(msg)

    ytdlp_supported = is_url_supported_by_ytdlp(opts.url)
    if not ytdlp_supported:
        msg = "URL not supported by yt-dlp; try download-file for manual downloads"
        if not opts.quiet:
            log(msg)
        if debug_logger is not None:
            debug_logger.write_record("ytdlp-unsupported", {"url": opts.url})
        raise DownloadError(msg)

    if opts.playlist_items:
        probe_result: Optional[Dict[str, Any]] = {"url": opts.url}
    else:
        probe_cookiefile = None
        try:
            if opts.cookies_path and opts.cookies_path.is_file():
                probe_cookiefile = str(opts.cookies_path)
        except Exception:
            probe_cookiefile = None

        probe_result = probe_url(opts.url, no_playlist=opts.no_playlist, timeout_seconds=15, cookiefile=probe_cookiefile)

    if probe_result is None:
        if debug_logger is not None:
            debug_logger.write_record("ytdlp-probe-miss-continue", {"url": opts.url})

    ensure_yt_dlp_ready()

    # Use provided config when available so user tool settings are honored
    ytdlp_tool = YtDlpTool(config or {})
    ytdl_options = ytdlp_tool.build_ytdlp_options(opts)
    hooks = ytdl_options.get("progress_hooks")
    if not isinstance(hooks, list):
        hooks = []
        ytdl_options["progress_hooks"] = hooks
    if _progress_callback not in hooks:
        hooks.append(_progress_callback)
    if not opts.quiet:
        debug_panel(
            "yt-dlp job",
            [
                ("url", opts.url),
                ("mode", opts.mode),
                ("format", ytdl_options.get("format") or "default"),
                ("cookiefile", ytdl_options.get("cookiefile")),
                ("download_sections", ytdl_options.get("download_sections") or "None"),
                ("force_keyframes", ytdl_options.get("force_keyframes_at_cuts", False)),
            ],
            border_style="blue",
        )
    if debug_logger is not None:
        debug_logger.write_record("ytdlp-start", {"url": opts.url})

    assert yt_dlp is not None
    info: Optional[Dict[str, Any]] = None
    session_id = None
    first_section_info: Dict[str, Any] = {}
    try:
        if ytdl_options.get("download_sections"):
            live_ui, _ = PipelineProgress(pipeline_context).ui_and_pipe_index()
            quiet_sections = bool(opts.quiet) or (live_ui is not None)
            section_list = ytdl_options.get("download_sections", [])
            try:
                session_id, first_section_info = _download_with_sections_via_cli(
                    opts.url,
                    ytdl_options,
                    section_list,
                    quiet=quiet_sections,
                )
            except Exception as clip_exc:
                if not _is_remote_section_ffmpeg_failure(clip_exc):
                    raise
                debug("[yt-dlp] remote section clipping failed; retrying via local trim fallback")
                session_id, first_section_info = _download_with_sections_via_local_trim(
                    opts.url,
                    ytdl_options,
                    section_list,
                    quiet=quiet_sections,
                )
            info = None
        else:
            with yt_dlp.YoutubeDL(ytdl_options) as ydl:  # type: ignore[arg-type]
                info = cast(Dict[str, Any], ydl.extract_info(opts.url, download=True))
    except Exception as exc:
        retry_attempted = False
        can_retry = (not ytdl_options.get("download_sections")) and (
            _is_http_403(exc) or _is_youtube_access_error(exc)
        )
        if can_retry:
            retry_attempted = True
            last_error: Exception = exc
            try:
                netloc = urlparse(opts.url).netloc.lower()
            except Exception:
                netloc = ""
            is_youtube = ("youtube.com" in netloc) or ("youtu.be" in netloc)
            retry_sets = (
                _youtube_retry_option_sets(ytdl_options, mode=str(opts.mode or ""))
                if is_youtube
                else []
            )
            if not retry_sets:
                # Non-YouTube 403: keep prior browser-cookie retry behavior.
                fallback_options = dict(ytdl_options)
                fallback_options.pop("cookiefile", None)
                _add_browser_cookies_if_available(fallback_options)
                retry_sets = [fallback_options]

            if not opts.quiet:
                debug(f"yt-dlp access/format failure; trying {len(retry_sets)} fallback(s)")

            recovered = False
            for idx, fallback_options in enumerate(retry_sets, 1):
                try:
                    debug(f"[ytdlp] retry {idx}/{len(retry_sets)}")
                    with yt_dlp.YoutubeDL(fallback_options) as ydl:  # type: ignore[arg-type]
                        info = cast(Dict[str, Any], ydl.extract_info(opts.url, download=True))
                    recovered = True
                    break
                except Exception as exc2:
                    last_error = exc2
                    continue

            if not recovered:
                if _is_bandcamp_url(opts.url):
                    fallback = _download_bandcamp_fallback(opts)
                    if fallback is not None:
                        return fallback
                log(f"yt-dlp failed: {last_error}", file=sys.stderr)
                if debug_logger is not None:
                    debug_logger.write_record(
                        "exception",
                        {
                            "phase": "yt-dlp",
                            "error": str(last_error),
                            "traceback": traceback.format_exc(),
                        },
                    )
                raise DownloadError("yt-dlp download failed") from last_error

        if not retry_attempted:
            if _is_bandcamp_url(opts.url):
                fallback = _download_bandcamp_fallback(opts)
                if fallback is not None:
                    return fallback
            log(f"yt-dlp failed: {exc}", file=sys.stderr)
            if debug_logger is not None:
                debug_logger.write_record(
                    "exception",
                    {"phase": "yt-dlp", "error": str(exc), "traceback": traceback.format_exc()},
                )
            raise DownloadError("yt-dlp download failed") from exc

    if info is None:
        try:
            time.sleep(0.5)
            files = sorted(opts.output_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            if not files:
                raise FileNotFoundError(f"No files found in {opts.output_dir}")

            if opts.clip_sections and session_id:
                section_pattern = re.compile(rf"^{re.escape(session_id)}_(\d+)")
                matching_files = [f for f in files if section_pattern.search(f.name)]

                if matching_files:
                    def extract_section_num(path: Path) -> int:
                        match = section_pattern.search(path.name)
                        return int(match.group(1)) if match else 999

                    matching_files.sort(key=extract_section_num)
                    debug(f"Found {len(matching_files)} section file(s) matching pattern")

                    by_index: Dict[int, List[Path]] = {}
                    for f in matching_files:
                        m = section_pattern.search(f.name)
                        if not m:
                            continue
                        try:
                            n = int(m.group(1))
                        except Exception:
                            continue
                        by_index.setdefault(n, []).append(f)

                    renamed_media_files: List[Path] = []

                    for sec_num in sorted(by_index.keys()):
                        group = by_index.get(sec_num) or []
                        if not group:
                            continue

                        def _is_subtitle(p: Path) -> bool:
                            try:
                                return p.suffix.lower() in _SUBTITLE_EXTS
                            except Exception:
                                return False

                        media_candidates = [p for p in group if not _is_subtitle(p)]
                        subtitle_candidates = [p for p in group if _is_subtitle(p)]

                        media_file: Optional[Path] = None
                        for cand in media_candidates:
                            try:
                                if cand.suffix.lower() in {".json", ".info.json"}:
                                    continue
                            except Exception:
                                from SYS.logger import logger
                                logger.exception("Failed to inspect candidate suffix for %s", cand)
                            media_file = cand
                            break
                        if media_file is None and media_candidates:
                            media_file = media_candidates[0]
                        if media_file is None:
                            continue

                        try:
                            media_hash = sha256_file(media_file)
                        except Exception as exc:
                            debug(f"Failed to hash section media file {media_file.name}: {exc}")
                            renamed_media_files.append(media_file)
                            continue

                        prefix = f"{session_id}_{sec_num}"

                        def _tail(name: str) -> str:
                            try:
                                if name.startswith(prefix):
                                    return name[len(prefix):]
                            except Exception:
                                from SYS.logger import logger
                                logger.exception("Failed to check name prefix for '%s'", name)
                            try:
                                return Path(name).suffix
                            except Exception:
                                from SYS.logger import logger
                                logger.exception("Failed to obtain suffix for name '%s'", name)
                                return ""

                        try:
                            new_media_name = f"{media_hash}{_tail(media_file.name)}"
                            new_media_path = opts.output_dir / new_media_name
                            if new_media_path.exists() and new_media_path != media_file:
                                debug(f"File with hash {media_hash} already exists, using existing file.")
                                try:
                                    media_file.unlink()
                                except OSError:
                                    from SYS.logger import logger
                                    logger.exception("Failed to unlink duplicate media file %s", media_file)
                            else:
                                media_file.rename(new_media_path)
                                debug(f"Renamed section file: {media_file.name} -> {new_media_name}")
                            renamed_media_files.append(new_media_path)
                        except Exception as exc:
                            debug(f"Failed to rename section media file {media_file.name}: {exc}")
                            renamed_media_files.append(media_file)
                            new_media_path = media_file

                        for sub_file in subtitle_candidates:
                            try:
                                new_sub_name = f"{media_hash}{_tail(sub_file.name)}"
                                new_sub_path = opts.output_dir / new_sub_name
                                if new_sub_path.exists() and new_sub_path != sub_file:
                                    try:
                                        sub_file.unlink()
                                    except OSError:
                                        pass
                                else:
                                    sub_file.rename(new_sub_path)
                                    debug(f"Renamed section file: {sub_file.name} -> {new_sub_name}")
                            except Exception as exc:
                                debug(f"Failed to rename section subtitle file {sub_file.name}: {exc}")

                    media_path = renamed_media_files[0] if renamed_media_files else matching_files[0]
                    media_paths = renamed_media_files if renamed_media_files else None
                    if not opts.quiet:
                        count = len(media_paths) if isinstance(media_paths, list) else 1
                        debug_panel(
                            "yt-dlp result",
                            [
                                ("files", count),
                                ("session", session_id),
                                ("file", media_path.name),
                            ],
                            border_style="green",
                        )
                else:
                    media_path = files[0]
                    media_paths = None
            else:
                media_path = files[0]
                media_paths = None
            if debug_logger is not None:
                debug_logger.write_record("ytdlp-file-found", {"path": str(media_path)})
        except Exception as exc:
            log(f"Error finding downloaded file: {exc}", file=sys.stderr)
            if debug_logger is not None:
                debug_logger.write_record("exception", {"phase": "find-file", "error": str(exc)})
            raise DownloadError(str(exc)) from exc

        file_hash = sha256_file(media_path)
        section_tags: List[str] = []
        title = ""
        if first_section_info:
            title = first_section_info.get("title", "")
            if title:
                section_tags.append(f"title:{title}")
                debug(f"Added title tag for section download: {title}")

        if first_section_info:
            info_dict_sec = first_section_info
        else:
            info_dict_sec = {"id": media_path.stem, "title": title or media_path.stem, "ext": media_path.suffix.lstrip(".")}

        return DownloadMediaResult(path=media_path, info=info_dict_sec, tag=section_tags, source_url=opts.url, hash_value=file_hash, paths=media_paths)

    if not isinstance(info, dict):
        log(f"Unexpected yt-dlp response: {type(info)}", file=sys.stderr)
        raise DownloadError("Unexpected yt-dlp response type")

    info_dict: Dict[str, Any] = cast(Dict[str, Any], info)
    if debug_logger is not None:
        debug_logger.write_record("ytdlp-info", {"keys": sorted(info_dict.keys()), "is_playlist": bool(info_dict.get("entries"))})

    if info_dict.get("entries") and not opts.no_playlist:
        resolved = _resolve_entries_and_paths(info_dict, opts.output_dir)
        if resolved:
            results: List[DownloadMediaResult] = []
            for entry, media_path in resolved:
                hash_value = _extract_sha256(entry) or _extract_sha256(info_dict)
                if not hash_value:
                    try:
                        hash_value = sha256_file(media_path)
                    except OSError:
                        hash_value = None

                tags: List[str] = []
                if extract_ytdlp_tags is not None:
                    try:
                        tags = extract_ytdlp_tags(entry)
                    except Exception as exc:
                        log(f"Error extracting tags: {exc}", file=sys.stderr)

                source_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or opts.url

                results.append(
                    DownloadMediaResult(
                        path=media_path,
                        info=entry,
                        tag=tags,
                        source_url=source_url,
                        hash_value=hash_value,
                    )
                )

            if not opts.quiet:
                debug(f"✓ Downloaded playlist items: {len(results)}")
            return results

    try:
        entry, media_path = _resolve_entry_and_path(info_dict, opts.output_dir)
    except FileNotFoundError as exc:
        log(f"Error: {exc}", file=sys.stderr)
        if debug_logger is not None:
            debug_logger.write_record("exception", {"phase": "resolve-path", "error": str(exc)})
        raise DownloadError(str(exc)) from exc

    if debug_logger is not None:
        debug_logger.write_record("resolved-media", {"path": str(media_path), "entry_keys": sorted(entry.keys())})

    hash_value = _extract_sha256(entry) or _extract_sha256(info_dict)
    if not hash_value:
        try:
            hash_value = sha256_file(media_path)
        except OSError as exc:
            if debug_logger is not None:
                debug_logger.write_record("hash-error", {"path": str(media_path), "error": str(exc)})

    tags_res: List[str] = []
    if extract_ytdlp_tags is not None:
        try:
            tags_res = extract_ytdlp_tags(entry)
        except Exception as exc:
            log(f"Error extracting tags: {exc}", file=sys.stderr)

    source_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")

    if not opts.quiet:
        debug_panel(
            "yt-dlp result",
            [
                ("file", media_path.name),
                ("tag_count", len(tags_res)),
                ("source_url", source_url or opts.url),
            ],
            border_style="green",
        )
    if debug_logger is not None:
        debug_logger.write_record(
            "downloaded",
            {
                "path": str(media_path),
                "tag_count": len(tags_res),
                "source_url": source_url,
                "sha256": hash_value,
            },
        )

    return DownloadMediaResult(path=media_path, info=entry, tag=tags_res, source_url=source_url, hash_value=hash_value)


def _download_with_timeout(opts: DownloadOptions, timeout_seconds: int = 300, config: Optional[Dict[str, Any]] = None) -> Any:
    from contextvars import copy_context
    import threading
    from typing import cast

    result_container: List[Optional[Any]] = [None, None]

    def _do_download() -> None:
        try:
            result_container[0] = download_media(opts, config=config)
        except Exception as exc:
            result_container[1] = exc

    # Preserve the active pipeline ContextVar state so yt-dlp progress hooks in the
    # worker thread can still see the shared Live UI and stage context.
    download_context = copy_context()

    # Use daemon=True so a hung download doesn't block process exit if the wall timeout hits.
    thread = threading.Thread(target=lambda: download_context.run(_do_download), daemon=True)
    thread.start()
    start_time = time.monotonic()
    
    # Keep only an activity timeout here. yt-dlp downloads can be legitimately slow
    # for large media or constrained connections, and a fixed wall-clock cutoff can
    # abort healthy downloads even while progress is still arriving.

    _record_progress_activity(start_time)
    try:
        while thread.is_alive():
            thread.join(1)
            if not thread.is_alive():
                break
                
            now = time.monotonic()
            
            # Check activity timeout
            last_activity = _get_last_progress_activity()
            if last_activity <= 0:
                last_activity = start_time
            if now - last_activity > timeout_seconds:
                raise DownloadError(f"Download activity timeout after {timeout_seconds} seconds for {opts.url}")
                
    finally:
        _clear_progress_activity()

    if result_container[1] is not None:
        raise cast(Exception, result_container[1])

    if result_container[0] is None:
        raise DownloadError(f"Download failed for {opts.url}")

    return cast(Any, result_container[0])
