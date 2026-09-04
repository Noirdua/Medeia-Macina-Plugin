from __future__ import annotations

import asyncio
import importlib.util
import inspect
import re
import shutil
import sys
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from PluginCore.base import Plugin, SearchResult
from SYS.logger import debug
from SYS.utils import coerce_bool, sanitize_filename, unique_path as _unique_path

_TELEGRAM_DEFAULT_TIMESTAMP_STEM_RE = re.compile(
    r"^(?P<prefix>photo|video|document|audio|voice|animation)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})(?: \(\d+\))?$",
    flags=re.IGNORECASE,
)


def _maybe_strip_telegram_timestamped_default_filename(
    *,
    downloaded_path: Path
) -> Path:
    """Normalize Telethon's default timestamped names.

    Examples:
    - photo_2025-12-27_02-58-09.jpg -> photo.jpg
    """
    try:
        stem = downloaded_path.stem
        suffix = downloaded_path.suffix
    except Exception:
        return downloaded_path

    if not suffix:
        return downloaded_path

    m = _TELEGRAM_DEFAULT_TIMESTAMP_STEM_RE.fullmatch(str(stem))
    if not m:
        return downloaded_path

    prefix = str(m.group("prefix") or "").strip().lower()
    if not prefix:
        return downloaded_path

    new_candidate = downloaded_path.with_name(f"{prefix}{suffix}")
    if new_candidate == downloaded_path:
        return downloaded_path

    new_path = _unique_path(new_candidate)
    try:
        if downloaded_path.exists():
            try:
                downloaded_path.rename(new_path)
                return new_path
            except Exception:
                shutil.move(str(downloaded_path), str(new_path))
                return new_path
    except Exception:
        return downloaded_path

    return downloaded_path


def _looks_like_telegram_message_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    host = (parsed.hostname or "").lower().strip()
    if host in {"t.me",
                "telegram.me"}:
        return True
    if host.endswith(".t.me"):
        return True
    return False


def _parse_telegram_message_url(url: str) -> Tuple[str, int]:
    """Parse a Telegram message URL into (entity, message_id).

    Supported:
    - https://t.me/<username>/<msg_id>
    - https://t.me/s/<username>/<msg_id>
    - https://t.me/c/<internal_channel_id>/<msg_id>
    """
    parsed = urlparse(str(url))
    path = (parsed.path or "").strip("/")
    if not path:
        raise ValueError(f"Invalid Telegram URL: {url}")

    parts = [p for p in path.split("/") if p]
    if not parts:
        raise ValueError(f"Invalid Telegram URL: {url}")

    # Strip preview prefix
    if parts and parts[0].lower() == "s":
        parts = parts[1:]

    if len(parts) < 2:
        raise ValueError(f"Invalid Telegram URL (expected /<chat>/<msg>): {url}")

    chat = parts[0]
    msg_raw = parts[1]

    # t.me/c/<id>/<msg>
    if chat.lower() == "c":
        if len(parts) < 3:
            raise ValueError(f"Invalid Telegram /c/ URL: {url}")
        chat = f"c:{parts[1]}"
        msg_raw = parts[2]

    m = re.fullmatch(r"\d+", str(msg_raw).strip())
    if not m:
        raise ValueError(f"Invalid Telegram message id in URL: {url}")

    return str(chat), int(msg_raw)


class Telegram(Plugin):
    """Telegram provider using Telethon.

    Config:
    [provider=telegram]
    app_id=
    api_hash=
    bot_token=
    """
    URL = ("t.me", "telegram.me")
    SUPPORTED_CMDLETS = frozenset({"download-file"})

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "app_id",
                "label": "API ID (from my.telegram.org)",
                "default": "",
                "required": True
            },
            {
                "key": "api_hash",
                "label": "API Hash",
                "default": "",
                "required": True,
                "secret": True
            },
            {
                "key": "bot_token",
                "label": "Bot Token (optional)",
                "default": "",
                "secret": True
            },
            {
                "key": "part_size_kb",
                "label": "Transfer chunk size KB (4-512)",
                "default": "512",
            },
            {
                "key": "connection_mode",
                "label": "Connection mode (abridged|full)",
                "default": "abridged",
            },
            {
                "key": "receive_updates",
                "label": "Receive updates during transfers",
                "default": False,
            }
        ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        telegram_conf = (
            self.config.get("plugin",
                            {}).get("telegram",
                                    {}) if isinstance(self.config,
                                                      dict) else {}
        )
        self._app_id = telegram_conf.get("app_id")
        self._api_hash = telegram_conf.get("api_hash")
        self._bot_token = telegram_conf.get("bot_token")
        self._last_login_error: Optional[str] = None
        # Telethon transfers are chunked; larger parts mean fewer round-trips.
        # Telethon typically expects 4..512 KB and divisible by 4.
        self._part_size_kb = telegram_conf.get("part_size_kb")
        if self._part_size_kb is None:
            self._part_size_kb = telegram_conf.get("chunk_kb")
        if self._part_size_kb is None:
            self._part_size_kb = telegram_conf.get("download_part_kb")
        self._connection_mode = str(
            telegram_conf.get("connection_mode") or telegram_conf.get("connection")
            or "abridged"
        ).strip().lower()
        self._receive_updates = coerce_bool(
            telegram_conf.get("receive_updates"), default=False
        )
        self._cryptg_available = self._detect_cryptg_available()
        self._emitted_cryptg_hint = False
        self._download_media_accepts_part_size = self._detect_download_media_accepts_part_size()

    @staticmethod
    def _detect_cryptg_available() -> bool:
        try:
            return importlib.util.find_spec("cryptg") is not None
        except Exception:
            return False

    @staticmethod
    def _detect_download_media_accepts_part_size() -> bool:
        try:
            from telethon import TelegramClient

            sig = inspect.signature(TelegramClient.download_media)
            params = sig.parameters
            if "part_size_kb" in params:
                return True
            return any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in params.values()
            )
        except Exception:
            return False

    def _emit_cryptg_speed_hint_once(self) -> None:
        if self._cryptg_available or self._emitted_cryptg_hint:
            return
        self._emitted_cryptg_hint = True
        try:
            sys.stderr.write(
                "[telegram] Tip: install 'cryptg' for faster Telegram media transfer performance.\n"
            )
            sys.stderr.flush()
        except Exception:
            pass

    def _new_client(
        self,
        *,
        session_base: Path,
        app_id: int,
        api_hash: str,
        receive_updates: Optional[bool] = None,
    ):
        from telethon import TelegramClient

        kwargs: Dict[str, Any] = {
            "receive_updates": bool(
                self._receive_updates
                if receive_updates is None else receive_updates
            )
        }
        mode = str(self._connection_mode or "").strip().lower()
        try:
            if mode in {"abridged", "tcpabridged", "fast"}:
                from telethon.network.connection.tcpabridged import ConnectionTcpAbridged

                kwargs["connection"] = ConnectionTcpAbridged
            elif mode in {"full", "tcpfull"}:
                from telethon.network.connection.tcpfull import ConnectionTcpFull

                kwargs["connection"] = ConnectionTcpFull
        except Exception:
            pass

        try:
            return TelegramClient(str(session_base), app_id, api_hash, **kwargs)
        except TypeError:
            return TelegramClient(str(session_base), app_id, api_hash)

    def _has_running_event_loop(self) -> bool:
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False
        except Exception:
            return False

    def _run_async_blocking(self, coro):
        """Run an awaitable to completion using a fresh event loop.

        If an event loop is already running in this thread (common in REPL),
        runs the coroutine in a worker thread with its own loop.
        """
        result: Dict[str,
                     Any] = {}
        err: Dict[str,
                  Any] = {}

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                result["value"] = loop.run_until_complete(coro)
            except BaseException as exc:
                # Ensure we don't leave Telethon tasks pending when the user hits Ctrl+C.
                err["error"] = exc
                try:
                    try:
                        pending = asyncio.all_tasks(loop)  # py3.8+
                    except TypeError:
                        pending = asyncio.all_tasks()  # type: ignore
                    pending = [t for t in pending if t is not None and not t.done()]
                    for t in pending:
                        try:
                            t.cancel()
                        except Exception:
                            pass
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending,
                                           return_exceptions=True)
                        )
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        pass
                except Exception:
                    pass
            finally:
                try:
                    try:
                        loop.run_until_complete(loop.shutdown_default_executor())
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    loop.close()
                except Exception:
                    pass

        if self._has_running_event_loop():
            th = threading.Thread(target=_runner, daemon=True)
            th.start()
            th.join()
        else:
            _runner()

        if "error" in err:
            raise err["error"]
        return result.get("value")

    def _stdin_is_interactive(self) -> bool:
        """Best-effort check for whether we can safely prompt the user.

        Some environments (e.g. prompt_toolkit) may wrap `sys.stdin` such that
        `sys.stdin.isatty()` is False even though interactive prompting works.
        """
        try:
            streams = [sys.stdin, getattr(sys, "__stdin__", None)]
            for stream in streams:
                if stream is None:
                    continue
                isatty = getattr(stream, "isatty", None)
                if callable(isatty) and bool(isatty()):
                    return True
        except Exception:
            return False
        return False

    def _legacy_session_base_path(self) -> Path:
        # Older versions stored sessions under Log/medeia_macina.
        root = Path(__file__).resolve().parents[1]
        return root / "Log" / "medeia_macina" / "telegram"

    def _migrate_legacy_session_if_needed(self) -> None:
        """If a legacy Telethon session exists, copy it to the new root location."""
        try:
            new_base = self._session_base_path()
            new_session = Path(str(new_base) + ".session")
            if new_session.is_file():
                return

            legacy_base = self._legacy_session_base_path()
            legacy_session = Path(str(legacy_base) + ".session")
            if not legacy_session.is_file():
                return

            for suffix in (".session",
                           ".session-journal",
                           ".session-wal",
                           ".session-shm"):
                src = Path(str(legacy_base) + suffix)
                dst = Path(str(new_base) + suffix)
                try:
                    if src.is_file() and not dst.exists():
                        shutil.copy2(str(src), str(dst))
                except Exception:
                    continue
        except Exception:
            return

    def _session_file_path(self) -> Path:
        self._migrate_legacy_session_if_needed()
        base = self._session_base_path()
        return Path(str(base) + ".session")

    def _has_session(self) -> bool:
        self._migrate_legacy_session_if_needed()
        try:
            return self._session_file_path().is_file()
        except Exception:
            return False

    def _session_is_authorized(self) -> bool:
        """Return True if the current session file represents an authorized login.

        This must never prompt.
        """
        self._migrate_legacy_session_if_needed()
        if not self._has_session():
            return False
        try:
            from telethon import TelegramClient
        except Exception:
            return False
        try:
            app_id, api_hash = self._credentials()
        except Exception:
            return False

        session_base = self._session_base_path()

        async def _check_async() -> bool:
            client = self._new_client(
                session_base=session_base,
                app_id=app_id,
                api_hash=api_hash,
            )
            try:
                await client.connect()
                return bool(await client.is_user_authorized())
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        try:
            return bool(self._run_async_blocking(_check_async()))
        except Exception:
            return False

    def _ensure_session_interactive(self) -> bool:
        """Best-effort interactive auth to create a Telethon session file.

        Returns True if a session exists and is authorized after the attempt.
        """
        self._last_login_error = None
        if self._session_is_authorized():
            return True

        # Never prompt in non-interactive contexts.
        if not self._stdin_is_interactive():
            self._last_login_error = "stdin is not interactive"
            return False

        try:
            from telethon import TelegramClient
        except Exception as exc:
            self._last_login_error = f"Telethon not available: {exc}"
            return False

        try:
            app_id, api_hash = self._credentials()
        except Exception:
            return False

        try:
            sys.stderr.write("[telegram] No session found; login required.\n")
            sys.stderr.write("[telegram] Choose login method: 1) phone  2) bot token\n")
            sys.stderr.write("[telegram] Enter 1 or 2: ")
            sys.stderr.flush()
            choice = ""
            try:
                choice = str(input()).strip().lower()
            except EOFError:
                choice = ""

            use_bot = choice in {"2",
                                 "b",
                                 "bot",
                                 "token"}
            bot_token = ""
            if use_bot:
                sys.stderr.write("[telegram] Bot token: ")
                sys.stderr.flush()
                try:
                    bot_token = str(input()).strip()
                except EOFError:
                    bot_token = ""
                if not bot_token:
                    self._last_login_error = "bot token was empty"
                    return False
                self._bot_token = bot_token
            else:
                sys.stderr.write(
                    "[telegram] Phone login selected (Telethon will prompt for phone + code).\n"
                )
                sys.stderr.flush()

            session_base = self._session_base_path()

            async def _auth_async() -> None:
                client = self._new_client(
                    session_base=session_base,
                    app_id=app_id,
                    api_hash=api_hash,
                    receive_updates=True,
                )
                try:
                    if use_bot:
                        await client.start(bot_token=bot_token)
                    else:
                        await client.start()
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

            def _run_in_new_loop() -> None:
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(_auth_async())
                finally:
                    try:
                        loop.run_until_complete(loop.shutdown_default_executor())
                    except Exception:
                        pass
                    try:
                        loop.close()
                    except Exception:
                        pass

            # If some framework is already running an event loop in this thread,
            # do the auth flow in a worker thread with its own loop.
            try:
                self._ensure_event_loop()
                main_loop = asyncio.get_event_loop()
                loop_running = bool(getattr(main_loop, "is_running", lambda: False)())
            except Exception:
                loop_running = False

            if loop_running:
                err: list[str] = []

                def _worker() -> None:
                    try:
                        _run_in_new_loop()
                    except Exception as exc:
                        err.append(str(exc))

                th = threading.Thread(target=_worker, daemon=True)
                th.start()
                th.join()
                if err:
                    self._last_login_error = err[0]
                    return False
            else:
                try:
                    _run_in_new_loop()
                except Exception as exc:
                    self._last_login_error = str(exc)
                    return False
        finally:
            try:
                sys.stderr.write("\n")
                sys.stderr.flush()
            except Exception:
                pass

        ok = self._has_session()
        if not ok:
            if not self._last_login_error:
                self._last_login_error = "session was not created"
            return False

        if not self._session_is_authorized():
            if not self._last_login_error:
                self._last_login_error = "session exists but is not authorized"
            return False
        return True

    def _ensure_session_with_bot_token(self, bot_token: str) -> bool:
        """Create a Telethon session using a bot token without prompting.

        Returns True if a session exists and is authorized after the attempt.
        """
        self._last_login_error = None
        if self._session_is_authorized():
            return True
        bot_token = str(bot_token or "").strip()
        if not bot_token:
            return False
        try:
            from telethon import TelegramClient
        except Exception as exc:
            self._last_login_error = f"Telethon not available: {exc}"
            return False
        try:
            app_id, api_hash = self._credentials()
        except Exception as exc:
            self._last_login_error = str(exc)
            return False

        session_base = self._session_base_path()

        async def _auth_async() -> None:
            client = self._new_client(
                session_base=session_base,
                app_id=app_id,
                api_hash=api_hash,
                receive_updates=True,
            )
            try:
                await client.start(bot_token=bot_token)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        try:
            self._run_async_blocking(_auth_async())
        except Exception as exc:
            self._last_login_error = str(exc)
            return False

        if not self._has_session():
            self._last_login_error = "bot login did not create a session"
            return False
        if not self._session_is_authorized():
            self._last_login_error = "bot session exists but is not authorized"
            return False
        return True

    def _resolve_part_size_kb(self, file_size: Optional[int]) -> int:
        # Default: bias to max throughput.
        val = self._part_size_kb
        try:
            if val not in (None, ""):
                ps = int(str(val).strip())
            else:
                ps = 512
        except Exception:
            ps = 512

        # Clamp to Telethon-safe range.
        if ps < 4:
            ps = 4
        if ps > 512:
            ps = 512
        # Must be divisible by 4.
        ps = int(ps / 4) * 4
        if ps <= 0:
            ps = 64

        # For very small files, reduce overhead a bit (still divisible by 4).
        try:
            if file_size is not None and int(file_size) > 0:
                if int(file_size) < 2 * 1024 * 1024:
                    ps = min(ps, 256)
                elif int(file_size) < 10 * 1024 * 1024:
                    ps = min(ps, 512)
        except Exception:
            pass
        return ps

    def validate(self) -> bool:
        """Return True when Telegram can be used in the current context.

        Important behavior: `validate()` must be side-effect free (no prompts).
        Session creation happens on first use.
        """
        try:
            __import__("telethon")
        except Exception:
            return False

        try:
            app_id = int(self._app_id) if self._app_id not in (None, "") else None
        except Exception:
            app_id = None
        api_hash = str(self._api_hash
                       ).strip() if self._api_hash not in (None,
                                                           "") else ""
        if not bool(app_id and api_hash):
            return False

        # Consider the provider "available" when configured.
        # Authentication/session creation is handled on first use.
        return True

    def ensure_session(self, *, prompt: bool = False) -> bool:
        """Ensure a Telethon session exists.

        - If an authorized session already exists: returns True.
        - If a bot token is configured: tries to create a session without prompting.
        - If `prompt=True`: attempts interactive login.
        """
        # Treat "session exists" as insufficient; we need authorization.
        if self._session_is_authorized():
            return True
        bot_token = str(self._bot_token or "").strip()
        if bot_token:
            return bool(
                self._ensure_session_with_bot_token(bot_token)
                and self._session_is_authorized()
            )
        if prompt:
            return bool(
                self._ensure_session_interactive() and self._session_is_authorized()
            )
        return False

    def list_chats(self, *, limit: int = 200) -> list[Dict[str, Any]]:
        """List dialogs/chats available to the authenticated account.

        Returns a list of dicts with keys: id, title, username, type.
        """
        # Do not prompt implicitly.
        if not self.ensure_session(prompt=False):
            return []

        try:
            from telethon import TelegramClient
            from telethon.tl.types import Channel, Chat, User
        except Exception:
            return []

        try:
            app_id, api_hash = self._credentials()
        except Exception:
            return []

        session_base = self._session_base_path()

        async def _list_async() -> list[Dict[str, Any]]:
            client = self._new_client(
                session_base=session_base,
                app_id=app_id,
                api_hash=api_hash,
            )
            rows: list[Dict[str, Any]] = []
            try:
                await client.connect()
                if not bool(await client.is_user_authorized()):
                    return []

                try:
                    dialogs = await client.get_dialogs(limit=int(limit))
                except TypeError:
                    dialogs = await client.get_dialogs()

                for d in dialogs or []:
                    entity = getattr(d, "entity", None)
                    title = ""
                    username = ""
                    chat_id = None
                    kind = ""
                    try:
                        title = str(getattr(d, "name", "") or "").strip()
                    except Exception:
                        title = ""

                    try:
                        if entity is not None:
                            maybe_id = getattr(entity, "id", None)
                            if maybe_id is not None:
                                chat_id = int(maybe_id)
                            maybe_username = getattr(entity, "username", None)
                            if isinstance(maybe_username, str):
                                username = maybe_username.strip()
                    except Exception:
                        pass

                    try:
                        if not title and entity is not None:
                            for attr in ("title", "first_name", "last_name"):
                                v = getattr(entity, attr, None)
                                if isinstance(v, str) and v.strip():
                                    title = v.strip()
                                    break
                    except Exception:
                        pass

                    try:
                        if isinstance(entity, Channel):
                            if bool(getattr(entity, "broadcast", False)):
                                kind = "channel"
                            elif bool(getattr(entity, "megagroup", False)):
                                kind = "group"
                            else:
                                kind = "channel"
                        elif isinstance(entity, Chat):
                            kind = "group"
                        elif isinstance(entity, User):
                            kind = "user"
                        else:
                            kind = (
                                type(entity).__name__.lower()
                                if entity is not None else "unknown"
                            )
                    except Exception:
                        kind = "unknown"

                    rows.append(
                        {
                            "id": chat_id,
                            "title": title,
                            "username": username,
                            "type": kind
                        }
                    )
                return rows
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        try:
            rows = self._run_async_blocking(_list_async())
        except Exception:
            rows = []

        # Sort for stable display.
        try:
            rows.sort(
                key=lambda r: (str(r.get("type") or ""), str(r.get("title") or ""))
            )
        except Exception:
            pass
        return rows

    def send_files_to_chats(
        self,
        *,
        chat_ids: Sequence[int],
        usernames: Sequence[str],
        files: Optional[Sequence[Dict[str,
                                      Any]]] = None,
        file_paths: Optional[Sequence[str]] = None,
    ) -> None:
        """Send local file(s) to one or more chats.

        This must never prompt. Requires an authorized session (run: .telegram -login).
        Uses Rich ProgressBar for upload progress.
        """
        # Never prompt implicitly.
        if not self.ensure_session(prompt=False):
            raise Exception("Telegram login required. Run: .telegram -login")

        try:
            from telethon import TelegramClient
            from telethon.tl.types import DocumentAttributeFilename
        except Exception as exc:
            raise Exception(f"Telethon not available: {exc}")

        try:
            from SYS.progress import print_progress, print_final_progress
        except Exception:
            print_progress = None  # type: ignore
            print_final_progress = None  # type: ignore

        try:
            app_id, api_hash = self._credentials()
        except Exception as exc:
            raise Exception(str(exc))

        # Back-compat: allow callers to pass `file_paths=`.
        if files is None:
            files = [{
                "path": str(p),
                "title": ""
            } for p in (file_paths or [])]

        def _sanitize_filename(text: str) -> str:
            return sanitize_filename(text, max_len=120, fallback="file")

        # Normalize and validate file paths + titles.
        jobs: list[Dict[str, Any]] = []
        seen_paths: set[str] = set()
        for f in files or []:
            try:
                path_text = str((f or {}).get("path") or "").strip()
            except Exception:
                path_text = ""
            if not path_text:
                continue
            path_obj = Path(path_text).expanduser()
            if not path_obj.exists():
                raise Exception(f"File not found: {path_obj}")
            key = str(path_obj).lower()
            if key in seen_paths:
                continue
            seen_paths.add(key)
            title_text = ""
            try:
                title_text = str((f or {}).get("title") or "").strip()
            except Exception:
                title_text = ""
            jobs.append({
                "path": str(path_obj),
                "title": title_text
            })

        if not jobs:
            raise Exception("No files to send")

        session_base = self._session_base_path()
        ids = [int(x) for x in (chat_ids or []) if x is not None]
        try:
            ids = list(dict.fromkeys(ids))
        except Exception:
            pass
        uns = [str(u or "").strip() for u in (usernames or []) if str(u or "").strip()]
        try:
            uns = list(dict.fromkeys([u.strip().lower() for u in uns if u.strip()]))
        except Exception:
            pass
        # Prefer IDs when available; avoid sending twice when both id and username exist.
        if ids:
            uns = []
        if not ids and not uns:
            raise Exception("No chat selected")

        async def _send_async() -> None:
            client = self._new_client(
                session_base=session_base,
                app_id=app_id,
                api_hash=api_hash,
            )
            try:
                await client.connect()
                if not bool(await client.is_user_authorized()):
                    raise Exception(
                        "Telegram session is not authorized. Run: .telegram -login"
                    )
                self._emit_cryptg_speed_hint_once()

                # Resolve entities: prefer IDs. Only fall back to usernames when IDs are absent.
                entities: list[Any] = []
                if ids:
                    for cid in ids:
                        try:
                            e = await client.get_input_entity(int(cid))
                            entities.append(e)
                        except Exception:
                            continue
                else:
                    seen_u: set[str] = set()
                    for u in uns:
                        key = str(u).strip().lower()
                        if not key or key in seen_u:
                            continue
                        seen_u.add(key)
                        try:
                            e = await client.get_input_entity(str(u))
                            entities.append(e)
                        except Exception:
                            continue

                if not entities:
                    raise Exception("Unable to resolve selected chat(s)")

                for entity in entities:
                    for job in jobs:
                        try:
                            p = str(job.get("path") or "").strip()
                            if not p:
                                continue
                            path_obj = Path(p)
                            file_size = None
                            try:
                                file_size = int(path_obj.stat().st_size)
                            except Exception:
                                file_size = None
                            ps = self._resolve_part_size_kb(file_size)

                            title_raw = str(job.get("title") or "").strip()
                            fallback = path_obj.stem
                            base = (
                                _sanitize_filename(title_raw)
                                if title_raw else _sanitize_filename(fallback)
                            )
                            ext = path_obj.suffix
                            send_name = f"{base}{ext}" if ext else base

                            attributes = [DocumentAttributeFilename(send_name)]

                            def _progress(sent: int, total: int) -> None:
                                if print_progress is None:
                                    return
                                try:
                                    print_progress(
                                        send_name,
                                        int(sent or 0),
                                        int(total or 0)
                                    )
                                except Exception:
                                    return

                            # Start the progress UI immediately (even if Telethon delays the first callback).
                            if print_progress is not None:
                                try:
                                    print_progress(send_name, 0, int(file_size or 0))
                                except Exception:
                                    pass

                            try:
                                await client.send_file(
                                    entity,
                                    str(path_obj),
                                    part_size_kb=ps,
                                    progress_callback=_progress,
                                    attributes=attributes,
                                )
                            finally:
                                if print_final_progress is not None:
                                    try:
                                        print_final_progress(
                                            send_name,
                                            int(file_size or 0),
                                            0.0
                                        )
                                    except Exception:
                                        pass
                        except Exception as exc:
                            raise Exception(str(exc))
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        self._run_async_blocking(_send_async())

    def _session_base_path(self) -> Path:
        # Store session alongside cookies.txt at repo root.
        # Telethon uses this as base name and writes "<base>.session".
        root = Path(__file__).resolve().parents[1]
        return root / "telegram"

    def _credentials(self) -> Tuple[int, str]:
        raw_app_id = self._app_id
        if raw_app_id in (None, ""):
            raise Exception("Telegram app_id missing")
        try:
            app_id = int(str(raw_app_id).strip())
        except Exception:
            raise Exception("Telegram app_id invalid")
        api_hash = str(self._api_hash or "").strip()
        if not api_hash:
            raise Exception("Telegram api_hash missing")
        return app_id, api_hash

    def _ensure_event_loop(self) -> None:
        """Telethon sync wrapper requires an event loop to exist in this thread."""
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

    def _download_message_media_sync(self,
                                     *,
                                     url: str,
                                     output_dir: Path) -> Tuple[Path,
                                                                Dict[str,
                                                                     Any]]:
        # Ensure we have an authorized session before attempting API calls.
        # Never prompt during downloads.
        if not self.ensure_session(prompt=False):
            raise Exception("Telegram login required. Run: .telegram -login")

        try:
            from telethon import TelegramClient, errors
            from telethon.tl.types import PeerChannel
        except Exception as exc:
            raise Exception(f"Telethon not available: {exc}")

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        app_id, api_hash = self._credentials()
        session_base = self._session_base_path()
        chat, message_id = _parse_telegram_message_url(url)

        async def _download_async() -> Tuple[Path, Dict[str, Any]]:
            client = self._new_client(
                session_base=session_base,
                app_id=app_id,
                api_hash=api_hash,
            )
            try:
                await client.connect()
                if not bool(await client.is_user_authorized()):
                    raise Exception(
                        "Telegram session is not authorized. Run: .telegram -login"
                    )

                if chat.startswith("c:"):
                    channel_id = int(chat.split(":", 1)[1])
                    entity = PeerChannel(channel_id)
                else:
                    entity = chat
                    if isinstance(entity,
                                  str) and entity and not entity.startswith("@"):
                        entity = "@" + entity

                messages = await client.get_messages(entity, ids=[message_id])
                message = None
                if isinstance(messages, (list, tuple)):
                    message = messages[0] if messages else None
                else:
                    try:
                        message = messages[0]  # type: ignore[index]
                    except Exception:
                        message = None
                if not message:
                    raise Exception("Telegram message not found")
                if not getattr(message, "media", None):
                    raise Exception("Telegram message has no media")

                chat_title = ""
                chat_username = ""
                chat_id = None
                try:
                    chat_obj = getattr(message, "chat", None)
                    if chat_obj is not None:
                        maybe_title = getattr(chat_obj, "title", None)
                        maybe_username = getattr(chat_obj, "username", None)
                        maybe_id = getattr(chat_obj, "id", None)
                        if isinstance(maybe_title, str):
                            chat_title = maybe_title.strip()
                        if isinstance(maybe_username, str):
                            chat_username = maybe_username.strip()
                        if maybe_id is not None:
                            chat_id = int(maybe_id)
                except Exception:
                    pass

                caption = ""
                try:
                    maybe_caption = getattr(message, "message", None)
                    if isinstance(maybe_caption, str):
                        caption = maybe_caption.strip()
                except Exception:
                    pass

                msg_id = None
                msg_date = None
                try:
                    msg_id = int(getattr(message, "id", 0) or 0)
                except Exception:
                    msg_id = None
                try:
                    msg_date = getattr(message, "date", None)
                except Exception:
                    msg_date = None

                file_name = ""
                file_mime = ""
                file_size = None
                try:
                    file_obj = getattr(message, "file", None)
                    maybe_name = getattr(file_obj, "name", None)
                    maybe_mime = getattr(file_obj, "mime_type", None)
                    maybe_size = getattr(file_obj, "size", None)
                    if isinstance(maybe_name, str):
                        file_name = maybe_name.strip()
                    if isinstance(maybe_mime, str):
                        file_mime = maybe_mime.strip()
                    if maybe_size is not None:
                        file_size = int(maybe_size)
                except Exception:
                    pass

                from SYS.models import ProgressBar

                progress_bar = ProgressBar()
                last_print = {
                    "t": 0.0
                }

                def _progress(current: int, total: int) -> None:
                    now = time.monotonic()
                    if now - float(last_print.get("t", 0.0)) < 0.25 and current < total:
                        return
                    last_print["t"] = now
                    progress_bar.update(
                        downloaded=int(current),
                        total=int(total),
                        label="telegram",
                        file=sys.stderr
                    )

                part_kb = self._resolve_part_size_kb(file_size)
                self._emit_cryptg_speed_hint_once()
                download_kwargs: Dict[str, Any] = {
                    "file": str(output_dir),
                    "progress_callback": _progress,
                }
                if self._download_media_accepts_part_size:
                    download_kwargs["part_size_kb"] = part_kb
                try:
                    downloaded = await client.download_media(message, **download_kwargs)
                except TypeError:
                    downloaded = await client.download_media(
                        message,
                        file=str(output_dir),
                        progress_callback=_progress,
                    )
                progress_bar.finish()
                if not downloaded:
                    raise Exception("Telegram download returned no file")
                downloaded_path = Path(str(downloaded))

                # Telethon's default media filenames include timestamps (e.g. photo_YYYY-MM-DD_HH-MM-SS.jpg).
                # Strip those timestamps ONLY when Telegram didn't provide an explicit filename.
                if not file_name:
                    downloaded_path = _maybe_strip_telegram_timestamped_default_filename(
                        downloaded_path=downloaded_path,
                    )

                date_iso = None
                try:
                    if msg_date is not None and hasattr(msg_date, "isoformat"):
                        date_iso = msg_date.isoformat()  # type: ignore[union-attr]
                except Exception:
                    date_iso = None

                info: Dict[str,
                           Any] = {
                               "plugin": "telegram",
                               "source_url": url,
                               "chat": {
                                   "key": chat,
                                   "title": chat_title,
                                   "username": chat_username,
                                   "id": chat_id,
                               },
                               "message": {
                                   "id": msg_id,
                                   "date": date_iso,
                                   "caption": caption,
                               },
                               "file": {
                                   "name": file_name,
                                   "mime_type": file_mime,
                                   "size": file_size,
                                   "downloaded_path": str(downloaded_path),
                               },
                           }
                return downloaded_path, info
            except errors.RPCError as exc:
                raise Exception(f"Telegram RPC error: {exc}")
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        return self._run_async_blocking(_download_async())

    def download_url(self, url: str, output_dir: Path) -> Tuple[Path, Dict[str, Any]]:
        """Download a Telegram message URL and return (path, metadata)."""
        if not _looks_like_telegram_message_url(url):
            raise ValueError("Not a Telegram URL")
        return self._download_message_media_sync(url=url, output_dir=output_dir)

    def handle_url(self, url: str, *, output_dir: Optional[Path] = None) -> Tuple[bool, Optional[Path | Dict[str, Any]]]:
        """Optional provider override to parse and act on URLs."""
        if not _looks_like_telegram_message_url(url):
            return False, None
        
        try:
            path, _info = self.download_url(url, output_dir or Path("."))
            return True, path
        except Exception as e:
            debug(f"[telegram] handle_url failed for {url}: {e}")
            return False, None

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        url = str(getattr(result, "path", "") or "")
        if not url:
            return None
        if not _looks_like_telegram_message_url(url):
            return None

        path, _info = self._download_message_media_sync(url=url, output_dir=output_dir)
        return path
