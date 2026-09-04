from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type, cast
import html as html_std
import re
import sys
import json
import subprocess

from API.HTTP import HTTPClient
from API.requests_client import get_requests_session
from PluginCore.base import SearchResult
try:
    from plugins.tidal import Tidal
except ImportError:  # pragma: no cover - optional
    Tidal = None
from plugins.tidal.api import (
    build_track_tags,
    extract_artists,
    stringify,
)
try:  # Optional dependency for IMDb scraping
    from imdbinfo.services import search_title  # type: ignore
except ImportError:  # pragma: no cover - optional
    search_title = None  # type: ignore[assignment]

from SYS.logger import log, debug
from SYS.metadata import imdb_tag
from SYS.json_table import normalize_record

try:  # Optional dependency
    import musicbrainzngs  # type: ignore
except ImportError:  # pragma: no cover - optional
    musicbrainzngs = None

try:  # Optional dependency
    import yt_dlp  # type: ignore
except ImportError:  # pragma: no cover - optional
    yt_dlp = None


def _dedup_text_values(values: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values or []:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _filter_default_scraped_tags(tags: List[str]) -> List[str]:
    blocked = {"title", "artist", "source"}
    out: List[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        text = str(tag or "").strip()
        if not text:
            continue
        namespace = text.split(":", 1)[0].strip().lower() if ":" in text else ""
        if namespace in blocked:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


class MetadataPlugin(ABC):
    """Base class for metadata plugins (music, movies, books, etc.)."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}

    @property
    def name(self) -> str:
        class_name = self.__class__.__name__
        if class_name.endswith("MetadataPlugin"):
            return class_name[: -len("MetadataPlugin")].lower()
        if class_name.endswith("Provider"):
            return class_name[: -len("Provider")].lower()
        return class_name.lower()

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Return a list of candidate metadata records."""

    def to_tags(self, item: Dict[str, Any]) -> List[str]:
        """Convert a result item into a list of tags."""
        tags: List[str] = []
        title = item.get("title")
        artist = item.get("artist")
        album = item.get("album")
        year = item.get("year")

        if title:
            tags.append(f"title:{title}")
        if artist:
            tags.append(f"artist:{artist}")
        if album:
            tags.append(f"album:{album}")
        if year:
            tags.append(f"year:{year}")

        tags.append(f"source:{self.name}")
        return tags

    def search_tags(self, query: str, limit: int = 1) -> List[str]:
        """Return tags for the best match from `search(query)`.

        Plugins can override this when tags should be extracted differently from
        the default search->first-item->to_tags flow.
        """

        try:
            items = self.search(query, limit=max(1, int(limit)))
        except Exception:
            return []
        if not items:
            return []
        try:
            return [str(t) for t in self.to_tags(items[0]) if t is not None]
        except Exception:
            return []

    def identifier_query(self, identifiers: Dict[str, Any]) -> Optional[str]:
        """Return plugin-specific identifier query text from parsed identifiers."""

        _ = identifiers
        return None

    def combined_query(
        self,
        *,
        title_hint: Optional[str],
        artist_hint: Optional[str],
    ) -> Optional[str]:
        """Return plugin-specific title+artist query text."""

        _ = title_hint
        _ = artist_hint
        return None

    def extract_url_query(self, result: Any, get_field: Any) -> Optional[str]:
        """Return plugin-specific URL query derived from a piped result."""

        _ = result
        _ = get_field
        return None

    def emits_direct_tags(self) -> bool:
        """True when the plugin should skip selection table and emit tags directly."""

        return False

    def default_subject_scrape_priority(self) -> int:
        """Priority used when `get-tag -scrape` is invoked without an explicit plugin."""

        return 0

    def url_scrape_priority(self, url: str) -> int:
        """Priority for handling a raw URL passed to `get-tag -scrape <url>`."""

        _ = url
        return 0

    def resolve_subject_query(
        self,
        result: Any,
        get_field: Any,
        *,
        backend: Any = None,
        file_hash: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve a plugin-specific query from the current subject/result."""

        _ = backend
        _ = file_hash
        return self.extract_url_query(result, get_field)

    def prefers_store_tag_overwrite(self) -> bool:
        """Whether direct subject scrapes should replace the store tag set."""

        return False

    def filter_tags_for_selection(self, tags: List[str]) -> List[str]:
        """Filter scraped tags before presenting a selectable metadata row."""

        return _filter_default_scraped_tags(tags)

    def filter_tags_for_store_apply(self, tags: List[str]) -> List[str]:
        """Filter scraped tags before applying them to an existing store-backed item."""

        return self.filter_tags_for_selection(tags)

    def scrape_url_payload(self, url: str) -> Optional[Dict[str, Any]]:
        """Return a URL scrape payload for `get-tag -scrape <url>` when supported."""

        items = self.search(url, limit=1)
        if not items:
            return None
        item = items[0] if isinstance(items[0], dict) else {}
        try:
            tags = [str(t) for t in self.to_tags(item) if t is not None]
        except Exception:
            tags = []
        return {
            "title": item.get("title"),
            "tag": _dedup_text_values(tags),
            "formats": [],
            "playlist_items": [],
        }


class ITunesMetadataPlugin(MetadataPlugin):
    """Metadata plugin using the iTunes Search API."""

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        params = {
            "term": query,
            "media": "music",
            "entity": "song",
            "limit": limit
        }
        try:
            resp = get_requests_session().get(
                "https://itunes.apple.com/search",
                params=params,
                timeout=10
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as exc:
            log(f"iTunes search failed: {exc}", file=sys.stderr)
            return []

        items: List[Dict[str, Any]] = []
        for r in results:
            item = {
                "title": r.get("trackName"),
                "artist": r.get("artistName"),
                "album": r.get("collectionName"),
                "year": str(r.get("releaseDate",
                                  ""))[:4],
                "plugin": self.name,
                "raw": r,
            }
            items.append(item)
        debug(f"iTunes returned {len(items)} items for '{query}'")
        return items

    def identifier_query(self, identifiers: Dict[str, Any]) -> Optional[str]:
        return identifiers.get("musicbrainz") or identifiers.get("musicbrainzalbum")

    def combined_query(
        self,
        *,
        title_hint: Optional[str],
        artist_hint: Optional[str],
    ) -> Optional[str]:
        title_text = str(title_hint or "").strip()
        artist_text = str(artist_hint or "").strip()
        if not title_text or not artist_text:
            return None
        return f"{title_text} {artist_text}"


class OpenLibraryMetadataPlugin(MetadataPlugin):
    """Metadata plugin for OpenLibrary book metadata."""

    @property
    def name(self) -> str:  # type: ignore[override]
        return "openlibrary"

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        query_clean = (query or "").strip()
        if not query_clean:
            return []

        try:
            # Prefer ISBN-specific search when the query looks like one
            if query_clean.replace("-",
                                   "").isdigit() and len(query_clean.replace("-",
                                                                             "")) in (
                                                                                 10,
                                                                                 13,
                                                                             ):
                q = f"isbn:{query_clean.replace('-', '')}"
            else:
                q = query_clean

            resp = get_requests_session().get(
                "https://openlibrary.org/search.json",
                params={
                    "q": q,
                    "limit": limit,
                    "fields": (
                        "title,author_name,publisher,first_publish_year,isbn,key,"
                        "oclc_numbers,lccn,edition_key,cover_i"
                    ),
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log(f"OpenLibrary search failed: {exc}", file=sys.stderr)
            return []

        items: List[Dict[str, Any]] = []
        for doc in data.get("docs", [])[:limit]:
            authors = doc.get("author_name") or []
            publisher = ""
            publishers = doc.get("publisher") or []
            if isinstance(publishers, list) and publishers:
                publisher = publishers[0]

            # Prefer 13-digit ISBN when available, otherwise 10-digit
            isbn_list = doc.get("isbn") or []
            isbn_13 = next((i for i in isbn_list if len(str(i)) == 13), None)
            isbn_10 = next((i for i in isbn_list if len(str(i)) == 10), None)

            # Prefer the edition key (OL...M) so `openlibrary:` links to the
            # edition and remains usable by scrape_openlibrary_metadata.
            olid = ""
            edition_keys = doc.get("edition_key") or []
            if edition_keys and str(edition_keys[0]).strip():
                olid = str(edition_keys[0]).strip()
            else:
                key = doc.get("key", "")
                if isinstance(key, str) and key:
                    olid = key.split("/")[-1]

            items.append(
                {
                    "title": doc.get("title") or "",
                    "artist": ", ".join(authors) if authors else "",
                    "album": publisher,
                    "year": str(doc.get("first_publish_year") or ""),
                    "plugin": self.name,
                    "authors": authors,
                    "publisher": publisher,
                    "identifiers": {
                        "isbn_13": isbn_13,
                        "isbn_10": isbn_10,
                        "openlibrary": olid,
                        "oclc": (doc.get("oclc_numbers") or [None])[0],
                        "lccn": (doc.get("lccn") or [None])[0],
                    },
                    "description": None,
                }
            )

        return items

    def to_tags(self, item: Dict[str, Any]) -> List[str]:
        tags: List[str] = []
        title = item.get("title")
        authors = item.get("authors") or []
        publisher = item.get("publisher")
        year = item.get("year")
        description = item.get("description") or ""

        if title:
            tags.append(f"title:{title}")
        for author in authors:
            if author:
                tags.append(f"author:{author}")
        if publisher:
            tags.append(f"publisher:{publisher}")
        if year:
            tags.append(f"year:{year}")
        if description:
            tags.append(f"description:{description[:200]}")

        identifiers = item.get("identifiers") or {}
        for key, value in identifiers.items():
            if value:
                tags.append(f"{key}:{value}")

        tags.append(f"source:{self.name}")
        return tags

    def identifier_query(self, identifiers: Dict[str, Any]) -> Optional[str]:
        return (
            identifiers.get("isbn_13")
            or identifiers.get("isbn_10")
            or identifiers.get("isbn")
            or identifiers.get("openlibrary")
        )


class GoogleBooksMetadataPlugin(MetadataPlugin):
    """Metadata plugin for Google Books volumes API."""

    @property
    def name(self) -> str:  # type: ignore[override]
        return "googlebooks"

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        query_clean = (query or "").strip()
        if not query_clean:
            return []

        # Prefer ISBN queries when possible
        if query_clean.replace("-",
                               "").isdigit() and len(query_clean.replace("-",
                                                                         "")) in (10,
                                                                                  13):
            q = f"isbn:{query_clean.replace('-', '')}"
        else:
            q = query_clean

        try:
            resp = get_requests_session().get(
                "https://www.googleapis.com/books/v1/volumes",
                params={
                    "q": q,
                    "maxResults": limit
                },
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            log(f"Google Books search failed: {exc}", file=sys.stderr)
            return []

        items: List[Dict[str, Any]] = []
        for volume in payload.get("items", [])[:limit]:
            info = volume.get("volumeInfo") or {}
            authors = info.get("authors") or []
            publisher = info.get("publisher", "")
            published_date = info.get("publishedDate", "")
            year = str(published_date)[:4] if published_date else ""

            identifiers_raw = info.get("industryIdentifiers") or []
            identifiers: Dict[str,
                              Optional[str]] = {
                                  "googlebooks": volume.get("id")
                              }
            for ident in identifiers_raw:
                if not isinstance(ident, dict):
                    continue
                ident_type = ident.get("type", "").lower()
                ident_value = ident.get("identifier")
                if not ident_value:
                    continue
                if ident_type == "isbn_13":
                    identifiers.setdefault("isbn_13", ident_value)
                elif ident_type == "isbn_10":
                    identifiers.setdefault("isbn_10", ident_value)
                else:
                    identifiers.setdefault(ident_type, ident_value)

            items.append(
                {
                    "title": info.get("title") or "",
                    "artist": ", ".join(authors) if authors else "",
                    "album": publisher,
                    "year": year,
                    "plugin": self.name,
                    "authors": authors,
                    "publisher": publisher,
                    "identifiers": identifiers,
                    "description": info.get("description",
                                            ""),
                }
            )

        return items

    def to_tags(self, item: Dict[str, Any]) -> List[str]:
        tags: List[str] = []
        title = item.get("title")
        authors = item.get("authors") or []
        publisher = item.get("publisher")
        year = item.get("year")
        description = item.get("description") or ""

        if title:
            tags.append(f"title:{title}")
        for author in authors:
            if author:
                tags.append(f"author:{author}")
        if publisher:
            tags.append(f"publisher:{publisher}")
        if year:
            tags.append(f"year:{year}")
        if description:
            tags.append(f"description:{description[:200]}")

        identifiers = item.get("identifiers") or {}
        for key, value in identifiers.items():
            if value:
                tags.append(f"{key}:{value}")

        tags.append(f"source:{self.name}")
        return tags

    def identifier_query(self, identifiers: Dict[str, Any]) -> Optional[str]:
        return (
            identifiers.get("isbn_13")
            or identifiers.get("isbn_10")
            or identifiers.get("isbn")
            or identifiers.get("openlibrary")
        )


class ISBNsearchMetadataPlugin(MetadataPlugin):
    """Metadata plugin that scrapes isbnsearch.org by ISBN.

    This is a best-effort HTML scrape. It expects the query to be an ISBN.
    """

    @property
    def name(self) -> str:  # type: ignore[override]
        return "isbnsearch"

    @staticmethod
    def _strip_html_to_text(raw: str) -> str:
        s = html_std.unescape(str(raw or ""))
        s = re.sub(r"(?i)<br\s*/?>", "\n", s)
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"\s+", " ", s)
        return s.strip()

    @staticmethod
    def _clean_isbn(query: str) -> str:
        s = str(query or "").strip()
        if not s:
            return ""
        s = s.replace("isbn:", "").replace("ISBN:", "")
        s = re.sub(r"[^0-9Xx]", "", s).upper()
        if len(s) in (10, 13):
            return s
        # Try to locate an ISBN-like token inside the query.
        m = re.search(r"\b(?:97[89])?\d{9}[\dXx]\b", s)
        return str(m.group(0)).upper() if m else ""

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        _ = limit
        isbn = self._clean_isbn(query)
        if not isbn:
            return []

        url = f"https://isbnsearch.org/isbn/{isbn}"
        try:
            resp = get_requests_session().get(url, timeout=10)
            resp.raise_for_status()
            html = str(resp.text or "")
            if not html:
                return []
        except Exception as exc:
            log(f"ISBNsearch scrape failed: {exc}", file=sys.stderr)
            return []

        title = ""
        m_title = re.search(r"(?is)<h1\b[^>]*>(.*?)</h1>", html)
        if m_title:
            title = self._strip_html_to_text(m_title.group(1))

        raw_fields: Dict[str,
                         str] = {}
        strong_matches = list(re.finditer(r"(?is)<strong\b[^>]*>(.*?)</strong>", html))
        for idx, m in enumerate(strong_matches):
            label_raw = self._strip_html_to_text(m.group(1))
            label = str(label_raw or "").strip()
            if not label:
                continue
            if label.endswith(":"):
                label = label[:-1].strip()

            chunk_start = m.end()
            # Stop at next <strong> or end of document.
            chunk_end = (
                strong_matches[idx + 1].start() if
                (idx + 1) < len(strong_matches) else len(html)
            )
            chunk = html[chunk_start:chunk_end]
            # Prefer stopping within the same paragraph when possible.
            m_end = re.search(r"(?is)(</p>|<br\s*/?>)", chunk)
            if m_end:
                chunk = chunk[:m_end.start()]

            val_text = self._strip_html_to_text(chunk)
            if not val_text:
                continue
            raw_fields[label] = val_text

        def _get(*labels: str) -> str:
            for lab in labels:
                for k, v in raw_fields.items():
                    if str(k).strip().lower() == str(lab).strip().lower():
                        return str(v or "").strip()
            return ""

        # Map common ISBNsearch labels.
        author_text = _get("Author", "Authors", "Author(s)")
        publisher = _get("Publisher")
        published = _get("Published", "Publication Date", "Publish Date")
        language = _get("Language")
        pages = _get("Pages")
        isbn_13 = _get("ISBN-13", "ISBN13")
        isbn_10 = _get("ISBN-10", "ISBN10")

        year = ""
        if published:
            m_year = re.search(r"\b(\d{4})\b", published)
            year = str(m_year.group(1)) if m_year else ""

        authors: List[str] = []
        if author_text:
            # Split on common separators; keep multi-part names intact.
            for part in re.split(r"\s*(?:,|;|\band\b|\&|\|)\s*",
                                 author_text,
                                 flags=re.IGNORECASE):
                p = str(part or "").strip()
                if p:
                    authors.append(p)

        # Prefer parsed title, but fall back to og:title if needed.
        if not title:
            m_og = re.search(
                r"(?is)<meta\b[^>]*property=['\"]og:title['\"][^>]*content=['\"](.*?)['\"][^>]*>",
                html,
            )
            if m_og:
                title = self._strip_html_to_text(m_og.group(1))

        # Ensure ISBN tokens are normalized.
        isbn_tokens: List[str] = []
        for token in [isbn_13, isbn_10, isbn]:
            t = self._clean_isbn(token)
            if t and t not in isbn_tokens:
                isbn_tokens.append(t)

        item: Dict[str,
                   Any] = {
                       "title": title or "",
                       # Keep UI columns compatible with the generic metadata table.
                       "artist": ", ".join(authors) if authors else "",
                       "album": publisher or "",
                       "year": year or "",
                       "plugin": self.name,
                       "authors": authors,
                       "publisher": publisher or "",
                       "language": language or "",
                       "pages": pages or "",
                       "identifiers": {
                           "isbn_13":
                           next((t for t in isbn_tokens if len(t) == 13),
                                None),
                           "isbn_10":
                           next((t for t in isbn_tokens if len(t) == 10),
                                None),
                       },
                       "raw_fields": raw_fields,
                   }

        # Only return usable items.
        if not item.get("title") and not any(item["identifiers"].values()):
            return []

        return [item]

    def to_tags(self, item: Dict[str, Any]) -> List[str]:
        tags: List[str] = []

        title = str(item.get("title") or "").strip()
        if title:
            tags.append(f"title:{title}")

        authors = item.get("authors") or []
        if isinstance(authors, list):
            for a in authors:
                a = str(a or "").strip()
                if a:
                    tags.append(f"author:{a}")

        publisher = str(item.get("publisher") or "").strip()
        if publisher:
            tags.append(f"publisher:{publisher}")

        year = str(item.get("year") or "").strip()
        if year:
            tags.append(f"year:{year}")

        language = str(item.get("language") or "").strip()
        if language:
            tags.append(f"language:{language}")

        identifiers = item.get("identifiers") or {}
        if isinstance(identifiers, dict):
            for key in ("isbn_13", "isbn_10"):
                val = identifiers.get(key)
                if val:
                    tags.append(f"isbn:{val}")

        tags.append(f"source:{self.name}")

        # Dedup case-insensitively, preserve order.
        seen: set[str] = set()
        out: List[str] = []
        for t in tags:
            s = str(t or "").strip()
            if not s:
                continue
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
        return out


class MusicBrainzMetadataPlugin(MetadataPlugin):
    """Metadata plugin for MusicBrainz recordings."""

    @property
    def name(self) -> str:  # type: ignore[override]
        return "musicbrainz"

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not musicbrainzngs:
            log(
                "musicbrainzngs is not installed; skipping MusicBrainz scrape",
                file=sys.stderr
            )
            return []

        q = (query or "").strip()
        if not q:
            return []

        try:
            # Ensure user agent is set (required by MusicBrainz)
            musicbrainzngs.set_useragent("Medeia-Macina", "0.1")
        except Exception:
            pass

        try:
            resp = musicbrainzngs.search_recordings(query=q, limit=limit)
            recordings = resp.get("recording-list") or resp.get("recordings") or []
        except Exception as exc:
            log(f"MusicBrainz search failed: {exc}", file=sys.stderr)
            return []

        items: List[Dict[str, Any]] = []
        for rec in recordings[:limit]:
            if not isinstance(rec, dict):
                continue
            title = rec.get("title") or ""

            artist = ""
            artist_credit = rec.get("artist-credit") or rec.get("artist_credit")
            if isinstance(artist_credit, list) and artist_credit:
                first = artist_credit[0]
                if isinstance(first, dict):
                    artist = first.get("name") or first.get("artist",
                                                            {}).get("name",
                                                                    "")
                elif isinstance(first, str):
                    artist = first

            album = ""
            release_list = rec.get("release-list") or rec.get("releases"
                                                              ) or rec.get("release")
            if isinstance(release_list, list) and release_list:
                first_rel = release_list[0]
                if isinstance(first_rel, dict):
                    album = first_rel.get("title", "") or ""
                    release_date = first_rel.get("date") or ""
                else:
                    album = str(first_rel)
                    release_date = ""
            else:
                release_date = rec.get("first-release-date") or ""

            year = str(release_date)[:4] if release_date else ""
            mbid = rec.get("id") or ""

            items.append(
                {
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "year": year,
                    "plugin": self.name,
                    "mbid": mbid,
                    "raw": rec,
                }
            )

        return items

    def to_tags(self, item: Dict[str, Any]) -> List[str]:
        tags = super().to_tags(item)
        mbid = item.get("mbid")
        if mbid:
            tags.append(f"musicbrainz:{mbid}")
        return tags

    def combined_query(
        self,
        *,
        title_hint: Optional[str],
        artist_hint: Optional[str],
    ) -> Optional[str]:
        title_text = str(title_hint or "").strip()
        artist_text = str(artist_hint or "").strip()
        if not title_text or not artist_text:
            return None
        return f'recording:"{title_text}" AND artist:"{artist_text}"'


class ImdbMetadataPlugin(MetadataPlugin):
    """Metadata plugin for IMDb titles (movies/series/episodes)."""

    @property
    def name(self) -> str:  # type: ignore[override]
        return "imdb"

    @staticmethod
    def _extract_imdb_id(text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""

        # Exact tt123 pattern
        m = re.search(r"(tt\d+)", raw, re.IGNORECASE)
        if m:
            imdb_id = m.group(1).lower()
            return imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"

        # Bare numeric IDs (e.g., "0118883")
        if raw.isdigit() and len(raw) >= 6:
            return f"tt{raw}"

        # Last-resort: extract first digit run
        m_digits = re.search(r"(\d{6,})", raw)
        if m_digits:
            return f"tt{m_digits.group(1)}"

        return ""

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []

        imdb_id = self._extract_imdb_id(q)
        if imdb_id:
            try:
                data = imdb_tag(imdb_id)
                raw_tags = data.get("tag") if isinstance(data, dict) else []
                title = None
                year = None
                if isinstance(raw_tags, list):
                    for tag in raw_tags:
                        if not isinstance(tag, str):
                            continue
                        if tag.startswith("title:"):
                            title = tag.split(":", 1)[1]
                        elif tag.startswith("year:"):
                            year = tag.split(":", 1)[1]
                return [
                    {
                        "title": title or imdb_id,
                        "artist": "",
                        "album": "",
                        "year": str(year or ""),
                        "plugin": self.name,
                        "imdb_id": imdb_id,
                        "raw": data,
                    }
                ]
            except Exception as exc:
                log(f"IMDb lookup failed: {exc}", file=sys.stderr)
                return []

        if search_title is None:
            log("imdbinfo is not installed; skipping IMDb scrape", file=sys.stderr)
            return []

        try:
            search_result = search_title(q)
            titles = getattr(search_result, "titles", None) or []
        except Exception as exc:
            log(f"IMDb search failed: {exc}", file=sys.stderr)
            return []

        items: List[Dict[str, Any]] = []
        for entry in titles[:limit]:
            imdb_id = self._extract_imdb_id(
                getattr(entry, "imdb_id", None)
                or getattr(entry, "imdbId", None)
                or getattr(entry, "id", None)
            )
            title = getattr(entry, "title", "") or getattr(entry, "title_localized", "")
            year = str(getattr(entry, "year", "") or "")[:4]
            kind = getattr(entry, "kind", "") or ""
            rating = getattr(entry, "rating", None)
            items.append(
                {
                    "title": title,
                    "artist": "",
                    "album": kind,
                    "year": year,
                    "plugin": self.name,
                    "imdb_id": imdb_id,
                    "kind": kind,
                    "rating": rating,
                    "raw": entry,
                }
            )
        return items

    def to_tags(self, item: Dict[str, Any]) -> List[str]:
        imdb_id = self._extract_imdb_id(
            item.get("imdb_id") or item.get("id") or item.get("imdb") or ""
        )
        try:
            if imdb_id:
                data = imdb_tag(imdb_id)
                raw_tags = data.get("tag") if isinstance(data, dict) else []
                tags = [t for t in raw_tags if isinstance(t, str)]
                if tags:
                    return tags
        except Exception as exc:
            log(f"IMDb tag extraction failed: {exc}", file=sys.stderr)

        tags = super().to_tags(item)
        if imdb_id:
            tags.append(f"imdb:{imdb_id}")
        seen: set[str] = set()
        deduped: List[str] = []
        for t in tags:
            s = str(t or "").strip()
            if not s:
                continue
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            deduped.append(s)
        return deduped

    def identifier_query(self, identifiers: Dict[str, Any]) -> Optional[str]:
        return identifiers.get("imdb")


class YtdlpMetadataPlugin(MetadataPlugin):
    """Metadata plugin that extracts tags from a supported URL using yt-dlp.

    This does NOT download media; it only probes metadata.
    """

    @property
    def name(self) -> str:  # type: ignore[override]
        return "ytdlp"

    def _extract_info(self, url: str) -> Optional[Dict[str, Any]]:
        url = (url or "").strip()
        if not url:
            return None

        # Prefer Python module when available.
        if yt_dlp is not None:
            try:
                opts: Any = {
                    "quiet": True,
                    "no_warnings": True,
                    "skip_download": True,
                    "noprogress": True,
                    "socket_timeout": 15,
                    "retries": 1,
                    "playlist_items": "1-10",
                }
                with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[attr-defined]
                    info = ydl.extract_info(url, download=False)
                return cast(Dict[str, Any], info) if isinstance(info, dict) else None
            except Exception:
                pass

        # Fallback to CLI.
        try:
            cmd = [
                "yt-dlp",
                "-J",
                "--no-warnings",
                "--skip-download",
                "--playlist-items",
                "1-10",
                url,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                return None
            payload = (proc.stdout or "").strip()
            if not payload:
                return None
            data = json.loads(payload)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        url = (query or "").strip()
        if not url.startswith(("http://", "https://")):
            return []

        info = self._extract_info(url)
        if not isinstance(info, dict):
            return []

        upload_date = str(info.get("upload_date") or "")
        release_date = str(info.get("release_date") or "")
        year = (release_date
                or upload_date)[:4] if (release_date or upload_date) else ""

        # Provide basic columns for the standard metadata selection table.
        # NOTE: This is best-effort; many extractors don't provide artist/album.
        artist = info.get("artist") or info.get("uploader") or info.get("channel") or ""
        album = info.get("album") or info.get("playlist_title") or ""
        title = info.get("title") or ""

        return [
            {
                "title": title,
                "artist": str(artist or ""),
                "album": str(album or ""),
                "year": str(year or ""),
                "plugin": self.name,
                "url": url,
                "raw": info,
            }
        ]

    def to_tags(self, item: Dict[str, Any]) -> List[str]:
        raw = item.get("raw")
        if not isinstance(raw, dict):
            return super().to_tags(item)

        tags: List[str] = []
        try:
            from SYS.yt_metadata import extract_ytdlp_tags
        except Exception:
            extract_ytdlp_tags = None  # type: ignore[assignment]

        if extract_ytdlp_tags:
            try:
                tags.extend(extract_ytdlp_tags(raw))
            except Exception:
                pass

        tags.append(f"source:{self.name}")

        # Dedup case-insensitively, preserve order.
        seen = set()
        out: List[str] = []
        for t in tags:
            if not isinstance(t, str):
                continue
            s = t.strip()
            if not s:
                continue
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
        return out

    def extract_url_query(self, result: Any, get_field: Any) -> Optional[str]:
        raw_url = (
            get_field(result, "url", None)
            or get_field(result, "source_url", None)
            or get_field(result, "target", None)
        )
        if isinstance(raw_url, list) and raw_url:
            raw_url = raw_url[0]
        if isinstance(raw_url, str):
            text = raw_url.strip()
            if text.startswith(("http://", "https://")):
                return text
        return None

    def emits_direct_tags(self) -> bool:
        return True

    def default_subject_scrape_priority(self) -> int:
        return 100

    def url_scrape_priority(self, url: str) -> int:
        text = str(url or "").strip()
        if not text.startswith(("http://", "https://")):
            return 0
        return 100

    def prefers_store_tag_overwrite(self) -> bool:
        return True

    def filter_tags_for_store_apply(self, tags: List[str]) -> List[str]:
        return _dedup_text_values(tags)

    def _resolve_candidate_urls_for_subject(
        self,
        result: Any,
        get_field: Any,
        *,
        backend: Any = None,
        file_hash: Optional[str] = None,
    ) -> List[str]:
        try:
            from SYS.metadata import normalize_urls
        except Exception:
            normalize_urls = None  # type: ignore[assignment]

        urls: List[str] = []

        if backend is not None and file_hash:
            try:
                backend_urls = backend.get_url(file_hash, config=self.config)
                if backend_urls:
                    if normalize_urls:
                        urls.extend(normalize_urls(backend_urls))
                    else:
                        urls.extend(
                            [str(u).strip() for u in backend_urls if isinstance(u, str) and str(u).strip()]
                        )
            except Exception:
                pass

            try:
                meta = backend.get_metadata(file_hash, config=self.config)
                if isinstance(meta, dict) and meta.get("url"):
                    raw = meta.get("url")
                    if normalize_urls:
                        urls.extend(normalize_urls(raw))
                    elif isinstance(raw, list):
                        urls.extend([str(u).strip() for u in raw if isinstance(u, str) and str(u).strip()])
                    elif isinstance(raw, str) and raw.strip():
                        urls.append(raw.strip())
            except Exception:
                pass

        for key in ("url", "webpage_url", "source_url", "target"):
            val = get_field(result, key, None)
            if not val:
                continue
            if normalize_urls:
                urls.extend(normalize_urls(val))
                continue
            if isinstance(val, str) and val.strip():
                urls.append(val.strip())
            elif isinstance(val, list):
                urls.extend([str(u).strip() for u in val if isinstance(u, str) and str(u).strip()])

        meta_field = get_field(result, "metadata", None)
        if isinstance(meta_field, dict) and meta_field.get("url"):
            raw = meta_field.get("url")
            if normalize_urls:
                urls.extend(normalize_urls(raw))
            elif isinstance(raw, list):
                urls.extend([str(u).strip() for u in raw if isinstance(u, str) and str(u).strip()])
            elif isinstance(raw, str) and raw.strip():
                urls.append(raw.strip())

        return _dedup_text_values(urls)

    def _pick_supported_subject_url(self, urls: List[str]) -> Optional[str]:
        if not urls:
            return None

        def _is_hydrus_file_url(u: str) -> bool:
            text = str(u or "").strip().lower()
            return bool(text and "/get_files/file" in text and "hash=" in text)

        candidates = []
        for url in urls:
            text = str(url or "").strip()
            if not text.startswith(("http://", "https://")):
                continue
            if _is_hydrus_file_url(text):
                continue
            candidates.append(text)
        if not candidates:
            return None

        try:
            from plugins.ytdlp.tooling import is_url_supported_by_ytdlp

            for text in candidates:
                try:
                    if is_url_supported_by_ytdlp(text):
                        return text
                except Exception:
                    continue
        except Exception:
            pass

        return candidates[0] if candidates else None

    def resolve_subject_query(
        self,
        result: Any,
        get_field: Any,
        *,
        backend: Any = None,
        file_hash: Optional[str] = None,
    ) -> Optional[str]:
        candidate_urls = self._resolve_candidate_urls_for_subject(
            result,
            get_field,
            backend=backend,
            file_hash=file_hash,
        )
        return self._pick_supported_subject_url(candidate_urls)

    @staticmethod
    def _extract_url_formats(formats: Any) -> List[tuple[str, str]]:
        if not isinstance(formats, list):
            return []

        video_formats: Dict[str, Dict[str, Any]] = {}
        audio_formats: Dict[str, Dict[str, Any]] = {}

        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            vcodec = fmt.get("vcodec", "none")
            acodec = fmt.get("acodec", "none")
            height = fmt.get("height")
            ext = fmt.get("ext", "unknown")
            format_id = fmt.get("format_id", "")
            tbr = fmt.get("tbr", 0)
            abr = fmt.get("abr", 0)

            if vcodec and vcodec != "none" and height:
                if int(height) < 480:
                    continue
                res_key = f"{int(height)}p"
                if res_key not in video_formats or tbr > video_formats[res_key].get("tbr", 0):
                    video_formats[res_key] = {
                        "label": f"{int(height)}p ({ext})",
                        "format_id": str(format_id),
                        "tbr": tbr,
                    }
            elif acodec and acodec != "none" and (not vcodec or vcodec == "none"):
                audio_key = f"audio_{abr}"
                if audio_key not in audio_formats or abr > audio_formats[audio_key].get("abr", 0):
                    audio_formats[audio_key] = {
                        "label": f"audio ({ext})",
                        "format_id": str(format_id),
                        "abr": abr,
                    }

        result: List[tuple[str, str]] = []
        for res in sorted(video_formats.keys(), key=lambda value: int(value.replace("p", "")), reverse=True):
            fmt = video_formats[res]
            result.append((str(fmt.get("label") or res), str(fmt.get("format_id") or "")))
        if audio_formats:
            best_audio_key = max(audio_formats.keys(), key=lambda key: float(audio_formats[key].get("abr", 0) or 0))
            fmt = audio_formats[best_audio_key]
            result.append((str(fmt.get("label") or "audio"), str(fmt.get("format_id") or "")))
        return [entry for entry in result if entry[1]]

    @staticmethod
    def _build_playlist_items(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
        entries = raw.get("entries")
        if not isinstance(entries, list):
            return []

        playlist_items: List[Dict[str, Any]] = []
        for idx, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                continue
            playlist_items.append(
                {
                    "index": idx,
                    "id": entry.get("id", f"track_{idx}"),
                    "title": entry.get("title", entry.get("id", f"Track {idx}")),
                    "duration": entry.get("duration", 0),
                    "url": entry.get("url") or entry.get("webpage_url", ""),
                }
            )
        return playlist_items

    def scrape_url_payload(self, url: str) -> Optional[Dict[str, Any]]:
        info = self._extract_info(url)
        if not isinstance(info, dict):
            return None

        item = {
            "title": info.get("title") or "",
            "artist": str(info.get("artist") or info.get("uploader") or info.get("channel") or ""),
            "album": str(info.get("album") or info.get("playlist_title") or ""),
            "year": str((str(info.get("release_date") or "") or str(info.get("upload_date") or ""))[:4]),
            "plugin": self.name,
            "url": str(url or "").strip(),
            "raw": info,
        }
        tags = _dedup_text_values([str(tag) for tag in self.to_tags(item) if tag is not None])
        return {
            "title": item.get("title") or None,
            "tag": tags,
            "formats": self._extract_url_formats(info.get("formats", [])),
            "playlist_items": self._build_playlist_items(info),
        }


def _coerce_archive_field_list(value: Any) -> List[str]:
    """Coerce an Archive.org metadata field to a list of strings."""

    if value is None:
        return []
    if isinstance(value, list):
        out: List[str] = []
        for v in value:
            try:
                s = str(v).strip()
            except Exception:
                continue
            if s:
                out.append(s)
        return out
    if isinstance(value, (tuple, set)):
        out = []
        for v in value:
            try:
                s = str(v).strip()
            except Exception:
                continue
            if s:
                out.append(s)
        return out
    try:
        s = str(value).strip()
    except Exception:
        return []
    return [s] if s else []


def archive_item_metadata_to_tags(archive_id: str,
                                  item_metadata: Dict[str, Any]) -> List[str]:
    """Coerce Archive.org metadata into a stable set of bibliographic tags."""

    archive_id_clean = str(archive_id or "").strip()
    meta = item_metadata if isinstance(item_metadata, dict) else {}

    tags: List[str] = []
    seen: set[str] = set()

    def _add(tag: str) -> None:
        try:
            t = str(tag).strip()
        except Exception:
            return
        if not t:
            return
        if t.lower() in seen:
            return
        seen.add(t.lower())
        tags.append(t)

    if archive_id_clean:
        _add(f"internet_archive:{archive_id_clean}")

    for title in _coerce_archive_field_list(meta.get("title"))[:1]:
        _add(f"title:{title}")

    creators: List[str] = []
    creators.extend(_coerce_archive_field_list(meta.get("creator")))
    creators.extend(_coerce_archive_field_list(meta.get("author")))
    for creator in creators[:3]:
        _add(f"author:{creator}")

    for publisher in _coerce_archive_field_list(meta.get("publisher"))[:3]:
        _add(f"publisher:{publisher}")

    for date_val in _coerce_archive_field_list(meta.get("date"))[:1]:
        _add(f"publish_date:{date_val}")
    for year_val in _coerce_archive_field_list(meta.get("year"))[:1]:
        _add(f"publish_date:{year_val}")

    for lang in _coerce_archive_field_list(meta.get("language"))[:3]:
        _add(f"language:{lang}")

    for subj in _coerce_archive_field_list(meta.get("subject"))[:15]:
        if len(subj) > 200:
            subj = subj[:200]
        _add(subj)

    def _clean_isbn(raw: str) -> str:
        return str(raw or "").replace("-", "").strip()

    for isbn in _coerce_archive_field_list(meta.get("isbn"))[:10]:
        isbn_clean = _clean_isbn(isbn)
        if isbn_clean:
            _add(f"isbn:{isbn_clean}")

    identifiers: List[str] = []
    identifiers.extend(_coerce_archive_field_list(meta.get("identifier")))
    identifiers.extend(_coerce_archive_field_list(meta.get("external-identifier")))
    added_other = 0
    for ident in identifiers:
        ident_s = str(ident or "").strip()
        if not ident_s:
            continue
        low = ident_s.lower()

        if low.startswith("urn:isbn:"):
            val = _clean_isbn(ident_s.split(":", 2)[-1])
            if val:
                _add(f"isbn:{val}")
            continue
        if low.startswith("isbn:"):
            val = _clean_isbn(ident_s.split(":", 1)[-1])
            if val:
                _add(f"isbn:{val}")
            continue
        if low.startswith("urn:oclc:"):
            val = ident_s.split(":", 2)[-1].strip()
            if val:
                _add(f"oclc:{val}")
            continue
        if low.startswith("oclc:"):
            val = ident_s.split(":", 1)[-1].strip()
            if val:
                _add(f"oclc:{val}")
            continue
        if low.startswith("urn:lccn:"):
            val = ident_s.split(":", 2)[-1].strip()
            if val:
                _add(f"lccn:{val}")
            continue
        if low.startswith("lccn:"):
            val = ident_s.split(":", 1)[-1].strip()
            if val:
                _add(f"lccn:{val}")
            continue
        if low.startswith("doi:"):
            val = ident_s.split(":", 1)[-1].strip()
            if val:
                _add(f"doi:{val}")
            continue

        if archive_id_clean and low == archive_id_clean.lower():
            continue
        if added_other >= 5:
            continue
        if len(ident_s) > 200:
            ident_s = ident_s[:200]
        _add(f"identifier:{ident_s}")
        added_other += 1

    return tags


def fetch_archive_item_metadata(archive_id: str,
                                *,
                                timeout: int = 8) -> Dict[str, Any]:
    ident = str(archive_id or "").strip()
    if not ident:
        return {}
    resp = get_requests_session().get(
        f"https://archive.org/metadata/{ident}",
        timeout=int(timeout),
    )
    resp.raise_for_status()
    data = resp.json() if resp is not None else {}
    if not isinstance(data, dict):
        return {}
    meta = data.get("metadata")
    return meta if isinstance(meta, dict) else {}


def scrape_isbn_metadata(isbn: str) -> List[str]:
    """Scrape metadata tags for an ISBN using OpenLibrary's books API."""

    new_tags: List[str] = []

    isbn_clean = str(isbn or "").replace("isbn:", "").replace("-", "").strip()
    if not isbn_clean:
        return []

    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn_clean}&jscmd=data&format=json"
    try:
        with HTTPClient() as client:
            response = client.get(url)
            response.raise_for_status()
            data = json.loads(response.content.decode("utf-8"))
    except Exception as exc:
        log(f"Failed to fetch ISBN metadata: {exc}", file=sys.stderr)
        return []

    if not data:
        log(f"No ISBN metadata found for: {isbn}")
        return []

    book_data = next(iter(data.values()), None)
    if not isinstance(book_data, dict):
        return []

    if "title" in book_data:
        new_tags.append(f"title:{book_data['title']}")

    authors = book_data.get("authors")
    if isinstance(authors, list):
        for author in authors[:3]:
            if isinstance(author, dict) and author.get("name"):
                new_tags.append(f"author:{author['name']}")

    if book_data.get("publish_date"):
        new_tags.append(f"publish_date:{book_data['publish_date']}")

    publishers = book_data.get("publishers")
    if isinstance(publishers, list) and publishers:
        pub = publishers[0]
        if isinstance(pub, dict) and pub.get("name"):
            new_tags.append(f"publisher:{pub['name']}")

    if "description" in book_data:
        desc = book_data.get("description")
        if isinstance(desc, dict) and "value" in desc:
            desc = desc.get("value")
        if desc:
            desc_str = str(desc).strip()
            if desc_str:
                new_tags.append(f"description:{desc_str[:200]}")

    page_count = book_data.get("number_of_pages")
    if isinstance(page_count, int) and page_count > 0:
        new_tags.append(f"pages:{page_count}")

    identifiers = book_data.get("identifiers")
    if isinstance(identifiers, dict):

        def _first(value: Any) -> Any:
            if isinstance(value, list) and value:
                return value[0]
            return value

        for key, ns in (
            ("openlibrary", "openlibrary"),
            ("lccn", "lccn"),
            ("oclc", "oclc"),
            ("goodreads", "goodreads"),
            ("librarything", "librarything"),
            ("doi", "doi"),
            ("internet_archive", "internet_archive"),
        ):
            val = _first(identifiers.get(key))
            if val:
                new_tags.append(f"{ns}:{val}")

    debug(f"Found {len(new_tags)} tag(s) from ISBN lookup")
    return new_tags


def normalize_openlibrary_id(olid: str) -> str:
    text = str(olid or "").strip()
    if not text:
        return ""
    if "/" in text:
        text = text.rstrip("/").split("/")[-1]
    text = text.strip()
    if text.lower().startswith("ol"):
        return "OL" + text[2:].upper()
    if text.isdigit():
        return f"OL{text}"
    return text.upper()


def openlibrary_json_urls(olid: str) -> List[str]:
    key = normalize_openlibrary_id(olid)
    if not key:
        return []
    if key.endswith("W"):
        return [f"https://openlibrary.org/works/{key}.json"]
    if key.endswith("M"):
        return [f"https://openlibrary.org/books/{key}.json"]
    return [
        f"https://openlibrary.org/books/{key}.json",
        f"https://openlibrary.org/works/{key}W.json",
        f"https://openlibrary.org/books/{key}M.json",
    ]


def scrape_openlibrary_metadata(olid: str) -> List[str]:
    """Scrape metadata tags for an OpenLibrary work or edition ID."""

    new_tags: List[str] = []

    olid_text = str(olid or "").strip()
    if not olid_text:
        return []

    olid_norm = normalize_openlibrary_id(olid_text)
    data: Any = None
    last_error: Optional[Exception] = None
    try:
        with HTTPClient() as client:
            for url in openlibrary_json_urls(olid_text):
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    data = json.loads(response.content.decode("utf-8"))
                    if isinstance(data, dict) and data:
                        break
                    data = None
                except Exception as exc:
                    last_error = exc
                    data = None
    except Exception as exc:
        last_error = exc
        data = None
    if not isinstance(data, dict) or not data:
        if last_error is not None:
            log(f"Failed to fetch OpenLibrary metadata: {last_error}", file=sys.stderr)
        return []

    if olid_norm:
        new_tags.append(f"openlibrary:{olid_norm}")

    if "title" in data:
        new_tags.append(f"title:{data['title']}")

    authors = data.get("authors")
    if isinstance(authors, list):
        for author in authors[:3]:
            if isinstance(author, dict) and author.get("name"):
                new_tags.append(f"author:{author['name']}")
                continue

            author_key = None
            if isinstance(author, dict):
                if isinstance(author.get("author"), dict):
                    author_key = author.get("author", {}).get("key")
                if not author_key:
                    author_key = author.get("key")

            if isinstance(author_key, str) and author_key.startswith("/"):
                try:
                    author_url = f"https://openlibrary.org{author_key}.json"
                    with HTTPClient(timeout=10) as client:
                        author_resp = client.get(author_url)
                        author_resp.raise_for_status()
                        author_data = json.loads(author_resp.content.decode("utf-8"))
                    if isinstance(author_data, dict) and author_data.get("name"):
                        new_tags.append(f"author:{author_data['name']}")
                        continue
                except Exception:
                    pass

            if isinstance(author, str) and author:
                new_tags.append(f"author:{author}")

    if data.get("publish_date"):
        new_tags.append(f"publish_date:{data['publish_date']}")

    publishers = data.get("publishers")
    if isinstance(publishers, list) and publishers:
        pub = publishers[0]
        if isinstance(pub, dict) and pub.get("name"):
            new_tags.append(f"publisher:{pub['name']}")
        elif isinstance(pub, str) and pub:
            new_tags.append(f"publisher:{pub}")

    if "description" in data:
        desc = data.get("description")
        if isinstance(desc, dict) and "value" in desc:
            desc = desc.get("value")
        if desc:
            desc_str = str(desc).strip()
            if desc_str:
                new_tags.append(f"description:{desc_str[:200]}")

    page_count = data.get("number_of_pages")
    if isinstance(page_count, int) and page_count > 0:
        new_tags.append(f"pages:{page_count}")

    subjects = data.get("subjects")
    if isinstance(subjects, list):
        for subject in subjects[:10]:
            if isinstance(subject, str):
                subject_clean = subject.strip()
                if subject_clean and subject_clean not in new_tags:
                    new_tags.append(subject_clean)

    identifiers = data.get("identifiers")
    if isinstance(identifiers, dict):

        def _first(value: Any) -> Any:
            if isinstance(value, list) and value:
                return value[0]
            return value

        for key, ns in (
            ("isbn_10", "isbn_10"),
            ("isbn_13", "isbn_13"),
            ("lccn", "lccn"),
            ("oclc_numbers", "oclc"),
            ("goodreads", "goodreads"),
            ("internet_archive", "internet_archive"),
        ):
            val = _first(identifiers.get(key))
            if val:
                new_tags.append(f"{ns}:{val}")

    ocaid = data.get("ocaid")
    if isinstance(ocaid, str) and ocaid.strip():
        new_tags.append(f"internet_archive:{ocaid.strip()}")

    debug(f"Found {len(new_tags)} tag(s) from OpenLibrary lookup")
    return new_tags


SAMPLE_ITEMS: List[Dict[str, Any]] = [
    {
        "title": "Sample OpenLibrary book",
        "path": "https://openlibrary.org/books/OL123M",
        "openlibrary_id": "OL123M",
        "archive_id": "samplearchive123",
        "availability": "borrow",
        "availability_reason": "sample",
        "direct_url": "https://archive.org/download/sample.pdf",
        "author_name": ["OpenLibrary Demo"],
        "first_publish_year": 2023,
        "ia": ["samplearchive123"],
    },
]

# Registry ---------------------------------------------------------------

class TidalMetadataPlugin(MetadataPlugin):
    """Metadata plugin that reuses the Tidal search plugin for tidal info."""

    @property
    def name(self) -> str:  # type: ignore[override]
        return "tidal"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        if Tidal is None:
            raise RuntimeError("Tidal provider unavailable for tidal metadata")
        super().__init__(config)
        self._provider = Tidal(self.config)

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        normalized = str(query or "").strip()
        if not normalized:
            return []

        try:
            results = self._provider.search(normalized, limit=limit)
        except Exception as exc:
            debug(f"[tidal-meta] search failed for '{normalized}': {exc}")
            return []

        items: List[Dict[str, Any]] = []
        for result in results:
            metadata = getattr(result, "full_metadata", {}) or {}
            if not isinstance(metadata, dict):
                metadata = {}

            title = stringify(metadata.get("title") or result.title)
            if not title:
                continue

            artists = extract_artists(metadata)
            artist_display = ", ".join(artists) if artists else stringify(metadata.get("artist"))

            album_obj = metadata.get("album")
            album = ""
            if isinstance(album_obj, dict):
                album = stringify(album_obj.get("title"))
            else:
                album = stringify(metadata.get("album"))

            year = stringify(metadata.get("releaseDate") or metadata.get("year") or metadata.get("date"))

            track_id = self._provider._parse_track_id(metadata.get("trackId") or metadata.get("id"))
            lyrics_data = None
            if track_id is not None:
                try:
                    lyrics_data = self._provider._fetch_track_lyrics(track_id)
                except Exception as exc:
                    debug(f"[tidal-meta] lyrics lookup failed for {track_id}: {exc}")

            lyrics = None
            if isinstance(lyrics_data, dict):
                lyrics = stringify(lyrics_data.get("lyrics") or lyrics_data.get("text"))
                subtitles = stringify(lyrics_data.get("subtitles"))
                if subtitles:
                    metadata.setdefault("_tidal_lyrics", {})["subtitles"] = subtitles

            tags = sorted(build_track_tags(metadata))
            items.append({
                "title": title,
                "artist": artist_display,
                "album": album,
                "year": year,
                "lyrics": lyrics,
                "tags": tags,
                "plugin": self.name,
                "path": getattr(result, "path", ""),
                "track_id": track_id,
                "full_metadata": metadata,
            })
        return items

    def to_tags(self, item: Dict[str, Any]) -> List[str]:
        tags: List[str] = []
        for value in item.get("tags", []):
            value_text = stringify(value)
            if value_text:
                normalized = value_text.lower()
                if normalized in {"tidal", "lossless"}:
                    continue
                if normalized.startswith("quality:lossless"):
                    continue
                tags.append(value_text)
        return tags

_METADATA_PLUGINS: Dict[str,
                        Type[MetadataPlugin]] = {
                              "itunes": ITunesMetadataPlugin,
                              "openlibrary": OpenLibraryMetadataPlugin,
                              "googlebooks": GoogleBooksMetadataPlugin,
                              "google": GoogleBooksMetadataPlugin,
                              "isbnsearch": ISBNsearchMetadataPlugin,
                              "musicbrainz": MusicBrainzMetadataPlugin,
                              "imdb": ImdbMetadataPlugin,
                              "ytdlp": YtdlpMetadataPlugin,
                              "tidal": TidalMetadataPlugin,
                          }


def register_metadata_plugin(name: str, plugin_cls: Type[MetadataPlugin]) -> None:
    _METADATA_PLUGINS[name.lower()] = plugin_cls


def list_metadata_plugins(config: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    availability: Dict[str,
                       bool] = {}
    for name, cls in _METADATA_PLUGINS.items():
        try:
            _ = cls(config)
            # Basic availability check: perform lightweight validation if defined
            availability[name] = True
        except Exception:
            availability[name] = False
    return availability


def get_metadata_plugin(name: str,
                        config: Optional[Dict[str,
                                              Any]] = None
                        ) -> Optional[MetadataPlugin]:
    cls = _METADATA_PLUGINS.get(name.lower())
    if not cls:
        return None
    try:
        return cls(config)
    except Exception as exc:
        log(f"Metadata plugin init failed for '{name}': {exc}", file=sys.stderr)
        return None


def get_default_subject_scrape_plugin(
    config: Optional[Dict[str, Any]] = None,
) -> Optional[MetadataPlugin]:
    best_plugin: Optional[MetadataPlugin] = None
    best_priority = 0
    for cls in _METADATA_PLUGINS.values():
        try:
            plugin = cls(config)
            priority = int(plugin.default_subject_scrape_priority())
        except Exception:
            continue
        if priority > best_priority:
            best_priority = priority
            best_plugin = plugin
    return best_plugin


def get_metadata_plugin_for_url(
    url: str,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[MetadataPlugin]:
    text = str(url or "").strip()
    if not text:
        return None

    best_plugin: Optional[MetadataPlugin] = None
    best_priority = 0
    for cls in _METADATA_PLUGINS.values():
        try:
            plugin = cls(config)
            priority = int(plugin.url_scrape_priority(text))
        except Exception:
            continue
        if priority > best_priority:
            best_priority = priority
            best_plugin = plugin
    return best_plugin
