from __future__ import annotations

import fnmatch
import ftplib
import io
import posixpath
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from PluginCore.base import Plugin, SearchResult, parse_inline_query_arguments
from SYS.metadata import build_sidecar_payloads, is_sidecar_filename, parse_sidecar_text
from SYS.utils import coerce_bool as _coerce_bool, coerce_int as _coerce_int, format_byte_size, unique_path as _unique_path


def _format_timestamp(raw_value: Any) -> str:
    text = str(raw_value or "").strip()
    if not text:
        return ""
    for pattern in ("%Y%m%d%H%M%S", "%Y%m%d%H%M%S.%f"):
        try:
            parsed = datetime.strptime(text, pattern)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except Exception:
            continue
    return text


def _safe_filename(name: Any) -> str:
    raw = str(name or "").strip()
    if not raw:
        raw = "download"
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", ".", " "} else "_" for ch in raw)
    cleaned = cleaned.strip(" ._")
    return cleaned or "download"


class FTP(Plugin):
    PLUGIN_NAME = "ftp"
    URL = ("ftp://", "ftps://")
    MULTI_INSTANCE = True
    SUPPORTED_CMDLETS = frozenset({"add-file", "delete-file", "download-file", "search-file"})

    @property
    def label(self) -> str:
        return "FTP"

    @property
    def preserve_order(self) -> bool:
        return True

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "host",
                "label": "Host",
                "default": "",
                "required": True,
                "placeholder": "ftp.example.com",
            },
            {
                "key": "port",
                "label": "Port",
                "type": "integer",
                "default": 21,
            },
            {
                "key": "username",
                "label": "Username",
                "default": "anonymous",
            },
            {
                "key": "password",
                "label": "Password",
                "type": "secret",
                "secret": True,
                "default": "",
            },
            {
                "key": "base_path",
                "label": "Base Path",
                "default": "/",
                "placeholder": "/incoming",
            },
            {
                "key": "tls",
                "label": "Use FTPS",
                "type": "boolean",
                "default": False,
            },
            {
                "key": "passive",
                "label": "Passive Mode",
                "type": "boolean",
                "default": True,
            },
            {
                "key": "timeout",
                "label": "Timeout Seconds",
                "type": "integer",
                "default": 20,
            },
            {
                "key": "search_depth",
                "label": "Default Search Depth",
                "type": "integer",
                "default": 1,
            },
        ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        _instance_name, conf = self.resolve_plugin_instance()
        defaults = self._settings_from_config(conf)
        self._host = str(defaults.get("host") or "").strip()
        self._tls = bool(defaults.get("tls"))
        self._port = int(defaults.get("port") or 21)
        self._username = str(defaults.get("username") or "anonymous").strip() or "anonymous"
        self._password = str(defaults.get("password") or "anonymous@").strip() or "anonymous@"
        self._passive = bool(defaults.get("passive"))
        self._timeout = max(1, int(defaults.get("timeout") or 20))
        self._search_depth = max(0, int(defaults.get("search_depth") or 1))
        self._base_path = self._normalize_remote_path(defaults.get("base_path") or "/", default="/")

    def _settings_from_config(self, conf: Optional[Dict[str, Any]], *, instance_name: Optional[str] = None) -> Dict[str, Any]:
        entry = dict(conf or {})
        password_value = entry.get("password")
        return {
            "instance": str(instance_name or entry.get("_instance_name") or "").strip() or None,
            "host": str(entry.get("host") or "").strip(),
            "tls": _coerce_bool(entry.get("tls"), False),
            "port": _coerce_int(entry.get("port"), 21),
            "username": str(entry.get("username") or entry.get("user") or "anonymous").strip() or "anonymous",
            "password": str(password_value).strip() if password_value not in (None, "") else "anonymous@",
            "passive": _coerce_bool(entry.get("passive"), True),
            "timeout": max(1, _coerce_int(entry.get("timeout"), 20)),
            "search_depth": max(0, _coerce_int(entry.get("search_depth"), 1)),
            "base_path": self._normalize_remote_path(entry.get("base_path") or "/", default="/"),
        }

    def _resolve_settings(
        self,
        *,
        filters: Optional[Dict[str, Any]] = None,
        instance_name: Optional[str] = None,
        require_explicit: bool = False,
    ) -> Dict[str, Any]:
        requested = self.requested_instance_name(filters, instance=instance_name)
        resolved_name, conf = self.resolve_plugin_instance(
            requested,
            require_explicit=require_explicit or bool(requested),
        )
        settings = self._settings_from_config(conf, instance_name=resolved_name)
        if settings.get("instance") is None and requested:
            settings["instance"] = requested
        return settings

    def validate(self) -> bool:
        settings = self._resolve_settings()
        return bool(settings.get("host"))

    def config_helper_text(self) -> str:
        return "Test the configured FTP/FTPS settings before searching or uploading."

    def config_actions(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "test_connection",
                "label": "Test connection",
                "variant": "primary",
            }
        ]

    def run_config_action(self, action_id: str, **_kwargs: Any) -> Dict[str, Any]:
        if str(action_id or "").strip().lower() != "test_connection":
            return super().run_config_action(action_id, **_kwargs)

        settings = self._resolve_settings()
        if not settings.get("host"):
            return {"ok": False, "message": "Set 'host' before testing the FTP connection."}

        ftp = None
        try:
            ftp = self._connect(settings=settings)
            active_path = str(settings.get("base_path") or "/")
            try:
                ftp.cwd(active_path)
                resolved_path = ftp.pwd()
            except Exception:
                resolved_path = active_path
            return {
                "ok": True,
                "message": f"Connected to FTP {settings.get('host')}:{settings.get('port')} and reached {resolved_path}.",
            }
        except Exception as exc:
            return {"ok": False, "message": f"FTP connection failed: {exc}"}
        finally:
            self._close(ftp)

    def extract_query_arguments(self, query: str) -> Tuple[str, Dict[str, Any]]:
        text, inline = parse_inline_query_arguments(query)
        filters: Dict[str, Any] = {}

        instance_name = str(inline.get("instance") or inline.get("store") or "").strip()
        if instance_name:
            filters["instance"] = instance_name

        if inline.get("path"):
            filters["path"] = inline.get("path")
        if inline.get("depth"):
            filters["depth"] = max(0, _coerce_int(inline.get("depth"), self._search_depth))
        if inline.get("type"):
            filters["type"] = str(inline.get("type") or "").strip().lower()

        return text, filters

    def get_table_title(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        settings = self._resolve_settings(filters=filters)
        active_path = self._normalize_remote_path((filters or {}).get("path") or settings.get("base_path") or "/", default=str(settings.get("base_path") or "/"))
        instance_name = str(settings.get("instance") or "").strip()
        text = str(query or "").strip()
        if not text or text == "*":
            return f"FTP{f'[{instance_name}]' if instance_name else ''}: {active_path}"
        return f"FTP{f'[{instance_name}]' if instance_name else ''}: {text} @ {active_path}"

    def get_table_metadata(self, query: str, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        settings = self._resolve_settings(filters=filters)
        return {
            "plugin": self.name,
            "instance": settings.get("instance"),
            "host": settings.get("host"),
            "path": self._normalize_remote_path((filters or {}).get("path") or settings.get("base_path") or "/", default=str(settings.get("base_path") or "/")),
            "query": str(query or "").strip(),
        }

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        _ = kwargs
        active_filters = dict(filters or {})
        settings = self._resolve_settings(filters=active_filters, require_explicit=True)
        if not settings.get("host"):
            requested = self.requested_instance_name(active_filters)
            if requested:
                raise RuntimeError(f"FTP instance '{requested}' is unavailable")
            return []
        start_path = self._normalize_remote_path(active_filters.get("path") or settings.get("base_path") or "/", default=str(settings.get("base_path") or "/"))
        search_depth = max(0, _coerce_int(active_filters.get("depth"), int(settings.get("search_depth") or self._search_depth)))
        type_filter = str(active_filters.get("type") or "any").strip().lower()
        show_all = _coerce_bool(active_filters.get("all"), False)
        needle = str(query or "").strip()
        max_results = max(0, int(limit or 0))
        if max_results <= 0:
            return []

        ftp = self._connect(settings=settings)
        try:
            return self._search_directory(
                ftp,
                start_path,
                needle=needle,
                limit=max_results,
                search_depth=search_depth,
                type_filter=type_filter,
                settings=settings,
                show_all=show_all,
            )
        finally:
            self._close(ftp)

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any,
    ) -> bool:
        if not stage_is_last:
            return False

        target_path = ""
        target_title = ""
        instance_name = ""
        for item in selected_items or []:
            metadata = self._item_metadata(item)
            if not metadata.get("is_dir"):
                continue
            settings = self._resolve_settings(instance_name=str(metadata.get("instance") or "").strip() or None, require_explicit=bool(metadata.get("instance")))
            target_path = self._normalize_remote_path(metadata.get("ftp_path") or metadata.get("selection_path"), default=str(settings.get("base_path") or "/"))
            target_title = str(metadata.get("title") or metadata.get("name") or "").strip()
            instance_name = str(settings.get("instance") or metadata.get("instance") or "").strip()
            if target_path:
                break

        if not target_path:
            return False

        settings = self._resolve_settings(instance_name=instance_name or None, require_explicit=bool(instance_name))
        ftp = self._connect(settings=settings)
        try:
            rows = self._search_directory(
                ftp,
                target_path,
                needle="*",
                limit=500,
                search_depth=0,
                type_filter="any",
                settings=settings,
            )
        finally:
            self._close(ftp)

        try:
            from SYS.result_table import Table
            from SYS.rich_display import stdout_console
        except Exception:
            return True

        title = target_title or target_path
        table = Table(f"FTP{f'[{instance_name}]' if instance_name else ''}: {title}")._perseverance(True)
        table.set_table("ftp")
        try:
            table.set_table_metadata({
                "plugin": "ftp",
                "instance": instance_name or None,
                "host": settings.get("host"),
                "path": target_path,
                "view": "directory",
            })
        except Exception:
            pass
        source_args = ["-plugin", "ftp"]
        if instance_name:
            source_args.extend(["-instance", instance_name])
        source_args.extend([f"path:{target_path}", "*"])
        table.set_source_command("search-file", source_args)

        payloads: List[Dict[str, Any]] = []
        for row in rows:
            table.add_result(row)
            payloads.append(row.to_dict())

        try:
            ctx.set_last_result_table(table, payloads, subject={"plugin": "ftp", "instance": instance_name or None, "path": target_path})
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
        item, _payload, _meta = self.resolve_selection_detail_subject(
            selected_items,
            stage_is_last=stage_is_last,
            source_command=source_command,
            require_media_kind="file",
        )
        if item is None:
            return False

        metadata = self._item_metadata(item)
        if bool(metadata.get("is_dir")):
            return False

        title = str(metadata.get("title") or metadata.get("name") or metadata.get("path") or "").strip() or "FTP Item"
        instance_name = str(metadata.get("instance") or (table_metadata or {}).get("instance") or "").strip()
        ftp_url = str(metadata.get("ftp_url") or metadata.get("selection_url") or metadata.get("path") or "").strip()
        remote_path = str(metadata.get("ftp_path") or "").strip()
        host = str(metadata.get("host") or "").strip()
        modified = str(metadata.get("modified") or "").strip()

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
            item,
            title=title,
            store=instance_name or self.name,
            path=ftp_url or remote_path or None,
            tags=metadata.get("tag") or metadata.get("tags"),
            extra_fields={
                "Plugin": self.name,
                "Host": host or None,
                "Instance": instance_name or None,
                "Remote Path": remote_path or None,
                "Directory": str(metadata.get("detail") or "").strip() or None,
                "Modified": modified or None,
                "Ftp Url": ftp_url or None,
            },
        )

        return render_selection_detail_view(
            ctx=ctx,
            item=item,
            title=f"FTP Item: {title}",
            metadata=detail_metadata,
            table_name=self.name,
            detail_order=["Title", "Instance", "Host", "Remote Path", "Directory", "Modified", "Path", "Ext", "FTP URL", "Plugin"],
            value_case="preserve",
        )

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        metadata = getattr(result, "full_metadata", None)
        if isinstance(metadata, dict) and metadata.get("is_dir"):
            return None
        target = str(getattr(result, "path", "") or "").strip()
        if not target:
            return None
        instance_name = str(metadata.get("instance") or "").strip() if isinstance(metadata, dict) else ""
        return self.download_url(target, output_dir, title=getattr(result, "title", None), instance=instance_name or None)

    def download_url(self, url: str, output_dir: Path, **kwargs: Any) -> Optional[Path]:
        parsed = kwargs.get("parsed") if isinstance(kwargs.get("parsed"), dict) else {}
        settings = self._connection_settings_for_url(
            url,
            instance_name=str(kwargs.get("instance") or parsed.get("instance") or "").strip() or None,
        )
        remote_path = settings["path"]
        if not remote_path or remote_path == "/":
            return None

        filename_hint = str(kwargs.get("title") or "").strip()
        parsed_name = posixpath.basename(remote_path.rstrip("/"))
        filename = _safe_filename(filename_hint or unquote(parsed_name) or "download")

        destination_dir = Path(output_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = _unique_path(destination_dir / filename)

        ftp = self._connect(settings=settings)
        try:
            with destination.open("wb") as handle:
                ftp.retrbinary(f"RETR {remote_path}", handle.write)
            return destination
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        finally:
            self._close(ftp)

    def resolve_pipe_result_download(
        self,
        result: Any,
        pipe_obj: Any,
    ) -> Tuple[Optional[Path], Optional[str], Optional[Path]]:
        metadata = self._item_metadata(result, pipe_obj=pipe_obj)
        if metadata.get("is_dir"):
            return None, None, None

        download_url = str(
            metadata.get("selection_url")
            or metadata.get("ftp_url")
            or metadata.get("path")
            or ""
        ).strip()
        if not download_url.startswith(("ftp://", "ftps://")):
            return None, None, None

        temp_dir = Path(tempfile.mkdtemp(prefix="ftp-add-file-"))
        downloaded = self.download_url(
            download_url,
            temp_dir,
            title=metadata.get("title"),
            instance=metadata.get("instance"),
        )
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
        return downloaded, None, temp_dir

    def upload(self, file_path: str, **kwargs: Any) -> str:
        local_path = Path(str(file_path or "")).expanduser()
        if not local_path.exists() or not local_path.is_file():
            raise FileNotFoundError(f"File not found: {local_path}")

        pipe_obj = kwargs.get("pipe_obj")

        settings = self._resolve_settings(
            instance_name=str(kwargs.get("instance") or kwargs.get("store") or "").strip() or None,
            require_explicit=bool(kwargs.get("instance") or kwargs.get("store")),
        )
        if not settings.get("host"):
            requested = str(kwargs.get("instance") or kwargs.get("store") or "").strip()
            if requested:
                raise RuntimeError(f"FTP instance '{requested}' is unavailable")
            raise RuntimeError("No configured FTP instance is available")

        remote_dir = self._normalize_remote_path(
            kwargs.get("remote_path") or kwargs.get("path") or settings.get("base_path") or "/",
            default=str(settings.get("base_path") or "/"),
        )
        remote_name = posixpath.basename(str(kwargs.get("remote_name") or local_path.name).replace("\\", "/")) or local_path.name
        remote_path = self._join_remote_path(remote_dir, remote_name)

        ftp = self._connect(settings=settings)
        try:
            self._ensure_directory(ftp, remote_dir, base_path=str(settings.get("base_path") or "/"))
            # FTP duplicate check is filename-based at the destination directory.
            # If the exact filename already exists remotely, skip re-upload.
            if self._remote_filename_exists(ftp, remote_dir, remote_name):
                try:
                    if pipe_obj is not None:
                        if not isinstance(getattr(pipe_obj, "extra", None), dict):
                            pipe_obj.extra = {}
                        pipe_obj.extra["upload_duplicate"] = True
                        pipe_obj.extra["upload_duplicate_rule"] = "filename"
                        pipe_obj.extra["upload_duplicate_target"] = remote_path
                except Exception:
                    pass
                return self._upload_result(remote_path, settings=settings, local_path=local_path, kwargs=kwargs)
            progress = kwargs.get("pipeline_progress")
            transfer_label = str(kwargs.get("title") or local_path.name or "ftp upload")
            try:
                total_bytes = int(local_path.stat().st_size)
            except Exception:
                total_bytes = None
            completed = 0
            if progress is not None:
                try:
                    progress.begin_transfer(label=transfer_label, total=total_bytes)
                except Exception:
                    progress = None

            def _ftp_progress(block: bytes) -> None:
                nonlocal completed
                completed += len(block or b"")
                if progress is None:
                    return
                try:
                    progress.update_transfer(
                        label=transfer_label,
                        completed=completed,
                        total=total_bytes,
                    )
                except Exception:
                    pass

            try:
                with local_path.open("rb") as handle:
                    ftp.storbinary(f"STOR {remote_path}", handle, callback=_ftp_progress)
            finally:
                if progress is not None:
                    try:
                        progress.finish_transfer(label=transfer_label)
                    except Exception:
                        pass
            if _coerce_bool(kwargs.get("write_metadata"), True):
                self._store_sidecars(
                    ftp,
                    remote_path,
                    tags=kwargs.get("tags"),
                    urls=kwargs.get("urls"),
                    hash_value=kwargs.get("hash_value"),
                    relationships=kwargs.get("relationships"),
                )
        finally:
            self._close(ftp)

        return self._upload_result(remote_path, settings=settings, local_path=local_path, kwargs=kwargs)

    def _upload_result(
        self,
        remote_path: str,
        *,
        settings: Dict[str, Any],
        local_path: Path,
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        url = self._build_url(remote_path, settings=settings)
        instance_name = str(settings.get("instance") or "").strip()
        tags = list(kwargs.get("tags") or [])
        title = str(kwargs.get("title") or local_path.stem).strip() or local_path.name
        try:
            size_value = int(local_path.stat().st_size)
        except Exception:
            size_value = None
        return {
            "hash": str(kwargs.get("hash_value") or "").strip() or "unknown",
            "store": instance_name or self.name,
            "plugin": self.name,
            "path": url,
            "tag": tags,
            "title": title,
            "url": [url],
            "ext": local_path.suffix.lstrip("."),
            "size": size_value,
            "extra": {
                "instance": instance_name,
                "plugin": self.name,
                "store": instance_name or self.name,
            },
        }

    def _store_sidecars(
        self,
        ftp: ftplib.FTP,
        remote_path: str,
        *,
        tags: Any = None,
        urls: Any = None,
        hash_value: Any = None,
        relationships: Any = None,
    ) -> None:
        payloads = build_sidecar_payloads(
            tags=tags,
            urls=urls,
            hash_value=str(hash_value or "").strip() or None,
            relationships=relationships,
        )
        for suffix, body in payloads.items():
            try:
                ftp.storbinary(f"STOR {remote_path}{suffix}", io.BytesIO(body.encode("utf-8")))
            except Exception:
                continue

    def delete_file(self, remote_path_or_url: str, **kwargs: Any) -> bool:
        """Delete a file from the FTP server.

        Accepts either a full FTP URL (ftp://host/path/file) or a raw remote
        path (/path/to/file).  Returns True on success, False on failure.
        """
        path_text = str(remote_path_or_url or "").strip()
        if not path_text:
            return False

        settings = self._resolve_settings(
            instance_name=str(kwargs.get("instance") or kwargs.get("store") or "").strip() or None,
            require_explicit=bool(kwargs.get("instance") or kwargs.get("store")),
        )
        if not settings.get("host"):
            requested = str(kwargs.get("instance") or kwargs.get("store") or "").strip()
            if requested:
                raise RuntimeError(f"FTP instance '{requested}' is unavailable")
            raise RuntimeError("No configured FTP instance is available")

        # Accept full FTP URL or raw path
        if path_text.startswith(("ftp://", "ftps://")):
            remote_path = self._normalize_remote_path(path_text, default=self._base_path)
        else:
            remote_path = self._normalize_remote_path(path_text, default=str(settings.get("base_path") or "/"))

        ftp = self._connect(settings=settings)
        try:
            ftp.delete(remote_path)
            return True
        except ftplib.error_perm:
            return False
        finally:
            self._close(ftp)

    def _connect(
        self,
        *,
        settings: Optional[Dict[str, Any]] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tls: Optional[bool] = None,
    ) -> ftplib.FTP:
        resolved = dict(settings or {})
        use_tls = bool(resolved.get("tls")) if tls is None else bool(tls)
        ftp: ftplib.FTP = ftplib.FTP_TLS() if use_tls else ftplib.FTP()
        ftp.connect(
            host or str(resolved.get("host") or self._host),
            int(port or resolved.get("port") or self._port),
            timeout=max(1, int(resolved.get("timeout") or self._timeout)),
        )
        ftp.login(
            username or str(resolved.get("username") or self._username),
            password or str(resolved.get("password") or self._password),
        )
        try:
            ftp.set_pasv(bool(resolved.get("passive")) if "passive" in resolved else self._passive)
        except Exception:
            pass
        if use_tls and isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        return ftp

    def _close(self, ftp: Optional[ftplib.FTP]) -> None:
        if ftp is None:
            return
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    def _normalize_remote_path(self, value: Any, *, default: str) -> str:
        text = str(value or "").strip().replace("\\", "/")
        if not text:
            text = default
        elif text.startswith(("ftp://", "ftps://")):
            try:
                text = unquote(urlparse(text).path or "/")
            except Exception:
                text = default
        elif not text.startswith("/"):
            text = posixpath.join(default, text)

        normalized = posixpath.normpath(text)
        normalized = "/" + normalized.lstrip("/")
        return normalized or "/"

    def _join_remote_path(self, parent: Any, child: Any) -> str:
        left = self._normalize_remote_path(parent, default=self._base_path)
        right = str(child or "").strip().replace("\\", "/")
        if not right:
            return left
        return self._normalize_remote_path(posixpath.join(left, right), default="/")

    def _build_url(
        self,
        remote_path: Any,
        *,
        settings: Optional[Dict[str, Any]] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        tls: Optional[bool] = None,
    ) -> str:
        resolved = dict(settings or {})
        path_text = self._normalize_remote_path(remote_path, default="/")
        scheme = "ftps" if ((bool(resolved.get("tls")) if tls is None else bool(tls))) else "ftp"
        host_text = str(host or resolved.get("host") or self._host).strip()
        port_value = int(port or resolved.get("port") or self._port)
        port_suffix = f":{port_value}" if port_value and port_value != 21 else ""
        _qsafe = "/-._~!$&'()*+,;=:@"
        return f"{scheme}://{host_text}{port_suffix}{quote(path_text, safe=_qsafe)}"

    def _connection_settings_for_url(self, url: str, *, instance_name: Optional[str] = None) -> Dict[str, Any]:
        settings = self._resolve_settings(instance_name=instance_name, require_explicit=bool(instance_name))
        parsed = urlparse(str(url or "").strip())
        scheme = (parsed.scheme or "ftp").strip().lower()
        host = parsed.hostname or settings.get("host") or self._host
        port = parsed.port or settings.get("port") or self._port
        username = parsed.username or settings.get("username") or self._username
        password = parsed.password or settings.get("password") or self._password
        path_text = self._normalize_remote_path(unquote(parsed.path or "/"), default=str(settings.get("base_path") or "/"))
        return {
            "instance": settings.get("instance"),
            "tls": scheme == "ftps",
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "path": path_text,
            "passive": settings.get("passive", self._passive),
            "timeout": settings.get("timeout", self._timeout),
            "base_path": settings.get("base_path", self._base_path),
        }

    def _search_directory(
        self,
        ftp: ftplib.FTP,
        start_path: str,
        *,
        needle: str,
        limit: int,
        search_depth: int,
        type_filter: str,
        settings: Dict[str, Any],
        show_all: bool = False,
    ) -> List[SearchResult]:
        results: List[SearchResult] = []
        visited: set[str] = set()

        def walk(current_path: str, depth_left: int) -> None:
            normalized = self._normalize_remote_path(current_path, default=str(settings.get("base_path") or self._base_path))
            if normalized in visited or len(results) >= limit:
                return
            visited.add(normalized)

            listing = self._list_directory(ftp, normalized, base_path=str(settings.get("base_path") or self._base_path))
            listed_names = {str(item.get("name") or "") for item in listing}
            for entry in listing:
                if len(results) >= limit:
                    return
                name_text = str(entry.get("name") or "")
                if is_sidecar_filename(name_text):
                    continue
                has_sidecar = f"{name_text}.metadata" in listed_names or f"{name_text}.tag" in listed_names
                if not show_all and (entry.get("is_dir") or not has_sidecar):
                    continue
                if self._matches_entry(entry, needle=needle, type_filter=type_filter):
                    sidecar = self._load_listing_sidecars(
                        ftp,
                        str(entry.get("ftp_path") or ""),
                        name_text,
                        listed_names,
                    )
                    results.append(self._build_result(entry, settings=settings, sidecar=sidecar))
                if entry.get("is_dir") and depth_left > 0:
                    walk(str(entry.get("ftp_path") or normalized), depth_left - 1)

        walk(start_path, max(0, search_depth))
        return results

    def _matches_entry(self, entry: Dict[str, Any], *, needle: str, type_filter: str) -> bool:
        is_dir = bool(entry.get("is_dir"))
        if type_filter in {"dir", "dirs", "folder", "folders"} and not is_dir:
            return False
        if type_filter in {"file", "files"} and is_dir:
            return False

        text = str(needle or "").strip().lower()
        if not text or text in {"*", "all", "list"}:
            return True

        haystacks = [
            str(entry.get("name") or "").lower(),
            str(entry.get("ftp_path") or "").lower(),
        ]
        for token in [part for part in text.split() if part]:
            if any(ch in token for ch in "*?[]"):
                if not any(fnmatch.fnmatch(haystack, token) for haystack in haystacks):
                    return False
            elif not any(token in haystack for haystack in haystacks):
                return False
        return True

    def _retr_text(self, ftp: ftplib.FTP, remote_path: str) -> str:
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {remote_path}", buf.write)
        return buf.getvalue().decode("utf-8", errors="replace")

    def _load_listing_sidecars(
        self,
        ftp: ftplib.FTP,
        file_path: str,
        file_name: str,
        listed_names: set[str],
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {"tags": [], "urls": [], "hash": None}
        parent = posixpath.dirname(file_path.rstrip("/")) or "/"
        for suffix in (".tag", ".metadata"):
            sidecar_name = f"{file_name}{suffix}"
            if sidecar_name not in listed_names:
                continue
            try:
                raw = self._retr_text(ftp, self._join_remote_path(parent, sidecar_name))
            except Exception:
                continue
            hash_value, tags, urls = parse_sidecar_text(raw)
            if hash_value:
                out["hash"] = hash_value
            out["tags"].extend(tags)
            out["urls"].extend(urls)
        return out

    def _build_result(
        self,
        entry: Dict[str, Any],
        *,
        settings: Dict[str, Any],
        sidecar: Optional[Dict[str, Any]] = None,
    ) -> SearchResult:
        ftp_path = str(entry.get("ftp_path") or "/")
        ftp_url = self._build_url(ftp_path, settings=settings)
        is_dir = bool(entry.get("is_dir"))
        size_value = entry.get("size")
        try:
            size_int = int(size_value) if size_value is not None else None
        except Exception:
            size_int = None
        modified = str(entry.get("modified") or "")
        parent = posixpath.dirname(ftp_path.rstrip("/")) or "/"
        instance_name = str(settings.get("instance") or "").strip()
        name_text = str(entry.get("name") or "").strip()
        ext = "" if is_dir else posixpath.splitext(name_text)[1].lstrip(".")
        sidecar = sidecar or {}
        tags = [str(tag) for tag in (sidecar.get("tags") or []) if str(tag).strip()]
        urls = [str(url) for url in (sidecar.get("urls") or []) if str(url).strip()]
        hash_value = str(sidecar.get("hash") or "").strip() or None
        store_label = f"ftp:{instance_name}" if instance_name else "ftp"
        metadata = {
            "plugin": "ftp",
            "instance": instance_name or None,
            "host": settings.get("host"),
            "ftp_path": ftp_path,
            "ftp_url": ftp_url,
            "selection_url": ftp_url,
            "is_dir": is_dir,
            "name": name_text,
            "store": store_label,
            "ext": ext,
            "size": size_int,
        }
        if modified:
            metadata["modified"] = modified
        if hash_value:
            metadata["hash"] = hash_value
        if urls:
            metadata["url"] = urls

        selection_args = ["-url", ftp_url]
        selection_action = ["download-file", "-plugin", "ftp"]
        if instance_name:
            selection_args = ["-instance", instance_name, *selection_args]
            selection_action.extend(["-instance", instance_name])
        selection_action.extend(["-url", ftp_url])

        tag_set = set(tags) | {"ftp", "folder" if is_dir else "file"}
        return SearchResult(
            table="ftp",
            title=name_text or ftp_path,
            path=ftp_url,
            detail=parent,
            annotations=["folder" if is_dir else "file"],
            media_kind="folder" if is_dir else "file",
            size_bytes=size_int,
            tag=tag_set,
            columns=[
                ("Title", name_text or ftp_path),
                ("Tag", ", ".join(tags[:8])),
                ("Instance", store_label),
                ("Plugin", self.name),
                ("Size", format_byte_size(size_int)),
                ("Ext", ext),
            ],
            selection_args=None if is_dir else selection_args,
            selection_action=None if is_dir else selection_action,
            full_metadata=metadata,
        )

    def _list_directory(self, ftp: ftplib.FTP, remote_path: str, *, base_path: str) -> List[Dict[str, Any]]:
        normalized = self._normalize_remote_path(remote_path, default=base_path)
        try:
            entries: List[Dict[str, Any]] = []
            for name, facts in ftp.mlsd(normalized):
                name_text = str(name or "").strip()
                if not name_text or name_text in {".", ".."}:
                    continue
                entry_type = str((facts or {}).get("type") or "").strip().lower()
                if entry_type in {"cdir", "pdir"}:
                    continue
                size_value = None
                raw_size = (facts or {}).get("size")
                if raw_size not in (None, ""):
                    try:
                        size_value = int(raw_size)
                    except Exception:
                        size_value = None
                entries.append(
                    {
                        "name": name_text,
                        "ftp_path": self._join_remote_path(normalized, name_text),
                        "is_dir": entry_type == "dir",
                        "size": size_value,
                        "modified": _format_timestamp((facts or {}).get("modify")),
                    }
                )
            return entries
        except Exception:
            return self._list_directory_fallback(ftp, normalized)

    def _list_directory_fallback(self, ftp: ftplib.FTP, remote_path: str) -> List[Dict[str, Any]]:
        try:
            listed = ftp.nlst(remote_path)
        except Exception:
            return []

        entries: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw_entry in listed:
            entry_text = str(raw_entry or "").strip()
            if not entry_text:
                continue
            entry_path = entry_text if entry_text.startswith("/") else self._join_remote_path(remote_path, entry_text)
            name_text = posixpath.basename(entry_path.rstrip("/")) or entry_path.rstrip("/")
            if not name_text or name_text in {".", ".."} or name_text in seen:
                continue
            seen.add(name_text)
            is_dir = self._is_directory(ftp, entry_path)
            size_value = None
            if not is_dir:
                try:
                    size_raw = ftp.size(entry_path)
                    if size_raw is not None:
                        size_value = int(size_raw)
                except Exception:
                    size_value = None
            entries.append(
                {
                    "name": name_text,
                    "ftp_path": entry_path,
                    "is_dir": is_dir,
                    "size": size_value,
                    "modified": self._read_modified(ftp, entry_path),
                }
            )
        return entries

    def _is_directory(self, ftp: ftplib.FTP, remote_path: str) -> bool:
        current = None
        try:
            current = ftp.pwd()
        except Exception:
            current = None
        try:
            ftp.cwd(remote_path)
            return True
        except Exception:
            return False
        finally:
            if current is not None:
                try:
                    ftp.cwd(current)
                except Exception:
                    pass

    def _read_modified(self, ftp: ftplib.FTP, remote_path: str) -> str:
        try:
            response = ftp.sendcmd(f"MDTM {remote_path}")
        except Exception:
            return ""
        parts = str(response or "").split()
        if len(parts) >= 2:
            return _format_timestamp(parts[1])
        return ""

    def _ensure_directory(self, ftp: ftplib.FTP, remote_path: str, *, base_path: str) -> None:
        normalized = self._normalize_remote_path(remote_path, default=base_path)
        if normalized == "/":
            return
        partial = ""
        for segment in [part for part in normalized.split("/") if part]:
            partial = f"{partial}/{segment}"
            if self._is_directory(ftp, partial):
                continue
            try:
                ftp.mkd(partial)
            except Exception:
                if not self._is_directory(ftp, partial):
                    raise

    def _remote_filename_exists(self, ftp: ftplib.FTP, remote_dir: str, filename: str) -> bool:
        target_name = str(filename or "").strip()
        if not target_name:
            return False

        normalized_dir = self._normalize_remote_path(remote_dir, default=self._base_path)

        try:
            for name, facts in ftp.mlsd(normalized_dir):
                _ = facts
                if str(name or "").strip() == target_name:
                    return True
        except Exception:
            pass

        try:
            entries = ftp.nlst(normalized_dir)
        except Exception:
            entries = []

        for entry in entries or []:
            entry_text = str(entry or "").strip().rstrip("/")
            if not entry_text:
                continue
            entry_name = posixpath.basename(entry_text)
            if entry_name == target_name:
                return True

        return False

    def _item_metadata(self, item: Any, *, pipe_obj: Any = None) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        for source in (item, pipe_obj):
            if isinstance(source, dict):
                for key in ("title", "path", "url"):
                    if source.get(key) is not None and key not in metadata:
                        metadata[key] = source.get(key)
                nested = source.get("full_metadata") or source.get("metadata")
                if isinstance(nested, dict):
                    metadata.update(nested)
            elif source is not None:
                for attr in ("title", "path", "url"):
                    try:
                        value = getattr(source, attr, None)
                    except Exception:
                        value = None
                    if value is not None and attr not in metadata:
                        metadata[attr] = value
                try:
                    nested = getattr(source, "full_metadata", None) or getattr(source, "metadata", None)
                except Exception:
                    nested = None
                if isinstance(nested, dict):
                    metadata.update(nested)

        ftp_path = metadata.get("ftp_path") or metadata.get("selection_path")
        if not ftp_path:
            path_value = metadata.get("path") or metadata.get("url") or metadata.get("ftp_url")
            path_text = str(path_value or "").strip()
            if path_text.startswith(("ftp://", "ftps://")):
                ftp_path = self._normalize_remote_path(path_text, default=self._base_path)
        if ftp_path:
            base_path = str(metadata.get("base_path") or self._base_path)
            metadata["ftp_path"] = self._normalize_remote_path(ftp_path, default=base_path)
            metadata.setdefault("selection_path", metadata["ftp_path"])

        if metadata.get("ftp_path") and not metadata.get("ftp_url"):
            metadata["ftp_url"] = self._build_url(
                metadata["ftp_path"],
                settings={
                    "host": metadata.get("host") or self._host,
                    "instance": metadata.get("instance"),
                },
            )
        if metadata.get("ftp_url") and not metadata.get("selection_url"):
            metadata["selection_url"] = metadata["ftp_url"]

        is_dir = metadata.get("is_dir")
        if is_dir is None and metadata.get("media_kind"):
            is_dir = str(metadata.get("media_kind") or "").strip().lower() == "folder"
        metadata["is_dir"] = bool(is_dir)
        return metadata