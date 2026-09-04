"""MPV IPC client for cross-platform communication.

This module provides a cross-platform interface to communicate with mpv
using either named pipes (Windows) or Unix domain sockets (Linux/macOS).

This is the central hub for all Python-mpv IPC communication. The Lua script
should use the Python CLI, which uses this module to manage mpv connections.
"""

import atexit
import ctypes
import json
import os
import platform
import socket
import subprocess
import sys
import time as _time
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, List, BinaryIO, Tuple, cast

from SYS.logger import debug

# Fixed pipe name for persistent MPV connection across all Python sessions
FIXED_IPC_PIPE_NAME = "mpv-medios-macina"
MPV_LUA_SCRIPT_PATH = str(Path(__file__).resolve().parent / "LUA" / "main.lua")

_LYRIC_PROCESS: Optional[subprocess.Popen] = None
_LYRIC_LOG_FH: Optional[Any] = None

_MPV_AVAILABILITY_CACHE: Optional[Tuple[bool, Optional[str]]] = None


def _repo_root() -> Path:
    package_dir = Path(__file__).resolve().parent
    if package_dir.name.lower() == "mpv" and package_dir.parent.name.lower() == "plugins":
        return package_dir.parent.parent
    return package_dir.parent


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def _windows_pipe_available(path: str) -> bool:
    """Check if a Windows named pipe is ready without raising."""
    if platform.system() != "Windows":
        return False
    if not path:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        WaitNamedPipeW = kernel32.WaitNamedPipeW
        WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        WaitNamedPipeW.restype = ctypes.c_bool
        # Timeout 0 ensures we don't block.
        return bool(WaitNamedPipeW(path, 0))
    except Exception:
        return False


def _windows_pipe_bytes_available(pipe: BinaryIO) -> Optional[int]:
    """Return the number of bytes ready to read from a Windows named pipe."""
    if platform.system() != "Windows":
        return None
    try:
        import msvcrt

        handle = msvcrt.get_osfhandle(pipe.fileno())
        kernel32 = ctypes.windll.kernel32
        PeekNamedPipe = kernel32.PeekNamedPipe
        PeekNamedPipe.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        PeekNamedPipe.restype = ctypes.c_bool

        total_available = ctypes.c_uint32(0)
        ok = PeekNamedPipe(
            ctypes.c_void_p(handle),
            None,
            0,
            None,
            ctypes.byref(total_available),
            None,
        )
        if not ok:
            return None
        return int(total_available.value)
    except Exception:
        return None


def _windows_pythonw_exe(python_exe: Optional[str]) -> Optional[str]:
    """Return a pythonw.exe adjacent to python.exe if available (Windows only)."""
    if platform.system() != "Windows":
        return python_exe
    try:
        exe = str(python_exe or "").strip()
    except Exception:
        exe = ""
    if not exe:
        return None
    low = exe.lower()
    if low.endswith("pythonw.exe"):
        return exe
    if low.endswith("python.exe"):
        try:
            candidate = exe[:-10] + "pythonw.exe"
            if os.path.exists(candidate):
                return candidate
        except Exception:
            pass
    return exe


def _windows_hidden_subprocess_kwargs() -> Dict[str, Any]:
    """Best-effort kwargs to avoid flashing console windows on Windows.

    Applies to subprocess.run/check_output/Popen.
    """
    if platform.system() != "Windows":
        return {}

    kwargs: Dict[str,
                 Any] = {}
    try:
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = int(create_no_window)
    except Exception:
        pass

    # Also set startupinfo to hidden, for APIs that honor it.
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    except Exception:
        pass

    return kwargs


def _windows_list_mpv_pids(ipc_path: str) -> List[int]:
    """Return PIDs of mpv.exe processes configured for the given IPC pipe."""
    if platform.system() != "Windows":
        return []
    try:
        ipc_path = str(ipc_path or "").strip()
    except Exception:
        ipc_path = ""
    if not ipc_path:
        return []

    ps_script = (
        "$ipc = " + json.dumps(ipc_path) + "; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^mpv(\\.exe)?$' -and $_.CommandLine -and $_.CommandLine -match ('--input-ipc-server=' + [regex]::Escape($ipc)) } | "
        "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )

    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_script],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
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

    raw = obj if isinstance(obj, list) else [obj]
    pids: List[int] = []
    for value in raw:
        try:
            pid = int(value)
        except Exception:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def _check_mpv_availability() -> Tuple[bool, Optional[str]]:
    """Return (available, reason) for the mpv executable.

    This checks that:
    - `mpv` is present in PATH
    - `mpv --version` can run successfully

    Result is cached per-process to avoid repeated subprocess calls.
    """
    global _MPV_AVAILABILITY_CACHE
    if _MPV_AVAILABILITY_CACHE is not None:
        return _MPV_AVAILABILITY_CACHE

    mpv_path = shutil.which("mpv")
    if not mpv_path:
        _MPV_AVAILABILITY_CACHE = (False, "Executable 'mpv' not found in PATH")
        return _MPV_AVAILABILITY_CACHE

    try:
        result = subprocess.run(
            [mpv_path,
             "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            **_windows_hidden_subprocess_kwargs(),
        )
        if result.returncode == 0:
            _MPV_AVAILABILITY_CACHE = (True, None)
            return _MPV_AVAILABILITY_CACHE
        _MPV_AVAILABILITY_CACHE = (
            False,
            f"MPV returned non-zero exit code: {result.returncode}"
        )
        return _MPV_AVAILABILITY_CACHE
    except Exception as exc:
        _MPV_AVAILABILITY_CACHE = (False, f"Error running MPV: {exc}")
        return _MPV_AVAILABILITY_CACHE


def _windows_list_lyric_helper_pids(ipc_path: str) -> List[int]:
    """Return PIDs of `python -m plugins.mpv.lyric --ipc <ipc_path>` helpers (Windows only)."""
    if platform.system() != "Windows":
        return []
    try:
        ipc_path = str(ipc_path or "")
    except Exception:
        ipc_path = ""
    if not ipc_path:
        return []

    # Use CIM to query command lines; output as JSON for robust parsing.
    # Note: `ConvertTo-Json` returns a number for single item, array for many, or null.
    ps_script = (
        "$ipc = " + json.dumps(ipc_path) + "; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -match ' -m\\s+plugins\\.mpv\\.lyric(\\s|$)' -and $_.CommandLine -match ('--ipc\\s+' + [regex]::Escape($ipc)) } | "
        "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )

    try:
        out = subprocess.check_output(
            ["powershell",
             "-NoProfile",
             "-Command",
             ps_script],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
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

    pids: List[int] = []
    if isinstance(obj, list):
        for v in obj:
            try:
                pids.append(int(v))
            except Exception:
                pass
    else:
        try:
            pids.append(int(obj))
        except Exception:
            pass

    # De-dupe and filter obvious junk.
    uniq: List[int] = []
    for pid in pids:
        if pid and pid > 0 and pid not in uniq:
            uniq.append(pid)
    return uniq


def _windows_kill_pids(pids: List[int]) -> None:
    if platform.system() != "Windows":
        return
    for pid in pids or []:
        try:
            subprocess.run(
                ["taskkill",
                 "/PID",
                 str(int(pid)),
                 "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                **_windows_hidden_subprocess_kwargs(),
            )
        except Exception:
            continue


class MPVIPCError(Exception):
    """Raised when MPV IPC communication fails."""

    pass


class MPV:
    """High-level MPV controller for this app.

    Responsibilities:
    - Own the IPC pipe/socket path
    - Start MPV with the bundled Lua script
    - Query playlist and currently playing item via IPC

    This class intentionally stays "dumb": it does not implement app logic.
    App behavior is driven by cmdlet (e.g. `.pipe`) and the bundled Lua script.
    """

    def __init__(
        self,
        ipc_path: Optional[str] = None,
        lua_script_path: Optional[str | Path] = None,
        timeout: float = 5.0,
        check_mpv: bool = True,
    ) -> None:

        if bool(check_mpv):
            ok, reason = _check_mpv_availability()
            if not ok:
                raise MPVIPCError(reason or "MPV unavailable")

        self.timeout = timeout
        self.ipc_path = ipc_path or get_ipc_pipe_path()

        if lua_script_path is None:
            lua_script_path = MPV_LUA_SCRIPT_PATH
        lua_path = Path(str(lua_script_path)).resolve()
        self.lua_script_path = str(lua_path)

    def client(self, silent: bool = False) -> "MPVIPCClient":
        return MPVIPCClient(
            socket_path=self.ipc_path,
            timeout=self.timeout,
            silent=bool(silent)
        )

    def is_running(self) -> bool:
        client = self.client(silent=True)
        try:
            ok = client.connect()
            if ok:
                return True
            return self.has_process_owner()
        finally:
            client.disconnect()

    def has_process_owner(self) -> bool:
        if platform.system() != "Windows":
            return False
        try:
            return bool(_windows_list_mpv_pids(str(self.ipc_path)))
        except Exception:
            return False

    def send(self,
             command: Dict[str,
                           Any] | List[Any],
             silent: bool = False,
             wait: bool = True) -> Optional[Dict[str,
                                                    Any]]:
        client = self.client(silent=bool(silent))
        try:
            if not client.connect():
                return None
            return client.send_command(command, wait=wait)
        except Exception as exc:
            if not silent:
                debug(f"MPV IPC error: {exc}")
            return None
        finally:
            client.disconnect()

    def get_property(self, name: str, default: Any = None) -> Any:
        resp = self.send({
            "command": ["get_property",
                        name]
        })
        if resp and resp.get("error") == "success":
            return resp.get("data", default)
        return default

    def set_property(self, name: str, value: Any) -> bool:
        resp = self.send({
            "command": ["set_property",
                        name,
                        value]
        })
        return bool(resp and resp.get("error") == "success")

    def download(
        self,
        *,
        url: str,
        fmt: str,
        store: Optional[str] = None,
        path: Optional[str] = None,
    ) -> Dict[str,
              Any]:
        """Download a URL using the same pipeline semantics as the MPV UI.

        This is intended as a stable Python entrypoint for "button actions".
        It does not require mpv.exe availability (set check_mpv=False if needed).
        """
        url = str(url or "").strip()
        fmt = str(fmt or "").strip()
        store = str(store or "").strip() if store is not None else None
        path = str(path or "").strip() if path is not None else None

        if not url:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": "Missing url"
            }
        if not fmt:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": "Missing fmt"
            }
        if bool(store) == bool(path):
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": "Provide exactly one of store or path",
            }

        # Ensure any in-process cmdlets that talk to MPV pick up this IPC path.
        try:
            os.environ["MEDEIA_MPV_IPC"] = str(self.ipc_path)
        except Exception:
            pass

        def _q(s: str) -> str:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

        pipeline = f"file -download -url {_q(url)} -query {_q(f'format:{fmt}')}"
        if store:
            pipeline += f" | file -add -plugin hydrusnetwork -instance {_q(store)}"
        else:
            pipeline += f" | file -add -plugin local -instance {_q(path or '')}"

        try:
            from SYS.pipeline_runner import PipelineRunner  # noqa: WPS433

            runner = PipelineRunner()
            result = runner.run_pipeline(pipeline)
            return {
                "success": bool(getattr(result,
                                        "success",
                                        False)),
                "stdout": getattr(result,
                                  "stdout",
                                  "") or "",
                "stderr": getattr(result,
                                  "stderr",
                                  "") or "",
                "error": getattr(result,
                                 "error",
                                 None),
                "pipeline": pipeline,
            }
        except Exception as exc:
            return {
                "success": False,
                "stdout": "",
                "stderr": "",
                "error": f"{type(exc).__name__}: {exc}",
                "pipeline": pipeline,
            }

    def get_playlist(self, silent: bool = False) -> Optional[List[Dict[str, Any]]]:
        resp = self.send(
            {
                "command": ["get_property",
                            "playlist"],
                "request_id": 100
            },
            silent=silent
        )
        if resp is None:
            return None
        if resp.get("error") == "success":
            data = resp.get("data", [])
            return data if isinstance(data, list) else []
        return []

    def get_now_playing(self) -> Optional[Dict[str, Any]]:
        if not self.is_running():
            return None

        playlist = self.get_playlist(silent=True) or []
        pos = self.get_property("playlist-pos", None)
        path = self.get_property("path", None)
        title = self.get_property("media-title", None)

        effective_path = _unwrap_memory_target(path) if isinstance(path, str) else path

        current_item: Optional[Dict[str, Any]] = None
        if isinstance(pos, int) and 0 <= pos < len(playlist):
            item = playlist[pos]
            current_item = item if isinstance(item, dict) else None
        else:
            for item in playlist:
                if isinstance(item, dict) and item.get("current") is True:
                    current_item = item
                    break

        return {
            "path": effective_path,
            "title": title,
            "playlist_pos": pos,
            "playlist_item": current_item,
        }

    def ensure_lua_loaded(self) -> None:
        try:
            script_path = self.lua_script_path
            if not script_path or not os.path.exists(script_path):
                return
            # Safe to call repeatedly; mpv will reload the script.
            self.send(
                {
                    "command": ["load-script",
                                script_path],
                    "request_id": 12
                },
                silent=True
            )
        except Exception:
            return

    def ensure_lyric_loader_running(self) -> None:
        """Start (or keep) the Python lyric overlay helper.

        Uses the fixed IPC pipe name so it can follow playback.
        """
        global _LYRIC_PROCESS, _LYRIC_LOG_FH

        # Cross-session guard (Windows): avoid spawning multiple helpers across separate CLI runs.
        # Also clean up stale helpers when mpv isn't running anymore.
        if platform.system() == "Windows":
            try:
                existing = _windows_list_lyric_helper_pids(str(self.ipc_path))
                if existing:
                    if not self.is_running():
                        _windows_kill_pids(existing)
                        return
                    # If multiple exist, kill them and start fresh (prevents double overlays).
                    if len(existing) == 1:
                        return
                    _windows_kill_pids(existing)
            except Exception:
                pass

        try:
            if _LYRIC_PROCESS is not None and _LYRIC_PROCESS.poll() is None:
                return
        except Exception:
            pass

        try:
            if _LYRIC_PROCESS is not None:
                try:
                    _LYRIC_PROCESS.terminate()
                except Exception:
                    pass
        finally:
            _LYRIC_PROCESS = None
            try:
                if _LYRIC_LOG_FH is not None:
                    _LYRIC_LOG_FH.close()
            except Exception:
                pass
            _LYRIC_LOG_FH = None

        try:
            try:
                tmp_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
            except Exception:
                tmp_dir = Path(".")

            # Ensure the module can be imported even when the app is launched from a different cwd.
            try:
                repo_root = _repo_root()
            except Exception:
                repo_root = Path.cwd()

            # Prefer a stable in-repo log so users can inspect it easily.
            log_path = None
            try:
                log_dir = (repo_root / "Log")
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = str((log_dir / "medeia-mpv-lyric.log").resolve())
            except Exception:
                log_path = None
            if not log_path:
                log_path = str((tmp_dir / "medeia-mpv-lyric.log").resolve())

            py = sys.executable
            if platform.system() == "Windows":
                py = _windows_pythonw_exe(py) or py

            cmd: List[str] = [
                py or "python",
                "-m",
                "plugins.mpv.lyric",
                "--ipc",
                str(self.ipc_path),
                "--log",
                log_path,
            ]

            # Redirect helper stdout/stderr to the log file so we can see crashes/import errors.
            try:
                _LYRIC_LOG_FH = open(log_path, "a", encoding="utf-8", errors="replace")
            except Exception:
                _LYRIC_LOG_FH = None

            kwargs: Dict[str,
                         Any] = {
                             "stdin": subprocess.DEVNULL,
                             "stdout": _LYRIC_LOG_FH or subprocess.DEVNULL,
                             "stderr": _LYRIC_LOG_FH or subprocess.DEVNULL,
                         }

            # Ensure immediate flushing to the log file.
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            try:
                existing_pp = env.get("PYTHONPATH")
                env["PYTHONPATH"] = (
                    str(repo_root) if not existing_pp else
                    (str(repo_root) + os.pathsep + str(existing_pp))
                )
            except Exception:
                pass
            kwargs["env"] = env

            # Make the current directory the repo root so `-m plugins.mpv.lyric` resolves reliably.
            kwargs["cwd"] = str(repo_root)
            if platform.system() == "Windows":
                # Ensure we don't flash a console window when spawning the helper.
                flags = 0
                try:
                    flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
                except Exception:
                    flags |= 0x00000008
                try:
                    flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                except Exception:
                    flags |= 0x08000000
                kwargs["creationflags"] = flags
                kwargs.update(
                    {
                        k: v
                        for k, v in _windows_hidden_subprocess_kwargs().items()
                        if k != "creationflags"
                    }
                )

            _LYRIC_PROCESS = subprocess.Popen(cmd, **kwargs)
            debug(f"Lyric loader started (log={log_path})")
        except Exception as exc:
            debug(f"Lyric loader failed to start: {exc}")

    def wait_for_ipc(self, retries: int = 20, delay_seconds: float = 0.2) -> bool:
        for _ in range(max(1, retries)):
            client = self.client(silent=True)
            try:
                if client.connect():
                    return True
            finally:
                client.disconnect()
            _time.sleep(delay_seconds)
        return False

    def kill_existing_windows(self) -> None:
        if platform.system() != "Windows":
            return
        try:
            subprocess.run(
                ["taskkill",
                 "/IM",
                 "mpv.exe",
                 "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                **_windows_hidden_subprocess_kwargs(),
            )
        except Exception:
            return

    def start(
        self,
        *,
        extra_args: Optional[List[str]] = None,
        ytdl_raw_options: Optional[str] = None,
        http_header_fields: Optional[str] = None,
        detached: bool = True,
    ) -> None:
        # uosc reads its config from "~~/script-opts/uosc.conf".
        # With --no-config, mpv makes ~~ expand to an empty string, so uosc can't load.
        # Instead, point mpv at a repo-controlled config directory.
        try:
            repo_root = _repo_root()
        except Exception:
            repo_root = Path.cwd()

        portable_config_dir = _package_root() / "portable_config"
        try:
            (portable_config_dir / "script-opts").mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Ensure uosc.conf is available at the location uosc expects.
        try:
            # Source uosc.conf is located within the bundled scripts.
            src_uosc_conf = portable_config_dir / "scripts" / "uosc" / "uosc.conf"
            dst_uosc_conf = portable_config_dir / "script-opts" / "uosc.conf"
            if src_uosc_conf.exists():
                # Only seed a default config if the user doesn't already have one.
                if not dst_uosc_conf.exists():
                    dst_uosc_conf.write_bytes(src_uosc_conf.read_bytes())
        except Exception:
            pass

        cmd: List[str] = [
            "mpv",
            f"--config-dir={str(portable_config_dir)}",
            "--load-scripts=yes",
            "--osc=no",
            "--ytdl=yes",
            f"--input-ipc-server={self.ipc_path}",
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=yes",
        ]

        # uosc and other scripts are expected to be auto-loaded from portable_config/scripts.
        # If --load-scripts=yes is set (standard), mpv will already pick up the loader shim
        # at scripts/uosc.lua. We only add a manual --script fallback if that file is missing.
        try:
            uosc_entry = portable_config_dir / "scripts" / "uosc.lua"
            if not uosc_entry.exists() and self.lua_script_path:
                lua_dir = Path(self.lua_script_path).resolve().parent
                # Check different possible source locations for uosc core.
                uosc_paths = [
                    portable_config_dir / "scripts" / "uosc" / "scripts" / "uosc" / "main.lua",
                    lua_dir / "uosc" / "scripts" / "uosc" / "main.lua"
                ]
                for p in uosc_paths:
                    if p.exists():
                        cmd.append(f"--script={str(p)}")
                        break
        except Exception:
            pass

        # Always load the bundled Lua script at startup.
        if self.lua_script_path and os.path.exists(self.lua_script_path):
            cmd.append(f"--script={self.lua_script_path}")

        if ytdl_raw_options:
            cmd.append(f"--ytdl-raw-options={ytdl_raw_options}")
        if http_header_fields:
            cmd.append(f"--http-header-fields={http_header_fields}")
        if extra_args:
            cmd.extend([str(a) for a in extra_args if a])

        kwargs: Dict[str,
                     Any] = {}
        if platform.system() == "Windows":
            # Ensure we don't flash a console window when spawning mpv.
            flags = 0
            try:
                flags |= int(
                    getattr(subprocess,
                            "DETACHED_PROCESS",
                            0x00000008)
                ) if detached else 0
            except Exception:
                flags |= 0x00000008 if detached else 0
            try:
                flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            except Exception:
                flags |= 0x08000000
            kwargs["creationflags"] = flags
            # startupinfo is harmless for GUI apps; helps hide flashes for console-subsystem builds.
            kwargs.update(
                {
                    k: v
                    for k, v in _windows_hidden_subprocess_kwargs().items()
                    if k != "creationflags"
                }
            )

        debug("Starting MPV")
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )


def get_ipc_pipe_path() -> str:
    """Get the fixed IPC pipe/socket path for persistent MPV connection.

    Uses a fixed name so all playback sessions connect to the same MPV
    window/process instead of creating new instances.

    Returns:
        Path to IPC pipe (Windows) or socket (Linux/macOS)
    """
    override = os.environ.get("MEDEIA_MPV_IPC") or os.environ.get("MPV_IPC_SERVER")
    if override:
        return str(override)

    system = platform.system()

    if system == "Windows":
        return f"\\\\.\\pipe\\{FIXED_IPC_PIPE_NAME}"
    elif system == "Darwin":  # macOS
        return f"/tmp/{FIXED_IPC_PIPE_NAME}.sock"
    else:  # Linux and others
        return f"/tmp/{FIXED_IPC_PIPE_NAME}.sock"


def _unwrap_memory_target(text: Optional[str]) -> Optional[str]:
    """Return the real target from a memory:// M3U payload if present."""
    if not isinstance(text, str) or not text.startswith("memory://"):
        return text
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("memory://"):
            continue
        return line
    return text


class MPVIPCClient:
    """Client for communicating with mpv via IPC socket/pipe.

    This is the unified interface for all Python code to communicate with mpv.
    It handles platform-specific differences (Windows named pipes vs Unix sockets).
    """

    def __init__(
        self,
        socket_path: Optional[str] = None,
        timeout: float = 5.0,
        silent: bool = False
    ):
        """Initialize MPV IPC client.

        Args:
            socket_path: Path to IPC socket/pipe. If None, uses the fixed persistent path.
            timeout: Socket timeout in seconds.
        """
        self.timeout = timeout
        self.socket_path = socket_path or get_ipc_pipe_path()
        self.sock: socket.socket | BinaryIO | None = None
        self.is_windows = platform.system() == "Windows"
        self.silent = bool(silent)
        self._recv_buffer: bytes = b""
        atexit.register(self.disconnect)

    def _write_payload(self, payload: str) -> None:
        if not self.sock:
            if not self.connect():
                raise MPVIPCError("Not connected")

        try:
            if self.is_windows:
                # BinaryIO pipe from open('\\\\.\\pipe\\...')
                pipe = cast(BinaryIO, self.sock)
                try:
                    pipe.write(payload.encode("utf-8"))
                    pipe.flush()
                except OSError as e:
                    # Windows Errno 22 (EINVAL) often means the pipe handle is now invalid/closed
                    if getattr(e, "errno", 0) == 22:
                        raise BrokenPipeError(str(e))
                    raise
            else:
                sock_obj = cast(socket.socket, self.sock)
                sock_obj.sendall(payload.encode("utf-8"))
        except (OSError, IOError, BrokenPipeError, ConnectionResetError) as exc:
            # Pipe became invalid (disconnected, corrupted, etc.).
            # Disconnect and attempt one reconnection.
            if not self.silent:
                debug(f"Pipe write failed: {exc}; attempting reconnect")
            self.disconnect()
            if self.connect():
                # Retry once after reconnect
                try:
                    if self.is_windows:
                        pipe = cast(BinaryIO, self.sock)
                        try:
                            pipe.write(payload.encode("utf-8"))
                            pipe.flush()
                        except OSError as e:
                            if getattr(e, "errno", 0) == 22:
                                raise BrokenPipeError(str(e))
                            raise
                    else:
                        sock_obj = cast(socket.socket, self.sock)
                        sock_obj.sendall(payload.encode("utf-8"))
                except (OSError, IOError, BrokenPipeError, ConnectionResetError) as retry_exc:
                    self.disconnect()
                    raise MPVIPCError(f"Pipe write failed after reconnect: {retry_exc}") from retry_exc
            else:
                raise MPVIPCError("Failed to reconnect after pipe write error") from exc

    def _readline(self, *, timeout: Optional[float] = None) -> Optional[bytes]:
        if not self.sock:
            if not self.connect():
                return None

        effective_timeout = self.timeout if timeout is None else float(timeout)
        deadline = _time.time() + max(0.0, effective_timeout)

        if self.is_windows:
            pipe = cast(BinaryIO, self.sock)
            while True:
                nl = self._recv_buffer.find(b"\n")
                if nl != -1:
                    line = self._recv_buffer[:nl + 1]
                    self._recv_buffer = self._recv_buffer[nl + 1:]
                    return line

                remaining = deadline - _time.time()
                if remaining <= 0:
                    return None

                try:
                    available = _windows_pipe_bytes_available(pipe)
                except Exception as exc:
                    if not self.silent:
                        debug(f"Pipe availability probe failed: {exc}")
                    self.disconnect()
                    return None

                if available is None:
                    self.disconnect()
                    return None

                if available <= 0:
                    _time.sleep(min(0.01, max(0.001, remaining)))
                    continue

                try:
                    chunk = pipe.read(min(available, 4096))
                except (OSError, IOError, BrokenPipeError, ConnectionResetError) as exc:
                    if not self.silent:
                        debug(f"Pipe readline failed: {exc}")
                    self.disconnect()
                    return None

                if not chunk:
                    return b""

                self._recv_buffer += chunk

        # Unix: buffer until newline.
        sock_obj = cast(socket.socket, self.sock)
        while True:
            nl = self._recv_buffer.find(b"\n")
            if nl != -1:
                line = self._recv_buffer[:nl + 1]
                self._recv_buffer = self._recv_buffer[nl + 1:]
                return line

            remaining = deadline - _time.time()
            if remaining <= 0:
                return None

            try:
                # Temporarily narrow timeout for this read.
                old_timeout = sock_obj.gettimeout()
                try:
                    sock_obj.settimeout(remaining)
                    chunk = sock_obj.recv(4096)
                finally:
                    sock_obj.settimeout(old_timeout)
            except socket.timeout:
                return None
            except Exception:
                return None

            if not chunk:
                # EOF
                return b""
            self._recv_buffer += chunk

    def read_message(self,
                     *,
                     timeout: Optional[float] = None) -> Optional[Dict[str,
                                                                       Any]]:
        """Read the next JSON message/event from MPV.

        Returns:
            - dict: parsed JSON message/event
            - {"event": "__eof__"} if the stream ended
            - None on timeout / no data
        """
        raw = self._readline(timeout=timeout)
        if raw is None:
            return None
        if raw == b"":
            return {
                "event": "__eof__"
            }
        try:
            return json.loads(raw.decode("utf-8", errors="replace").strip())
        except Exception:
            return None

    def send_command_no_wait(self,
                             command_data: Dict[str,
                                                Any] | List[Any]) -> Optional[int]:
        """Send a command to mpv without waiting for its response.

        This is important for long-running event loops (helpers) so we don't
        consume/lose async events (like property-change) while waiting.
        """
        try:
            request: Dict[str, Any]
            if isinstance(command_data, list):
                request = {
                    "command": command_data
                }
            else:
                request = dict(command_data)

            if "request_id" not in request:
                request["request_id"] = int(_time.time() * 1000) % 100000

            payload = json.dumps(request) + "\n"
            self._write_payload(payload)
            return int(request["request_id"])
        except Exception as exc:
            if not self.silent:
                debug(f"Error sending no-wait command to MPV: {exc}")
            try:
                self.disconnect()
            except Exception:
                pass
            return None

    def connect(self) -> bool:
        """Connect to mpv IPC socket.

        Returns:
            True if connection successful, False otherwise.
        """
        try:
            if self.is_windows:
                # Windows named pipes
                if not _windows_pipe_available(self.socket_path):
                    if not self.silent:
                        debug("Named pipe not available yet: %s" % self.socket_path)
                    return False

                try:
                    # Try to open the named pipe
                    self.sock = open(self.socket_path, "r+b", buffering=0)
                    self._recv_buffer = b""
                    return True
                except (OSError, IOError) as exc:
                    if not self.silent:
                        debug(f"Failed to connect to MPV named pipe: {exc}")
                    return False
            else:
                # Unix domain socket (Linux, macOS)
                if not os.path.exists(self.socket_path):
                    if not self.silent:
                        debug(f"IPC socket not found: {self.socket_path}")
                    return False

                af_unix = getattr(socket, "AF_UNIX", None)
                if af_unix is None:
                    if not self.silent:
                        debug("IPC AF_UNIX is not available on this platform")
                    return False

                self.sock = socket.socket(af_unix, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                self.sock.connect(self.socket_path)
                self._recv_buffer = b""
                return True
        except Exception as exc:
            if not self.silent:
                debug(f"Failed to connect to MPV IPC: {exc}")
            self.sock = None
            return False

    def send_command(self,
                     command_data: Dict[str,
                                        Any] | List[Any],
                     wait: bool = True) -> Optional[Dict[str,
                                                                           Any]]:
        """Send a command to mpv and get response.

        Args:
            command_data: Command dict (e.g. {"command": [...]}) or list (e.g. ["loadfile", ...])
            wait: If True, wait for the command response.

        Returns:
            Response dict with 'error' key (value 'success' on success), or None on error.
        """
        if not self.sock:
            if not self.connect():
                return None

        try:
            # Format command as JSON (mpv IPC protocol)
            request: Dict[str, Any]
            if isinstance(command_data, list):
                request = {
                    "command": command_data
                }
            else:
                request = command_data

            # Add request_id if not present to match response
            if "request_id" not in request:
                request["request_id"] = int(_time.time() * 1000) % 100000

            rid = request["request_id"]
            payload = json.dumps(request) + "\n"

            # Send command
            self._write_payload(payload)

            if not wait:
                return {"error": "success", "request_id": rid, "async": True}

            # Receive response
            # We need to read lines until we find the one with matching request_id
            # or until timeout/error. MPV might send events in between.
            start_time = _time.time()
            while _time.time() - start_time < self.timeout:
                response_data = self._readline(timeout=self.timeout)
                if response_data is None:
                    return None

                if not response_data:
                    break

                try:
                    lines = response_data.decode(
                        "utf-8",
                        errors="replace"
                    ).strip().split("\n")
                    for line in lines:
                        if not line:
                            continue
                        resp = json.loads(line)

                        # Check if this is the response to our request
                        if resp.get("request_id") == request.get("request_id"):
                            return resp

                        # Handle async log messages/events for visibility
                        event_type = resp.get("event")
                        if event_type == "log-message":
                            level = resp.get("level", "info")
                            prefix = resp.get("prefix", "")
                            text = resp.get("text", "").strip()
                            debug(f"[MPV {level}] {prefix} {text}".strip())
                        elif event_type:
                            debug(f"[MPV event] {event_type}: {resp}")
                        elif "error" in resp and "request_id" not in resp:
                            debug(f"[MPV error] {resp}")
                except json.JSONDecodeError:
                    pass

            return None
        except Exception as exc:
            debug(f"Error sending command to MPV: {exc}")
            self.disconnect()
            return None

    def disconnect(self) -> None:
        """Disconnect from mpv IPC socket."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self._recv_buffer = b""

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()
