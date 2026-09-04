from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Callable, Tuple
from urllib.parse import urlparse

from API.HTTP import HTTPClient, download_direct_file
from plugins.alldebrid.api import AllDebridClient, parse_magnet_or_hash, is_torrent_file
from PluginCore.base import Plugin, SearchResult
from SYS.plugin_helpers import TablePluginMixin
from SYS.item_accessors import get_field as _extract_value
from SYS.utils import sanitize_filename
from SYS.logger import log
from SYS.models import DownloadError, PipeObject

_HOSTS_CACHE_TTL_SECONDS = 24 * 60 * 60
_RUNTIME_HOSTS_PAYLOAD: Optional[Dict[str, Any]] = None
_RUNTIME_HOSTS_FETCHED_AT: float = 0.0


def _plugin_dir() -> Path:
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path(".")


def _legacy_hosts_cache_paths() -> Tuple[Path, ...]:
    try:
        repo_root = Path(__file__).resolve().parents[2]
        plugins_root = Path(__file__).resolve().parents[1]
    except Exception:
        return tuple()
    return (
        plugins_root / "API" / "data" / "alldebrid.json",
        repo_root / "API" / "data" / "alldebrid.json",
    )


def _hosts_cache_path() -> Path:
    # Keep this local to the plugin so plugin-specific cache/state stays bundled
    # with the plugin itself in portable installs.
    #
    # This file is expected to be the JSON payload shape from AllDebrid:
    #   {"status":"success","data":{"hosts":[...],"streams":[...],"redirectors":[...]}}
    return _plugin_dir() / "alldebrid.json"


def _resolve_hosts_cache_path() -> Path:
    path = _hosts_cache_path()
    try:
        if path.exists() and path.is_file():
            return path
    except Exception:
        return path

    for legacy in _legacy_hosts_cache_paths():
        try:
            if legacy.exists() and legacy.is_file():
                return legacy
        except Exception:
            continue
    return path


def _runtime_hosts_payload_is_fresh() -> bool:
    if not isinstance(_RUNTIME_HOSTS_PAYLOAD, dict) or not _RUNTIME_HOSTS_PAYLOAD:
        return False
    try:
        return (time.time() - float(_RUNTIME_HOSTS_FETCHED_AT)) < _HOSTS_CACHE_TTL_SECONDS
    except Exception:
        return False


def _load_hosts_payload() -> Optional[Dict[str, Any]]:
    if _runtime_hosts_payload_is_fresh():
        return _RUNTIME_HOSTS_PAYLOAD

    path = _resolve_hosts_cache_path()
    try:
        if not path.exists() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    return payload if isinstance(payload, dict) else None


def _load_cached_domains(category: str) -> List[str]:
    """Load domain list from the runtime cache or bundled alldebrid.json snapshot.

    category: "hosts" | "streams" | "redirectors"
    """

    wanted = str(category or "").strip().lower()
    if wanted not in {"hosts", "streams", "redirectors"}:
        return []

    payload = _load_hosts_payload()
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if not isinstance(data, dict):
        # Back-compat for older cache shapes.
        data = payload
        if not isinstance(data, dict):
            return []

    raw_list = data.get(wanted)
    if not isinstance(raw_list, (list, dict)):
        return []

    out: List[str] = []
    seen: set[str] = set()

    domain_candidates: List[Any] = []
    if isinstance(raw_list, list):
        domain_candidates.extend(raw_list)
    else:
        for entry in raw_list.values():
            if isinstance(entry, dict):
                nested_domains = entry.get("domains")
                if isinstance(nested_domains, list):
                    domain_candidates.extend(nested_domains)
                elif isinstance(nested_domains, str):
                    domain_candidates.append(nested_domains)
            elif isinstance(entry, str):
                domain_candidates.append(entry)

    for d in domain_candidates:
        try:
            dom = str(d or "").strip().lower()
        except Exception:
            continue
        if not dom:
            continue
        if dom.startswith("http://") or dom.startswith("https://"):
            # Accidentally stored as a URL; normalize to hostname.
            try:
                p = urlparse(dom)
                dom = str(p.hostname or "").strip().lower()
            except Exception:
                continue
        if dom.startswith("www."):
            dom = dom[4:]
        if not dom or dom in seen:
            continue
        seen.add(dom)
        out.append(dom)
    return out


def _load_cached_hoster_domains() -> List[str]:
    # For URL routing (download-file), we intentionally use only the "hosts" list.
    # The "streams" list is extremely broad and would steal URLs from other providers.
    return _load_cached_domains("hosts")


def _fetch_hosts_payload_v4_hosts() -> Optional[Dict[str, Any]]:
    """Fetch the public AllDebrid hosts payload.

    This intentionally does NOT require an API key.
    Endpoint referenced by user: https://api.alldebrid.com/v4/hosts
    """

    url = "https://api.alldebrid.com/v4/hosts"
    try:
        with HTTPClient(timeout=20.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception as exc:
        log(f"[alldebrid] Failed to fetch hosts list: {exc}", file=sys.stderr)
        return None


def refresh_alldebrid_hoster_cache(*, force: bool = False) -> None:
    """Refresh the in-memory host-domain cache for the current process."""
    global _RUNTIME_HOSTS_PAYLOAD, _RUNTIME_HOSTS_FETCHED_AT

    if (not force) and _runtime_hosts_payload_is_fresh():
        return

    payload = _fetch_hosts_payload_v4_hosts()
    if isinstance(payload, dict) and payload:
        _RUNTIME_HOSTS_PAYLOAD = payload
        _RUNTIME_HOSTS_FETCHED_AT = time.time()


def _get_debrid_api_key(config: Dict[str, Any]) -> Optional[str]:
    """Read the canonical AllDebrid API key from config."""
    try:
        from SYS.config import get_debrid_api_key

        key = get_debrid_api_key(config, service="All-debrid")
        return key.strip() if key else None
    except Exception:
        return None


def _consume_bencoded_value(data: bytes, pos: int) -> int:
    if pos >= len(data):
        raise ValueError("Unexpected end of bencode")
    token = data[pos:pos + 1]
    if token == b"i":
        end = data.find(b"e", pos + 1)
        if end == -1:
            raise ValueError("Unterminated integer")
        return end + 1
    if token == b"l" or token == b"d":
        cursor = pos + 1
        while cursor < len(data):
            if data[cursor:cursor + 1] == b"e":
                return cursor + 1
            cursor = _consume_bencoded_value(data, cursor)
        raise ValueError("Unterminated list/dict")
    if token and b"0" <= token <= b"9":
        colon = data.find(b":", pos)
        if colon == -1:
            raise ValueError("Invalid string length")
        length = int(data[pos:colon])
        return colon + 1 + length
    raise ValueError("Unknown bencode token")


def _info_hash_from_torrent_bytes(data: bytes) -> Optional[str]:
    needle = b"4:info"
    idx = data.find(needle)
    if idx == -1:
        return None

    start = idx + len(needle)
    try:
        end = _consume_bencoded_value(data, start)
    except ValueError:
        return None

    info_bytes = data[start:end]
    try:
        return hashlib.sha1(info_bytes).hexdigest()
    except Exception:
        return None


def _fetch_torrent_bytes(target: str) -> Optional[bytes]:
    path_obj = Path(str(target))
    try:
        if path_obj.exists() and path_obj.is_file():
            return path_obj.read_bytes()
    except Exception:
        pass

    try:
        parsed = urlparse(target)
    except Exception:
        parsed = None

    if parsed is None or not parsed.scheme or parsed.scheme.lower() not in {"http", "https"}:
        return None

    if not target.lower().endswith(".torrent"):
        return None

    try:
        with HTTPClient(timeout=30.0) as client:
            response = client.get(target)
            return response.content
    except Exception as exc:
        log(f"Failed to download .torrent from {target}: {exc}", file=sys.stderr)
        return None


_ALD_MAGNET_PREFIX = "alldebrid:magnet:"


def _parse_alldebrid_magnet_id(target: str) -> Optional[int]:
    candidate = str(target or "").strip()
    if not candidate:
        return None

    # Handle various prefix variations: alldebrid:magnet: (standard), alldebrid🧲, or alldebrid:
    id_part = None
    if candidate.lower().startswith(_ALD_MAGNET_PREFIX):
        id_part = candidate[len(_ALD_MAGNET_PREFIX):].strip()
    elif candidate.startswith("alldebrid🧲"):
        id_part = candidate[len("alldebrid🧲"):].strip()
    elif candidate.lower().startswith("alldebrid:"):
        id_part = candidate[len("alldebrid:"):].strip()

    if id_part:
        try:
            return int(id_part)
        except Exception:
            return None
    return None


def resolve_magnet_spec(target: str) -> Optional[str]:
    """Resolve a magnet/hash/torrent URL into a magnet/hash string."""
    candidate = str(target or "").strip()
    if not candidate:
        return None

    parsed = parse_magnet_or_hash(candidate)
    if parsed:
        return parsed

    if is_torrent_file(candidate):
        torrent_bytes = _fetch_torrent_bytes(candidate)
        if not torrent_bytes:
            return None
        hash_value = _info_hash_from_torrent_bytes(torrent_bytes)
        if hash_value:
            return hash_value
    return None


def _looks_like_torrent_source(candidate: str) -> bool:
    value = str(candidate or "").strip()
    if not value:
        return False

    def _clean_segment(text: str) -> str:
        cleaned = text.split("#", 1)[0].split("?", 1)[0]
        return cleaned.strip().lower()

    paths: list[str] = []
    try:
        parsed = urlparse(value)
    except Exception:
        parsed = None

    if parsed and parsed.path:
        paths.append(_clean_segment(parsed.path))
    paths.append(_clean_segment(value))

    for path in paths:
        if path and is_torrent_file(path):
            return True
    return False


def _build_queued_magnet_item(
    *,
    magnet_spec: str,
    magnet_id: int,
    magnet_info: Dict[str, Any],
) -> Dict[str, Any]:
    title = str(
        magnet_info.get("filename")
        or magnet_info.get("name")
        or magnet_info.get("hash")
        or f"magnet-{magnet_id}"
    ).strip() or f"magnet-{magnet_id}"
    status_label = str(magnet_info.get("status") or "queued").strip() or "queued"
    status_tag = re.sub(r"[^a-z0-9]+", "-", status_label.lower()).strip("-") or "queued"

    metadata: Dict[str, Any] = {
        "magnet_id": magnet_id,
        "plugin": "alldebrid",
        "plugin_view": "files",
        "magnet_spec": magnet_spec,
        "source_url": magnet_spec,
        "status": status_label,
    }
    for key in ("filename", "name", "hash", "size", "statusCode", "ready"):
        value = magnet_info.get(key)
        if value is not None:
            metadata[key] = value

    return {
        "table": "alldebrid",
        "plugin": "alldebrid",
        "path": f"{_ALD_MAGNET_PREFIX}{magnet_id}",
        "title": title,
        "detail": f"Queued in AllDebrid ({status_label})",
        "media_kind": "folder",
        "tag": ["folder", f"status:{status_tag}", "provider:alldebrid"],
        "columns": [
            ("Title", title),
            ("Status", status_label),
            ("Provider", "alldebrid"),
            ("Magnet ID", magnet_id),
        ],
        "full_metadata": metadata,
        "source_url": magnet_spec,
        "_selection_action": ["search-file", "-plugin", "alldebrid", f"ID={magnet_id}"],
    }


def prepare_magnet(
    magnet_spec: str,
    config: Dict[str, Any],
) -> tuple[Optional[AllDebridClient], Optional[int], Dict[str, Any]]:
    api_key = _get_debrid_api_key(config or {})
    if not api_key:
        log("AllDebrid API key not configured. Use .config to set it.", file=sys.stderr)
        return None, None, {}

    try:
        client = AllDebridClient(api_key)
    except Exception as exc:
        log(f"Failed to initialize AllDebrid client: {exc}", file=sys.stderr)
        return None, None, {}

    try:
        magnet_info = client.magnet_add(magnet_spec)
        magnet_id_val = magnet_info.get("id") or 0
        magnet_id = int(magnet_id_val)
        if magnet_id <= 0:
            log(f"AllDebrid magnet submission failed: {magnet_info}", file=sys.stderr)
            return None, None, {}
    except Exception as exc:
        log(f"Failed to submit magnet to AllDebrid: {exc}", file=sys.stderr)
        return None, None, {}

    return client, magnet_id, magnet_info if isinstance(magnet_info, dict) else {}


def _flatten_files_with_relpath(items: Any) -> Iterable[Dict[str, Any]]:
    for node in AllDebrid._flatten_files(items):
        enriched = dict(node)
        enriched.setdefault("name", node.get("n") or node.get("name"))
        enriched.setdefault("link", node.get("l") or node.get("link"))
        rel = node.get("_relpath") or node.get("relpath")
        if not rel:
            name = enriched.get("name")
            rel = str(name or "").strip()
        enriched["relpath"] = rel
        yield enriched


def download_magnet(
    magnet_spec: str,
    original_url: str,
    final_output_dir: Path,
    config: Dict[str, Any],
    progress: Any,
    quiet_mode: bool,
    path_from_result: Callable[[Any], Path],
    on_emit: Callable[[Path, str, str, Dict[str, Any]], None],
) -> tuple[int, Optional[int]]:
    client, magnet_id, magnet_info = prepare_magnet(magnet_spec, config)
    if client is None or magnet_id is None:
        return 0, None

    wait_timeout = 300
    try:
        streaming_config = config.get("streaming", {}) if isinstance(config, dict) else {}
        wait_timeout = int(streaming_config.get("wait_timeout", 300))
    except Exception:
        wait_timeout = 300

    elapsed = 0
    while elapsed < wait_timeout:
        try:
            status = client.magnet_status(magnet_id)
        except Exception as exc:
            log(f"Failed to read magnet status {magnet_id}: {exc}", file=sys.stderr)
            return 0, magnet_id
        ready = bool(status.get("ready")) or status.get("statusCode") == 4
        if ready:
            break
        time.sleep(5)
        elapsed += 5
    else:
        log(f"AllDebrid magnet {magnet_id} timed out after {wait_timeout}s", file=sys.stderr)
        return 0, magnet_id

    try:
        files_result = client.magnet_links([magnet_id])
    except Exception as exc:
        log(f"Failed to list AllDebrid magnet files: {exc}", file=sys.stderr)
        return 0, magnet_id

    magnet_files = files_result.get(str(magnet_id), {}) if isinstance(files_result, dict) else {}
    file_nodes = magnet_files.get("files") if isinstance(magnet_files, dict) else []
    if not file_nodes:
        log(f"AllDebrid magnet {magnet_id} produced no files", file=sys.stderr)
        return 0, magnet_id

    magnet_folder_name = _resolve_alldebrid_folder_name(
        magnet_info,
        fallback_metadata=magnet_files,
        magnet_id=magnet_id,
    )
    downloaded = 0
    for node in _flatten_files_with_relpath(file_nodes):
        file_url = str(node.get("link") or "").strip()
        file_name = str(node.get("name") or "").strip()
        relpath = str(node.get("relpath") or file_name).strip()
        if not file_url or not relpath:
            continue

        rel_path_obj = Path(relpath)
        output_dir = adjust_output_dir_for_alldebrid(
            final_output_dir,
            {"folder": magnet_folder_name, "relpath": relpath},
            magnet_files,
        )

        try:
            result_obj = download_direct_file(
                file_url,
                output_dir,
                quiet=quiet_mode,
                suggested_filename=rel_path_obj.name,
                pipeline_progress=progress,
            )
        except Exception as exc:
            log(f"Failed to download AllDebrid file {file_url}: {exc}", file=sys.stderr)
            continue

        downloaded_path = path_from_result(result_obj)
        metadata = {
            "magnet_id": magnet_id,
            "folder": magnet_folder_name,
            "relpath": relpath,
            "name": file_name,
        }
        on_emit(downloaded_path, file_url or original_url, relpath, metadata)
        downloaded += 1

    return downloaded, magnet_id


def expand_folder_item(
    item: Any,
    get_plugin: Optional[Callable[[str, Dict[str, Any]], Any]],
    config: Dict[str, Any],
) -> Tuple[List[Any], Optional[str]]:
    table = getattr(item, "table", None) if not isinstance(item, dict) else item.get("table")
    media_kind = getattr(item, "media_kind", None) if not isinstance(item, dict) else item.get("media_kind")
    full_metadata = getattr(item, "full_metadata", None) if not isinstance(item, dict) else item.get("full_metadata")
    target = None
    if isinstance(item, dict):
        target = item.get("path") or item.get("url")
    else:
        target = getattr(item, "path", None) or getattr(item, "url", None)

    if (str(table or "").lower() != "alldebrid") or (str(media_kind or "").lower() != "folder"):
        return [], None

    magnet_id = None
    if isinstance(full_metadata, dict):
        magnet_id = full_metadata.get("magnet_id")
    if magnet_id is None and isinstance(target, str) and target.lower().startswith("alldebrid:magnet:"):
        try:
            magnet_id = int(target.split(":")[-1])
        except Exception:
            magnet_id = None

    if magnet_id is None or get_plugin is None:
        return [], None

    plugin = get_plugin("alldebrid", config) if get_plugin else None
    if plugin is None:
        return [], None

    try:
        files = plugin.search("*", limit=10_000, filters={"view": "files", "magnet_id": int(magnet_id)})
    except Exception:
        files = []

    if files and len(files) == 1 and getattr(files[0], "media_kind", "") == "folder":
        detail = getattr(files[0], "detail", "")
        return [], str(detail or "unknown")

    expanded: List[Any] = []
    for sr in files:
        expanded.append(sr.to_dict() if hasattr(sr, "to_dict") else sr)
    return expanded, None


def adjust_output_dir_for_alldebrid(
    base_output_dir: Path,
    full_metadata: Optional[Dict[str, Any]],
    item: Any,
) -> Path:
    from SYS.utils import sanitize_filename as _sf

    output_dir = base_output_dir
    md = full_metadata if isinstance(full_metadata, dict) else {}
    magnet_name = md.get("magnet_name") or md.get("folder")
    if not magnet_name:
        try:
            detail_val = getattr(item, "detail", None) if not isinstance(item, dict) else item.get("detail")
            magnet_name = str(detail_val or "").strip() or None
        except Exception:
            magnet_name = None

    magnet_dir_name = _sf(str(magnet_name)) if magnet_name else ""
    try:
        base_tail = str(Path(output_dir).name or "")
    except Exception:
        base_tail = ""
    base_tail_norm = _sf(base_tail).lower() if base_tail.strip() else ""
    magnet_dir_norm = magnet_dir_name.lower() if magnet_dir_name else ""

    if magnet_dir_name and (not base_tail_norm or base_tail_norm != magnet_dir_norm):
        output_dir = Path(output_dir) / magnet_dir_name

    relpath = md.get("relpath") if isinstance(md, dict) else None
    if (not relpath) and isinstance(md.get("file"), dict):
        relpath = md["file"].get("_relpath")

    if relpath:
        parts = [p for p in str(relpath).replace("\\", "/").split("/") if p and p not in {".", ".."}]
        if magnet_dir_name and parts:
            try:
                if _sf(parts[0]).lower() == magnet_dir_norm:
                    parts = parts[1:]
            except Exception:
                pass
        for part in parts[:-1]:
            output_dir = Path(output_dir) / _sf(part)

    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    except Exception:
        output_dir = base_output_dir

    return output_dir


def _resolve_alldebrid_folder_name(
    metadata: Any,
    *,
    fallback_metadata: Any = None,
    magnet_id: Optional[int] = None,
) -> str:
    sources = [
        source
        for source in (metadata, fallback_metadata)
        if isinstance(source, dict)
    ]
    for key in ("folder", "magnet_name", "filename", "name"):
        for source in sources:
            value = str(source.get(key) or "").strip()
            if value:
                return value
    for source in sources:
        value = str(source.get("hash") or "").strip()
        if value:
            return value
    return f"magnet-{magnet_id}" if magnet_id is not None else "download"


class AllDebrid(TablePluginMixin, Plugin):
    """AllDebrid account provider with magnet folder/file browsing and downloads.
    
    This provider uses the new table system (strict ResultTable adapter pattern) for
    consistent selection and auto-stage integration across all providers. It exposes
    magnets as folder rows and files as file rows, with metadata enrichment for:
    - magnet_id: For routing to _download_magnet_by_id
    - status/ready: For showing sync state
    - _selection_args/_selection_action: For @N expansion control
    - relpath: For proper file hierarchy in downloads
    
    KEY FEATURES:
    - Table system: Using ResultTable adapter for strict column/metadata handling
    - Selection override: Full metadata control via _selection_args/_selection_action
    - Auto-stages: download-file is auto-inserted when @N is used on magnet folders
    - File unlocking: URLs with /f/ paths are automatically unlocked via API before download
    - Drill-down: Selecting a folder row (@N) fetches and displays all files
    
    SELECTION FLOW:
    1. User runs: search-file -plugin alldebrid "ubuntu"
    2. Results show magnet folders and (optionally) files
    3. User selects a row: @1
    4. Selection metadata routes to download-file with -url alldebrid:magnet:<id>
    5. download-file invokes provider.download_items() via provider URL handling
    6. Provider fetches files, unlocks locked URLs, and downloads
    """
    # Magnet URIs should be routed through this provider.
    TABLE_AUTO_STAGES = {"alldebrid": ["download-file"]}
    AUTO_STAGE_USE_SELECTION_ARGS = True
    URL = ("magnet:", "alldebrid:magnet:", "alldebrid:", "alldebrid🧲", "alldebrid.com")
    URL_DOMAINS = ()
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_AUTHOR = "Medeia"
    PLUGIN_DESCRIPTION = "Search and download AllDebrid magnets and files."
    SUPPORTED_CMDLETS = frozenset({"download-file", "search-file"})
    CONFIG_HELP = (
        "Create an API key at https://alldebrid.com/apikeys (account API password / PIN) and paste it below.",
    )

    def extract_query_arguments(self, query: str) -> Tuple[str, Dict[str, Any]]:
        normalized = str(query or "").strip()
        filters: Dict[str, Any] = {}

        # Pull out id=123 or id:123
        match = re.search(r"\bid\s*[=:]\s*(\d+)", normalized, flags=re.IGNORECASE)
        if match:
            filters["magnet_id"] = int(match.group(1))
            normalized = re.sub(
                r"\bid\s*[=:]\s*\d+", "", normalized, flags=re.IGNORECASE
            ).strip()

        if not normalized:
            normalized = "*"

        return normalized, filters

    def get_table_title(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        f = filters or {}
        magnet_id = f.get("magnet_id")
        if magnet_id is not None:
            return f"{self.label} Files: {magnet_id}"
        q = str(query or "").strip() or "*"
        return f"{self.label}: {q}"

    def get_table_metadata(
        self, query: str, filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        meta = super().get_table_metadata(query, filters)
        f = filters or {}
        magnet_id = f.get("magnet_id")
        meta["view"] = "files" if magnet_id is not None else "folders"
        if magnet_id is not None:
            meta["magnet_id"] = magnet_id
        return meta

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "label": "API Key",
                "default": "",
                "required": True,
                "secret": True,
                "help": "https://alldebrid.com/apikeys",
            }
        ]

    @staticmethod
    def _resolve_magnet_spec_from_result(result: Any) -> Optional[str]:
        table = getattr(result, "table", None)
        media_kind = getattr(result, "media_kind", None)
        tags = getattr(result, "tag", None)
        full_metadata = getattr(result, "full_metadata", None)
        target = getattr(result, "path", None) or getattr(result, "url", None)

        if not table or str(table).strip().lower() != "alldebrid":
            return None

        kind_val = str(media_kind or "").strip().lower()
        is_folder = kind_val == "folder"
        if not is_folder and isinstance(tags, (list, set)):
            for tag in tags:
                if str(tag or "").strip().lower() == "folder":
                    is_folder = True
                    break
        if not is_folder:
            return resolve_magnet_spec(str(target or "")) if isinstance(target, str) else None

        metadata = full_metadata if isinstance(full_metadata, dict) else {}
        candidates: List[str] = []

        def _maybe_add(value: Any) -> None:
            if isinstance(value, str):
                cleaned = value.strip()
                if cleaned:
                    candidates.append(cleaned)

        magnet_block = metadata.get("magnet")
        if isinstance(magnet_block, dict):
            for inner in ("magnet", "magnet_link", "link", "url"):
                _maybe_add(magnet_block.get(inner))
            for inner in ("hash", "info_hash", "torrenthash", "magnethash"):
                _maybe_add(magnet_block.get(inner))
        else:
            _maybe_add(magnet_block)

        for extra in ("magnet_link", "magnet_url", "magnet_spec"):
            _maybe_add(metadata.get(extra))
        _maybe_add(metadata.get("hash"))
        _maybe_add(metadata.get("info_hash"))

        for candidate in candidates:
            spec = resolve_magnet_spec(candidate)
            if spec:
                return spec
        return resolve_magnet_spec(str(target)) if isinstance(target, str) else None

    def handle_url(self, url: str, *, output_dir: Optional[Path] = None) -> Tuple[bool, Optional[Path | Dict[str, Any]]]:
        magnet_id = _parse_alldebrid_magnet_id(url)
        if magnet_id is not None:
            return True, {
                "action": "download_items",
                "path": f"{_ALD_MAGNET_PREFIX}{magnet_id}",
                "title": f"magnet-{magnet_id}",
                "metadata": {
                    "magnet_id": magnet_id,
                    "plugin": "alldebrid",
                    "plugin_view": "files",
                },
            }

        spec = resolve_magnet_spec(url)
        if not spec:
            if _looks_like_torrent_source(url):
                log(f"[alldebrid] Torrent source ignored (unable to parse magnet/hash): {url}", file=sys.stderr)
            return False, None

        cfg = self.config if isinstance(self.config, dict) else {}
        try:
            _client, magnet_id, magnet_info = prepare_magnet(spec, cfg)
            if magnet_id is None:
                return False, None
            return True, {
                "action": "emit_items",
                "items": [
                    _build_queued_magnet_item(
                        magnet_spec=str(url),
                        magnet_id=int(magnet_id),
                        magnet_info=magnet_info,
                    )
                ],
                "exit_code": 0,
            }
        except Exception:
            return False, None

    @classmethod
    def url_patterns(cls) -> Tuple[str, ...]:
        patterns = list(super().url_patterns())
        try:
            refresh_alldebrid_hoster_cache(force=False)
        except Exception:
            pass
        try:
            cached = _load_cached_hoster_domains()
            for d in cached:
                dom = str(d or "").strip().lower()
                if dom and dom not in patterns:
                    patterns.append(dom)
        except Exception:
            pass
        return tuple(patterns)

    """Search provider for AllDebrid account content.

    This provider lists and searches the files/magnets already present in the
    user's AllDebrid account.

    Query behavior:
    - "*" / "all" / "list": list recent files from ready magnets
    - otherwise: substring match on file name OR magnet name, or exact magnet id
    """

    def validate(self) -> bool:
        return bool(_get_debrid_api_key(self.config or {}))

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        """Download an AllDebrid SearchResult into output_dir.

        AllDebrid magnet file listings often provide links that require an API
        "unlock" step to produce a true direct-download URL. Without unlocking,
        callers may download a small HTML/redirect page instead of file bytes.

        This is used by the download-file cmdlet when a provider item is piped.
        """
        try:
            api_key = _get_debrid_api_key(self.config or {})
            if not api_key:
                log("[alldebrid] download skipped: missing api_key", file=sys.stderr)
                return None

            target = str(getattr(result, "path", "") or "").strip()
            if not target.startswith(("http://", "https://")):
                log(f"[alldebrid] download skipped: target not http(s): {target}", file=sys.stderr)
                return None

            try:
                from plugins.alldebrid.api import AllDebridClient

                client = AllDebridClient(api_key)
            except Exception as exc:
                log(f"[alldebrid] Failed to init client: {exc}", file=sys.stderr)
                return None

            # Prefer provider title as the output filename; later we may override if unlocked URL has a better basename.
            suggested = sanitize_filename(str(getattr(result, "title", "") or "").strip())
            suggested_name = suggested if suggested else None

            # Quiet mode when download-file is mid-pipeline.
            quiet = bool(self.config.get("_quiet_background_output")) if isinstance(self.config, dict) else False

            def _html_guard(path: Path) -> bool:
                try:
                    if path.exists():
                        size = path.stat().st_size
                        if size > 0 and size <= 250_000 and path.suffix.lower() not in (".html", ".htm"):
                            head = path.read_bytes()[:512]
                            try:
                                text = head.decode("utf-8", errors="ignore").lower()
                            except Exception:
                                text = ""
                            if "<html" in text or "<!doctype html" in text:
                                return True
                except Exception:
                    return False
                return False

            def _download_unlocked(unlocked_url: str, *, allow_html: bool = False) -> Optional[Path]:
                # If this is an unlocked debrid link (allow_html=True), stream it directly and skip
                # the generic HTML guard to avoid falling back to the public hoster.
                if allow_html:
                    try:
                        from API.HTTP import HTTPClient

                        fname = suggested_name or sanitize_filename(Path(urlparse(unlocked_url).path).name)
                        if not fname:
                            fname = "download"
                        if not Path(fname).suffix:
                            fname = f"{fname}.bin"
                        dest = Path(output_dir) / fname
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with HTTPClient(timeout=30.0) as client:
                            with client._request_stream("GET", unlocked_url, follow_redirects=True) as resp:
                                resp.raise_for_status()
                                with dest.open("wb") as fh:
                                    for chunk in resp.iter_bytes():
                                        if not chunk:
                                            continue
                                        fh.write(chunk)
                        return dest if dest.exists() else None
                    except Exception:
                        return None

                # Otherwise, use standard downloader with guardrails.
                pipe_progress = None
                try:
                    if isinstance(self.config, dict):
                        pipe_progress = self.config.get("_pipeline_progress")
                except Exception:
                    pipe_progress = None

                try:
                    dl_res = download_direct_file(
                        unlocked_url,
                        Path(output_dir),
                        quiet=quiet,
                        suggested_filename=suggested_name,
                        pipeline_progress=pipe_progress,
                    )
                    downloaded_path = getattr(dl_res, "path", None)
                    if downloaded_path is None:
                        return None
                    downloaded_path = Path(str(downloaded_path))
                except DownloadError as exc:
                    log(
                        f"[alldebrid] download_direct_file rejected URL ({exc}); no further fallback", file=sys.stderr
                    )
                    return None

                try:
                    if _html_guard(downloaded_path):
                        log(
                            "[alldebrid] Download returned HTML page (not file bytes). Try again or check AllDebrid link status.",
                            file=sys.stderr,
                        )
                        return None
                except Exception:
                    pass

                return downloaded_path if downloaded_path.exists() else None

            unlocked_url = target
            try:
                unlocked = client.resolve_unlock_link(target, poll=True, max_wait_seconds=120, poll_interval_seconds=5)
                if isinstance(unlocked, str) and unlocked.strip().startswith(("http://", "https://")) and unlocked.strip() != target:
                    unlocked_url = unlocked.strip()
                else:
                    # Surface the raw API response so failures are diagnosable.
                    try:
                        raw = client._request("link/unlock", {"link": target})
                        log(f"[alldebrid] unlock response for {target}: {raw}", file=sys.stderr)
                    except Exception:
                        pass
                    log(f"[alldebrid] unlock returned no direct link for {target}", file=sys.stderr)
            except Exception as exc:
                log(f"[alldebrid] unlock failed for {target}: {exc}", file=sys.stderr)

            if unlocked_url == target:
                log(f"[alldebrid] no unlocked link; refusing to download raw hoster URL {target}", file=sys.stderr)
                return None

            # Prefer filename from unlocked URL path.
            try:
                unlocked_name = sanitize_filename(Path(urlparse(unlocked_url).path).name)
                if unlocked_name:
                    suggested_name = unlocked_name
            except Exception:
                pass

            # Stream the unlocked premium link directly; never fall back to the raw hoster URL.
            downloaded = _download_unlocked(unlocked_url, allow_html=True)
            return downloaded
        except Exception:
            return None

    def resolve_pipe_result_download(
        self,
        result: Any,
        pipe_obj: Optional[PipeObject],
    ) -> Tuple[Optional[Path], Optional[str], Optional[Path]]:
        """Download a remote provider result on behalf of add-file."""

        download_url = (
            _extract_value(result, "path")
            or (getattr(pipe_obj, "url", None) if pipe_obj is not None else None)
        )
        download_url = str(download_url or "").strip()
        if not download_url:
            return None, None, None

        metadata: Dict[str, Any] = {}
        maybe_meta = _extract_value(result, "full_metadata") or _extract_value(result, "metadata")
        if isinstance(maybe_meta, dict):
            metadata.update(maybe_meta)
        if pipe_obj is not None:
            pipe_meta = getattr(pipe_obj, "metadata", None)
            if isinstance(pipe_meta, dict):
                metadata.update(pipe_meta)

        table_name = str(
            _extract_value(result, "table")
            or (getattr(pipe_obj, "provider", None) if pipe_obj is not None else None)
            or "alldebrid"
        ).strip()
        if not table_name:
            table_name = "alldebrid"

        title = str(
            _extract_value(result, "title")
            or (getattr(pipe_obj, "title", None) if pipe_obj is not None else None)
            or metadata.get("name")
            or ""
        ).strip()
        if not title:
            fallback_name = None
            parsed_url = None
            try:
                parsed_url = urlparse(download_url)
            except Exception:
                parsed_url = None
            if parsed_url and parsed_url.path:
                try:
                    fallback_name = Path(parsed_url.path).stem
                except Exception:
                    fallback_name = None
            title = fallback_name or "alldebrid"

        media_kind = str(
            _extract_value(result, "media_kind")
            or metadata.get("media_kind")
            or "file"
        ).strip() or "file"

        search_result = SearchResult(
            table=table_name,
            title=title,
            path=download_url,
            detail=str(_extract_value(result, "detail") or ""),
            annotations=[],
            media_kind=media_kind,
            tag=set(getattr(pipe_obj, "tag", []) or []),
            columns=[],
            full_metadata=metadata,
        )

        if not download_url.startswith(("http://", "https://")):
            return None, None, None

        download_dir = Path(tempfile.mkdtemp(prefix="add-file-alldebrid-"))
        try:
            downloaded_path = self.download(search_result, download_dir)
            if not downloaded_path:
                shutil.rmtree(download_dir, ignore_errors=True)
                return None, None, None
            if pipe_obj is not None:
                pipe_obj.is_temp = True
            hash_hint = (
                _extract_value(result, "hash")
                or _extract_value(result, "file_hash")
                or (getattr(pipe_obj, "hash", None) if pipe_obj is not None else None)
            )
            if not hash_hint:
                for candidate in ("hash", "file_hash", "info_hash"):
                    candidate_val = metadata.get(candidate)
                    if isinstance(candidate_val, str) and candidate_val.strip():
                        hash_hint = candidate_val.strip()
                        break
            return downloaded_path, hash_hint, download_dir
        except Exception as exc:
            log(f"[alldebrid] add-file download failed: {exc}", file=sys.stderr)
            shutil.rmtree(download_dir, ignore_errors=True)
            return None, None, None

    def status_summary(self) -> Dict[str, Any]:
        try:
            api_key = _get_debrid_api_key(self.config)
            if not api_key:
                return {
                    "status": "DISABLED",
                    "name": self.label,
                    "plugin": self.name,
                    "detail": "Not configured",
                }
            client = AllDebridClient(api_key)
            base_url = str(getattr(client, "base_url", "") or "").strip()
            return {
                "status": "ENABLED",
                "name": self.label,
                "plugin": self.name,
                "detail": base_url or "Connected",
            }
        except Exception as exc:
            return {
                "status": "DISABLED",
                "name": self.label,
                "plugin": self.name,
                "detail": str(exc),
            }

    def resolve_url(self, url: str, **_kwargs: Any) -> str:
        target = str(url or "").strip()
        if not target.startswith(("http://", "https://")):
            return target

        try:
            parsed = urlparse(target)
            host = str(parsed.netloc or "").lower()
            path = str(parsed.path or "")
        except Exception:
            return target

        if host != "alldebrid.com" or not path.startswith("/f/"):
            return target

        api_key = _get_debrid_api_key(self.config)
        if not api_key:
            return target

        try:
            client = AllDebridClient(str(api_key))
            unlocked = client.unlock_link(target)
            if isinstance(unlocked, str) and unlocked.strip():
                return unlocked.strip()
        except Exception:
            pass

        return target

    def download_url(self, url: str, output_dir: Path, **_kwargs: Any) -> Optional[Path]:
        """Unlock and download a supported hoster URL (e.g. 1fichier) via AllDebrid."""
        target = str(url or "").strip()
        if not target.startswith(("http://", "https://")):
            return None

        api_key = _get_debrid_api_key(self.config or {})
        if not api_key:
            log("[alldebrid] download_url skipped: missing api_key", file=sys.stderr)
            return None

        try:
            parsed = urlparse(target)
            name = Path(parsed.path).name or "download"
        except Exception:
            name = "download"

        sr = SearchResult(
            table="alldebrid",
            title=sanitize_filename(name) or "download",
            path=target,
            media_kind="file",
        )
        return self.download(sr, Path(output_dir))

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
        # Check if this is a direct magnet_id from the account (from selector or custom URL scheme)
        path_id = _parse_alldebrid_magnet_id(getattr(result, "path", ""))
        full_metadata = getattr(result, "full_metadata", None) or {}
        magnet_id_direct = None
        if isinstance(full_metadata, dict):
            magnet_id_direct = full_metadata.get("magnet_id")

        active_id = magnet_id_direct if magnet_id_direct is not None else path_id

        if active_id is not None:
            try:
                magnet_id = int(active_id)
                cfg = config if isinstance(config, dict) else (self.config or {})
                count = self._download_magnet_by_id(
                    magnet_id,
                    output_dir,
                    cfg,
                    emit,
                    progress,
                    quiet_mode,
                    path_from_result,
                )
                return count
            except Exception:
                pass
        
        spec = self._resolve_magnet_spec_from_result(result)
        if not spec:
            return 0

        cfg = config if isinstance(config, dict) else (self.config or {})

        def _on_emit(path: Path, file_url: str, relpath: str, metadata: Dict[str, Any]) -> None:
            emit(path, file_url, relpath, metadata)

        downloaded, _ = download_magnet(
            spec,
            str(getattr(result, "path", "") or ""),
            output_dir,
            cfg,
            progress,
            quiet_mode,
            path_from_result,
            _on_emit,
        )
        return downloaded

    @staticmethod
    def _flatten_files(items: Any,
                       *,
                       _prefix: Optional[List[str]] = None) -> Iterable[Dict[str, Any]]:
        """Flatten AllDebrid magnet file tree into file dicts, preserving relative paths.

        API commonly returns:
          - file: {n: name, s: size, l: link}
          - folder: {n: name, e: [sub_items]}

        This flattener attaches a best-effort relative path to each yielded file node
        as `_relpath` using POSIX separators (e.g., "Season 1/E01.mkv").

        Some call sites in this repo also expect {name, size, link}, so we accept both.
        """
        prefix = list(_prefix or [])

        if not items:
            return
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return

        for node in items:
            if not isinstance(node, dict):
                continue

            children = node.get("e") or node.get("children")
            if isinstance(children, list):
                folder_name = node.get("n") or node.get("name")
                next_prefix = prefix
                if isinstance(folder_name, str) and folder_name.strip():
                    next_prefix = prefix + [folder_name.strip()]
                yield from AllDebrid._flatten_files(children, _prefix=next_prefix)
                continue

            name = node.get("n") or node.get("name")
            link = node.get("l") or node.get("link")
            if isinstance(name, str) and name.strip() and isinstance(link, str) and link.strip():
                rel_parts = prefix + [name.strip()]
                relpath = "/".join([p for p in rel_parts if p])
                enriched = dict(node)
                enriched["_relpath"] = relpath
                yield enriched

    def _download_magnet_by_id(
        self,
        magnet_id: int,
        output_dir: Path,
        config: Dict[str, Any],
        emit: Callable[[Path, str, str, Dict[str, Any]], None],
        progress: Any,
        quiet_mode: bool,
        path_from_result: Callable[[Any], Path],
    ) -> int:
        """Download files from an existing magnet ID (already in account)."""
        api_key = _get_debrid_api_key(config or {})
        if not api_key:
            log("AllDebrid API key not configured", file=sys.stderr)
            return 0

        try:
            client = AllDebridClient(api_key)
        except Exception as exc:
            log(f"Failed to init AllDebrid client: {exc}", file=sys.stderr)
            return 0

        try:
            files_result = client.magnet_links([magnet_id])
        except Exception as exc:
            log(f"Failed to list files for magnet {magnet_id}: {exc}", file=sys.stderr)
            return 0

        magnet_files = files_result.get(str(magnet_id), {}) if isinstance(files_result, dict) else {}
        file_nodes = magnet_files.get("files") if isinstance(magnet_files, dict) else []
        if not file_nodes:
            log(f"AllDebrid magnet {magnet_id} has no files", file=sys.stderr)
            return 0

        file_entries = list(self._flatten_files(file_nodes))
        total_files = len(file_entries)
        if total_files <= 0:
            log(f"AllDebrid magnet {magnet_id} has no downloadable files", file=sys.stderr)
            return 0

        magnet_metadata: Dict[str, Any] = {}
        try:
            for magnet in client.magnet_list() or []:
                if not isinstance(magnet, dict):
                    continue
                try:
                    listed_id = int(magnet.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if listed_id == magnet_id:
                    magnet_metadata = magnet
                    break
        except Exception:
            magnet_metadata = {}

        magnet_path_metadata: Dict[str, Any] = {}
        magnet_folder_name = _resolve_alldebrid_folder_name(
            magnet_metadata,
            fallback_metadata=magnet_files,
            magnet_id=magnet_id,
        )
        if magnet_folder_name:
            magnet_path_metadata["folder"] = magnet_folder_name

        downloaded = 0
        for file_idx, node in enumerate(file_entries, 1):
            locked_url = str(node.get("l") or node.get("link") or "").strip()
            file_name = str(node.get("n") or node.get("name") or "").strip()
            relpath = str(node.get("_relpath") or file_name or "").strip()

            try:
                if progress is not None and hasattr(progress, "set_status"):
                    progress.set_status(
                        f"downloading file {file_idx}/{total_files}: {relpath or file_name or 'download'}"
                    )
            except Exception:
                pass
            
            if not locked_url or not relpath:
                continue

            # Unlock the URL if it's restricted (contains /f/)
            file_url = locked_url
            if "/f/" in locked_url:
                try:
                    unlocked = client.unlock_link(locked_url)
                    if unlocked:
                        file_url = unlocked
                except Exception:
                    pass

            rel_path_obj = Path(relpath)
            target_path = adjust_output_dir_for_alldebrid(
                output_dir,
                {**magnet_path_metadata, "relpath": relpath},
                magnet_files,
            )

            suggested_name = sanitize_filename(rel_path_obj.name) or sanitize_filename(file_name)
            if not suggested_name:
                suggested_name = rel_path_obj.name or file_name or f"file-{file_idx}"

            try:
                result_obj = download_direct_file(
                    file_url,
                    target_path,
                    quiet=quiet_mode,
                    suggested_filename=suggested_name,
                    pipeline_progress=progress,
                )
            except Exception:
                continue

            downloaded_path = path_from_result(result_obj)
            metadata = {
                "magnet_id": magnet_id,
                "folder": magnet_folder_name,
                "relpath": relpath,
                "name": file_name,
            }
            emit(downloaded_path, file_url, relpath, metadata)
            downloaded += 1

        if downloaded == 0:
            log(f"AllDebrid magnet {magnet_id} produced no downloads", file=sys.stderr)

        try:
            if progress is not None and hasattr(progress, "clear_status"):
                progress.clear_status()
        except Exception:
            pass

        return downloaded

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str,
                               Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        q = (query or "").strip()
        if not q:
            return []

        api_key = _get_debrid_api_key(self.config or {})
        if not api_key:
            return []

        view = None
        if isinstance(filters, dict):
            view = str(filters.get("view") or "").strip().lower() or None
        view = view or "folders"

        try:
            from plugins.alldebrid.api import AllDebridClient

            client = AllDebridClient(api_key)
        except Exception as exc:
            log(f"[alldebrid] Failed to init client: {exc}", file=sys.stderr)
            return []

        q_lower = q.lower()
        needle = "" if q_lower in {"*",
                                   "all",
                                   "list"} else q_lower

        # Second-stage: list files for a specific magnet id.
        if view == "files":
            magnet_id_val = None
            if isinstance(filters, dict):
                magnet_id_val = filters.get("magnet_id")
            if magnet_id_val is None:
                magnet_id_val = kwargs.get("magnet_id")

            if magnet_id_val is None:
                return []

            try:
                magnet_id = int(magnet_id_val)
            except (TypeError, ValueError):
                return []

            magnet_status: Dict[str,
                                Any] = {}
            try:
                magnet_status = client.magnet_status(magnet_id)
            except Exception:
                magnet_status = {}

            magnet_name = str(
                magnet_status.get("filename") or magnet_status.get("name")
                or magnet_status.get("hash") or f"magnet-{magnet_id}"
            )
            status_code = magnet_status.get("statusCode")
            status_text = str(magnet_status.get("status") or "").strip() or "unknown"
            ready = status_code == 4 or bool(magnet_status.get("ready"))

            if not ready:
                return [
                    SearchResult(
                        table="alldebrid",
                        title=magnet_name,
                        path=f"alldebrid:magnet:{magnet_id}",
                        detail=status_text,
                        annotations=["folder",
                                     "not-ready"],
                        media_kind="folder",
                        tag={"alldebrid",
                             "folder",
                             str(magnet_id),
                             "not-ready"},
                        columns=[
                            ("Folder",
                             magnet_name),
                            ("ID",
                             str(magnet_id)),
                            ("Status",
                             status_text),
                            ("Ready",
                             "no"),
                        ],
                        full_metadata={
                            "magnet": magnet_status,
                            "magnet_id": magnet_id,
                            "plugin": "alldebrid",
                            "plugin_view": "files",
                            "magnet_name": magnet_name,
                        },
                    )
                ]

            try:
                files_result = client.magnet_links([magnet_id])
                magnet_files = (
                    files_result.get(str(magnet_id),
                                     {}) if isinstance(files_result,
                                                       dict) else {}
                )
                file_tree = magnet_files.get("files",
                                             []) if isinstance(magnet_files,
                                                               dict) else []
            except Exception as exc:
                log(
                    f"[alldebrid] Failed to list files for magnet {magnet_id}: {exc}",
                    file=sys.stderr,
                )
                file_tree = []

            results: List[SearchResult] = []
            for file_node in self._flatten_files(file_tree):
                file_name = str(file_node.get("n") or file_node.get("name")
                                or "").strip()
                file_url = str(file_node.get("l") or file_node.get("link")
                               or "").strip()
                relpath = str(file_node.get("_relpath") or file_name or "").strip()
                file_size = file_node.get("s") or file_node.get("size")
                if not file_name or not file_url:
                    continue

                if needle and needle not in file_name.lower():
                    continue

                size_bytes: Optional[int] = None
                try:
                    if isinstance(file_size, (int, float)):
                        size_bytes = int(file_size)
                    elif isinstance(file_size, str) and file_size.isdigit():
                        size_bytes = int(file_size)
                except Exception:
                    size_bytes = None

                metadata = {
                    "magnet": magnet_status,
                    "magnet_id": magnet_id,
                    "magnet_name": magnet_name,
                    "relpath": relpath,
                    "file": file_node,
                    "plugin": "alldebrid",
                    "plugin_view": "files",
                    # Selection metadata for table system
                    "_selection_args": ["-url", f"{_ALD_MAGNET_PREFIX}{magnet_id}"],
                    "_selection_action": ["download-file", "-plugin", "alldebrid", "-url", f"{_ALD_MAGNET_PREFIX}{magnet_id}"],
                }

                results.append(
                    SearchResult(
                        table="alldebrid",
                        title=file_name,
                        path=file_url,
                        detail=magnet_name,
                        annotations=["file"],
                        media_kind="file",
                        size_bytes=size_bytes,
                        tag={"alldebrid",
                             "file",
                             str(magnet_id)},
                        columns=[
                            ("File",
                             file_name),
                            ("Folder",
                             magnet_name),
                            ("ID",
                             str(magnet_id)),
                        ],
                        full_metadata=metadata,
                    )
                )
                if len(results) >= max(1, limit):
                    break

            return results

        # Default: folders view (magnets)
        try:
            magnets = client.magnet_list() or []
        except Exception as exc:
            log(f"[alldebrid] Failed to list account magnets: {exc}", file=sys.stderr)
            return []

        wanted_id: Optional[int] = None
        if needle.isdigit():
            try:
                wanted_id = int(needle)
            except Exception:
                wanted_id = None

        results: List[SearchResult] = []
        for magnet in magnets:
            if not isinstance(magnet, dict):
                continue

            magnet_id_val = magnet.get("id")
            if magnet_id_val is None:
                continue
            try:
                magnet_id = int(magnet_id_val)
            except (TypeError, ValueError):
                continue

            magnet_name = str(
                magnet.get("filename") or magnet.get("name") or magnet.get("hash")
                or f"magnet-{magnet_id}"
            )
            magnet_name_lower = magnet_name.lower()

            status_text = str(magnet.get("status") or "").strip() or "unknown"
            status_code = magnet.get("statusCode")
            ready = status_code == 4 or bool(magnet.get("ready"))

            if wanted_id is not None:
                if magnet_id != wanted_id:
                    continue
            elif needle and (needle not in magnet_name_lower):
                continue

            size_bytes: Optional[int] = None
            try:
                size_val = magnet.get("size")
                if isinstance(size_val, (int, float)):
                    size_bytes = int(size_val)
                elif isinstance(size_val, str) and size_val.isdigit():
                    size_bytes = int(size_val)
            except Exception:
                size_bytes = None

            results.append(
                SearchResult(
                    table="alldebrid",
                    title=magnet_name,
                    path=f"alldebrid:magnet:{magnet_id}",
                    detail=status_text,
                    annotations=["folder"],
                    media_kind="folder",
                    size_bytes=size_bytes,
                    tag={"alldebrid",
                         "folder",
                         str(magnet_id)}
                    | ({"ready"} if ready else {"not-ready"}),
                    columns=[
                        ("Folder",
                         magnet_name),
                        ("ID",
                         str(magnet_id)),
                        ("Status",
                         status_text),
                        ("Ready",
                         "yes" if ready else "no"),
                    ],
                    full_metadata={
                        "magnet": magnet,
                        "magnet_id": magnet_id,
                        "plugin": "alldebrid",
                        "plugin_view": "folders",
                        "magnet_name": magnet_name,
                        # Selection metadata: allow @N expansion to drive downloads directly
                        "_selection_args": ["-url", f"{_ALD_MAGNET_PREFIX}{magnet_id}"],
                        "_selection_action": ["download-file", "-plugin", "alldebrid", "-url", f"{_ALD_MAGNET_PREFIX}{magnet_id}"],
                    },
                )
            )

            if len(results) >= max(1, limit):
                break

        return results

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any,
    ) -> bool:
        """Handle AllDebrid `@N` selection by drilling into magnet files."""
        if not stage_is_last:
            return False

        def _as_payload(item: Any) -> Dict[str, Any]:
            if isinstance(item, dict):
                return dict(item)
            try:
                if hasattr(item, "to_dict"):
                    maybe = item.to_dict()  # type: ignore[attr-defined]
                    if isinstance(maybe, dict):
                        return maybe
            except Exception:
                pass
            payload: Dict[str, Any] = {}
            try:
                payload = {
                    "title": getattr(item, "title", None),
                    "path": getattr(item, "path", None),
                    "table": getattr(item, "table", None),
                    "annotations": getattr(item, "annotations", None),
                    "media_kind": getattr(item, "media_kind", None),
                    "full_metadata": getattr(item, "full_metadata", None),
                }
            except Exception:
                payload = {}
            return payload

        chosen: List[Dict[str, Any]] = []
        for item in selected_items or []:
            payload = _as_payload(item)
            meta = payload.get("full_metadata") or payload.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}

            ann_set: set[str] = set()
            for ann_source in (payload.get("annotations"), meta.get("annotations")):
                if isinstance(ann_source, (list, tuple, set)):
                    for ann in ann_source:
                        ann_text = str(ann or "").strip().lower()
                        if ann_text:
                            ann_set.add(ann_text)

            media_kind = str(payload.get("media_kind") or meta.get("media_kind") or "").strip().lower()
            is_folder = (media_kind == "folder") or ("folder" in ann_set)
            magnet_id = meta.get("magnet_id")
            if magnet_id is None or (not is_folder):
                continue

            title = str(payload.get("title") or meta.get("magnet_name") or meta.get("name") or "").strip()
            if not title:
                title = f"magnet-{magnet_id}"

            chosen.append({
                "magnet_id": magnet_id,
                "title": title,
            })

        if not chosen:
            return False

        target = chosen[0]
        magnet_id = target.get("magnet_id")
        title = target.get("title") or f"magnet-{magnet_id}"

        try:
            files = self.search("*", limit=200, filters={"view": "files", "magnet_id": magnet_id})
        except Exception as exc:
            print(f"alldebrid selector failed: {exc}\n")
            return True

        try:
            from SYS.result_table import Table
            from SYS.rich_display import stdout_console
        except Exception:
            return True

        table = Table(f"AllDebrid Files: {title}")._perseverance(True)
        table.set_table("alldebrid")
        try:
            table.set_table_metadata({"plugin": "alldebrid", "view": "files", "magnet_id": magnet_id})
        except Exception:
            pass
        table.set_source_command("download-file", ["-plugin", "alldebrid"])

        results_payload: List[Dict[str, Any]] = []
        for r in files or []:
            table.add_result(r)
            try:
                results_payload.append(r.to_dict())
            except Exception:
                results_payload.append(
                    {
                        "table": getattr(r, "table", "alldebrid"),
                        "title": getattr(r, "title", ""),
                        "path": getattr(r, "path", ""),
                        "full_metadata": getattr(r, "full_metadata", None),
                    }
                )

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

    def show_selection_details(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        source_command: str = "",
        table_type: str = "",
        table_metadata: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> bool:
        _ = table_type
        _item, payload, meta = self.resolve_selection_detail_subject(
            selected_items,
            stage_is_last=stage_is_last,
            source_command=source_command,
            require_media_kind="file",
        )
        if not isinstance(payload, dict):
            return False

        title = str(payload.get("title") or meta.get("name") or "").strip() or "AllDebrid Item"
        magnet_name = str(meta.get("magnet_name") or payload.get("detail") or "").strip()
        magnet_id = meta.get("magnet_id")
        relpath = str(meta.get("relpath") or "").strip()
        direct_url = str(payload.get("path") or "").strip()
        selection_url = ""
        action = meta.get("_selection_action") or meta.get("selection_action")
        if isinstance(action, (list, tuple)):
            action_tokens = [str(x) for x in action if x is not None]
            for idx, token in enumerate(action_tokens):
                if str(token).strip().lower() in {"-url", "--url"} and idx + 1 < len(action_tokens):
                    selection_url = str(action_tokens[idx + 1] or "").strip()
                    break

        try:
            from SYS.detail_view_helpers import prepare_detail_metadata, render_selection_detail_view
        except Exception:
            return super().show_selection_details(
                selected_items,
                ctx=ctx,
                stage_is_last=stage_is_last,
                source_command=source_command,
                table_type=table_type,
                table_metadata=table_metadata,
            )

        detail_metadata = prepare_detail_metadata(
            payload,
            title=title,
            store=self.name,
            path=direct_url or None,
            tags=meta.get("tag") or meta.get("tags"),
            extra_fields={
                "Plugin": self.name,
                "Magnet": magnet_name or None,
                "Magnet ID": magnet_id,
                "Relative Path": relpath or None,
                "View": str(meta.get("plugin_view") or meta.get("view") or (table_metadata or {}).get("view") or "").strip() or None,
                "Direct Url": direct_url or None,
                "Selection Url": selection_url or None,
            },
        )

        return render_selection_detail_view(
            ctx=ctx,
            item=payload,
            title=f"AllDebrid Item: {title}",
            metadata=detail_metadata,
            table_name=self.name,
            detail_order=["Title", "Instance", "Magnet", "Magnet ID", "Relative Path", "View", "Path", "File", "Folder", "ID", "Direct URL", "Selection URL", "Plugin"],
            value_case="preserve",
        )
