from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from PluginCore.base import Plugin
from plugins.archiveorg._common import plugin_config_entry
from plugins.archiveorg.ia import (
    InternetArchiveOps,
    extract_identifier,
    is_details_url,
    is_download_file_url,
    list_download_files,
    maybe_show_formats_table,
)
from plugins.archiveorg.openlibrary import (
    OpenLibraryOps,
    _create_archive_session,
    _looks_like_isbn,
)

_OL_SEARCH_NAMES = {
    "openlibrary",
    "ol",
}
_OL_VIEWS = {
    "openlibrary",
    "ol",
    "book",
    "books",
    "edition",
    "editions",
    "borrowable-editions",
    "borrowable_editions",
    "work",
}


class ArchiveOrg(OpenLibraryOps, InternetArchiveOps, Plugin):
    PLUGIN_NAME = "archiveorg"
    PLUGIN_ALIASES = (
        "archive.org",
        "openlibrary",
        "internetarchive",
        "ia",
    )
    URL = (
        "archive.org",
        "openlibrary.org",
    )
    URL_DOMAINS = URL
    SUPPORTED_CMDLETS = frozenset({"add-file", "download-file", "search-file"})
    TABLE_AUTO_STAGES = {
        "openlibrary.edition": ["download-file"],
        "internetarchive": ["download-file"],
        "internetarchive.folder": ["download-file"],
        "internetarchive.format": ["download-file"],
        "internetarchive.formats": ["download-file"],
    }
    QUERY_ARG_CHOICES = {
        "book": (),
        "quality": ["high", "medium", "low"],
        "language": [
            "english",
            "spanish",
            "french",
            "german",
            "italian",
            "portuguese",
            "polish",
            "russian",
            "chinese",
            "japanese",
        ],
    }
    INLINE_QUERY_FIELD_CHOICES = QUERY_ARG_CHOICES

    _QUERY_DELIMITERS_RE = re.compile(r"[;,]")
    _QUERY_SEGMENT_RE = re.compile(r"(?=\b\w+\s*:)")

    @property
    def label(self) -> str:
        return "Archive.org"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        Plugin.__init__(self, config)
        conf = plugin_config_entry(self.config)
        self._session = _create_archive_session()
        self._access_key = conf.get("access_key")
        self._secret_key = conf.get("secret_key")
        self._collection = conf.get("collection") or conf.get("default_collection")
        self._mediatype = conf.get("mediatype") or conf.get("default_mediatype")
        self.requested_name = str(getattr(self, "name", "") or self.PLUGIN_NAME).strip().lower()

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "email",
                "label": "Archive.org Email",
                "default": "",
            },
            {
                "key": "password",
                "label": "Archive.org Password",
                "default": "",
                "secret": True,
            },
            {
                "key": "quality",
                "label": "OpenLibrary Image Quality",
                "default": "medium",
                "choices": ["high", "medium", "low"],
            },
            {
                "key": "preferred_language",
                "label": "OpenLibrary Preferred Edition Language",
                "default": "English",
                "choices": [
                    "English",
                    "Spanish",
                    "French",
                    "German",
                    "Italian",
                    "Portuguese",
                    "Polish",
                    "Russian",
                    "Chinese",
                    "Japanese",
                ],
            },
            {
                "key": "access_key",
                "label": "Access Key (for uploads)",
                "default": "",
                "secret": True,
            },
            {
                "key": "secret_key",
                "label": "Secret Key (for uploads)",
                "default": "",
                "secret": True,
            },
            {
                "key": "collection",
                "label": "Default Collection",
                "default": "",
            },
            {
                "key": "mediatype",
                "label": "Default Mediatype",
                "default": "",
            },
        ]

    def extract_query_arguments(self, query: str) -> Tuple[str, Dict[str, Any]]:
        cleaned = str(query or "").strip()
        if not cleaned:
            return "", {}

        segments: List[str] = []
        for chunk in self._QUERY_DELIMITERS_RE.split(cleaned):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                for sub in self._QUERY_SEGMENT_RE.split(chunk):
                    part = sub.strip()
                    if part:
                        segments.append(part)
            else:
                segments.append(chunk)

        parsed_args: Dict[str, Any] = {}
        free_text: List[str] = []
        for segment in segments:
            sep_index = segment.find(":")
            if sep_index < 0:
                sep_index = segment.find("=")
            if sep_index <= 0:
                free_text.append(segment)
                continue
            key = segment[:sep_index].strip().lower()
            value = segment[sep_index + 1:].strip().strip('"').strip("'")
            if not key or not value:
                free_text.append(segment)
                continue
            if key in self.QUERY_ARG_CHOICES:
                parsed_args[key] = value
            else:
                free_text.append(segment)

        normalized = " ".join(part for part in free_text if part).strip()
        if not normalized:
            book = str(parsed_args.get("book") or "").strip()
            if book:
                normalized = book
        return normalized, parsed_args

    @staticmethod
    def _strip_ol_prefix(query: str) -> str:
        text = str(query or "").strip()
        low = text.lower()
        for prefix in ("openlibrary:", "ol:", "book:"):
            if low.startswith(prefix):
                return text[len(prefix):].strip().strip('"').strip("'")
        return text

    def _requested_plugin_name(self) -> str:
        return str(getattr(self, "requested_name", "") or self.name or "").strip().lower()

    def _use_openlibrary_search(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
    ) -> bool:
        requested = self._requested_plugin_name()
        if requested in _OL_SEARCH_NAMES:
            return True
        filters = filters or {}
        if str(filters.get("book") or "").strip():
            return True
        view = str(filters.get("view") or filters.get("source") or "").strip().lower()
        if view in _OL_VIEWS:
            return True
        q = str(query or "").strip()
        low = q.lower()
        if low.startswith("openlibrary:") or low.startswith("ol:") or low.startswith("book:"):
            return True
        if _looks_like_isbn(q):
            return True
        if "openlibrary.org" in low:
            return True
        return False

    @staticmethod
    def _is_openlibrary_url(url: str) -> bool:
        raw = str(url or "").strip()
        if not raw:
            return False
        try:
            parsed = urlparse(raw)
            host = (parsed.hostname or "").strip().lower()
            path = (parsed.path or "").strip().lower()
        except Exception:
            return False
        if host.startswith("www."):
            host = host[4:]
        if host == "openlibrary.org" or host.endswith(".openlibrary.org"):
            return True
        if host == "archive.org" or host.endswith(".archive.org"):
            return (
                path.startswith("/borrow/")
                or path.startswith("/stream/")
                or path.startswith("/services/loans/")
                or "/services/loans/" in path
            )
        return False

    def get_table_type(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        if self._use_openlibrary_search(query, filters):
            return OpenLibraryOps.get_table_type(self, query, filters)
        return InternetArchiveOps.get_table_type(self, query, filters)

    def validate(self) -> bool:
        return True

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[Any]:
        filters = dict(filters or {})
        raw_query = str(query or "").strip()
        book = str(filters.get("book") or "").strip()
        if book and (not raw_query or raw_query == "*"):
            raw_query = book
        if self._use_openlibrary_search(raw_query, filters):
            if str(filters.get("view") or "").strip().lower() not in _OL_VIEWS:
                filters["view"] = "openlibrary"
            ol_query = book or self._strip_ol_prefix(raw_query)
            return OpenLibraryOps.search(
                self,
                ol_query,
                limit,
                filters,
                **kwargs,
            )
        return InternetArchiveOps.search(self, raw_query, limit, filters, **kwargs)

    def download(
        self,
        result: Any,
        output_dir: Path,
        progress_callback: Optional[Callable[[str, int, Optional[int], str], None]] = None,
    ) -> Optional[Path]:
        table = str(getattr(result, "table", "") or "").strip().lower()
        if table.startswith("openlibrary"):
            return OpenLibraryOps.download(self, result, output_dir, progress_callback)
        path = str(getattr(result, "path", "") or "").strip()
        if self._is_openlibrary_url(path) or self._should_delegate_borrow(path):
            return OpenLibraryOps.download(self, result, output_dir, progress_callback)
        return InternetArchiveOps.download(self, result, output_dir)

    def download_url(
        self,
        url: str,
        output_dir: Path,
        progress_callback: Optional[Callable[[str, int, Optional[int], str], None]] = None,
    ) -> Optional[Any]:
        raw = str(url or "").strip()
        if self._is_openlibrary_url(raw) or self._should_delegate_borrow(raw):
            return OpenLibraryOps.download_url(self, raw, output_dir, progress_callback)
        return InternetArchiveOps.download_url(self, raw, output_dir)


OpenLibrary = ArchiveOrg
InternetArchive = ArchiveOrg

__all__ = [
    "ArchiveOrg",
    "OpenLibrary",
    "InternetArchive",
    "extract_identifier",
    "is_details_url",
    "is_download_file_url",
    "list_download_files",
    "maybe_show_formats_table",
]
