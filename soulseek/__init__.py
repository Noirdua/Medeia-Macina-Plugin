from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, List, Optional, TypeVar

from PluginCore.base import Plugin, SearchResult
from SYS.logger import log, debug, debug_panel, status_panel
from SYS.models import ProgressBar

_SOULSEEK_NOISE_SUBSTRINGS = (
    "unhandled exception on loop",
    "Task exception was never retrieved",
    "future: <Task finished",
    "ConnectionFailedError",
    "PeerConnectionError",
    "indirect connection failed",
    "indirect connection timed out",
    "failed to connect",
    "search reply ticket does not match any search request",
    "failed to receive transfer ticket on file connection",
    "aioslsk.exceptions.ConnectionReadError",
    "proactor_events.py",
    "self._sockets is not None",
)

_AsyncResult = TypeVar("_AsyncResult")

_DELIMITERS_RE = re.compile(r"[;,]")
_SEGMENT_BOUNDARY_RE = re.compile(r"(?=\b\w+\s*:)")


def _run_async_from_sync(
    coroutine_factory: Callable[[], Coroutine[Any, Any, _AsyncResult]],
) -> _AsyncResult:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine_factory())

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="soulseek-async") as executor:
        return executor.submit(lambda: asyncio.run(coroutine_factory())).result()


@contextlib.asynccontextmanager
async def _suppress_aioslsk_asyncio_task_noise() -> AsyncGenerator[None, None]:
    """Suppress non-fatal aioslsk task exceptions emitted via asyncio's loop handler.

    aioslsk may spawn background tasks (e.g. direct peer connection attempts) that
    can fail with ConnectionFailedError. These are often expected and should not
    end a successful download with a scary "Task exception was never retrieved"
    traceback.

    We only swallow those specific cases and delegate everything else to the
    previous/default handler.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Not in an event loop.
        yield
        return

    previous_handler = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
        try:
            exc = context.get("exception")
            msg = str(context.get("message") or "")

            if exc is not None:
                # Suppress internal asyncio AssertionError on Windows teardown (Proactor loop)
                if isinstance(exc, AssertionError):
                    m_lower = msg.lower()
                    if "proactor" in m_lower or "_start_serving" in m_lower or "self._sockets is not None" in str(exc):
                        return
                    # Bare `assert ...` (empty message) from loop teardown.
                    if not str(exc).strip():
                        return

                # Only suppress un-retrieved task exceptions from aioslsk connection failures.
                if msg == "Task exception was never retrieved":
                    cls = getattr(exc, "__class__", None)
                    name = getattr(cls, "__name__", "")
                    exc_text = str(exc or "").lower()

                    # Suppress expected peer direct-connect failures from aioslsk.
                    if name == "ConnectionFailedError" or "failed to connect" in exc_text:
                        return
        except Exception:
            # If our filter logic fails, fall through to default handling.
            import logging as _logging
            _logging.getLogger(__name__).debug("soulseek exception filter failed for context: %s", context, exc_info=True)
            pass

        if previous_handler is not None:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    try:
        yield
    finally:
        try:
            loop.set_exception_handler(previous_handler)
        except Exception:
            pass


def _configure_aioslsk_logging() -> None:
    """Reduce aioslsk internal log noise.

    Some aioslsk components emit non-fatal warnings/errors during high churn
    (search + download + disconnect). We keep our own debug output, but push
    aioslsk to ERROR and stop propagation so it doesn't spam the CLI.
    """
    for name in (
            "aioslsk",
            "aioslsk.network",
            "aioslsk.search",
            "aioslsk.transfer",
            "aioslsk.transfer.manager",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.ERROR)
        logger.propagate = False


class _LineFilterStream(io.TextIOBase):
    """A minimal stream wrapper that filters known noisy lines.

    It also suppresses entire traceback blocks when they contain known non-fatal
    aioslsk noise (e.g. ConnectionReadError during peer init).
    """

    def __init__(self, underlying: Any, suppress_substrings: tuple[str, ...]):
        super().__init__()
        self._underlying = underlying
        self._suppress = suppress_substrings
        self._buf = ""
        self._in_tb = False
        self._tb_lines: list[str] = []
        self._tb_suppress = False
        self._in_task_block = False
        self._task_lines: list[str] = []
        self._task_suppress = False

    def writable(self) -> bool:  # pragma: no cover
        return True

    def _should_suppress_line(self, line: str) -> bool:
        return any(sub in line for sub in self._suppress)

    def _flush_tb(self) -> None:
        if not self._tb_lines:
            return
        if not self._tb_suppress:
            for l in self._tb_lines:
                try:
                    self._underlying.write(l + "\n")
                except Exception:
                    pass
        self._tb_lines = []
        self._tb_suppress = False
        self._in_tb = False

    def _flush_task_block(self) -> None:
        if not self._task_lines:
            return
        if not self._task_suppress:
            for l in self._task_lines:
                try:
                    self._underlying.write(l + "\n")
                except Exception:
                    pass
        self._task_lines = []
        self._task_suppress = False
        self._in_task_block = False

    def write(self, s: str) -> int:
        self._buf += str(s)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._handle_line(line)
        return len(s)

    def _handle_line(self, line: str) -> None:
        if not self._in_task_block and self._should_suppress_line(line) and (
            line.startswith("Task exception was never retrieved")
            or line.startswith("future: <Task finished")
            or line.startswith("unhandled exception on loop")
        ):
            self._in_task_block = True
            self._task_lines = [line]
            self._task_suppress = True
            return

        if self._in_task_block:
            if line.startswith("Traceback (most recent call last):"):
                self._in_tb = True
                self._tb_lines = [line]
                self._tb_suppress = True
                return
            self._task_lines.append(line)
            if self._should_suppress_line(line):
                self._task_suppress = True
            if line.strip() == "":
                self._flush_task_block()
            return

        # Start capturing tracebacks so we can suppress the whole block if it matches.
        if not self._in_tb and line.startswith("Traceback (most recent call last):"):
            self._in_tb = True
            self._tb_lines = [line]
            self._tb_suppress = False
            return

        if self._in_tb:
            self._tb_lines.append(line)
            if self._should_suppress_line(line):
                self._tb_suppress = True
            # End traceback block on blank line.
            if line.strip() == "":
                self._flush_tb()
                if self._in_task_block:
                    self._flush_task_block()
            return

        # Non-traceback line
        if self._should_suppress_line(line):
            return
        try:
            self._underlying.write(line + "\n")
        except Exception:
            pass

    def flush(self) -> None:
        # Flush any pending traceback block.
        if self._in_tb:
            # If the traceback ends without a trailing blank line, decide here.
            self._flush_tb()
        if self._in_task_block:
            self._flush_task_block()
        if self._buf:
            line = self._buf
            self._buf = ""
            if not self._should_suppress_line(line):
                try:
                    self._underlying.write(line)
                except Exception:
                    pass
        try:
            self._underlying.flush()
        except Exception:
            pass


@contextlib.contextmanager
def _suppress_aioslsk_noise() -> Any:
    """Temporarily suppress known aioslsk noise printed to stdout/stderr.

    Opt out by setting DOWNLOAD_SOULSEEK_VERBOSE=1.

    Note: The old env var name DOWNLOW_SOULSEEK_VERBOSE (typo) is deprecated.
    """
    if os.environ.get("DOWNLOAD_SOULSEEK_VERBOSE") or os.environ.get("DOWNLOW_SOULSEEK_VERBOSE"):
        yield
        return

    _configure_aioslsk_logging()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _LineFilterStream(old_out, _SOULSEEK_NOISE_SUBSTRINGS)
    sys.stderr = _LineFilterStream(old_err, _SOULSEEK_NOISE_SUBSTRINGS)
    try:
        yield
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        sys.stdout, sys.stderr = old_out, old_err


class Soulseek(Plugin):

    SUPPORTED_CMDLETS = frozenset({"download-file", "search-file"})
    TABLE_AUTO_STAGES = {
        "soulseek": ["download-file", "-plugin", "soulseek"],
    }
    QUERY_ARG_CHOICES = {
        "artist": (),
        "album": (),
        "track": (),
        "title": (),
    }
    INLINE_QUERY_FIELD_CHOICES = QUERY_ARG_CHOICES
    """Search provider for Soulseek P2P network."""

    def extract_query_arguments(self, query: str) -> tuple[str, Dict[str, Any]]:
        """Parse inline `key:value` query arguments (e.g. ``artist:kid cudi``).

        Returns ``(normalized_free_text_query, parsed_args)``. Multi-word values
        are supported, so ``artist:kid cudi`` yields ``kid cudi`` as both the
        free-text search term and the ``artist`` filter.
        """

        cleaned = str(query or "").strip()
        if not cleaned:
            return "", {}

        segments: List[str] = []
        for chunk in _DELIMITERS_RE.split(cleaned):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                for sub in _SEGMENT_BOUNDARY_RE.split(chunk):
                    part = sub.strip()
                    if part:
                        segments.append(part)
            else:
                segments.append(chunk)

        parsed_args: Dict[str, Any] = {}
        free_text: List[str] = []
        for segment in segments:
            sep_index = segment.find(":")
            if sep_index < 0:
                sep_index = segment.find("=")
            if sep_index <= 0:
                free_text.append(segment)
                continue
            key = segment[:sep_index].strip().lower()
            value = segment[sep_index + 1 :].strip().strip('"').strip("'")
            if not key or not value:
                free_text.append(segment)
                continue
            if key in self.QUERY_ARG_CHOICES:
                parsed_args[key] = value
            else:
                free_text.append(segment)

        normalized = " ".join(part for part in free_text if part).strip()
        if not normalized and parsed_args:
            # Join all structured values so `artist:x album:y` becomes a precise
            # Soulseek search for `x y` while still filtering by each field.
            normalized = " ".join(
                str(v) for v in parsed_args.values() if str(v).strip()
            ).strip()

        return normalized, parsed_args

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any,
    ) -> bool:
        """Handle Soulseek selection.
        
        Currently defaults to download-file via TABLE_AUTO_STAGES, but this
        hook allows for future 'Browse User' or 'Browse Folder' drill-down.
        """
        if not stage_is_last:
            return False

        # If we wanted to handle drill-down (like Tidal.py) we would:
        # 1. Fetch more data (e.g. user shares)
        # 2. Create a new ResultTable
        # 3. ctx.set_current_stage_table(new_table)
        # 4. return True
        
        return False

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "username",
                "label": "Soulseek Username",
                "default": "",
                "required": True
            },
            {
                "key": "password",
                "label": "Soulseek Password",
                "default": "",
                "required": True,
                "secret": True
            }
        ]

    MUSIC_EXTENSIONS = {
        ".flac",
        ".mp3",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".wav",
        ".alac",
        ".wma",
        ".ape",
        ".aiff",
        ".dsf",
        ".dff",
        ".wv",
        ".tta",
        ".tak",
        ".ac3",
        ".dts",
    }

    # NOTE: These defaults preserve existing behavior.
    USERNAME = "asjhkjljhkjfdsd334"
    PASSWORD = "khhhg"
    DOWNLOAD_DIR = None
    MAX_WAIT_TRANSFER = 1200

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        try:
            from SYS.config import get_soulseek_username, get_soulseek_password

            user = get_soulseek_username(self.config)
            pwd = get_soulseek_password(self.config)
            if user:
                Soulseek.USERNAME = user
            if pwd:
                Soulseek.PASSWORD = pwd
        except Exception:
            pass

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        """Download file from Soulseek."""

        try:
            full_metadata = result.full_metadata or {}
            username = full_metadata.get("username")
            filename = full_metadata.get("filename") or result.path

            if not username or not filename:
                # If we were invoked via generic download-file on a SearchResult
                # that has minimal data (e.g. from table selection), try to rescue it.
                if isinstance(result, SearchResult) and result.full_metadata:
                    username = result.full_metadata.get("username")
                    filename = result.full_metadata.get("filename")

            if not username or not filename:
                log(
                    f"[soulseek] Missing metadata for download: {result.title}",
                    file=sys.stderr
                )
                return None
            
            # Cast to str for Mypy
            username = str(username)
            filename = str(filename)
            
            # Use tempfile directory as default if generic path elements were passed or None.
            if output_dir is None:
                import tempfile
                target_dir = Path(tempfile.gettempdir()) / "Medios" / "Soulseek"
            else:
                target_dir = Path(output_dir)
                if str(target_dir) in (".", "downloads", "Downloads"):
                    import tempfile
                    target_dir = Path(tempfile.gettempdir()) / "Medios" / "Soulseek"
            
            target_dir.mkdir(parents=True, exist_ok=True)

            return _run_async_from_sync(
                lambda: download_soulseek_file(
                    username=username,
                    filename=filename,
                    output_dir=target_dir,
                    timeout=self.MAX_WAIT_TRANSFER,
                )
            )

        except Exception as exc:
            log(f"[soulseek] Download error: {exc}", file=sys.stderr)
            return None

    async def perform_search(self,
                             query: str,
                             timeout: float = 9.0,
                             limit: int = 50) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Perform async Soulseek search."""

        from aioslsk.client import SoulSeekClient
        from aioslsk.settings import CredentialsSettings, Settings

        # Removed legacy os.makedirs(self.DOWNLOAD_DIR) - specific commands handle output dirs.

        settings = Settings(
            credentials=CredentialsSettings(
                username=self.USERNAME,
                password=self.PASSWORD
            )
        )
        client = SoulSeekClient(settings)

        with _suppress_aioslsk_noise():
            async with _suppress_aioslsk_asyncio_task_noise():
                try:
                    await client.start()
                    await client.login()
                except Exception as exc:
                    log(
                        f"[soulseek] Login failed: {type(exc).__name__}: {exc}",
                        file=sys.stderr
                    )
                    return [], {}

                search_request: Any = None
                try:
                    search_request = await client.searches.search(query)
                    summary = await self._collect_results(search_request, timeout=timeout)
                    return self._flatten_results(search_request)[:limit], summary
                except Exception as exc:
                    log(
                        f"[soulseek] Search error: {type(exc).__name__}: {exc}",
                        file=sys.stderr
                    )
                    return [], {}
                finally:
                    # Best-effort: try to cancel/close the search request before stopping
                    # the client to reduce stray reply spam.
                    try:
                        if search_request is not None:
                            cancel = getattr(search_request, "cancel", None)
                            if callable(cancel):
                                maybe = cancel()
                                if asyncio.iscoroutine(maybe):
                                    await maybe
                    except Exception:
                        pass
                    try:
                        await client.stop()
                    except Exception:
                        pass
                    # Give Proactor/Windows loop a moment to drain internal buffers after stop.
                    try:
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass

    def _flatten_results(self, search_request: Any) -> List[dict]:
        flat: List[dict] = []
        for result in getattr(search_request, "results", []):
            username = getattr(result, "username", "?")

            def _add(file_data: Any) -> None:
                flat.append({
                    "file": file_data,
                    "username": username,
                    "filename": getattr(file_data, "filename", "?"),
                    "size": getattr(file_data, "filesize", 0)
                })

            for file_data in getattr(result, "shared_items", []):
                _add(file_data)

            for file_data in getattr(result, "locked_results", []):
                _add(file_data)

        return flat

    async def _collect_results(
        self,
        search_request: Any,
        timeout: float = 75.0
    ) -> Dict[str, Any]:
        start = time.time()
        end = start + timeout
        last_count = 0
        update_count = 0
        while time.time() < end:
            current_count = len(getattr(search_request, "results", []))
            if current_count > last_count:
                last_count = current_count
                update_count += 1
            await asyncio.sleep(0.5)

        return {
            "peer_hits": last_count,
            "count_updates": update_count,
            "elapsed_seconds": round(max(0.0, time.time() - start), 1),
        }

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str,
                               Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        filters = filters or {}

        # Ensure temp download dir structure exists, but don't create legacy ./downloads here.
        import tempfile
        base_tmp = Path(tempfile.gettempdir()) / "Medios" / "Soulseek"
        base_tmp.mkdir(parents=True, exist_ok=True)

        flat_results, search_summary = _run_async_from_sync(
            lambda: self.perform_search(query, timeout=9.0, limit=limit)
        )

        try:
            if not flat_results:
                return []

            music_results: List[dict] = []
            for item in flat_results:
                filename = item["filename"]
                ext = (
                    "." + filename.rsplit(".",
                                          1)[-1].lower()
                ) if "." in filename else ""
                if ext in self.MUSIC_EXTENSIONS:
                    music_results.append(item)

            if not music_results:
                return []

            enriched_results: List[dict] = []
            for item in music_results:
                filename = item["filename"]
                ext = (
                    "." + filename.rsplit(".",
                                          1)[-1].lower()
                ) if "." in filename else ""

                display_name = filename.replace("\\", "/").split("/")[-1]
                path_parts = filename.replace("\\", "/").split("/")
                artist = path_parts[-3] if len(path_parts) >= 3 else ""
                album = (
                    path_parts[-2] if len(path_parts) >= 3 else
                    (path_parts[-2] if len(path_parts) == 2 else "")
                )

                base_name = display_name.rsplit(
                    ".",
                    1
                )[0] if "." in display_name else display_name
                track_num = ""
                title = base_name
                filename_artist = ""

                match = re.match(r"^(\d{1,3})\s*[\.\-]?\s+(.+)$", base_name)
                if match:
                    track_num = match.group(1)
                    rest = match.group(2)
                    if " - " in rest:
                        filename_artist, title = rest.split(" - ", 1)
                    else:
                        title = rest

                if filename_artist:
                    artist = filename_artist

                enriched_results.append(
                    {
                        **item,
                        "artist": artist,
                        "album": album,
                        "title": title,
                        "track_num": track_num,
                        "ext": ext,
                    }
                )

            if filters:
                artist_filter = (filters.get("artist", "") or "").lower()
                album_filter = (filters.get("album", "") or "").lower()
                track_filter = (filters.get("track", "") or "").lower()

                if artist_filter or album_filter or track_filter:
                    filtered: List[dict] = []
                    for item in enriched_results:
                        if artist_filter and artist_filter not in item["artist"].lower(
                        ):
                            continue
                        if album_filter and album_filter not in item["album"].lower():
                            continue
                        if track_filter and track_filter not in item["title"].lower():
                            continue
                        filtered.append(item)
                    enriched_results = filtered

            enriched_results.sort(
                key=lambda item: (item["ext"].lower() != ".flac", -item["size"])
            )

            results: List[SearchResult] = []
            for item in enriched_results:
                artist_display = item["artist"] if item["artist"] else "(no artist)"
                album_display = item["album"] if item["album"] else "(no album)"
                size_mb = int(item["size"] / 1024 / 1024)

                columns = [
                    ("Track",
                     item["track_num"] or "?"),
                    ("Title",
                     item["title"][:40]),
                    ("Artist",
                     artist_display[:32]),
                    ("Album",
                     album_display[:32]),
                    ("Size",
                     f"{size_mb} MB"),
                ]

                tags: set[str] = {"source:soulseek"}
                if str(item["artist"] or "").strip():
                    tags.add(f"artist:{item['artist'].strip()}")
                if str(item["album"] or "").strip():
                    tags.add(f"album:{item['album'].strip()}")
                if str(item["track_num"] or "").strip():
                    tags.add(f"track:{item['track_num'].strip()}")
                if str(item["title"] or "").strip():
                    tags.add(f"title:{item['title'].strip()}")

                results.append(
                    SearchResult(
                        table="soulseek",
                        title=item["title"],
                        path=item["filename"],
                        detail=f"{artist_display} - {album_display}",
                        annotations=[f"{size_mb} MB",
                                     item["ext"].lstrip(".").upper()],
                        media_kind="audio",
                        size_bytes=item["size"],
                        tag=tags,
                        columns=columns,
                        selection_action=["download-file", "-plugin", "soulseek"],
                        full_metadata={
                            "username": item["username"],
                            "filename": item["filename"],
                            "artist": item["artist"],
                            "album": item["album"],
                            "track_num": item["track_num"],
                            "ext": item["ext"],
                            "plugin": "soulseek"
                        },
                    )
                )

            try:
                debug_panel(
                    "soulseek search",
                    [
                        ("query", query),
                        ("peer_hits", search_summary.get("peer_hits", 0)),
                        ("file_hits", len(flat_results)),
                        ("audio_hits", len(music_results)),
                        ("results", len(results)),
                        ("poll_updates", search_summary.get("count_updates", 0)),
                        ("elapsed_s", search_summary.get("elapsed_seconds", 0.0)),
                    ],
                    border_style="magenta",
                )
            except Exception:
                pass

            return results

        except Exception as exc:
            log(f"[soulseek] Search error: {exc}", file=sys.stderr)
            return []

    def validate(self) -> bool:
        try:
            from aioslsk.client import SoulSeekClient  # noqa: F401

            # Require configured credentials.
            try:
                from SYS.config import get_soulseek_username, get_soulseek_password

                user = get_soulseek_username(self.config)
                pwd = get_soulseek_password(self.config)
                return bool(user and pwd)
            except Exception:
                # Fall back to legacy class defaults if config helpers aren't available.
                return bool(Soulseek.USERNAME and Soulseek.PASSWORD)
        except ImportError:
            return False


async def download_soulseek_file(
    username: str,
    filename: str,
    output_dir: Optional[Path] = None,
    timeout: int = 1200,
    *,
    client_username: Optional[str] = None,
    client_password: Optional[str] = None,
) -> Optional[Path]:
    """Download a file from a Soulseek peer."""

    try:
        from aioslsk.client import SoulSeekClient
        from aioslsk.settings import CredentialsSettings, Settings
        from aioslsk.transfer.model import Transfer, TransferDirection
        from aioslsk.transfer.state import TransferState

        if output_dir is None:
            import tempfile
            output_dir = Path(tempfile.gettempdir()) / "Medios" / "Soulseek"
        
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        local_filename = filename.replace("\\", "/").split("/")[-1]
        output_user_dir = output_dir / username
        output_user_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_user_dir / local_filename

        if output_path.exists():
            base = output_path.stem
            ext = output_path.suffix
            counter = 1
            while output_path.exists():
                output_path = output_user_dir / f"{base}_{counter}{ext}"
                counter += 1

        output_path = output_path.resolve()

        login_user = (client_username or Soulseek.USERNAME or "").strip()
        login_pass = (client_password or Soulseek.PASSWORD or "").strip()
        if not login_user or not login_pass:
            raise RuntimeError(
                "Soulseek credentials not configured (set provider=soulseek username/password)"
            )

        settings = Settings(
            credentials=CredentialsSettings(username=login_user,
                                            password=login_pass)
        )

        async def _attempt_once(
            attempt_num: int
        ) -> tuple[Optional[Path],
                   Any,
                   int,
                   float]:
            client = SoulSeekClient(settings)
            with _suppress_aioslsk_noise():
                async with _suppress_aioslsk_asyncio_task_noise():
                    try:
                        await client.start()
                        await client.login()
                        debug(f"[soulseek] Logged in as {login_user}")

                        status_panel(
                            "Soulseek download",
                            [
                                ("attempt", attempt_num),
                                ("user", username),
                                ("file", local_filename),
                            ],
                        )
                        debug(
                            f"[soulseek] Requesting download from {username}: {filename}"
                        )

                        transfer = await client.transfers.add(
                            Transfer(username,
                                     filename,
                                     TransferDirection.DOWNLOAD)
                        )
                        transfer.local_path = str(output_path)
                        await client.transfers.queue(transfer)

                        start_time = time.time()
                        last_progress_time = start_time
                        progress_bar = ProgressBar()

                        while not transfer.is_finalized():
                            elapsed = time.time() - start_time
                            if elapsed > timeout:
                                log(
                                    f"[soulseek] Download timeout after {timeout}s",
                                    file=sys.stderr
                                )
                                bytes_done = int(
                                    getattr(transfer,
                                            "bytes_transfered",
                                            0) or 0
                                )
                                state_val = getattr(
                                    getattr(transfer,
                                            "state",
                                            None),
                                    "VALUE",
                                    None
                                )
                                progress_bar.finish()
                                return None, state_val, bytes_done, elapsed

                            bytes_done = int(
                                getattr(transfer,
                                        "bytes_transfered",
                                        0) or 0
                            )
                            total_bytes = int(getattr(transfer, "filesize", 0) or 0)
                            now = time.time()
                            if now - last_progress_time >= 0.5:
                                progress_bar.update(
                                    downloaded=bytes_done,
                                    total=total_bytes if total_bytes > 0 else None,
                                    label="download",
                                    file=sys.stderr,
                                )
                                last_progress_time = now

                            await asyncio.sleep(1)

                        final_state = getattr(
                            getattr(transfer,
                                    "state",
                                    None),
                            "VALUE",
                            None
                        )
                        downloaded_path = (
                            Path(transfer.local_path)
                            if getattr(transfer,
                                       "local_path",
                                       None) else output_path
                        )
                        final_elapsed = time.time() - start_time

                        # Clear in-place progress bar.
                        progress_bar.finish()

                        # If a file was written, treat it as success even if state is odd.
                        try:
                            if downloaded_path.exists() and downloaded_path.stat(
                            ).st_size > 0:
                                if final_state != TransferState.COMPLETE:
                                    log(
                                        f"[soulseek] Transfer finalized as {final_state}, but file exists ({downloaded_path.stat().st_size} bytes). Keeping file.",
                                        file=sys.stderr,
                                    )
                                return (
                                    downloaded_path,
                                    final_state,
                                    int(downloaded_path.stat().st_size),
                                    final_elapsed,
                                )
                        except Exception:
                            pass

                        if final_state == TransferState.COMPLETE and downloaded_path.exists(
                        ):
                            debug(f"[soulseek] Download complete: {downloaded_path}")
                            return (
                                downloaded_path,
                                final_state,
                                int(downloaded_path.stat().st_size),
                                final_elapsed,
                            )

                        fail_bytes = int(getattr(transfer, "bytes_transfered", 0) or 0)
                        fail_total = int(getattr(transfer, "filesize", 0) or 0)
                        reason = getattr(transfer, "reason", None)
                        log(
                            f"[soulseek] Download failed: state={final_state} bytes={fail_bytes}/{fail_total} reason={reason}",
                            file=sys.stderr,
                        )

                        # Clean up 0-byte placeholder.
                        try:
                            if downloaded_path.exists() and downloaded_path.stat(
                            ).st_size == 0:
                                downloaded_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return None, final_state, fail_bytes, final_elapsed
                    finally:
                        try:
                            await client.stop()
                        except Exception:
                            pass
                        # Let cancellation/cleanup callbacks run while our exception handler is still installed.
                        # Increased to 0.2s for Windows Proactor loop stability.
                        try:
                            await asyncio.sleep(0.2)
                        except Exception:
                            pass

        # Retry a couple times only for fast 0-byte failures (common transient case).
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            result_path, final_state, bytes_done, elapsed = await _attempt_once(attempt)
            if result_path:
                return result_path

            should_retry = (bytes_done == 0) and (elapsed < 15.0)
            if attempt < max_attempts and should_retry:
                log(
                    f"[soulseek] Retrying after fast failure (state={final_state})",
                    file=sys.stderr
                )
                await asyncio.sleep(2)
                continue
            break
        return None

    except ImportError:
        log(
            "[soulseek] aioslsk not installed. Install with: pip install aioslsk",
            file=sys.stderr
        )
        return None
    except Exception as exc:
        log(f"[soulseek] Download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
