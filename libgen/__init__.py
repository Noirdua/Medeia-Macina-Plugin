from __future__ import annotations

import html as html_std
import logging
import re
import requests

from API.requests_client import get_requests_session
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, unquote

from PluginCore.base import Plugin, SearchResult
from SYS.utils import safe_output_dir, sanitize_filename
from SYS.logger import log, debug, debug_panel
from SYS.models import ProgressBar

# Optional dependency for HTML scraping fallbacks
try:
    from lxml import html as lxml_html
except ImportError:
    lxml_html = None


def _libgen_panel(title: str, rows: List[tuple[str, Any]]) -> None:
    try:
        debug_panel(title, rows)
    except Exception:
        pass


def _strip_html_to_text(raw: str) -> str:
    s = html_std.unescape(str(raw or ""))
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    # Help keep lists readable when they are link-heavy.
    s = re.sub(r"(?i)</a>", ", ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _strip_html_to_lines(raw: str) -> List[str]:
    """Convert a small HTML snippet to a list of meaningful text lines.

    Unlike `_strip_html_to_text`, this preserves `<br>` as line breaks so we can
    parse LibGen ads.php tag blocks that use `<br>` separators.
    """

    s = html_std.unescape(str(raw or ""))
    s = re.sub(r"(?is)<script\b.*?</script>", " ", s)
    s = re.sub(r"(?is)<style\b.*?</style>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n", s)
    s = re.sub(r"(?i)</tr\s*>", "\n", s)
    # Help keep link-heavy lists readable.
    s = re.sub(r"(?i)</a>", ", ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")

    out: List[str] = []
    for line in s.split("\n"):
        t = re.sub(r"\s+", " ", str(line or "")).strip()
        if t:
            out.append(t)
    return out


def _libgen_md5_from_url(url: str) -> str:
    try:
        p = urlparse(str(url or ""))
        q = p.query or ""
    except Exception:
        q = ""
    m = re.search(r"(?:^|[&?])md5=([a-fA-F0-9]{32})(?:&|$)", q)
    return str(m.group(1)).lower() if m else ""


def _libgen_ads_url_for_target(url: str) -> str:
    """Best-effort conversion of any LibGen URL to an ads.php URL (same host).

    If md5 is not present, returns empty string.
    """

    md5 = _libgen_md5_from_url(url)
    if not md5:
        return ""
    try:
        p = urlparse(str(url or ""))
        scheme = p.scheme or "https"
        netloc = p.netloc
        if not netloc:
            return ""
        return f"{scheme}://{netloc}/ads.php?md5={md5}"
    except Exception:
        return ""


def _parse_libgen_ads_tags_html(html: str) -> Dict[str, Any]:
    """Parse tags embedded on LibGen ads.php pages.

    Some mirrors render all metadata as a single `<td>` with `<br>` separators:
        title: ...<br>author(s): ...<br>isbn: ...

    Returns a metadata dict similar to `_parse_libgen_details_html` (subset), plus
    `_raw_fields` with captured keys.
    """

    s = str(html or "")
    td_blocks = re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", s)

    best_lines: List[str] = []
    best_score = 0
    for td in td_blocks:
        lines = _strip_html_to_lines(td)
        if not lines:
            continue
        score = 0
        for ln in lines:
            lo = ln.lower()
            if ":" in ln and any(k in lo for k in (
                    "title",
                    "author",
                    "publisher",
                    "year",
                    "isbn",
                    "language",
                    "series",
                    "tags", )):
                score += 1
        if score > best_score:
            best_score = score
            best_lines = lines

    # Fallback: treat the entire page as a line list.
    if not best_lines:
        best_lines = _strip_html_to_lines(s)

    raw_fields: Dict[str,
                     str] = {}
    pending_key: Optional[str] = None

    def _norm_key(k: str) -> str:
        kk = str(k or "").strip().lower()
        kk = re.sub(r"\s+", " ", kk)
        if kk in {"authors",
                  "author(s)",
                  "author(s).",
                  "author(s):"}:
            return "author"
        if kk in {"tag",
                  "tags"}:
            return "tags"
        return kk

    for ln in best_lines:
        line = str(ln or "").strip()
        if not line:
            continue

        if ":" in line:
            k, v = line.split(":", 1)
            k = _norm_key(k)
            v = str(v or "").strip()
            if v:
                raw_fields[k] = v
                pending_key = None
            else:
                pending_key = k
            continue

        # Continuation line: if the previous key had no inline value, use this.
        if pending_key:
            raw_fields[pending_key] = line
            pending_key = None

    out: Dict[str,
              Any] = {
                  "_raw_fields": dict(raw_fields)
              }

    title = str(raw_fields.get("title") or "").strip()
    if title:
        out["title"] = title

    publisher = str(raw_fields.get("publisher") or "").strip()
    if publisher:
        out["publisher"] = publisher

    year = str(raw_fields.get("year") or "").strip()
    if year:
        out["year"] = year

    language = str(raw_fields.get("language") or "").strip()
    if language:
        out["language"] = language

    authors_raw = str(raw_fields.get("author") or "").strip()
    if authors_raw:
        out["authors"] = _split_listish_text(authors_raw)

    # ISBN: extract all tokens (some pages include multiple).
    isbn_raw = str(raw_fields.get("isbn") or "").strip()
    if isbn_raw:
        isbns = _extract_isbns(isbn_raw)
        if isbns:
            out["isbn"] = isbns

    tags_raw = str(raw_fields.get("tags") or "").strip()
    if tags_raw:
        # Keep these as freeform tags (split on commas/semicolons/pipes).
        out["tags"] = _split_listish_text(tags_raw)

    return out


def _extract_anchor_texts(raw_html: str) -> List[str]:
    out: List[str] = []
    for m in re.finditer(r"(?is)<a\b[^>]*>(.*?)</a>", str(raw_html or "")):
        t = _strip_html_to_text(m.group(1))
        if t:
            out.append(t)
    # De-dupe, preserve order
    seen: set[str] = set()
    uniq: List[str] = []
    for x in out:
        k = x.strip()
        if not k:
            continue
        if k.lower() in seen:
            continue
        seen.add(k.lower())
        uniq.append(k)
    return uniq


def _split_listish_text(value: str) -> List[str]:
    s = str(value or "").strip()
    if not s:
        return []
    parts = re.split(r"\s*(?:,|;|\|)\s*", s)
    out: List[str] = []
    for p in parts:
        p = str(p or "").strip()
        if p:
            out.append(p)
    return out


def _extract_isbns(text: str) -> List[str]:
    s = str(text or "")
    candidates = re.findall(r"\b[0-9Xx][0-9Xx\-\s]{8,20}[0-9Xx]\b", s)
    out: List[str] = []
    for c in candidates:
        n = re.sub(r"[^0-9Xx]", "", c).upper()
        if len(n) not in (10, 13):
            continue
        if n not in out:
            out.append(n)
    # Also handle already-clean tokens.
    for c in re.findall(r"\b(?:97[89])?\d{9}[\dXx]\b", s):
        n = str(c).upper()
        if n not in out:
            out.append(n)
    return out


def _libgen_id_from_url(url: str) -> str:
    # Handles edition.php?id=..., file.php?id=...
    m = re.search(r"(?:\?|&)id=(\d+)", str(url or ""), flags=re.IGNORECASE)
    return str(m.group(1)) if m else ""


def _prefer_isbn(isbns: List[str]) -> str:
    vals = [str(x or "").strip() for x in (isbns or []) if str(x or "").strip()]
    # Prefer ISBN-13, then ISBN-10.
    for v in vals:
        if len(v) == 13:
            return v
    for v in vals:
        if len(v) == 10:
            return v
    return vals[0] if vals else ""


def _enrich_book_tags_from_isbn(isbn: str,
                                *,
                                config: Optional[Dict[str,
                                                      Any]] = None) -> Tuple[List[str],
                                                                             str]:
    """Return (tags, source_name) for the given ISBN.

    Priority:
    1) OpenLibrary API-by-ISBN scrape (fast, structured)
    2) isbnsearch.org scrape via metadata plugin
    """

    isbn_clean = re.sub(r"[^0-9Xx]", "", str(isbn or "")).upper()
    if len(isbn_clean) not in (10, 13):
        return [], ""

    # 1) OpenLibrary API lookup by ISBN (short timeout, silent failure).
    try:
        url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn_clean}&jscmd=data&format=json"
        resp = get_requests_session().get(url, timeout=4)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data:
            book_data = next(iter(data.values()), None)
        else:
            book_data = None

        if isinstance(book_data, dict):
            tags: List[str] = []

            def _add(t: str) -> None:
                s = str(t or "").strip()
                if s:
                    tags.append(s)

            if book_data.get("title"):
                _add(f"title:{book_data['title']}")

            authors = book_data.get("authors")
            if isinstance(authors, list):
                for a in authors[:3]:
                    if isinstance(a, dict) and a.get("name"):
                        _add(f"author:{a['name']}")

            if book_data.get("publish_date"):
                _add(f"publish_date:{book_data['publish_date']}")

            publishers = book_data.get("publishers")
            if isinstance(publishers, list) and publishers:
                pub0 = publishers[0]
                if isinstance(pub0, dict) and pub0.get("name"):
                    _add(f"publisher:{pub0['name']}")

            desc = book_data.get("description")
            if isinstance(desc, dict) and "value" in desc:
                desc = desc.get("value")
            if desc:
                desc_str = str(desc).strip()
                if desc_str:
                    _add(f"description:{desc_str[:200]}")

            pages = book_data.get("number_of_pages")
            if isinstance(pages, int) and pages > 0:
                _add(f"pages:{pages}")

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
                        _add(f"{ns}:{val}")

            if not any(str(t).lower().startswith("isbn:") for t in tags):
                tags.insert(0, f"isbn:{isbn_clean}")

            # De-dupe case-insensitively, preserve order.
            seen: set[str] = set()
            out: List[str] = []
            for t in tags:
                k = str(t).strip().lower()
                if not k or k in seen:
                    continue
                seen.add(k)
                out.append(str(t).strip())

            if out:
                return out, "openlibrary"
    except Exception:
        pass

    # 2) isbnsearch metadata plugin fallback.
    try:
        from plugins.metadata_plugin import get_metadata_plugin

        provider = get_metadata_plugin("isbnsearch",
                                       config or {})
        if provider is None:
            return [], ""
        items = provider.search(isbn_clean, limit=1)
        if not items:
            return [], ""
        tags = provider.to_tags(items[0])
        if not any(str(t).lower().startswith("isbn:") for t in tags):
            tags = [f"isbn:{isbn_clean}"] + [str(t) for t in tags]
        return [str(t) for t in tags if str(t).strip()], provider.name
    except Exception:
        return [], ""


def _fetch_libgen_details_html(
    url: str,
    *,
    timeout: Optional[Tuple[float,
                            float]] = None
) -> Optional[str]:
    try:
        if timeout is None:
            timeout = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)
        session = get_requests_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        }
        with session.get(str(url), stream=True, timeout=timeout, headers=headers) as resp:
            resp.raise_for_status()
            ct = str(resp.headers.get("Content-Type", "")).lower()
            if "text/html" not in ct:
                return None
            return resp.text
    except Exception:
        return None


def _parse_libgen_details_html(html: str) -> Dict[str, Any]:
    """Parse LibGen details-page HTML (edition.php/file.php) into a metadata dict.

    Best-effort and intentionally tolerant of mirror variations.
    """

    out: Dict[str,
              Any] = {}
    raw_fields: Dict[str,
                     str] = {}
    s = str(html or "")

    # Fast path: try to pull simple Label/Value table rows.
    for m in re.finditer(
            r"(?is)<tr\b[^>]*>\s*<t[dh]\b[^>]*>\s*([^<]{1,80}?)\s*:??\s*</t[dh]>\s*<t[dh]\b[^>]*>(.*?)</t[dh]>\s*</tr>",
            s,
    ):
        label = _strip_html_to_text(m.group(1))
        raw_val_html = str(m.group(2) or "")
        if not label:
            continue
        val_text = _strip_html_to_text(raw_val_html)
        if not val_text:
            continue
        raw_fields[label] = val_text

        norm = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
        if not norm:
            continue

        # Prefer anchors for multi-valued fields.
        anchors = _extract_anchor_texts(raw_val_html)
        if anchors:
            out[norm] = anchors
        else:
            out[norm] = val_text

    # Some libgen.gl edition pages group metadata as repeated blocks like:
    #   <strong>Title:</strong>
    #   The Title
    # We'll parse those too (best-effort, no DOM required).
    strong_matches = list(re.finditer(r"(?is)<strong\b[^>]*>(.*?)</strong>", s))
    if strong_matches:
        for idx, m in enumerate(strong_matches):
            label_raw = _strip_html_to_text(m.group(1))
            label = str(label_raw or "").strip()
            if not label:
                continue

            # Normalize label (strip trailing colon if present).
            if label.endswith(":"):
                label = label[:-1].strip()

            chunk_start = m.end()
            chunk_end = (
                strong_matches[idx + 1].start() if
                (idx + 1) < len(strong_matches) else len(s)
            )
            raw_val_html = s[chunk_start:chunk_end]

            # If we already have a value for this label from a table row, keep it.
            if label in raw_fields:
                continue

            val_text = _strip_html_to_text(raw_val_html)
            if not val_text:
                continue

            raw_fields[label] = val_text

            norm = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
            if not norm:
                continue

            anchors = _extract_anchor_texts(raw_val_html)
            if anchors:
                out[norm] = anchors
            else:
                out[norm] = val_text

    # Normalize keys of interest.
    def _first_str(v: Any) -> str:
        if isinstance(v, list) and v:
            return str(v[0] or "").strip()
        return str(v or "").strip()

    title = _first_str(out.get("title"))
    if title:
        out["title"] = title

    authors = out.get("author_s") or out.get("authors") or out.get("author")
    if isinstance(authors, str):
        authors_list = _split_listish_text(authors)
    elif isinstance(authors, list):
        authors_list = [str(x).strip() for x in authors if str(x).strip()]
    else:
        authors_list = []
    if authors_list:
        out["authors"] = authors_list

    publisher = _first_str(out.get("publisher"))
    if publisher:
        out["publisher"] = publisher

    year = _first_str(out.get("year"))
    if year:
        out["year"] = year

    language = _first_str(out.get("language"))
    if language:
        out["language"] = language

    oclc = _first_str(out.get("oclc_worldcat")) or _first_str(out.get("oclc"))
    if oclc:
        m_oclc = re.search(r"\b\d{5,15}\b", oclc)
        out["oclc"] = str(m_oclc.group(0)) if m_oclc else oclc

    tags_val = out.get("tags")
    if isinstance(tags_val, list):
        tags_list = [str(x).strip() for x in tags_val if str(x).strip()]
    elif isinstance(tags_val, str):
        tags_list = _split_listish_text(tags_val)
    else:
        tags_list = []
    if tags_list:
        out["tags"] = tags_list

    isbn_val = out.get("isbn")
    isbn_text = ""
    if isinstance(isbn_val, list):
        isbn_text = " ".join([str(x) for x in isbn_val if x])
    else:
        isbn_text = str(isbn_val or "")
    isbns = _extract_isbns(isbn_text)
    if isbns:
        out["isbn"] = isbns

    edition_id = _first_str(out.get("edition_id"))
    if edition_id:
        m_eid = re.search(r"\b\d+\b", edition_id)
        out["edition_id"] = str(m_eid.group(0)) if m_eid else edition_id

    if raw_fields:
        out["_raw_fields"] = raw_fields

    return out


def _libgen_metadata_to_tags(meta: Dict[str, Any]) -> List[str]:
    tags: List[str] = []
    seen: set[str] = set()

    def _add(t: str) -> None:
        s = str(t or "").strip()
        if not s:
            return
        k = s.lower()
        if k in seen:
            return
        seen.add(k)
        tags.append(s)

    title = str(meta.get("title") or "").strip()
    if title:
        _add(f"title:{title}")

    for a in meta.get("authors") or []:
        a = str(a or "").strip()
        if a:
            _add(f"author:{a}")

    publisher = str(meta.get("publisher") or "").strip()
    if publisher:
        _add(f"publisher:{publisher}")

    year = str(meta.get("year") or "").strip()
    if year:
        _add(f"year:{year}")

    language = str(meta.get("language") or "").strip()
    if language:
        _add(f"language:{language}")

    for isbn in meta.get("isbn") or []:
        isbn = str(isbn or "").strip().replace("-", "")
        if isbn:
            _add(f"isbn:{isbn}")

    oclc = str(meta.get("oclc") or "").strip()
    if oclc:
        _add(f"oclc:{oclc}")

    edition_id = str(meta.get("edition_id") or "").strip()
    if edition_id:
        _add(f"libgen_edition_id:{edition_id}")

    # Freeform tags (no "tags:" prefix).
    for t in meta.get("tags") or []:
        t = str(t or "").strip()
        if t:
            _add(t)

    # Any additional structured fields we captured are preserved under a libgen_ namespace.
    raw_fields = meta.get("_raw_fields")
    if isinstance(raw_fields, dict):
        for k, v in raw_fields.items():
            lk = str(k or "").strip().lower()
            if lk in {
                    "title",
                    "author(s)",
                    "authors",
                    "author",
                    "publisher",
                    "year",
                    "isbn",
                    "language",
                    "oclc/worldcat",
                    "tags",
                    "edition id",
            }:
                continue
            vv = str(v or "").strip()
            if not vv:
                continue
            ns = re.sub(r"[^a-z0-9]+", "_", lk).strip("_")
            if ns:
                _add(f"libgen_{ns}:{vv}")

    return tags


class Libgen(Plugin):

    SUPPORTED_CMDLETS = frozenset({"download-file", "search-file"})
    TABLE_AUTO_STAGES = {
        "libgen": ["download-file"],
    }
    # Domains that should be routed to this provider when the user supplies a URL.
    # (Used by PluginCore.registry.match_provider_name_for_url)
    URL_DOMAINS = (
        "libgen.gl",
        "libgen.li",
        "libgen.is",
        "libgen.rs",
        "libgen.st",
    )
    URL = URL_DOMAINS
    """Search provider for Library Genesis books."""

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str,
                               Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        filters = filters or {}

        try:
            from SYS.cli_syntax import get_field, get_free_text, parse_query

            parsed = parse_query(query)
            isbn = get_field(parsed, "isbn")
            author = get_field(parsed, "author")
            title = get_field(parsed, "title")
            free_text = get_free_text(parsed)

            search_query = isbn or title or author or free_text or query

            books = search_libgen(
                search_query,
                limit=limit,
                log_info=None,
                log_error=lambda msg: _libgen_panel("libgen", [("error", msg)]),
            )

            results: List[SearchResult] = []
            for idx, book in enumerate(books, 1):
                title = str(book.get("title") or "").strip() or "Unknown"
                author = str(book.get("author") or "").strip() or "Unknown"
                year = book.get("year", "Unknown")
                pages = book.get("pages") or book.get("pages_str") or ""
                extension = book.get("extension", "") or book.get("ext", "")
                filesize = book.get("filesize_str", "Unknown")
                isbn = book.get("isbn", "")
                mirror_url = book.get("mirror_url", "")

                columns = [
                    ("Title",
                     title),
                    ("Author",
                     author),
                    ("Pages",
                     str(pages)),
                    ("Ext",
                     str(extension)),
                ]

                detail = f"By: {author}"
                if year and year != "Unknown":
                    detail += f" ({year})"

                annotations = [f"{filesize}"]
                if isbn:
                    annotations.append(f"ISBN: {isbn}")

                results.append(
                    SearchResult(
                        table="libgen",
                        title=title,
                        path=mirror_url or f"libgen:{book.get('id', '')}",
                        detail=detail,
                        annotations=annotations,
                        media_kind="book",
                        columns=columns,
                        full_metadata={
                            "number": idx,
                            "author": author,
                            "year": year,
                            "isbn": isbn,
                            "filesize": filesize,
                            "pages": pages,
                            "extension": extension,
                            "book_id": book.get("book_id",
                                                ""),
                            "md5": book.get("md5",
                                            ""),
                        },
                    )
                )

            return results

        except Exception as exc:
            log(f"[libgen] Search error: {exc}", file=sys.stderr)
            return []

    def validate(self) -> bool:
        # JSON-based searching can work without lxml; HTML parsing is a fallback.
        return True

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        """Download a LibGen SearchResult into output_dir.

        This is used by the download-file cmdlet when a provider item is piped.
        """
        try:
            output_dir = safe_output_dir(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            target = str(getattr(result, "path", "") or "")
            md = getattr(result, "full_metadata", None)
            if not isinstance(md, dict):
                md = {}
                try:
                    setattr(result, "full_metadata", md)
                except Exception:
                    pass

            title = str(getattr(result, "title", "") or "").strip()
            md5 = str(md.get("md5") or "").strip()
            extension = str(md.get("extension") or "").strip().lstrip(".")

            # If the user passed ads.php/get.php directly, capture md5 from the URL so
            # filenames are stable (avoid always writing `libgen.pdf`).
            if (not md5) and isinstance(target, str) and target.startswith("http"):
                md5 = _libgen_md5_from_url(target)
                if md5:
                    md["md5"] = md5

            # Defer LibGen details-page metadata and ISBN enrichment until AFTER the file is downloaded.

            if (not target) or target.startswith("libgen:"):
                if md5 and re.fullmatch(r"[a-fA-F0-9]{32}", md5):
                    target = urljoin(MIRRORS[0], f"/ads.php?md5={md5}")

            if not target:
                return None

            if title and title.startswith("http"):
                title = ""

            base_name = sanitize_filename(
                title or md5 or (
                    f"libgen_{_libgen_id_from_url(target)}"
                    if _libgen_id_from_url(target) else "libgen"
                )
            )
            out_path = output_dir / base_name
            if extension:
                out_path = out_path.with_suffix(f".{extension}")

            if out_path.exists():
                stem = out_path.stem
                suffix = out_path.suffix
                counter = 1
                while out_path.exists() and counter < 200:
                    out_path = out_path.with_name(f"{stem}({counter}){suffix}")
                    counter += 1

            # Show a progress bar on stderr (safe for pipelines).
            progress_bar = ProgressBar()
            last_progress_time = [0.0]
            label = out_path.name
            live = None
            transfer_started = [False]
            try:
                from SYS import pipeline as pipeline_ctx

                live = pipeline_ctx.get_live_progress()
            except Exception:
                live = None

            def progress_callback(bytes_downloaded: int, content_length: int) -> None:
                total = int(content_length) if content_length and content_length > 0 else None
                downloaded = int(bytes_downloaded) if bytes_downloaded and bytes_downloaded > 0 else 0
                if live is not None:
                    try:
                        if not transfer_started[0] and hasattr(live, "begin_transfer"):
                            live.begin_transfer(label=str(label or "download"), total=total)
                            transfer_started[0] = True
                        if hasattr(live, "update_transfer"):
                            live.update_transfer(
                                label=str(label or "download"),
                                completed=downloaded,
                                total=total,
                            )
                    except Exception:
                        pass
                now = time.time()
                if now - last_progress_time[0] < 0.25 and downloaded not in {0, total}:
                    return
                progress_bar.update(
                    downloaded=downloaded,
                    total=total,
                    label=str(label or "download"),
                    file=sys.stderr,
                )
                last_progress_time[0] = now

            ok, final_path = download_from_mirror(
                target,
                out_path,
                progress_callback=progress_callback,
                log_info=None,
                log_error=lambda msg: _libgen_panel("libgen download", [("error", msg)]),
            )
            progress_bar.finish()
            if live is not None and transfer_started[0] and hasattr(live, "finish_transfer"):
                try:
                    live.finish_transfer(label=str(label or "download"))
                except Exception:
                    pass
            if ok and final_path:
                # After the download completes, best-effort fetch details metadata (title + ISBN)
                # and then enrich tags via OpenLibrary/isbnsearch. This ensures enrichment never
                # blocks the download itself.
                try:
                    target_str = str(target)
                    if isinstance(target, str) and target_str.startswith("http"):
                        low = target_str.lower()
                        # Preferred: ads.php pages often embed a complete tag block.
                        # Parse it post-download (best-effort) and do NOT perform external
                        # enrichment (OpenLibrary/isbnsearch) unless the user later chooses to.
                        if ("/ads.php" in low) or ("/get.php" in low):
                            ads_url = (
                                target_str if "/ads.php" in low else
                                _libgen_ads_url_for_target(target_str)
                            )
                            if ads_url:
                                html = _fetch_libgen_details_html(
                                    ads_url,
                                    timeout=(DEFAULT_CONNECT_TIMEOUT,
                                             4.0)
                                )
                                if html:
                                    meta = _parse_libgen_ads_tags_html(html)
                                    extracted_title = str(meta.get("title")
                                                          or "").strip()
                                    if extracted_title:
                                        if md is not None:
                                            md["title"] = extracted_title
                                        result.tag.add(f"title:{extracted_title}")
                                        if (not title) or title.startswith("http"):
                                            title = extracted_title

                                    authors = (
                                        meta.get("authors")
                                        if isinstance(meta.get("authors"),
                                                      list) else []
                                    )
                                    for a in authors or []:
                                        aa = str(a or "").strip()
                                        if aa:
                                            result.tag.add(f"author:{aa}")

                                    publisher = str(meta.get("publisher") or "").strip()
                                    if publisher:
                                        if md is not None:
                                            md["publisher"] = publisher
                                        result.tag.add(f"publisher:{publisher}")

                                    year = str(meta.get("year") or "").strip()
                                    if year:
                                        if md is not None:
                                            md["year"] = year
                                        result.tag.add(f"year:{year}")

                                    language = str(meta.get("language") or "").strip()
                                    if language:
                                        if md is not None:
                                            md["language"] = language
                                        result.tag.add(f"language:{language}")

                                    isbns = (
                                        meta.get("isbn")
                                        if isinstance(meta.get("isbn"),
                                                      list) else []
                                    )
                                    isbns = [
                                        str(x).strip() for x in (isbns or [])
                                        if str(x).strip()
                                    ]
                                    if isbns:
                                        if md is not None:
                                            md["isbn"] = isbns
                                        for isbn_val in isbns:
                                            result.tag.add(f"isbn:{isbn_val}")

                                    free_tags = (
                                        meta.get("tags")
                                        if isinstance(meta.get("tags"),
                                                      list) else []
                                    )
                                    for t in free_tags or []:
                                        tt = str(t or "").strip()
                                        if tt:
                                            result.tag.add(tt)

                                    # Preserve any other extracted fields (namespaced).
                                    raw_fields = meta.get("_raw_fields")
                                    if isinstance(raw_fields, dict):
                                        for k, v in raw_fields.items():
                                            lk = str(k or "").strip().lower()
                                            if lk in {
                                                    "title",
                                                    "author",
                                                    "authors",
                                                    "publisher",
                                                    "year",
                                                    "isbn",
                                                    "language",
                                                    "tags",
                                            }:
                                                continue
                                            vv = str(v or "").strip()
                                            if not vv:
                                                continue
                                            ns = re.sub(r"[^a-z0-9]+",
                                                        "_",
                                                        lk).strip("_")
                                            if ns:
                                                result.tag.add(f"libgen_{ns}:{vv}")

                        # Legacy: edition/file/series details pages (title + ISBN) + external enrichment.
                        if (("/edition.php" in low) or ("/file.php" in low)
                                or ("/series.php" in low)):
                            html = _fetch_libgen_details_html(target_str)
                            if html:
                                meta = _parse_libgen_details_html(html)

                                if not meta.get("edition_id"):
                                    eid = _libgen_id_from_url(target_str)
                                    if eid:
                                        meta["edition_id"] = eid

                                extracted_title = str(meta.get("title") or "").strip()
                                extracted_isbns = (
                                    meta.get("isbn")
                                    if isinstance(meta.get("isbn"),
                                                  list) else []
                                )
                                extracted_isbns = [
                                    str(x).strip() for x in (extracted_isbns or [])
                                    if str(x).strip()
                                ]

                                if extracted_title:
                                    if md is not None:
                                        md["title"] = extracted_title
                                    result.tag.add(f"title:{extracted_title}")
                                if extracted_isbns:
                                    if md is not None:
                                        md["isbn"] = extracted_isbns
                                    for isbn_val in extracted_isbns:
                                        isbn_norm = str(isbn_val
                                                        ).strip().replace("-",
                                                                          "")
                                        if isbn_norm:
                                            result.tag.add(f"isbn:{isbn_norm}")
                                if meta.get("edition_id"):
                                    if md is not None:
                                        md["edition_id"] = str(meta.get("edition_id"))

                                preferred_isbn = _prefer_isbn(extracted_isbns)
                                if preferred_isbn:
                                    enriched_tags, enriched_source = _enrich_book_tags_from_isbn(
                                        preferred_isbn,
                                        config=getattr(self, "config", None),
                                    )
                                    if enriched_tags:
                                        try:
                                            result.tag.update(set(enriched_tags))
                                        except Exception:
                                            pass
                                    if enriched_source:
                                        if md is not None:
                                            md["metadata_enriched_from"] = enriched_source

                                if extracted_title and ((not title)
                                                        or title.startswith("http")):
                                    title = extracted_title
                except Exception as e:
                    debug(f"[libgen] Post-download enrichment failed: {e}")

                debug(f"[libgen] Returning downloaded path: {final_path}")
                return Path(final_path)
            
            _libgen_panel("libgen download", [("ok", ok), ("path", final_path)])
            return None
        except Exception as exc:
            _libgen_panel("libgen download", [("error", str(exc))])
            return None

    def download_url(self, url: str, output_dir: Path) -> Optional[Path]:
        """Download a direct LibGen URL using the regular mirror logic."""
        try:
            from PluginCore.base import SearchResult
            sr = SearchResult(
                table="libgen",
                title="libgen",
                path=url,
                full_metadata={
                    "md5": _libgen_md5_from_url(url)
                }
            )
            return self.download(sr, output_dir)
        except Exception:
            return None


LogFn = Optional[Callable[[str], None]]
ErrorFn = Optional[Callable[[str], None]]

DEFAULT_TIMEOUT = 20.0
DEFAULT_LIMIT = 50

# Keep LibGen searches responsive even if mirrors are blocked or slow.
# Note: requests' timeout doesn't always cover DNS stalls, but this prevents
# multi-mirror attempts from taking minutes.
DEFAULT_SEARCH_TOTAL_TIMEOUT = 20.0
DEFAULT_CONNECT_TIMEOUT = 4.0
DEFAULT_READ_TIMEOUT = 10.0

# Mirrors to try in order
MIRRORS = [
    # Prefer .gl first (often most reachable/stable)
    "https://libgen.gl",
    "http://libgen.gl",
    "https://libgen.li",
    "http://libgen.li",
    "https://libgen.is",
    "https://libgen.rs",
    "https://libgen.st",
    "http://libgen.is",
    "http://libgen.rs",
    "http://libgen.st",
]

logging.getLogger(__name__).setLevel(logging.INFO)


def _call(logger: LogFn, message: str) -> None:
    if logger:
        logger(message)


class LibgenSearch:
    """Robust LibGen searcher."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or get_requests_session()
        # Ensure a modern browser UA is present without clobbering existing one.
        if not any(k.lower() == "user-agent" for k in (self.session.headers or {})):
            self.session.headers.update(
                {
                    "User-Agent":
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                }
            )

    def _search_libgen_json(
        self,
        mirror: str,
        query: str,
        limit: int,
        *,
        timeout: Any = DEFAULT_TIMEOUT,
    ) -> List[Dict[str,
                   Any]]:
        """Search libgen.rs/is/st JSON API when available.

        Many LibGen mirrors expose /json.php which is less brittle than scraping.
        """
        url = f"{mirror}/json.php"
        params = {
            "req": query,
            "res": max(1,
                       min(100,
                           int(limit) if limit else 50)),
            "column": "def",
            "phrase": 1,
        }

        resp = self.session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()

        data = resp.json()
        if not isinstance(data, list):
            return []

        results: List[Dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue

            # LibGen JSON responses vary by mirror; accept several common keys.
            raw_id = item.get("ID") or item.get("Id") or item.get("id") or ""
            title = item.get("Title") or item.get("title") or ""
            author = item.get("Author") or item.get("author") or ""
            publisher = item.get("Publisher") or item.get("publisher") or ""
            year = item.get("Year") or item.get("year") or ""
            pages = item.get("Pages") or item.get("pages") or ""
            language = item.get("Language") or item.get("language") or ""
            size = item.get("Size") or item.get("size") or item.get("filesize") or ""
            extension = item.get("Extension") or item.get("extension"
                                                          ) or item.get("ext") or ""
            md5 = item.get("MD5") or item.get("md5") or ""

            download_link = f"http://library.lol/main/{md5}" if md5 else ""

            results.append(
                {
                    "id": str(raw_id),
                    "title": str(title),
                    "author": str(author),
                    "publisher": str(publisher),
                    "year": str(year),
                    "pages": str(pages),
                    "language": str(language),
                    "filesize_str": str(size),
                    "extension": str(extension),
                    "md5": str(md5),
                    "mirror_url": download_link,
                    "cover": "",
                }
            )

            if len(results) >= limit:
                break

        return results

    def search(
        self,
        query: str,
        limit: int = DEFAULT_LIMIT,
        *,
        total_timeout: float = DEFAULT_SEARCH_TOTAL_TIMEOUT,
        log_info: LogFn = None,
        log_error: ErrorFn = None,
    ) -> List[Dict[str,
                   Any]]:
        """Search LibGen mirrors.

        Uses a total time budget across mirrors to avoid long hangs.
        """
        # Prefer JSON API (no lxml needed); HTML scraping is a fallback.
        has_lxml = lxml_html is not None

        started = time.monotonic()

        for mirror in MIRRORS:
            elapsed = time.monotonic() - started
            remaining = total_timeout - elapsed
            if remaining <= 0:
                _call(
                    log_error,
                    f"[libgen] Search timed out after {total_timeout:.0f}s"
                )
                break

            # Bound each request so we can try multiple mirrors within the budget.
            # Keep connect+read within the remaining budget as a best-effort.
            connect_timeout = min(DEFAULT_CONNECT_TIMEOUT, max(0.1, remaining))
            read_budget = max(0.1, remaining - connect_timeout)
            read_timeout = min(DEFAULT_READ_TIMEOUT, read_budget)
            request_timeout: Any = (connect_timeout, read_timeout)

            _call(log_info, f"[libgen] Trying mirror: {mirror}")

            try:
                # Try JSON first on *all* mirrors (including .gl/.li), then fall back to HTML scraping.
                results: List[Dict[str, Any]] = []
                try:
                    results = self._search_libgen_json(
                        mirror,
                        query,
                        limit,
                        timeout=request_timeout
                    )
                except Exception:
                    results = []

                if not results:
                    if not has_lxml:
                        continue

                    if "libgen.li" in mirror or "libgen.gl" in mirror:
                        results = self._search_libgen_li(
                            mirror,
                            query,
                            limit,
                            timeout=request_timeout
                        )
                    else:
                        results = self._search_libgen_rs(
                            mirror,
                            query,
                            limit,
                            timeout=request_timeout
                        )

                if results:
                    _call(log_info, f"[libgen] Using mirror: {mirror}")
                    return results
                else:
                    _call(log_info, "[libgen] Mirror returned 0 results; stopping mirror fallback")
                    break
            except requests.exceptions.Timeout:
                _call(log_info, f"[libgen] Mirror timed out: {mirror}")
                continue
            except requests.exceptions.RequestException:
                _call(log_info, f"[libgen] Mirror request failed: {mirror}")
                continue
            except Exception as e:
                logging.debug(f"Mirror {mirror} failed: {e}")
                continue

        return []

    def _search_libgen_rs(
        self,
        mirror: str,
        query: str,
        limit: int,
        *,
        timeout: Any = DEFAULT_TIMEOUT,
    ) -> List[Dict[str,
                   Any]]:
        """Search libgen.rs/is/st style mirrors."""
        url = f"{mirror}/search.php"
        params = {
            "req": query,
            "res": 100,
            "column": "def",
            "open": 0,
            "view": "simple",
            "phrase": 1,
        }

        resp = self.session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()

        if lxml_html is None:
            return []

        def _text(el: Any) -> str:
            return " ".join([t.strip() for t in el.itertext()
                             if t and str(t).strip()]).strip()

        try:
            doc = lxml_html.fromstring(resp.content)
        except Exception:
            return []

        table_nodes = doc.xpath(
            "//table[contains(concat(' ', normalize-space(@class), ' '), ' c ')]"
        )
        table = table_nodes[0] if table_nodes else None
        if table is None:
            for t in doc.xpath("//table"):
                if len(t.xpath(".//tr")) > 5:
                    table = t
                    break

        if table is None:
            return []

        results: List[Dict[str, Any]] = []
        rows = table.xpath(".//tr")[1:]

        for row in rows:
            cols = row.xpath("./td")
            if len(cols) < 9:
                continue

            try:
                libgen_id = _text(cols[0])

                author_links = cols[1].xpath(".//a")
                authors = [_text(a) for a in author_links if _text(a)]
                if not authors:
                    authors = [_text(cols[1])]

                title_tag = None
                title_links = cols[2].xpath(".//a")
                if title_links:
                    title_tag = title_links[0]
                title = _text(title_tag) if title_tag is not None else _text(cols[2])

                md5 = ""
                if title_tag is not None:
                    href = str(title_tag.get("href") or "")
                    match = re.search(r"md5=([a-fA-F0-9]{32})", href)
                    if match:
                        md5 = match.group(1)

                publisher = _text(cols[3])
                year = _text(cols[4])
                pages = _text(cols[5])
                language = _text(cols[6])
                size = _text(cols[7])
                extension = _text(cols[8])

                mirror_links: List[str] = []
                for i in range(9, len(cols)):
                    a_nodes = cols[i].xpath(".//a[@href]")
                    if a_nodes:
                        href = str(a_nodes[0].get("href") or "").strip()
                        if href:
                            mirror_links.append(href)

                if md5:
                    download_link = f"http://library.lol/main/{md5}"
                elif mirror_links:
                    download_link = mirror_links[0]
                else:
                    download_link = ""

                results.append(
                    {
                        "id": libgen_id,
                        "title": title,
                        "author": ", ".join([a for a in authors if a]) or "Unknown",
                        "publisher": publisher,
                        "year": year,
                        "pages": pages,
                        "language": language,
                        "filesize_str": size,
                        "extension": extension,
                        "md5": md5,
                        "mirror_url": download_link,
                        "cover": "",
                    }
                )

                if len(results) >= limit:
                    break
            except Exception as e:
                logging.debug(f"Error parsing row: {e}")
                continue

        return results

    def _search_libgen_li(
        self,
        mirror: str,
        query: str,
        limit: int,
        *,
        timeout: Any = DEFAULT_TIMEOUT,
    ) -> List[Dict[str,
                   Any]]:
        """Search libgen.li/gl style mirrors."""
        url = f"{mirror}/index.php"
        params = {
            "req": query,
            # Keep the request lightweight; covers slow the HTML response.
            "res": max(1,
                       min(100,
                           int(limit) if limit else 50)),
            "covers": "off",
            "filesuns": "all",
        }

        resp = self.session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()

        if lxml_html is None:
            return []

        def _text(el: Any) -> str:
            return " ".join([t.strip() for t in el.itertext()
                             if t and str(t).strip()]).strip()

        try:
            doc = lxml_html.fromstring(resp.content)
        except Exception:
            return []

        table_nodes = doc.xpath("//table[@id='tablelibgen']")
        table = table_nodes[0] if table_nodes else None
        if table is None:
            # Common libgen.li/gl fallback
            table_nodes = doc.xpath(
                "//table[contains(concat(' ', normalize-space(@class), ' '), ' table ') and "
                "contains(concat(' ', normalize-space(@class), ' '), ' table-striped ')]"
            )
            table = table_nodes[0] if table_nodes else None

        if table is None:
            return []

        results: List[Dict[str, Any]] = []
        rows = table.xpath(".//tr")[1:]

        for row in rows:
            cols = row.xpath("./td")
            if len(cols) < 9:
                continue

            try:
                # Extract md5 (libgen.gl exposes /ads.php?md5=... in mirror column)
                md5 = ""
                mirror_url = ""
                for a in row.xpath(".//a[@href]"):
                    href = str(a.get("href") or "")
                    if not href:
                        continue
                    m = re.search(r"md5=([a-fA-F0-9]{32})", href)
                    if m:
                        md5 = m.group(1)
                        if "ads.php" in href:
                            mirror_url = urljoin(mirror, href)
                        break
                if not mirror_url and md5:
                    mirror_url = urljoin(mirror, f"/ads.php?md5={md5}")

                # Extract numeric file id from /file.php?id=...
                libgen_id = ""
                for a in row.xpath(".//a[@href]"):
                    href = str(a.get("href") or "")
                    if not href:
                        continue
                    if re.search(r"/file\.php\?id=\d+", href):
                        m = re.search(r"id=(\d+)", href)
                        if m:
                            libgen_id = m.group(1)
                            break

                title = ""
                authors = ""
                publisher = ""
                year = ""
                language = ""
                pages = ""
                size = ""
                extension = ""
                isbn = ""

                # libgen.gl columns shift depending on whether covers are enabled.
                # With covers on:  cover, meta, author, publisher, year, language, pages, size, ext, mirrors  (10)
                # With covers off: meta, author, publisher, year, language, pages, size, ext, mirrors          (9)
                offset: Optional[int] = None
                if len(cols) >= 10:
                    offset = 1
                elif len(cols) >= 9:
                    offset = 0

                if offset is not None:
                    meta_cell = cols[offset]
                    meta_text = _text(meta_cell)

                    # Extract ISBNs from meta cell (avoid using them as title)
                    # Matches 10 or 13-digit ISBN with optional leading 978/979.
                    isbn_candidates = re.findall(
                        r"\b(?:97[89])?\d{9}[\dXx]\b",
                        meta_text
                    )
                    if isbn_candidates:
                        seen: List[str] = []
                        for s in isbn_candidates:
                            s = s.upper()
                            if s not in seen:
                                seen.append(s)
                        isbn = "; ".join(seen)

                    # Choose a "real" title from meta cell.
                    # libgen.gl meta can include series/edition/isbn blobs; prefer text with letters.
                    raw_candidates: List[str] = []
                    for a in meta_cell.xpath(".//a"):
                        t = _text(a)
                        if t:
                            raw_candidates.append(t)
                    for s in meta_cell.itertext():
                        t = str(s).strip()
                        if t:
                            raw_candidates.append(t)

                    deduped: List[str] = []
                    for t in raw_candidates:
                        t = t.strip()
                        if t and t not in deduped:
                            deduped.append(t)

                    def _looks_like_isbn_blob(text: str) -> bool:
                        if re.fullmatch(r"[0-9Xx;\s\-]+", text):
                            # Numbers-only (common for ISBN lists)
                            return True
                        if ";" in text and len(re.findall(r"[A-Za-z]", text)) == 0:
                            return True
                        return False

                    best_title = ""
                    best_score: Optional[tuple] = None
                    for cand in deduped:
                        low = cand.lower().strip()
                        if low in {"cover",
                                   "edition"}:
                            continue
                        if _looks_like_isbn_blob(cand):
                            continue

                        letters = len(re.findall(r"[A-Za-z]", cand))
                        if letters < 3:
                            continue

                        digits = len(re.findall(r"\d", cand))
                        digit_ratio = digits / max(1, len(cand))
                        # Prefer more letters, fewer digits, and longer strings.
                        score = (letters, -digit_ratio, len(cand))
                        if best_score is None or score > best_score:
                            best_score = score
                            best_title = cand

                    title = best_title or _text(meta_cell)

                    authors = _text(cols[offset + 1])
                    publisher = _text(cols[offset + 2])
                    year = _text(cols[offset + 3])
                    language = _text(cols[offset + 4])
                    pages = _text(cols[offset + 5])
                    size = _text(cols[offset + 6])
                    extension = _text(cols[offset + 7])
                else:
                    # Older fallback structure
                    title_col = cols[1]
                    title_links = title_col.xpath(".//a")
                    title = _text(title_links[0]) if title_links else _text(title_col)
                    authors = _text(cols[2])
                    publisher = _text(cols[3])
                    year = _text(cols[4])
                    language = _text(cols[5])
                    pages = _text(cols[6])
                    size = _text(cols[7])
                    extension = _text(cols[8])

                title = (title or "").strip() or "Unknown"
                authors = (authors or "").strip() or "Unknown"

                results.append(
                    {
                        "id": libgen_id,
                        "title": title,
                        "author": authors,
                        "isbn": (isbn or "").strip(),
                        "publisher": (publisher or "").strip(),
                        "year": (year or "").strip(),
                        "pages": (pages or "").strip(),
                        "language": (language or "").strip(),
                        "filesize_str": (size or "").strip(),
                        "extension": (extension or "").strip(),
                        "md5": md5,
                        "mirror_url": mirror_url,
                    }
                )

                if len(results) >= limit:
                    break
            except Exception:
                continue

        return results


def search_libgen(
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    log_info: LogFn = None,
    log_error: ErrorFn = None,
    session: Optional[requests.Session] = None,
) -> List[Dict[str,
               Any]]:
    """Search Libgen using the robust scraper."""
    searcher = LibgenSearch(session=session)
    try:
        results = searcher.search(
            query,
            limit=limit,
            total_timeout=DEFAULT_SEARCH_TOTAL_TIMEOUT,
            log_info=log_info,
            log_error=log_error,
        )
        _call(log_info, f"[libgen] Found {len(results)} results")
        return results
    except Exception as e:
        _call(log_error, f"[libgen] Search failed: {e}")
        return []


def _resolve_download_url(
    session: requests.Session,
    url: str,
    log_info: LogFn = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the final download URL by following the LibGen chain."""
    current_url = url
    referer = None
    visited = set()

    def _resolve_html_links_regex(base_url: str, html: str) -> Optional[str]:
        """Best-effort HTML link resolver without lxml.

        This is intentionally minimal: it primarily targets LibGen landing pages like
        `/ads.php?md5=...` which contain a `get.php?md5=...` link.
        """
        if not html:
            return None

        # Use a more relaxed regex for href that handles spaces and missing quotes.
        def _find_link(pattern: str) -> Optional[str]:
            regex = r"href\s*=\s*['\"]?(" + pattern + r")['\"]?"
            match = re.search(regex, html, flags=re.IGNORECASE)
            if match:
                u = str(match.group(1) or "").strip()
                u = u.split("'")[0].split('"')[0].split(">")[0].split(" ")[0].strip()
                if u and not u.lower().startswith("javascript:"):
                    return urljoin(base_url, u)
            return None

        # Priority patterns for LibGen mirrors (e.g., library.lol, libgen.li)
        # 1. library.lol "GET" link or direct /main/
        found = _find_link(r'[^"\' >]*/main/\d+/[^"\' >]*')
        if found:
            return found

        # 2. get.php md5 links
        found = _find_link(r'[^"\' >]*get\.php\?md5=[a-fA-F0-9]{32}[^"\' >]*')
        if found:
            return found

        # 3. ads.php md5 links
        found = _find_link(r'[^"\' >]*ads\.php\?md5=[a-fA-F0-9]{32}[^"\' >]*')
        if found:
            return found

        # 4. file.php id links
        found = _find_link(r'[^"\' >]*file\.php\?id=\d+[^"\' >]*')
        if found:
            return found

        # 5. edition.php id links
        found = _find_link(r'[^"\' >]*edition\.php\?id=\d+[^"\' >]*')
        if found:
            return found

        # 6. Direct file extensions
        found = _find_link(r'[^"\' >]+\.(?:pdf|epub|mobi|djvu|azw3|cbz|cbr)(?:\?[^"\' >]*)?')
        if found:
            return found

        return None

    def _find_href_by_text(doc: Any, pattern: str) -> Optional[str]:
        for a in doc.xpath("//a[@href]"):
            t = " ".join([s.strip() for s in a.itertext()
                          if s and str(s).strip()]).strip()
            if t and re.search(pattern, t, re.IGNORECASE):
                href = str(a.get("href") or "").strip()
                if href and not href.lower().startswith("javascript:"):
                    return href
        return None

    for idx in range(10):
        if current_url in visited:
            break
        visited.add(current_url)

        if current_url.lower().split("?")[0].split("#")[0].endswith(
            (".pdf", ".epub", ".mobi", ".djvu", ".azw3", ".cbz", ".cbr")
        ):
            return current_url, referer

        try:
            # Pass Referer to stay in the mirror's good graces
            headers = {}
            if referer:
                headers["Referer"] = referer

            with session.get(current_url, stream=True, timeout=30, headers=headers) as resp:
                resp.raise_for_status()
                ct = str(resp.headers.get("Content-Type", "")).lower()

                if "text/html" not in ct:
                    return current_url, referer

                # Only read if it's small enough to be a landing page
                content = resp.text
        except Exception as e:
            return None, None

        next_url = None
        doc = None
        if lxml_html is not None:
            try:
                doc = lxml_html.fromstring(content)
            except Exception:
                doc = None

        if doc is not None:
            # Try to find common mirror links via XPath
            get_href = _find_href_by_text(doc, r"^GET$")
            if get_href:
                next_url = urljoin(current_url, get_href)

            if not next_url:
                # Mirror-specific patterns
                if "series.php" in current_url:
                    hrefs = doc.xpath("//a[contains(@href,'edition.php')]/@href")
                    if hrefs:
                        next_url = urljoin(current_url, str(hrefs[0] or ""))
                elif "edition.php" in current_url:
                    hrefs = doc.xpath("//a[contains(@href,'file.php')]/@href")
                    if hrefs:
                        next_url = urljoin(current_url, str(hrefs[0] or ""))
                elif "file.php" in current_url:
                    libgen_href = None
                    for a in doc.xpath("//a[@href]"):
                        if str(a.get("title") or "").strip().lower() == "libgen":
                            libgen_href = str(a.get("href") or "").strip()
                            break
                    if not libgen_href:
                        libgen_href = _find_href_by_text(doc, r"Libgen")
                    if libgen_href:
                        next_url = urljoin(current_url, libgen_href)
                elif "ads.php" in current_url:
                    hrefs = doc.xpath("//a[contains(@href,'get.php')]/@href")
                    if hrefs:
                        next_url = urljoin(current_url, str(hrefs[0] or ""))

            if not next_url:
                # General provider links
                for text in ["Cloudflare", "IPFS.io", "Infura"]:
                    href = _find_href_by_text(doc, re.escape(text))
                    if href:
                        next_url = urljoin(current_url, href)
                        break

        # Fallback to regex if XPath failed or lxml is missing
        if not next_url:
            next_url = _resolve_html_links_regex(current_url, content)

        if next_url:
            referer = current_url
            current_url = next_url
            continue

        break

    return None, None


def _guess_filename_extension(download_url: str,
                              headers: Dict[str,
                                            str]) -> Optional[str]:
    """Guess the file extension from headers or the download URL."""
    content_disposition = headers.get("content-disposition", "")
    if content_disposition:
        match = re.search(
            r"filename\*?=(?:UTF-8\'\'|\"?)([^\";]+)",
            content_disposition,
            flags=re.IGNORECASE
        )
        if match:
            filename = unquote(match.group(1).strip('"'))
            suffix = Path(filename).suffix
            if suffix:
                return suffix.lstrip(".")

    parsed = urlparse(download_url)
    suffix = Path(parsed.path).suffix
    if suffix:
        ext = suffix.lstrip(".").lower()
        if ext not in {"php",
                       "php3",
                       "html",
                       "htm",
                       "aspx",
                       "asp"}:
            return ext

    content_type = headers.get("content-type", "").lower()
    mime_map = {
        "application/pdf": "pdf",
        "application/epub+zip": "epub",
        "application/epub": "epub",
        "application/x-mobipocket-ebook": "mobi",
        "application/x-cbr": "cbr",
        "application/x-cbz": "cbz",
        "application/zip": "zip",
    }

    for mime, ext in mime_map.items():
        if mime in content_type:
            return ext

    return None


def _apply_extension(path: Path, extension: Optional[str]) -> Path:
    """Rename the path to match the detected extension, if needed."""
    if not extension:
        return path

    suffix = extension if extension.startswith(".") else f".{extension}"
    if path.suffix.lower() == suffix.lower():
        return path

    candidate = path.with_suffix(suffix)
    base_stem = path.stem
    counter = 1
    while candidate.exists() and counter < 100:
        candidate = path.with_name(f"{base_stem}({counter}){suffix}")
        counter += 1

    try:
        path.replace(candidate)
        return candidate
    except Exception:
        return path


def download_from_mirror(
    mirror_url: str,
    output_path: Path,
    *,
    log_info: LogFn = None,
    log_error: ErrorFn = None,
    session: Optional[requests.Session] = None,
    progress_callback: Optional[Callable[[int,
                                          int],
                                         None]] = None,
) -> Tuple[bool,
           Optional[Path]]:
    """Download file from a LibGen mirror URL with optional progress tracking."""
    session = session or get_requests_session()
    # Ensure a modern browser User-Agent is used for downloads to avoid mirror blocks.
    if not any(
            k.lower() == "user-agent"
            for k in (session.headers or {})
    ):
        session.headers.update(
            {
                "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        download_url, referer = _resolve_download_url(session, mirror_url, log_info)

        if not download_url:
            _call(log_error, "Could not find direct download link")
            return False, None

        _libgen_panel(
            "libgen download",
            [("url", download_url), ("output", str(output_path))],
        )

        req_headers: Dict[str, str] = {}
        if referer:
            req_headers["Referer"] = referer

        from API.HTTP import HTTPClient

        headers: Dict[str, str] = {}
        ua = str((session.headers or {}).get("User-Agent") or "")
        with HTTPClient(timeout=120.0, retries=5, user_agent=ua or "Mozilla/5.0") as client:
            try:
                head = client._request(
                    "HEAD",
                    download_url,
                    headers=req_headers,
                    follow_redirects=True,
                    raise_for_status=False,
                )
                headers = dict(getattr(head, "headers", {}) or {})
                ct = str(headers.get("content-type", "")).lower()
                if "text/html" in ct:
                    _call(log_error, "Final URL returned HTML, not a file.")
                    return False, None
            except Exception:
                headers = {}
            client.download(
                download_url,
                str(output_path),
                chunk_size=262144,
                progress_callback=progress_callback,
                headers=req_headers,
            )

        final_extension = _guess_filename_extension(download_url, headers)
        final_path = _apply_extension(output_path, final_extension)
        return True, final_path

    except Exception as e:
        _call(log_error, f"Download failed: {e}")
        return False, None
