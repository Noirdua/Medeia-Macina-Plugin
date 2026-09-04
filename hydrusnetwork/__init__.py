from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from PluginCore.base import Plugin, SearchResult
from plugins.hydrusnetwork.store_backend import HydrusStoreOperations
from SYS.selection_builder import build_hash_store_selection


_SHA256_RE = re.compile(r"\b[a-fA-F0-9]{64}\b")


def _normalize_hash(value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64:
        return ""
    if any(ch not in "0123456789abcdef" for ch in text):
        return ""
    return text


def get_store_backend_classes() -> Dict[str, Any]:
    """Expose Hydrus store backends through the plugin package.

    Store discovery should flow through this plugin-owned hook so the backend can
    stay inside the Hydrus plugin package while older import paths remain available
    through compatibility shims.
    """
    from plugins.hydrusnetwork.store_proxy import HydrusStoreProxy

    return {"hydrusnetwork": HydrusStoreProxy}


class HydrusNetwork(Plugin):
    PLUGIN_NAME = "hydrusnetwork"
    PLUGIN_ALIASES = ("hydrus",)
    URL = ("hydrus://",)
    EXPOSE_AS_FILE_PROVIDER = True
    MULTI_INSTANCE = True
    ITEM_DETAIL_FIELDS = (("File ID", "file_id", "Hash"),)
    SUPPORTED_CMDLETS = frozenset({
        "add-file", "delete-file", "download-file", "get-file", "get-metadata",
        "tag",
        "get-url", "add-url", "delete-url",
        "get-note", "set-note", "delete-note",
        "search-file",
    })

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        from plugins.hydrusnetwork.store_backend import HydrusStoreOperations
        return HydrusStoreOperations.config_schema()

    @property
    def label(self) -> str:
        return "Hydrus Network"

    def validate(self) -> bool:
        return bool(self._configured_store_names())

    def config_helper_text(self) -> str:
        return "Hydrus uses plugin.hydrusnetwork instances. This plugin delegates to those configured Hydrus instances."

    def extract_query_arguments(self, query: str) -> Tuple[str, Dict[str, Any]]:
        from SYS.utils import split_instance_names

        query_text = str(query or "").strip()
        if not query_text:
            return "", {}

        leftover: List[str] = []
        instances: List[str] = []
        current: List[str] = []
        depth = 0
        for ch in query_text:
            if ch == "[":
                depth += 1
                current.append(ch)
            elif ch == "]":
                depth = max(0, depth - 1)
                current.append(ch)
            elif depth == 0 and ch in {",", " ", "\t"}:
                if current:
                    leftover.append("".join(current))
                    current = []
            else:
                current.append(ch)
        if current:
            leftover.append("".join(current))

        kept: List[str] = []
        for token in leftover:
            if not token:
                continue
            sep_index = token.find(":")
            if sep_index < 0:
                sep_index = token.find("=")
            if sep_index > 0:
                key = token[:sep_index].strip().lower()
                value = token[sep_index + 1 :].strip()
                if key in {"instance", "store"} and value:
                    instances.extend(split_instance_names(value))
                    continue
            kept.append(token)

        text = " ".join(kept).strip()
        filters: Dict[str, Any] = {}
        if instances:
            names = split_instance_names(",".join(instances))
            joined = ",".join(names)
            filters["instance"] = joined
            filters["store"] = joined
        return text, filters

    def get_table_title(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        store_name = str((filters or {}).get("instance") or (filters or {}).get("store") or "").strip()
        query_text = str(query or "").strip() or "*"
        if store_name:
            return f"Hydrus: {query_text} @ {store_name}"
        return f"Hydrus: {query_text}"

    def get_table_metadata(self, query: str, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        meta = super().get_table_metadata(query, filters)
        meta.update({
            "stores": self._configured_store_names(),
            "instance": str((filters or {}).get("instance") or (filters or {}).get("store") or "").strip(),
            "store": str((filters or {}).get("instance") or (filters or {}).get("store") or "").strip(),
        })
        return meta

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        _ = kwargs
        max_results = max(0, int(limit or 0))
        if max_results <= 0:
            return []

        target_store = str((filters or {}).get("instance") or (filters or {}).get("store") or "").strip()
        results: List[SearchResult] = []
        for store_name, backend in self.iter_backends(target_store or None):
            if len(results) >= max_results:
                break
            remaining = max_results - len(results)
            try:
                rows = backend.search(query, limit=remaining) or []
            except Exception:
                continue
            for row in rows:
                if len(results) >= max_results:
                    break
                converted = self._search_result_from_backend_row(store_name, row)
                if converted is not None:
                    results.append(converted)
        return results

    def upload(self, file_path: str, **kwargs: Any) -> str:
        store_name = str(kwargs.get("instance") or kwargs.get("store") or "").strip()
        _, backend = self.resolve_backend(store_name or None, require_explicit=bool(store_name))
        if backend is None:
            if store_name:
                raise RuntimeError(f"Hydrus store '{store_name}' is unavailable")
            raise RuntimeError("No configured Hydrus store is available")
        file_hash = backend.add_file(Path(str(file_path or "")).expanduser(), **kwargs)
        file_url = self.build_file_url(str(file_hash or ""), store_name=backend.get_name())
        return file_url or str(file_hash or "")

    def download_url(self, url: str, output_dir: Path, **_kwargs: Any) -> Optional[Path]:
        store_name, file_hash = self.parse_hydrus_url(url)
        if not file_hash:
            return None
        return self.download_hash_to_temp(file_hash, store_name=store_name, temp_root=output_dir)

    def resolve_pipe_result_download(
        self,
        result: Any,
        pipe_obj: Any,
    ) -> Tuple[Optional[Path], Optional[str], Optional[Path]]:
        metadata = self.item_metadata(result, pipe_obj=pipe_obj)
        file_hash = _normalize_hash(metadata.get("hash") or metadata.get("hash_hex") or metadata.get("path"))
        store_name = str(metadata.get("store") or "").strip() or None
        if not file_hash:
            return None, None, None

        temp_dir = Path(tempfile.mkdtemp(prefix="hydrus-add-file-"))
        downloaded = self.download_hash_to_temp(file_hash, store_name=store_name, temp_root=temp_dir)
        if downloaded is None:
            try:
                temp_dir.rmdir()
            except Exception:
                pass
            return None, None, None
        try:
            if pipe_obj is not None:
                pipe_obj.is_temp = True
        except Exception:
            pass
        return downloaded, file_hash, temp_dir

    def resolve_url(self, url: str, **_kwargs: Any) -> str:
        store_name, file_hash = self.parse_hydrus_url(url)
        if not file_hash:
            return str(url or "")
        resolved = self.build_file_url(file_hash, store_name=store_name)
        return resolved or str(url or "")

    def resolve_playback_path(self, item: Any, **_kwargs: Any) -> Optional[str]:
        metadata = self.item_metadata(item)
        file_hash = _normalize_hash(metadata.get("hash") or metadata.get("hash_hex") or metadata.get("path"))
        store_name = str(metadata.get("store") or "").strip() or None
        if file_hash:
            return self.build_file_url(file_hash, store_name=store_name)
        path_value = metadata.get("path") or metadata.get("url")
        if isinstance(path_value, str) and path_value.strip():
            return self.resolve_url(path_value)
        return None

    def resolve_pipe_item_context(
        self,
        item: Any,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        store: Optional[str] = None,
        file_hash: Optional[str] = None,
        targets: Optional[Sequence[str]] = None,
    ) -> Optional[Tuple[Optional[str], Optional[str]]]:
        _ = item, metadata
        resolved_store = str(store).strip() if store else None
        resolved_hash = _normalize_hash(file_hash)
        matched = False

        try:
            if resolved_store and self.is_store_name(resolved_store):
                matched = True
        except Exception:
            pass

        for target in targets or ():
            try:
                parsed_store, parsed_hash = self.parse_hydrus_url(str(target or ""))
            except Exception:
                parsed_store, parsed_hash = None, ""
            if parsed_hash:
                matched = True
                if not resolved_hash:
                    resolved_hash = parsed_hash
            if parsed_store:
                matched = True
                resolved_store = parsed_store

        if not matched:
            return None

        if resolved_store and resolved_store.upper() in {"PATH", "LOCAL", "UNKNOWN"}:
            resolved_store = None

        return resolved_store, resolved_hash or None

    def infer_playlist_store(
        self,
        item: Any,
        *,
        target: str,
        file_storage: Any = None,
    ) -> Optional[str]:
        _ = item, file_storage
        raw_target = str(target or "").strip()
        if not raw_target:
            return None

        normalized_hash = _normalize_hash(raw_target)
        if normalized_hash:
            matched_store = self._find_store_name_for_hash(normalized_hash)
            return matched_store or "hydrus"

        parsed_store, parsed_hash = self.parse_hydrus_url(raw_target)
        if parsed_hash:
            matched_store = self._find_store_name_for_hash(parsed_hash)
            return matched_store or parsed_store or "hydrus"

        matched_store = self.match_store_name_for_url(raw_target)
        if matched_store:
            return matched_store

        parsed = urlparse(raw_target)
        scheme = str(parsed.scheme or "").lower()
        host = str(parsed.hostname or "").strip().lower()
        path = str(parsed.path or "")
        if scheme == "hydrus":
            return parsed_store or "hydrus"
        if host in {"127.0.0.1", "localhost"} and "get_files" in path:
            return "hydrus"
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host) and "get_files" in path:
            return "hydrus"
        return None

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
        item, _payload, _meta = self.resolve_selection_detail_subject(
            selected_items,
            stage_is_last=stage_is_last,
            source_command=source_command,
            require_media_kind="file",
        )
        if item is None:
            return False

        metadata = self.item_metadata(item)
        file_hash = _normalize_hash(metadata.get("hash") or metadata.get("hash_hex") or metadata.get("path"))
        store_name = str(metadata.get("store") or (table_metadata or {}).get("store") or (table_metadata or {}).get("instance") or "").strip()
        title = str(metadata.get("title") or metadata.get("name") or "").strip()
        if file_hash and not title:
            title = self.get_title(file_hash, store_name=store_name or None)
        if not title:
            title = "Hydrus Item"

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

        direct_url = str(metadata.get("hydrus_url") or metadata.get("url") or "").strip()
        if not direct_url and file_hash:
            direct_url = str(self.build_file_url(file_hash, store_name=store_name or None) or "").strip()
        selection_url = str(metadata.get("selection_url") or metadata.get("path") or "").strip()
        tag_value = metadata.get("tag") or metadata.get("tags") or metadata.get("tags_flat")

        detail_metadata = prepare_detail_metadata(
            item,
            title=title,
            hash_value=file_hash or None,
            store=store_name or None,
            path=direct_url or selection_url or None,
            tags=tag_value,
            extra_fields={
                "Plugin": self.name,
                "Selection Url": selection_url or None,
                "Hydrus Url": direct_url or None,
                "Ext": str(metadata.get("ext") or "").strip() or None,
            },
        )

        return render_selection_detail_view(
            ctx=ctx,
            item=item,
            title=f"Hydrus Item: {title}",
            metadata=detail_metadata,
            table_name=self.name,
            detail_order=["Title", "Instance", "Hash", "Path", "Ext", "Plugin", "Selection URL", "Hydrus URL"],
            value_case="preserve",
        )

    def status_summary(self) -> Dict[str, Any]:
        configured = self._configured_store_names()
        if not configured:
            return {
                "status": "DISABLED",
                "name": self.label,
                "plugin": self.name,
                "detail": "No Hydrus stores configured",
            }

        available: List[str] = []
        unavailable: List[str] = []
        for store_name in configured:
            try:
                resolved_name, backend = self.resolve_backend(store_name, require_explicit=True)
                if backend is not None and resolved_name is not None:
                    available.append(resolved_name)
                else:
                    unavailable.append(store_name)
            except Exception:
                unavailable.append(store_name)

        if available:
            detail = ", ".join(available)
            if unavailable:
                detail = f"available: {detail}; unavailable: {', '.join(unavailable)}"
            return {
                "status": "ENABLED",
                "name": self.label,
                "plugin": self.name,
                "detail": detail,
                "files": None,
            }

        return {
            "status": "DISABLED",
            "name": self.label,
            "plugin": self.name,
            "detail": ", ".join(unavailable) or "Unavailable",
        }

    def is_backend(self, backend: Any, store_name: Optional[str] = None) -> bool:
        if backend is None:
            return False
        backend_type = str(getattr(backend, "STORE_TYPE", "") or "").strip().lower()
        if backend_type == self.name:
            return True
        class_name = type(backend).__name__.strip().lower()
        if class_name == self.name:
            return True
        candidate_name = str(store_name or getattr(backend, "NAME", "") or getattr(backend, "name", "") or "").strip()
        return bool(candidate_name and candidate_name.lower() in {name.lower() for name in self._configured_store_names()})

    def is_store_name(self, store_name: str) -> bool:
        text = str(store_name or "").strip().lower()
        if not text:
            return False
        configured = {name.lower() for name in self._configured_store_names()}
        return text in configured or text in {self.name, "hydrus"}

    def get_client(self, store_name: Optional[str] = None, *, allow_default: bool = True) -> Any:
        _, backend = self.resolve_backend(store_name, require_explicit=not allow_default and bool(store_name))
        if backend is None and store_name and not allow_default:
            return None
        if backend is None:
            return None
        return getattr(backend, "_client", None)

    def hash_exists(self, file_hash: str, *, store_name: Optional[str] = None) -> bool:
        return self.fetch_metadata(file_hash, store_name=store_name) is not None

    def fetch_metadata(self, file_hash: str, *, store_name: Optional[str] = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        normalized = _normalize_hash(file_hash)
        if not normalized:
            return None
        _, backend = self.resolve_backend(store_name)
        if backend is None:
            return None
        fetcher = getattr(backend, "fetch_file_metadata", None)
        if callable(fetcher):
            try:
                payload = fetcher(normalized, **kwargs)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                items = payload.get("metadata")
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    return items[0]
            if isinstance(payload, dict):
                return payload
        getter = getattr(backend, "get_metadata", None)
        if callable(getter):
            try:
                metadata = getter(normalized)
            except Exception:
                metadata = None
            if isinstance(metadata, dict):
                return metadata
        return None

    def find_hashes_by_url(self, url_value: str, *, store_name: Optional[str] = None) -> List[str]:
        normalized_url = str(url_value or "").strip()
        if not normalized_url:
            return []
        seen: set[str] = set()
        out: List[str] = []
        for _store_name, backend in self.iter_backends(store_name):
            try:
                finder = getattr(backend, "find_hashes_by_url", None)
            except Exception:
                finder = None
            if not callable(finder):
                continue
            try:
                hashes = finder(normalized_url) or []
            except Exception:
                continue
            for item in hashes:
                normalized = _normalize_hash(item)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    out.append(normalized)
        return out

    def delete_hash(self, file_hash: str, *, store_name: Optional[str] = None, reason: Optional[str] = None) -> bool:
        return self.delete_hashes([file_hash], store_name=store_name, reason=reason)

    def delete_hashes(
        self,
        file_hashes: Sequence[str],
        *,
        store_name: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        normalized: List[str] = []
        seen: set[str] = set()
        for raw in file_hashes or []:
            item = _normalize_hash(raw)
            if item and item not in seen:
                seen.add(item)
                normalized.append(item)
        if not normalized:
            return False
        _, backend = self.resolve_backend(store_name)
        if backend is None:
            return False
        bulk = getattr(backend, "delete_files_bulk", None)
        if callable(bulk):
            try:
                if bool(bulk(normalized, reason=reason)):
                    return True
            except Exception:
                pass
        deleter = getattr(backend, "delete_file", None)
        if not callable(deleter):
            return False
        any_ok = False
        for item in normalized:
            try:
                if bool(deleter(item, reason=reason)):
                    any_ok = True
            except Exception:
                continue
        return any_ok

    def set_relationship(
        self,
        alt_hash: str,
        king_hash: str,
        kind: str = "alt",
        *,
        store_name: Optional[str] = None,
    ) -> bool:
        alt_norm = _normalize_hash(alt_hash)
        king_norm = _normalize_hash(king_hash)
        if not alt_norm or not king_norm:
            return False
        _, backend = self.resolve_backend(store_name)
        if backend is None:
            return False
        setter = getattr(backend, "set_relationship", None)
        if callable(setter):
            try:
                return bool(setter(alt_norm, king_norm, str(kind or "alt")))
            except Exception:
                return False
        client = getattr(backend, "_client", None)
        if client is None or not hasattr(client, "set_relationship"):
            return False
        try:
            client.set_relationship(alt_norm, king_norm, str(kind or "alt"))
            return True
        except Exception:
            return False

    def get_relationships(self, file_hash: str, *, store_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        normalized = _normalize_hash(file_hash)
        if not normalized:
            return None
        _, backend = self.resolve_backend(store_name)
        if backend is None:
            return None
        getter = getattr(backend, "get_relationships", None)
        if callable(getter):
            try:
                payload = getter(normalized)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                return payload
        client = getattr(backend, "_client", None)
        if client is None or not hasattr(client, "get_file_relationships"):
            return None
        try:
            payload = client.get_file_relationships(normalized)
        except Exception:
            payload = None
        return payload if isinstance(payload, dict) else None

    def get_title(self, file_hash: str, *, store_name: Optional[str] = None) -> str:
        metadata = self.fetch_metadata(file_hash, store_name=store_name)
        if not isinstance(metadata, dict):
            normalized = _normalize_hash(file_hash)
            return normalized[:16] + "..." if normalized else str(file_hash or "")

        for key in ("title", "name"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        tags = metadata.get("tags_flat")
        if isinstance(tags, list):
            for item in tags:
                tag_text = str(item or "").strip()
                if tag_text.lower().startswith("title:"):
                    value = tag_text.split(":", 1)[1].strip()
                    if value:
                        return value

        normalized = _normalize_hash(file_hash)
        return normalized[:16] + "..." if normalized else str(file_hash or "")

    def build_file_url(self, file_hash: str, *, store_name: Optional[str] = None) -> Optional[str]:
        normalized = _normalize_hash(file_hash)
        if not normalized:
            return None
        resolved_store, backend = self.resolve_backend(store_name)
        if backend is None:
            return None
        builder = getattr(backend, "build_file_url", None)
        if callable(builder):
            try:
                url = builder(normalized)
            except Exception:
                url = None
            if isinstance(url, str) and url.strip():
                return url.strip()
        base_url = str(getattr(backend, "URL", "") or "").rstrip("/")
        api_key = str(getattr(backend, "API", "") or "").strip()
        if not base_url:
            return None
        url = f"{base_url}/get_files/file?hash={quote(normalized)}"
        if api_key:
            url = f"{url}&Hydrus-Client-API-Access-Key={quote(api_key)}"
        return url

    def download_hash_to_temp(
        self,
        file_hash: str,
        *,
        store_name: Optional[str] = None,
        temp_root: Optional[Path] = None,
    ) -> Optional[Path]:
        normalized = _normalize_hash(file_hash)
        if not normalized:
            return None
        _, backend = self.resolve_backend(store_name)
        if backend is None:
            return None
        downloader = getattr(backend, "download_to_temp", None)
        if not callable(downloader):
            return None
        try:
            return downloader(normalized, temp_root=temp_root)
        except Exception:
            return None

    def parse_hydrus_url(self, url: str) -> Tuple[Optional[str], str]:
        text = str(url or "").strip()
        if not text:
            return None, ""
        parsed = urlparse(text)
        scheme = str(parsed.scheme or "").lower()
        if scheme == "hydrus":
            store_name = str(parsed.netloc or "").strip() or None
            path_hash = _normalize_hash(unquote(parsed.path or "").strip("/"))
            if path_hash:
                return store_name, path_hash
            query_hash = _normalize_hash(parse_qs(parsed.query).get("hash", [""])[0])
            return store_name, query_hash

        query_hash = _normalize_hash(parse_qs(parsed.query).get("hash", [""])[0])
        if query_hash:
            matched_store = self.match_store_name_for_url(text)
            return matched_store, query_hash

        direct_hash = _normalize_hash(text)
        if direct_hash:
            return None, direct_hash
        return None, ""

    def match_store_name_for_url(self, url: str) -> Optional[str]:
        target = str(url or "").strip().lower()
        if not target:
            return None
        for store_name, backend in self.iter_backends():
            base_url = str(getattr(backend, "URL", "") or "").strip().rstrip("/").lower()
            if base_url and target.startswith(base_url):
                return store_name
        return None

    def item_metadata(self, item: Any, *, pipe_obj: Any = None) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        for source in (item, pipe_obj):
            if isinstance(source, dict):
                metadata.update({k: v for k, v in source.items() if v is not None})
                nested = source.get("full_metadata") or source.get("metadata")
                if isinstance(nested, dict):
                    metadata.update(nested)
            elif source is not None:
                for attr in ("title", "path", "url", "store", "hash", "hash_hex"):
                    try:
                        value = getattr(source, attr, None)
                    except Exception:
                        value = None
                    if value is not None:
                        metadata.setdefault(attr, value)
                try:
                    nested = getattr(source, "full_metadata", None) or getattr(source, "metadata", None)
                except Exception:
                    nested = None
                if isinstance(nested, dict):
                    metadata.update(nested)
        return metadata

    def _find_store_name_for_hash(self, file_hash: str) -> Optional[str]:
        normalized = _normalize_hash(file_hash)
        if not normalized:
            return None
        try:
            for store_name, _backend in self.iter_backends():
                try:
                    if self.hash_exists(normalized, store_name=str(store_name)):
                        return str(store_name)
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def iter_backends(self, store_name: Optional[str] = None) -> Iterable[Tuple[str, Any]]:
        from SYS.utils import split_instance_names

        target = str(store_name or "").strip()
        names = split_instance_names(target) if target else []
        if names:
            seen: set[str] = set()
            for name in names:
                resolved_name, backend = self.resolve_backend(name, require_explicit=True)
                if backend is None or resolved_name is None:
                    continue
                key = str(resolved_name).strip().lower()
                if key in seen:
                    continue
                seen.add(key)
                yield resolved_name, backend
            return

        for configured_name in self._configured_store_names():
            resolved_name, backend = self.resolve_backend(configured_name, require_explicit=True)
            if backend is not None and resolved_name is not None:
                yield resolved_name, backend

    def resolve_backend(
        self,
        store_name: Optional[str] = None,
        *,
        storage: Optional[Any] = None,
        require_explicit: bool = False,
    ) -> Tuple[Optional[str], Any]:
        _ = storage
        configured = self._configured_store_names()
        target = str(store_name or "").strip()
        candidates: List[str]
        if target:
            if self.is_store_name(target) and target.lower() in {self.name, "hydrus"}:
                candidates = configured
            else:
                candidates = [target]
        else:
            candidates = configured[:1]

        for candidate in candidates:
            try:
                resolved_name, backend = self._get_backend_instance(candidate)
                if backend is not None and self.is_backend(backend, resolved_name):
                    return resolved_name, backend
            except Exception:
                continue

        if require_explicit:
            return None, None
        return None, None

    def _configured_store_entries(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Return (display_name, entry_dict) pairs for all configured Hydrus instances.

        Uses plugin/provider config only.
        """
        entries: List[Tuple[str, Dict[str, Any]]] = []
        seen_keys: set[str] = set()

        def _add_from_dict(source: Dict[str, Any]) -> None:
            for instance_name, entry in source.items():
                if not isinstance(entry, dict):
                    continue
                display_name = str(entry.get("NAME") or instance_name or "").strip()
                if not display_name:
                    continue
                key = display_name.lower()
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                seen_keys.add(str(instance_name or "").strip().lower())
                normalized_entry = dict(entry)
                normalized_entry["NAME"] = display_name
                entries.append((display_name, normalized_entry))

        # New format: plugin/provider section (config["plugin"]["hydrusnetwork"])
        plugin_instances = self.plugin_instance_configs()
        # plugin_instance_configs returns {"default": {...}} for single-instance config.
        # For hydrusnetwork we only want true named instances.
        if plugin_instances and set(plugin_instances.keys()) != {"default"}:
            _add_from_dict(plugin_instances)

        return entries

    def _configured_store_lookup(self) -> Dict[str, Tuple[str, Dict[str, Any]]]:
        """Build a case-insensitive lookup dict from all configured Hydrus instances."""
        lookup: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        for display_name, entry in self._configured_store_entries():
            # Index by display name and by the raw instance key (may differ if NAME overrides)
            for key in (display_name.lower(), str(entry.get("_instance_key") or "").strip().lower()):
                if key:
                    lookup.setdefault(key, (display_name, entry))
        return lookup

    def _instance_cache(self) -> Dict[str, Any]:
        cache = getattr(self, "_hydrus_instance_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_hydrus_instance_cache", cache)
        return cache

    def _instance_error_cache(self) -> Dict[str, str]:
        cache = getattr(self, "_hydrus_instance_error_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_hydrus_instance_error_cache", cache)
        return cache

    def _get_backend_instance(self, store_name: str) -> Tuple[str, Any]:
        lookup = self._configured_store_lookup()
        display_name, entry = lookup.get(str(store_name or "").strip().lower(), ("", {}))
        if not display_name or not isinstance(entry, dict):
            raise KeyError(store_name)

        cache = self._instance_cache()
        if display_name in cache:
            return display_name, cache[display_name]

        error_cache = self._instance_error_cache()
        if display_name in error_cache:
            raise RuntimeError(error_cache[display_name])

        url = str(entry.get("URL") or "").strip()
        api_key = str(entry.get("API") or "").strip()
        if not url or not api_key:
            message = f"Hydrus instance '{display_name}' is missing URL or API"
            error_cache[display_name] = message
            raise RuntimeError(message)

        try:
            backend = HydrusStoreOperations(NAME=display_name, API=api_key, URL=url)
        except Exception as exc:
            message = str(exc)
            error_cache[display_name] = message
            raise

        cache[display_name] = backend
        return display_name, backend

    def _configured_store_names(self) -> List[str]:
        return [display_name for display_name, _entry in self._configured_store_entries()]

    def _resolve_backend_for_kwargs(self, kwargs: Dict[str, Any]) -> Tuple[str, Any]:
        """Resolve a backend instance from kwargs containing 'instance' or 'store' keys."""
        instance_name = (
            str(kwargs.pop("instance", "") or "").strip()
            or str(kwargs.pop("store", "") or "").strip()
            or None
        )
        _, backend = self.resolve_backend(instance_name)
        if backend is None:
            configured = self._configured_store_names()
            desc = f"'{instance_name}'" if instance_name else "any"
            raise RuntimeError(
                f"No Hydrus instance found for {desc}. "
                f"Configured instances: {configured or 'none'}"
            )
        return instance_name or "", backend

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

    def __getattr__(self, name: str) -> Any:
        if name in {
            "add_file",
            "get_file",
            "get_metadata",
            "get_tag",
            "add_tag",
            "delete_tag",
            "get_url",
            "add_url",
            "delete_url",
            "get_note",
            "set_note",
            "delete_note",
        }:
            def _forward(*args: Any, **kwargs: Any) -> Any:
                _n, backend = self._resolve_backend_for_kwargs(kwargs)
                return getattr(backend, name)(*args, **kwargs)

            return _forward
        raise AttributeError(name)

    @staticmethod
    def _build_tag_column_text(tags: Sequence[Any]) -> str:
        namespace_tags: List[str] = []
        seen: set[str] = set()
        for raw in tags or []:
            text = str(raw or "").strip()
            if not text or ":" not in text:
                continue
            ns, value = text.split(":", 1)
            ns_norm = ns.strip().lower()
            value_norm = value.strip()
            if not value_norm or ns_norm == "title":
                continue
            normalized = f"{ns_norm}:{value_norm}"
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            namespace_tags.append(normalized)
        return ", ".join(namespace_tags)

    @staticmethod
    def _format_size_column(size_value: Optional[int]) -> str:
        if size_value is None:
            return ""
        try:
            bytes_value = int(size_value)
        except Exception:
            return ""
        if bytes_value < 0:
            return ""
        if bytes_value >= 1024 ** 3:
            value = bytes_value / float(1024 ** 3)
            unit = "GB"
        else:
            value = bytes_value / float(1024 ** 2)
            unit = "MB"
        number = f"{value:.2f}".rstrip("0").rstrip(".")
        return f"{number} {unit}"

    def _search_result_from_backend_row(self, store_name: str, row: Any) -> Optional[SearchResult]:
        if not isinstance(row, dict):
            return None
        file_hash = _normalize_hash(row.get("hash") or row.get("hash_hex"))
        if not file_hash:
            return None
        title = str(row.get("title") or row.get("name") or f"Hydrus_{file_hash[:12]}").strip()
        size_value = row.get("size_bytes")
        if size_value is None:
            size_value = row.get("size")
        try:
            size_int = int(size_value) if size_value is not None else None
        except Exception:
            size_int = None
        tags = row.get("tag") or row.get("tags") or []
        tag_list = [str(item) for item in tags if isinstance(item, str)] if isinstance(tags, list) else []
        tag_text = self._build_tag_column_text(tag_list)
        playable_url = self.build_file_url(file_hash, store_name=store_name) or row.get("url") or ""
        selection_url = f"hydrus://{store_name}/{file_hash}"
        selection_args, selection_action = build_hash_store_selection(file_hash, store_name)
        metadata = {
            "provider": self.name,
            "store": store_name,
            "hash": file_hash,
            "hash_hex": file_hash,
            "hydrus_url": playable_url,
            "selection_url": selection_url,
            "title": title,
            "tag": list(tag_list),
            "ext": str(row.get("ext") or "").strip(),
            "url": row.get("url"),
            "name": str(row.get("name") or title),
            "mime": row.get("mime"),
            "file_id": row.get("file_id"),
            "size": size_int,
            "size_bytes": size_int,
        }
        result = SearchResult(
            table=self.name,
            title=title,
            path=selection_url,
            detail=store_name,
            annotations=[store_name],
            media_kind="file",
            size_bytes=size_int,
            tag={"hydrusnetwork", store_name, *tag_list},
            columns=[
                ("Title", title),
                ("Tag", tag_text),
                ("Instance", store_name),
                ("Plugin", self.name),
                ("Size", self._format_size_column(size_int)),
                ("Ext", str(row.get("ext") or "")),
            ],
            selection_action=selection_action,
            selection_args=selection_args,
            full_metadata=metadata,
        )
        result.url = row.get("url")
        result.hash = file_hash
        result.hash_hex = file_hash
        result.store = store_name
        result.name = str(row.get("name") or title)
        result.mime = row.get("mime")
        result.file_id = row.get("file_id")
        result.ext = str(row.get("ext") or "").strip()
        result.size = size_int
        return result