"""Persistent MPV pipeline helper.

This process connects to MPV's IPC server, observes a user-data property for
pipeline execution requests, runs the pipeline in-process, and posts results
back to MPV via user-data properties.

Why:
- Avoid spawning a new Python process for every MPV action.
- Enable MPV Lua scripts to trigger any cmdlet pipeline cheaply.

Protocol (user-data properties):
- Request:  user-data/medeia-pipeline-request (JSON string)
  {"id": "...", "pipeline": "...", "seeds": [...]}  (seeds optional)
- Response: user-data/medeia-pipeline-response (JSON string)
  {"id": "...", "success": bool, "stdout": "...", "stderr": "...", "error": "..."}
- Ready:    user-data/medeia-pipeline-ready ("1")

This helper is intentionally minimal: one request at a time, last-write-wins.
"""

from __future__ import annotations

MEDEIA_MPV_HELPER_VERSION = "2026-03-23.1"

import argparse
import json
import os
import sys
import tempfile
import time
import threading
import logging
import re
import hashlib
import subprocess
import platform
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def _repo_root() -> Path:
    package_dir = Path(__file__).resolve().parent
    if package_dir.name.lower() == "mpv" and package_dir.parent.name.lower() == "plugins":
        return package_dir.parent.parent
    return package_dir.parent


def _runtime_config_root() -> Path:
    """Best-effort config root for runtime execution.

    MPV can spawn this helper from an installed location while setting `cwd` to
    the repo root (see plugins.mpv.mpv_ipc). Prefer `cwd` when it contains `config.conf`.
    """
    try:
        cwd = Path.cwd().resolve()
        if (cwd / "config.conf").exists():
            return cwd
    except Exception:
        pass
    return _repo_root()


# Make repo-local packages importable even when mpv starts us from another cwd.
_root_str = str(_repo_root())
_path_added = _root_str not in sys.path
if _path_added:
    sys.path.insert(0, _root_str)
try:
    from plugins.mpv.mpv_ipc import MPVIPCClient, _windows_kill_pids, _windows_hidden_subprocess_kwargs, _windows_list_mpv_pids  # noqa: E402
    from SYS.config import load_config, reload_config  # noqa: E402
    from SYS.logger import set_debug, debug, set_thread_stream  # noqa: E402
    from SYS.repl_queue import enqueue_repl_command, repl_state_is_alive  # noqa: E402
    from SYS.utils import format_bytes  # noqa: E402
    from PluginCore.registry import get_plugin, get_plugin_class  # noqa: E402
    from plugins.ytdlp.tooling import get_display_format_id, get_selection_format_id  # noqa: E402
finally:
    if _path_added:
        sys.path.remove(_root_str)
    del _root_str, _path_added

REQUEST_PROP = "user-data/medeia-pipeline-request"
RESPONSE_PROP = "user-data/medeia-pipeline-response"
READY_PROP = "user-data/medeia-pipeline-ready"
VERSION_PROP = "user-data/medeia-pipeline-helper-version"

OBS_ID_REQUEST = 1001

_HELPER_MPV_LOG_EMITTER: Optional[Callable[[str], None]] = None
_HELPER_LOG_BACKLOG: list[str] = []
_HELPER_LOG_BACKLOG_LIMIT = 200
_ASYNC_PIPELINE_JOBS: Dict[str, Dict[str, Any]] = {}
_ASYNC_PIPELINE_JOBS_LOCK = threading.Lock()
_ASYNC_PIPELINE_JOB_TTL_SECONDS = 900.0
_STORE_CHOICES_CACHE: list[str] = []
_STORE_CHOICES_CACHE_LOCK = threading.Lock()


def _normalize_store_choices(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, (list, tuple, set)):
        return out
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return sorted(out, key=str.casefold)


def _get_cached_store_choices() -> list[str]:
    with _STORE_CHOICES_CACHE_LOCK:
        return list(_STORE_CHOICES_CACHE)


def _set_cached_store_choices(choices: Any) -> list[str]:
    normalized = _normalize_store_choices(choices)
    with _STORE_CHOICES_CACHE_LOCK:
        _STORE_CHOICES_CACHE[:] = normalized
        return list(_STORE_CHOICES_CACHE)


def _store_choices_payload(choices: Any) -> Optional[str]:
    cached = _normalize_store_choices(choices)
    if not cached:
        return None
    return json.dumps(
        {
            "success": True,
            "choices": cached,
        },
        ensure_ascii=False,
    )


def _load_store_choices_from_config(*, force_reload: bool = False) -> list[str]:
    from PluginCore.backend_registry import list_configured_backend_names  # noqa: WPS433

    cfg = reload_config() if force_reload else load_config()
    return _normalize_store_choices(list_configured_backend_names(cfg or {}))


def _prune_async_pipeline_jobs(now: Optional[float] = None) -> None:
    cutoff = float(now or time.time()) - _ASYNC_PIPELINE_JOB_TTL_SECONDS
    with _ASYNC_PIPELINE_JOBS_LOCK:
        expired = [
            job_id
            for job_id, job in _ASYNC_PIPELINE_JOBS.items()
            if float(job.get("updated_at") or 0.0) < cutoff
        ]
        for job_id in expired:
            _ASYNC_PIPELINE_JOBS.pop(job_id, None)


def _update_async_pipeline_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    if not job_id:
        return None

    now = time.time()
    _prune_async_pipeline_jobs(now)

    with _ASYNC_PIPELINE_JOBS_LOCK:
        job = dict(_ASYNC_PIPELINE_JOBS.get(job_id) or {})
        if not job:
            job = {
                "job_id": job_id,
                "status": "queued",
                "success": False,
                "created_at": now,
            }
        job.update(fields)
        job["updated_at"] = now
        _ASYNC_PIPELINE_JOBS[job_id] = job
        return dict(job)


def _get_async_pipeline_job(job_id: str) -> Optional[Dict[str, Any]]:
    if not job_id:
        return None
    _prune_async_pipeline_jobs()
    with _ASYNC_PIPELINE_JOBS_LOCK:
        job = _ASYNC_PIPELINE_JOBS.get(job_id)
        return dict(job) if job else None


def _append_prefixed_log_lines(prefix: str, text: Any, *, max_lines: int = 40) -> None:
    payload = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not payload.strip():
        return

    emitted = 0
    for line in payload.split("\n"):
        line = line.rstrip()
        if not line:
            continue
        _append_helper_log(f"{prefix}{line}")
        emitted += 1
        if emitted >= max_lines:
            _append_helper_log(f"{prefix}... truncated after {max_lines} lines")
            break


def _start_ready_heartbeat(
    ipc_path: str,
    stop_event: threading.Event,
    mark_alive: Optional[Callable[[str], None]] = None,
    note_ipc_unavailable: Optional[Callable[[str], None]] = None,
) -> threading.Thread:
    """Keep READY_PROP fresh even when the main loop blocks on Windows pipes."""

    def _heartbeat_loop() -> None:
        hb_client = MPVIPCClient(socket_path=ipc_path, timeout=0.5, silent=True)
        while not stop_event.is_set():
            try:
                was_disconnected = hb_client.sock is None
                if was_disconnected:
                    if not hb_client.connect():
                        if note_ipc_unavailable is not None:
                            note_ipc_unavailable("heartbeat-connect")
                        stop_event.wait(0.25)
                        continue
                    if mark_alive is not None:
                        mark_alive("heartbeat-connect")
                hb_client.send_command_no_wait(
                    ["set_property_string", READY_PROP, str(int(time.time()))]
                )
                if mark_alive is not None:
                    mark_alive("heartbeat-send")
            except Exception:
                if note_ipc_unavailable is not None:
                    note_ipc_unavailable("heartbeat-send")
                try:
                    hb_client.disconnect()
                except Exception:
                    pass
            stop_event.wait(0.75)

        try:
            hb_client.disconnect()
        except Exception:
            pass

    thread = threading.Thread(
        target=_heartbeat_loop,
        name="mpv-helper-heartbeat",
        daemon=True,
    )
    thread.start()
    return thread


def _windows_list_pipeline_helper_pids(ipc_path: str) -> list[int]:
    if platform.system() != "Windows":
        return []
    try:
        ipc_path = str(ipc_path or "")
    except Exception:
        ipc_path = ""
    if not ipc_path:
        return []

    ps_script = (
        "$ipc = " + json.dumps(ipc_path) + "; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and (($_.CommandLine -match 'pipeline_helper\\.py') -or ($_.CommandLine -match ' -m\\s+MPV\\.pipeline_helper(\\s|$)')) -and $_.CommandLine -match ('--ipc\\s+' + [regex]::Escape($ipc)) } | "
        "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )

    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_script],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            text=True,
            **_windows_hidden_subprocess_kwargs(),
        )
    except Exception:
        return []

    txt = (out or "").strip()
    if not txt or txt == "null":
        return []

    try:
        obj = json.loads(txt)
    except Exception:
        return []

    raw_pids = obj if isinstance(obj, list) else [obj]
    out_pids: list[int] = []
    for value in raw_pids:
        try:
            pid = int(value)
        except Exception:
            continue
        if pid > 0 and pid not in out_pids:
            out_pids.append(pid)
    return out_pids


def _run_pipeline(
    pipeline_text: str,
    *,
    seeds: Any = None,
    json_output: bool = False,
) -> Dict[str, Any]:
    # Import after sys.path fix.
    from SYS.pipeline_runner import PipelineRunner  # noqa: WPS433

    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for key, item in value.items():
                out[str(key)] = _json_safe(item)
            return out
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(item) for item in value]
        if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
            try:
                return _json_safe(value.to_dict())
            except Exception:
                pass
        return str(value)

    def _table_to_payload(table: Any) -> Optional[Dict[str, Any]]:
        if table is None:
            return None
        try:
            title = getattr(table, "title", "")
        except Exception:
            title = ""

        rows_payload = []
        try:
            rows = getattr(table, "rows", None)
        except Exception:
            rows = None
        if isinstance(rows, list):
            for r in rows:
                cols_payload = []
                try:
                    cols = getattr(r, "columns", None)
                except Exception:
                    cols = None
                if isinstance(cols, list):
                    for c in cols:
                        try:
                            cols_payload.append(
                                {
                                    "name": getattr(c,
                                                    "name",
                                                    ""),
                                    "value": getattr(c,
                                                     "value",
                                                     ""),
                                }
                            )
                        except Exception:
                            continue

                sel_args = None
                try:
                    sel = getattr(r, "selection_args", None)
                    if isinstance(sel, list):
                        sel_args = [str(x) for x in sel]
                except Exception:
                    sel_args = None

                rows_payload.append(
                    {
                        "columns": cols_payload,
                        "selection_args": sel_args
                    }
                )

        # Only return JSON-serializable data (Lua only needs title + rows).
        return {
            "title": str(title or ""),
            "rows": rows_payload
        }

    start_time = time.time()
    runner = PipelineRunner()
    result = runner.run_pipeline(pipeline_text, seeds=seeds)
    duration = time.time() - start_time
    try:
        _append_helper_log(
            f"[pipeline] run_pipeline completed in {duration:.2f}s pipeline={pipeline_text[:64]}"
        )
    except Exception:
        pass

    table_payload = None
    try:
        table_payload = _table_to_payload(getattr(result, "result_table", None))
    except Exception:
        table_payload = None

    data_payload = None
    if json_output:
        try:
            data_payload = _json_safe(getattr(result, "emitted", None) or [])
        except Exception:
            data_payload = []

    return {
        "success": bool(result.success),
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "error": result.error,
        "table": table_payload,
        "data": data_payload,
    }


def _run_pipeline_background(
    pipeline_text: str,
    *,
    seeds: Any,
    req_id: str,
    job_id: Optional[str] = None,
    json_output: bool = False,
) -> None:
    def _target() -> None:
        try:
            if job_id:
                _update_async_pipeline_job(
                    job_id,
                    status="running",
                    success=False,
                    pipeline=pipeline_text,
                    req_id=req_id,
                    started_at=time.time(),
                    finished_at=None,
                    error=None,
                    stdout="",
                    stderr="",
                    table=None,
                    data=None,
                )

            result = _run_pipeline(
                pipeline_text,
                seeds=seeds,
                json_output=json_output,
            )
            status = "success" if result.get("success") else "failed"
            if job_id:
                _update_async_pipeline_job(
                    job_id,
                    status=status,
                    success=bool(result.get("success")),
                    finished_at=time.time(),
                    error=result.get("error"),
                    stdout=result.get("stdout", ""),
                    stderr=result.get("stderr", ""),
                    table=result.get("table"),
                    data=result.get("data"),
                )
            _append_helper_log(
                f"[pipeline async {req_id}] {status}"
                + (f" job={job_id}" if job_id else "")
                + f" error={result.get('error')}"
            )
            _append_prefixed_log_lines(
                f"[pipeline async stdout {req_id}] ",
                result.get("stdout", ""),
            )
            _append_prefixed_log_lines(
                f"[pipeline async stderr {req_id}] ",
                result.get("stderr", ""),
            )
        except Exception as exc:  # pragma: no cover - best-effort logging
            if job_id:
                _update_async_pipeline_job(
                    job_id,
                    status="failed",
                    success=False,
                    finished_at=time.time(),
                    error=f"{type(exc).__name__}: {exc}",
                    stdout="",
                    stderr="",
                    table=None,
                    data=None,
                )
            _append_helper_log(
                f"[pipeline async {req_id}] exception"
                + (f" job={job_id}" if job_id else "")
                + f": {type(exc).__name__}: {exc}"
            )

    thread = threading.Thread(
        target=_target,
        name=f"pipeline-async-{req_id}",
        daemon=True,
    )
    thread.start()


def _is_load_url_pipeline(pipeline_text: str) -> bool:
    return str(pipeline_text or "").lstrip().lower().startswith(".mpv -url")


def _run_op(op: str, data: Any) -> Dict[str, Any]:
    """Run a helper-only operation.

    These are NOT cmdlets and are not available via CLI pipelines. They exist
    solely so MPV Lua can query lightweight metadata (e.g., autocomplete lists)
    without inventing user-facing commands.
    """
    op_name = str(op or "").strip().lower()

    if op_name in {"run-background",
                   "run_background",
                   "pipeline-background",
                   "pipeline_background"}:
        pipeline_text = ""
        seeds = None
        json_output = False
        requested_job_id = ""
        if isinstance(data, dict):
            pipeline_text = str(data.get("pipeline") or "").strip()
            seeds = data.get("seeds")
            json_output = bool(data.get("json") or data.get("output_json"))
            requested_job_id = str(data.get("job_id") or "").strip()

        if not pipeline_text:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": "Missing pipeline",
                "table": None,
            }

        token = requested_job_id or f"job-{int(time.time() * 1000)}-{threading.get_ident()}"
        job_id = hashlib.sha1(
            f"{token}|{pipeline_text}|{time.time()}".encode("utf-8", "ignore")
        ).hexdigest()[:16]

        _update_async_pipeline_job(
            job_id,
            status="queued",
            success=False,
            pipeline=pipeline_text,
            req_id=job_id,
            finished_at=None,
            error=None,
            stdout="",
            stderr="",
            table=None,
            data=None,
        )
        _run_pipeline_background(
            pipeline_text,
            seeds=seeds,
            req_id=job_id,
            job_id=job_id,
            json_output=json_output,
        )
        return {
            "success": True,
            "stdout": "",
            "stderr": "",
            "error": None,
            "table": None,
            "job_id": job_id,
            "status": "queued",
        }

    if op_name in {"job-status",
                   "job_status",
                   "pipeline-job-status",
                   "pipeline_job_status"}:
        job_id = ""
        if isinstance(data, dict):
            job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": "Missing job_id",
                "table": None,
            }

        job = _get_async_pipeline_job(job_id)
        if not job:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": f"Unknown job_id: {job_id}",
                "table": None,
            }

        payload = dict(job)
        return {
            "success": True,
            "stdout": str(payload.get("stdout") or ""),
            "stderr": str(payload.get("stderr") or ""),
            "error": payload.get("error"),
            "table": payload.get("table"),
            "data": payload.get("data"),
            "job": payload,
        }

    if op_name in {"queue-repl-command",
                   "queue_repl_command",
                   "repl-command",
                   "repl_command"}:
        command_text = ""
        source = "mpv"
        metadata = None
        if isinstance(data, dict):
            command_text = str(data.get("command") or data.get("pipeline") or "").strip()
            source = str(data.get("source") or "mpv").strip() or "mpv"
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else None

        if not command_text:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": "Missing command",
                "table": None,
            }

        repo_root = _repo_root()
        if not repl_state_is_alive(repo_root):
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": "REPL not running",
                "table": None,
                "queued": False,
            }

        queue_path = enqueue_repl_command(
            repo_root,
            command_text,
            source=source,
            metadata=metadata,
        )
        _append_helper_log(
            f"[repl-queue] queued source={source} path={queue_path.name} cmd={command_text}"
        )
        return {
            "success": True,
            "stdout": "",
            "stderr": "",
            "error": None,
            "table": None,
            "path": str(queue_path),
            "queued": True,
        }

    if op_name in {"run-detached",
                   "run_detached",
                   "pipeline-detached",
                   "pipeline_detached"}:
        pipeline_text = ""
        seeds = None
        if isinstance(data, dict):
            pipeline_text = str(data.get("pipeline") or "").strip()
            seeds = data.get("seeds")
        if not pipeline_text:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": "Missing pipeline",
                "table": None,
            }

        py = sys.executable or "python"
        if platform.system() == "Windows":
            try:
                exe = str(py or "").strip()
            except Exception:
                exe = ""
            low = exe.lower()
            if low.endswith("python.exe"):
                try:
                    candidate = exe[:-10] + "pythonw.exe"
                    if os.path.exists(candidate):
                        py = candidate
                except Exception:
                    pass

        cmd = [
            py,
            str((_repo_root() / "CLI.py").resolve()),
            "pipeline",
            "--pipeline",
            pipeline_text,
        ]
        if seeds is not None:
            try:
                cmd.extend(["--seeds-json", json.dumps(seeds, ensure_ascii=False)])
            except Exception:
                # Best-effort; seeds are optional.
                pass

        popen_kwargs: Dict[str,
                           Any] = {
                               "stdin": subprocess.DEVNULL,
                               "stdout": subprocess.DEVNULL,
                               "stderr": subprocess.DEVNULL,
                               "cwd": str(_repo_root()),
                           }
        if platform.system() == "Windows":
            flags = 0
            try:
                flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
            except Exception:
                flags |= 0x00000008
            try:
                flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            except Exception:
                flags |= 0x08000000
            popen_kwargs["creationflags"] = int(flags)
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= int(
                    getattr(subprocess,
                            "STARTF_USESHOWWINDOW",
                            0x00000001)
                )
                si.wShowWindow = subprocess.SW_HIDE
                popen_kwargs["startupinfo"] = si
            except Exception:
                pass
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except Exception as exc:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error":
                f"Failed to spawn detached pipeline: {type(exc).__name__}: {exc}",
                "table": None,
            }

        return {
            "success": True,
            "stdout": "",
            "stderr": "",
            "error": None,
            "table": None,
            "pid": int(getattr(proc,
                               "pid",
                               0) or 0),
        }

    # Provide store backend choices using the dynamic registered store registry only.
    if op_name in {"store-choices",
                   "store_choices",
                   "get-store-choices",
                   "get_store_choices"}:
        cached_choices = _get_cached_store_choices()
        refresh = False
        if isinstance(data, dict):
            refresh = bool(data.get("refresh") or data.get("reload"))

        if cached_choices and not refresh:
            return {
                "success": True,
                "stdout": "",
                "stderr": "",
                "error": None,
                "table": None,
                "choices": cached_choices,
            }

        try:
            choices = _load_store_choices_from_config(force_reload=refresh)

            if not choices and cached_choices:
                choices = cached_choices

            if choices:
                choices = _set_cached_store_choices(choices)

            return {
                "success": True,
                "stdout": "",
                "stderr": "",
                "error": None,
                "table": None,
                "choices": choices,
            }
        except Exception as exc:
            if cached_choices:
                return {
                    "success": True,
                    "stdout": "",
                    "stderr": "",
                    "error": None,
                    "table": None,
                    "choices": cached_choices,
                }
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": f"store-choices failed: {type(exc).__name__}: {exc}",
                "table": None,
                "choices": [],
            }

    if op_name in {"url-exists",
                   "url_exists",
                   "find-url",
                   "find_url"}:
        try:
            from PluginCore.backend_registry import BackendRegistry  # noqa: WPS433

            cfg = load_config() or {}
            storage = BackendRegistry(config=cfg, suppress_debug=True)

            raw_needles: list[str] = []
            if isinstance(data, dict):
                maybe_needles = data.get("needles")
                if isinstance(maybe_needles, (list, tuple, set)):
                    for item in maybe_needles:
                        text = str(item or "").strip()
                        if text and text not in raw_needles:
                            raw_needles.append(text)
                elif isinstance(maybe_needles, str):
                    text = maybe_needles.strip()
                    if text:
                        raw_needles.append(text)

                if not raw_needles:
                    text = str(data.get("url") or "").strip()
                    if text:
                        raw_needles.append(text)

            if not raw_needles:
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "error": "Missing url",
                    "table": None,
                    "data": [],
                }

            matches: list[dict[str, Any]] = []
            seen_keys: set[str] = set()

            for backend_name in storage.list_backends() or []:
                try:
                    backend = storage[backend_name]
                except Exception:
                    continue

                search_fn = getattr(backend, "search", None)
                if not callable(search_fn):
                    continue

                for needle in raw_needles:
                    query = f"url:{needle}"
                    try:
                        results = backend.search(
                            query,
                            limit=1,
                            minimal=True,
                            url_only=True,
                        ) or []
                    except Exception:
                        continue

                    for item in results:
                        if hasattr(item, "to_dict") and callable(getattr(item, "to_dict")):
                            try:
                                item = item.to_dict()
                            except Exception:
                                item = {"title": str(item)}
                        elif not isinstance(item, dict):
                            item = {"title": str(item)}

                        payload = dict(item)
                        payload.setdefault("store", str(backend_name))
                        payload.setdefault("needle", str(needle))

                        key = str(payload.get("hash") or payload.get("url") or payload.get("title") or needle).strip().lower()
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        matches.append(payload)

                    if matches:
                        break

                if matches:
                    break

            return {
                "success": True,
                "stdout": "",
                "stderr": "",
                "error": None,
                "table": None,
                "data": matches,
            }
        except Exception as exc:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": f"url-exists failed: {type(exc).__name__}: {exc}",
                "table": None,
                "data": [],
            }

    # Provide yt-dlp format list for a URL (for MPV "Change format" menu).
    # Returns a ResultTable-like payload so the Lua UI can render without running cmdlets.
    if op_name in {"ytdlp-formats",
                   "ytdlp_formats",
                   "ytdl-formats",
                   "ytdl_formats"}:
        try:
            url = None
            if isinstance(data, dict):
                url = data.get("url")
            url = str(url or "").strip()
            if not url:
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "error": "Missing url",
                    "table": None,
                }

            cfg = load_config() or {}
            plugin = get_plugin("ytdlp", cfg)
            if plugin is None or not hasattr(plugin, "list_url_formats"):
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "error": "yt-dlp plugin unavailable",
                    "table": None,
                }

            try:
                formats = plugin.list_url_formats(
                    url,
                    no_playlist=True,
                    timeout_seconds=25,
                )
            except Exception as exc:
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "error": f"yt-dlp plugin probe failed: {type(exc).__name__}: {exc}",
                    "table": None,
                }

            def _format_bytes(n: Any) -> str:
                """Format bytes using centralized utility."""
                return format_bytes(n)

            if formats is None:
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "error": "yt-dlp format probe failed or timed out",
                    "table": None,
                }

            if not formats:
                return {
                    "success": True,
                    "stdout": "",
                    "stderr": "",
                    "error": None,
                    "table": {
                        "title": "Formats",
                        "rows": []
                    },
                }

            try:
                formats = plugin.filter_picker_formats(formats)
            except Exception:
                pass

            # Debug: dump a short summary of the format list to the helper log.
            try:
                count = len(formats)
                _append_helper_log(
                    f"[ytdlp-formats] extracted formats count={count} url={url}"
                )

                limit = 60
                for i, f in enumerate(formats[:limit], start=1):
                    if not isinstance(f, dict):
                        continue
                    fid = str(f.get("format_id") or "")
                    ext = str(f.get("ext") or "")
                    note = f.get("format_note") or f.get("format") or ""
                    vcodec = str(f.get("vcodec") or "")
                    acodec = str(f.get("acodec") or "")
                    size = f.get("filesize") or f.get("filesize_approx")
                    res = str(f.get("resolution") or "")
                    if not res:
                        try:
                            w = f.get("width")
                            h = f.get("height")
                            if w and h:
                                res = f"{int(w)}x{int(h)}"
                            elif h:
                                res = f"{int(h)}p"
                        except Exception:
                            res = ""
                    _append_helper_log(
                        f"[ytdlp-format {i:02d}] id={fid} ext={ext} res={res} note={note} codecs={vcodec}/{acodec} size={size}"
                    )
                if count > limit:
                    _append_helper_log(
                        f"[ytdlp-formats] (truncated; total={count})"
                    )
            except Exception:
                pass

            rows = []
            for fmt in formats:
                if not isinstance(fmt, dict):
                    continue
                format_id = str(fmt.get("format_id") or "").strip()
                if not format_id:
                    continue
                display_id = get_display_format_id(fmt) or format_id

                # Prefer human-ish resolution.
                resolution = str(fmt.get("resolution") or "").strip()
                if not resolution:
                    w = fmt.get("width")
                    h = fmt.get("height")
                    try:
                        if w and h:
                            resolution = f"{int(w)}x{int(h)}"
                        elif h:
                            resolution = f"{int(h)}p"
                    except Exception:
                        resolution = ""

                ext = str(fmt.get("ext") or "").strip()
                size = _format_bytes(fmt.get("filesize") or fmt.get("filesize_approx"))

                selection_id = get_selection_format_id(fmt, video_audio_suffix="ba") or format_id

                # Build selection args compatible with MPV Lua picker.
                # Use -format instead of -query so Lua can extract the ID easily.
                selection_args = ["-format", selection_id]

                rows.append(
                    {
                        "columns": [
                            {
                                "name": "ID",
                                "value": display_id
                            },
                            {
                                "name": "Resolution",
                                "value": resolution or ""
                            },
                            {
                                "name": "Ext",
                                "value": ext or ""
                            },
                            {
                                "name": "Size",
                                "value": size or ""
                            },
                        ],
                        "selection_args":
                        selection_args,
                    }
                )

            return {
                "success": True,
                "stdout": "",
                "stderr": "",
                "error": None,
                "table": {
                    "title": "Formats",
                    "rows": rows
                },
            }
        except Exception as exc:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": f"{type(exc).__name__}: {exc}",
                "table": None,
            }

    return {
        "success": False,
        "stdout": "",
        "stderr": "",
        "error": f"Unknown op: {op_name}",
        "table": None,
    }


def _append_helper_log(text: str) -> None:
    """Log helper diagnostics to file, database, and the mpv console emitter."""
    payload = (text or "").rstrip()
    if not payload:
        return

    _HELPER_LOG_BACKLOG.append(payload)
    if len(_HELPER_LOG_BACKLOG) > _HELPER_LOG_BACKLOG_LIMIT:
        del _HELPER_LOG_BACKLOG[:-_HELPER_LOG_BACKLOG_LIMIT]

    try:
        with open(_helper_log_path(), "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {payload}\n")
    except Exception:
        pass

    try:
        # Try database logging first (best practice: unified logging)
        from SYS.database import log_to_db
        log_to_db("INFO", "mpv", payload)
    except Exception:
        # Fallback to stderr if database unavailable
        import sys
        print(f"[mpv-helper] {payload}", file=sys.stderr)

    emitter = _HELPER_MPV_LOG_EMITTER
    if emitter is not None:
        try:
            emitter(payload)
        except Exception:
            pass


def _helper_log_path() -> str:
    try:
        log_dir = _repo_root() / "Log"
        log_dir.mkdir(parents=True, exist_ok=True)
        return str((log_dir / "medeia-mpv-helper.log").resolve())
    except Exception:
        tmp = tempfile.gettempdir()
        return str((Path(tmp) / "medeia-mpv-helper.log").resolve())


def _get_ipc_lock_path(ipc_path: str) -> Path:
    """Return the lock file path for a given IPC path."""
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(ipc_path or ""))
    if not safe:
        safe = "mpv"
    lock_dir = Path(tempfile.gettempdir()) / "medeia-mpv-helper"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"medeia-mpv-helper-{safe}.lock"


def _get_ipc_lock_meta_path(ipc_path: str) -> Path:
    lock_path = _get_ipc_lock_path(ipc_path)
    return lock_path.with_suffix(lock_path.suffix + ".json")


def _read_lock_file_pid(ipc_path: str) -> Optional[int]:
    """Return the PID recorded in the lock file by the current holder, or None.

    The lock file can be opened for reading even while another process holds the
    byte-range lock (msvcrt.locking is advisory, not a file-open exclusive lock).
    This lets a challenger identify the exact holder PID and kill only that process,
    avoiding the race where concurrent sibling helpers all kill each other.
    """
    try:
        lock_path = _get_ipc_lock_meta_path(ipc_path)
        with open(str(lock_path), "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read().strip()
        if not content:
            return None
        data = json.loads(content)
        pid = int(data.get("pid") or 0)
        return pid if pid > 0 else None
    except Exception:
        return None


def _write_lock_file_metadata(ipc_path: str) -> None:
    meta_path = _get_ipc_lock_meta_path(ipc_path)
    meta_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "version": MEDEIA_MPV_HELPER_VERSION,
                "ipc": str(ipc_path),
                "started_at": int(time.time()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
        errors="replace",
    )


def _release_ipc_lock(fh: Any, ipc_path: Optional[str] = None) -> None:
    if fh is None:
        if ipc_path:
            try:
                _get_ipc_lock_meta_path(ipc_path).unlink(missing_ok=True)
            except Exception:
                pass
        return
    try:
        if os.name == "nt":
            import msvcrt  # type: ignore

            try:
                fh.seek(0)
            except Exception:
                pass
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # type: ignore

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass
    if ipc_path:
        try:
            _get_ipc_lock_meta_path(ipc_path).unlink(missing_ok=True)
        except Exception:
            pass


def _acquire_ipc_lock(ipc_path: str) -> Optional[Any]:
    """Best-effort singleton lock per IPC path.

    Multiple helpers subscribing to mpv log-message events causes duplicated
    log output. Use a tiny file lock to ensure one helper per mpv instance.
    """
    try:
        lock_path = _get_ipc_lock_path(ipc_path)
        fh = open(lock_path, "a+", encoding="utf-8", errors="replace")

        # On Windows, locking a zero-length file can fail even when no process
        # actually owns the lock anymore. Prime the file with a single byte so
        # stale empty lock files do not wedge future helper startups.
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() < 1:
                fh.write("\n")
                fh.flush()
        except Exception:
            pass

        if os.name == "nt":
            try:
                import msvcrt  # type: ignore

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except Exception:
                try:
                    fh.close()
                except Exception:
                    pass
                return None
        else:
            try:
                import fcntl  # type: ignore

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                try:
                    fh.close()
                except Exception:
                    pass
                return None

        try:
            _write_lock_file_metadata(ipc_path)
        except Exception:
            pass

        return fh
    except Exception:
        return None


def _parse_request(data: Any) -> Optional[Dict[str, Any]]:
    if data is None:
        return None
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None
    if isinstance(data, dict):
        return data
    return None


def _start_request_poll_loop(
    ipc_path: str,
    stop_event: threading.Event,
    handle_request: Callable[[Any, str], bool],
    mark_alive: Optional[Callable[[str], None]] = None,
    note_ipc_unavailable: Optional[Callable[[str], None]] = None,
) -> threading.Thread:
    """Poll the request property on a separate IPC connection.

    Windows named-pipe event delivery can stall even while direct get/set
    property commands still work. Polling the request property provides a more
    reliable fallback transport for helper ops and pipeline dispatch.
    """

    def _poll_loop() -> None:
        poll_client = MPVIPCClient(socket_path=ipc_path, timeout=0.75, silent=True)
        while not stop_event.is_set():
            try:
                was_disconnected = poll_client.sock is None
                if was_disconnected:
                    if not poll_client.connect():
                        if note_ipc_unavailable is not None:
                            note_ipc_unavailable("request-poll-connect")
                        stop_event.wait(0.10)
                        continue
                    if mark_alive is not None:
                        mark_alive("request-poll-connect")

                resp = poll_client.send_command(["get_property", REQUEST_PROP])
                if not resp:
                    if note_ipc_unavailable is not None:
                        note_ipc_unavailable("request-poll-read")
                    try:
                        poll_client.disconnect()
                    except Exception:
                        pass
                    stop_event.wait(0.10)
                    continue

                if mark_alive is not None:
                    mark_alive("request-poll-read")
                if resp.get("error") == "success":
                    handle_request(resp.get("data"), "poll")
                stop_event.wait(0.05)
            except Exception:
                if note_ipc_unavailable is not None:
                    note_ipc_unavailable("request-poll-exception")
                try:
                    poll_client.disconnect()
                except Exception:
                    pass
                stop_event.wait(0.15)

        try:
            poll_client.disconnect()
        except Exception:
            pass

    thread = threading.Thread(
        target=_poll_loop,
        name="mpv-helper-request-poll",
        daemon=True,
    )
    thread.start()
    return thread


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mpv-pipeline-helper")
    parser.add_argument("--ipc", required=True, help="mpv --input-ipc-server path")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    # Load config once and configure logging similar to CLI.pipeline.
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}

    try:
        debug_enabled = bool(isinstance(cfg, dict) and cfg.get("debug", False))
        set_debug(debug_enabled)

        if debug_enabled:
            logging.basicConfig(
                level=logging.DEBUG,
                format="[%(name)s] %(levelname)s: %(message)s",
                stream=sys.stderr,
            )
            for noisy in ("httpx",
                          "httpcore",
                          "httpcore.http11",
                          "httpcore.connection"):
                try:
                    logging.getLogger(noisy).setLevel(logging.WARNING)
                except Exception:
                    pass
    except Exception:
        pass

    # Ensure all in-process cmdlets that talk to MPV pick up the exact IPC server
    # path used by this helper (which comes from the running MPV instance).
    os.environ["MEDEIA_MPV_IPC"] = str(args.ipc)

    # Generous deadline: the kill + OS-lock-release cycle can take several seconds,
    # especially when a stale helper is running as a different process image.
    lock_wait_deadline = time.time() + 12.0
    lock_wait_logged = False
    _lock_fh = None
    _kill_attempted = False  # kill at most once; re-killing on every loop causes sibling helpers to kill each other

    while _lock_fh is None:
        # Try to acquire the lock first — avoids unnecessary process enumeration
        # when there is no contention (normal cold-start path).
        _lock_fh = _acquire_ipc_lock(str(args.ipc))
        if _lock_fh is not None:
            break

        if not lock_wait_logged:
            _append_helper_log(
                f"[helper] waiting for helper lock release ipc={args.ipc}"
            )
            lock_wait_logged = True

        if time.time() >= lock_wait_deadline:
            _append_helper_log(
                f"[helper] another instance still holds lock for ipc={args.ipc}; exiting after wait"
            )
            return 0

        # Kill the lock holder at most once.  Repeatedly scanning for all matching
        # processes on every iteration caused concurrent sibling helpers (spawned by
        # the Lua 3-second timeout cycle) to kill each other before any could start.
        if platform.system() == "Windows" and not _kill_attempted:
            _kill_attempted = True
            try:
                # Prefer targeted kill via PID recorded in the lock file.
                # msvcrt byte-range locking does not prevent reading the file from
                # another process, so we can always identify the exact holder PID.
                holder_pid = _read_lock_file_pid(str(args.ipc))
                if holder_pid and holder_pid != os.getpid():
                    _append_helper_log(
                        f"[helper] killing lock holder pid={holder_pid} ipc={args.ipc}"
                    )
                    _windows_kill_pids([holder_pid])
                else:
                    # Fallback: old helpers (pre-PID-in-lock-file) left no PID.
                    # Scan once by command-line pattern.
                    sibling_pids = [
                        p for p in _windows_list_pipeline_helper_pids(str(args.ipc))
                        if p and p != os.getpid()
                    ]
                    if sibling_pids:
                        _append_helper_log(
                            f"[helper] killing old-style sibling pids={sibling_pids} ipc={args.ipc}"
                        )
                        _windows_kill_pids(sibling_pids)
                    else:
                        _append_helper_log(
                            f"[helper] no identifiable lock holder for ipc={args.ipc}; waiting"
                        )
            except Exception as exc:
                _append_helper_log(
                    f"[helper] kill failed: {type(exc).__name__}: {exc}"
                )

        time.sleep(0.5)

    try:
        _append_helper_log(
            f"[helper] version={MEDEIA_MPV_HELPER_VERSION} started ipc={args.ipc}"
        )
        try:
            _write_lock_file_metadata(str(args.ipc))
        except Exception:
            pass
        try:
            _append_helper_log(
                f"[helper] file={Path(__file__).resolve()} cwd={Path.cwd().resolve()}"
            )
        except Exception:
            pass
        try:
            runtime_root = _runtime_config_root()
            _append_helper_log(
                f"[helper] config_root={runtime_root} exists={bool((runtime_root / 'config.conf').exists())}"
            )
        except Exception:
            pass
        debug(f"[mpv-helper] logging to: {_helper_log_path()}")
    except Exception:
        pass

    # Route SYS.logger output into the helper log file so diagnostics are not
    # lost in mpv's console/terminal output.
    try:

        class _HelperLogStream:

            def __init__(self) -> None:
                self._pending = ""

            def write(self, s: str) -> int:
                if not s:
                    return 0
                text = self._pending + str(s)
                lines = text.splitlines(keepends=True)
                if lines and not lines[-1].endswith(("\n", "\r")):
                    self._pending = lines[-1]
                    lines = lines[:-1]
                else:
                    self._pending = ""
                for line in lines:
                    payload = line.rstrip("\r\n")
                    if payload:
                        _append_helper_log("[py] " + payload)
                return len(s)

            def flush(self) -> None:
                if self._pending:
                    _append_helper_log("[py] " + self._pending.rstrip("\r\n"))
                    self._pending = ""

        set_thread_stream(_HelperLogStream())
    except Exception:
        pass

    # Prefer a stable repo-local log folder for discoverability.
    error_log_dir = _repo_root() / "Log"
    try:
        error_log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        error_log_dir = Path(tempfile.gettempdir())
    last_error_log = error_log_dir / "medeia-mpv-pipeline-last-error.log"
    seen_request_ids: Dict[str, float] = {}
    seen_request_ids_lock = threading.Lock()
    seen_request_ttl_seconds = 180.0
    request_processing_lock = threading.Lock()
    command_client_lock = threading.Lock()
    stop_event = threading.Event()
    ipc_loss_grace_seconds = 4.0
    ipc_lost_since: Optional[float] = None
    ipc_connected_once = False
    shutdown_reason = ""
    shutdown_reason_lock = threading.Lock()
    _send_helper_command = lambda _command, _label='': False
    _publish_store_choices_cached_property = lambda _choices: None

    def _request_shutdown(reason: str) -> None:
        nonlocal shutdown_reason
        message = str(reason or "").strip() or "unknown"
        with shutdown_reason_lock:
            if shutdown_reason:
                return
            shutdown_reason = message
        _append_helper_log(f"[helper] shutdown requested: {message}")
        stop_event.set()

    def _mark_ipc_alive(source: str = "") -> None:
        nonlocal ipc_lost_since, ipc_connected_once
        if ipc_lost_since is not None and source:
            _append_helper_log(f"[helper] ipc restored via {source}")
        ipc_connected_once = True
        ipc_lost_since = None

    def _note_ipc_unavailable(source: str) -> None:
        nonlocal ipc_lost_since
        if stop_event.is_set() or not ipc_connected_once:
            return
        now = time.time()
        if ipc_lost_since is None:
            ipc_lost_since = now
            _append_helper_log(
                f"[helper] ipc unavailable via {source}; waiting {ipc_loss_grace_seconds:.1f}s for reconnect"
            )
            return
        if (now - ipc_lost_since) >= ipc_loss_grace_seconds:
            if platform.system() == "Windows":
                try:
                    if _windows_list_mpv_pids(str(args.ipc)):
                        ipc_lost_since = now
                        _append_helper_log(
                            f"[helper] ipc still owned by live mpv process; continuing reconnect wait source={source}"
                        )
                        return
                except Exception:
                    pass
            _request_shutdown(
                f"mpv ipc unavailable for {now - ipc_lost_since:.2f}s via {source}"
            )

    def _write_error_log(text: str, *, req_id: str) -> Optional[str]:
        try:
            error_log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        payload = (text or "").strip()
        if not payload:
            return None

        stamped = error_log_dir / f"medeia-mpv-pipeline-error-{req_id}.log"
        try:
            stamped.write_text(payload, encoding="utf-8", errors="replace")
        except Exception:
            stamped = None

        try:
            last_error_log.write_text(payload, encoding="utf-8", errors="replace")
        except Exception:
            pass

        return str(stamped) if stamped else str(last_error_log)

    def _mark_request_seen(req_id: str) -> bool:
        if not req_id:
            return False
        now = time.time()
        cutoff = now - seen_request_ttl_seconds
        with seen_request_ids_lock:
            expired = [key for key, ts in seen_request_ids.items() if ts < cutoff]
            for key in expired:
                seen_request_ids.pop(key, None)
            if req_id in seen_request_ids:
                return False
            seen_request_ids[req_id] = now
            return True

    def _publish_response(resp: Dict[str, Any]) -> None:
        req_id = str(resp.get("id") or "")
        ok = _send_helper_command(
            [
                "set_property_string",
                RESPONSE_PROP,
                json.dumps(resp, ensure_ascii=False),
            ],
            f"response:{req_id or 'unknown'}",
        )
        if ok:
            _append_helper_log(
                f"[response {req_id or '?'}] published success={bool(resp.get('success'))}"
            )
        else:
            _append_helper_log(
                f"[response {req_id or '?'}] publish failed success={bool(resp.get('success'))}"
            )

    def _process_request(raw: Any, source: str) -> bool:
        req = _parse_request(raw)
        if not req:
            try:
                if isinstance(raw, str) and raw.strip():
                    snippet = raw.strip().replace("\r", "").replace("\n", " ")
                    if len(snippet) > 220:
                        snippet = snippet[:220] + "…"
                    _append_helper_log(
                        f"[request-raw {source}] could not parse request json: {snippet}"
                    )
            except Exception:
                pass
            return False

        req_id = str(req.get("id") or "")
        op = str(req.get("op") or "").strip()
        data = req.get("data")
        pipeline_text = str(req.get("pipeline") or "").strip()
        seeds = req.get("seeds")
        json_output = bool(req.get("json") or req.get("output_json"))

        if not req_id:
            return False

        if not _mark_request_seen(req_id):
            return False

        try:
            label = pipeline_text if pipeline_text else (op and ("op=" + op) or "(empty)")
            _append_helper_log(f"\n[request {req_id} via {source}] {label}")
        except Exception:
            pass

        with request_processing_lock:
            async_dispatch = False
            try:
                if op:
                    run = _run_op(op, data)
                else:
                    if not pipeline_text:
                        return False
                    if _is_load_url_pipeline(pipeline_text):
                        async_dispatch = True
                        run = {
                            "success": True,
                            "stdout": "",
                            "stderr": "",
                            "error": "",
                            "table": None,
                        }
                        _run_pipeline_background(
                            pipeline_text,
                            seeds=seeds,
                            req_id=req_id,
                        )
                    else:
                        run = _run_pipeline(
                            pipeline_text,
                            seeds=seeds,
                            json_output=json_output,
                        )

                resp = {
                    "id": req_id,
                    "success": bool(run.get("success")),
                    "stdout": run.get("stdout", ""),
                    "stderr": run.get("stderr", ""),
                    "error": run.get("error"),
                    "table": run.get("table"),
                    "data": run.get("data"),
                }
                if "choices" in run:
                    resp["choices"] = run.get("choices")
                if "job_id" in run:
                    resp["job_id"] = run.get("job_id")
                if "job" in run:
                    resp["job"] = run.get("job")
                if "status" in run:
                    resp["status"] = run.get("status")
                if "pid" in run:
                    resp["pid"] = run.get("pid")
                if async_dispatch:
                    resp["info"] = "queued asynchronously"
            except Exception as exc:
                resp = {
                    "id": req_id,
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "error": f"{type(exc).__name__}: {exc}",
                    "table": None,
                }

            try:
                if op:
                    extra = ""
                    if isinstance(resp.get("choices"), list):
                        extra = f" choices={len(resp.get('choices') or [])}"
                    _append_helper_log(
                        f"[request {req_id}] op-finished success={bool(resp.get('success'))}{extra}"
                    )
            except Exception:
                pass

            try:
                if resp.get("stdout"):
                    _append_helper_log("[stdout]\n" + str(resp.get("stdout")))
                if resp.get("stderr"):
                    _append_helper_log("[stderr]\n" + str(resp.get("stderr")))
                if resp.get("error"):
                    _append_helper_log("[error]\n" + str(resp.get("error")))
            except Exception:
                pass

            if not resp.get("success"):
                details = ""
                if resp.get("error"):
                    details += str(resp.get("error"))
                if resp.get("stderr"):
                    details = (details + "\n" if details else "") + str(resp.get("stderr"))
                log_path = _write_error_log(details, req_id=req_id)
                if log_path:
                    resp["log_path"] = log_path

            try:
                _publish_response(resp)
            except Exception:
                pass

            if resp.get("success") and isinstance(resp.get("choices"), list):
                try:
                    _publish_store_choices_cached_property(resp.get("choices"))
                except Exception:
                    pass

        return True

    # Connect to mpv's JSON IPC. On Windows, the pipe can exist but reject opens
    # briefly during startup; also mpv may create the IPC server slightly after
    # the Lua script launches us. Retry until timeout.
    connect_deadline = time.time() + max(0.5, float(args.timeout))
    last_connect_error: Optional[str] = None

    client = MPVIPCClient(socket_path=args.ipc, timeout=0.5, silent=True)
    while True:
        try:
            if client.connect():
                _mark_ipc_alive("startup-connect")
                break
        except Exception as exc:
            last_connect_error = f"{type(exc).__name__}: {exc}"

        if time.time() > connect_deadline:
            _append_helper_log(
                f"[helper] failed to connect ipc={args.ipc} error={last_connect_error or 'timeout'}"
            )
            return 2

        # Keep trying.
        time.sleep(0.10)

    use_shared_ipc_client = platform.system() == "Windows"
    command_client = None if use_shared_ipc_client else MPVIPCClient(socket_path=str(args.ipc), timeout=0.75, silent=True)

    def _send_helper_command(command: Any, label: str = "") -> bool:
        with command_client_lock:
            target_client = client if use_shared_ipc_client else command_client
            if target_client is None:
                return False
            try:
                if target_client.sock is None:
                    if use_shared_ipc_client:
                        _note_ipc_unavailable(f"helper-command-connect:{label or '?'}")
                        return False
                    if not target_client.connect():
                        _append_helper_log(
                            f"[helper-ipc] connect failed label={label or '?'}"
                        )
                        _note_ipc_unavailable(f"helper-command-connect:{label or '?' }")
                        return False
                    _mark_ipc_alive(f"helper-command-connect:{label or '?'}")
                rid = target_client.send_command_no_wait(command)
                if rid is None:
                    _append_helper_log(
                        f"[helper-ipc] send failed label={label or '?'}"
                    )
                    _note_ipc_unavailable(f"helper-command-send:{label or '?'}")
                    try:
                        target_client.disconnect()
                    except Exception:
                        pass
                    return False
                _mark_ipc_alive(f"helper-command-send:{label or '?'}")
                return True
            except Exception as exc:
                _append_helper_log(
                    f"[helper-ipc] exception label={label or '?'} error={type(exc).__name__}: {exc}"
                )
                _note_ipc_unavailable(f"helper-command-exception:{label or '?'}")
                try:
                    target_client.disconnect()
                except Exception:
                    pass
                return False

    def _emit_helper_log_to_mpv(payload: str) -> None:
        safe = str(payload or "").replace("\r", " ").replace("\n", " ").strip()
        if not safe:
            return
        if len(safe) > 900:
            safe = safe[:900] + "..."
        try:
            client.send_command_no_wait(["print-text", f"medeia-helper: {safe}"])
        except Exception:
            return

    global _HELPER_MPV_LOG_EMITTER
    _HELPER_MPV_LOG_EMITTER = _emit_helper_log_to_mpv
    for backlog_line in list(_HELPER_LOG_BACKLOG):
        try:
            _emit_helper_log_to_mpv(backlog_line)
        except Exception:
            break

    # Mark ready ASAP and keep it fresh.
    # Use a unix timestamp so the Lua side can treat it as a heartbeat.
    last_ready_ts: float = 0.0

    def _touch_ready() -> None:
        nonlocal last_ready_ts
        now = time.time()
        # Throttle updates to reduce IPC chatter.
        if (now - last_ready_ts) < 0.75:
            return
        try:
            _send_helper_command(
                ["set_property_string", READY_PROP, str(int(now))],
                "ready-heartbeat",
            )
            last_ready_ts = now
        except Exception:
            return

    def _publish_store_choices_cached_property(choices: Any) -> None:
        payload = _store_choices_payload(choices)
        if not payload:
            return
        _send_helper_command(
            [
                "set_property_string",
                "user-data/medeia-store-choices-cached",
                payload,
            ],
            "store-choices-cache",
        )

    def _publish_helper_version() -> None:
        if _send_helper_command(
            ["set_property_string", VERSION_PROP, MEDEIA_MPV_HELPER_VERSION],
            "helper-version",
        ):
            _append_helper_log(
                f"[helper] published helper version {MEDEIA_MPV_HELPER_VERSION}"
            )

    # Mirror mpv's own log messages into our helper log file so debugging does
    # not depend on the mpv on-screen console or mpv's log-file.
    try:
        # IMPORTANT: mpv debug logs can be extremely chatty (especially ytdl_hook)
        # and can starve request handling. Default to warn unless explicitly overridden.
        level = os.environ.get("MEDEIA_MPV_HELPER_MPVLOG", "").strip() or "warn"
        client.send_command_no_wait(["request_log_messages", level])
        _append_helper_log(f"[helper] requested mpv log messages level={level}")
    except Exception:
        pass

    # De-dup/throttle mpv log-message lines (mpv and yt-dlp can be very chatty).
    last_mpv_line: Optional[str] = None
    last_mpv_count: int = 0
    last_mpv_ts: float = 0.0

    def _flush_mpv_repeat() -> None:
        nonlocal last_mpv_line, last_mpv_count
        if last_mpv_line and last_mpv_count > 1:
            _append_helper_log(f"[mpv] (previous line repeated {last_mpv_count}x)")
        last_mpv_line = None
        last_mpv_count = 0

    # Observe request property changes.
    try:
        client.send_command_no_wait(
            ["observe_property",
             OBS_ID_REQUEST,
             REQUEST_PROP,
             "string"]
        )
    except Exception:
        return 3

    # Mark ready only after the observer is installed to avoid races where Lua
    # sends a request before we can receive property-change notifications.
    try:
        _touch_ready()
        _publish_helper_version()
        _append_helper_log(f"[helper] ready heartbeat armed prop={READY_PROP}")
    except Exception:
        pass

    if use_shared_ipc_client:
        _append_helper_log(
            "[helper] Windows single-client IPC mode enabled; auxiliary heartbeat/poll disabled"
        )
    else:
        _start_ready_heartbeat(
            str(args.ipc),
            stop_event,
            _mark_ipc_alive,
            _note_ipc_unavailable,
        )
        _start_request_poll_loop(
            str(args.ipc),
            stop_event,
            _process_request,
            _mark_ipc_alive,
            _note_ipc_unavailable,
        )

    # Pre-compute store choices at startup and publish to a cached property so Lua
    # can read immediately without waiting for a request/response cycle (which may timeout).
    try:
        startup_choices_payload = _run_op("store-choices", None)
        startup_choices = (
            startup_choices_payload.get("choices")
            if isinstance(startup_choices_payload,
                          dict) else None
        )
        if isinstance(startup_choices, list):
            startup_choices = _set_cached_store_choices(startup_choices)
            preview = ", ".join(str(x) for x in startup_choices[:50])
            _append_helper_log(
                f"[helper] startup store-choices count={len(startup_choices)} items={preview}"
            )

            # Publish to a cached property for Lua to read without IPC request.
            try:
                _publish_store_choices_cached_property(startup_choices)
                _append_helper_log(
                    "[helper] published store-choices to user-data/medeia-store-choices-cached"
                )
            except Exception as exc:
                _append_helper_log(
                    f"[helper] failed to publish store-choices: {type(exc).__name__}: {exc}"
                )
        else:
            _append_helper_log("[helper] startup store-choices unavailable")
    except Exception as exc:
        _append_helper_log(
            f"[helper] startup store-choices failed: {type(exc).__name__}: {exc}"
        )

    # Also publish config temp directory if available
    try:
        cfg = load_config()
        temp_dir = cfg.get("temp", "").strip() or os.getenv("TEMP") or "/tmp"
        if temp_dir:
            _send_helper_command(
                ["set_property_string",
                 "user-data/medeia-config-temp",
                 temp_dir],
                "config-temp",
            )
            _append_helper_log(
                f"[helper] published config temp to user-data/medeia-config-temp={temp_dir}"
            )
    except Exception as exc:
        _append_helper_log(
            f"[helper] failed to publish config temp: {type(exc).__name__}: {exc}"
        )

    # Publish yt-dlp supported domains for Lua menu filtering
    try:
        plugin_class = get_plugin_class("ytdlp")
        domains = []
        if plugin_class is not None:
            domains = sorted(
                {
                    str(value).strip().lower()
                    for value in plugin_class.url_patterns()
                    if isinstance(value, str)
                    and str(value).strip()
                    and "://" not in str(value)
                    and not str(value).strip().endswith(":")
                }
            )
        if domains:
            # We join them into a space-separated string for Lua to parse easily
            domains_str = " ".join(domains)
            _send_helper_command(
                [
                    "set_property_string",
                    "user-data/medeia-ytdlp-domains-cached",
                    domains_str
                ],
                "ytdlp-domains",
            )
            _append_helper_log(
                f"[helper] published {len(domains)} ytdlp domains for Lua menu filtering"
            )
    except Exception as exc:
        _append_helper_log(
            f"[helper] failed to publish ytdlp domains: {type(exc).__name__}: {exc}"
        )

    try:
        _append_helper_log(f"[helper] connected to ipc={args.ipc}")
    except Exception:
        pass

    try:
        while not stop_event.is_set():
            msg = client.read_message(timeout=0.25)
            if msg is None:
                if client.sock is None:
                    _note_ipc_unavailable("main-read")
                else:
                    _mark_ipc_alive("main-idle")
                # Keep READY fresh even when idle (Lua may clear it on timeouts).
                _touch_ready()
                time.sleep(0.02)
                continue

            _mark_ipc_alive("main-read")

            if msg.get("event") == "__eof__":
                _request_shutdown("mpv closed ipc stream")
                break

            if msg.get("event") == "log-message":
                try:
                    level = str(msg.get("level") or "")
                    prefix = str(msg.get("prefix") or "")
                    text = str(msg.get("text") or "").rstrip()

                    if not text:
                        continue

                    # Filter excessive noise unless debug is enabled.
                    if not debug_enabled:
                        lower_prefix = prefix.lower()
                        if "quic" in lower_prefix and "DEBUG:" in text:
                            continue
                        # Suppress progress-bar style lines (keep true errors).
                        if ("ETA" in text or "%" in text) and ("ERROR:" not in text
                                                               and "WARNING:" not in text):
                            # Typical yt-dlp progress bar line.
                            if text.lstrip().startswith("["):
                                continue

                    line = f"[mpv {level}] {prefix} {text}".strip()

                    now = time.time()
                    if last_mpv_line == line and (now - last_mpv_ts) < 2.0:
                        last_mpv_count += 1
                        last_mpv_ts = now
                        continue

                    _flush_mpv_repeat()
                    last_mpv_line = line
                    last_mpv_count = 1
                    last_mpv_ts = now
                    _append_helper_log(line)
                except Exception:
                    pass
                continue

            if msg.get("event") != "property-change":
                continue

            if msg.get("id") != OBS_ID_REQUEST:
                continue

            _process_request(msg.get("data"), "observe")
    finally:
        stop_event.set()
        try:
            _flush_mpv_repeat()
        except Exception:
            pass
        if shutdown_reason:
            try:
                _append_helper_log(f"[helper] exiting reason={shutdown_reason}")
            except Exception:
                pass
        if command_client is not None:
            try:
                command_client.disconnect()
            except Exception:
                pass
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            _release_ipc_lock(_lock_fh, str(args.ipc))
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
