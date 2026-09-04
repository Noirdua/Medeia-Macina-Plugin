from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PluginCore.base import Plugin, SearchResult
from SYS.metadata import (
    _read_sidecar_metadata,
    is_sidecar_filename,
    parse_sidecar_text,
    read_tags_from_file,
    write_merged_sidecar,
)
from SYS.utils import coerce_bool
from SYS.utils import sanitize_filename, sha256_file, unique_path


def _format_size_safe(size_bytes: Any) -> str:
    if size_bytes is None:
        return ""
    try:
        size = int(size_bytes)
    except (TypeError, ValueError):
        return str(size_bytes or "")
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _copy_sidecars(source_path: Path, target_path: Path) -> None:
    possible_sidecars = [
        source_path.with_suffix(source_path.suffix + ".json"),
        source_path.with_name(source_path.name + ".tag"),
        source_path.with_name(source_path.name + ".metadata"),
        source_path.with_name(source_path.name + ".notes"),
    ]
    for sidecar in possible_sidecars:
        try:
            if not sidecar.exists():
                continue
            suffix_part = sidecar.name.replace(source_path.name, "", 1)
            target_sidecar = target_path.parent / f"{target_path.name}{suffix_part}"
            target_sidecar.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(sidecar), target_sidecar)
        except Exception:
            continue


def _copy_with_progress(
    source_path: Path,
    target_path: Path,
    *,
    pipeline_progress: Any = None,
    label: str = "local export",
    chunk_size: int = 1024 * 1024,
) -> None:
    total_bytes: Optional[int] = None
    try:
        total_bytes = int(source_path.stat().st_size)
    except Exception:
        total_bytes = None

    transfer_started = False
    completed = 0
    transfer_label = str(label or target_path.name or source_path.name)
    try:
        if pipeline_progress is not None and hasattr(pipeline_progress, "begin_transfer"):
            pipeline_progress.begin_transfer(
                label=transfer_label,
                total=total_bytes if isinstance(total_bytes, int) and total_bytes > 0 else None,
            )
            transfer_started = True

        with source_path.open("rb") as src, target_path.open("wb") as dst:
            while True:
                chunk = src.read(max(4096, int(chunk_size or 0) or 1024 * 1024))
                if not chunk:
                    break
                dst.write(chunk)
                completed += len(chunk)
                if pipeline_progress is not None and hasattr(pipeline_progress, "update_transfer"):
                    pipeline_progress.update_transfer(
                        label=transfer_label,
                        completed=completed,
                        total=total_bytes if isinstance(total_bytes, int) and total_bytes > 0 else None,
                    )

        shutil.copystat(str(source_path), str(target_path))
    finally:
        if pipeline_progress is not None and transfer_started and hasattr(pipeline_progress, "finish_transfer"):
            try:
                pipeline_progress.finish_transfer(label=transfer_label)
            except Exception:
                pass


class Local(Plugin):
    PLUGIN_NAME = "local"
    PLUGIN_ALIASES = ("filesystem", "fs")
    MULTI_INSTANCE = True
    SUPPORTED_CMDLETS = frozenset({"add-file", "search-file"})

    @property
    def label(self) -> str:
        return "Local Filesystem"

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "path",
                "label": "Destination Path",
                "type": "path",
                "default": "",
                "required": True,
                "placeholder": "~/Downloads",
            },
            {
                "key": "create_dirs",
                "label": "Create Missing Directories",
                "type": "boolean",
                "default": True,
            },
        ]

    def config_helper_text(self) -> str:
        return "Configure named local export destinations and use add-file -plugin local -instance <name|path>."

    @staticmethod
    def _looks_like_path(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if text.startswith((".", "~")):
            return True
        if "\\" in text or "/" in text:
            return True
        if len(text) >= 2 and text[1] == ":":
            return True
        return False

    def _settings_from_config(
        self,
        conf: Optional[Dict[str, Any]],
        *,
        instance_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        entry = dict(conf or {})
        path_value = str(entry.get("path") or entry.get("PATH") or "").strip()
        return {
            "instance": str(instance_name or entry.get("_instance_name") or "").strip() or None,
            "path": path_value,
            "create_dirs": bool(entry.get("create_dirs", entry.get("createDirs", True))),
        }

    def resolve_destination(
        self,
        instance_name: Optional[str] = None,
        *,
        require_explicit: bool = False,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        requested = str(instance_name or "").strip()
        if requested:
            resolved_name, conf = self.resolve_plugin_instance(requested, require_explicit=True)
            settings = self._settings_from_config(conf, instance_name=resolved_name)
            if settings.get("path"):
                return resolved_name or requested, settings
            if self._looks_like_path(requested):
                return requested, {
                    "instance": requested,
                    "path": requested,
                    "create_dirs": True,
                }
            if require_explicit:
                return None, {}

        resolved_name, conf = self.resolve_plugin_instance(None, require_explicit=False)
        settings = self._settings_from_config(conf, instance_name=resolved_name)
        if settings.get("path"):
            return resolved_name, settings
        return None, {}

    def validate(self) -> bool:
        return True

    @staticmethod
    def _infer_media_kind(ext: str) -> str:
        e = str(ext or "").strip().lower().lstrip(".")
        if e in {"mp3", "flac", "wav", "ogg", "opus", "m4a", "aac", "wma", "aiff"}:
            return "audio"
        if e in {"mp4", "mkv", "avi", "mov", "webm", "wmv", "flv", "m4v"}:
            return "video"
        if e in {"pdf", "epub", "mobi", "azw3", "djvu", "cbr", "cbz", "chm"}:
            return "book"
        if e in {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"}:
            return "archive"
        if e in {"exe", "msi", "apk", "deb", "rpm", "appimage", "bin"}:
            return "software"
        if e in {"jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "tiff", "ico"}:
            return "image"
        return "file"

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        q = str(query or "").strip()
        match_all = not q or q == "*"
        query_tokens = [t.lower() for t in q.split()] if not match_all else []
        max_results = max(1, int(limit))

        target_instance = None
        show_all = False
        if filters:
            target_instance = str(filters.get("instance") or filters.get("store") or "").strip() or None
            show_all = coerce_bool(filters.get("all"), False)

        results: List[SearchResult] = []
        instances = self.plugin_instance_configs()

        for inst_name, inst_cfg in instances.items():
            if target_instance and str(inst_name or "").strip().lower() != target_instance.lower():
                continue

            settings = self._settings_from_config(inst_cfg, instance_name=inst_name)
            root_text = str(settings.get("path") or "").strip()
            if not root_text:
                continue

            root = Path(root_text).expanduser()
            if not root.is_dir():
                continue

            try:
                for entry in root.rglob("*"):
                    if len(results) >= max_results:
                        break
                    if not entry.is_file():
                        continue
                    if is_sidecar_filename(entry.name):
                        continue

                    file_name = entry.name
                    file_stem = entry.stem

                    tag_path = entry.parent / (file_name + ".tag")
                    meta_path = entry.parent / (file_name + ".metadata")

                    has_tag = tag_path.is_file()
                    has_meta = meta_path.is_file()
                    if not show_all and not has_meta and not has_tag:
                        continue

                    tags: List[str] = []
                    hash_value: Optional[str] = None
                    urls: List[str] = []
                    if has_meta:
                        try:
                            hash_value, tags, urls = _read_sidecar_metadata(meta_path)
                        except Exception:
                            tags = []
                    if has_tag:
                        try:
                            legacy = read_tags_from_file(tag_path)
                            for tag in legacy:
                                if tag not in tags:
                                    tags.append(tag)
                        except Exception:
                            pass

                    if not match_all and query_tokens:
                        url_text = " ".join(urls) if urls else ""
                        search_text = " ".join([file_name.lower(), file_stem.lower().replace("_", " "), *tags, url_text])
                        if hash_value:
                            search_text += " " + hash_value.lower()
                        if not all(token in search_text for token in query_tokens):
                            continue

                    try:
                        size_bytes = int(entry.stat().st_size)
                    except Exception:
                        size_bytes = None

                    ext = entry.suffix.lstrip(".")
                    media_kind = self._infer_media_kind(ext)

                    inst_label = str(inst_name or "").strip()
                    if inst_label.lower() == "default":
                        inst_label = ""

                    store_label = f"local:{inst_label}" if inst_label else "local"
                    tag_text = ", ".join(tags[:8]) if tags else ""

                    metadata: Dict[str, Any] = {
                        "store": store_label,
                        "ext": ext,
                        "size": size_bytes,
                    }
                    if inst_label:
                        metadata["instance"] = inst_label
                    if hash_value:
                        metadata["hash"] = hash_value
                    if urls:
                        metadata["url"] = urls

                    sr = SearchResult(
                        table="local",
                        title=file_name,
                        path=str(entry),
                        detail=store_label,
                        annotations=[store_label],
                        media_kind=media_kind,
                        size_bytes=size_bytes,
                        tag=set(tags),
                        columns=[
                            ("Title", file_name),
                            ("Tag", tag_text),
                            ("Instance", store_label),
                            ("Plugin", self.name),
                            ("Size", _format_size_safe(size_bytes)),
                            ("Ext", ext),
                        ],
                        full_metadata=metadata,
                    )
                    sr.ext = ext
                    sr.size = size_bytes
                    if hash_value:
                        sr.hash = hash_value
                    results.append(sr)

            except Exception:
                continue

        return results[:max_results]

    @staticmethod
    def _folder_name_from_pipe(pipe_obj: Any) -> str:
        metadata = getattr(pipe_obj, "metadata", None)
        extra = getattr(pipe_obj, "extra", None)
        if not isinstance(metadata, dict):
            metadata = {}
        if not isinstance(extra, dict):
            extra = {}

        plugin_name = str(
            getattr(pipe_obj, "plugin", None)
            or metadata.get("plugin")
            or extra.get("plugin")
            or ""
        ).strip().lower()
        has_alldebrid_metadata = any(
            isinstance(source, dict)
            and (source.get("magnet_id") is not None or source.get("plugin") == "alldebrid")
            for source in (metadata, extra)
        )
        if plugin_name != "alldebrid" and not has_alldebrid_metadata:
            return ""

        for source in (metadata, extra):
            for key in ("folder_name", "folder", "magnet_name"):
                value = str(source.get(key) or "").strip()
                if value:
                    return value

            magnet = source.get("magnet")
            if isinstance(magnet, dict):
                for key in ("filename", "name", "hash"):
                    value = str(magnet.get(key) or "").strip()
                    if value:
                        return value
        return ""

    def upload(self, file_path: str, **kwargs: Any) -> Dict[str, Any]:
        source_path = Path(str(file_path or "")).expanduser()
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(f"File not found: {source_path}")

        pipeline_progress = kwargs.get("pipeline_progress")

        def _set_status(text: str) -> None:
            if pipeline_progress is None or not hasattr(pipeline_progress, "set_status"):
                return
            try:
                pipeline_progress.set_status(f"local: {text}")
            except Exception:
                pass

        def _clear_status() -> None:
            if pipeline_progress is None or not hasattr(pipeline_progress, "clear_status"):
                return
            try:
                pipeline_progress.clear_status()
            except Exception:
                pass

        try:
            requested_instance = str(kwargs.get("instance") or kwargs.get("store") or "").strip() or None
            resolved_name, settings = self.resolve_destination(
                requested_instance,
                require_explicit=bool(requested_instance),
            )
            destination_text = str(settings.get("path") or "").strip()
            if not destination_text:
                requested_label = requested_instance or "<default>"
                raise ValueError(
                    f"Local destination '{requested_label}' is not configured. Use -plugin local -instance <name|path>."
                )

            destination_root = Path(destination_text).expanduser()
            create_dirs = bool(settings.get("create_dirs", True))
            if create_dirs:
                destination_root.mkdir(parents=True, exist_ok=True)
            elif not destination_root.exists():
                raise FileNotFoundError(f"Destination directory does not exist: {destination_root}")
            elif not destination_root.is_dir():
                raise NotADirectoryError(f"Destination is not a directory: {destination_root}")

            direct_export_download = bool(kwargs.get("direct_export_download", False))
            folder_name = str(kwargs.get("folder_name") or "").strip()
            if not folder_name and not direct_export_download:
                folder_name = self._folder_name_from_pipe(kwargs.get("pipe_obj"))
            if not folder_name and not direct_export_download:
                title_hint = str(kwargs.get("title") or "").strip()
                if not title_hint:
                    title_hint = source_path.stem.replace("_", " ").strip()
                if title_hint:
                    folder_name = title_hint

            export_root = destination_root
            if folder_name and not direct_export_download:
                export_root = destination_root / sanitize_filename(folder_name)
                if create_dirs:
                    export_root.mkdir(parents=True, exist_ok=True)
                elif not export_root.is_dir():
                    raise FileNotFoundError(f"Destination directory does not exist: {export_root}")

            title = str(kwargs.get("title") or "").strip()
            if not title:
                title = source_path.stem.replace("_", " ").strip()
            base_name = sanitize_filename(title or source_path.stem)

            file_ext = source_path.suffix
            if file_ext and base_name.lower().endswith(file_ext.lower()):
                target_name = base_name
            else:
                target_name = base_name + file_ext

            target_path = source_path if direct_export_download else export_root / target_name

            if not direct_export_download:
                if target_path.exists():
                    target_path = unique_path(target_path)
                _set_status(f"copying {target_path.name}")
                _copy_with_progress(
                    source_path,
                    target_path,
                    pipeline_progress=pipeline_progress,
                    label=str(target_path.name or source_path.name or "local export"),
                )
                _copy_sidecars(source_path, target_path)
            else:
                _set_status(f"finalizing {target_path.name}")

            tags = list(kwargs.get("tags") or [])
            urls = list(kwargs.get("urls") or [])
            hash_value = str(kwargs.get("hash_value") or "").strip() or None
            if not hash_value:
                try:
                    hash_value = sha256_file(target_path)
                except Exception:
                    hash_value = None

            relationships = kwargs.get("relationships")
            try:
                if coerce_bool(kwargs.get("write_metadata"), True):
                    _set_status(f"writing metadata for {target_path.name}")
                    write_merged_sidecar(
                        target_path,
                        tags=tags,
                        urls=urls,
                        hash_value=hash_value,
                        relationships=relationships or [],
                    )
            except Exception:
                pass

            extra_updates: Dict[str, Any] = {
                "url": urls,
                "export_path": str(destination_root),
            }
            if export_root != destination_root:
                extra_updates["export_folder"] = str(export_root)
            if resolved_name:
                extra_updates["instance"] = resolved_name
            if relationships:
                extra_updates["relationships"] = relationships

            return {
                "hash": hash_value or "unknown",
                "store": "local",
                "plugin": self.name,
                "path": str(target_path),
                "tag": tags,
                "title": title or target_path.name,
                "relationships": relationships,
                "extra": extra_updates,
            }
        finally:
            _clear_status()