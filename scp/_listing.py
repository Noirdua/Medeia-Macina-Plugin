from __future__ import annotations

import fnmatch
import posixpath
import shlex
import stat
from typing import Any, Dict, List

from PluginCore.base import SearchResult
from SYS.metadata import is_sidecar_filename, parse_sidecar_text
from SYS.utils import format_byte_size

from ._connection import run_ssh_command
from ._helpers import build_url, format_epoch, join_remote_path, normalize_remote_path


def matches_entry(entry: Dict[str, Any], *, needle: str, type_filter: str) -> bool:
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
        str(entry.get("scp_path") or "").lower(),
    ]
    for token in [part for part in text.split() if part]:
        if any(ch in token for ch in "*?[]"):
            if not any(fnmatch.fnmatch(haystack, token) for haystack in haystacks):
                return False
        elif not any(token in haystack for haystack in haystacks):
            return False
    return True


def _load_sftp_sidecars(sftp: Any, file_path: str, file_name: str, listed_names: set[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"tags": [], "urls": [], "hash": None}
    parent = posixpath.dirname(file_path.rstrip("/")) or "/"
    for suffix in (".tag", ".metadata"):
        sidecar_name = f"{file_name}{suffix}"
        if sidecar_name not in listed_names:
            continue
        remote = join_remote_path(parent, sidecar_name, parent)
        try:
            with sftp.open(remote, "r") as handle:
                raw = handle.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            hash_value, tags, urls = parse_sidecar_text(str(raw or ""))
            if hash_value:
                out["hash"] = hash_value
            out["tags"].extend(tags)
            out["urls"].extend(urls)
        except Exception:
            continue
    return out


def build_result(
    entry: Dict[str, Any],
    *,
    settings: Dict[str, Any],
    default_host: str,
    default_port: int,
    sidecar: Dict[str, Any] | None = None,
) -> SearchResult:
    scp_path = str(entry.get("scp_path") or "/")
    scp_url = build_url(
        normalize_remote_path(scp_path, default="/"),
        default_host,
        default_port,
        settings=settings,
    )
    is_dir = bool(entry.get("is_dir"))
    size_value = entry.get("size")
    modified = str(entry.get("modified") or "")
    parent = posixpath.dirname(scp_path.rstrip("/")) or "/"
    instance_name = str(settings.get("instance") or "").strip()
    name_text = str(entry.get("name") or "").strip()
    ext = "" if is_dir else posixpath.splitext(name_text)[1].lstrip(".")
    try:
        size_int = int(size_value) if size_value is not None else None
    except Exception:
        size_int = None
    sidecar = sidecar or {}
    tags = [str(tag) for tag in (sidecar.get("tags") or []) if str(tag).strip()]
    urls = [str(url) for url in (sidecar.get("urls") or []) if str(url).strip()]
    hash_value = str(sidecar.get("hash") or "").strip() or None
    store_label = f"scp:{instance_name}" if instance_name else "scp"
    metadata = {
        "plugin": "scp",
        "instance": instance_name or None,
        "host": settings.get("host"),
        "scp_path": scp_path,
        "scp_url": scp_url,
        "selection_url": scp_url,
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

    selection_args = ["-url", scp_url]
    selection_action = ["download-file", "-plugin", "scp"]
    if instance_name:
        selection_args = ["-instance", instance_name, *selection_args]
        selection_action.extend(["-instance", instance_name])
    selection_action.extend(["-url", scp_url])

    return SearchResult(
        table="scp",
        title=str(entry.get("name") or scp_path),
        path=scp_url,
        detail=parent,
        annotations=["folder" if is_dir else "file"],
        media_kind="folder" if is_dir else "file",
        size_bytes=size_int,
        tag=set(tags) | {"scp", "folder" if is_dir else "file"},
        columns=[
            ("Title", name_text or scp_path),
            ("Tag", ", ".join(tags[:8])),
            ("Instance", store_label),
            ("Plugin", "scp"),
            ("Size", format_byte_size(size_int)),
            ("Ext", ext),
        ],
        selection_args=None if is_dir else selection_args,
        selection_action=None if is_dir else selection_action,
        full_metadata=metadata,
    )


def list_directory(sftp: Any, remote_path: str, base_path: str) -> List[Dict[str, Any]]:
    try:
        attrs = sftp.listdir_attr(remote_path)
    except Exception:
        return []

    entries: List[Dict[str, Any]] = []
    for attr in attrs:
        name_text = str(getattr(attr, "filename", "") or "").strip()
        if not name_text or name_text in {".", ".."}:
            continue
        mode = getattr(attr, "st_mode", 0)
        is_dir = stat.S_ISDIR(mode)
        size_value = getattr(attr, "st_size", None)
        try:
            size_int = int(size_value) if size_value is not None else None
        except Exception:
            size_int = None
        entries.append(
            {
                "name": name_text,
                "scp_path": join_remote_path(remote_path, name_text, base_path),
                "is_dir": is_dir,
                "size": size_int,
                "modified": format_epoch(getattr(attr, "st_mtime", None)),
            }
        )
    return entries


def list_directory_via_ssh(
    ssh: Any,
    remote_path: str,
    base_path: str,
    depth: int,
    timeout: int,
) -> List[Dict[str, Any]]:
    normalized = normalize_remote_path(remote_path, default=base_path)
    max_depth = max(1, int(depth) + 1)
    quoted_path = shlex.quote(normalized)
    command = (
        f"find {quoted_path} -mindepth 1 -maxdepth {max_depth} "
        f"\\( -type d -o -type f \\) -exec sh -c 'for path do "
        f"if [ -d \"$path\" ]; then kind=d; else kind=f; fi; "
        f"name=$(basename \"$path\"); "
        f"size=$(stat -c%s \"$path\" 2>/dev/null || echo); "
        f"printf \"%s\\0%s\\0%s\\0%s\\0\" \"$kind\" \"$path\" \"$name\" \"$size\"; "
        f"done' sh {{}} +"
    )
    status, output, error = run_ssh_command(ssh, command, timeout)
    if status != 0:
        error_text = error.strip().lower()
        if "no such file" in error_text or "cannot access" in error_text:
            return []
        raise RuntimeError(error.strip() or f"SSH listing failed for {normalized}")

    chunks = [part for part in output.split("\0") if part]
    entries: List[Dict[str, Any]] = []
    for index in range(0, len(chunks), 4):
        if index + 2 >= len(chunks):
            break
        kind = chunks[index]
        scp_path = normalize_remote_path(chunks[index + 1], default=normalized)
        name_text = str(chunks[index + 2] or "").strip()
        size_raw = chunks[index + 3] if index + 3 < len(chunks) else ""
        if not name_text or name_text in {".", ".."}:
            continue
        size_int = None
        try:
            if str(size_raw).strip():
                size_int = int(size_raw)
        except Exception:
            size_int = None
        entries.append(
            {
                "name": name_text,
                "scp_path": scp_path,
                "is_dir": kind == "d",
                "size": size_int,
                "modified": "",
            }
        )
    return entries


def search_directory(
    sftp: Any,
    start_path: str,
    *,
    needle: str,
    limit: int,
    search_depth: int,
    type_filter: str,
    settings: Dict[str, Any],
    base_path: str,
    default_host: str,
    default_port: int,
    show_all: bool = False,
) -> List[SearchResult]:
    results: List[SearchResult] = []
    visited: set[str] = set()

    def walk(current_path: str, depth_left: int) -> None:
        if len(results) >= limit:
            return
        normalized = normalize_remote_path(current_path, default=str(settings.get("base_path") or base_path))
        if normalized in visited:
            return
        visited.add(normalized)

        listing = list_directory(sftp, normalized, base_path)
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
            if matches_entry(entry, needle=needle, type_filter=type_filter):
                sidecar = _load_sftp_sidecars(
                    sftp,
                    str(entry.get("scp_path") or ""),
                    name_text,
                    listed_names,
                )
                results.append(
                    build_result(
                        entry,
                        settings=settings,
                        default_host=default_host,
                        default_port=default_port,
                        sidecar=sidecar,
                    )
                )
                if len(results) >= limit:
                    return
            if entry.get("is_dir") and depth_left > 0:
                walk(str(entry.get("scp_path") or normalized), depth_left - 1)

    walk(start_path, max(0, search_depth))
    return results


def search_directory_via_ssh(
    ssh: Any,
    start_path: str,
    *,
    needle: str,
    limit: int,
    search_depth: int,
    type_filter: str,
    settings: Dict[str, Any],
    base_path: str,
    default_host: str,
    default_port: int,
    timeout: int,
    show_all: bool = False,
) -> List[SearchResult]:
    entries = list_directory_via_ssh(ssh, start_path, base_path, depth=search_depth, timeout=timeout)
    listed_names = {str(item.get("name") or "") for item in entries}
    results: List[SearchResult] = []
    for entry in entries:
        if len(results) >= limit:
            break
        name_text = str(entry.get("name") or "")
        if is_sidecar_filename(name_text):
            continue
        has_sidecar = f"{name_text}.metadata" in listed_names or f"{name_text}.tag" in listed_names
        if not show_all and (entry.get("is_dir") or not has_sidecar):
            continue
        if matches_entry(entry, needle=needle, type_filter=type_filter):
            results.append(build_result(entry, settings=settings, default_host=default_host, default_port=default_port))
    return results


def ensure_directory(sftp: Any, remote_path: str, *, base_path: str) -> None:
    normalized = normalize_remote_path(remote_path, default=base_path)
    if normalized == "/":
        return
    partial = ""
    for segment in [part for part in normalized.split("/") if part]:
        partial = f"{partial}/{segment}"
        try:
            attrs = sftp.stat(partial)
            if stat.S_ISDIR(getattr(attrs, "st_mode", 0)):
                continue
        except Exception:
            pass
        try:
            sftp.mkdir(partial)
        except Exception:
            try:
                attrs = sftp.stat(partial)
                if stat.S_ISDIR(getattr(attrs, "st_mode", 0)):
                    continue
            except Exception:
                pass
            raise


def ensure_directory_via_ssh(ssh: Any, remote_path: str, base_path: str, timeout: int) -> None:
    normalized = normalize_remote_path(remote_path, default=base_path)
    if normalized == "/":
        return
    quoted_path = shlex.quote(normalized)
    status, _, error = run_ssh_command(ssh, f"mkdir -p {quoted_path}", timeout)
    if status != 0:
        raise RuntimeError(error.strip() or f"mkdir -p failed for {normalized}")


def path_exists_via_ssh(ssh: Any, remote_path: str, base_path: str, timeout: int) -> bool:
    normalized = normalize_remote_path(remote_path, default=base_path)
    quoted_path = shlex.quote(normalized)
    status, _, _ = run_ssh_command(ssh, f"test -d {quoted_path}", timeout)
    return status == 0


def remote_filename_exists(sftp: Any, remote_path: str) -> bool:
    try:
        sftp.stat(remote_path)
        return True
    except Exception:
        return False


def remote_filename_exists_via_ssh(ssh: Any, remote_path: str, base_path: str, timeout: int) -> bool:
    normalized = normalize_remote_path(remote_path, default=base_path)
    quoted_path = shlex.quote(normalized)
    status, _, _ = run_ssh_command(ssh, f"test -e {quoted_path}", timeout)
    return status == 0
