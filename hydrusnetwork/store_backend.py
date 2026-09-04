"""Internal Hydrus store operations owned by the Hydrus plugin package."""

from __future__ import annotations

import fnmatch
import re
import sys
import tempfile
import shutil
from collections.abc import Mapping, Sequence as SequenceABC
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from urllib.parse import quote, parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from API.httpx_shared import get_shared_httpx_client

from SYS.logger import debug, debug_panel, log
from SYS.utils_constant import mime_maps

_KNOWN_EXTS = {
    str(info.get("ext") or "").strip().lstrip(".")
    for category in mime_maps.values()
    for info in category.values()
    if isinstance(info, dict) and info.get("ext")
}
# Maps common file extensions to Hydrus system:filetype predicate values.
# Used to translate `ext:jpg` → `system:filetype = jpeg` for server-side filtering.
_EXT_TO_HYDRUS_FILETYPE: dict[str, str] = {
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "png": "png",
    "gif": "gif",
    "apng": "apng",
    "webp": "webp",
    "tiff": "tiff",
    "tif": "tiff",
    "bmp": "bmp",
    "mp3": "mp3",
    "ogg": "ogg",
    "flac": "flac",
    "m4a": "m4a",
    "mp4": "mp4",
    "webm": "webm",
    "mkv": "mkv",
    "avi": "avi",
    "mov": "mov",
    "pdf": "pdf",
    "epub": "epub",
    "cbz": "cbz",
    "cbr": "cbr",
    "zip": "zip",
    "7z": "7z",
    "rar": "rar",
}

_STRUCTURED_TAG_START_RE = re.compile(
    r"(?:(?<=^)|(?<=[,\s]))[!\-]?[a-z0-9_]+(?:\:[a-z0-9_]+)?\s*(?:=|:)",
    flags=re.IGNORECASE,
)


def _split_structured_tag_query(query: Any) -> list[str]:
    text = str(query or "").strip().lower()
    if not text or text == "*":
        return []

    matches = list(_STRUCTURED_TAG_START_RE.finditer(text))
    if not matches:
        return []

    clauses: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        clause = text[start:end].strip().strip(",").strip()
        if clause:
            clauses.append(clause)

    return clauses


def _build_structured_search_tags(query: Any) -> list[str]:
    clauses = _split_structured_tag_query(query)
    if not clauses:
        return []

    tags: list[str] = []
    for clause in clauses:
        normalized = str(clause or "").strip().lower()
        if not normalized:
            continue
        # Friendly negation sugar: !namespace:value -> -namespace:value
        # Hydrus native tag negation uses a leading '-'.
        if normalized.startswith("!"):
            negated = normalized[1:].strip()
            if negated:
                normalized = f"-{negated}"
        tags.append(normalized)

    return tags


def _hydrus_wildcard_predicate(pred: str) -> str:
    text = str(pred or "").strip().lower()
    if ":" not in text:
        return text
    ns, _, val = text.partition(":")
    ns, val = ns.strip(), val.strip()
    if not ns:
        return text
    if val in {"", "*"} or val.startswith("*"):
        return f"{ns}:*"
    prefix = val.split("*", 1)[0].strip()
    if not prefix:
        return f"{ns}:*"
    token = prefix.split()[0]
    return f"{ns}:{token}*"


def _tag_matches_glob(tag: str, glob: str) -> bool:
    tag_l = str(tag or "").strip().lower()
    glob_l = str(glob or "").strip().lower()
    if not tag_l or not glob_l:
        return False
    if ":" in glob_l:
        gns, _, gval = glob_l.partition(":")
        if ":" not in tag_l:
            return False
        tns, _, tval = tag_l.partition(":")
        if tns != gns:
            return False
        return fnmatch.fnmatchcase(tval, gval or "*")
    return fnmatch.fnmatchcase(tag_l, glob_l)


def _tags_match_globs(tags: Sequence[Any], globs: Sequence[str]) -> bool:
    if not globs:
        return True
    tag_texts = [str(item).strip() for item in (tags or []) if str(item).strip()]
    for glob in globs:
        if not any(_tag_matches_glob(text, glob) for text in tag_texts):
            return False
    return True


def _expand_wildcard_namespace_tags(tags: Sequence[str]) -> tuple[list[str], list[str]]:
    hydrus: list[str] = []
    globs: list[str] = []
    for raw in tags or []:
        pred = str(raw or "").strip()
        if not pred:
            continue
        if (
            pred.startswith("-")
            or pred.startswith("system:")
            or "*" not in pred
            or pred == "*"
        ):
            hydrus.append(pred)
            continue
        globs.append(pred.lower())
        hydrus.append(_hydrus_wildcard_predicate(pred))
    return hydrus, globs


_EXCLUDE_RE = re.compile(
    r"(?:^|[\s,])(?:"
    r"!\(([^)]+)\)"
    r"|!\"([^\"]+)\""
    r"|!'([^']+)'"
    r"|!([a-z][a-z0-9_]*):([^,!]+)"
    r"|!([^\s,]+)"
    r"|-\(([^)]+)\)"
    r"|-\"([^\"]+)\""
    r"|-'([^']+)'"
    r")",
    flags=re.IGNORECASE,
)


def _extract_query_excludes(text: str) -> tuple[str, list[str]]:
    excludes: list[str] = []

    def _keep(match: re.Match[str]) -> str:
        ns = match.group(4)
        val = match.group(5)
        if ns and val:
            phrase = f"{ns.strip()}:{val.strip()}"
        else:
            phrase = next(
                (group for group in match.groups() if group and group is not ns and group is not val),
                "",
            )
            if not phrase:
                phrase = next((group for group in match.groups() if group), "")
        phrase = str(phrase or "").strip().strip("-").lower()
        if phrase:
            excludes.append(phrase)
        return " "

    remaining = _EXCLUDE_RE.sub(_keep, str(text or ""))
    remaining = re.sub(r"\s{2,}", " ", remaining).strip().strip(",")
    return remaining, excludes


def _hydrus_exclude_predicates(excludes: Sequence[str]) -> list[str]:
    preds: list[str] = []
    seen: set[str] = set()
    for raw in excludes or []:
        phrase = str(raw or "").strip().lower().lstrip("-")
        if not phrase:
            continue
        if phrase.endswith(":*"):
            pred = f"-{phrase}"
        elif phrase.endswith(":"):
            pred = f"-{phrase.rstrip(':')}:*"
        elif ":" in phrase:
            pred = f"-{phrase}"
        elif " " not in phrase:
            pred = f"-{phrase}:*"
        else:
            pred = f"-{phrase}"
        if pred not in seen:
            seen.add(pred)
            preds.append(pred)
    return preds


def _text_hits_excludes(title: str, tags: Sequence[Any], excludes: Sequence[str]) -> bool:
    if not excludes:
        return False
    title_l = str(title or "").lower()
    parsed: List[Tuple[str, str]] = []
    freeform: List[str] = []
    for tag in tags or []:
        text = str(tag or "").strip()
        if not text:
            continue
        if ":" in text:
            ns, _, val = text.partition(":")
            parsed.append((ns.strip().lower(), val.strip().lower()))
        else:
            freeform.append(text.lower())
    for ex in excludes:
        phrase = str(ex or "").strip().lower()
        if not phrase:
            continue
        if ":" in phrase:
            ns, _, val = phrase.partition(":")
            ns, val = ns.strip().lower(), val.strip().lower()
            if not val or val == "*":
                if any(tns == ns for tns, _tval in parsed):
                    return True
                continue
            if ns == "title" and val and val in title_l:
                return True
            for tns, tval in parsed:
                if tns == ns and val and val in tval:
                    return True
            continue
        if " " not in phrase:
            if any(tns == phrase for tns, _tval in parsed):
                return True
            continue
        hay = " ".join([title_l, *freeform, *[f"{n}:{v}" for n, v in parsed]])
        if phrase in hay:
            return True
    return False


def _resolve_ext_from_meta(meta: Dict[str, Any], mime_type: Optional[str]) -> str:
    ext = ""
    for key in ("ext", "file_ext", "extension", "file_extension"):
        raw = meta.get(key)
        if raw:
            ext = str(raw).strip().lstrip(".")
            break
    if ext and ext not in _KNOWN_EXTS:
        ext = ""
    if ext.lower() == "ebook":
        ext = ""

    if not ext:
        filetype_human = (
            meta.get("filetype_human")
            or meta.get("mime_human")
            or meta.get("mime_string")
            or meta.get("filetype")
        )
        ft = str(filetype_human or "").strip().lstrip(".").lower()
        if ft and ft != "unknown filetype":
            if ft.isalnum() and len(ft) <= 8:
                ext = ft
            else:
                try:
                    for token in re.findall(r"[a-z0-9]+", ft):
                        if token in _KNOWN_EXTS:
                            ext = token
                            break
                except Exception:
                    pass

    if not ext:
        if not mime_type or not isinstance(mime_type, str) or "/" not in mime_type:
            mime_type = meta.get("mime_string") or meta.get("mime_human") or meta.get("filetype_mime") or mime_type

    if not ext and mime_type:
        try:
            mime_type = str(mime_type).split(";", 1)[0].strip().lower()
        except Exception:
            mime_type = str(mime_type)
        for category in mime_maps.values():
            for _ext_key, info in category.items():
                if mime_type in info.get("mimes", []):
                    ext = str(info.get("ext", "")).strip().lstrip(".")
                    break
            if ext:
                break
    return ext

_HYDRUS_INIT_CHECK_CACHE: dict[tuple[str,
                                     str],
                               tuple[bool,
                                     Optional[str]]] = {}


class HydrusStoreOperations:
    """File storage backend for Hydrus client.

    Each instance represents a specific Hydrus client connection.
    Maintains its own HydrusClient.
    """

    STORE_TYPE = "hydrusnetwork"

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        # Instance name is the multi-instance config key (set via Add Instance).
        # Do not expose a separate NAME field in .config.
        return [
            {
                "key": "URL",
                "label": "Hydrus URL",
                "default": "http://127.0.0.1:45869",
                "placeholder": "http://127.0.0.1:45869",
                "required": True
            },
            {
                "key": "API",
                "label": "API Key",
                "default": "",
                "required": True,
                "secret": True
            }
        ]

    @property
    def is_remote(self) -> bool:
        return True

    @property
    def prefer_defer_tags(self) -> bool:
        return True

    @property
    def supports_url_association(self) -> bool:
        return True

    @property
    def supports_note_association(self) -> bool:
        return True

    @property
    def supports_relationship_association(self) -> bool:
        return True

    def _log_prefix(self) -> str:
        store_name = getattr(self, "NAME", None) or "unknown"
        return f"[hydrusnetwork:{store_name}]"

    def _append_access_key(self, url: str) -> str:
        if not url:
            return url
        if "access_key=" in url:
            return url
        if not getattr(self, "API", None):
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}access_key={quote(str(self.API))}"

    def __new__(cls, *args: Any, **kwargs: Any) -> "HydrusStoreOperations":
        instance = super().__new__(cls)
        name = kwargs.get("NAME")
        api = kwargs.get("API")
        url = kwargs.get("URL")
        if name is not None:
            setattr(instance, "NAME", str(name))
        if api is not None:
            setattr(instance, "API", str(api))
        if url is not None:
            setattr(instance, "URL", str(url))
        return instance

    def __init__(
        self,
        instance_name: Optional[str] = None,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
        *,
        NAME: Optional[str] = None,
        API: Optional[str] = None,
        URL: Optional[str] = None,
    ) -> None:
        """Initialize Hydrus storage backend.

        Args:
            instance_name: Name of this Hydrus instance (e.g., 'home', 'work')
            api_key: Hydrus Client API access key
            url: Hydrus client URL (e.g., 'http://127.0.0.1:45869')
        """
        from plugins.hydrusnetwork.api import HydrusNetwork as HydrusClient

        if instance_name is None and NAME is not None:
            instance_name = str(NAME)
        if api_key is None and API is not None:
            api_key = str(API)
        if url is None and URL is not None:
            url = str(URL)

        if not instance_name or not api_key or not url:
            raise ValueError("HydrusNetwork requires NAME, API, and URL")

        self.NAME = instance_name
        self.API = api_key
        self.URL = url.rstrip("/")

        # Total count (best-effort, used for startup diagnostics)
        self.total_count: Optional[int] = None

        # Self health-check: validate the URL is reachable and the access key is accepted.
        # This MUST NOT attempt to acquire a session key.
        cache_key = (self.URL, self.API)
        cached = _HYDRUS_INIT_CHECK_CACHE.get(cache_key)
        if cached is not None:
            ok, err = cached
            if not ok:
                raise RuntimeError(
                    f"Hydrus '{self.NAME}' unavailable: {err or 'Unavailable'}"
                )
        else:
            api_version_url = f"{self.URL}/api_version"
            verify_key_url = f"{self.URL}/verify_access_key"
            try:
                client = get_shared_httpx_client(timeout=5.0, verify_ssl=False)
                version_resp = client.get(api_version_url, follow_redirects=True)
                version_resp.raise_for_status()
                version_payload = version_resp.json()
                if not isinstance(version_payload, dict):
                    raise RuntimeError(
                        "Hydrus /api_version returned an unexpected response"
                    )

                verify_resp = client.get(
                    verify_key_url,
                    headers={
                        "Hydrus-Client-API-Access-Key": self.API
                    },
                    follow_redirects=True,
                )
                verify_resp.raise_for_status()
                verify_payload = verify_resp.json()
                if not isinstance(verify_payload, dict):
                    raise RuntimeError(
                        "Hydrus /verify_access_key returned an unexpected response"
                    )

                _HYDRUS_INIT_CHECK_CACHE[cache_key] = (True, None)
            except Exception as exc:
                err = str(exc)
                _HYDRUS_INIT_CHECK_CACHE[cache_key] = (False, err)
                raise RuntimeError(f"Hydrus '{self.NAME}' unavailable: {err}") from exc

        # Create a persistent client for this instance (auth via access key by default).
        self._client = HydrusClient(
            url=self.URL,
            access_key=self.API,
            instance_name=self.NAME
        )

        self._service_key_cache: Dict[str, Optional[str]] = {}

        # Best-effort total count (used for startup diagnostics). Avoid heavy payloads.
        # Some Hydrus setups appear to return no count via the CBOR client for this endpoint,
        # so prefer a direct JSON request with a short timeout.
        # NOTE: Disabled to avoid unnecessary API call during init; count will be retrieved on first search/list if needed.
        # try:
        #     self.get_total_count(refresh=True)
        # except Exception:
        #     pass

    def _get_service_key(self, service_name: str, *, refresh: bool = False) -> Optional[str]:
        """Resolve (and cache) the Hydrus service key for the given service name."""
        normalized = str(service_name or "my tags").strip()
        if not normalized:
            normalized = "my tags"
        cache_key = normalized.lower()
        if not refresh and cache_key in self._service_key_cache:
            return self._service_key_cache[cache_key]

        client = self._client
        if client is None:
            self._service_key_cache[cache_key] = None
            return None

        try:
            from plugins.hydrusnetwork.api import get_tag_service_key

            resolved = get_tag_service_key(client, normalized)
        except Exception:
            resolved = None

        self._service_key_cache[cache_key] = resolved
        return resolved

    def get_total_count(self, *, refresh: bool = False) -> Optional[int]:
        """Best-effort total file count for this Hydrus instance.

        Intended for diagnostics (e.g., REPL startup checks). This should be fast,
        and it MUST NOT raise.
        """
        if self.total_count is not None and not refresh:
            return self.total_count

        # 1) Prefer a direct JSON request (fast + avoids CBOR edge cases).
        try:
            import json as _json

            url = f"{self.URL}/get_files/search_files"
            params = {
                "tags": _json.dumps(["system:everything"]),
                "return_hashes": "false",
                "return_file_ids": "false",
                "return_file_count": "true",
            }
            headers = {
                "Hydrus-Client-API-Access-Key": self.API,
                "Accept": "application/json",
            }
            client = get_shared_httpx_client(timeout=5.0, verify_ssl=False)
            resp = client.get(url, params=params, headers=headers, follow_redirects=True)
            resp.raise_for_status()
            payload = resp.json()

            count_val = None
            if isinstance(payload, dict):
                count_val = payload.get("file_count")
                if count_val is None:
                    count_val = payload.get("file_count_inclusive")
                if count_val is None:
                    count_val = payload.get("num_files")
            if isinstance(count_val, int):
                self.total_count = count_val
                return self.total_count
        except Exception as exc:
            debug(
                f"{self._log_prefix()} total count (json) unavailable: {exc}",
                file=sys.stderr
            )

        # 2) Fallback to the API client (CBOR).
        try:
            payload = self._client.search_files(
                tags=["system:everything"],
                return_hashes=False,
                return_file_ids=False,
                return_file_count=True,
            )
            count_val = None
            if isinstance(payload, dict):
                count_val = payload.get("file_count")
                if count_val is None:
                    count_val = payload.get("file_count_inclusive")
                if count_val is None:
                    count_val = payload.get("num_files")
            if isinstance(count_val, int):
                self.total_count = count_val
                return self.total_count
        except Exception as exc:
            debug(
                f"{self._log_prefix()} total count (client) unavailable: {exc}",
                file=sys.stderr
            )

        return self.total_count

    def name(self) -> str:
        return self.NAME

    def get_name(self) -> str:
        return self.NAME

    def set_relationship(self, alt_hash: str, king_hash: str, kind: str = "alt") -> bool:
        """Persist a relationship via the Hydrus client API for this backend instance."""
        try:
            alt_norm = str(alt_hash or "").strip().lower()
            king_norm = str(king_hash or "").strip().lower()
            if len(alt_norm) != 64 or len(king_norm) != 64 or alt_norm == king_norm:
                return False

            client = getattr(self, "_client", None)
            if client is None or not hasattr(client, "set_relationship"):
                return False

            client.set_relationship(alt_norm, king_norm, str(kind or "alt"))
            return True
        except Exception:
            return False

    @staticmethod
    def _has_current_file_service(meta: Dict[str, Any]) -> bool:
        services = meta.get("file_services")
        if not isinstance(services, dict):
            return False
        current = services.get("current")
        if isinstance(current, dict):
            return any(bool(v) for v in current.values())
        if isinstance(current, list):
            return len(current) > 0
        return False

    def has_file(self, file_hash: str) -> bool:
        """Return True only when this Hydrus instance has the actual file bytes."""
        h = str(file_hash or "").strip().lower()
        if len(h) != 64 or not all(ch in "0123456789abcdef" for ch in h):
            return False

        client = self._client
        if client is None:
            return False

        try:
            payload = client.fetch_file_metadata(
                hashes=[h],
                include_service_keys_to_tags=False,
                include_file_services=True,
                include_is_trashed=True,
                include_file_url=False,
                include_duration=False,
                include_size=True,
                include_mime=False,
            )
        except Exception:
            return False

        metas = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(metas, list) or not metas:
            return False
        meta = metas[0]
        if not isinstance(meta, dict) or meta.get("file_id") is None:
            return False
        if "is_local" in meta and not bool(meta.get("is_local")):
            return False
        if isinstance(meta.get("file_services"), dict) and not self._has_current_file_service(meta):
            return False

        # Metadata can exist for orphaned/deleted imports. Confirm bytes are present.
        try:
            from plugins.hydrusnetwork.api import HydrusFileMissingError

            path_payload = client.get_file_path(h)
            if isinstance(path_payload, dict) and str(path_payload.get("path") or "").strip():
                return True
        except Exception as exc:
            try:
                from plugins.hydrusnetwork.api import HydrusFileMissingError

                if isinstance(exc, HydrusFileMissingError):
                    return False
            except Exception:
                pass

        # Fallback: probe the file endpoint for a successful response.
        try:
            file_url = client.file_url(h)
            stream_client = get_shared_httpx_client(timeout=15.0, verify_ssl=False)
            with stream_client.stream(
                "GET",
                file_url,
                headers={
                    "Hydrus-Client-API-Access-Key": self.API,
                    "Accept": "*/*",
                    "Range": "bytes=0-0",
                },
                follow_redirects=True,
                timeout=15.0,
            ) as resp:
                if resp.status_code == 404:
                    return False
                return resp.status_code in (200, 206)
        except Exception:
            return False

    def add_file(self, file_path: Path, **kwargs: Any) -> str:
        """Upload file to Hydrus with full metadata support.

        Args:
            file_path: Path to the file to upload
            tag: Optional list of tag values to add
            url: Optional list of url to associate with the file
            title: Optional title (will be added as 'title:value' tag)

        Returns:
            File hash from Hydrus

        Raises:
            Exception: If upload fails
        """
        from SYS.utils import sha256_file

        tag_list = kwargs.get("tag", [])
        url = kwargs.get("url", [])
        title = kwargs.get("title")
        pipeline_progress = kwargs.get("pipeline_progress")

        def _set_status(text: str) -> None:
            if pipeline_progress is None:
                return
            try:
                pipeline_progress.set_status(f"{self.NAME}: {text}")
            except Exception:
                pass

        def _clear_status() -> None:
            if pipeline_progress is None:
                return
            try:
                pipeline_progress.clear_status()
            except Exception:
                pass

        from SYS.metadata import ensure_single_title_tag

        tag_list = [
            str(t).strip().lower() for t in (tag_list or [])
            if isinstance(t, str) and str(t).strip()
        ]
        preferred_title = str(title or "").strip()
        tag_list = [
            t.lower()
            for t in ensure_single_title_tag(tag_list, preferred_title or None)
            if str(t).strip()
        ]

        try:
            if not file_path.is_file():
                raise Exception(f"Upload source is not a file: {file_path}")
            try:
                source_size = int(file_path.stat().st_size)
            except Exception:
                source_size = -1
            if source_size <= 0:
                raise Exception(f"Upload source is empty or unreadable: {file_path}")

            # Compute file hash (or use hint from kwargs to avoid redundant IO)
            file_hash = kwargs.get("hash") or kwargs.get("file_hash")
            transfer_label = kwargs.get("transfer_label")
            if not file_hash:
                file_hash = sha256_file(file_path)
            
            # Use persistent client with session key
            client = self._client
            if client is None:
                raise Exception("Hydrus client unavailable")

            # Check if file already exists in Hydrus.
            # IMPORTANT: some Hydrus deployments can return a metadata record (file_id)
            # even when the file is not in any current file service (e.g. trashed/missing).
            # Only treat as a real duplicate if it is in a current file service.
            file_exists = False
            try:
                _set_status("checking existing file")
                metadata = client.fetch_file_metadata(
                    hashes=[file_hash],
                    include_service_keys_to_tags=False,
                    include_file_services=True,
                    include_is_trashed=True,
                    include_file_url=True,
                    include_duration=False,
                    include_size=True,
                    include_mime=True,
                )
                if metadata and isinstance(metadata, dict):
                    metas = metadata.get("metadata", [])
                    if isinstance(metas, list) and metas:
                        # Hydrus returns placeholder rows for unknown hashes.
                        # A concrete file_id is enough to treat the file as existing for
                        # compatibility with existing store behavior, even when older or
                        # partial metadata responses omit file_services/size fields.
                        for meta in metas:
                            if not isinstance(meta, dict):
                                continue
                            if meta.get("file_id") is None:
                                continue
                            file_exists = True
                            # Preferred: use file_services.current.
                            if isinstance(meta.get("file_services"), dict):
                                if self._has_current_file_service(meta):
                                    break
                                file_exists = False
                                break

                            # Fallback: if Hydrus doesn't return file_services, only treat as
                            # existing when the metadata looks like a real file (non-zero size).
                            # Keep the file_id-based duplicate result regardless.
                            size_val = meta.get("size")
                            if size_val is None:
                                size_val = meta.get("size_bytes")
                            try:
                                size_int = int(size_val) if size_val is not None else 0
                            except Exception:
                                size_int = 0
                            if size_int > 0:
                                break
                            break
                if file_exists and not self.has_file(file_hash):
                    debug(
                        f"{self._log_prefix()} Metadata exists for {file_hash[:12]} but file bytes are missing; will re-upload"
                    )
                    file_exists = False
                if file_exists:
                    debug(
                        f"{self._log_prefix()} Duplicate detected - file already in Hydrus with hash: {file_hash}"
                    )
            except Exception as exc:
                debug(f"{self._log_prefix()} metadata fetch failed: {exc}")

            # If Hydrus reports an existing file, it may be in trash. Best-effort restore it to 'my files'.
            # Then re-check that it is actually in a current file service; if not, we'll proceed to upload.
            if file_exists:
                try:
                    client.undelete_files([file_hash])
                except Exception:
                    pass

                try:
                    metadata2 = client.fetch_file_metadata(
                        hashes=[file_hash],
                        include_service_keys_to_tags=False,
                        include_file_services=True,
                        include_is_trashed=True,
                        include_file_url=False,
                        include_duration=False,
                        include_size=False,
                        include_mime=False,
                    )
                    metas2 = metadata2.get("metadata", []) if isinstance(metadata2, dict) else []
                    if isinstance(metas2, list) and metas2:
                        still_current = False
                        for meta in metas2:
                            if not isinstance(meta, dict):
                                continue
                            if meta.get("file_id") is None:
                                continue
                            still_current = True
                            if isinstance(meta.get("file_services"), dict):
                                if self._has_current_file_service(meta):
                                    break
                                still_current = False
                                break

                            size_val = meta.get("size")
                            if size_val is None:
                                size_val = meta.get("size_bytes")
                            try:
                                size_int = int(size_val) if size_val is not None else 0
                            except Exception:
                                size_int = 0
                            if size_int > 0:
                                break
                            break
                        if not still_current:
                            file_exists = False
                except Exception:
                    # If re-check fails, keep prior behavior (avoid forcing uploads in unknown states)
                    pass

            # Upload file if not already present
            if not file_exists:
                # Clear any prior deletion/missing-file record so Hydrus accepts the re-import.
                try:
                    clearer = getattr(client, "clear_file_deletion_record", None)
                    if callable(clearer):
                        clearer([file_hash])
                except Exception:
                    pass
                _set_status("uploading file")
                response = client.add_file(
                    file_path,
                    pipeline_progress=pipeline_progress,
                    transfer_label=transfer_label,
                )

                upload_status = response.get("status") if isinstance(response, dict) else None
                if upload_status == 4:
                    error_note = response.get("note", "") if isinstance(response, dict) else ""
                    raise Exception(
                        f"Hydrus rejected the file upload (status=4): {error_note or 'unknown error'}. "
                        f"Source path={file_path} size={source_size} bytes. "
                        f"If this is a cross-instance copy, the source download may be corrupt."
                    )
                if upload_status == 7:
                    error_note = response.get("note", "") if isinstance(response, dict) else ""
                    raise Exception(
                        f"Hydrus reported a file veto (status=7): {error_note or 'file was vetoed by the server'}."
                    )

                hydrus_hash: Optional[str] = None
                if isinstance(response, dict):
                    hydrus_hash = response.get("hash") or response.get("file_hash")
                    if not hydrus_hash:
                        hashes = response.get("hashes")
                        if isinstance(hashes, list) and hashes:
                            hydrus_hash = hashes[0]

                if isinstance(hydrus_hash, (bytes, bytearray)):
                    try:
                        hydrus_hash = bytes(hydrus_hash).hex()
                    except Exception:
                        hydrus_hash = None

                if hydrus_hash:
                    try:
                        hydrus_hash = str(hydrus_hash).strip().lower()
                    except Exception:
                        hydrus_hash = None

                if not hydrus_hash or len(str(hydrus_hash)) != 64:
                    raise Exception(f"Hydrus response missing file hash: {response}")

                file_hash = hydrus_hash
            else:
                _set_status("reusing existing file")

            # Add tags if provided (both for new and existing files)
            if tag_list:
                try:
                    _set_status("applying tags")
                    # Use default tag service
                    service_name = "my tags"
                except Exception:
                    service_name = "my tags"

                try:
                    client.add_tag(file_hash, tag_list, service_name)
                except Exception as exc:
                    log(
                        f"{self._log_prefix()} ⚠️  Failed to add tags: {exc}",
                        file=sys.stderr
                    )

            # Associate url if provided (both for new and existing files)
            if url:
                _set_status("associating urls")
                for url in url:
                    if url:
                        try:
                            client.associate_url(file_hash, str(url))
                        except Exception as exc:
                            log(
                                f"{self._log_prefix()} ⚠️  Failed to associate URL {url}: {exc}",
                                file=sys.stderr,
                            )
            _clear_status()
            return file_hash

        except Exception as exc:
            _clear_status()
            log(f"{self._log_prefix()} ❌ upload failed: {exc}", file=sys.stderr)
            raise

    def search(self, query: str, **kwargs: Any) -> list[Dict[str, Any]]:
        """Search Hydrus database for files matching query.

        Args:
            query: Search query (tags, filenames, hashes, etc.)
            limit: Maximum number of results to return (default: 100)

        Returns:
            List of dicts with 'name', 'hash', 'size', 'tags' fields

        Example:
            results = storage["hydrus"].search("artist:john_doe music")
            results = storage["hydrus"].search("Simple Man")
        """
        limit = kwargs.get("limit", 100)
        minimal = bool(kwargs.get("minimal", False))
        url_only = bool(kwargs.get("url_only", False))
        prefix = self._log_prefix()

        try:
            client = self._client
            if client is None:
                raise Exception("Hydrus client unavailable")

            def _extract_urls(meta_obj: Any) -> list[str]:
                if not isinstance(meta_obj, dict):
                    return []
                raw = meta_obj.get("known_urls")
                if raw is None:
                    raw = meta_obj.get("url")
                if raw is None:
                    raw = meta_obj.get("urls")
                if isinstance(raw, str):
                    val = raw.strip()
                    return [val] if val else []
                if isinstance(raw, list):
                    out: list[str] = []
                    for item in raw:
                        if not isinstance(item, str):
                            continue
                        s = item.strip()
                        if s:
                            out.append(s)
                    return out
                return []

            def _extract_search_ids(payload: Any) -> tuple[list[int], list[str]]:
                if not isinstance(payload, dict):
                    return [], []
                raw_ids = payload.get("file_ids", [])
                raw_hashes = payload.get("hashes", [])
                ids_out: list[int] = []
                hashes_out: list[str] = []
                if isinstance(raw_ids, list):
                    for item in raw_ids:
                        try:
                            if isinstance(item, (int, float)):
                                ids_out.append(int(item))
                                continue
                            if isinstance(item, str) and item.strip().isdigit():
                                ids_out.append(int(item.strip()))
                        except Exception:
                            continue
                if isinstance(raw_hashes, list):
                    for item in raw_hashes:
                        try:
                            candidate = str(item or "").strip().lower()
                            if candidate:
                                hashes_out.append(candidate)
                        except Exception:
                            continue
                return ids_out, hashes_out

            def _fetch_search_metadata(
                *,
                file_ids: Optional[Sequence[Any]] = None,
                hashes: Optional[Sequence[Any]] = None,
                include_tags: bool = True,
                include_urls: bool = True,
                include_mime: bool = True,
            ) -> list[dict[str, Any]]:
                try:
                    payload = client.fetch_file_metadata(
                        file_ids=file_ids,
                        hashes=hashes,
                        include_service_keys_to_tags=include_tags,
                        include_file_url=include_urls,
                        include_duration=False,
                        include_size=True,
                        include_mime=include_mime,
                    )
                except Exception:
                    return []

                metadata = payload.get("metadata", []) if isinstance(payload, dict) else []
                return metadata if isinstance(metadata, list) else []

            def _iter_url_filtered_metadata(
                url_value: str | None,
                want_any: bool,
                fetch_limit: int,
                scan_limit: int | None = None,
                needles: Optional[Sequence[str]] = None,
                *,
                minimal: bool = False,
            ) -> list[dict[str, Any]]:
                """Best-effort URL search by scanning Hydrus metadata with include_file_url=True."""

                try:
                    from plugins.hydrusnetwork.api import generate_hydrus_url_variants
                except Exception:
                    generate_hydrus_url_variants = None  # type: ignore[assignment]

                def _normalize_url_match_token(value: str | None) -> str:
                    token = str(value or "").strip()
                    if not token:
                        return ""

                    token = token.split("#", 1)[0]
                    try:
                        parsed_url = urlsplit(token)
                    except Exception:
                        return token.lower()

                    if parsed_url.scheme and parsed_url.scheme.lower() not in {"http", "https"}:
                        return token.lower()

                    netloc = str(parsed_url.netloc or "").strip().lower()
                    if netloc.startswith("www."):
                        netloc = netloc[4:]

                    try:
                        query_pairs = parse_qsl(parsed_url.query, keep_blank_values=True)
                    except Exception:
                        query_pairs = []

                    filtered_pairs = []
                    for key, val in query_pairs:
                        key_norm = str(key or "").lower()
                        if key_norm in {"t", "start", "time_continue", "timestamp", "time", "begin"}:
                            continue
                        if key_norm.startswith("utm_"):
                            continue
                        filtered_pairs.append((key, val))

                    normalized_query = urlencode(filtered_pairs, doseq=True) if filtered_pairs else ""
                    normalized = urlunsplit(("", netloc, parsed_url.path or "", normalized_query, ""))
                    return str(normalized or token).lstrip("/").lower()

                def _append_url_needles(output: list[str], candidate: str | None) -> None:
                    raw_candidate = str(candidate or "").strip()
                    if not raw_candidate:
                        return

                    expanded_candidates = [raw_candidate]
                    if callable(generate_hydrus_url_variants):
                        try:
                            expanded_candidates.extend(generate_hydrus_url_variants(raw_candidate) or [])
                        except Exception:
                            pass

                    for expanded in expanded_candidates:
                        expanded_text = str(expanded or "").strip()
                        if not expanded_text:
                            continue
                        lowered = expanded_text.lower()
                        if lowered and lowered not in output:
                            output.append(lowered)
                        normalized = _normalize_url_match_token(expanded_text)
                        if normalized and normalized not in output:
                            output.append(normalized)

                candidate_file_ids: list[int] = []
                candidate_hashes: list[str] = []
                seen_file_ids: set[int] = set()
                seen_hashes: set[str] = set()

                def _add_candidates(ids: list[int], hashes: list[str]) -> None:
                    for fid in ids:
                        if fid in seen_file_ids:
                            continue
                        seen_file_ids.add(fid)
                        candidate_file_ids.append(fid)
                    for hh in hashes:
                        if hh in seen_hashes:
                            continue
                        seen_hashes.add(hh)
                        candidate_hashes.append(hh)

                predicate_supported = getattr(self, "_has_url_predicate", None)
                if predicate_supported is not False:
                    try:
                        predicate = "system:has urls"
                        url_search = client.search_files(
                            tags=[predicate],
                            return_hashes=True,
                            return_file_ids=False,
                            return_file_count=False,
                        )
                        ids, hashes = _extract_search_ids(url_search)
                        _add_candidates(ids, hashes)
                        self._has_url_predicate = True
                    except Exception as exc:
                        try:
                            from plugins.hydrusnetwork.api import HydrusRequestError

                            if isinstance(exc, HydrusRequestError) and getattr(exc, "status", None) == 400:
                                self._has_url_predicate = False
                        except Exception:
                            pass

                if not candidate_file_ids and not candidate_hashes:
                    everything = client.search_files(
                        tags=["system:everything"],
                        return_hashes=True,
                        return_file_ids=False,
                        return_file_count=False,
                    )
                    ids, hashes = _extract_search_ids(everything)
                    _add_candidates(ids, hashes)

                if not candidate_file_ids and not candidate_hashes:
                    return []

                needle_list: list[str] = []
                if isinstance(needles, (list, tuple, set)):
                    for item in needles:
                        _append_url_needles(needle_list, str(item or ""))
                if not needle_list:
                    _append_url_needles(needle_list, url_value)
                chunk_size = 200
                out: list[dict[str, Any]] = []
                if scan_limit is None:
                    try:
                        if not want_any and needle_list:
                            if len(needle_list) > 1:
                                scan_limit = max(int(fetch_limit) * 20, 2000)
                            else:
                                scan_limit = max(200, min(int(fetch_limit), 400))
                        else:
                            scan_limit = max(int(fetch_limit) * 5, 1000)
                    except Exception:
                        scan_limit = 400 if (not want_any and needle_list) else 1000
                if scan_limit is not None:
                    scan_limit = min(int(scan_limit), 10000)
                scanned = 0

                def _process_source(items: list[Any], kind: str) -> None:
                    nonlocal scanned
                    for start in range(0, len(items), chunk_size):
                        if len(out) >= fetch_limit:
                            return
                        if scan_limit is not None and scanned >= scan_limit:
                            return
                        chunk = items[start:start + chunk_size]
                        if scan_limit is not None:
                            remaining = scan_limit - scanned
                            if remaining <= 0:
                                return
                            if len(chunk) > remaining:
                                chunk = chunk[:remaining]
                        scanned += len(chunk)
                        try:
                            if kind == "hashes":
                                payload = client.fetch_file_metadata(
                                    hashes=chunk,
                                    include_file_url=True,
                                    include_service_keys_to_tags=not minimal,
                                    include_duration=not minimal,
                                    include_size=not minimal,
                                    include_mime=not minimal,
                                )
                            else:
                                payload = client.fetch_file_metadata(
                                    file_ids=chunk,
                                    include_file_url=True,
                                    include_service_keys_to_tags=not minimal,
                                    include_duration=not minimal,
                                    include_size=not minimal,
                                    include_mime=not minimal,
                                )
                        except Exception:
                            continue

                        metas = payload.get("metadata",
                                            []) if isinstance(payload,
                                                              dict) else []
                        if not isinstance(metas, list):
                            continue

                        for meta in metas:
                            if len(out) >= fetch_limit:
                                break
                            if not isinstance(meta, dict):
                                continue
                            urls = _extract_urls(meta)
                            if not urls:
                                continue
                            if want_any:
                                out.append(meta)
                                continue
                            if not needle_list:
                                continue
                            normalized_urls = [_normalize_url_match_token(u) for u in urls]
                            if any(
                                any(
                                    n in str(u or "").lower() or (normalized_urls[idx] and n in normalized_urls[idx])
                                    for idx, u in enumerate(urls)
                                )
                                for n in needle_list
                            ):
                                out.append(meta)
                                continue

                sources: list[tuple[str, list[Any]]] = []
                if candidate_hashes:
                    sources.append(("hashes", candidate_hashes))
                elif candidate_file_ids:
                    sources.append(("file_ids", candidate_file_ids))

                for kind, items in sources:
                    if len(out) >= fetch_limit:
                        break
                    _process_source(items, kind)

                return out

            def _search_url_query_metadata(
                url_query: str,
                fetch_limit: int,
                *,
                minimal: bool = False,
            ) -> list[dict[str, Any]]:
                """Run a strict url:<pattern> search without falling back to system predicates."""

                if not url_query:
                    return []

                try:
                    payload = client.search_files(
                        tags=[url_query],
                        return_hashes=True,
                        return_file_ids=True,
                    )
                except Exception:
                    return []

                candidate_ids, candidate_hashes = _extract_search_ids(payload)
                if not candidate_ids and not candidate_hashes:
                    return []

                metas_out: list[dict[str, Any]] = []
                chunk_size = 200

                def _fetch_chunk(kind: Literal["file_ids", "hashes"], values: list[Any]) -> None:
                    nonlocal metas_out
                    if not values or len(metas_out) >= fetch_limit:
                        return
                    for start in range(0, len(values), chunk_size):
                        if len(metas_out) >= fetch_limit:
                            break
                        remaining = fetch_limit - len(metas_out)
                        if remaining <= 0:
                            break
                        end = start + min(chunk_size, remaining)
                        chunk = values[start:end]
                        if not chunk:
                            continue
                        try:
                            if kind == "file_ids":
                                metadata = client.fetch_file_metadata(
                                    file_ids=chunk,
                                    include_file_url=True,
                                    include_service_keys_to_tags=False,
                                    include_duration=False,
                                    include_size=not minimal,
                                    include_mime=False,
                                )
                            else:
                                metadata = client.fetch_file_metadata(
                                    hashes=chunk,
                                    include_file_url=True,
                                    include_service_keys_to_tags=False,
                                    include_duration=False,
                                    include_size=not minimal,
                                    include_mime=False,
                                )
                        except Exception:
                            continue

                        fetched = metadata.get("metadata", []) if isinstance(metadata, dict) else []
                        if not isinstance(fetched, list):
                            continue
                        for meta in fetched:
                            if len(metas_out) >= fetch_limit:
                                break
                            if not isinstance(meta, dict):
                                continue
                            metas_out.append(meta)

                if candidate_ids:
                    _fetch_chunk("file_ids", candidate_ids)
                if len(metas_out) < fetch_limit and candidate_hashes:
                    _fetch_chunk("hashes", candidate_hashes)

                return metas_out[:fetch_limit]

            def _cap_metadata_candidates(
                file_ids_in: list[int],
                hashes_in: list[str],
                *,
                requested_limit: Any,
                freeform_mode: bool = False,
                fallback_scan: bool = False,
            ) -> tuple[list[int], list[str]]:
                """Cap metadata hydration to a sane subset of Hydrus hits.

                Hydrus native tag search is fast, but fetching metadata for every
                matched file can explode for broad queries. Keep the native search,
                but only hydrate a bounded working set and let downstream filtering
                stop once enough display rows are collected.
                """

                try:
                    base_limit = int(requested_limit or 100)
                except Exception:
                    base_limit = 100
                if base_limit <= 0:
                    base_limit = 100

                hydrate_limit = base_limit
                if freeform_mode:
                    hydrate_limit = max(hydrate_limit * 4, 200)
                if fallback_scan:
                    hydrate_limit = max(hydrate_limit * 2, 200)
                hydrate_limit = min(hydrate_limit, 1000)

                ids_out = list(file_ids_in or [])
                hashes_out = list(hashes_in or [])
                total_candidates = len(ids_out) + len(hashes_out)
                if total_candidates <= hydrate_limit:
                    return ids_out, hashes_out

                debug_panel(
                    "Hydrus metadata hydration cap",
                    [
                        ("store", self.NAME),
                        ("candidates", total_candidates),
                        ("hydrate_limit", hydrate_limit),
                        ("freeform_mode", freeform_mode),
                        ("fallback_scan", fallback_scan),
                    ],
                    border_style="cyan",
                )

                if ids_out:
                    ids_out = ids_out[:hydrate_limit]
                    remaining = max(0, hydrate_limit - len(ids_out))
                    hashes_out = hashes_out[:remaining] if remaining > 0 else []
                else:
                    hashes_out = hashes_out[:hydrate_limit]

                return ids_out, hashes_out

            raw_query = str(query or "").strip()
            query_lower = raw_query.lower().strip()

            # Support `ext:<value>` anywhere in the query. We filter results by the
            # Hydrus metadata extension field.
            def _normalize_ext_filter(value: str) -> str:
                v = str(value or "").strip().lower().lstrip(".")
                v = "".join(ch for ch in v if ch.isalnum())
                return v

            ext_filter: str | None = None
            ext_only: bool = False
            try:
                m = re.search(r"\bext:([^\s,]+)", query_lower)
                if not m:
                    m = re.search(r"\bextension:([^\s,]+)", query_lower)
                if m:
                    ext_filter = _normalize_ext_filter(m.group(1)) or None
                    query_lower = re.sub(
                        r"\s*\b(?:ext|extension):[^\s,]+",
                        " ",
                        query_lower
                    )
                    query_lower = re.sub(r"\s{2,}", " ", query_lower).strip().strip(",")
                    query = query_lower
                    if ext_filter and not query_lower:
                        query = "*"
                        query_lower = "*"
                        ext_only = True
            except Exception:
                ext_filter = None
                ext_only = False

            query_lower, exclude_terms = _extract_query_excludes(query_lower)
            query = query_lower
            if exclude_terms and not query_lower:
                query = "*"
                query_lower = "*"
                if ext_filter:
                    ext_only = True
            exclude_preds = _hydrus_exclude_predicates(exclude_terms)

            # Split into meaningful terms for AND logic.
            # Avoid punctuation tokens like '-' that would make matching brittle.
            search_terms = [t for t in re.findall(r"[a-z0-9]+", query_lower) if t]

            # Special case: url:* and url:<value>
            metadata_list: list[dict[str, Any]] | None = None
            pattern_hint_raw = kwargs.get("pattern_hint")
            pattern_hints: list[str] = []
            if isinstance(pattern_hint_raw, (list, tuple, set)):
                for item in pattern_hint_raw:
                    text = str(item or "").strip().lower()
                    if text and text not in pattern_hints:
                        pattern_hints.append(text)
            elif isinstance(pattern_hint_raw, str):
                text = pattern_hint_raw.strip().lower()
                if text:
                    pattern_hints.append(text)
            pattern_hint = pattern_hints[0] if pattern_hints else ""

            hashes: list[str] = []
            file_ids: list[int] = []

            if ":" in raw_query and not raw_query.startswith(":"):
                namespace_raw, pattern_raw = raw_query.split(":", 1)
                namespace = namespace_raw.strip().lower()
                pattern = pattern_raw.strip()
                if namespace == "url":
                    try:
                        fetch_limit_raw = int(limit) if limit else 100
                    except Exception:
                        fetch_limit_raw = 100
                    if url_only:
                        metadata_list = _search_url_query_metadata(
                            f"url:{pattern}",
                            fetch_limit_raw,
                            minimal=minimal,
                        )
                    else:
                        if not pattern or pattern == "*":
                            if pattern_hints:
                                metadata_list = _iter_url_filtered_metadata(
                                    None,
                                    want_any=False,
                                    fetch_limit=fetch_limit_raw,
                                    needles=pattern_hints,
                                    minimal=minimal,
                                )
                            else:
                                metadata_list = _iter_url_filtered_metadata(
                                    None,
                                    want_any=True,
                                    fetch_limit=fetch_limit_raw,
                                    minimal=minimal,
                                )
                        else:
                            def _clean_url_search_token(value: str | None) -> str:
                                token = str(value or "").strip().lower()
                                if not token:
                                    return ""
                                return token.replace("*", "")

                            # Fast-path: exact URL via /add_urls/get_url_files when a full URL is provided.
                            exact_url_attempted = False
                            try:
                                has_wildcards = ("*" in pattern)
                                if (pattern.startswith("http://") or pattern.startswith("https://")) and not has_wildcards:
                                    exact_url_attempted = True
                                    metadata_list = self.lookup_url_metadata(pattern, minimal=minimal)
                            except Exception:
                                metadata_list = [] if exact_url_attempted else None

                            # Fallback: substring scan
                            if metadata_list is None:
                                search_token = _clean_url_search_token(pattern_hint or pattern)
                                scan_limit_override: int | None = None
                                if search_token:
                                    is_domain_only = ("://" not in search_token and "/" not in search_token)
                                    if is_domain_only:
                                        try:
                                            scan_limit_override = max(fetch_limit_raw * 20, 2000)
                                        except Exception:
                                            scan_limit_override = 2000
                                metadata_list = _iter_url_filtered_metadata(
                                    search_token,
                                    want_any=False,
                                    fetch_limit=fetch_limit_raw,
                                    scan_limit=scan_limit_override,
                                    needles=pattern_hints if pattern_hints else None,
                                    minimal=minimal,
                                )
                elif namespace == "system":
                    normalized_system_predicate = pattern.strip().lower()
                    if normalized_system_predicate == "has url":
                        try:
                            fetch_limit = int(limit) if limit else 100
                        except Exception:
                            fetch_limit = 100
                        metadata_list = _iter_url_filtered_metadata(
                            None,
                            want_any=not bool(pattern_hints),
                            fetch_limit=fetch_limit,
                            needles=pattern_hints if pattern_hints else None,
                            minimal=minimal,
                        )

            # Parse the query into tags
            # "*" means "match all" - use system:everything tag in Hydrus
            # If query has explicit namespace, use it as a tag search.
            # If query is free-form, search BOTH:
            #   - title:*term*  (title: is the only namespace searched implicitly)
            #   - *term*        (freeform tags; we will filter out other namespace matches client-side)
            tags: list[str] = []
            freeform_union_search: bool = False
            title_predicates: list[str] = []
            freeform_predicates: list[str] = []

            structured_tags = _build_structured_search_tags(raw_query)

            if query.strip() == "*":
                if ext_only and ext_filter and ext_filter in _EXT_TO_HYDRUS_FILETYPE:
                    # Translate ext:foo → system:filetype = bar for server-side filtering.
                    # This avoids fetching metadata for all files just to filter by extension.
                    hydrus_ft = _EXT_TO_HYDRUS_FILETYPE[ext_filter]
                    tags = [f"system:filetype = {hydrus_ft}"]
                    ext_only = False  # Hydrus handles the filtering; skip chunked scan path
                else:
                    tags = ["system:everything"]
                tags = tags + exclude_preds
            elif structured_tags:
                tags = structured_tags + exclude_preds
            elif ":" in query_lower:
                tags = [query_lower] + exclude_preds
            else:
                freeform_union_search = True
                if search_terms:
                    # Hydrus supports wildcard matching primarily as a prefix (e.g., tag*).
                    # Use per-term prefix matching for both title: and freeform tags.
                    title_predicates = [f"title:{term}*" for term in search_terms] + exclude_preds
                    freeform_predicates = [f"{term}*" for term in search_terms] + exclude_preds
                else:
                    title_predicates = [f"title:{query_lower}*"] + exclude_preds
                    freeform_predicates = [f"{query_lower}*"] + exclude_preds

            wildcard_globs: list[str] = []
            if tags:
                tags, wildcard_globs = _expand_wildcard_namespace_tags(tags)

            # Search files with the tags (unless url: search already produced metadata)
            results: list[dict[str, Any]] = []

            if metadata_list is None:
                file_ids = []
                hashes = []

                if freeform_union_search:
                    if not title_predicates and not freeform_predicates:
                        return []

                    payloads: list[Any] = []
                    try:
                        payloads.append(
                            client.search_files(
                                tags=title_predicates,
                                return_hashes=False,
                                return_file_ids=True,
                            )
                        )
                    except Exception:
                        pass

                    # Extra pass: match a full title phrase when the query includes
                    # spaces or punctuation (e.g., "i've been down").
                    try:
                        if query_lower and query_lower != "*" and "*" not in query_lower:
                            if any(ch in query_lower for ch in (" ", "'", "-", "_")):
                                payloads.append(
                                    client.search_files(
                                        tags=[f"title:{query_lower}*"],
                                        return_hashes=False,
                                        return_file_ids=True,
                                    )
                                )
                    except Exception:
                        pass

                    try:
                        payloads.append(
                            client.search_files(
                                tags=freeform_predicates,
                                return_hashes=False,
                                return_file_ids=True,
                            )
                        )
                    except Exception:
                        pass

                    id_set: set[int] = set()
                    for payload in payloads:
                        ids_part, _ = _extract_search_ids(payload)
                        for fid in ids_part:
                            id_set.add(fid)
                    file_ids = list(id_set)
                    hashes = []
                else:
                    if not tags:
                        return []

                    search_result = client.search_files(
                        tags=tags,
                        return_hashes=False,
                        return_file_ids=True
                    )
                    file_ids, _ = _extract_search_ids(search_result)
                    hashes = []

                # Fast path: ext-only search. Avoid fetching metadata for an unbounded
                # system:everything result set; fetch in chunks until we have enough.
                if ext_only and ext_filter:
                    results = []
                    if not file_ids and not hashes:
                        return []

                    # Prefer file_ids if available.
                    if file_ids:
                        chunk_size = 200
                        for start in range(0, len(file_ids), chunk_size):
                            if len(results) >= limit:
                                break
                            chunk = file_ids[start:start + chunk_size]
                            metas = _fetch_search_metadata(
                                file_ids=chunk,
                                include_tags=True,
                                include_urls=True,
                                include_mime=True,
                            )
                            if not metas:
                                continue
                            for meta in metas:
                                if len(results) >= limit:
                                    break
                                if not isinstance(meta, dict):
                                    continue
                                mime_type = meta.get("mime")
                                ext = _resolve_ext_from_meta(meta, mime_type)
                                if _normalize_ext_filter(ext) != ext_filter:
                                    continue

                                file_id = meta.get("file_id")
                                hash_hex = meta.get("hash")
                                size_val = meta.get("size")
                                if size_val is None:
                                    size_val = meta.get("size_bytes")
                                try:
                                    size = int(size_val) if size_val is not None else 0
                                except Exception:
                                    size = 0

                                title, all_tags = self._extract_title_and_tags(meta, file_id)
                                if exclude_terms and _text_hits_excludes(title, all_tags, exclude_terms):
                                    continue
                                if wildcard_globs and not _tags_match_globs(all_tags, wildcard_globs):
                                    continue

                                # Use known URLs (source URLs) from Hydrus if available (matches get-url cmdlet)
                                item_url = meta.get("known_urls") or meta.get("urls") or meta.get("url") or []
                                if not item_url:
                                    item_url = meta.get("file_url") or f"{self.URL.rstrip('/')}/view_file?hash={hash_hex}"
                                if isinstance(item_url, str) and "/view_file" in item_url:
                                    item_url = self._append_access_key(item_url)

                                results.append(
                                    {
                                        "hash": hash_hex,
                                        "url": item_url,
                                        "name": title,
                                        "title": title,
                                        "size": size,
                                        "size_bytes": size,
                                        "store": self.NAME,
                                        "tag": all_tags,
                                        "file_id": file_id,
                                        "mime": mime_type,
                                        "ext": _resolve_ext_from_meta(meta, mime_type),
                                    }
                                )
                        return results[:limit]

                    # If we only got hashes, fall back to the normal flow below.

                if not file_ids and not hashes:
                    return []

                file_ids, hashes = _cap_metadata_candidates(
                    file_ids,
                    hashes,
                    requested_limit=limit,
                    freeform_mode=freeform_union_search,
                )

                if file_ids:
                    metadata_list = _fetch_search_metadata(
                        file_ids=file_ids,
                        include_tags=True,
                        include_urls=True,
                        include_mime=True,
                    )
                elif hashes:
                    metadata_list = _fetch_search_metadata(
                        hashes=hashes,
                        include_tags=True,
                        include_urls=True,
                        include_mime=True,
                    )
                else:
                    metadata_list = []

                # If our free-text searches produce nothing (or nothing survived downstream filtering), fallback to scanning.
                if (not metadata_list) and (query_lower
                                            != "*") and (":" not in query_lower):
                    try:
                        search_result = client.search_files(
                            tags=["system:everything"],
                            return_hashes=False,
                            return_file_ids=True,
                        )
                        file_ids, _ = _extract_search_ids(search_result)
                        hashes = []

                        file_ids, hashes = _cap_metadata_candidates(
                            file_ids,
                            hashes,
                            requested_limit=limit,
                            freeform_mode=True,
                            fallback_scan=True,
                        )

                        if file_ids:
                            metadata_list = _fetch_search_metadata(
                                file_ids=file_ids,
                                include_tags=True,
                                include_urls=True,
                                include_mime=True,
                            )
                        elif hashes:
                            metadata_list = _fetch_search_metadata(
                                hashes=hashes,
                                include_tags=True,
                                include_urls=True,
                                include_mime=True,
                            )
                    except Exception:
                        pass

            if not isinstance(metadata_list, list):
                metadata_list = []

            for meta in metadata_list:
                if len(results) >= limit:
                    break

                file_id = meta.get("file_id")
                hash_hex = meta.get("hash")
                size_val = meta.get("size")
                if size_val is None:
                    size_val = meta.get("size_bytes")
                try:
                    size = int(size_val) if size_val is not None else 0
                except Exception:
                    size = 0

                title, all_tags = self._extract_title_and_tags(meta, file_id)

                # Prefer Hydrus-provided extension (e.g. ".webm"); fall back to MIME map.
                mime_type = meta.get("mime")
                ext = _resolve_ext_from_meta(meta, mime_type)

                # Filter results based on query type
                # If user provided explicit namespace (has ':'), don't do substring filtering
                # Just include what the tag search returned
                has_namespace = ":" in query_lower

                # Use known URLs (source URLs) from Hydrus if available (matches get-url cmdlet)
                item_url = meta.get("known_urls") or meta.get("urls") or meta.get("url") or []
                if not item_url:
                    item_url = meta.get("file_url") or f"{self.URL.rstrip('/')}/view_file?hash={hash_hex}"
                if isinstance(item_url, str) and "/view_file" in item_url:
                    item_url = self._append_access_key(item_url)

                if exclude_terms and _text_hits_excludes(title, all_tags, exclude_terms):
                    continue
                if wildcard_globs and not _tags_match_globs(all_tags, wildcard_globs):
                    continue
                if has_namespace:
                    results.append(
                        {
                            "hash": hash_hex,
                            "url": item_url,
                            "name": title,
                            "title": title,
                            "size": size,
                            "size_bytes": size,
                            "store": self.NAME,
                            "tag": all_tags,
                            "file_id": file_id,
                            "mime": mime_type,
                            "ext": ext,
                        }
                    )
                else:
                    # Free-form search: check if search terms match title or FREEFORM tags.
                    # Do NOT implicitly match other namespace tags (except title:).
                    freeform_tags = [
                        t for t in all_tags
                        if isinstance(t, str) and t and (":" not in t)
                    ]
                    searchable_text = (title + " " + " ".join(freeform_tags)).lower()

                    match = True
                    if query_lower != "*" and search_terms:
                        for term in search_terms:
                            if term not in searchable_text:
                                match = False
                                break

                    if match:
                        results.append(
                            {
                                "hash": hash_hex,
                                "url": item_url,
                                "name": title,
                                "title": title,
                                "size": size,
                                "size_bytes": size,
                                "store": self.NAME,
                                "tag": all_tags,
                                "file_id": file_id,
                                "mime": mime_type,
                                "ext": ext,
                            }
                        )
            if ext_filter:
                wanted = ext_filter
                filtered: list[dict[str, Any]] = []
                for item in results:
                    try:
                        if _normalize_ext_filter(str(item.get("ext") or "")) == wanted:
                            filtered.append(item)
                    except Exception:
                        continue
                results = filtered

            return results[:limit]

        except Exception as exc:
            try:
                from plugins.hydrusnetwork.api import is_invalid_tag_query_error

                if is_invalid_tag_query_error(exc):
                    detail = str(getattr(exc, "message", "") or exc).strip()
                    message = f"{prefix} Invalid Hydrus tag query: {detail}"
                    if detail and not detail.endswith("."):
                        message += "."
                    message += " If you intended a namespace wildcard search, use 'namespace:*'."
                    log(message, file=sys.stderr)
                    return []
            except Exception:
                pass

            log(f"❌ Hydrus search failed: {exc}", file=sys.stderr)
            import traceback

            traceback.print_exc(file=sys.stderr)
            raise

    def get_file(self, file_hash: str, **kwargs: Any) -> Path | str | None:
        """Return the local file system path if available, else a browser URL.

        IMPORTANT: this method must be side-effect free (do not auto-open a browser).
        Only explicit user actions (e.g. the get-file cmdlet) should open files.
        """
        file_hash = str(file_hash or "").strip().lower()
        try:
            debug_panel(
                "Hydrus get_file",
                [
                    ("hash", file_hash),
                    ("prefer_url", bool(kwargs.get("url"))),
                ],
                border_style="blue",
            )
        except Exception:
            pass

        # If 'url=True' is passed, we preference the browser URL even if a local path is available.
        # This is typically used by the 'get-file' cmdlet for interactive viewing.
        if kwargs.get("url"):
            base_url = str(self.URL).rstrip("/")
            access_key = str(self.API)
            browser_url = (
                f"{base_url}/get_files/file?hash={file_hash}&Hydrus-Client-API-Access-Key={access_key}"
            )
            try:
                debug_panel(
                    "Hydrus get_file",
                    [
                        ("mode", "browser-url"),
                        ("url", browser_url),
                    ],
                    border_style="blue",
                )
            except Exception:
                pass
            return browser_url

        # Try to get the local disk path if possible (works if Hydrus is on same machine)
        server_path = None
        try:
            path_res = self._client.get_file_path(file_hash)
            if isinstance(path_res, dict) and "path" in path_res:
                server_path = path_res["path"]
                if server_path:
                    local_path = Path(server_path)
                    if local_path.exists():
                        try:
                            debug_panel(
                                "Hydrus get_file",
                                [
                                    ("mode", "local-path"),
                                    ("path", local_path),
                                ],
                                border_style="green",
                            )
                        except Exception:
                            pass
                        return local_path
        except Exception as e:
            try:
                debug_panel(
                    "Hydrus get_file",
                    [
                        ("mode", "path-lookup-error"),
                        ("error", e),
                    ],
                    border_style="yellow",
                )
            except Exception:
                pass

        # If we found a path on the server but it's not locally accessible,
        # keep it for logging but continue to the browser URL fallback so the UI
        # can still open the file via the Hydrus web UI.
        if server_path:
            try:
                debug_panel(
                    "Hydrus get_file fallback",
                    [
                        ("mode", "remote-http"),
                        ("server_path", server_path),
                    ],
                    border_style="yellow",
                )
            except Exception:
                pass

        # Fallback to browser URL with access key
        base_url = str(self.URL).rstrip("/")
        access_key = str(self.API)
        browser_url = (
            f"{base_url}/get_files/file?hash={file_hash}&Hydrus-Client-API-Access-Key={access_key}"
        )
        try:
            debug_panel(
                "Hydrus get_file fallback",
                [
                    ("mode", "remote-http"),
                    ("url", browser_url),
                ],
                border_style="yellow",
            )
        except Exception:
            pass
        return browser_url

    def download_to_temp(
        self,
        file_hash: str,
        *,
        temp_root: Optional[Path] = None,
        pipeline_progress: Any = None,
        transfer_label: Optional[str] = None,
    ) -> Optional[Path]:
        """Download a Hydrus file to a temporary path for downstream uploads."""

        try:
            client = self._client
            if client is None:
                return None

            h = str(file_hash or "").strip().lower()
            if len(h) != 64 or not all(ch in "0123456789abcdef" for ch in h):
                return None

            created_tmp = False
            try:
                from SYS.utils import default_staging_dir, is_unsafe_output_dir
            except Exception:
                default_staging_dir = None  # type: ignore[assignment]
                is_unsafe_output_dir = None  # type: ignore[assignment]
            staging = None
            if callable(default_staging_dir):
                try:
                    staging = default_staging_dir()
                except Exception:
                    staging = None
            if temp_root is not None and not (callable(is_unsafe_output_dir) and is_unsafe_output_dir(temp_root)):
                base_tmp = Path(temp_root)
            else:
                created_tmp = True
                mkdtemp_dir = None
                if staging is not None and not (
                    callable(is_unsafe_output_dir) and is_unsafe_output_dir(staging)
                ):
                    mkdtemp_dir = str(staging)
                base_tmp = Path(
                    tempfile.mkdtemp(
                        prefix="hydrus-file-",
                        dir=mkdtemp_dir,
                    )
                )
            base_tmp.mkdir(parents=True, exist_ok=True)

            def _safe_filename(raw: str) -> str:
                cleaned = re.sub(r"[\\/:*?\"<>|]", "_", str(raw or "")).strip()
                if not cleaned:
                    return h
                cleaned = cleaned.strip(". ") or h
                return cleaned

            # Prefer ext/title from metadata when available.
            fname = h
            ext_val = ""
            try:
                meta = self.get_metadata(h) or {}
                if isinstance(meta, dict):
                    title_val = str(meta.get("title") or "").strip()
                    if title_val:
                        fname = _safe_filename(title_val)
                    ext_val = str(meta.get("ext") or "").strip().lstrip(".")
            except Exception:
                pass

            if not fname:
                fname = h
            if ext_val and not fname.lower().endswith(f".{ext_val.lower()}"):
                fname = f"{fname}.{ext_val}"

            def _set_status(text: str) -> None:
                if pipeline_progress is None:
                    return
                try:
                    pipeline_progress.set_status(f"{self.NAME}: {text}")
                except Exception:
                    pass

            def _clear_status() -> None:
                if pipeline_progress is None:
                    return
                try:
                    pipeline_progress.clear_status()
                except Exception:
                    pass

            try:
                file_url = client.file_url(h)
            except Exception:
                file_url = f"{self.URL.rstrip('/')}/get_files/file?hash={quote(h)}"

            dest_path = base_tmp / fname
            total_bytes: Optional[int] = None
            transfer_started = False
            completed = 0
            label = str(transfer_label or dest_path.name or fname or h).strip() or h
            stream_client = get_shared_httpx_client(timeout=600.0, verify_ssl=False)
            try:
                _set_status(f"downloading {dest_path.name}")
                with stream_client.stream(
                    "GET",
                    file_url,
                    headers={
                        "Hydrus-Client-API-Access-Key": self.API,
                        "Accept": "*/*",
                    },
                    follow_redirects=True,
                    timeout=600.0,
                ) as resp:
                    resp.raise_for_status()
                    content_type = str(resp.headers.get("content-type") or "").lower()
                    if any(
                        bad in content_type
                        for bad in ("application/json", "text/html", "text/plain", "application/cbor")
                    ):
                        raise RuntimeError(
                            f"Hydrus file download returned non-file content-type: {content_type or 'unknown'}"
                        )
                    try:
                        total_header = resp.headers.get("content-length")
                        total_bytes = int(total_header) if total_header else None
                    except Exception:
                        total_bytes = None

                    if pipeline_progress is not None:
                        try:
                            pipeline_progress.begin_transfer(label=label, total=total_bytes)
                            transfer_started = True
                        except Exception:
                            transfer_started = False

                    with dest_path.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            if not chunk:
                                continue
                            fh.write(chunk)
                            completed += len(chunk)
                            if pipeline_progress is not None:
                                try:
                                    pipeline_progress.update_transfer(
                                        label=label,
                                        completed=completed,
                                        total=total_bytes,
                                    )
                                except Exception:
                                    pass
            finally:
                if pipeline_progress is not None and transfer_started:
                    try:
                        pipeline_progress.finish_transfer(label=label)
                    except Exception:
                        pass
                _clear_status()

            if not dest_path.exists() or dest_path.stat().st_size <= 0:
                try:
                    dest_path.unlink(missing_ok=True)
                except Exception:
                    pass
                if created_tmp:
                    try:
                        shutil.rmtree(base_tmp, ignore_errors=True)
                    except Exception:
                        pass
                log(
                    f"{self._log_prefix()} download_to_temp produced empty file for hash {h[:12]}",
                    file=sys.stderr,
                )
                return None

            # Verify we received all expected bytes.
            if total_bytes is not None and total_bytes > 0 and completed != total_bytes:
                try:
                    dest_path.unlink(missing_ok=True)
                except Exception:
                    pass
                if created_tmp:
                    try:
                        shutil.rmtree(base_tmp, ignore_errors=True)
                    except Exception:
                        pass
                log(
                    f"{self._log_prefix()} download_to_temp incomplete for hash {h[:12]}: "
                    f"received {completed} of {total_bytes} bytes",
                    file=sys.stderr,
                )
                return None

            # Diagnostic: log file type magic bytes to catch download corruption.
            try:
                with dest_path.open("rb") as peek:
                    head = peek.read(16)
                    head_hex = head.hex() if len(head) >= 4 else (head.hex() if head else "empty")
                    if head[:3] == b"ID3":
                        kind = "MP3 (ID3 header)"
                    elif head[:2] == b"\xff\xfb" or head[:2] == b"\xff\xf3" or head[:2] == b"\xff\xfa":
                        kind = "MP3 (raw frame)"
                    elif head[:4] == b"\x00\x00\x00\x1cftyp":
                        kind = "MP4/M4A"
                    elif head[:4] == b"RIFF":
                        kind = "WAV"
                    elif head[:4] == b"OggS":
                        kind = "OGG"
                    elif head[:4] == b"fLaC":
                        kind = "FLAC"
                    elif head[:2] == b"\x1f\x8b":
                        kind = "GZIP"
                    elif head[:3] == b"PK\x03\x04":
                        kind = "ZIP"
                    else:
                        kind = "unknown"
                    debug(
                        f"{self._log_prefix()} downloaded {dest_path.stat().st_size} bytes, "
                        f"magic={head_hex[:24]} ({kind})"
                    )
            except Exception:
                pass

            # Verify the downloaded bytes match the expected Hydrus hash when possible.
            try:
                from SYS.utils import sha256_file

                actual_hash = str(sha256_file(dest_path) or "").strip().lower()
                if actual_hash and actual_hash != h:
                    try:
                        dest_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    if created_tmp:
                        try:
                            shutil.rmtree(base_tmp, ignore_errors=True)
                        except Exception:
                            pass
                    log(
                        f"{self._log_prefix()} download_to_temp hash mismatch "
                        f"(expected {h[:12]}..., got {actual_hash[:12]}...)",
                        file=sys.stderr,
                    )
                    return None
            except Exception as hash_exc:
                debug(f"{self._log_prefix()} download hash verify skipped: {hash_exc}")

            return dest_path
        except Exception as exc:
            log(f"{self._log_prefix()} download_to_temp failed: {exc}", file=sys.stderr)
            try:
                if created_tmp and "base_tmp" in locals():
                    shutil.rmtree(base_tmp, ignore_errors=True)  # type: ignore[arg-type]
            except Exception:
                pass
            return None

    def delete_file(self, file_identifier: str, **kwargs: Any) -> bool:
        """Delete a file from Hydrus, then clear the deletion record.

        This is used by the delete-file cmdlet when the item belongs to a HydrusNetwork store.
        """
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} delete_file: client unavailable")
                return False

            file_hash = str(file_identifier or "").strip().lower()
            if len(file_hash) != 64 or not all(ch in "0123456789abcdef"
                                               for ch in file_hash):
                debug(
                    f"{self._log_prefix()} delete_file: invalid file hash '{file_identifier}'"
                )
                return False

            reason = kwargs.get("reason")
            reason_text = (
                str(reason).strip() if isinstance(reason,
                                                  str) and reason.strip() else None
            )

            # 1) Delete file
            client.delete_files([file_hash], reason=reason_text)

            # 2) Clear deletion record (best-effort)
            try:
                client.clear_file_deletion_record([file_hash])
            except Exception as exc:
                debug(
                    f"{self._log_prefix()} delete_file: clear_file_deletion_record failed: {exc}"
                )

            return True
        except Exception as exc:
            debug(f"{self._log_prefix()} delete_file failed: {exc}")
            return False

    def delete_files_bulk(
        self,
        file_identifiers: Sequence[str],
        **kwargs: Any,
    ) -> bool:
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} delete_files_bulk: client unavailable")
                return False
            hashes: List[str] = []
            for raw in file_identifiers or []:
                file_hash = str(raw or "").strip().lower()
                if len(file_hash) == 64 and all(ch in "0123456789abcdef" for ch in file_hash):
                    hashes.append(file_hash)
            if not hashes:
                return False
            reason = kwargs.get("reason")
            reason_text = (
                str(reason).strip() if isinstance(reason, str) and reason.strip() else None
            )
            client.delete_files(hashes, reason=reason_text)
            try:
                client.clear_file_deletion_record(hashes)
            except Exception as exc:
                debug(
                    f"{self._log_prefix()} delete_files_bulk: clear_file_deletion_record failed: {exc}"
                )
            return True
        except Exception as exc:
            debug(f"{self._log_prefix()} delete_files_bulk failed: {exc}")
            return False

    def build_file_url(self, file_hash: str, *, include_access_key: bool = True) -> str:
        normalized = str(file_hash or "").strip().lower()
        base_url = str(self.URL).rstrip("/")
        url = f"{base_url}/get_files/file?hash={quote(normalized)}"
        if include_access_key and str(self.API or "").strip():
            url = f"{url}&Hydrus-Client-API-Access-Key={quote(str(self.API))}"
        return url

    def fetch_file_metadata(self, file_hash: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        try:
            client = self._client
            if client is None:
                return None
            return client.fetch_file_metadata(hashes=[str(file_hash or "").strip().lower()], **kwargs)
        except Exception:
            return None

    def fetch_files_metadata(self, file_hashes: Sequence[str], **kwargs: Any) -> Optional[Dict[str, Any]]:
        try:
            client = self._client
            if client is None:
                return None
            hashes: List[str] = []
            seen: set[str] = set()
            for raw_hash in file_hashes or []:
                hash_text = str(raw_hash or "").strip().lower()
                if not hash_text or hash_text in seen:
                    continue
                seen.add(hash_text)
                hashes.append(hash_text)
            if not hashes:
                return {"metadata": []}
            chunk_size = 200
            if len(hashes) <= chunk_size:
                return client.fetch_file_metadata(hashes=hashes, **kwargs)
            merged: List[Any] = []
            for offset in range(0, len(hashes), chunk_size):
                payload = client.fetch_file_metadata(
                    hashes=hashes[offset : offset + chunk_size],
                    **kwargs,
                )
                items = payload.get("metadata") if isinstance(payload, dict) else None
                if isinstance(items, list):
                    merged.extend(items)
            return {"metadata": merged}
        except Exception:
            return None

    def get_relationships(self, file_hash: str) -> Optional[Dict[str, Any]]:
        try:
            client = self._client
            if client is None:
                return None
            payload = client.get_file_relationships(str(file_hash or "").strip().lower())
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def get_metadata(self, file_hash: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Get metadata for a file from Hydrus by hash.

        Args:
            file_hash: SHA256 hash of the file (64-char hex string)

        Returns:
            Dict with metadata fields or None if not found
        """
        try:
            client = self._client
            if not client:
                debug(f"{self._log_prefix()} get_metadata: client unavailable")
                return None

            # Fetch file metadata with the fields we need for CLI display.
            payload = client.fetch_file_metadata(
                hashes=[file_hash],
                include_service_keys_to_tags=True,
                include_file_url=True,
                include_duration=True,
                include_size=True,
                include_mime=True,
            )

            if not payload or not payload.get("metadata"):
                return None

            meta = payload["metadata"][0]

            # Hydrus can return placeholder metadata rows for unknown hashes.
            if not isinstance(meta, dict) or meta.get("file_id") is None:
                return None

            # Extract title from tags
            title = f"Hydrus_{file_hash[:12]}"
            extracted_tags = self._extract_tags_from_hydrus_meta(
                meta,
                service_key=None,
                service_name=None,
            )
            for raw_tag in extracted_tags:
                tag_text = str(raw_tag or "").strip()
                if not tag_text:
                    continue
                if tag_text.lower().startswith("title:"):
                    value = tag_text.split(":", 1)[1].strip()
                    if value:
                        title = value
                        break

            # Hydrus may return mime as an int enum, or sometimes a human label.
            mime_val = meta.get("mime")
            filetype_human = (
                meta.get("filetype_human") or meta.get("mime_human")
                or meta.get("mime_string")
            )

            # Determine ext: prefer Hydrus metadata ext, then filetype_human (when it looks like an ext),
            # then title suffix, then file path suffix.
            ext = str(meta.get("ext") or "").strip().lstrip(".")
            if not ext:
                ft = str(filetype_human or "").strip().lstrip(".").lower()
                if ft and ft != "unknown filetype" and ft.isalnum() and len(ft) <= 8:
                    # Treat simple labels like "mp4", "m4a", "webm" as extensions.
                    ext = ft
            if not ext and isinstance(title, str) and "." in title:
                try:
                    ext = Path(title).suffix.lstrip(".")
                except Exception:
                    ext = ""
            if not ext:
                try:
                    path_payload = client.get_file_path(file_hash)
                    if isinstance(path_payload, dict):
                        p = path_payload.get("path")
                        if isinstance(p, str) and p.strip():
                            ext = Path(p.strip()).suffix.lstrip(".")
                except Exception:
                    ext = ""

            # If extension is still unknown, attempt a best-effort lookup from MIME.
            def _mime_from_ext(ext_value: str) -> str:
                ext_clean = str(ext_value or "").strip().lstrip(".").lower()
                if not ext_clean:
                    return ""
                try:
                    for category in mime_maps.values():
                        info = category.get(ext_clean)
                        if isinstance(info, dict):
                            mimes = info.get("mimes")
                            if isinstance(mimes, list) and mimes:
                                first = mimes[0]
                                return str(first)
                except Exception:
                    return ""
                return ""

            # Normalize to a MIME string for CLI output.
            # Avoid passing through human labels like "unknown filetype".
            mime_type = ""
            if isinstance(mime_val, str):
                candidate = mime_val.strip()
                if "/" in candidate and candidate.lower() != "unknown filetype":
                    mime_type = candidate
            if not mime_type and isinstance(filetype_human, str):
                candidate = filetype_human.strip()
                if "/" in candidate and candidate.lower() != "unknown filetype":
                    mime_type = candidate
            if not mime_type:
                mime_type = _mime_from_ext(ext)

            # Normalize size/duration to stable scalar types.
            size_val = meta.get("size")
            if size_val is None:
                size_val = meta.get("size_bytes")
            size_int: int | None = None
            if size_val is not None:
                try:
                    parsed = int(size_val)
                    size_int = parsed if parsed > 0 else None
                except Exception:
                    size_int = None

            dur_val = meta.get("duration")
            if dur_val is None:
                dur_val = meta.get("duration_ms")
            try:
                dur_int: int | None = int(dur_val) if dur_val is not None else None
            except Exception:
                dur_int = None

            raw_urls = meta.get("known_urls") or meta.get("urls") or meta.get("url"
                                                                              ) or []
            url_list: list[str] = []
            if isinstance(raw_urls, str):
                s = raw_urls.strip()
                url_list = [s] if s else []
            elif isinstance(raw_urls, list):
                url_list = [
                    str(u).strip() for u in raw_urls
                    if isinstance(u, str) and str(u).strip()
                ]

            return {
                "hash": file_hash,
                "title": title,
                "ext": ext,
                "size": size_int,
                "mime": mime_type,
                "hydrus_mime": mime_val,
                "filetype_human": filetype_human,
                "duration_ms": dur_int,
                "url": url_list,
                "tag": list(extracted_tags),
            }

        except Exception as exc:
            debug(f"{self._log_prefix()} get_metadata failed: {exc}")
            return None

    def get_tag(self, file_identifier: str, **kwargs: Any) -> Tuple[List[str], str]:
        """Get tags for a file from Hydrus by hash.

        Args:
            file_identifier: File hash (SHA256 hex string)
            **kwargs: Optional service_name parameter

        Returns:
            Tuple of (tags_list, source_description)
            where source is always "hydrus"
        """
        try:
            file_hash = str(file_identifier or "").strip().lower()
            if len(file_hash) != 64 or not all(ch in "0123456789abcdef"
                                               for ch in file_hash):
                debug(
                    f"{self._log_prefix()} get_tags: invalid file hash '{file_identifier}'"
                )
                return [], "unknown"

            # Get Hydrus client and service info
            client = self._client
            if not client:
                debug(f"{self._log_prefix()} get_tags: client unavailable")
                return [], "unknown"

            # Fetch file metadata
            payload = client.fetch_file_metadata(
                hashes=[file_hash],
                include_service_keys_to_tags=True,
                include_file_url=True
            )

            items = payload.get("metadata") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                debug(
                    f"{self._log_prefix()} get_tags: no metadata for hash {file_hash}"
                )
                return [], "unknown"

            meta = items[0] if isinstance(items[0], dict) else None
            if not isinstance(meta, dict) or meta.get("file_id") is None:
                debug(
                    f"{self._log_prefix()} get_tags: invalid metadata for hash {file_hash}"
                )
                return [], "unknown"

            service_name = kwargs.get("service_name") or "my tags"
            service_key = self._get_service_key(service_name)

            # Extract tags from metadata
            tags = self._extract_tags_from_hydrus_meta(meta, service_key, service_name)

            return [
                str(t).strip().lower() for t in tags if isinstance(t, str) and t.strip()
            ], "hydrus"

        except Exception as exc:
            debug(f"{self._log_prefix()} get_tags failed: {exc}")
            return [], "unknown"

    def _local_tag_services(self) -> List[tuple[str, str]]:
        client = self._client
        if client is None:
            return []
        try:
            payload = client.get_services()
        except Exception:
            return []
        rows: List[tuple[str, str]] = []
        local = payload.get("local_tags") if isinstance(payload, dict) else None
        if not isinstance(local, list):
            return rows
        from plugins.hydrusnetwork.api import normalize_hydrus_service_key

        for item in local:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            key_raw = item.get("service_key") or item.get("key")
            key = normalize_hydrus_service_key(key_raw) or str(key_raw or "").strip()
            if name and key:
                rows.append((name, key))
        return rows

    def _replace_namespace_local_services(
        self,
        file_hash: str,
        namespace: str,
        new_tag: str,
        *,
        target_service: str = "my tags",
    ) -> bool:
        prefix = str(namespace or "").strip().lower() + ":"
        new_text = str(new_tag or "").strip().lower()
        if prefix == ":" or not new_text.startswith(prefix):
            return False
        existing, _src = self.get_tag(file_hash)
        old = [
            str(tag).strip().lower()
            for tag in existing
            if str(tag).strip().lower().startswith(prefix)
        ]
        client = self._client
        if client is None:
            return False
        target_name = str(target_service or "my tags").strip().lower() or "my tags"
        target_key = self._get_service_key(target_service)
        did_any = False
        for name, key in self._local_tag_services():
            is_target = str(name or "").strip().lower() == target_name
            if target_key and key:
                is_target = is_target or str(key).strip().lower() == str(target_key).strip().lower()
            to_remove = [tag for tag in old if (tag != new_text) or not is_target]
            try:
                if is_target:
                    client.mutate_tags_by_key(
                        file_hash,
                        key,
                        add_tags=[new_text],
                        remove_tags=to_remove or None,
                    )
                elif to_remove:
                    client.mutate_tags_by_key(
                        file_hash,
                        key,
                        remove_tags=to_remove,
                    )
                did_any = True
            except Exception:
                try:
                    if to_remove:
                        client.delete_tag(file_hash, to_remove, name)
                    if is_target:
                        client.add_tag(file_hash, [new_text], name)
                    did_any = True
                except Exception as exc:
                    debug(
                        f"{self._log_prefix()} replace {namespace}: failed on {name}: {exc}"
                    )
        return did_any

    def add_tag(self, file_identifier: str, tags: List[str], **kwargs: Any) -> bool:
        """Add tags to a Hydrus file."""
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} add_tag: client unavailable")
                return False

            file_hash = str(file_identifier or "").strip().lower()
            if len(file_hash) != 64 or not all(ch in "0123456789abcdef"
                                               for ch in file_hash):
                debug(
                    f"{self._log_prefix()} add_tag: invalid file hash '{file_identifier}'"
                )
                return False
            service_name = kwargs.get("service_name") or "my tags"

            from SYS.metadata import ensure_single_title_tag

            incoming_tags = [
                str(t).strip().lower() for t in (tags or [])
                if isinstance(t, str) and str(t).strip()
            ]
            incoming_tags = [
                str(t).strip().lower()
                for t in ensure_single_title_tag(incoming_tags)
                if str(t).strip()
            ]
            if not incoming_tags:
                return True

            title_incoming = [t for t in incoming_tags if t.startswith("title:")]
            title_replaced = False
            if title_incoming:
                title_replaced = self._replace_namespace_local_services(
                    file_hash,
                    "title",
                    title_incoming[-1],
                    target_service=str(service_name),
                )
                incoming_tags = [t for t in incoming_tags if not t.startswith("title:")]
                if not incoming_tags:
                    return bool(title_replaced)

            existing_tags = kwargs.get("existing_tags")
            if existing_tags is None:
                try:
                    existing_tags, _src = self.get_tag(file_hash)
                except Exception:
                    existing_tags = []
            if isinstance(existing_tags, (list, tuple, set)):
                existing_tags = [
                    str(t).strip().lower() for t in existing_tags
                    if isinstance(t, str) and str(t).strip()
                ]
            else:
                existing_tags = []

            from SYS.metadata import compute_namespaced_tag_overwrite

            tags_to_remove, tags_to_add, _merged = compute_namespaced_tag_overwrite(
                existing_tags, incoming_tags
            )

            if not tags_to_add and not tags_to_remove:
                return True

            service_key: Optional[str] = None
            service_key = self._get_service_key(service_name)

            mutate_success = False
            if service_key:
                try:
                    client.mutate_tags_by_key(
                        file_hash,
                        service_key,
                        add_tags=tags_to_add,
                        remove_tags=tags_to_remove,
                    )
                    mutate_success = True
                except Exception as exc:
                    debug(
                        f"{self._log_prefix()} add_tag: mutate_tags_by_key failed: {exc}"
                    )

            did_any = False
            if not mutate_success:
                if tags_to_remove:
                    try:
                        client.delete_tag(file_hash, tags_to_remove, service_name)
                        did_any = True
                    except Exception as exc:
                        debug(
                            f"{self._log_prefix()} add_tag: delete_tag failed: {exc}"
                        )
                if tags_to_add:
                    try:
                        client.add_tag(file_hash, tags_to_add, service_name)
                        did_any = True
                    except Exception as exc:
                        debug(
                            f"{self._log_prefix()} add_tag: add_tag failed: {exc}"
                        )
            else:
                did_any = bool(tags_to_add or tags_to_remove)

            return did_any
        except Exception as exc:
            debug(f"{self._log_prefix()} add_tag failed: {exc}")
            return False

    def delete_tag(self, file_identifier: str, tags: List[str], **kwargs: Any) -> bool:
        """Delete tags from a Hydrus file."""
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} delete_tag: client unavailable")
                return False

            file_hash = str(file_identifier or "").strip().lower()
            if len(file_hash) != 64 or not all(ch in "0123456789abcdef"
                                               for ch in file_hash):
                debug(
                    f"{self._log_prefix()} delete_tag: invalid file hash '{file_identifier}'"
                )
                return False
            service_name = kwargs.get("service_name") or "my tags"
            raw_list = list(tags) if isinstance(tags, (list, tuple)) else [str(tags)]
            tag_list = [
                str(t).strip().lower() for t in raw_list
                if isinstance(t, str) and str(t).strip()
            ]
            if not tag_list:
                return False
            client.delete_tag(file_hash, tag_list, service_name)
            return True
        except Exception as exc:
            debug(f"{self._log_prefix()} delete_tag failed: {exc}")
            return False

    def delete_tags_bulk(self, items: List[Any], *, service_name: str | None = None) -> bool:
        """Bulk delete tags from multiple Hydrus files.

        Each item may be a 2-tuple ``(hash, remove_tags)``.  Items are grouped
        by their canonical remove_tags set so that all files with the same
        deletion are sent in a single ``mutate_tags_by_key`` request.
        Falls back to one batched ``delete_tag`` call per group when no service
        key is available.
        """
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} delete_tags_bulk: client unavailable")
                return False

            # Group by canonical remove_tags tuple
            buckets: dict[tuple[str, ...], list[str]] = {}
            for item in items or []:
                file_identifier, remove_tags = item[0], item[1]
                h = str(file_identifier or "").strip().lower()
                if len(h) != 64:
                    continue
                remove_list = [
                    str(t).strip().lower() for t in (remove_tags or [])
                    if isinstance(t, str) and str(t).strip()
                ]
                if not remove_list:
                    continue
                key = tuple(sorted(remove_list))
                buckets.setdefault(key, []).append(h)

            if not buckets:
                return False

            svc = service_name or "my tags"
            service_key = self._get_service_key(svc)
            any_success = False

            for remove_tuple, hashes in buckets.items():
                if service_key:
                    try:
                        try:
                            client.mutate_tags_by_key(
                                hashes,
                                service_key,
                                remove_tags=list(remove_tuple),
                            )
                        except TypeError:
                            client.mutate_tags_by_key(
                                hash=hashes,
                                service_key=service_key,
                                remove_tags=list(remove_tuple),
                            )
                        any_success = True
                        continue
                    except Exception as exc:
                        debug(f"{self._log_prefix()} delete_tags_bulk mutate failed: {exc}")

                # Batched fallback: client.delete_tag() accepts a list of hashes
                try:
                    client.delete_tag(list(hashes), list(remove_tuple), svc)
                    any_success = True
                except Exception as exc:
                    debug(f"{self._log_prefix()} delete_tags_bulk fallback failed: {exc}")

            return any_success
        except Exception as exc:
            debug(f"{self._log_prefix()} delete_tags_bulk failed: {exc}")
            return False

    def get_url(self, file_identifier: str, **kwargs: Any) -> List[str]:
        """Get known url for a Hydrus file."""
        try:
            client = self._client

            file_hash = str(file_identifier or "").strip().lower()
            if len(file_hash) != 64 or not all(ch in "0123456789abcdef"
                                               for ch in file_hash):
                return []

            payload = client.fetch_file_metadata(
                hashes=[file_hash],
                include_file_url=True
            )
            items = payload.get("metadata") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                return []
            meta = items[0] if isinstance(items[0],
                                          dict) else {}

            raw_urls: Any = meta.get("known_urls"
                                     ) or meta.get("urls") or meta.get("url") or []
            
            def _is_url(s: Any) -> bool:
                if not isinstance(s, str):
                    return False
                v = s.strip().lower()
                return bool(v and ("://" in v or v.startswith(("magnet:", "torrent:"))))

            if isinstance(raw_urls, str):
                val = raw_urls.strip()
                return [val] if _is_url(val) else []
            if isinstance(raw_urls, list):
                out: list[str] = []
                for u in raw_urls:
                    if not isinstance(u, str):
                        continue
                    u = u.strip()
                    if u and _is_url(u):
                        out.append(u)
                return out
            return []
        except Exception as exc:
            debug(f"{self._log_prefix()} get_url failed: {exc}")
            return []

    def lookup_url_metadata(self, url_value: str, *, minimal: bool = False) -> List[Dict[str, Any]]:
        """Resolve an exact URL to Hydrus metadata using /add_urls/get_url_files variants."""
        candidate_url = str(url_value or "").strip()
        if not candidate_url:
            return []

        client = self._client
        if client is None:
            return []

        try:
            from plugins.hydrusnetwork.api import HydrusRequestSpec, generate_hydrus_url_variants
        except Exception:
            return []

        pending: deque[str] = deque([candidate_url])
        fallback_variants: deque[str] = deque()
        try:
            for variant in generate_hydrus_url_variants(candidate_url) or []:
                variant_text = str(variant or "").strip()
                if not variant_text or variant_text == candidate_url:
                    continue
                fallback_variants.append(variant_text)
        except Exception:
            fallback_variants = deque()
        seen_urls: set[str] = set()
        file_ids: List[int] = []
        hashes: List[str] = []
        seen_ids: set[int] = set()
        seen_hashes: set[str] = set()
        saw_response_canonical = False

        while pending:
            current = str(pending.popleft() or "").strip()
            if not current or current in seen_urls:
                continue
            seen_urls.add(current)

            try:
                response = client._perform_request(
                    HydrusRequestSpec(
                        method="GET",
                        endpoint="/add_urls/get_url_files",
                        query={"url": current},
                    )
                )
            except Exception:
                continue

            if not isinstance(response, dict):
                continue

            raw_hashes = response.get("hashes") or response.get("file_hashes")
            if isinstance(raw_hashes, list):
                for item in raw_hashes:
                    try:
                        file_hash = str(item or "").strip().lower()
                    except Exception:
                        continue
                    if not file_hash or file_hash in seen_hashes:
                        continue
                    seen_hashes.add(file_hash)
                    hashes.append(file_hash)

            raw_ids = response.get("file_ids") or response.get("file_id")
            id_values = raw_ids if isinstance(raw_ids, list) else [raw_ids] if raw_ids is not None else []
            for item in id_values:
                try:
                    file_id = int(item)
                except (TypeError, ValueError):
                    continue
                if file_id in seen_ids:
                    continue
                seen_ids.add(file_id)
                file_ids.append(file_id)

            statuses = response.get("url_file_statuses")
            if isinstance(statuses, list):
                for entry in statuses:
                    if not isinstance(entry, dict):
                        continue

                    status_hash = entry.get("hash") or entry.get("file_hash")
                    try:
                        normalized_hash = str(status_hash or "").strip().lower()
                    except Exception:
                        normalized_hash = ""
                    if normalized_hash and normalized_hash not in seen_hashes:
                        seen_hashes.add(normalized_hash)
                        hashes.append(normalized_hash)

                    status_id = entry.get("file_id") or entry.get("fileid")
                    try:
                        file_id = int(status_id) if status_id is not None else None
                    except (TypeError, ValueError):
                        file_id = None
                    if file_id is None or file_id in seen_ids:
                        continue
                    seen_ids.add(file_id)
                    file_ids.append(file_id)

            for key in ("normalised_url", "normalized_url", "redirect_url", "url"):
                value = response.get(key)
                if isinstance(value, str):
                    next_url = value.strip()
                    if next_url and key != "url":
                        saw_response_canonical = True
                    if next_url and next_url not in seen_urls:
                        pending.append(next_url)

            if not pending and not file_ids and not hashes and not saw_response_canonical and fallback_variants:
                while fallback_variants:
                    fallback = str(fallback_variants.popleft() or "").strip()
                    if not fallback or fallback in seen_urls:
                        continue
                    pending.append(fallback)

            if minimal and (file_ids or hashes):
                break

        if not file_ids and not hashes:
            return []

        try:
            payload = client.fetch_file_metadata(
                file_ids=file_ids or None,
                hashes=hashes or None,
                include_file_url=True,
                include_service_keys_to_tags=not minimal,
                include_duration=not minimal,
                include_size=not minimal,
                include_mime=not minimal,
            )
        except Exception:
            return []

        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(metadata, list):
            return []
        return [entry for entry in metadata if isinstance(entry, dict)]

    def find_hashes_by_url(self, url_value: str) -> List[str]:
        hashes: List[str] = []
        seen: set[str] = set()
        for entry in self.lookup_url_metadata(url_value, minimal=True):
            raw_hash = entry.get("hash") or entry.get("hash_hex") or entry.get("file_hash")
            try:
                file_hash = str(raw_hash or "").strip().lower()
            except Exception:
                continue
            if len(file_hash) != 64 or file_hash in seen:
                continue
            # Ignore URL associations that point at hashes without local file bytes
            # (orphaned metadata from earlier failed imports).
            if not self.has_file(file_hash):
                continue
            seen.add(file_hash)
            hashes.append(file_hash)
        return hashes

    def get_url_info(self, url: str, **kwargs: Any) -> dict[str, Any] | None:
        """Return Hydrus URL info for a single URL (Hydrus-only helper).

        Uses: GET /add_urls/get_url_info
        """
        try:
            client = self._client
            if client is None:
                return None
            u = str(url or "").strip()
            if not u:
                return None
            try:
                return client.get_url_info(u)  # type: ignore[attr-defined]
            except Exception:
                from plugins.hydrusnetwork.api import HydrusRequestSpec

                spec = HydrusRequestSpec(
                    method="GET",
                    endpoint="/add_urls/get_url_info",
                    query={
                        "url": u
                    },
                )
                response = client._perform_request(spec)  # type: ignore[attr-defined]
                return response if isinstance(response, dict) else None
        except Exception as exc:
            debug(f"{self._log_prefix()} get_url_info failed: {exc}")
            return None

    def add_url(self, file_identifier: str, url: List[str], **kwargs: Any) -> bool:
        """Associate one or more url with a Hydrus file."""
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} add_url: client unavailable")
                return False
            for u in url:
                client.associate_url(file_identifier, u)
            return True
        except Exception as exc:
            debug(f"{self._log_prefix()} add_url failed: {exc}")
            return False

    def add_url_bulk(self, items: List[tuple[str, List[str]]], **kwargs: Any) -> bool:
        """Bulk associate urls with Hydrus files.

        This is a best-effort convenience wrapper used by cmdlets to batch url associations.
        Hydrus' client API is still called per (hash,url) pair, but this consolidates the
        cmdlet-level control flow so url association can be deferred until the end.
        """
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} add_url_bulk: client unavailable")
                return False

            any_success = False
            for file_identifier, urls in items or []:
                h = str(file_identifier or "").strip().lower()
                if len(h) != 64:
                    continue
                for u in urls or []:
                    s = str(u or "").strip()
                    if not s:
                        continue
                    try:
                        client.associate_url(h, s)
                        any_success = True
                    except Exception:
                        continue
            return any_success
        except Exception as exc:
            debug(f"{self._log_prefix()} add_url_bulk failed: {exc}")
            return False

    def add_tags_bulk(self, items: List[Any], *, service_name: str | None = None) -> bool:
        """Bulk add (and optionally remove) tags across multiple Hydrus files.

        Each item may be a 2-tuple ``(hash, add_tags)`` or a 3-tuple
        ``(hash, add_tags, remove_tags)``.  Items are grouped by their canonical
        (add_tags, remove_tags) combination so that all files with the same
        mutation are sent in a single ``mutate_tags_by_key`` request.
        Falls back to per-hash ``add_tag`` calls if necessary.
        """
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} add_tags_bulk: client unavailable")
                return False

            # Group by canonical (add, remove) pair to batch identical mutations
            # bucket key: (sorted_add_tuple, sorted_remove_tuple)
            buckets: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
            for item in items or []:
                if len(item) == 3:
                    file_identifier, add_tags, remove_tags = item
                else:
                    file_identifier, add_tags = item
                    remove_tags = []
                h = str(file_identifier or "").strip().lower()
                if len(h) != 64:
                    continue
                add_list = [str(t).strip().lower() for t in (add_tags or []) if isinstance(t, str) and str(t).strip()]
                remove_list = [str(t).strip().lower() for t in (remove_tags or []) if isinstance(t, str) and str(t).strip()]
                if not add_list and not remove_list:
                    continue
                key = (tuple(sorted(add_list)), tuple(sorted(remove_list)))
                buckets.setdefault(key, []).append(h)

            if not buckets:
                return False

            svc = service_name or "my tags"
            service_key = self._get_service_key(svc)
            any_success = False

            for (add_tuple, remove_tuple), hashes in buckets.items():
                if any(str(t).startswith("title:") for t in add_tuple):
                    for file_hash in hashes:
                        try:
                            if self.add_tag(file_hash, list(add_tuple), service_name=svc):
                                any_success = True
                        except Exception as exc:
                            debug(f"{self._log_prefix()} add_tags_bulk title replace failed: {exc}")
                    continue
                # Prefer mutate_tags_by_key when a service key is available: supports
                # both adds and removes in one request and accepts a list of hashes.
                if service_key:
                    try:
                        try:
                            client.mutate_tags_by_key(
                                hashes,
                                service_key,
                                add_tags=list(add_tuple) or None,
                                remove_tags=list(remove_tuple) or None,
                            )
                        except TypeError:
                            client.mutate_tags_by_key(
                                hash=hashes,
                                service_key=service_key,
                                add_tags=list(add_tuple) or None,
                                remove_tags=list(remove_tuple) or None,
                            )
                        any_success = True
                        continue
                    except Exception as exc:
                        debug(f"{self._log_prefix()} add_tags_bulk mutate failed for adds={add_tuple} removes={remove_tuple}: {exc}")

                # Batched fallback: client.add_tag()/delete_tag() accept a list of hashes.
                # This still results in a single API request regardless of hash count.
                if add_tuple:
                    try:
                        client.add_tag(list(hashes), list(add_tuple), svc)
                        any_success = True
                    except Exception as exc:
                        debug(f"{self._log_prefix()} add_tags_bulk fallback add_tag failed: {exc}")
                if remove_tuple:
                    try:
                        client.delete_tag(list(hashes), list(remove_tuple), svc)
                        any_success = True
                    except Exception as exc:
                        debug(f"{self._log_prefix()} add_tags_bulk fallback delete_tag failed: {exc}")

            return any_success
        except Exception as exc:
            debug(f"{self._log_prefix()} add_tags_bulk failed: {exc}")
            return False

    def delete_url(self, file_identifier: str, url: List[str], **kwargs: Any) -> bool:
        """Delete one or more url from a Hydrus file."""
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} delete_url: client unavailable")
                return False
            for u in url:
                client.delete_url(file_identifier, u)
            return True
        except Exception as exc:
            debug(f"{self._log_prefix()} delete_url failed: {exc}")
            return False

    def get_note(self, file_identifier: str, **kwargs: Any) -> Dict[str, str]:
        """Get notes for a Hydrus file (default note service only)."""
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} get_note: client unavailable")
                return {}

            file_hash = str(file_identifier or "").strip().lower()
            if len(file_hash) != 64 or not all(ch in "0123456789abcdef"
                                               for ch in file_hash):
                return {}

            payload = client.fetch_file_metadata(hashes=[file_hash], include_notes=True)
            items = payload.get("metadata") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                return {}
            meta = items[0] if isinstance(items[0], dict) else None
            if not isinstance(meta, dict):
                return {}

            notes_payload = meta.get("notes")
            if isinstance(notes_payload, dict):
                return {
                    str(k): str(v or "")
                    for k, v in notes_payload.items() if str(k).strip()
                }

            return {}
        except Exception as exc:
            debug(f"{self._log_prefix()} get_note failed: {exc}")
            return {}

    def set_note(
        self,
        file_identifier: str,
        name: str,
        text: str,
        **kwargs: Any
    ) -> bool:
        """Set a named note for a Hydrus file (default note service only)."""
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} set_note: client unavailable")
                return False

            file_hash = str(file_identifier or "").strip().lower()
            if len(file_hash) != 64 or not all(ch in "0123456789abcdef"
                                               for ch in file_hash):
                return False

            note_name = str(name or "").strip()
            if not note_name:
                return False
            note_text = str(text or "")

            client.set_notes(file_hash,
                             {
                                 note_name: note_text
                             })
            return True
        except Exception as exc:
            debug(f"{self._log_prefix()} set_note failed: {exc}")
            return False

    def delete_note(self, file_identifier: str, name: str, **kwargs: Any) -> bool:
        """Delete a named note for a Hydrus file (default note service only)."""
        try:
            client = self._client
            if client is None:
                debug(f"{self._log_prefix()} delete_note: client unavailable")
                return False

            file_hash = str(file_identifier or "").strip().lower()
            if len(file_hash) != 64 or not all(ch in "0123456789abcdef"
                                               for ch in file_hash):
                return False

            note_name = str(name or "").strip()
            if not note_name:
                return False

            client.delete_notes(file_hash, [note_name])
            return True
        except Exception as exc:
            debug(f"{self._log_prefix()} delete_note failed: {exc}")
            return False

    @staticmethod
    def _extract_tags_from_hydrus_meta(
        meta: Dict[str,
                   Any],
        service_key: Optional[str],
        service_name: Optional[str]
    ) -> List[str]:
        """Extract current tags from Hydrus metadata dict.

        Prefers display_tags (includes siblings/parents, excludes deleted).
        Falls back to storage_tags status '0' (current).
        """
        tags_payload = meta.get("tags")
        if not isinstance(tags_payload, Mapping):
            return []

        desired_service_name = str(service_name or "").strip().lower()
        desired_service_key = str(service_key).strip() if service_key is not None else ""

        def _append_tag(out: List[str], value: Any) -> None:
            text = ""
            if isinstance(value, bytes):
                try:
                    text = value.decode("utf-8", errors="ignore")
                except Exception:
                    text = str(value)
            elif isinstance(value, str):
                text = value
            if not text:
                return
            cleaned = text.strip()
            if cleaned:
                out.append(cleaned)

        def _collect_current(container: Any, out: List[str]) -> None:
            if isinstance(container, SequenceABC) and not isinstance(container, (str, bytes, bytearray, Mapping)):
                for tag in container:
                    _append_tag(out, tag)
                return
            if isinstance(container, Mapping):
                current = container.get("0")
                if current is None:
                    current = container.get(0)
                if isinstance(current, SequenceABC) and not isinstance(current, (str, bytes, bytearray, Mapping)):
                    for tag in current:
                        _append_tag(out, tag)
                elif isinstance(current, Mapping):
                    _collect_current(current, out)
                status_keys = {"0", "1", "2", "3", 0, 1, 2, 3}
                for ns_name, tag_list in container.items():
                    if ns_name in status_keys:
                        continue
                    ns_text = str(ns_name or "").strip()
                    if isinstance(tag_list, Mapping):
                        _collect_current(tag_list, out)
                        continue
                    if not isinstance(tag_list, SequenceABC) or isinstance(tag_list, (str, bytes, bytearray)):
                        text = str(tag_list or "").strip()
                        if not text:
                            continue
                        if ":" in text or not ns_text:
                            _append_tag(out, text)
                        else:
                            _append_tag(out, f"{ns_text}:{text}")
                        continue
                    for tag_item in tag_list:
                        text = str(tag_item or "").strip()
                        if not text:
                            continue
                        if ":" in text or not ns_text:
                            _append_tag(out, text)
                        else:
                            _append_tag(out, f"{ns_text}:{text}")

        def _collect_service_data(service_data: Any, out: List[str]) -> None:
            if not isinstance(service_data, Mapping):
                return

            display = (
                service_data.get("display_tags")
                or service_data.get("display_friendly_tags")
                or service_data.get("display")
            )
            _collect_current(display, out)

            storage = (
                service_data.get("storage_tags")
                or service_data.get("statuses_to_tags")
                or service_data.get("tags")
            )
            _collect_current(storage, out)

        collected: List[str] = []

        if desired_service_key:
            _collect_service_data(tags_payload.get(desired_service_key), collected)

        if not collected and desired_service_name:
            for maybe_service in tags_payload.values():
                if not isinstance(maybe_service, Mapping):
                    continue
                svc_name = str(
                    maybe_service.get("service_name")
                    or maybe_service.get("name")
                    or ""
                ).strip().lower()
                if svc_name and svc_name == desired_service_name:
                    _collect_service_data(maybe_service, collected)

        names_map = tags_payload.get("service_keys_to_names")
        statuses_map = tags_payload.get("service_keys_to_statuses_to_tags")
        if isinstance(statuses_map, Mapping):
            keys_to_collect: List[str] = []
            if desired_service_key:
                keys_to_collect.append(desired_service_key)
            if desired_service_name and isinstance(names_map, Mapping):
                for raw_key, raw_name in names_map.items():
                    if str(raw_name or "").strip().lower() == desired_service_name:
                        keys_to_collect.append(str(raw_key))
            keys_filter = {k for k in keys_to_collect if k}

            for raw_key, status_payload in statuses_map.items():
                raw_key_text = str(raw_key)
                if keys_filter and raw_key_text not in keys_filter:
                    continue
                _collect_current(status_payload, collected)

        if not collected:
            for maybe_service in tags_payload.values():
                _collect_service_data(maybe_service, collected)

        top_level_tags = meta.get("tags_flat")
        if isinstance(top_level_tags, SequenceABC) and not isinstance(top_level_tags, (str, bytes, bytearray, Mapping)):
            _collect_current(top_level_tags, collected)

        deduped: List[str] = []
        seen: set[str] = set()
        for tag in collected:
            key = str(tag).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(tag)
        return deduped

    @staticmethod
    def _extract_title_and_tags(meta: Dict[str, Any], file_id: Any) -> Tuple[str, List[str]]:
        title = f"Hydrus File {file_id}"
        tags = HydrusStoreOperations._extract_tags_from_hydrus_meta(
            meta,
            service_key=None,
            service_name=None,
        )

        normalized_tags: List[str] = []
        seen: set[str] = set()
        for raw_tag in tags:
            text = str(raw_tag or "").strip().lower()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized_tags.append(text)
            if text.startswith("title:") and title == f"Hydrus File {file_id}":
                value = text.split(":", 1)[1].strip()
                if value:
                    title = value

        return title, normalized_tags


HydrusNetwork = HydrusStoreOperations
