from __future__ import annotations

import posixpath
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import paramiko

from PluginCore.base import Plugin, SearchResult, parse_inline_query_arguments
from SYS.metadata import build_sidecar_payloads

from ._connection import (
    close_client,
    connect_ssh,
    is_sftp_negotiation_error,
    open_scp,
    open_sftp,
    run_ssh_command,
)
from ._helpers import (
    build_url,
    coerce_bool,
    coerce_int,
    connection_settings_for_url,
    item_metadata,
    join_remote_path,
    normalize_remote_path,
    safe_filename,
    unique_path,
)
from ._listing import (
    build_result,
    ensure_directory,
    ensure_directory_via_ssh,
    list_directory,
    list_directory_via_ssh,
    matches_entry,
    path_exists_via_ssh,
    remote_filename_exists,
    remote_filename_exists_via_ssh,
    search_directory,
    search_directory_via_ssh,
)


class SCP(Plugin):
    PLUGIN_NAME = "scp"
    URL = ("scp://", "sftp://")
    SUPPORTED_CMDLETS = frozenset({"add-file", "download-file", "search-file"})

    @property
    def label(self) -> str:
        return "SCP"

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
                "placeholder": "ssh.example.com",
            },
            {
                "key": "port",
                "label": "Port",
                "type": "integer",
                "default": 22,
            },
            {
                "key": "username",
                "label": "Username",
                "default": "",
                "required": True,
                "placeholder": "deploy",
            },
            {
                "key": "password",
                "label": "Password",
                "type": "secret",
                "secret": True,
                "default": "",
            },
            {
                "key": "key_path",
                "label": "SSH Key Path",
                "type": "path",
                "default": "",
                "placeholder": "~/.ssh/id_ed25519",
            },
            {
                "key": "base_path",
                "label": "Base Path",
                "default": "/",
                "placeholder": "/srv/files",
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
            {
                "key": "allow_agent",
                "label": "Use SSH Agent",
                "type": "boolean",
                "default": True,
            },
            {
                "key": "look_for_keys",
                "label": "Look For Default Keys",
                "type": "boolean",
                "default": True,
            },
        ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        _instance_name, conf = self.resolve_plugin_instance()
        defaults = self._settings_from_config(conf)
        self._host = str(defaults.get("host") or "").strip()
        self._port = int(defaults.get("port") or 22)
        self._username = str(defaults.get("username") or "").strip()
        self._password = str(defaults.get("password") or "").strip()
        self._key_path = str(defaults.get("key_path") or "").strip()
        self._timeout = max(1, int(defaults.get("timeout") or 20))
        self._search_depth = max(0, int(defaults.get("search_depth") or 1))
        self._allow_agent = bool(defaults.get("allow_agent"))
        self._look_for_keys = bool(defaults.get("look_for_keys"))
        self._base_path = self._normalize_remote_path(defaults.get("base_path") or "/", default="/")

    def _settings_from_config(self, conf: Optional[Dict[str, Any]], *, instance_name: Optional[str] = None) -> Dict[str, Any]:
        entry = dict(conf or {})
        return {
            "instance": str(instance_name or entry.get("_instance_name") or "").strip() or None,
            "host": str(entry.get("host") or "").strip(),
            "port": coerce_int(entry.get("port"), 22),
            "username": str(entry.get("username") or entry.get("user") or "").strip(),
            "password": str(entry.get("password") or "").strip(),
            "key_path": str(entry.get("key_path") or entry.get("identity_file") or "").strip(),
            "timeout": max(1, coerce_int(entry.get("timeout"), 20)),
            "search_depth": max(0, coerce_int(entry.get("search_depth"), 1)),
            "allow_agent": coerce_bool(entry.get("allow_agent"), True),
            "look_for_keys": coerce_bool(entry.get("look_for_keys"), True),
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
        return bool(settings.get("host") and settings.get("username"))

    def config_helper_text(self) -> str:
        return "Test the SSH/SCP connection before searching. You can also generate an RSA key pair from here."

    def config_actions(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "test_connection",
                "label": "Test connection",
                "variant": "primary",
            },
            {
                "id": "generate_ssh_key",
                "label": "Generate SSH key",
                "variant": "default",
            },
        ]

    def run_config_action(self, action_id: str, **_kwargs: Any) -> Dict[str, Any]:
        normalized = str(action_id or "").strip().lower()
        if normalized == "test_connection":
            return self._run_test_connection()
        if normalized == "generate_ssh_key":
            return self._generate_ssh_keypair()
        return super().run_config_action(action_id, **_kwargs)

    def extract_query_arguments(self, query: str) -> Tuple[str, Dict[str, Any]]:
        text, inline = parse_inline_query_arguments(query)
        filters: Dict[str, Any] = {}

        instance_name = str(inline.get("instance") or inline.get("store") or "").strip()
        if instance_name:
            filters["instance"] = instance_name

        if inline.get("path"):
            filters["path"] = inline.get("path")
        if inline.get("depth"):
            filters["depth"] = max(0, coerce_int(inline.get("depth"), self._search_depth))
        if inline.get("type"):
            filters["type"] = str(inline.get("type") or "").strip().lower()

        return text, filters

    def get_table_title(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        settings = self._resolve_settings(filters=filters)
        active_path = self._normalize_remote_path((filters or {}).get("path") or settings.get("base_path") or "/", default=str(settings.get("base_path") or "/"))
        instance_name = str(settings.get("instance") or "").strip()
        text = str(query or "").strip()
        if not text or text == "*":
            return f"SCP{f'[{instance_name}]' if instance_name else ''}: {active_path}"
        return f"SCP{f'[{instance_name}]' if instance_name else ''}: {text} @ {active_path}"

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
        if not settings.get("host") or not settings.get("username"):
            requested = self.requested_instance_name(active_filters)
            if requested:
                raise RuntimeError(f"SCP instance '{requested}' is unavailable")
            return []
        start_path = self._normalize_remote_path(active_filters.get("path") or settings.get("base_path") or "/", default=str(settings.get("base_path") or "/"))
        search_depth = max(0, coerce_int(active_filters.get("depth"), int(settings.get("search_depth") or self._search_depth)))
        type_filter = str(active_filters.get("type") or "any").strip().lower()
        show_all = coerce_bool(active_filters.get("all"), False)
        needle = str(query or "").strip()
        max_results = max(0, int(limit or 0))
        if max_results <= 0:
            return []

        ssh = self._connect_ssh(settings)
        sftp = None
        try:
            try:
                sftp = self._open_sftp(ssh)
            except Exception as exc:
                if not self._is_sftp_negotiation_error(exc):
                    raise
                return self._search_directory_via_ssh(
                    ssh,
                    start_path,
                    needle=needle,
                    limit=max_results,
                    search_depth=search_depth,
                    type_filter=type_filter,
                    settings=settings,
                    show_all=show_all,
                )

            return self._search_directory(
                sftp,
                start_path,
                needle=needle,
                limit=max_results,
                search_depth=search_depth,
                type_filter=type_filter,
                settings=settings,
                show_all=show_all,
            )
        finally:
            self._close_client(sftp)
            self._close_client(ssh)

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
            target_path = self._normalize_remote_path(metadata.get("scp_path") or metadata.get("selection_path"), default=str(settings.get("base_path") or "/"))
            target_title = str(metadata.get("title") or metadata.get("name") or "").strip()
            instance_name = str(settings.get("instance") or metadata.get("instance") or "").strip()
            if target_path:
                break

        if not target_path:
            return False

        settings = self._resolve_settings(instance_name=instance_name or None, require_explicit=bool(instance_name))
        ssh = self._connect_ssh(settings)
        sftp = None
        try:
            try:
                sftp = self._open_sftp(ssh)
            except Exception as exc:
                if not self._is_sftp_negotiation_error(exc):
                    raise
                rows = self._search_directory_via_ssh(
                    ssh,
                    target_path,
                    needle="*",
                    limit=500,
                    search_depth=0,
                    type_filter="any",
                    settings=settings,
                )
            else:
                rows = self._search_directory(
                    sftp,
                    target_path,
                    needle="*",
                    limit=500,
                    search_depth=0,
                    type_filter="any",
                    settings=settings,
                )
        finally:
            self._close_client(sftp)
            self._close_client(ssh)

        try:
            from SYS.result_table import Table
            from SYS.rich_display import stdout_console
        except Exception:
            return True

        title = target_title or target_path
        table = Table(f"SCP{f'[{instance_name}]' if instance_name else ''}: {title}")._perseverance(True)
        table.set_table("scp")
        try:
            table.set_table_metadata({
                "plugin": "scp",
                "instance": instance_name or None,
                "host": settings.get("host"),
                "path": target_path,
                "view": "directory",
            })
        except Exception:
            pass
        source_args = ["-plugin", "scp"]
        if instance_name:
            source_args.extend(["-instance", instance_name])
        source_args.extend([f"path:{target_path}", "*"])
        table.set_source_command("search-file", source_args)

        payloads: List[Dict[str, Any]] = []
        for row in rows:
            table.add_result(row)
            payloads.append(row.to_dict())

        try:
            ctx.set_last_result_table(table, payloads, subject={"plugin": "scp", "instance": instance_name or None, "path": target_path})
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

        title = str(metadata.get("title") or metadata.get("name") or metadata.get("path") or "").strip() or "SCP Item"
        instance_name = str(metadata.get("instance") or (table_metadata or {}).get("instance") or "").strip()
        scp_url = str(metadata.get("scp_url") or metadata.get("selection_url") or metadata.get("path") or "").strip()
        remote_path = str(metadata.get("scp_path") or "").strip()
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
            path=scp_url or remote_path or None,
            tags=metadata.get("tag") or metadata.get("tags"),
            extra_fields={
                "Plugin": self.name,
                "Host": host or None,
                "Instance": instance_name or None,
                "Remote Path": remote_path or None,
                "Directory": str(metadata.get("detail") or "").strip() or None,
                "Modified": modified or None,
                "Scp Url": scp_url or None,
            },
        )

        return render_selection_detail_view(
            ctx=ctx,
            item=item,
            title=f"SCP Item: {title}",
            metadata=detail_metadata,
            table_name=self.name,
            detail_order=["Title", "Instance", "Host", "Remote Path", "Directory", "Modified", "Path", "Ext", "SCP URL", "Plugin"],
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
        filename = safe_filename(filename_hint or unquote(parsed_name) or "download")

        destination_dir = Path(output_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = unique_path(destination_dir / filename)

        ssh = self._connect_ssh(settings)
        scp_client = None
        try:
            scp_client = self._open_scp(ssh)
            scp_client.get(remote_path, local_path=str(destination))
            return destination
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        finally:
            self._close_client(scp_client)
            self._close_client(ssh)

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
            or metadata.get("scp_url")
            or metadata.get("path")
            or ""
        ).strip()
        if not download_url.startswith(("scp://", "sftp://")):
            return None, None, None

        temp_dir = Path(tempfile.mkdtemp(prefix="scp-add-file-"))
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
        if not settings.get("host") or not settings.get("username"):
            requested = str(kwargs.get("instance") or kwargs.get("store") or "").strip()
            if requested:
                raise RuntimeError(f"SCP instance '{requested}' is unavailable")
            raise RuntimeError("No configured SCP instance is available")

        remote_dir = self._normalize_remote_path(
            kwargs.get("remote_path") or kwargs.get("path") or settings.get("base_path") or "/",
            default=str(settings.get("base_path") or "/"),
        )
        remote_name = posixpath.basename(str(kwargs.get("remote_name") or local_path.name).replace("\\", "/")) or local_path.name
        remote_path = self._join_remote_path(remote_dir, remote_name)

        ssh = self._connect_ssh(settings)
        sftp = None
        scp_client = None
        try:
            try:
                sftp = self._open_sftp(ssh)
            except Exception as exc:
                if not self._is_sftp_negotiation_error(exc):
                    raise
                self._ensure_directory_via_ssh(ssh, remote_dir)
                if self._remote_filename_exists_via_ssh(ssh, remote_path):
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
            else:
                self._ensure_directory(sftp, remote_dir, base_path=str(settings.get("base_path") or "/"))
                if self._remote_filename_exists(sftp, remote_path):
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
            scp_client = self._open_scp(ssh)
            progress = kwargs.get("pipeline_progress")
            transfer_label = str(kwargs.get("title") or local_path.name or "scp upload")
            try:
                total_bytes = int(local_path.stat().st_size)
            except Exception:
                total_bytes = None
            if progress is not None:
                try:
                    progress.begin_transfer(label=transfer_label, total=total_bytes)
                except Exception:
                    progress = None

            def _scp_progress(_filename: Any, size: Any, sent: Any) -> None:
                if progress is None:
                    return
                try:
                    progress.update_transfer(
                        label=transfer_label,
                        completed=int(sent or 0),
                        total=int(size) if size else total_bytes,
                    )
                except Exception:
                    pass

            try:
                scp_client.put(str(local_path), remote_path=remote_path, progress=_scp_progress)
            finally:
                if progress is not None:
                    try:
                        progress.finish_transfer(label=transfer_label)
                    except Exception:
                        pass
            payloads = {}
            if coerce_bool(kwargs.get("write_metadata"), True):
                payloads = build_sidecar_payloads(
                    tags=kwargs.get("tags"),
                    urls=kwargs.get("urls"),
                    hash_value=str(kwargs.get("hash_value") or "").strip() or None,
                    relationships=kwargs.get("relationships"),
                )
            for suffix, body in payloads.items():
                tmp_path = None
                try:
                    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=suffix)
                    tmp.write(body)
                    tmp.close()
                    tmp_path = tmp.name
                    scp_client.put(tmp_path, remote_path=f"{remote_path}{suffix}")
                except Exception:
                    continue
                finally:
                    if tmp_path:
                        try:
                            Path(tmp_path).unlink()
                        except Exception:
                            pass
        finally:
            self._close_client(scp_client)
            self._close_client(sftp)
            self._close_client(ssh)

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

    # ── delegated helpers ──────────────────────────────────────────

    def _remote_filename_exists(self, sftp: Any, remote_path: str) -> bool:
        return remote_filename_exists(sftp, remote_path)

    def _remote_filename_exists_via_ssh(self, ssh: Any, remote_path: str) -> bool:
        return remote_filename_exists_via_ssh(ssh, remote_path, self._base_path, self._timeout)

    def _run_test_connection(self) -> Dict[str, Any]:
        settings = self._resolve_settings()
        if not settings.get("host"):
            return {"ok": False, "message": "Set 'host' before testing the SCP connection."}
        if not settings.get("username"):
            return {"ok": False, "message": "Set 'username' before testing the SCP connection."}

        ssh = None
        sftp = None
        try:
            ssh = self._connect_ssh(settings)
            base_path = str(settings.get("base_path") or "/")
            transport_detail = "SFTP available"
            try:
                sftp = self._open_sftp(ssh)
            except Exception as exc:
                if not self._is_sftp_negotiation_error(exc):
                    raise
                is_dir = self._path_exists_via_ssh(ssh, base_path)
                transport_detail = "SFTP unavailable; using SSH command fallback"
            else:
                try:
                    attrs = sftp.stat(base_path)
                    is_dir = stat.S_ISDIR(getattr(attrs, "st_mode", 0))
                except Exception:
                    is_dir = False
            detail = f" and confirmed {base_path}" if is_dir else ""
            key_path = str(settings.get("key_path") or "").strip()
            auth_mode = f"key {key_path}" if key_path else "password/agent auth"
            return {
                "ok": True,
                "message": f"Connected to SCP {settings.get('host')}:{settings.get('port')} as {settings.get('username')} via {auth_mode}. {transport_detail}{detail}.",
            }
        except Exception as exc:
            return {"ok": False, "message": f"SCP connection failed: {exc}"}
        finally:
            self._close_client(sftp)
            self._close_client(ssh)

    def _generate_ssh_keypair(self) -> Dict[str, Any]:
        settings = self._resolve_settings()
        key_path = str(settings.get("key_path") or "").strip()
        target = Path(key_path).expanduser() if key_path else (Path.home() / ".ssh" / "medeia_scp_rsa")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return {"ok": False, "message": f"Could not create key directory: {exc}"}

        public_path = target.with_name(target.name + ".pub")
        if target.exists() or public_path.exists():
            return {
                "ok": False,
                "message": f"SSH key already exists at {target}. Remove it or choose a different key_path first.",
            }

        try:
            key = paramiko.RSAKey.generate(bits=4096)
            key.write_private_key_file(str(target))
            comment = f"{settings.get('username') or 'medeia'}@{settings.get('host') or 'scp'}"
            public_path.write_text(f"{key.get_name()} {key.get_base64()} {comment}\n", encoding="utf-8")
            try:
                target.chmod(0o600)
            except Exception:
                pass
            return {
                "ok": True,
                "message": f"Generated SSH key pair at {target}. Save the config to persist key_path.",
                "config_updates": {"key_path": str(target)},
            }
        except Exception as exc:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                public_path.unlink(missing_ok=True)
            except Exception:
                pass
            return {"ok": False, "message": f"SSH key generation failed: {exc}"}

    def _connect_ssh(self, overrides: Optional[Dict[str, Any]] = None) -> paramiko.SSHClient:
        return connect_ssh(
            self._host,
            self._port,
            self._username,
            self._password,
            self._key_path,
            self._timeout,
            self._allow_agent,
            self._look_for_keys,
            overrides=overrides,
        )

    def _open_sftp(self, ssh: Any) -> Any:
        return open_sftp(ssh)

    def _open_scp(self, ssh: Any) -> Any:
        return open_scp(ssh)

    def _is_sftp_negotiation_error(self, exc: Exception) -> bool:
        return is_sftp_negotiation_error(exc)

    def _run_ssh_command(self, ssh: Any, command: str) -> Tuple[int, str, str]:
        return run_ssh_command(ssh, command, self._timeout)

    def _path_exists_via_ssh(self, ssh: Any, remote_path: str) -> bool:
        return path_exists_via_ssh(ssh, remote_path, self._base_path, self._timeout)

    def _ensure_directory_via_ssh(self, ssh: Any, remote_path: str) -> None:
        ensure_directory_via_ssh(ssh, remote_path, self._base_path, self._timeout)

    def _close_client(self, client: Any) -> None:
        close_client(client)

    def _normalize_remote_path(self, value: Any, *, default: str) -> str:
        return normalize_remote_path(value, default=default)

    def _join_remote_path(self, parent: Any, child: Any) -> str:
        return join_remote_path(parent, child, self._base_path)

    def _build_url(
        self,
        remote_path: Any,
        *,
        settings: Optional[Dict[str, Any]] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        scheme: str = "scp",
    ) -> str:
        path_text = normalize_remote_path(remote_path, default="/")
        return build_url(path_text, self._host, self._port, settings=settings, host=host, port=port, scheme=scheme)

    def _connection_settings_for_url(self, url: str, *, instance_name: Optional[str] = None) -> Dict[str, Any]:
        settings = self._resolve_settings(instance_name=instance_name, require_explicit=bool(instance_name))
        return connection_settings_for_url(
            url,
            settings,
            self._host,
            self._port,
            self._username,
            self._password,
            self._key_path,
            self._allow_agent,
            self._look_for_keys,
            self._timeout,
            self._base_path,
        )

    def _search_directory(
        self,
        sftp: Any,
        start_path: str,
        *,
        needle: str,
        limit: int,
        search_depth: int,
        type_filter: str,
        settings: Dict[str, Any],
        show_all: bool = False,
    ) -> List[SearchResult]:
        return search_directory(
            sftp,
            start_path,
            needle=needle,
            limit=limit,
            search_depth=search_depth,
            type_filter=type_filter,
            settings=settings,
            base_path=self._base_path,
            default_host=self._host,
            default_port=self._port,
            show_all=show_all,
        )

    def _search_directory_via_ssh(
        self,
        ssh: Any,
        start_path: str,
        *,
        needle: str,
        limit: int,
        search_depth: int,
        type_filter: str,
        settings: Dict[str, Any],
        show_all: bool = False,
    ) -> List[SearchResult]:
        return search_directory_via_ssh(
            ssh,
            start_path,
            needle=needle,
            limit=limit,
            search_depth=search_depth,
            type_filter=type_filter,
            settings=settings,
            base_path=self._base_path,
            default_host=self._host,
            default_port=self._port,
            timeout=self._timeout,
            show_all=show_all,
        )

    def _matches_entry(self, entry: Dict[str, Any], *, needle: str, type_filter: str) -> bool:
        return matches_entry(entry, needle=needle, type_filter=type_filter)

    def _build_result(self, entry: Dict[str, Any], *, settings: Dict[str, Any]) -> SearchResult:
        return build_result(entry, settings=settings, default_host=self._host, default_port=self._port)

    def _list_directory(self, sftp: Any, remote_path: str) -> List[Dict[str, Any]]:
        return list_directory(sftp, remote_path, self._base_path)

    def _list_directory_via_ssh(self, ssh: Any, remote_path: str, *, depth: int) -> List[Dict[str, Any]]:
        return list_directory_via_ssh(ssh, remote_path, self._base_path, depth=depth, timeout=self._timeout)

    def _ensure_directory(self, sftp: Any, remote_path: str, *, base_path: str) -> None:
        ensure_directory(sftp, remote_path, base_path=base_path)

    def _item_metadata(self, item: Any, *, pipe_obj: Any = None) -> Dict[str, Any]:
        return item_metadata(item, pipe_obj=pipe_obj, base_path=self._base_path, default_host=self._host, default_port=self._port)
