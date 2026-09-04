from __future__ import annotations

import base64
import io
from concurrent import futures
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from API.HTTP import HTTPClient
from API.requests_client import get_requests_session
from PluginCore.base import SearchResult
from plugins.archiveorg._common import archive_credentials, plugin_config_entry
from SYS.utils import sanitize_filename
from SYS.cli_syntax import get_field, get_free_text, parse_query
from SYS.logger import log
from plugins.metadata_plugin import (
    archive_item_metadata_to_tags,
    fetch_archive_item_metadata,
)
from SYS.utils import unique_path

_DEFAULT_ARCHIVE_SCALE = 4
_DEFAULT_PREFERRED_LANGUAGE = "eng"
_QUALITY_TO_ARCHIVE_SCALE = {
    "high": 2,
    "medium": 5,
    "low": 8,
}
_LANGUAGE_NAME_TO_CODE = {
    "english": "eng",
    "eng": "eng",
    "en": "eng",
    "spanish": "spa",
    "spa": "spa",
    "es": "spa",
    "french": "fre",
    "fre": "fre",
    "fra": "fre",
    "fr": "fre",
    "german": "ger",
    "ger": "ger",
    "deu": "ger",
    "de": "ger",
    "italian": "ita",
    "ita": "ita",
    "it": "ita",
    "portuguese": "por",
    "por": "por",
    "pt": "por",
    "polish": "pol",
    "pol": "pol",
    "pl": "pol",
    "russian": "rus",
    "rus": "rus",
    "ru": "rus",
    "chinese": "chi",
    "chi": "chi",
    "zho": "chi",
    "zh": "chi",
    "japanese": "jpn",
    "jpn": "jpn",
    "ja": "jpn",
}
_LANGUAGE_CODE_TO_NAME = {
    "arm": "Armenian",
    "chi": "Chinese",
    "eng": "English",
    "fre": "French",
    "spa": "Spanish",
    "ger": "German",
    "ice": "Icelandic",
    "ita": "Italian",
    "jpn": "Japanese",
    "kor": "Korean",
    "por": "Portuguese",
    "pol": "Polish",
    "rus": "Russian",
    "swe": "Swedish",
}


def _create_archive_session() -> requests.Session:
    return get_requests_session()

try:
    from Crypto.Cipher import AES  # type: ignore
    from Crypto.Util import Counter  # type: ignore
except ImportError:
    AES = None  # type: ignore
    Counter = None  # type: ignore

try:
    from tqdm import tqdm  # type: ignore
except ImportError:
    tqdm = None  # type: ignore


def _image_paths_to_pdf_bytes(images: List[str]) -> Optional[bytes]:
    if not images:
        return None
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None

    pil_images: List[Any] = []
    try:
        for p in images:
            img_path = Path(p)
            if not img_path.is_file():
                continue
            with Image.open(img_path) as im:  # type: ignore[attr-defined]
                # Ensure PDF-compatible mode.
                if im.mode in {"RGBA",
                               "LA",
                               "P"}:
                    im = im.convert("RGB")
                else:
                    im = im.convert("RGB")
                pil_images.append(im.copy())
    except Exception:
        for im in pil_images:
            try:
                im.close()
            except Exception:
                pass
        return None

    if not pil_images:
        return None

    buf = io.BytesIO()
    first, rest = pil_images[0], pil_images[1:]
    try:
        first.save(buf, format="PDF", save_all=True, append_images=rest)
        return buf.getvalue()
    except Exception:
        return None
    finally:
        for im in pil_images:
            try:
                im.close()
            except Exception:
                pass


def _looks_like_isbn(text: str) -> bool:
    t = (text or "").replace("-", "").strip()
    return t.isdigit() and len(t) in (10, 13)


def _first_str(value: Any) -> Optional[str]:
    if isinstance(value, str):
        v = value.strip()
        return v if v else None
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str):
            v = first.strip()
            return v if v else None
        return str(first) if first is not None else None
    return None


def _resolve_edition_id(doc: Dict[str, Any]) -> str:
    candidate_ids = _resolve_candidate_edition_ids(doc)
    return candidate_ids[0] if candidate_ids else ""


def _resolve_candidate_edition_ids(doc: Dict[str, Any]) -> List[str]:
    out: List[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)

    _add(doc.get("lending_edition_s"))

    edition_key = doc.get("edition_key")
    if isinstance(edition_key, list):
        for value in edition_key:
            _add(value)
    elif isinstance(edition_key, str):
        _add(edition_key)

    _add(doc.get("cover_edition_key"))
    _add(doc.get("openlibrary_id"))

    key = doc.get("key")
    if isinstance(key, str) and key.startswith("/books/"):
        _add(key.split("/books/", 1)[1].strip("/"))

    return out


def _normalize_language_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("/languages/"):
        text = text.rsplit("/", 1)[-1].strip().lower()
    return _LANGUAGE_NAME_TO_CODE.get(text, text)


def _extract_language_codes(value: Any) -> List[str]:
    out: List[str] = []

    def _add(raw: Any) -> None:
        code = _normalize_language_code(raw)
        if code and code not in out:
            out.append(code)

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                _add(item.get("key") or item.get("code") or item.get("name"))
            else:
                _add(item)
    elif isinstance(value, dict):
        _add(value.get("key") or value.get("code") or value.get("name"))
    else:
        _add(value)

    return out


def _language_label(codes: List[str]) -> str:
    labels = [
        _LANGUAGE_CODE_TO_NAME.get(code, str(code or "").upper())
        for code in codes
        if str(code or "").strip()
    ]
    if not labels:
        return "Unknown"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:3])


def _order_language_codes(codes: List[str], preferred_language: str) -> List[str]:
    cleaned: List[str] = []
    for code in codes:
        text = str(code or "").strip().lower()
        if text and text not in cleaned:
            cleaned.append(text)

    preferred = str(preferred_language or "").strip().lower() or _DEFAULT_PREFERRED_LANGUAGE
    indexed_codes = list(enumerate(cleaned))
    indexed_codes.sort(key=lambda item: (0 if item[1] == preferred else 1, item[0]))
    return [code for _, code in indexed_codes]


def _extract_archive_candidates(payload: Any) -> List[str]:
    if not isinstance(payload, dict):
        return []

    out: List[str] = []

    def _add(raw: Any) -> None:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)

    _add(payload.get("ocaid"))
    for key in ("ia", "internet_archive", "archive_id", "ocaids"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                _add(item)
        else:
            _add(value)

    identifiers = payload.get("identifiers")
    if isinstance(identifiers, dict):
        ia_value = identifiers.get("internet_archive")
        if isinstance(ia_value, list):
            for item in ia_value:
                _add(item)
        else:
            _add(ia_value)

    return out


def _check_lendable(session: requests.Session, edition_id: str) -> Tuple[bool, str]:
    """Return (lendable, status_text) using OpenLibrary volumes API."""
    try:
        if not edition_id or not edition_id.startswith("OL") or not edition_id.endswith(
                "M"):
            return False, "not-an-edition"

        url = f"https://openlibrary.org/api/volumes/brief/json/OLID:{edition_id}"
        resp = session.get(url, timeout=6)
        resp.raise_for_status()
        data = resp.json() or {}
        wrapped = data.get(f"OLID:{edition_id}")
        if not isinstance(wrapped, dict):
            return False, "no-availability"

        items = wrapped.get("items")
        if not isinstance(items, list) or not items:
            return False, "no-items"

        first = items[0]
        status_val = ""
        if isinstance(first, dict):
            status_val = str(first.get("status", ""))
        else:
            status_val = str(first)

        return ("lendable" in status_val.lower()), status_val
    except requests.exceptions.Timeout:
        return False, "api-timeout"
    except Exception:
        return False, "api-error"


def _resolve_archive_id(
    session: requests.Session,
    edition_id: str,
    ia_candidates: List[str]
) -> str:
    # Prefer IA identifiers already present in search results.
    if ia_candidates:
        first = ia_candidates[0].strip()
        if first:
            return first

    # Otherwise query the edition JSON.
    try:
        resp = session.get(
            f"https://openlibrary.org/books/{edition_id}.json",
            timeout=6
        )
        resp.raise_for_status()
        data = resp.json() or {}

        ocaid = data.get("ocaid")
        if isinstance(ocaid, str) and ocaid.strip():
            return ocaid.strip()

        identifiers = data.get("identifiers")
        if isinstance(identifiers, dict):
            ia = identifiers.get("internet_archive")
            ia_id = _first_str(ia)
            if ia_id:
                return ia_id

    except Exception:
        pass

    return ""


def _fetch_work_editions(
    session: requests.Session,
    work_key: str,
    *,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    work_path = str(work_key or "").strip()
    if not work_path.startswith("/works/"):
        return []

    try:
        resp = session.get(
            f"https://openlibrary.org{work_path}/editions.json",
            params={"limit": int(limit)},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception:
        return []

    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        edition_id = _resolve_edition_id(entry)
        if not edition_id or edition_id in seen:
            continue
        seen.add(edition_id)
        out.append({
            "edition_id": edition_id,
            "raw": dict(entry),
            "language_codes": _extract_language_codes(entry.get("languages") or entry.get("language")),
            "archive_candidates": _extract_archive_candidates(entry),
        })
    return out


def _fetch_openlibrary_edition_metadata(
    session: requests.Session,
    edition_id: str,
) -> Dict[str, Any]:
    if not edition_id:
        return {}

    try:
        resp = session.get(
            f"https://openlibrary.org/books/{edition_id}.json",
            timeout=6,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    identifiers = data.get("identifiers")
    if not isinstance(identifiers, dict):
        identifiers = {}

    def _first_clean(value: Any) -> str:
        raw = _first_str(value)
        return str(raw or "").strip()

    isbn_10 = _first_clean(identifiers.get("isbn_10"))
    isbn_13 = _first_clean(identifiers.get("isbn_13"))
    archive_id = str(data.get("ocaid") or "").strip()
    if not archive_id:
        archive_id = _first_clean(identifiers.get("internet_archive"))

    out: Dict[str, Any] = {
        "openlibrary_id": str(edition_id).strip(),
        "openlibrary": str(edition_id).strip(),
    }
    language_codes = _extract_language_codes(data.get("languages") or data.get("language"))
    if language_codes:
        out["language_codes"] = language_codes
        out["language_label"] = _language_label(language_codes)
    if isbn_10:
        out["isbn_10"] = isbn_10
    if isbn_13:
        out["isbn_13"] = isbn_13
    if archive_id:
        out["archive_id"] = archive_id
    return out


def _select_preferred_isbns(values: Any) -> Tuple[str, str]:
    items: List[Any]
    if isinstance(values, list):
        items = values
    elif values in (None, ""):
        items = []
    else:
        items = [values]

    isbn_10 = ""
    isbn_13 = ""
    for raw in items:
        token = re.sub(r"[^0-9Xx]", "", str(raw or "")).upper().strip()
        if not token:
            continue
        if len(token) == 13 and not isbn_13:
            isbn_13 = token
        elif len(token) == 10 and not isbn_10:
            isbn_10 = token
    return isbn_10, isbn_13


def _build_pipeline_progress_callback(
    progress: Any,
    title: str,
) -> Callable[[str, int, Optional[int], str], None]:
    transfer_label = str(title or "book").strip() or "book"
    state = {"active": False, "finished": False}

    def _ensure_started(total: Optional[int]) -> None:
        if state["active"]:
            return
        try:
            progress.begin_transfer(label=transfer_label, total=total)
            state["active"] = True
            state["finished"] = False
        except Exception:
            pass

    def _finish() -> None:
        if not state["active"] or state["finished"]:
            return
        try:
            progress.finish_transfer(label=transfer_label)
        except Exception:
            pass
        state["finished"] = True
        state["active"] = False

    def _callback(kind: str, completed: int, total: Optional[int], label: str) -> None:
        text = str(label or kind or "download").strip() or "download"
        try:
            progress.set_status(f"openlibrary: {text}")
        except Exception:
            pass

        if kind == "step":
            if text != "download pages":
                _finish()
            return

        if kind in {"pages", "bytes"}:
            _ensure_started(total)
            try:
                progress.update_transfer(
                    label=transfer_label,
                    completed=int(completed) if completed is not None else None,
                    total=int(total) if total is not None else None,
                )
            except Exception:
                pass
            if total is not None:
                try:
                    if int(completed) >= int(total):
                        _finish()
                except Exception:
                    pass

    setattr(_callback, "_finish_transfer", _finish)
    return _callback


def _archive_id_from_url(url: str) -> str:
    """Best-effort extraction of an Archive.org item identifier from a URL."""

    u = str(url or "").strip()
    if not u:
        return ""

    try:
        p = urlparse(u)
        host = (p.hostname or "").lower().strip()
        if not host.endswith("archive.org"):
            return ""
        parts = [x for x in (p.path or "").split("/") if x]
    except Exception:
        return ""

    # Common patterns:
    # - /details/<id>/...
    # - /borrow/<id>
    # - /download/<id>/...
    # - /stream/<id>/...
    # - /metadata/<id>
    if len(parts) >= 2 and parts[0].lower() in {
        "details",
        "borrow",
        "download",
        "stream",
        "metadata",
    }:
        return str(parts[1]).strip()

    # Sometimes the identifier is the first segment.
    if len(parts) >= 1:
        first = str(parts[0]).strip()
        if first and first.lower() not in {"account",
                                           "services",
                                           "metadata",
                                           "search",
                                           "advancedsearch.php"}:
            return first

    return ""


def edition_id_from_url(u: str) -> str:
    """Extract an OpenLibrary edition id (OL...M) from a book URL."""
    try:
        p = urlparse(str(u))
        parts = [x for x in (p.path or "").split("/") if x]
    except Exception:
        parts = []
    if len(parts) >= 2 and str(parts[0]).lower() == "books":
        return str(parts[1]).strip()
    return ""


def title_hint_from_url_slug(u: str) -> str:
    """Derive a human-friendly title hint from the URL slug."""
    try:
        p = urlparse(str(u))
        parts = [x for x in (p.path or "").split("/") if x]
        slug = parts[-1] if parts else ""
    except Exception:
        slug = ""
    slug = (slug or "").strip().replace("_", " ")
    return slug or "OpenLibrary"


class OpenLibraryOps:

    SUPPORTED_CMDLETS = frozenset({"download-file", "search-file"})
    TABLE_AUTO_STAGES = {
        "openlibrary.edition": ["download-file"],
    }
    QUERY_ARG_CHOICES = {
        "quality": ["high", "medium", "low"],
        "language": [
            "english", "spanish", "french", "german", "italian",
            "portuguese", "polish", "russian", "chinese", "japanese",
        ],
    }

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "email",
                "label": "Archive.org Email",
                "default": "",
                "required": True
            },
            {
                "key": "password",
                "label": "Archive.org Password",
                "default": "",
                "required": True,
                "secret": True
            },
            {
                "key": "quality",
                "label": "Image Quality",
                "default": "medium",
                "choices": ["high", "medium", "low"]
            },
            {
                "key": "preferred_language",
                "label": "Preferred Edition Language",
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
                ]
            }
        ]

    # Domains that should be routed to this provider when the user supplies a URL.
    # (Used by PluginCore.registry.match_provider_name_for_url)
    URL_DOMAINS = (
        "openlibrary.org",
        "archive.org",
    )
    URL = URL_DOMAINS
    """Search provider for OpenLibrary books + Archive.org direct/borrow download."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        if not hasattr(self, "config"):
            self.config = config or {}
        if not getattr(self, "_session", None):
            self._session = _create_archive_session()

    class BookNotAvailableError(Exception):
        """Raised when a book is not available for borrowing (waitlisted/in use)."""

    @staticmethod
    def _preferred_language_from_config(config: Dict[str, Any]) -> str:
        if not isinstance(config, dict):
            return _DEFAULT_PREFERRED_LANGUAGE

        entry = plugin_config_entry(config)
        if not isinstance(entry, dict):
            return _DEFAULT_PREFERRED_LANGUAGE

        value = entry.get("preferred_language") or entry.get("language")
        code = _normalize_language_code(value)
        return code or _DEFAULT_PREFERRED_LANGUAGE

    @staticmethod
    def _edition_language_sort_key(language_codes: List[str], preferred_language: str, ordinal: int) -> Tuple[int, int, int]:
        codes = [str(code or "").strip().lower() for code in language_codes if str(code or "").strip()]
        preferred = str(preferred_language or "").strip().lower() or _DEFAULT_PREFERRED_LANGUAGE
        preferred_rank = 0 if preferred in codes else 1
        unknown_rank = 1 if not codes else 0
        return preferred_rank, unknown_rank, ordinal

    def _build_edition_candidates(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        meta = payload.get("full_metadata") or payload.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}

        raw_doc = meta.get("raw") if isinstance(meta.get("raw"), dict) else {}
        candidate_map: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []

        def _is_edition_raw(raw_entry: Optional[Dict[str, Any]]) -> bool:
            if not isinstance(raw_entry, dict):
                return False
            key = str(raw_entry.get("key") or "").strip()
            return key.startswith("/books/")

        def _upsert(edition_id: str, raw_entry: Optional[Dict[str, Any]] = None) -> None:
            text = str(edition_id or "").strip()
            if not text:
                return
            existing = candidate_map.get(text)
            if existing is None:
                existing = {
                    "edition_id": text,
                    "raw": {},
                    "language_codes": [],
                    "archive_candidates": [],
                    "ordinal": len(order),
                }
                candidate_map[text] = existing
                order.append(text)

            if _is_edition_raw(raw_entry):
                existing_raw = existing.get("raw")
                if not isinstance(existing_raw, dict) or not existing_raw:
                    existing["raw"] = dict(raw_entry)
                language_codes = existing.get("language_codes") or []
                if not language_codes:
                    existing["language_codes"] = _extract_language_codes(raw_entry.get("languages") or raw_entry.get("language"))
                archive_candidates = existing.get("archive_candidates") or []
                if not archive_candidates:
                    existing["archive_candidates"] = _extract_archive_candidates(raw_entry)

        if isinstance(raw_doc, dict):
            for edition_id in _resolve_candidate_edition_ids(raw_doc):
                _upsert(edition_id)
        for edition_id in _resolve_candidate_edition_ids(meta):
            _upsert(edition_id)

        work_key = str(meta.get("openlibrary_key") or "").strip()
        if work_key:
            for entry in _fetch_work_editions(self._session, work_key):
                if not isinstance(entry, dict):
                    continue
                _upsert(
                    str(entry.get("edition_id") or "").strip(),
                    entry.get("raw") if isinstance(entry.get("raw"), dict) else None,
                )
                existing = candidate_map.get(str(entry.get("edition_id") or "").strip())
                if isinstance(existing, dict):
                    if not existing.get("language_codes"):
                        existing["language_codes"] = list(entry.get("language_codes") or [])
                    if not existing.get("archive_candidates"):
                        existing["archive_candidates"] = list(entry.get("archive_candidates") or [])

        preferred_language = self._preferred_language_from_config(self.config)
        candidates = [candidate_map[edition_id] for edition_id in order if edition_id in candidate_map]
        candidates.sort(
            key=lambda item: self._edition_language_sort_key(
                list(item.get("language_codes") or []),
                preferred_language,
                int(item.get("ordinal") or 0),
            )
        )
        return candidates

    def get_table_type(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        filters = filters or {}
        view = str(filters.get("view") or "").strip().lower()
        if view in {"edition", "editions", "borrowable-editions", "borrowable_editions"}:
            return "openlibrary.edition"
        return "openlibrary.work"

    @staticmethod
    def _selection_payload(item: Any) -> Dict[str, Any]:
        if isinstance(item, dict):
            return dict(item)
        try:
            if hasattr(item, "to_dict"):
                payload = item.to_dict()  # type: ignore[attr-defined]
                if isinstance(payload, dict):
                    return payload
        except Exception:
            pass
        try:
            return {
                "table": getattr(item, "table", None),
                "title": getattr(item, "title", None),
                "path": getattr(item, "path", None),
                "detail": getattr(item, "detail", None),
                "annotations": getattr(item, "annotations", None),
                "media_kind": getattr(item, "media_kind", None),
                "full_metadata": getattr(item, "full_metadata", None),
            }
        except Exception:
            return {}

    def _build_borrowable_edition_results(self, payload: Dict[str, Any]) -> List[SearchResult]:
        meta = payload.get("full_metadata") or payload.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}

        raw_doc = meta.get("raw") if isinstance(meta.get("raw"), dict) else {}

        candidates = self._build_edition_candidates(payload)
        if not candidates:
            return []

        parent_title = str(payload.get("title") or meta.get("title") or raw_doc.get("title") or "Unknown").strip() or "Unknown"

        authors_value = meta.get("authors") or raw_doc.get("author_name") or []
        if isinstance(authors_value, str):
            authors_value = [authors_value]
        if not isinstance(authors_value, list):
            authors_value = []
        authors_list = [str(author).strip() for author in authors_value if str(author or "").strip()]

        parent_year = str(meta.get("year") or raw_doc.get("first_publish_year") or "").strip()

        ia_candidates: List[str] = []
        for source in (meta.get("ia"), raw_doc.get("ia")):
            if isinstance(source, str):
                source = [source]
            if isinstance(source, list):
                for value in source:
                    text = str(value or "").strip()
                    if text and text not in ia_candidates:
                        ia_candidates.append(text)

        preferred_language = self._preferred_language_from_config(self.config)

        return self._build_borrowable_edition_results_from_candidates(
            candidates,
            raw_doc=raw_doc,
            meta=meta,
            parent_title=parent_title,
            authors_list=authors_list,
            parent_year=parent_year,
            ia_candidates=ia_candidates,
            preferred_language=preferred_language,
        )

    def _build_borrowable_edition_results_from_candidates(
        self,
        candidates: List[Dict[str, Any]],
        *,
        raw_doc: Dict[str, Any],
        meta: Dict[str, Any],
        parent_title: str,
        authors_list: List[str],
        parent_year: str,
        ia_candidates: List[str],
        preferred_language: str,
    ) -> List[SearchResult]:
        if not candidates:
            return []

        def _build_one(candidate: Dict[str, Any]) -> Optional[SearchResult]:
            return self._build_borrowable_edition_result(
                candidate,
                raw_doc=raw_doc,
                meta=meta,
                parent_title=parent_title,
                authors_list=authors_list,
                parent_year=parent_year,
                ia_candidates=ia_candidates,
                preferred_language=preferred_language,
            )

        results: List[SearchResult] = []
        max_workers = min(12, max(1, len(candidates)))
        with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_id = {
                executor.submit(_build_one, candidate): str(candidate.get("edition_id") or "").strip()
                for candidate in candidates
            }
            resolved: Dict[str, SearchResult] = {}
            for future in futures.as_completed(list(future_to_id.keys())):
                edition_id = future_to_id[future]
                try:
                    built = future.result()
                except Exception:
                    built = None
                if built is not None:
                    resolved[edition_id] = built

        for candidate in candidates:
            edition_id = str(candidate.get("edition_id") or "").strip()
            built = resolved.get(edition_id)
            if built is not None:
                results.append(built)
        return results

    def _build_borrowable_edition_result(
        self,
        candidate: Dict[str, Any],
        *,
        raw_doc: Dict[str, Any],
        meta: Dict[str, Any],
        parent_title: str,
        authors_list: List[str],
        parent_year: str,
        ia_candidates: List[str],
        preferred_language: str,
    ) -> Optional[SearchResult]:
        edition_id = str(candidate.get("edition_id") or "").strip()
        if not edition_id:
            return None
        session_local = _create_archive_session()
        lendable, reason = _check_lendable(session_local, edition_id)
        archive_candidates = list(candidate.get("archive_candidates") or [])
        for fallback_candidate in ia_candidates:
            if fallback_candidate not in archive_candidates:
                archive_candidates.append(fallback_candidate)

        archive_id = _first_str(archive_candidates) or ""
        if lendable and not archive_id:
            archive_id = _resolve_archive_id(session_local, edition_id, ia_candidates)

        if not lendable:
            if not archive_id:
                archive_id = _resolve_archive_id(session_local, edition_id, ia_candidates)
            if not archive_id:
                return None
            lendable2, reason2 = self._archive_is_lendable(archive_id)
            if not lendable2:
                return None
            reason = reason2 or reason

        edition_meta = _fetch_openlibrary_edition_metadata(session_local, edition_id)
        if not archive_id:
            archive_id = str(edition_meta.get("archive_id") or "").strip()
        if not archive_id:
            return None

        isbn_10 = str(edition_meta.get("isbn_10") or meta.get("isbn_10") or "").strip()
        isbn_13 = str(edition_meta.get("isbn_13") or meta.get("isbn_13") or "").strip()
        language_codes = list(edition_meta.get("language_codes") or candidate.get("language_codes") or [])
        language_codes = _order_language_codes(language_codes, preferred_language)
        language_label = _language_label(language_codes)
        book_path = f"https://openlibrary.org/books/{edition_id}"
        selection_url = (
            f"https://archive.org/details/{archive_id}"
            if archive_id else book_path
        )

        annotations: List[str] = ["borrow", f"edition:{edition_id}"]
        if archive_id:
            annotations.append("archive")
        if language_codes:
            annotations.append(f"lang:{language_codes[0]}")
        if isbn_13:
            annotations.append(f"isbn_13:{isbn_13}")
        elif isbn_10:
            annotations.append(f"isbn_10:{isbn_10}")

        edition_metadata = {
            "openlibrary_id": edition_id,
            "openlibrary_key": f"/books/{edition_id}",
            "authors": authors_list,
            "year": parent_year,
            "isbn_10": isbn_10,
            "isbn_13": isbn_13,
            "language_codes": language_codes,
            "language": language_label,
            "ia": [archive_id] if archive_id else [],
            "availability": "borrow",
            "availability_reason": reason,
            "archive_id": archive_id,
            "direct_url": "",
            "selection_view": "edition",
            "selection_url": selection_url,
            "raw": raw_doc,
            "_selection_args": ["-url", selection_url],
            "_selection_action": ["download-file", "-url", selection_url],
        }

        return SearchResult(
            table="openlibrary.edition",
            title=parent_title,
            path=book_path,
            detail=(
                (f"By: {', '.join(authors_list)}" if authors_list else "")
                + (f" ({parent_year})" if parent_year else "")
            ).strip(),
            annotations=annotations,
            media_kind="book",
            columns=[
                ("Title", parent_title),
                ("Author", ", ".join(authors_list)),
                ("Language", language_label),
                ("Year", parent_year),
                ("Avail", "borrow"),
                ("OLID", edition_id),
            ],
            full_metadata=edition_metadata,
        )

    def _build_preferred_borrowable_edition(self, payload: Dict[str, Any]) -> Optional[SearchResult]:
        meta = payload.get("full_metadata") or payload.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}

        raw_doc = meta.get("raw") if isinstance(meta.get("raw"), dict) else {}
        candidates = self._build_edition_candidates(payload)
        if not candidates:
            return None

        parent_title = str(payload.get("title") or meta.get("title") or raw_doc.get("title") or "Unknown").strip() or "Unknown"
        authors_value = meta.get("authors") or raw_doc.get("author_name") or []
        if isinstance(authors_value, str):
            authors_value = [authors_value]
        if not isinstance(authors_value, list):
            authors_value = []
        authors_list = [str(author).strip() for author in authors_value if str(author or "").strip()]
        parent_year = str(meta.get("year") or raw_doc.get("first_publish_year") or "").strip()

        ia_candidates: List[str] = []
        for source in (meta.get("ia"), raw_doc.get("ia")):
            if isinstance(source, str):
                source = [source]
            if isinstance(source, list):
                for value in source:
                    text = str(value or "").strip()
                    if text and text not in ia_candidates:
                        ia_candidates.append(text)

        preferred_language = self._preferred_language_from_config(self.config)
        for candidate in candidates:
            built = self._build_borrowable_edition_result(
                candidate,
                raw_doc=raw_doc,
                meta=meta,
                parent_title=parent_title,
                authors_list=authors_list,
                parent_year=parent_year,
                ia_candidates=ia_candidates,
                preferred_language=preferred_language,
            )
            if built is not None:
                return built
        return None

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
        if normalized_table != "openlibrary.work":
            return None

        for item in selected_items or []:
            payload = self._selection_payload(item)
            meta = payload.get("full_metadata") or payload.get("metadata") or {}
            if not isinstance(meta, dict):
                continue
            if str(meta.get("selection_view") or "").strip().lower() != "work":
                continue
            preferred_edition = self._build_preferred_borrowable_edition(payload)
            if preferred_edition is not None:
                return [preferred_edition]
        return None

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any,
    ) -> bool:
        _ = stage_is_last

        chosen_payload: Optional[Dict[str, Any]] = None
        for item in selected_items or []:
            payload = self._selection_payload(item)
            meta = payload.get("full_metadata") or payload.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            selection_view = str(meta.get("selection_view") or "").strip().lower()
            table_type = str(payload.get("table") or "").strip().lower()
            if selection_view == "edition" or table_type == "openlibrary.edition":
                continue
            if selection_view == "work" or table_type == "openlibrary.work":
                chosen_payload = payload
                break

        if chosen_payload is None:
            return False

        try:
            editions = self._build_borrowable_edition_results(chosen_payload)
        except Exception as exc:
            print(f"openlibrary selector failed: {exc}\n")
            return True

        if not editions:
            print("No borrowable OpenLibrary editions were found for that work.\n")
            return True

        try:
            from SYS.result_table import Table
            from SYS.rich_display import stdout_console
        except Exception:
            return True

        title = str(chosen_payload.get("title") or "OpenLibrary").strip() or "OpenLibrary"
        table = Table(f"OpenLibrary Editions: {title}")._perseverance(True)
        table.set_table("openlibrary.edition")
        try:
            table.set_table_metadata({"plugin": "openlibrary", "view": "borrowable_editions"})
        except Exception:
            pass
        table.set_source_command("search-file", ["-plugin", "archive.org"])

        results_payload: List[Dict[str, Any]] = []
        for edition in editions:
            table.add_result(edition)
            try:
                results_payload.append(edition.to_dict())
            except Exception:
                results_payload.append({
                    "table": getattr(edition, "table", "openlibrary.edition"),
                    "title": getattr(edition, "title", ""),
                    "path": getattr(edition, "path", ""),
                    "full_metadata": getattr(edition, "full_metadata", None),
                })

        try:
            ctx.set_last_result_table(table, results_payload)
            ctx.set_current_stage_table(table)
        except Exception:
            pass

        stdout_console().print()
        stdout_console().print(table)
        return True

    def search_result_from_url(self, url: str) -> Optional[SearchResult]:
        """Build a minimal SearchResult from a bare OpenLibrary/Archive URL."""
        edition_id = edition_id_from_url(url)
        archive_id = _archive_id_from_url(url)
        title_hint = title_hint_from_url_slug(url)
        metadata: Dict[str, Any] = {}
        if edition_id:
            metadata["openlibrary_id"] = edition_id
        if archive_id:
            metadata["archive_id"] = archive_id
        return SearchResult(
            table="openlibrary",
            title=title_hint,
            path=str(url),
            media_kind="book",
            full_metadata=metadata,
        )

    def download_url(
        self,
        url: str,
        output_dir: Path,
        progress_callback: Optional[Callable[[str, int, Optional[int], str], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Download a book directly from an OpenLibrary/Archive URL.

        Returns a dict with the downloaded path and SearchResult when successful.
        """
        sr = self.search_result_from_url(url)
        if sr is None:
            return None

        downloaded = self.download(sr, output_dir, progress_callback)
        if not downloaded:
            return None

        return {
            "path": Path(downloaded),
            "search_result": sr,
        }

    def resolve_pipe_result_download(
        self,
        result: Any,
        pipe_obj: Any,
    ) -> Tuple[Optional[Path], Optional[str], Optional[Path]]:
        download_url = ""
        for source in (
            getattr(pipe_obj, "url", None) if pipe_obj is not None else None,
            getattr(pipe_obj, "source_url", None) if pipe_obj is not None else None,
            getattr(pipe_obj, "metadata", {}).get("selection_url") if pipe_obj is not None and isinstance(getattr(pipe_obj, "metadata", None), dict) else None,
            getattr(pipe_obj, "metadata", {}).get("selection_action", [None, None])[-1] if pipe_obj is not None and isinstance(getattr(pipe_obj, "metadata", None), dict) and isinstance(getattr(pipe_obj, "metadata", {}).get("selection_action"), list) else None,
        ):
            text = str(source or "").strip()
            if text.startswith(("http://", "https://")):
                download_url = text
                break

        if not download_url and isinstance(result, dict):
            for source in (
                result.get("url"),
                result.get("path"),
                result.get("full_metadata", {}).get("selection_url") if isinstance(result.get("full_metadata"), dict) else None,
            ):
                text = str(source or "").strip()
                if text.startswith(("http://", "https://")):
                    download_url = text
                    break

        if not download_url:
            return None, None, None

        progress_callback = None
        if isinstance(self.config, dict):
            pipeline_progress = self.config.get("_pipeline_progress")
            if pipeline_progress is not None:
                label = ""
                for source in (
                    getattr(pipe_obj, "title", None) if pipe_obj is not None else None,
                    result.get("title") if isinstance(result, dict) else None,
                    getattr(pipe_obj, "metadata", {}).get("openlibrary_id") if pipe_obj is not None and isinstance(getattr(pipe_obj, "metadata", None), dict) else None,
                ):
                    text = str(source or "").strip()
                    if text:
                        label = text
                        break
                progress_callback = _build_pipeline_progress_callback(
                    pipeline_progress,
                    label or "openlibrary",
                )

        tmp_dir = Path(tempfile.mkdtemp(prefix="openlibrary-add-file-"))
        try:
            downloaded = self.download_url(
                download_url,
                tmp_dir,
                progress_callback=progress_callback,
            )
        except Exception:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            return None, None, None

        if not isinstance(downloaded, dict):
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            return None, None, None

        downloaded_path = downloaded.get("path")
        if isinstance(downloaded_path, Path) and downloaded_path.exists():
            return downloaded_path, None, tmp_dir

        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        return None, None, None

    @staticmethod
    def _credential_archive(config: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Get Archive.org email/password from the merged plugin config."""
        return archive_credentials(config)

    @classmethod
    def _archive_scale_from_config(cls, config: Dict[str, Any]) -> int:
        """Resolve Archive.org book-reader scale from plugin config."""
        if not isinstance(config, dict):
            return _DEFAULT_ARCHIVE_SCALE

        entry = plugin_config_entry(config)
        if not isinstance(entry, dict):
            return _DEFAULT_ARCHIVE_SCALE

        raw_quality = entry.get("quality")
        if raw_quality is None:
            return _DEFAULT_ARCHIVE_SCALE

        if isinstance(raw_quality, (int, float)):
            val = int(raw_quality)
            return val if val > 0 else _DEFAULT_ARCHIVE_SCALE

        q = str(raw_quality).strip().lower()
        if not q:
            return _DEFAULT_ARCHIVE_SCALE

        mapped = _QUALITY_TO_ARCHIVE_SCALE.get(q)
        if isinstance(mapped, int) and mapped > 0:
            return mapped

        try:
            val = int(q)
            return val if val > 0 else _DEFAULT_ARCHIVE_SCALE
        except Exception:
            return _DEFAULT_ARCHIVE_SCALE

    @staticmethod
    def _archive_error_body(response: requests.Response) -> str:
        try:
            body = response.text or ""
        except Exception:
            return ""
        if len(body) > 2000:
            return body[:1200] + "\n... (truncated) ...\n" + body[-400:]
        return body

    @classmethod
    def _archive_logged_in(cls, session: requests.Session) -> bool:
        try:
            cookies = getattr(session, "cookies", None)
            if cookies is None:
                return False
            return bool(cookies.get("logged-in-user") or cookies.get("logged-in-sig"))
        except Exception:
            return False

    @classmethod
    def _archive_login(cls, email: str, password: str) -> requests.Session:
        """Login to archive.org via CSRF token (OpenLibrary uses the same account)."""
        session = _create_archive_session()
        email_text = str(email or "").strip()
        password_text = str(password or "").strip()
        if not email_text or not password_text:
            raise RuntimeError("Archive.org credentials missing")

        token_resp = session.get("https://archive.org/services/csrf-token", timeout=30)
        try:
            token_json = token_resp.json()
        except Exception as exc:
            raise RuntimeError(
                f"Archive login token parse failed: {exc}\n{cls._archive_error_body(token_resp)}"
            )
        if not isinstance(token_json, dict) or not token_json.get("success"):
            raise RuntimeError(
                f"Archive login token fetch failed\n{cls._archive_error_body(token_resp)}"
            )
        token = (token_json.get("value") or {}).get("token")
        if not token:
            raise RuntimeError("Archive login token missing")

        login_resp = session.post(
            "https://archive.org/services/account/login/",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Csrf-Token": str(token),
            },
            data=json.dumps({
                "username": email_text,
                "password": password_text,
                "t": str(token),
            }),
            timeout=30,
        )
        try:
            login_json = login_resp.json()
        except Exception as exc:
            raise RuntimeError(
                f"Archive login parse failed: {exc}\n{cls._archive_error_body(login_resp)}"
            )

        if login_json.get("success") is False:
            if login_json.get("value") == "bad_login":
                raise RuntimeError("Invalid Archive.org credentials")
            raise RuntimeError(f"Archive login failed: {login_json}")
        if login_json.get("success") or cls._archive_logged_in(session):
            return session
        raise RuntimeError(f"Archive login failed: {login_json}")

    @classmethod
    def _archive_loan(
        cls,
        session: requests.Session,
        book_id: str,
        *,
        verbose: bool = True
    ) -> requests.Session:
        data = {
            "action": "grant_access",
            "identifier": book_id
        }
        session.post(
            "https://archive.org/services/loans/loan/searchInside.php",
            data=data,
            timeout=30
        )
        data["action"] = "browse_book"
        response = session.post(
            "https://archive.org/services/loans/loan/",
            data=data,
            timeout=30
        )

        if response.status_code == 400:
            try:
                err = (response.json() or {}).get("error")
                if (err ==
                        "This book is not available to borrow at this time. Please try again later."
                    ):
                    raise cls.BookNotAvailableError("Book is waitlisted or in use")
                raise RuntimeError(f"Borrow failed: {err or response.text}")
            except cls.BookNotAvailableError:
                raise
            except Exception:
                raise RuntimeError("The book cannot be borrowed")

        data["action"] = "create_token"
        response = session.post(
            "https://archive.org/services/loans/loan/",
            data=data,
            timeout=30
        )
        if "token" in (response.text or ""):
            return session
        raise RuntimeError("Something went wrong when trying to borrow the book")

    @staticmethod
    def _archive_return_loan(session: requests.Session, book_id: str) -> None:
        data = {
            "action": "return_loan",
            "identifier": book_id
        }
        response = session.post(
            "https://archive.org/services/loans/loan/",
            data=data,
            timeout=30
        )
        if response.status_code == 200:
            try:
                if (response.json() or {}).get("success"):
                    return
            except Exception:
                pass
        raise RuntimeError("Something went wrong when trying to return the book")

    @staticmethod
    def _archive_logout(session: requests.Session) -> None:
        """Best-effort logout from archive.org.

        Archive sessions are cookie-based; returning the loan is the critical step.
        Logout is attempted for cleanliness but failures should not abort the workflow.
        """

        if session is None:
            return
        for url in (
                "https://archive.org/account/logout",
                "https://archive.org/account/logout.php",
        ):
            try:
                resp = session.get(url, timeout=15, allow_redirects=True)
                code = int(getattr(resp, "status_code", 0) or 0)
                if code and code < 500:
                    return
            except Exception:
                continue

    @staticmethod
    def _archive_is_lendable(book_id: str) -> tuple[bool, str]:
        """Heuristic lendable check using Archive.org item metadata.

        Some lendable items do not map cleanly to an OpenLibrary edition id.
        In practice, Archive metadata collections often include markers like:
        - inlibrary
        - printdisabled
        """

        ident = str(book_id or "").strip()
        if not ident:
            return False, "no-archive-id"
        try:
            resp = get_requests_session().get(
                f"https://archive.org/metadata/{ident}",
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json() if resp is not None else {}
            meta = data.get("metadata",
                            {}) if isinstance(data,
                                              dict) else {}
            collection = meta.get("collection") if isinstance(meta, dict) else None

            values: List[str] = []
            if isinstance(collection, list):
                values = [str(x).strip().lower() for x in collection if str(x).strip()]
            elif isinstance(collection, str):
                values = [collection.strip().lower()]

            # Treat borrowable as "inlibrary" (and keep "lendinglibrary" as a safe alias).
            # IMPORTANT: do NOT treat "printdisabled" alone as borrowable.
            if any(v in {"inlibrary", "lendinglibrary"} for v in values):
                return True, "archive-collection"
            return False, "archive-not-lendable"
        except Exception:
            return False, "archive-metadata-error"

    @staticmethod
    def _archive_get_book_infos(session: requests.Session,
                                url: str) -> Tuple[str,
                                                   List[str],
                                                   Dict[str,
                                                        Any]]:
        """Extract page links from Archive.org book reader."""
        r = session.get(url, timeout=30).text

        # Matches: "url":"//archive.org/..." (allow whitespace)
        match = re.search(r'"url"\s*:\s*"([^"]+)"', r)
        if not match:
            raise RuntimeError("Failed to extract book info URL from response")

        url_path = match.group(1)
        infos_url = ("https:" + url_path) if url_path.startswith("//") else url_path
        infos_url = infos_url.replace("\\u0026", "&")

        response = session.get(infos_url, timeout=30)
        payload = response.json()
        data = payload["data"]

        title = str(data["brOptions"]["bookTitle"]).strip().replace(" ", "_")
        title = "".join(c for c in title if c not in '<>:"/\\|?*')
        title = title[:150]

        metadata = data.get("metadata") or {}
        links: List[str] = []
        br_data = (data.get("brOptions") or {}).get("data",
                                                    [])
        if isinstance(br_data, list):
            for item in br_data:
                if isinstance(item, list):
                    for page in item:
                        if isinstance(page, dict) and "uri" in page:
                            links.append(page["uri"])
                elif isinstance(item, dict) and "uri" in item:
                    links.append(item["uri"])

        if not links:
            raise RuntimeError("No pages found in book data")
        return title, links, metadata if isinstance(metadata, dict) else {}

    @staticmethod
    def _archive_image_name(pages: int, page: int, directory: str) -> str:
        return f"{directory}/{(len(str(pages)) - len(str(page))) * '0'}{page}.jpg"

    @staticmethod
    def _archive_deobfuscate_image(
        image_data: bytes,
        link: str,
        obf_header: str
    ) -> bytes:
        if not AES or not Counter:
            raise RuntimeError("Crypto library not available")

        try:
            version, counter_b64 = obf_header.split("|")
        except Exception as exc:
            raise ValueError("Invalid X-Obfuscate header format") from exc

        if version != "1":
            raise ValueError("Unsupported obfuscation version: " + version)

        aes_key = re.sub(r"^https?:\/\/.*?\/", "/", link)
        sha1_digest = hashlib.sha1(aes_key.encode("utf-8")).digest()
        key = sha1_digest[:16]

        counter_bytes = base64.b64decode(counter_b64)
        if len(counter_bytes) != 16:
            raise ValueError(
                f"Expected counter to be 16 bytes, got {len(counter_bytes)}"
            )

        prefix = counter_bytes[:8]
        initial_value = int.from_bytes(counter_bytes[8:], byteorder="big")
        ctr = Counter.new(
            64,
            prefix=prefix,
            initial_value=initial_value,
            little_endian=False
        )  # type: ignore
        cipher = AES.new(key, AES.MODE_CTR, counter=ctr)  # type: ignore

        decrypted_part = cipher.decrypt(image_data[:1024])
        return decrypted_part + image_data[1024:]

    @classmethod
    def _archive_download_one_image(
        cls,
        session: requests.Session,
        link: str,
        i: int,
        directory: str,
        book_id: str,
        pages: int,
    ) -> None:
        headers = {
            "Referer": "https://archive.org/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "image",
        }

        while True:
            try:
                response = session.get(link, headers=headers, timeout=30)
                if response.status_code == 403:
                    cls._archive_loan(session, book_id, verbose=False)
                    raise RuntimeError("Borrow again")
                if response.status_code == 200:
                    break
            except Exception:
                time.sleep(1)

        image = cls._archive_image_name(pages, i, directory)
        obf_header = response.headers.get("X-Obfuscate")
        if obf_header:
            image_content = cls._archive_deobfuscate_image(
                response.content,
                link,
                obf_header
            )
        else:
            image_content = response.content

        with open(image, "wb") as f:
            f.write(image_content)

    @classmethod
    def _archive_download(
        cls,
        session: requests.Session,
        n_threads: int,
        directory: str,
        links: List[str],
        scale: int,
        book_id: str,
        progress_callback: Optional[Callable[[int,
                                              int],
                                             None]] = None,
    ) -> List[str]:
        links_scaled = [f"{link}&rotate=0&scale={scale}" for link in links]
        pages = len(links_scaled)

        tasks = []
        with futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
            for i, link in enumerate(links_scaled):
                tasks.append(
                    executor.submit(
                        cls._archive_download_one_image,
                        session=session,
                        link=link,
                        i=i,
                        directory=directory,
                        book_id=book_id,
                        pages=pages,
                    )
                )
            if progress_callback is not None:
                done = 0
                total = len(tasks)
                for fut in futures.as_completed(tasks):
                    try:
                        _ = fut.result()
                    except Exception:
                        pass
                    done += 1
                    try:
                        progress_callback(done, total)
                    except Exception:
                        pass
            elif tqdm:
                for _ in tqdm(futures.as_completed(tasks),
                              total=len(tasks)):  # type: ignore
                    pass
            else:
                for _ in futures.as_completed(tasks):
                    pass

        return [cls._archive_image_name(pages, i, directory) for i in range(pages)]

    @staticmethod
    def _archive_check_direct_download(book_id: str) -> Tuple[bool, str]:
        """Check for a directly downloadable original PDF in Archive.org metadata."""
        try:
            metadata_url = f"https://archive.org/metadata/{book_id}"
            response = get_requests_session().get(
                metadata_url,
                timeout=6,
            )
            response.raise_for_status()
            metadata = response.json()
            files = metadata.get("files") if isinstance(metadata, dict) else None
            if isinstance(files, list):
                for file_info in files:
                    if not isinstance(file_info, dict):
                        continue
                    filename = str(file_info.get("name", ""))
                    if filename.endswith(".pdf") and file_info.get("source"
                                                                   ) == "original":
                        pdf_url = (
                            f"https://archive.org/download/{book_id}/{filename.replace(' ', '%20')}"
                        )
                        check_response = get_requests_session().head(
                            pdf_url,
                            timeout=4,
                            allow_redirects=True,
                        )
                        if check_response.status_code == 200:
                            return True, pdf_url
            return False, ""
        except Exception:
            return False, ""

    @property
    def preserve_order(self) -> bool:
        return True

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str,
                               Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        filters = filters or {}

        parsed = parse_query(query)
        isbn = get_field(parsed, "isbn")
        author = get_field(parsed, "author")
        title = get_field(parsed, "title")
        book = get_field(parsed, "book") or str((filters or {}).get("book") or "").strip()
        free_text = get_free_text(parsed)

        q = (isbn or title or author or book or free_text or query or "").strip()
        if not q:
            return []

        if _looks_like_isbn(q):
            q = f"isbn:{q.replace('-', '')}"

        try:
            resp = self._session.get(
                "https://openlibrary.org/search.json",
                params={
                    "q": q,
                    "limit": int(limit)
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:
            log(f"[archive.org] Search failed: {exc}", file=sys.stderr)
            return []

        results: List[SearchResult] = []
        docs = data.get("docs") or []
        if not isinstance(docs, list):
            return []

        # Availability enrichment can be slow if done sequentially (it may require multiple
        # network calls per row). Do it concurrently to keep the pipeline responsive.
        docs = docs[:int(limit)]

        def _compute_availability(doc_dict: Dict[str,
                                                 Any]) -> Tuple[str,
                                                                str,
                                                                str,
                                                                str,
                                                                str]:
            candidate_edition_ids = _resolve_candidate_edition_ids(doc_dict)
            if not candidate_edition_ids:
                return "no-olid", "", "", "", ""

            ia_val_local = doc_dict.get("ia") or []
            if isinstance(ia_val_local, str):
                ia_val_local = [ia_val_local]
            if not isinstance(ia_val_local, list):
                ia_val_local = []
            ia_ids_local = [str(x) for x in ia_val_local if x]

            session_local = _create_archive_session()

            last_reason = ""
            last_archive_id = ""
            last_edition_id = candidate_edition_ids[0]
            for edition_id_local in candidate_edition_ids[:25]:
                last_edition_id = edition_id_local
                try:
                    archive_id_local = _resolve_archive_id(
                        session_local,
                        edition_id_local,
                        ia_ids_local
                    )
                except Exception:
                    archive_id_local = ""

                if not archive_id_local:
                    continue

                last_archive_id = archive_id_local
                lendable_local, reason_local = _check_lendable(session_local, edition_id_local)
                if lendable_local:
                    return "borrow", reason_local, archive_id_local, "", edition_id_local

                try:
                    lendable2, reason2 = self._archive_is_lendable(archive_id_local)
                    if lendable2:
                        return "borrow", reason2 or reason_local, archive_id_local, "", edition_id_local
                except Exception:
                    pass

                last_reason = reason_local

            if last_archive_id:
                return "unavailable", last_reason, last_archive_id, "", last_edition_id
            return "no-archive", "", "", "", last_edition_id

        availability_rows: List[Tuple[str,
                                      str,
                                      str,
                                      str,
                                      str]] = [
                                          ("unknown",
                                           "",
                                           "",
                                           "",
                                           "") for _ in range(len(docs))
                                      ]
        if docs:
            max_workers = min(8, max(1, len(docs)))
            done = 0
            with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_index = {
                    executor.submit(_compute_availability,
                                    doc_dict): i
                    for i, doc_dict in enumerate(docs) if isinstance(doc_dict, dict)
                }
                for fut in futures.as_completed(list(future_to_index.keys())):
                    i = future_to_index[fut]
                    try:
                        availability_rows[i] = fut.result()
                    except Exception:
                        availability_rows[i] = ("unknown", "", "", "", "")
                    done += 1

        for idx, doc in enumerate(docs):
            if not isinstance(doc, dict):
                continue

            book_title = str(doc.get("title") or "").strip() or "Unknown"

            authors = doc.get("author_name") or []
            if isinstance(authors, str):
                authors = [authors]
            if not isinstance(authors, list):
                authors = []
            authors_list = [str(a) for a in authors if a]

            year_val = doc.get("first_publish_year")
            year = str(year_val) if year_val is not None else ""

            edition_id = _resolve_edition_id(doc)
            work_key = doc.get("key") if isinstance(doc.get("key"), str) else ""

            ia_val = doc.get("ia") or []
            if isinstance(ia_val, str):
                ia_val = [ia_val]
            if not isinstance(ia_val, list):
                ia_val = []
            ia_ids = [str(x) for x in ia_val if x]

            isbn_list = doc.get("isbn") or []
            if isinstance(isbn_list, str):
                isbn_list = [isbn_list]
            if not isinstance(isbn_list, list):
                isbn_list = []

            isbn_13 = next((str(i) for i in isbn_list if len(str(i)) == 13), "")
            isbn_10 = next((str(i) for i in isbn_list if len(str(i)) == 10), "")

            columns = [
                ("Title",
                 book_title),
                ("Author",
                 ", ".join(authors_list)),
                ("Year",
                 year),
                ("Avail",
                 ""),
                ("OLID",
                 edition_id),
            ]

            # Determine availability using the concurrently computed enrichment.
            availability, availability_reason, archive_id, direct_url, preferred_edition_id = ("unknown", "", "", "", "")
            if 0 <= idx < len(availability_rows):
                availability, availability_reason, archive_id, direct_url, preferred_edition_id = availability_rows[idx]

            # UX requirement: OpenLibrary provider should ONLY show borrowable books.
            # Ignore printdisabled-only and non-borrow items.
            if availability != "borrow":
                continue

            candidate_edition_ids = _resolve_candidate_edition_ids(doc)
            if preferred_edition_id and preferred_edition_id not in candidate_edition_ids:
                candidate_edition_ids.insert(0, preferred_edition_id)

            # Patch the display column.
            for column_idx, (name, _val) in enumerate(columns):
                if name == "Avail":
                    columns[column_idx] = ("Avail", availability)
                    break

            annotations: List[str] = []
            if isbn_13:
                annotations.append(f"isbn_13:{isbn_13}")
            elif isbn_10:
                annotations.append(f"isbn_10:{isbn_10}")
            if ia_ids:
                annotations.append("archive")
            if availability in {"download",
                                "borrow"}:
                annotations.append(availability)

            selected_edition_id = preferred_edition_id or edition_id
            book_path = (
                f"https://openlibrary.org/books/{selected_edition_id}" if selected_edition_id else
                (
                    f"https://openlibrary.org{work_key}"
                    if isinstance(work_key, str) and work_key.startswith("/") else
                    "https://openlibrary.org"
                )
            )
            metadata = {
                "openlibrary_id": selected_edition_id,
                "openlibrary_key": work_key,
                "authors": authors_list,
                "year": year,
                "isbn_10": isbn_10,
                "isbn_13": isbn_13,
                "ia": ia_ids,
                "candidate_edition_ids": candidate_edition_ids,
                "availability": availability,
                "availability_reason": availability_reason,
                "archive_id": archive_id,
                "direct_url": direct_url,
                "selection_view": "work",
                "raw": doc,
            }
            if book_path:
                metadata["selection_url"] = book_path

            results.append(
                SearchResult(
                    table="openlibrary.work",
                    title=book_title,
                    path=book_path,
                    detail=(
                        (f"By: {', '.join(authors_list)}" if authors_list else "") +
                        (f" ({year})" if year else "")
                    ).strip(),
                    annotations=annotations,
                    media_kind="book",
                    columns=columns,
                    full_metadata=metadata,
                )
            )

        return results

    def download(
        self,
        result: SearchResult,
        output_dir: Path,
        progress_callback: Optional[Callable[[str,
                                              int,
                                              Optional[int],
                                              str],
                                             None]] = None,
    ) -> Optional[Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        meta = result.full_metadata or {}
        edition_id = str(meta.get("openlibrary_id") or "").strip()
        edition_meta = _fetch_openlibrary_edition_metadata(self._session, edition_id)
        if edition_meta and isinstance(meta, dict):
            for key, value in edition_meta.items():
                if value and not meta.get(key):
                    meta[key] = value
            result.full_metadata = meta

        # Accept direct Archive.org URLs too (details/borrow/download) even when no OL edition id is known.
        archive_id = str(meta.get("archive_id") or "").strip()

        ia_ids = meta.get("ia") or []
        if isinstance(ia_ids, str):
            ia_ids = [ia_ids]
        if not isinstance(ia_ids, list):
            ia_ids = []
        ia_candidates = [str(x) for x in ia_ids if x]

        if not archive_id:
            archive_id = _first_str(ia_candidates) or ""

        if not archive_id and edition_id:
            archive_id = str(edition_meta.get("archive_id") or "").strip()
            if not archive_id:
                archive_id = _resolve_archive_id(self._session, edition_id, ia_candidates)

        if not archive_id:
            # Try to extract identifier from the SearchResult path (URL).
            archive_id = _archive_id_from_url(str(getattr(result, "path", "") or ""))

        if not archive_id:
            log(
                "[archive.org] No archive identifier available; cannot download",
                file=sys.stderr
            )
            return None

        # Best-effort metadata scrape to attach bibliographic tags for downstream cmdlets.
        try:
            archive_meta = fetch_archive_item_metadata(archive_id)
            tags = archive_item_metadata_to_tags(archive_id, archive_meta)
            if edition_id:
                tags.append(f"openlibrary:{edition_id}")
            if tags:
                try:
                    result.tag.update(tags)
                except Exception:
                    # Fallback for callers that pass plain dicts.
                    pass

            isbn_10 = str(meta.get("isbn_10") or edition_meta.get("isbn_10") or "").strip()
            isbn_13 = str(meta.get("isbn_13") or edition_meta.get("isbn_13") or "").strip()
            if not isbn_10 and not isbn_13:
                isbn_10, isbn_13 = _select_preferred_isbns(archive_meta.get("isbn"))

            if isinstance(meta, dict):
                meta["archive_id"] = archive_id
                if archive_meta:
                    meta["archive_metadata"] = archive_meta
                if edition_id:
                    meta.setdefault("openlibrary_id", edition_id)
                    meta.setdefault("openlibrary", edition_id)
                if isbn_10:
                    meta.setdefault("isbn_10", isbn_10)
                if isbn_13:
                    meta.setdefault("isbn_13", isbn_13)
                if not meta.get("isbn"):
                    meta["isbn"] = isbn_13 or isbn_10
                result.full_metadata = meta

            extra_identifier_tags: List[str] = []
            if edition_id:
                extra_identifier_tags.append(f"openlibrary:{edition_id}")
            if isbn_13:
                extra_identifier_tags.append(f"isbn_13:{isbn_13}")
                extra_identifier_tags.append(f"isbn:{isbn_13}")
            elif isbn_10:
                extra_identifier_tags.append(f"isbn_10:{isbn_10}")
                extra_identifier_tags.append(f"isbn:{isbn_10}")
            if extra_identifier_tags:
                try:
                    result.tag.update(extra_identifier_tags)
                except Exception:
                    pass
        except Exception:
            # Never block downloads on metadata fetch.
            pass

        safe_title = sanitize_filename(result.title)
        if not safe_title or "http" in safe_title.lower():
            safe_title = sanitize_filename(archive_id) or "archive"

        internal_progress_finish = None
        if progress_callback is None and isinstance(self.config, dict):
            pipeline_progress = self.config.get("_pipeline_progress")
            if pipeline_progress is not None:
                progress_callback = _build_pipeline_progress_callback(pipeline_progress, safe_title)
                internal_progress_finish = getattr(progress_callback, "_finish_transfer", None)

        # 1) Direct download if available.
        try:
            can_direct, pdf_url = self._archive_check_direct_download(archive_id)
        except Exception:
            can_direct, pdf_url = False, ""

        if can_direct and pdf_url:
            try:
                if progress_callback is not None:
                    progress_callback("step", 0, None, "direct download")
            except Exception:
                pass
            out_path = unique_path(output_dir / f"{safe_title}.pdf")
            try:
                with HTTPClient(timeout=30.0) as client:
                    path = client.download(
                        pdf_url,
                        str(out_path),
                        chunk_size=1024 * 256,
                        progress_callback=(
                            (lambda downloaded, total: progress_callback("bytes", downloaded, total, safe_title))
                            if progress_callback is not None
                            else None
                        ),
                    )
                if path and path.exists():
                    return path
                log("[archive.org] Direct download failed", file=sys.stderr)
                return None
            except Exception:
                log("[archive.org] Direct download failed", file=sys.stderr)
                return None

        # 2) Borrow flow (credentials required).
        try:
            email, password = self._credential_archive(self.config or {})
            if not email or not password:
                log(
                    "[archive.org] Archive credentials missing; cannot borrow. Use .config to set them.",
                    file=sys.stderr
                )
                return None

            lendable = True
            reason = ""
            if edition_id:
                lendable, reason = _check_lendable(self._session, edition_id)
                if not lendable:
                    # OpenLibrary API can be a false-negative; fall back to Archive metadata.
                    lendable2, reason2 = self._archive_is_lendable(archive_id)
                    if lendable2:
                        lendable, reason = True, reason2
            else:
                lendable, reason = self._archive_is_lendable(archive_id)

            if not lendable:
                log(f"[archive.org] Not lendable: {reason}", file=sys.stderr)
                return None

            session = self._archive_login(email, password)
            loaned = False
            try:
                try:
                    if progress_callback is not None:
                        progress_callback("step", 0, None, "login")
                except Exception:
                    pass

                try:
                    session = self._archive_loan(session, archive_id, verbose=False)
                    loaned = True
                except self.BookNotAvailableError:
                    log("[archive.org] Book not available to borrow", file=sys.stderr)
                    return None
                except Exception:
                    log("[archive.org] Borrow failed", file=sys.stderr)
                    return None

                try:
                    if progress_callback is not None:
                        progress_callback("step", 0, None, "borrow")
                except Exception:
                    pass

                urls = [
                    f"https://archive.org/borrow/{archive_id}",
                    f"https://archive.org/details/{archive_id}",
                ]
                title = safe_title
                links: Optional[List[str]] = None
                last_exc: Optional[Exception] = None
                for u in urls:
                    try:
                        title_raw, links, _metadata = self._archive_get_book_infos(session, u)
                        if title_raw:
                            title = sanitize_filename(title_raw)
                        break
                    except Exception as exc:
                        last_exc = exc
                        continue

                if not links:
                    log(
                        f"[archive.org] Failed to extract pages: {last_exc}",
                        file=sys.stderr
                    )
                    return None

                try:
                    if progress_callback is not None:
                        progress_callback("step", 0, None, "download pages")
                except Exception:
                    pass

                temp_dir = tempfile.mkdtemp(prefix=f"{title}_", dir=str(output_dir))
                try:
                    images = self._archive_download(
                        session=session,
                        n_threads=10,
                        directory=temp_dir,
                        links=links,
                        scale=self._archive_scale_from_config(self.config or {}),
                        book_id=archive_id,
                        progress_callback=(
                            (
                                lambda done, total:
                                progress_callback("pages", done, total, "pages")
                            ) if progress_callback is not None else None
                        ),
                    )

                    pdf_bytes = _image_paths_to_pdf_bytes(images)
                    if not pdf_bytes:
                        # Keep images folder for manual conversion.
                        log(
                            "[archive.org] PDF conversion failed; keeping images folder",
                            file=sys.stderr,
                        )
                        return Path(temp_dir)

                    try:
                        if progress_callback is not None:
                            progress_callback("step", 0, None, "stitch pdf")
                    except Exception:
                        pass

                    pdf_path = unique_path(output_dir / f"{title}.pdf")
                    with open(pdf_path, "wb") as f:
                        f.write(pdf_bytes)

                    try:
                        shutil.rmtree(temp_dir)
                    except Exception:
                        pass
                    return pdf_path

                except Exception:
                    try:
                        shutil.rmtree(temp_dir)
                    except Exception:
                        pass
                    raise
            finally:
                # Always return the loan after a successful borrow, even if download/stitch fails.
                if loaned:
                    try:
                        if progress_callback is not None:
                            progress_callback("step", 0, None, "return book")
                    except Exception:
                        pass
                    try:
                        self._archive_return_loan(session, archive_id)
                    except Exception as exc:
                        log(
                            f"[archive.org] Warning: failed to return loan: {exc}",
                            file=sys.stderr
                        )
                try:
                    self._archive_logout(session)
                except Exception:
                    pass

        except Exception as exc:
            log(f"[archive.org] Borrow workflow error: {exc}", file=sys.stderr)
            return None
        finally:
            if callable(internal_progress_finish):
                try:
                    internal_progress_finish()
                except Exception:
                    pass

    def validate(self) -> bool:
        return True
