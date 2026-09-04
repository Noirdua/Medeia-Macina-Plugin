from __future__ import annotations

import importlib
import os
import re
import sys
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional

from urllib.parse import quote, unquote, urlparse

from API.HTTP import download_direct_file
from PluginCore.base import SearchResult
from SYS.utils import sanitize_filename, unique_path
from SYS.logger import log
from plugins.archiveorg._common import archive_credentials, plugin_config_entry

# Helper for download-file: render selectable formats for a details URL.
def maybe_show_formats_table(
    *,
    raw_urls: Any,
    piped_items: Any,
    parsed: Dict[str, Any],
    config: Dict[str, Any],
    quiet_mode: bool,
    get_field: Any,
) -> Optional[int]:
    """If input is a single Internet Archive details URL, render a formats table.

    Returns an exit code when handled; otherwise None.
    """
    # Do not suppress the picker in quiet/background mode: this selector UX is
    # required for Internet Archive "details" pages (which are not directly downloadable).

    try:
        total_inputs = int(len(raw_urls or []) + len(piped_items or []))
    except Exception:
        total_inputs = 0

    if total_inputs != 1:
        return None

    item = piped_items[0] if piped_items else None
    target = ""
    if item is not None:
        try:
            target = str(get_field(item,
                                   "path") or get_field(item,
                                                         "url") or "").strip()
        except Exception:
            target = ""
    if not target and raw_urls:
        target = str(raw_urls[0]).strip()
    if not target:
        return None

    identifier = ""
    try:
        md = get_field(item, "full_metadata") if item is not None else None
        if isinstance(md, dict):
            identifier = str(md.get("identifier") or "").strip()
    except Exception:
        identifier = ""
    if not identifier:
        try:
            identifier = str(extract_identifier(target) or "").strip()
        except Exception:
            identifier = ""
    if not identifier:
        return None

    # Only show picker for item pages (details); direct download URLs should download immediately.
    try:
        if not is_details_url(target):
            return None
    except Exception:
        return None

    try:
        files = list_download_files(identifier)
    except Exception as exc:
        log(f"download-file: Internet Archive lookup failed: {exc}", file=sys.stderr)
        return 1

    if not files:
        log("download-file: Internet Archive item has no downloadable files", file=sys.stderr)
        return 1

    title = ""
    try:
        title = str(get_field(item, "title") or "").strip() if item is not None else ""
    except Exception:
        title = ""
    table_title = (
        f"Internet Archive: {title}".strip().rstrip(":")
        if title else f"Internet Archive: {identifier}"
    )

    try:
        from SYS.result_table import Table
        from SYS import pipeline as pipeline_context
    except Exception as exc:
        log(f"download-file: ResultTable unavailable: {exc}", file=sys.stderr)
        return 1

    base_args: List[str] = []
    out_arg = parsed.get("path")
    if out_arg:
        base_args.extend(["-path", str(out_arg)])

    table = Table(table_title)._perseverance(True)
    table.set_table("internetarchive.format")
    table.set_source_command("download-file", base_args)

    rows: List[Dict[str, Any]] = []
    for f in files:
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        fmt = str(f.get("format") or "").strip()
        src = str(f.get("source") or "").strip()
        direct_url = str(f.get("direct_url") or "").strip()
        if not direct_url:
            continue

        size_val: Any = f.get("size")
        try:
            size_val = int(size_val) if size_val not in (None, "") else ""
        except Exception:
            pass

        row_item: Dict[str, Any] = {
            "table": "internetarchive",
            "title": fmt or name,
            "path": direct_url,
            "url": direct_url,
            "columns": [
                ("Format", fmt),
                ("Name", name),
                ("Size", size_val),
                ("Source", src),
            ],
            "_selection_args": [direct_url],
            "full_metadata": {
                "identifier": identifier,
                "name": name,
                "format": fmt,
                "source": src,
                "size": f.get("size"),
            },
        }
        rows.append(row_item)
        table.add_result(row_item)

    if not rows:
        log("download-file: no downloadable files found for this item", file=sys.stderr)
        return 1

    try:
        pipeline_context.set_last_result_table(table, rows, subject=item)
        pipeline_context.set_current_stage_table(table)
    except Exception:
        pass

    log("Internet Archive item detected: select a file with @N to download", file=sys.stderr)
    return 0


def _ia() -> Any:
    try:
        return importlib.import_module("internetarchive")
    except Exception as exc:
        raise Exception(f"internetarchive module not available: {exc}")


def _pick_provider_config(config: Any) -> Dict[str, Any]:
    return plugin_config_entry(config)


def _pick_archive_credentials(config: Any) -> tuple[Optional[str], Optional[str]]:
    return archive_credentials(config)


def _filename_from_response(url: str, response: requests.Response, suggested_filename: Optional[str] = None) -> str:
    suggested = str(suggested_filename or "").strip()
    if suggested:
        guessed_ext = Path(str(_extract_download_filename_from_url(url) or "")).suffix
        if Path(suggested).suffix:
            return sanitize_filename(suggested)
        merged = f"{suggested}{guessed_ext}" if guessed_ext else suggested
        return sanitize_filename(merged)

    content_disposition = ""
    try:
        content_disposition = str(response.headers.get("content-disposition", "") or "")
    except Exception:
        content_disposition = ""

    if content_disposition:
        m = re.search(r'filename\*?=(?:"([^"]+)"|([^;\s]+))', content_disposition)
        if m:
            candidate = (m.group(1) or m.group(2) or "").strip().strip('"')
            if candidate:
                return sanitize_filename(unquote(candidate))

    extracted = _extract_download_filename_from_url(url)
    if extracted:
        return sanitize_filename(extracted)

    fallback = Path(urlparse(url).path).name or "download.bin"
    return sanitize_filename(unquote(fallback))


def _download_with_requests_session(
    *,
    session: requests.Session,
    url: str,
    output_dir: Path,
    suggested_filename: Optional[str] = None,
) -> Path:
    headers = {
        "Referer": "https://archive.org/",
        "Accept": "*/*",
    }
    response = session.get(url, headers=headers, stream=True, allow_redirects=True, timeout=120)
    response.raise_for_status()

    filename = _filename_from_response(url, response, suggested_filename=suggested_filename)
    out_path = unique_path(Path(output_dir) / filename)

    with open(out_path, "wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)

    return out_path


def _looks_fielded_query(q: str) -> bool:
    low = (q or "").lower()
    return (":" in low) or (" and " in low) or (" or "
                                                in low) or (" not "
                                                            in low) or ("(" in low)


def _extract_identifier_from_any(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    if raw.lower().startswith("ia:"):
        return raw.split(":", 1)[1].strip()

    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            from urllib.parse import urlparse

            p = urlparse(raw)
            host = (p.hostname or "").lower().strip()
            path = (p.path or "").strip("/")
        except Exception:
            return ""

        if not host.endswith("archive.org"):
            return ""

        parts = [x for x in path.split("/") if x]
        # /details/<identifier>
        if len(parts) >= 2 and parts[0].lower() == "details":
            return str(parts[1]).strip()
        # /download/<identifier>/<filename>
        if len(parts) >= 2 and parts[0].lower() == "download":
            return str(parts[1]).strip()

        return ""

    # Assume bare identifier
    return raw


def extract_identifier(value: str) -> str:
    """Public wrapper for extracting an IA identifier from URLs/tags/bare ids."""
    return _extract_identifier_from_any(value)


def is_details_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    if not (raw.startswith("http://") or raw.startswith("https://")):
        return False
    try:
        p = urlparse(raw)
        host = (p.hostname or "").lower().strip()
        parts = [x for x in (p.path or "").split("/") if x]
    except Exception:
        return False
    if not host.endswith("archive.org"):
        return False
    return len(parts) >= 2 and parts[0].lower() == "details" and bool(parts[1].strip())


def is_download_file_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    if not (raw.startswith("http://") or raw.startswith("https://")):
        return False
    try:
        p = urlparse(raw)
        host = (p.hostname or "").lower().strip()
        parts = [x for x in (p.path or "").split("/") if x]
    except Exception:
        return False
    if not host.endswith("archive.org"):
        return False
    # /download/<identifier>/<filename>
    return (
        len(parts) >= 3 and parts[0].lower() == "download" and bool(parts[1].strip())
        and bool(parts[2].strip())
    )


def _archive_item_access(identifier: str) -> Dict[str, Any]:
    ident = str(identifier or "").strip()
    if not ident:
        return {"mediatype": "", "lendable": False, "collection": []}

    session = requests.Session()
    try:
        response = session.get(f"https://archive.org/metadata/{ident}", timeout=8)
        response.raise_for_status()
        data = response.json() if response is not None else {}
    except Exception:
        return {"mediatype": "", "lendable": False, "collection": []}
    finally:
        try:
            session.close()
        except Exception:
            pass

    meta = data.get("metadata", {}) if isinstance(data, dict) else {}
    if not isinstance(meta, dict):
        meta = {}

    mediatype = str(meta.get("mediatype") or "").strip().lower()
    collection = meta.get("collection")
    values: List[str] = []
    if isinstance(collection, list):
        values = [str(x).strip().lower() for x in collection if str(x).strip()]
    elif isinstance(collection, str) and collection.strip():
        values = [collection.strip().lower()]

    lendable = any(v in {"inlibrary", "lendinglibrary"} for v in values)
    return {
        "mediatype": mediatype,
        "lendable": lendable,
        "collection": values,
    }


def list_download_files(identifier: str) -> List[Dict[str, Any]]:
    """Return a sorted list of downloadable files for an IA identifier.

    Each entry includes: name, size, format, source, direct_url.
    """
    ident = str(identifier or "").strip()
    if not ident:
        return []

    ia = _ia()
    get_item = getattr(ia, "get_item", None)
    if not callable(get_item):
        raise Exception("internetarchive.get_item is not available")

    try:
        item: Any = get_item(str(ident))
    except Exception as exc:
        raise Exception(f"Internet Archive item lookup failed: {exc}")

    files: List[Dict[str, Any]] = []
    try:
        raw_files = getattr(item, "files", None)
        if isinstance(raw_files, list):
            for f in raw_files:
                if isinstance(f, dict):
                    files.append(f)
    except Exception:
        files = []

    if not files:
        try:
            for f in item.get_files():
                name = getattr(f, "name", None)
                if not name and isinstance(f, dict):
                    name = f.get("name")
                if not name:
                    continue
                files.append(
                    {
                        "name": str(name),
                        "size": getattr(f,
                                        "size",
                                        None),
                        "format": getattr(f,
                                          "format",
                                          None),
                        "source": getattr(f,
                                          "source",
                                          None),
                    }
                )
        except Exception:
            files = []

    if not files:
        return []

    def _is_ia_metadata_file(f: Dict[str, Any]) -> bool:
        try:
            source = str(f.get("source") or "").strip().lower()
            fmt = str(f.get("format") or "").strip().lower()
        except Exception:
            source = ""
            fmt = ""

        if source == "metadata":
            return True
        if fmt in {"metadata",
                   "archive bittorrent"}:
            return True
        if fmt.startswith("thumbnail"):
            return True
        return False

    candidates = [
        f for f in files if isinstance(f, dict) and not _is_ia_metadata_file(f)
    ]
    if not candidates:
        candidates = [f for f in files if isinstance(f, dict)]

    out: List[Dict[str, Any]] = []
    for f in candidates:
        name = str(f.get("name") or "").strip()
        if not name:
            continue

        direct_url = f"https://archive.org/download/{ident}/{quote(name, safe='')}"
        out.append(
            {
                "name": name,
                "size": f.get("size"),
                "format": f.get("format"),
                "source": f.get("source"),
                "direct_url": direct_url,
            }
        )

    def _key(f: Dict[str, Any]) -> tuple[str, str]:
        fmt = str(f.get("format") or "").strip().lower()
        name = str(f.get("name") or "").strip().lower()
        return (fmt, name)

    out.sort(key=_key)
    return out


def _extract_download_filename_from_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if not (raw.startswith("http://") or raw.startswith("https://")):
        return ""

    try:
        from urllib.parse import urlparse

        p = urlparse(raw)
        host = (p.hostname or "").lower().strip()
        path = (p.path or "").strip("/")
    except Exception:
        return ""

    if not host.endswith("archive.org"):
        return ""

    parts = [x for x in path.split("/") if x]
    # /download/<identifier>/<filename...>
    if len(parts) >= 3 and parts[0].lower() == "download":
        # Keep subpath segments if present and decode URL encoding.
        encoded_name = "/".join(parts[2:]).strip()
        return unquote(encoded_name)

    return ""


def _normalize_identifier(s: str) -> str:
    text = str(s or "").strip().lower()
    if not text:
        return ""

    # Internet Archive identifiers are fairly permissive; keep alnum, '_', '-', '.' and collapse the rest.
    text = re.sub(r"[^a-z0-9_.-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")

    if len(text) > 80:
        text = text[:80].rstrip("-._")

    return text


def _best_file_candidate(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not files:
        return None

    def _is_metadata(f: Dict[str, Any]) -> bool:
        source = str(f.get("source") or "").strip().lower()
        fmt = str(f.get("format") or "").strip().lower()
        if source == "metadata":
            return True
        if fmt in {"metadata",
                   "archive bittorrent"}:
            return True
        if fmt.startswith("thumbnail"):
            return True
        return False

    def _size(f: Dict[str, Any]) -> int:
        try:
            return int(f.get("size") or 0)
        except Exception:
            return 0

    candidates = [f for f in files if not _is_metadata(f)]
    if not candidates:
        candidates = list(files)

    # Prefer originals.
    originals = [
        f for f in candidates
        if str(f.get("source") or "").strip().lower() == "original"
    ]
    pool = originals if originals else candidates

    pool = [f for f in pool if str(f.get("name") or "").strip()]
    if not pool:
        return None

    pool.sort(key=_size, reverse=True)
    return pool[0]


class InternetArchiveOps:
    """Internet Archive search/download/upload operations."""
    URL = ("archive.org",)
    SUPPORTED_CMDLETS = frozenset({"add-file", "download-file", "search-file"})

    def get_table_type(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        return "internetarchive.folder"

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "email",
                "label": "Archive.org Email (restricted downloads)",
                "default": ""
            },
            {
                "key": "password",
                "label": "Archive.org Password (restricted downloads)",
                "default": "",
                "secret": True
            },
            {
                "key": "access_key",
                "label": "Access Key (for uploads)",
                "default": "",
                "secret": True
            },
            {
                "key": "secret_key",
                "label": "Secret Key (for uploads)",
                "default": "",
                "secret": True
            },
            {
                "key": "collection",
                "label": "Default Collection",
                "default": ""
            },
            {
                "key": "mediatype",
                "label": "Default Mediatype",
                "default": ""
            }
        ]

    TABLE_AUTO_STAGES = {
        "internetarchive": ["download-file"],
        "internetarchive.folder": ["download-file"],
        "internetarchive.format": ["download-file"],
        "internetarchive.formats": ["download-file"],
    }

    def maybe_show_picker(
        self,
        *,
        url: str,
        item: Optional[Any] = None,
        parsed: Dict[str, Any],
        config: Dict[str, Any],
        quiet_mode: bool,
    ) -> Optional[int]:
        """Generic hook for download-file to show a selection table for IA items."""
        try:
            if self._should_delegate_borrow(str(url or "")):
                return None
        except Exception:
            pass
        from SYS.field_access import get_field as sh_get_field
        return maybe_show_formats_table(
            raw_urls=[url] if url else [],
            piped_items=[item] if item else [],
            parsed=parsed,
            config=config,
            quiet_mode=quiet_mode,
            get_field=sh_get_field,
        )

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        if not hasattr(self, "config"):
            self.config = config or {}
        conf = _pick_provider_config(self.config)
        if not hasattr(self, "_access_key"):
            self._access_key = conf.get("access_key")
        if not hasattr(self, "_secret_key"):
            self._secret_key = conf.get("secret_key")
        if not hasattr(self, "_collection"):
            self._collection = conf.get("collection") or conf.get("default_collection")
        if not hasattr(self, "_mediatype"):
            self._mediatype = conf.get("mediatype") or conf.get("default_mediatype")

    @staticmethod
    def _should_delegate_borrow(url: str) -> bool:
        raw = str(url or "").strip()
        if not is_details_url(raw):
            return False
        identifier = extract_identifier(raw)
        if not identifier:
            return False
        access = _archive_item_access(identifier)
        return bool(access.get("lendable")) and str(access.get("mediatype") or "") == "texts"

    def _download_via_openlibrary(self, url: str, output_dir: Path) -> Optional[Dict[str, Any]]:
        try:
            from plugins.archiveorg.openlibrary import OpenLibraryOps
        except Exception as ext:
            log(f"[archive.org] OpenLibrary borrow helper unavailable: {ext}", file=sys.stderr)
            return None

        try:
            result = OpenLibraryOps.download_url(self, url, output_dir)
        except Exception as exc:
            log(f"[archive.org] OpenLibrary borrow helper failed: {exc}", file=sys.stderr)
            return None

        if not isinstance(result, dict):
            return result

        search_result = result.get("search_result")
        metadata: Dict[str, Any] = {}
        title = None
        tags: List[str] = []
        if search_result is not None:
            try:
                title = str(getattr(search_result, "title", "") or "").strip() or None
            except Exception:
                title = None
            try:
                metadata = dict(getattr(search_result, "full_metadata", {}) or {})
            except Exception:
                metadata = {}
            try:
                tags_val = getattr(search_result, "tag", None)
                if isinstance(tags_val, set):
                    tags = [str(t) for t in sorted(tags_val) if t]
                elif isinstance(tags_val, list):
                    tags = [str(t) for t in tags_val if t]
            except Exception:
                tags = []

        normalized: Dict[str, Any] = {"path": result.get("path")}
        if metadata:
            normalized["metadata"] = metadata
            normalized["full_metadata"] = metadata
        if title:
            normalized["title"] = title
        if tags:
            normalized["tags"] = tags
        normalized["media_kind"] = "book"
        normalized["plugin_action"] = "borrow"
        return normalized

    def validate(self) -> bool:
        try:
            _ia()
            return True
        except Exception:
            return False

    def _download_with_archive_auth(
        self,
        *,
        url: str,
        output_dir: Path,
        suggested_filename: Optional[str] = None,
    ) -> Optional[Path]:
        email, password = _pick_archive_credentials(self.config or {})
        if not email or not password:
            return None

        try:
            from plugins.archiveorg.openlibrary import OpenLibraryOps
        except Exception as exc:
            log(f"[archive.org] Archive auth helper unavailable: {exc}", file=sys.stderr)
            return None

        identifier = _extract_identifier_from_any(url)
        session: Optional[requests.Session] = None
        loaned = False
        try:
            session = OpenLibraryOps._archive_login(email, password)

            if identifier:
                try:
                    session.get(
                        f"https://archive.org/details/{identifier}",
                        timeout=30,
                        allow_redirects=True,
                    )
                except Exception:
                    pass
                try:
                    session.get(
                        f"https://archive.org/download/{identifier}",
                        timeout=30,
                        allow_redirects=True,
                    )
                except Exception:
                    pass
                try:
                    session = OpenLibraryOps._archive_loan(session, identifier, verbose=False)
                    loaned = True
                except Exception:
                    loaned = False

            return _download_with_requests_session(
                session=session,
                url=url,
                output_dir=output_dir,
                suggested_filename=suggested_filename,
            )
        except Exception as exc:
            log(f"[archive.org] authenticated download failed: {exc}", file=sys.stderr)
            return None
        finally:
            if session is not None:
                if loaned and identifier:
                    try:
                        OpenLibraryOps._archive_return_loan(session, identifier)
                    except Exception:
                        pass
                try:
                    OpenLibraryOps._archive_logout(session)
                except Exception:
                    pass

    @staticmethod
    def _media_kind_from_mediatype(mediatype: str) -> str:
        mt = str(mediatype or "").strip().lower()
        if mt in {"texts"}:
            return "book"
        if mt in {"audio",
                  "etree"}:
            return "audio"
        if mt in {"movies"}:
            return "video"
        if mt in {"image"}:
            return "image"
        return "file"

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str,
                               Any]] = None,
        **_kwargs: Any,
    ) -> List[SearchResult]:
        ia = _ia()
        search_items = getattr(ia, "search_items", None)
        if not callable(search_items):
            raise Exception("internetarchive.search_items is not available")

        q = str(query or "").strip()
        if not q:
            return []

        # If the user supplied a plain string, default to title search.
        if not _looks_fielded_query(q) and q not in {"*",
                                                     "*.*"}:
            q = f'title:("{q}")'

        fields = [
            "identifier",
            "title",
            "mediatype",
            "creator",
            "date",
            "collection",
        ]

        try:
            search: Any = search_items(q, fields=fields)
        except Exception as exc:
            raise Exception(f"Internet Archive search failed: {exc}")

        out: List[SearchResult] = []
        for row in search:
            if len(out) >= int(limit or 50):
                break

            if not isinstance(row, dict):
                continue

            identifier = str(row.get("identifier") or "").strip()
            if not identifier:
                continue

            title = str(row.get("title") or identifier).strip() or identifier
            mediatype = str(row.get("mediatype") or "").strip()
            creator_raw = row.get("creator")
            if isinstance(creator_raw, list):
                creator = ", ".join(str(x) for x in creator_raw if x)
            else:
                creator = str(creator_raw or "").strip()
            date = str(row.get("date") or "").strip()

            annotations: List[str] = []
            if mediatype:
                annotations.append(mediatype)
            if date:
                annotations.append(date)
            if creator:
                annotations.append(creator)

            detail_parts: List[str] = []
            if creator:
                detail_parts.append(creator)
            if date:
                detail_parts.append(date)

            path = f"https://archive.org/details/{identifier}"

            sr = SearchResult(
                table="internetarchive.folder",
                title=title,
                path=path,
                detail=" · ".join(detail_parts),
                annotations=annotations,
                media_kind=self._media_kind_from_mediatype(mediatype),
                size_bytes=None,
                tag=set(),
                columns=[
                    ("title",
                     title),
                    ("mediatype",
                     mediatype),
                    ("date",
                     date),
                    ("creator",
                     creator),
                ],
                full_metadata=dict(row),
            )
            out.append(sr)

        return out

    def download_url(self, url: str, output_dir: Path) -> Optional[Any]:
        """Download an Internet Archive URL.

        Supports:
        - https://archive.org/details/<identifier>
        - https://archive.org/download/<identifier>/<filename>
        """
        if self._should_delegate_borrow(url):
            delegated = self._download_via_openlibrary(url, output_dir)
            if delegated is not None:
                return delegated

        sr = SearchResult(
            table="internetarchive",
            title=str(url),
            path=str(url),
            full_metadata={}
        )
        return self.download(sr, output_dir)

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        raw_path = str(getattr(result, "path", "") or "").strip()

        if self._should_delegate_borrow(raw_path):
            delegated = self._download_via_openlibrary(raw_path, output_dir)
            if isinstance(delegated, dict):
                delegated_path = delegated.get("path")
                if delegated_path:
                    return Path(str(delegated_path))
            if isinstance(delegated, (str, Path)):
                return Path(str(delegated))

        # Fast path for explicit IA file URLs.
        # This uses the shared direct downloader, which already integrates with
        # pipeline transfer progress bars.
        if is_download_file_url(raw_path):
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            suggested_filename: Optional[str] = None
            try:
                extracted = _extract_download_filename_from_url(raw_path)
                if extracted:
                    suggested_filename = extracted
            except Exception:
                suggested_filename = None

            quiet_mode = False
            pipeline_progress = None
            try:
                if isinstance(self.config, dict):
                    quiet_mode = bool(self.config.get("_quiet_background_output"))
                    pipeline_progress = self.config.get("_pipeline_progress")
            except Exception:
                quiet_mode = False
                pipeline_progress = None

            try:
                direct_result = download_direct_file(
                    raw_path,
                    output_dir,
                    quiet=quiet_mode,
                    suggested_filename=suggested_filename,
                    pipeline_progress=pipeline_progress,
                )
                direct_path = getattr(direct_result, "path", None)
                if direct_path is not None:
                    return Path(str(direct_path))
                if isinstance(direct_result, (str, Path)):
                    return Path(str(direct_result))
                return None
            except Exception as exc:
                log(f"[archive.org] direct file download failed, falling back to IA API: {exc}", file=sys.stderr)
                auth_path = self._download_with_archive_auth(
                    url=raw_path,
                    output_dir=output_dir,
                    suggested_filename=suggested_filename,
                )
                if auth_path is not None:
                    return auth_path

        ia = _ia()
        get_item = getattr(ia, "get_item", None)
        download_fn = getattr(ia, "download", None)
        if not callable(get_item):
            raise Exception("internetarchive.get_item is not available")
        if not callable(download_fn):
            raise Exception("internetarchive.download is not available")

        identifier = _extract_identifier_from_any(
            str(getattr(result,
                        "path",
                        "") or "")
        )
        if not identifier:
            return None

        requested_filename = ""
        try:
            requested_filename = _extract_download_filename_from_url(str(result.path))
        except Exception:
            requested_filename = ""

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        try:
            item: Any = get_item(identifier)
        except Exception as exc:
            raise Exception(f"Internet Archive item lookup failed: {exc}")

        files: List[Dict[str, Any]] = []
        try:
            raw_files = getattr(item, "files", None)
            if isinstance(raw_files, list):
                for f in raw_files:
                    if isinstance(f, dict):
                        files.append(f)
        except Exception:
            files = []

        if not files:
            try:
                for f in item.get_files():
                    name = getattr(f, "name", None)
                    if not name and isinstance(f, dict):
                        name = f.get("name")
                    if not name:
                        continue
                    files.append(
                        {
                            "name": str(name),
                            "size": getattr(f,
                                            "size",
                                            None),
                            "format": getattr(f,
                                              "format",
                                              None),
                            "source": getattr(f,
                                              "source",
                                              None),
                        }
                    )
            except Exception:
                files = []

        chosen_name = ""
        if requested_filename:
            requested_variants: List[str] = []
            req_raw = str(requested_filename or "").strip()
            req_decoded = unquote(req_raw).strip() if req_raw else ""
            for candidate in (req_decoded, req_raw):
                if candidate and candidate not in requested_variants:
                    requested_variants.append(candidate)

            available_names = [
                str(f.get("name") or "").strip()
                for f in files
                if isinstance(f, dict) and str(f.get("name") or "").strip()
            ]
            available_set = {name for name in available_names}

            for candidate in requested_variants:
                if candidate in available_set:
                    chosen_name = candidate
                    break

            if not chosen_name:
                available_lower = {name.lower(): name for name in available_names}
                for candidate in requested_variants:
                    hit = available_lower.get(candidate.lower())
                    if hit:
                        chosen_name = hit
                        break

            if not chosen_name and requested_variants:
                chosen_name = requested_variants[0]
        else:
            chosen = _best_file_candidate(files)
            if chosen is not None:
                chosen_name = str(chosen.get("name") or "").strip()

        if not chosen_name:
            raise Exception("Internet Archive item has no downloadable files")

        # Download the selected file.
        def _download_one(file_name: str) -> None:
            try:
                download_fn(
                    identifier,
                    files=[file_name],
                    destdir=str(output_dir),
                    no_directory=True,
                    ignore_existing=True,
                    verbose=False,
                )
            except TypeError:
                # Older versions may not support some flags.
                download_fn(
                    identifier,
                    files=[file_name],
                    destdir=str(output_dir),
                )

        try:
            _download_one(chosen_name)
        except Exception as exc:
            retry_name = unquote(str(chosen_name or "")).strip()
            if retry_name and retry_name != chosen_name:
                try:
                    _download_one(retry_name)
                    chosen_name = retry_name
                except Exception as retry_exc:
                    raise Exception(f"Internet Archive download failed: {retry_exc}")
            else:
                raise Exception(f"Internet Archive download failed: {exc}")

        # Resolve downloaded path (library behavior varies by version/flags).
        chosen_basename = Path(chosen_name).name if chosen_name else ""
        candidates = [
            output_dir / chosen_name,
            output_dir / chosen_basename,
            output_dir / identifier / chosen_name,
            output_dir / identifier / chosen_basename,
        ]
        for p in candidates:
            try:
                if p.exists():
                    return p
            except Exception:
                continue

        # As a last resort, try to find by basename.
        try:
            for root in (output_dir, output_dir / identifier):
                if root.exists() and root.is_dir():
                    for child in root.iterdir():
                        if child.is_file() and child.name == chosen_name:
                            return child
        except Exception:
            pass

        return None

    def upload(self, file_path: str, **kwargs: Any) -> str:
        """Upload a file to Internet Archive.

        If a piped item includes a tag `ia:<identifier>`, uploads to that identifier.
        Otherwise creates a new identifier derived from the filename/title and hash.

        Returns the item URL.
        """
        ia = _ia()
        upload_fn = getattr(ia, "upload", None)
        if not callable(upload_fn):
            raise Exception("internetarchive.upload is not available")

        p = Path(str(file_path))
        if not p.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        pipe_obj = kwargs.get("pipe_obj")

        title = ""
        file_hash = ""
        tags: List[str] = []
        try:
            if pipe_obj is not None:
                title = str(getattr(pipe_obj, "title", "") or "").strip()
                file_hash = str(getattr(pipe_obj, "hash", "") or "").strip()
                tags_val = getattr(pipe_obj, "tag", None)
                if isinstance(tags_val, list):
                    tags = [str(t) for t in tags_val if t]
        except Exception:
            title = ""
            file_hash = ""
            tags = []

        identifier = ""
        for t in tags:
            low = str(t or "").strip()
            if low.lower().startswith("ia:"):
                identifier = low.split(":", 1)[1].strip()
                break
            if low.lower().startswith("internetarchive:"):
                identifier = low.split(":", 1)[1].strip()
                break

        if not identifier:
            base_title = title or p.stem
            slug = _normalize_identifier(base_title)
            suffix = ""
            if file_hash:
                suffix = str(file_hash)[:10]
            if slug and suffix:
                identifier = f"{slug}-{suffix}"
            elif slug:
                identifier = slug
            elif suffix:
                identifier = f"medeia-{suffix}"
            else:
                identifier = _normalize_identifier(p.stem) or "medeia-upload"

        identifier = _normalize_identifier(identifier)
        if not identifier:
            raise Exception("Could not determine Internet Archive identifier")

        meta: Dict[str,
                   Any] = {}
        if title:
            meta["title"] = title
        else:
            meta["title"] = p.stem

        if isinstance(self._collection, str) and self._collection.strip():
            meta["collection"] = self._collection.strip()
        if isinstance(self._mediatype, str) and self._mediatype.strip():
            meta["mediatype"] = self._mediatype.strip()

        # Build upload options; credentials are optional if the user has internetarchive configured globally.
        upload_kwargs: Dict[str,
                            Any] = {
                                "metadata": meta
                            }
        ak = os.getenv("IA_ACCESS_KEY") or self._access_key
        sk = os.getenv("IA_SECRET_KEY") or self._secret_key
        if isinstance(ak, str) and ak.strip():
            upload_kwargs["access_key"] = ak.strip()
        if isinstance(sk, str) and sk.strip():
            upload_kwargs["secret_key"] = sk.strip()

        # Use a friendly uploaded filename.
        upload_name = sanitize_filename(p.name)
        files = {
            upload_name: str(p)
        }

        try:
            resp: Any = upload_fn(identifier, files=files, **upload_kwargs)
        except TypeError:
            # Older versions may require positional args.
            resp = upload_fn(identifier, files, meta)
        except Exception as exc:
            log(f"[archive.org] Upload error: {exc}", file=sys.stderr)
            raise

        # Drain generator responses to catch failures.
        try:
            if resp is not None:
                for r in resp:
                    if isinstance(r, dict) and r.get("success") is False:
                        raise Exception(str(r.get("error") or r))
        except Exception as exc:
            raise Exception(f"Internet Archive upload failed: {exc}")

        item_url = f"https://archive.org/details/{identifier}"

        try:
            if pipe_obj is not None:
                from PluginCore.backend_registry import BackendRegistry

                BackendRegistry(
                    self.config,
                    suppress_debug=True
                ).try_add_url_for_pipe_object(pipe_obj,
                                              item_url)
        except Exception:
            pass

        return item_url
