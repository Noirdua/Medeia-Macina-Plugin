"""Hydrus API helpers and export utilities."""

from __future__ import annotations

import base64
import http.client
import json
import os
import ssl
import re
import shutil
import subprocess
import sys
import time
from collections import deque

from SYS.logger import log
from SYS.utils_constant import ALL_SUPPORTED_EXTENSIONS as GLOBAL_SUPPORTED_EXTENSIONS
import tempfile
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Type, TypeVar, Union, cast
from urllib.parse import urlsplit, urlencode, quote, urlunsplit, unquote
import httpx

logger = logging.getLogger(__name__)

from SYS.utils import (
    decode_cbor,
    jsonify,
    ensure_directory,
    unique_path,
)
from API.HTTP import HTTPClient


class HydrusRequestError(RuntimeError):
    """Raised when the Hydrus Client API returns an error response."""

    def __init__(self, status: int, message: str, payload: Any | None = None) -> None:
        super().__init__(f"Hydrus request failed ({status}): {message}")
        self.status = status
        self.message = str(message or "")
        self.payload = payload


class HydrusFileMissingError(HydrusRequestError):
    """Raised when Hydrus knows a hash but does not have the local file bytes."""

    def __init__(self, message: str = "The client does not have this file!", payload: Any | None = None) -> None:
        super().__init__(404, message, payload)


class HydrusConnectionError(HydrusRequestError):
    """Raised when Hydrus service is unavailable (connection refused, timeout, etc.).

    This is an expected error when Hydrus is not running and should not include
    a full traceback in logs.
    """

    def __init__(self, message: str) -> None:
        super().__init__(0, message, None)  # status 0 indicates connection error
        self.is_connection_error = True


def is_invalid_tag_query_error(exc: BaseException | None) -> bool:
    """Return True when Hydrus rejected a tag query as invalid user input."""

    if not isinstance(exc, HydrusRequestError):
        return False
    if getattr(exc, "status", None) != 400:
        return False

    message_parts: list[str] = [str(getattr(exc, "message", "") or "")]
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        for key in ("message", "error"):
            value = payload.get(key)
            if value:
                message_parts.append(str(value))
    elif payload:
        message_parts.append(str(payload))

    text = " ".join(part for part in message_parts if part).lower()
    return "could not understand the tag" in text


@dataclass(slots=True)
class HydrusRequestSpec:
    method: str
    endpoint: str
    query: dict[str, Any] | None = None
    data: Any | None = None
    file_path: Path | None = None
    content_type: str | None = None
    accept: str | None = "application/cbor"
    pipeline_progress: Any | None = None
    transfer_label: str | None = None


@dataclass(slots=True)
class HydrusNetwork:
    """Thin wrapper around the Hydrus Client API."""

    url: str
    access_key: str = ""
    timeout: float = 9.0
    upload_io_timeout: float = 120.0
    upload_chunk_size: int = 64 * 1024
    instance_name: str = ""  # Optional store name (e.g., 'home') for namespaced logs

    scheme: str = field(init=False)
    hostname: str = field(init=False)
    port: int = field(init=False)
    base_path: str = field(init=False)
    _session_key: str = field(init=False, default="", repr=False)  # Cached session key

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("Hydrus base URL is required")
        self.url = self.url.rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http",
                                 "https"}:
            raise ValueError("Hydrus base URL must use http or https")
        self.scheme = parsed.scheme
        self.hostname = parsed.hostname or "localhost"
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        self.base_path = parsed.path.rstrip("/")
        self.access_key = self.access_key or ""
        self.instance_name = str(self.instance_name or "").strip()

    def _log_prefix(self) -> str:
        if self.instance_name:
            return f"[hydrusnetwork:{self.instance_name}]"
        return f"[hydrusnetwork:{self.hostname}:{self.port}]"

    # ------------------------------------------------------------------
    # low-level helpers
    # ------------------------------------------------------------------

    def _build_path(self, endpoint: str, query: dict[str, Any] | None = None) -> str:
        path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        if self.base_path:
            path = f"{self.base_path}{path}"
        if query:
            encoded = urlencode(query, doseq=True)
            if encoded:
                path = f"{path}?{encoded}"
        return path

    def _put_raw_file(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        file_path: Path,
        file_size: int,
        timeout: float,
        on_progress: Any,
        chunk_size: int,
    ) -> tuple[int, str, bytes, str]:
        """POST raw file bytes with Content-Length (never HTTP chunked).

        Reverse proxies such as Caddy turn httpx streaming uploads into
        chunked bodies, which Hydrus then rejects as "Unknown filetype!".
        """
        parsed = urlsplit(url)
        host = parsed.hostname or self.hostname
        port = parsed.port or self.port
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        connection_cls = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        conn_kwargs: dict[str, Any] = {"timeout": timeout}
        if parsed.scheme == "https":
            conn_kwargs["context"] = ssl._create_unverified_context()
        connection = connection_cls(host, port, **conn_kwargs)

        sent = 0
        try:
            connection.putrequest(method or "POST", path, skip_host=True, skip_accept_encoding=True)
            host_header = host
            if (parsed.scheme == "http" and port not in (80, None)) or (
                parsed.scheme == "https" and port not in (443, None)
            ):
                host_header = f"{host}:{port}"
            connection.putheader("Host", host_header)
            connection.putheader("Connection", "close")
            for key, value in headers.items():
                if not value or key.lower() in {"host", "transfer-encoding", "connection"}:
                    continue
                connection.putheader(key, value)
            if "content-length" not in {k.lower() for k in headers}:
                connection.putheader("Content-Length", str(int(file_size)))
            connection.endheaders()
            with file_path.open("rb") as handle:
                while True:
                    chunk = handle.read(max(1, int(chunk_size)))
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    try:
                        on_progress(sent, final=False)
                    except Exception:
                        pass
            try:
                on_progress(sent, final=True)
            except Exception:
                pass
            response = connection.getresponse()
            body = response.read() or b""
            content_type = response.getheader("Content-Type", "") or ""
            return int(response.status), str(response.reason or ""), body, content_type
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def _perform_request(self, spec: HydrusRequestSpec) -> Any:
        headers: dict[str,
                      str] = {}

        # Use session key if available, otherwise use access key
        if self._session_key:
            headers["Hydrus-Client-API-Session-Key"] = self._session_key
        elif self.access_key:
            headers["Hydrus-Client-API-Access-Key"] = self.access_key
        if spec.accept:
            headers["Accept"] = spec.accept

        path = self._build_path(spec.endpoint, spec.query)
        url = f"{self.scheme}://{self.hostname}:{self.port}{path}"

        # Log request details
        logger.debug(
            f"{self._log_prefix()} {spec.method} {spec.endpoint} (auth: {'session_key' if self._session_key else 'access_key' if self.access_key else 'none'})"
        )

        status = 0
        reason = ""
        body = b""
        content_type = ""

        try:
            with HTTPClient(timeout=self.timeout,
                            headers=headers,
                            verify_ssl=False,
                            retries=1,
                            trust_env=False) as client:
                response = None

                if spec.file_path is not None:
                    file_path = Path(spec.file_path)
                    if not file_path.is_file():
                        error_msg = f"Upload file not found: {file_path}"
                        logger.error(f"{self._log_prefix()} {error_msg}")
                        raise FileNotFoundError(error_msg)

                    file_size = file_path.stat().st_size
                    headers["Content-Type"] = spec.content_type or "application/octet-stream"
                    # Hydrus expects the raw file bytes as the POST body. Prefer a
                    # Content-Length body over chunked transfer encoding — some Hydrus
                    # server stacks mis-handle chunked uploads on larger files and then
                    # report "Unknown filetype!".
                    headers["Content-Length"] = str(int(file_size))
                    headers.pop("Transfer-Encoding", None)

                    logger.debug(
                        f"{self._log_prefix()} Uploading file {file_path.name} ({file_size} bytes)"
                    )

                    # Upload timeout policy:
                    # - Keep normal requests fast via `self.timeout`.
                    # - For streaming uploads, use an activity timeout for read/write so
                    #   long transfers do not fail due to a short generic timeout.
                    # - Scale IO timeout with file size (min 120s, ~1MB/s floor, cap 30m).
                    try:
                        upload_io_timeout = float(self.upload_io_timeout)
                    except Exception:
                        upload_io_timeout = 120.0
                    size_based_timeout = max(120.0, float(file_size) / (1024.0 * 1024.0) + 60.0)
                    size_based_timeout = min(size_based_timeout, 30.0 * 60.0)
                    if upload_io_timeout <= 0:
                        effective_io_timeout = None
                    else:
                        effective_io_timeout = max(float(upload_io_timeout), size_based_timeout)
                    # Handshake/connect through a reverse proxy can exceed the
                    # short API timeout used for GETs. Keep IO timeouts large.
                    connect_timeout = max(float(self.timeout), 30.0)
                    if effective_io_timeout is None:
                        upload_timeout = httpx.Timeout(
                            connect=connect_timeout,
                            read=None,
                            write=None,
                            pool=connect_timeout,
                        )
                    else:
                        upload_timeout = httpx.Timeout(
                            connect=connect_timeout,
                            read=effective_io_timeout,
                            write=effective_io_timeout,
                            pool=connect_timeout,
                        )

                    try:
                        chunk_size = int(self.upload_chunk_size)
                    except Exception:
                        chunk_size = 64 * 1024
                    if chunk_size <= 0:
                        chunk_size = 64 * 1024

                    pipeline_progress = spec.pipeline_progress
                    use_pipeline_transfer = False
                    if pipeline_progress is not None:
                        try:
                            ui, _ = pipeline_progress.ui_and_pipe_index()
                            use_pipeline_transfer = ui is not None
                        except Exception:
                            use_pipeline_transfer = False

                    bar = None
                    if not use_pipeline_transfer:
                        from SYS.models import ProgressBar

                        bar = ProgressBar()

                    label = str(spec.transfer_label or getattr(file_path, "name", None) or "upload")
                    traffic_sent_total = [0]

                    upload_attempts = 5
                    last_upload_exc: Exception | None = None
                    for attempt in range(upload_attempts):
                        sent_this_attempt = [0]
                        last_render_t = [time.time()]

                        def _render_progress(completed: int, final: bool = False) -> None:
                            if file_size <= 0:
                                return
                            sent_this_attempt[0] = int(completed)
                            traffic_sent_total[0] = max(int(traffic_sent_total[0]), int(completed))
                            now = time.time()
                            if not final and (now - float(last_render_t[0])) < 0.25:
                                return
                            last_render_t[0] = now
                            if use_pipeline_transfer:
                                try:
                                    if int(completed) <= 0:
                                        pipeline_progress.begin_transfer(
                                            label=str(label),
                                            total=int(file_size),
                                        )
                                    pipeline_progress.update_transfer(
                                        label=str(label),
                                        completed=int(completed),
                                        total=int(file_size),
                                    )
                                    if final:
                                        pipeline_progress.finish_transfer(label=str(label))
                                except Exception:
                                    pass
                            elif bar is not None:
                                bar.update(
                                    downloaded=int(completed),
                                    total=int(file_size),
                                    label=str(label),
                                    file=sys.stderr,
                                )
                                if final:
                                    bar.finish()

                        try:
                            if use_pipeline_transfer:
                                try:
                                    pipeline_progress.begin_transfer(
                                        label=str(label),
                                        total=int(file_size),
                                    )
                                except Exception:
                                    pass
                            status, reason, body, content_type = self._put_raw_file(
                                method=spec.method,
                                url=url,
                                headers=headers,
                                file_path=file_path,
                                file_size=file_size,
                                timeout=float(
                                    effective_io_timeout
                                    if effective_io_timeout is not None
                                    else connect_timeout
                                ),
                                on_progress=_render_progress,
                                chunk_size=chunk_size,
                            )
                            sent_bytes = int(sent_this_attempt[0])
                            if file_size > 0 and sent_bytes > 0 and sent_bytes < file_size:
                                raise OSError(
                                    f"Upload truncated: sent {sent_bytes} of {file_size} bytes"
                                )
                            logger.debug(
                                f"{self._log_prefix()} Upload completed on attempt {attempt + 1}/{upload_attempts}; "
                                f"attempt_bytes={sent_bytes}, file_size={file_size}, "
                                f"total_traffic_bytes={int(traffic_sent_total[0])}"
                            )
                            last_upload_exc = None
                            break
                        except (OSError, TimeoutError, http.client.HTTPException) as exc:
                            last_upload_exc = exc
                            logger.warning(
                                f"{self._log_prefix()} Upload timeout/error on attempt {attempt + 1}/{upload_attempts}: {exc} "
                                f"(attempt_bytes={int(sent_this_attempt[0])}, total_traffic_bytes={int(traffic_sent_total[0])})"
                            )
                            if attempt < upload_attempts - 1:
                                try:
                                    backoff = (2 ** attempt) * 0.75 + (0.25 * (time.time() % 1.0))
                                    logger.debug(
                                        f"{self._log_prefix()} Upload retry {attempt + 1}/{upload_attempts} "
                                        f"after {backoff:.1f}s backoff (sent {int(sent_this_attempt[0])} bytes this attempt, "
                                        f"{int(traffic_sent_total[0])} total)"
                                    )
                                    time.sleep(backoff)
                                except Exception:
                                    pass
                                continue
                            raise
                    if response is None and last_upload_exc is not None:
                        raise last_upload_exc
                else:
                    content = None
                    json_data = None
                    if spec.data is not None:
                        if isinstance(spec.data, (bytes, bytearray)):
                            content = spec.data
                        else:
                            json_data = spec.data
                            # Hydrus expects JSON bodies to be sent with Content-Type: application/json.
                            # httpx will usually set this automatically, but we set it explicitly to
                            # match the Hydrus API docs and avoid edge cases.
                            headers.setdefault("Content-Type", "application/json")
                        logger.debug(
                            f"{self._log_prefix()} Request body size: {len(content) if content else 'json'}"
                        )

                    response = client.request(
                        spec.method,
                        url,
                        content=content,
                        json=json_data,
                        headers=headers,
                        raise_for_status=False,
                        log_http_errors=False,
                    )

                if spec.file_path is None:
                    status = response.status_code
                    reason = response.reason_phrase
                    body = response.content
                    content_type = response.headers.get("Content-Type", "") or ""

                logger.debug(
                    f"{self._log_prefix()} Response {status} {reason} ({len(body)} bytes)"
                )

        except FileNotFoundError:
            raise
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, OSError, TimeoutError, http.client.HTTPException) as exc:
            msg = f"Hydrus unavailable: {exc}"
            logger.warning(f"{self._log_prefix()} {msg}")
            raise HydrusConnectionError(msg) from exc
        except Exception as exc:
            logger.error(f"{self._log_prefix()} Connection error: {exc}", exc_info=True)
            raise

        payload: Any
        payload = {}
        if body:
            content_main = content_type.split(";", 1)[0].strip().lower()
            if "json" in content_main:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = body.decode("utf-8", "replace")
            elif "cbor" in content_main:
                try:
                    payload = decode_cbor(body)
                except Exception:
                    payload = body
            else:
                payload = body

        if status >= 400:
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("message") or payload.get("error") or payload)
            elif isinstance(payload, str):
                message = payload
            else:
                message = reason or "HTTP error"

            # Some endpoints are naturally "missing" sometimes and should not spam logs.
            if status == 404 and spec.endpoint.rstrip("/") == "/get_files/file_path":
                body_str = ""
                if isinstance(payload, dict):
                    body_str = str(payload.get("error") or payload.get("message") or "")
                elif isinstance(payload, (bytes, bytearray)):
                    try:
                        body_str = bytes(payload).decode("utf-8", "replace")
                    except Exception:
                        body_str = ""
                elif isinstance(payload, str):
                    body_str = payload

                if "does not have this file" in body_str.lower():
                    logger.warning(
                        f"{self._log_prefix()} /get_files/file_path returned 404 — file not in this Hydrus instance"
                    )
                    raise HydrusFileMissingError(body_str or message, payload)

                logger.debug(
                    f"{self._log_prefix()} /get_files/file_path returned 404 (not supported) - caller should fallback to HTTP"
                )
                return {}

            if is_invalid_tag_query_error(HydrusRequestError(status, message, payload)):
                logger.warning(f"{self._log_prefix()} Invalid Hydrus tag query: {message}")
            else:
                logger.error(f"{self._log_prefix()} HTTP {status}: {message}")

            # Handle expired session key (419) by clearing cache and retrying once
            if status == 419 and self._session_key and "session" in message.lower():
                logger.warning(
                    f"{self._log_prefix()} Session key expired, acquiring new one and retrying..."
                )
                self._session_key = ""  # Clear expired session key
                try:
                    self._acquire_session_key()
                    # Retry the request with new session key
                    return self._perform_request(spec)
                except Exception as retry_error:
                    logger.error(
                        f"{self._log_prefix()} Retry failed: {retry_error}",
                        exc_info=True
                    )
                    # If retry fails, raise the original error
                    raise HydrusRequestError(status, message, payload) from retry_error

            raise HydrusRequestError(status, message, payload)

        return payload

    def _acquire_session_key(self) -> str:
        """Acquire a session key from the Hydrus API using the access key.

        Session keys are temporary authentication tokens that expire after 24 hours
        of inactivity, client restart, or if the access key is deleted. They are
        more secure than passing access keys in every request.

        Returns the session key string.
        Raises HydrusRequestError if the request fails.
        """
        if not self.access_key:
            raise HydrusRequestError(
                401,
                "Cannot acquire session key: no access key configured"
            )

        # Temporarily use access key to get session key
        original_session_key = self._session_key
        try:
            self._session_key = ""  # Clear session key to use access key for this request

            result = self._get("/session_key")
            session_key = result.get("session_key")

            if not session_key:
                raise HydrusRequestError(
                    500,
                    "Session key response missing 'session_key' field",
                    result
                )

            self._session_key = session_key
            return session_key
        except HydrusRequestError:
            self._session_key = original_session_key
            raise
        except Exception as e:
            self._session_key = original_session_key
            raise HydrusRequestError(500, f"Failed to acquire session key: {e}")

    def ensure_session_key(self) -> str:
        """Ensure a valid session key exists, acquiring one if needed.

        Returns the session key. If one is already cached, returns it.
        Otherwise acquires a new session key from the API.
        """
        if self._session_key:
            return self._session_key
        return self._acquire_session_key()

    def _get(self,
             endpoint: str,
             *,
             query: dict[str,
                         Any] | None = None) -> dict[str,
                                                     Any]:
        spec = HydrusRequestSpec("GET", endpoint, query=query)
        return cast(dict[str, Any], self._perform_request(spec))

    def _post(
        self,
        endpoint: str,
        *,
        data: dict[str,
                   Any] | None = None,
        file_path: Path | None = None,
        content_type: str | None = None,
        pipeline_progress: Any | None = None,
        transfer_label: str | None = None,
    ) -> dict[str,
              Any]:
        spec = HydrusRequestSpec(
            "POST",
            endpoint,
            data=data,
            file_path=file_path,
            content_type=content_type,
            pipeline_progress=pipeline_progress,
            transfer_label=transfer_label,
        )
        return cast(dict[str, Any], self._perform_request(spec))

    def _ensure_hashes(self, hash: Union[str, Iterable[str]]) -> list[str]:
        if isinstance(hash, str):
            return [hash]
        return list(hash)

    def _append_access_key(self, url: str) -> str:
        if not self.access_key:
            return url
        separator = "&" if "?" in url else "?"
        # Use the correct parameter name for Hydrus API compatibility
        return f"{url}{separator}access_key={quote(self.access_key)}"

    def add_file(
        self,
        path: Union[str, Path],
        *,
        pipeline_progress: Any | None = None,
        transfer_label: str | None = None,
    ) -> dict[str, Any]:
        """Add a file to Hydrus using the octet-stream upload mode.

        This mirrors the Hydrus API POST /add_files/add_file behavior when sending
        the file bytes as the POST body. The method accepts either a filesystem
        `Path` or a string path and will raise FileNotFoundError if the target
        path is not a readable file.
        """
        # Accept both Path and str for convenience
        file_path = Path(path) if not isinstance(path, Path) else path
        if not file_path.is_file():
            raise FileNotFoundError(f"Upload file not found: {file_path}")

        # Forward as file_path so the request body is streamed as application/octet-stream
        return self._post(
            "/add_files/add_file",
            file_path=file_path,
            pipeline_progress=pipeline_progress,
            transfer_label=transfer_label,
        )

    def undelete_files(self, hashes: Union[str, Iterable[str]]) -> dict[str, Any]:
        """Restore files from Hydrus trash back into 'my files'.

        Hydrus Client API: POST /add_files/undelete_files
        Required JSON args: {"hashes": [<sha256 hex>, ...]}
        """
        hash_list = self._ensure_hashes(hashes)
        body = {
            "hashes": hash_list
        }
        return self._post("/add_files/undelete_files", data=body)

    def delete_files(
        self,
        hashes: Union[str,
                      Iterable[str]],
        *,
        reason: str | None = None
    ) -> dict[str,
              Any]:
        """Delete files in Hydrus.

        Hydrus Client API: POST /add_files/delete_files
        Required JSON args: {"hashes": [<sha256 hex>, ...]}
        Optional JSON args: {"reason": "..."}
        """
        hash_list = self._ensure_hashes(hashes)
        body: dict[str,
                   Any] = {
                       "hashes": hash_list
                   }
        if isinstance(reason, str) and reason.strip():
            body["reason"] = reason.strip()
        return self._post("/add_files/delete_files", data=body)

    def clear_file_deletion_record(self,
                                   hashes: Union[str,
                                                 Iterable[str]]) -> dict[str,
                                                                         Any]:
        """Clear Hydrus's file deletion record for the provided hashes.

        Hydrus Client API: POST /add_files/clear_file_deletion_record
        Required JSON args: {"hashes": [<sha256 hex>, ...]}
        """
        hash_list = self._ensure_hashes(hashes)
        body = {
            "hashes": hash_list
        }
        return self._post("/add_files/clear_file_deletion_record", data=body)

    def add_tag(
        self,
        hash: Union[str,
                    Iterable[str]],
        tags: Iterable[str],
        service_name: str
    ) -> dict[str,
              Any]:
        hash = self._ensure_hashes(hash)
        body = {
            "hashes": hash,
            "service_names_to_tags": {
                service_name: list(tags)
            }
        }
        return self._post("/add_tags/add_tags", data=body)

    def delete_tag(
        self,
        file_hashes: Union[str,
                           Iterable[str]],
        tags: Iterable[str],
        service_name: str,
        *,
        action: int = 1,
    ) -> dict[str,
              Any]:
        hashes = self._ensure_hashes(file_hashes)
        body = {
            "hashes": hashes,
            "service_names_to_actions_to_tags": {
                service_name: {
                    action: list(tags)
                }
            },
        }
        return self._post("/add_tags/add_tags", data=body)

    def add_tags_by_key(
        self,
        hash: Union[str,
                    Iterable[str]],
        tags: Iterable[str],
        service_key: str
    ) -> dict[str,
              Any]:
        hash = self._ensure_hashes(hash)
        body = {
            "hashes": hash,
            "service_keys_to_tags": {
                service_key: list(tags)
            }
        }
        return self._post("/add_tags/add_tags", data=body)

    def delete_tags_by_key(
        self,
        file_hashes: Union[str,
                           Iterable[str]],
        tags: Iterable[str],
        service_key: str,
        *,
        action: int = 1,
    ) -> dict[str,
              Any]:
        hashes = self._ensure_hashes(file_hashes)
        body = {
            "hashes": hashes,
            "service_keys_to_actions_to_tags": {
                service_key: {
                    action: list(tags)
                }
            },
        }
        return self._post("/add_tags/add_tags", data=body)

    def mutate_tags_by_key(
        self,
        hash: Union[str,
                    Iterable[str]],
        service_key: str,
        *,
        add_tags: Optional[Iterable[str]] = None,
        remove_tags: Optional[Iterable[str]] = None,
    ) -> dict[str,
              Any]:
        """Add or remove tags with a single /add_tags/add_tags call.

        Hydrus Client API: POST /add_tags/add_tags
        Use `service_keys_to_actions_to_tags` so the client can apply additions
        and removals in a single request (action '0' = add, '1' = remove).
        """
        hash_list = self._ensure_hashes(hash)
        def _clean(tags: Optional[Iterable[str]]) -> list[str]:
            if not tags:
                return []
            clean_list: list[str] = []
            for tag in tags:
                if not isinstance(tag, str):
                    continue
                text = tag.strip()
                if not text:
                    continue
                clean_list.append(text)
            return clean_list

        actions: dict[str, list[str]] = {}
        adds = _clean(add_tags)
        removes = _clean(remove_tags)
        if adds:
            actions["0"] = adds
        if removes:
            actions["1"] = removes
        if not actions:
            return {}
        body = {
            "hashes": hash_list,
            "service_keys_to_actions_to_tags": {
                str(service_key): actions
            },
        }
        return self._post("/add_tags/add_tags", data=body)

    def associate_url(self,
                      file_hashes: Union[str,
                                         Iterable[str]],
                      url: str) -> dict[str,
                                        Any]:
        hashes = self._ensure_hashes(file_hashes)
        if len(hashes) == 1:
            body = {
                "hash": hashes[0],
                "url_to_add": url
            }
            return self._post("/add_urls/associate_url", data=body)

        results: dict[str,
                      Any] = {}
        for file_hash in hashes:
            body = {
                "hash": file_hash,
                "url_to_add": url
            }
            results[file_hash] = self._post("/add_urls/associate_url", data=body)
        return {
            "batched": results
        }

    def get_url_info(self, url: str) -> dict[str, Any]:
        """Get information about a URL.

        Hydrus Client API: GET /add_urls/get_url_info
        Docs: https://hydrusnetwork.github.io/hydrus/developer_api.html#add_urls_get_url_info
        """
        url = str(url or "").strip()
        if not url:
            raise ValueError("url must not be empty")

        spec = HydrusRequestSpec(
            method="GET",
            endpoint="/add_urls/get_url_info",
            query={
                "url": url
            },
        )
        return cast(dict[str, Any], self._perform_request(spec))

    def delete_url(self,
                   file_hashes: Union[str,
                                      Iterable[str]],
                   url: str) -> dict[str,
                                     Any]:
        hashes = self._ensure_hashes(file_hashes)
        if len(hashes) == 1:
            body = {
                "hash": hashes[0],
                "url_to_delete": url
            }
            return self._post("/add_urls/associate_url", data=body)

        results: dict[str,
                      Any] = {}
        for file_hash in hashes:
            body = {
                "hash": file_hash,
                "url_to_delete": url
            }
            results[file_hash] = self._post("/add_urls/associate_url", data=body)
        return {
            "batched": results
        }

    def set_notes(
        self,
        file_hash: str,
        notes: dict[str,
                    str],
        *,
        merge_cleverly: bool = False,
        extend_existing_note_if_possible: bool = True,
        conflict_resolution: int = 3,
    ) -> dict[str,
              Any]:
        """Add or update notes associated with a file.

        Hydrus Client API: POST /add_notes/set_notes
        Required JSON args: {"hash": <sha256 hex>, "notes": {name: text}}
        """
        if not notes:
            raise ValueError("notes mapping must not be empty")

        file_hash = str(file_hash or "").strip().lower()
        if not file_hash:
            raise ValueError("file_hash must not be empty")

        body: dict[str,
                   Any] = {
                       "hash": file_hash,
                       "notes": notes
                   }

        if merge_cleverly:
            body["merge_cleverly"] = True
            body["extend_existing_note_if_possible"] = bool(
                extend_existing_note_if_possible
            )
            body["conflict_resolution"] = int(conflict_resolution)
        return self._post("/add_notes/set_notes", data=body)

    def delete_notes(
        self,
        file_hash: str,
        note_names: Sequence[str],
    ) -> dict[str,
              Any]:
        """Delete notes associated with a file.

        Hydrus Client API: POST /add_notes/delete_notes
        Required JSON args: {"hash": <sha256 hex>, "note_names": [..]}
        """
        names = [str(name) for name in note_names if str(name or "").strip()]
        if not names:
            raise ValueError("note_names must not be empty")

        file_hash = str(file_hash or "").strip().lower()
        if not file_hash:
            raise ValueError("file_hash must not be empty")

        body = {
            "hash": file_hash,
            "note_names": names
        }
        return self._post("/add_notes/delete_notes", data=body)

    def get_file_relationships(self, file_hash: str) -> dict[str, Any]:
        query = {
            "hash": file_hash
        }
        return self._get(
            "/manage_file_relationships/get_file_relationships",
            query=query
        )

    def set_relationship(
        self,
        hash_a: str,
        hash_b: str,
        relationship: Union[str,
                            int],
        do_default_content_merge: bool = False,
    ) -> dict[str,
              Any]:
        """Set a relationship between two files in Hydrus.

        This wraps Hydrus Client API: POST /manage_file_relationships/set_file_relationships.

        Hydrus relationship enum (per Hydrus developer API docs):
        - 0: set as potential duplicates
        - 1: set as false positives
        - 2: set as same quality (duplicates)
        - 3: set as alternates
        - 4: set A as better (duplicates)

        Args:
            hash_a: First file SHA256 hex
            hash_b: Second file SHA256 hex
            relationship: Relationship type as string or integer enum (0-4)
            do_default_content_merge: Whether to perform default duplicate content merge

        Returns:
            Response from Hydrus API
        """
        # Convert string relationship types to integers
        if isinstance(relationship, str):
            rel_map = {
                # Potential duplicates
                "potential": 0,
                "potentials": 0,
                "potential duplicate": 0,
                "potential duplicates": 0,
                # False positives
                "false positive": 1,
                "false_positive": 1,
                "false positives": 1,
                "false_positives": 1,
                "not related": 1,
                "not_related": 1,
                # Duplicates (same quality)
                "duplicate": 2,
                "duplicates": 2,
                "same quality": 2,
                "same_quality": 2,
                "equal": 2,
                # Alternates
                "alt": 3,
                "alternate": 3,
                "alternates": 3,
                "alternative": 3,
                "related": 3,
                # Better/worse (duplicates)
                "better": 4,
                "a better": 4,
                "a_better": 4,
                # Back-compat: some older call sites used 'king' for primary.
                # Hydrus does not accept 'king' as a relationship; this maps to 'A is better'.
                "king": 4,
            }
            relationship = rel_map.get(
                relationship.lower().strip(),
                3
            )  # Default to alternates

        body = {
            "relationships": [
                {
                    "hash_a": hash_a,
                    "hash_b": hash_b,
                    "relationship": relationship,
                    "do_default_content_merge": do_default_content_merge,
                }
            ]
        }
        return self._post(
            "/manage_file_relationships/set_file_relationships",
            data=body
        )

    def get_services(self) -> dict[str, Any]:
        return self._get("/get_services")

    def search_files(
        self,
        tags: Sequence[Any],
        *,
        file_service_name: str | None = None,
        return_hashes: bool = False,
        return_file_ids: bool = True,
        return_file_count: bool = False,
        include_current_tags: bool | None = None,
        include_pending_tags: bool | None = None,
        file_sort_type: int | None = None,
        file_sort_asc: bool | None = None,
        file_sort_key: str | None = None,
    ) -> dict[str,
              Any]:
        if not tags:
            raise ValueError("tags must not be empty")

        query: dict[str,
                    Any] = {}
        query_fields = [
            ("tags",
             tags, lambda v: json.dumps(list(v))),
            ("file_service_name",
             file_service_name, lambda v: v),
            ("return_hashes",
             return_hashes, lambda v: "true" if v else None),
            ("return_file_ids",
             return_file_ids, lambda v: "true" if v else None),
            ("return_file_count",
             return_file_count, lambda v: "true" if v else None),
            (
                "include_current_tags",
                include_current_tags,
                lambda v: "true" if v else "false" if v is not None else None,
            ),
            (
                "include_pending_tags",
                include_pending_tags,
                lambda v: "true" if v else "false" if v is not None else None,
            ),
            (
                "file_sort_type",
                file_sort_type, lambda v: str(v) if v is not None else None
            ),
            (
                "file_sort_asc",
                file_sort_asc,
                lambda v: "true" if v else "false" if v is not None else None,
            ),
            ("file_sort_key",
             file_sort_key, lambda v: v),
        ]

        for key, value, formatter in query_fields:
            if value is None or value == []:
                continue
            formatted = formatter(value)
            if formatted is not None:
                query[key] = formatted

        return self._get("/get_files/search_files", query=query)

    def fetch_file_metadata(
        self,
        *,
        file_ids: Sequence[int] | None = None,
        hashes: Sequence[str] | None = None,
        include_service_keys_to_tags: bool = True,
        include_tag_services: bool = False,
        include_file_services: bool = False,
        include_file_url: bool = False,
        include_duration: bool = True,
        include_size: bool = True,
        include_mime: bool = False,
        include_is_trashed: bool = False,
        include_notes: bool = False,
    ) -> dict[str,
              Any]:
        if not file_ids and not hashes:
            raise ValueError("Either file_ids or hashes must be provided")

        query: dict[str,
                    Any] = {}
        query_fields = [
            ("file_ids",
             file_ids, lambda v: json.dumps(list(v))),
            ("hashes",
             hashes, lambda v: json.dumps(list(v))),
            (
                "include_service_keys_to_tags",
                include_service_keys_to_tags,
                lambda v: "true" if v else None,
            ),
            (
                "include_tag_services",
                include_tag_services,
                lambda v: "true" if v else None,
            ),
            (
                "include_file_services",
                include_file_services,
                lambda v: "true" if v else None,
            ),
            ("include_file_url",
             include_file_url, lambda v: "true" if v else None),
            ("include_duration",
             include_duration, lambda v: "true" if v else None),
            ("include_size",
             include_size, lambda v: "true" if v else None),
            ("include_mime",
             include_mime, lambda v: "true" if v else None),
            (
                "include_is_trashed",
                include_is_trashed,
                lambda v: "true" if v else None,
            ),
            ("include_notes",
             include_notes, lambda v: "true" if v else None),
        ]

        for key, value, formatter in query_fields:
            if not value:
                continue
            formatted = formatter(value)
            if formatted is not None:
                query[key] = formatted

        return self._get("/get_files/file_metadata", query=query)

    def get_file_path(self, file_hash: str) -> dict[str, Any]:
        """Get the local file system path for a given file hash."""
        query = {
            "hash": file_hash
        }
        return self._get("/get_files/file_path", query=query)

    def file_url(self, file_hash: str) -> str:
        hash_param = quote(file_hash)
        # Don't append access_key parameter for file downloads - use header instead
        url = f"{self.url}/get_files/file?hash={hash_param}"
        return url

    def thumbnail_url(self, file_hash: str) -> str:
        hash_param = quote(file_hash)
        # Don't append access_key parameter for file downloads - use header instead
        url = f"{self.url}/get_files/thumbnail?hash={hash_param}"
        return url


HydrusCliOptionsT = TypeVar("HydrusCliOptionsT", bound="HydrusCliOptions")


@dataclass(slots=True)
class HydrusCliOptions:
    url: str
    method: str
    access_key: str
    accept: str
    timeout: float
    content_type: str | None
    body_bytes: bytes | None = None
    body_path: Path | None = None
    debug: bool = False

    @classmethod
    def from_namespace(
        cls: Type[HydrusCliOptionsT],
        namespace: Any
    ) -> HydrusCliOptionsT:
        accept_header = namespace.accept or "application/cbor"
        body_bytes: bytes | None = None
        body_path: Path | None = None
        if namespace.body_file:
            body_path = Path(namespace.body_file)
        elif namespace.body is not None:
            body_bytes = namespace.body.encode("utf-8")
        return cls(
            url=namespace.url,
            method=namespace.method.upper(),
            access_key=namespace.access_key or "",
            accept=accept_header,
            timeout=namespace.timeout,
            content_type=namespace.content_type,
            body_bytes=body_bytes,
            body_path=body_path,
            debug=bool(os.environ.get("DOWNLOW_DEBUG")),
        )


def hydrus_request(args, parser) -> int:
    if args.body and args.body_file:
        parser.error("Only one of --body or --body-file may be supplied")

    options = HydrusCliOptions.from_namespace(args)

    parsed = urlsplit(options.url)
    if parsed.scheme not in ("http", "https"):
        parser.error("Only http and https url are supported")
    if not parsed.hostname:
        parser.error("Invalid Hydrus URL")

    headers: dict[str,
                  str] = {}
    if options.access_key:
        headers["Hydrus-Client-API-Access-Key"] = options.access_key
    if options.accept:
        headers["Accept"] = options.accept

    request_body_bytes: bytes | None = None
    body_path: Path | None = None
    if options.body_path is not None:
        body_path = options.body_path
        if not body_path.is_file():
            parser.error(f"File not found: {body_path}")
        headers.setdefault(
            "Content-Type",
            options.content_type or "application/octet-stream"
        )
        headers["Content-Length"] = str(body_path.stat().st_size)
    elif options.body_bytes is not None:
        request_body_bytes = options.body_bytes
        headers["Content-Type"] = options.content_type or "application/json"
        assert request_body_bytes is not None
        headers["Content-Length"] = str(len(request_body_bytes))
    elif options.content_type:
        headers["Content-Type"] = options.content_type

    if parsed.username or parsed.password:
        userinfo = f"{parsed.username or ''}:{parsed.password or ''}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(userinfo).decode("ascii")

    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    connection_cls = (
        http.client.HTTPSConnection
        if parsed.scheme == "https" else http.client.HTTPConnection
    )
    host = parsed.hostname or "localhost"
    connection = connection_cls(host, port, timeout=options.timeout)

    if options.debug:
        log(
            f"Hydrus connecting to {parsed.scheme}://{host}:{port}{path}",
            file=sys.stderr
        )
    response_bytes: bytes = b""
    content_type = ""
    status = 0
    try:
        if body_path is not None:
            with body_path.open("rb") as handle:
                if options.debug:
                    size_hint = headers.get("Content-Length", "unknown")
                    log(
                        f"Hydrus sending file body ({size_hint} bytes)",
                        file=sys.stderr
                    )
                connection.putrequest(options.method, path)
                host_header = host
                if (parsed.scheme == "http"
                        and port not in (80,
                                         None)) or (parsed.scheme == "https"
                                                    and port not in (443,
                                                                     None)):
                    host_header = f"{host}:{port}"
                connection.putheader("Host", host_header)
                for key, value in headers.items():
                    if value:
                        connection.putheader(key, value)
                connection.endheaders()
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    connection.send(chunk)
                if options.debug:
                    log(
                        "[downlow.py] Hydrus upload complete; awaiting response",
                        file=sys.stderr
                    )
        else:
            if options.debug:
                size_hint = "none" if request_body_bytes is None else str(
                    len(request_body_bytes)
                )
                log(f"Hydrus sending request body bytes={size_hint}", file=sys.stderr)
            sanitized_headers = {
                k: v
                for k, v in headers.items() if v
            }
            connection.request(
                options.method,
                path,
                body=request_body_bytes,
                headers=sanitized_headers
            )
        response = connection.getresponse()
        status = response.status
        response_bytes = response.read()
        if options.debug:
            log(
                f"Hydrus response received ({len(response_bytes)} bytes)",
                file=sys.stderr
            )
        content_type = response.getheader("Content-Type", "")
    except (OSError, http.client.HTTPException) as exc:
        log(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    content_type_lower = (content_type or "").split(";", 1)[0].strip().lower()
    accept_value = options.accept or ""
    expect_cbor = "cbor" in (content_type_lower or "") or "cbor" in accept_value.lower()
    payload = None
    decode_error: Exception | None = None
    if response_bytes:
        if expect_cbor:
            try:
                payload = decode_cbor(response_bytes)
            except Exception as exc:  # pragma: no cover - library errors surfaced
                decode_error = exc
        if payload is None and not expect_cbor:
            try:
                payload = json.loads(response_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = response_bytes.decode("utf-8", "replace")
        elif payload is None and expect_cbor and decode_error is not None:
            log(
                f"Expected CBOR response but decoding failed: {decode_error}",
                file=sys.stderr
            )
            return 1
    json_ready = jsonify(payload) if isinstance(payload, (dict, list)) else payload
    if options.debug:
        log(f"Hydrus {options.method} {options.url} -> {status}", file=sys.stderr)
    if isinstance(json_ready, (dict, list)):
        log(json.dumps(json_ready, ensure_ascii=False))
    elif json_ready is None:
        log("{}")
    else:
        log(json.dumps({
            "value": json_ready
        },
                       ensure_ascii=False))
    return 0 if 200 <= status < 400 else 1


def hydrus_export(args, _parser) -> int:
    from SYS.metadata import apply_mutagen_metadata, build_ffmpeg_command, prepare_ffmpeg_metadata

    output_path: Path = args.output
    original_suffix = output_path.suffix
    target_dir = output_path.parent
    metadata_payload: Optional[dict[str, Any]] = None
    metadata_raw = getattr(args, "metadata_json", None)
    if metadata_raw:
        try:
            parsed = json.loads(metadata_raw)
        except json.JSONDecodeError as exc:
            log(f"Invalid metadata JSON: {exc}", file=sys.stderr)
            return 1
        if isinstance(parsed, dict):
            metadata_payload = parsed
        else:
            log("[downlow.py] Metadata JSON must decode to an object", file=sys.stderr)
            return 1
    ffmpeg_metadata = prepare_ffmpeg_metadata(metadata_payload)

    def _normalize_ext(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not cleaned.startswith("."):  # tolerate inputs like "mp4"
            cleaned = "." + cleaned.lstrip(".")
        return cleaned

    def _extension_from_mime(mime: Optional[str]) -> Optional[str]:
        if not mime:
            return None
        mime_map = {
            # Images / bitmaps
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/avif": ".avif",
            "image/jxl": ".jxl",  # JPEG XL
            "image/bmp": ".bmp",
            "image/heic": ".heic",
            "image/heif": ".heif",
            "image/x-icon": ".ico",
            "image/vnd.microsoft.icon": ".ico",
            "image/qoi": ".qoi",  # Quite OK Image
            "image/tiff": ".tiff",
            "image/svg+xml": ".svg",
            "image/vnd.adobe.photoshop": ".psd",
            # Animation / sequence variants
            "image/apng": ".apng",
            "image/avif-sequence": ".avifs",
            "image/heic-sequence": ".heics",
            "image/heif-sequence": ".heifs",
            # Video
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
            "video/ogg": ".ogv",
            "video/mpeg": ".mpeg",
            "video/x-msvideo": ".avi",
            "video/x-flv": ".flv",
            "video/x-matroska": ".mkv",
            "video/x-ms-wmv": ".wmv",
            "video/vnd.rn-realvideo": ".rv",
            # Audio
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/ogg": ".ogg",
            "audio/flac": ".flac",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/x-ms-wma": ".wma",
            "audio/x-tta": ".tta",
            "audio/vnd.wave": ".wav",
            "audio/x-wavpack": ".wv",
            # Documents / office
            "application/pdf": ".pdf",
            "application/epub+zip": ".epub",
            "application/vnd.djvu": ".djvu",
            "application/rtf": ".rtf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/msword": ".doc",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.ms-powerpoint": ".ppt",
            # Archive / comicbook / zip-like
            "application/zip": ".zip",
            "application/x-7z-compressed": ".7z",
            "application/x-rar-compressed": ".rar",
            "application/gzip": ".gz",
            "application/x-tar": ".tar",
            "application/x-cbz": ".cbz",  # often just ZIP with images; CBZ is not an official mime type but used as mapping
            # App / project / other
            "application/clip": ".clip",  # Clip Studio
            "application/x-krita": ".kra",
            "application/x-procreate": ".procreate",
            "application/x-shockwave-flash": ".swf",
        }

        return mime_map.get(mime.lower())

    def _extract_hash(file_url: str) -> Optional[str]:
        match = re.search(r"[?&]hash=([0-9a-fA-F]+)", file_url)
        return match.group(1) if match else None

    # Ensure output and temp directories exist using global helper
    for dir_path in [target_dir, Path(args.tmp_dir) if args.tmp_dir else target_dir]:
        try:
            ensure_directory(dir_path)
        except RuntimeError as exc:
            log(f"{exc}", file=sys.stderr)
            return 1

    source_suffix = _normalize_ext(getattr(args, "source_ext", None))
    if source_suffix and source_suffix.lower() == ".bin":
        source_suffix = None

    if source_suffix is None:
        hydrus_url = getattr(args, "hydrus_url", None)
        if not hydrus_url:
            try:
                from SYS.config import load_config, get_hydrus_url

                hydrus_url = get_hydrus_url(load_config())
            except Exception as exc:
                hydrus_url = None
                if os.environ.get("DOWNLOW_DEBUG"):
                    log(
                        f"hydrus-export could not load Hydrus URL: {exc}",
                        file=sys.stderr
                    )
        if hydrus_url:
            try:
                setattr(args, "hydrus_url", hydrus_url)
            except Exception:
                pass
        resolved_suffix: Optional[str] = None
        file_hash = getattr(args, "file_hash", None) or _extract_hash(args.file_url)
        if hydrus_url and file_hash:
            try:
                client = HydrusNetwork(
                    url=hydrus_url,
                    access_key=args.access_key,
                    timeout=args.timeout
                )
                meta_response = client.fetch_file_metadata(
                    hashes=[file_hash],
                    include_mime=True
                )
                entries = meta_response.get("metadata") if isinstance(
                    meta_response,
                    dict
                ) else None
                if isinstance(entries, list) and entries:
                    entry = entries[0]
                    ext_value = _normalize_ext(
                        entry.get("ext") if isinstance(entry,
                                                       dict) else None
                    )
                    if ext_value:
                        resolved_suffix = ext_value
                    else:
                        mime_value = entry.get("mime"
                                               ) if isinstance(entry,
                                                               dict) else None
                        resolved_suffix = _extension_from_mime(mime_value)
            except Exception as exc:  # pragma: no cover - defensive
                if os.environ.get("DOWNLOW_DEBUG"):
                    log(f"hydrus metadata fetch failed: {exc}", file=sys.stderr)
        if not resolved_suffix:
            fallback_suffix = _normalize_ext(original_suffix)
            if fallback_suffix and fallback_suffix.lower() == ".bin":
                fallback_suffix = None
            resolved_suffix = fallback_suffix or ".hydrus"
        source_suffix = resolved_suffix

    suffix = source_suffix or ".hydrus"
    if suffix and output_path.suffix.lower() in {"",
                                                 ".bin"}:
        if output_path.suffix.lower() != suffix.lower():
            output_path = output_path.with_suffix(suffix)
            target_dir = output_path.parent
    # Determine temp directory (prefer provided tmp_dir, fallback to output location)
    temp_dir = Path(getattr(args, "tmp_dir", None) or target_dir)
    try:
        ensure_directory(temp_dir)
    except RuntimeError:
        temp_dir = target_dir
    temp_file = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=suffix,
        dir=str(temp_dir)
    )
    temp_path = Path(temp_file.name)
    temp_file.close()
    downloaded_bytes = 0
    headers = {
        "Hydrus-Client-API-Access-Key": args.access_key,
    }
    try:
        downloaded_bytes = download_hydrus_file(
            args.file_url,
            headers,
            temp_path,
            args.timeout
        )
        if os.environ.get("DOWNLOW_DEBUG"):
            log(f"hydrus-export downloaded {downloaded_bytes} bytes", file=sys.stderr)
    except httpx.RequestError as exc:
        if temp_path.exists():
            temp_path.unlink()
        log(f"hydrus-export download failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - unexpected
        if temp_path.exists():
            temp_path.unlink()
        log(f"hydrus-export error: {exc}", file=sys.stderr)
        return 1
    ffmpeg_log: Optional[str] = None
    converted_tmp: Optional[Path] = None
    try:
        final_target = unique_path(output_path)
        if args.format == "copy":
            shutil.move(str(temp_path), str(final_target))
            result_path = final_target
        else:
            ffmpeg_path = shutil.which("ffmpeg")
            if not ffmpeg_path:
                raise RuntimeError("ffmpeg executable not found in PATH")
            converted_tmp = final_target.with_suffix(final_target.suffix + ".part")
            if converted_tmp.exists():
                converted_tmp.unlink()
            max_width = args.max_width if args.max_width and args.max_width > 0 else 0
            cmd = build_ffmpeg_command(
                ffmpeg_path,
                temp_path,
                converted_tmp,
                args.format,
                max_width,
                metadata=ffmpeg_metadata if ffmpeg_metadata else None,
            )
            if os.environ.get("DOWNLOW_DEBUG"):
                log(f"ffmpeg command: {' '.join(cmd)}", file=sys.stderr)
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
            )
            ffmpeg_log = (completed.stderr or "").strip()
            if completed.returncode != 0:
                error_details = ffmpeg_log or (completed.stdout or "").strip()
                raise RuntimeError(
                    f"ffmpeg failed with exit code {completed.returncode}" +
                    (f": {error_details}" if error_details else "")
                )
            shutil.move(str(converted_tmp), str(final_target))
            result_path = final_target
        apply_mutagen_metadata(result_path, ffmpeg_metadata, args.format)
        result_size = result_path.stat().st_size if result_path.exists() else None
        payload: dict[str,
                      object] = {
                          "output": str(result_path)
                      }
        if downloaded_bytes:
            payload["source_bytes"] = downloaded_bytes
        if result_size is not None:
            payload["size_bytes"] = result_size
        if metadata_payload:
            payload["metadata_keys"] = sorted(ffmpeg_metadata.keys()
                                              ) if ffmpeg_metadata else []
        log(json.dumps(payload, ensure_ascii=False))
        if ffmpeg_log:
            log(ffmpeg_log, file=sys.stderr)
        return 0
    except Exception as exc:
        log(f"hydrus-export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        if converted_tmp and converted_tmp.exists():
            try:
                converted_tmp.unlink()
            except OSError:
                pass


# ============================================================================
# Hydrus Wrapper Functions - Utilities for client initialization and config
# ============================================================================
# This section consolidates functions formerly in hydrus_wrapper.py
# Provides: supported filetypes, client initialization, caching, service resolution

# Official Hydrus supported filetypes
# Source: https://hydrusnetwork.github.io/hydrus/filetypes.html
SUPPORTED_FILETYPES = {
    # Images
    "image": {
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".avif": "image/avif",
        ".jxl": "image/jxl",
        ".bmp": "image/bmp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".ico": "image/x-icon",
        ".qoi": "image/qoi",
        ".tiff": "image/tiff",
    },
    # Animated Images
    "animation": {
        ".apng": "image/apng",
        ".avifs": "image/avif-sequence",
        ".heics": "image/heic-sequence",
        ".heifs": "image/heif-sequence",
    },
    # Video
    "video": {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".flv": "video/x-flv",
        ".mov": "video/quicktime",
        ".mpeg": "video/mpeg",
        ".ogv": "video/ogg",
        ".rm": "video/vnd.rn-realvideo",
        ".wmv": "video/x-ms-wmv",
    },
    # Audio
    "audio": {
        ".mp3": "audio/mp3",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mka": "audio/x-matroska",
        ".mkv": "audio/x-matroska",
        ".mp4": "audio/mp4",
        ".ra": "audio/vnd.rn-realaudio",
        ".tta": "audio/x-tta",
        ".wav": "audio/x-wav",
        ".wv": "audio/wavpack",
        ".wma": "audio/x-ms-wma",
    },
    # Applications & Documents
    "application": {
        ".swf": "application/x-shockwave-flash",
        ".pdf": "application/pdf",
        ".epub": "application/epub+zip",
        ".djvu": "image/vnd.djvu",
        ".docx":
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx":
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".doc": "application/msword",
        ".xls": "application/vnd.ms-excel",
        ".ppt": "application/vnd.ms-powerpoint",
        ".rtf": "application/rtf",
    },
    # Image Project Files
    "project": {
        ".clip": "application/clip1",
        ".kra": "application/x-krita",
        ".procreate": "application/x-procreate1",
        ".psd": "image/vnd.adobe.photoshop",
        ".sai2": "application/sai21",
        ".svg": "image/svg+xml",
        ".xcf": "application/x-xcf",
    },
    # Archives
    "archive": {
        ".cbz": "application/vnd.comicbook+zip",
        ".7z": "application/x-7z-compressed",
        ".gz": "application/gzip",
        ".rar": "application/vnd.rar",
        ".zip": "application/zip",
    },
}

# Flatten to get all supported extensions
ALL_SUPPORTED_EXTENSIONS = set(GLOBAL_SUPPORTED_EXTENSIONS)

# Global Hydrus client cache to reuse session keys
_hydrus_client_cache: dict[str,
                           Any] = {}

# Cache Hydrus availability across the session
_HYDRUS_AVAILABLE: Optional[bool] = None
_HYDRUS_UNAVAILABLE_REASON: Optional[str] = None


def reset_cache() -> None:
    """Reset the availability cache (useful for testing)."""
    global _HYDRUS_AVAILABLE, _HYDRUS_UNAVAILABLE_REASON
    _HYDRUS_AVAILABLE = None
    _HYDRUS_UNAVAILABLE_REASON = None


def is_available(config: dict[str,
                              Any],
                 use_cache: bool = True) -> tuple[bool,
                                                  Optional[str]]:
    """Check if Hydrus is available and accessible.

    Performs a lightweight probe to verify:
    - At least one configured Hydrus instance has URL + API configured
    - A TCP connection to that instance's host/port can be established

    Results are cached per session unless use_cache=False.

    Args:
        config: Configuration dict with Hydrus settings
        use_cache: If True, use cached result from previous probe

    Returns:
        Tuple of (is_available: bool, reason: Optional[str])
        reason is None if available, or an error message if not
    """
    global _HYDRUS_AVAILABLE, _HYDRUS_UNAVAILABLE_REASON

    if use_cache and _HYDRUS_AVAILABLE is not None:
        return _HYDRUS_AVAILABLE, _HYDRUS_UNAVAILABLE_REASON

    # Use new config helpers first, fallback to old method
    from SYS.config import get_hydrus_url, get_hydrus_access_key

    # Collect candidate instances (prioritize 'home') from plugin/provider config.
    provider_block = (config or {}).get("plugin")
    if not isinstance(provider_block, dict):
        provider_block = (config or {}).get("provider")
    hydrus_block = provider_block.get("hydrusnetwork") if isinstance(provider_block, dict) else {}

    candidate_names: list[str] = []
    if isinstance(hydrus_block, dict) and hydrus_block:
        names = [n for n, v in hydrus_block.items() if isinstance(v, dict)]
        if "home" in names:
            candidate_names = ["home"] + [n for n in names if n != "home"]
        else:
            candidate_names = names

    # If no configured instances, keep previous behavior for clearer message
    if not candidate_names:
        reason = "Hydrus URL not configured (check config.conf store.hydrusnetwork.home.URL)"
        _HYDRUS_AVAILABLE = False
        _HYDRUS_UNAVAILABLE_REASON = reason
        return False, reason

    timeout_raw = config.get("HydrusNetwork_Request_Timeout")
    try:
        timeout = float(timeout_raw) if timeout_raw is not None else 5.0
    except (TypeError, ValueError):
        timeout = 5.0

    import socket
    from urllib.parse import urlparse

    errors: list[str] = []

    for name in candidate_names:
        url = (get_hydrus_url(config, name) or "").strip()
        access_key = get_hydrus_access_key(config, name) or ""
        if not url:
            errors.append(f"Hydrus URL not configured for instance '{name}'")
            continue
        if not access_key:
            errors.append(f"Hydrus access key not configured for instance '{name}'")
            continue

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                result = sock.connect_ex((hostname, port))
                if result == 0:
                    _HYDRUS_AVAILABLE = True
                    _HYDRUS_UNAVAILABLE_REASON = None
                    return True, None
                errors.append(f"Cannot connect to {hostname}:{port} (instance '{name}')")
            finally:
                sock.close()
        except Exception as exc:
            errors.append(str(exc))
            continue

    # No candidate succeeded
    _HYDRUS_AVAILABLE = False
    reason = "; ".join(errors[:3]) if errors else "No Hydrus instances configured with URL and API"
    _HYDRUS_UNAVAILABLE_REASON = reason
    return False, reason


def is_hydrus_available(config: dict[str, Any]) -> bool:
    """Check if Hydrus is available without raising.

    Args:
        config: Configuration dict

    Returns:
        True if Hydrus is available, False otherwise
    """
    available, _ = is_available(config)
    return available


def get_client(config: dict[str, Any], instance_name: str | None = None) -> HydrusNetwork:
    """Create and return a Hydrus client.

    If `instance_name` is provided, return a client for that named instance.
    If omitted, prefer the 'home' instance when configured, otherwise fall back
    to the first configured Hydrus instance found in `config['store']['hydrusnetwork']`.

    Uses access-key authentication by default (no session key acquisition).
    A session key may still be acquired explicitly by calling
    `HydrusNetwork.ensure_session_key()`.

    Args:
        config: Configuration dict with Hydrus settings
        instance_name: Optional named Hydrus instance to use (e.g. 'home')

    Returns:
        HydrusNetwork client instance

    Raises:
        RuntimeError: If Hydrus is not configured or unavailable
    """
    # Perform a lightweight availability check first - this checks any configured instance.
    available, reason = is_available(config)
    # We will still attempt to instantiate a client for a configured instance even if the
    # availability probe reported unreachable (this keeps behavior resilient in mixed
    # network setups), but if no configured instance exists we'll raise below.

    from SYS.config import get_hydrus_url, get_hydrus_access_key

    chosen_instance: str | None = None

    if instance_name:
        chosen_instance = str(instance_name).strip()
        # Validate existence of configuration
        url_candidate = (get_hydrus_url(config, chosen_instance) or "").strip()
        if not url_candidate:
            raise RuntimeError(f"Hydrus URL is not configured for instance '{chosen_instance}'")
        access_key_candidate = get_hydrus_access_key(config, chosen_instance) or ""
        if not access_key_candidate:
            raise RuntimeError(f"Hydrus access key is not configured for instance '{chosen_instance}'")
    else:
        # Determine candidate instances from config and prefer 'home' when present.
        provider_block = (config or {}).get("plugin")
        if not isinstance(provider_block, dict):
            provider_block = (config or {}).get("provider")
        hydrus_block = provider_block.get("hydrusnetwork") if isinstance(provider_block, dict) else {}
        candidate_names: list[str] = []
        if isinstance(hydrus_block, dict) and hydrus_block:
            names = [n for n, v in hydrus_block.items() if isinstance(v, dict)]
            if "home" in names:
                candidate_names = ["home"] + [n for n in names if n != "home"]
            else:
                candidate_names = names

        # Try to pick the first instance with URL+API configured
        for name in candidate_names:
            url_candidate = (get_hydrus_url(config, name) or "").strip()
            access_key_candidate = get_hydrus_access_key(config, name) or ""
            if url_candidate and access_key_candidate:
                chosen_instance = name
                break

        # If nothing suitable found in config, fall back to 'home' behavior (for backwards compatibility)
        if chosen_instance is None:
            hydrus_url = (get_hydrus_url(config, "home") or "").strip()
            if not hydrus_url:
                raise RuntimeError(
                    "Hydrus URL is not configured (check config.conf store.hydrusnetwork.home.URL)"
                )
            chosen_instance = "home"

    # Now we have a chosen instance name; resolve its URL and access key (these validations are defensive)
    hydrus_url = (get_hydrus_url(config, chosen_instance) or "").strip()
    access_key = get_hydrus_access_key(config, chosen_instance) or ""

    if not hydrus_url:
        raise RuntimeError(f"Hydrus URL is not configured for instance '{chosen_instance}'")
    if not access_key:
        raise RuntimeError(f"Hydrus access key is not configured for instance '{chosen_instance}'")

    timeout_raw = config.get("HydrusNetwork_Request_Timeout")
    try:
        timeout = float(timeout_raw) if timeout_raw is not None else 60.0
    except (TypeError, ValueError):
        timeout = 60.0

    # Create cache key from URL, access key, and instance name
    cache_key = f"{hydrus_url}#{access_key}#{chosen_instance}"

    # Check if we have a cached client
    if cache_key in _hydrus_client_cache:
        return _hydrus_client_cache[cache_key]

    # Create new client and cache it
    client = HydrusNetwork(hydrus_url, access_key, timeout, instance_name=chosen_instance)
    _hydrus_client_cache[cache_key] = client
    return client

    return client


def get_tag_service_name(config: dict[str, Any]) -> str:
    """Get the name of the tag service to use for tagging operations.

    Currently always returns "my tags" to avoid remote service errors.

    Args:
        config: Configuration dict (not currently used)

    Returns:
        Service name string, typically "my tags"
    """
    # Always use 'my tags' to avoid remote service errors
    return "my tags"


def normalize_hydrus_service_key(value: Any) -> Optional[str]:
    """Return a Hydrus service key as lowercase hex.

    Default services use short keys (hex of ASCII names like ``local tags``),
    not 32-byte/64-hex keys.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.hex()
        except Exception:
            return None
    try:
        text = str(value).strip().lower()
    except Exception:
        return None
    if text.startswith("0x"):
        text = text[2:]
    if text and len(text) % 2 == 0 and all(ch in "0123456789abcdef" for ch in text):
        return text
    return None


def get_tag_service_key(client: HydrusNetwork,
                        fallback_name: str = "my tags") -> Optional[str]:
    """Get the service key for a named tag service.

    Queries the Hydrus client's services and finds the service key matching
    the given name.

    Args:
        client: HydrusClient instance
        fallback_name: Name of the service to find (e.g., "my tags")

    Returns:
        Service key string if found, None otherwise
    """
    try:
        services = client.get_services()
    except Exception:
        return None

    if not isinstance(services, dict):
        return None

    def _normalize_name(value: Any) -> str:
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="ignore").strip().lower()
            except Exception:
                return ""
        try:
            return str(value or "").strip().lower()
        except Exception:
            return ""

    target_name = _normalize_name(fallback_name)
    if not target_name:
        target_name = "my tags"

    service_keys_to_names = services.get("service_keys_to_names")
    if isinstance(service_keys_to_names, dict):
        for key, value in service_keys_to_names.items():
            if _normalize_name(value) != target_name:
                continue
            normalized_key = normalize_hydrus_service_key(key)
            if normalized_key:
                return normalized_key
            text = str(key or "").strip()
            if text:
                return text

    for group in services.values():
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            name = _normalize_name(item.get("name"))
            if name != target_name:
                continue
            key_raw = item.get("service_key") or item.get("key")
            normalized_key = normalize_hydrus_service_key(key_raw)
            if normalized_key:
                return normalized_key
            text = str(key_raw or "").strip()
            if text:
                return text

    return None


def is_request_error(exc: Exception) -> bool:
    """Check if an exception is a Hydrus request error.

    Args:
        exc: Exception to check

    Returns:
        True if this is a HydrusRequestError
    """
    return isinstance(exc, HydrusRequestError)


CHUNK_SIZE = 1024 * 1024  # 1 MiB


def download_hydrus_file(
    file_url: str,
    headers: dict[str,
                  str],
    destination: Path,
    timeout: float
) -> int:
    """Download *file_url* into *destination* returning the byte count with progress bar."""
    from SYS.progress import print_progress, print_final_progress

    downloaded = 0
    start_time = time.time()
    last_update = start_time

    # Try to get file size from headers if available
    file_size = None
    with HTTPClient(timeout=timeout, headers=headers) as client:
        response = client.get(file_url)
        response.raise_for_status()

        # Try to get size from content-length header
        try:
            file_size = int(response.headers.get("content-length", 0))
        except (ValueError, TypeError):
            file_size = None

        filename = destination.name

        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(CHUNK_SIZE):
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)

                # Update progress every 0.5 seconds if we know total size
                if file_size:
                    now = time.time()
                    if now - last_update >= 0.5:
                        elapsed = now - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        print_progress(filename, downloaded, file_size, speed)
                        last_update = now

    # Print final progress line if we tracked it
    if file_size:
        elapsed = time.time() - start_time
        print_final_progress(filename, file_size, elapsed)

    return downloaded


# ============================================================================
# Hydrus metadata helpers (moved from SYS.metadata)
# ============================================================================


def _normalize_hash(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    if not candidate:
        raise ValueError("Hydrus hash is required")
    if len(candidate) != 64 or any(ch not in "0123456789abcdef" for ch in candidate):
        raise ValueError("Hydrus hash must be a 64-character hex string")
    return candidate


def _normalize_tag(tag: Any) -> Optional[str]:
    if tag is None:
        return None
    if isinstance(tag, str):
        candidate = tag.strip()
    else:
        candidate = str(tag).strip()
    return candidate or None


def _dedup_tags_by_namespace(tags: List[str], keep_first: bool = True) -> List[str]:
    if not tags:
        return []

    namespace_to_tags: Dict[Optional[str], List[Tuple[int, str]]] = {}
    first_appearance: Dict[Optional[str], int] = {}

    for idx, tag in enumerate(tags):
        namespace: Optional[str] = tag.split(":", 1)[0] if ":" in tag else None
        if namespace not in first_appearance:
            first_appearance[namespace] = idx
        if namespace not in namespace_to_tags:
            namespace_to_tags[namespace] = []
        namespace_to_tags[namespace].append((idx, tag))

    result: List[Tuple[int, str]] = []
    for namespace, tag_list in namespace_to_tags.items():
        chosen_tag = tag_list[0][1] if keep_first else tag_list[-1][1]
        result.append((first_appearance[namespace], chosen_tag))

    result.sort(key=lambda x: x[0])
    return [tag for _, tag in result]


def _extract_tag_services(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    tags_section = entry.get("tags")
    services: List[Dict[str, Any]] = []
    if not isinstance(tags_section, dict):
        return services
    names_map = tags_section.get("service_keys_to_names")
    if not isinstance(names_map, dict):
        names_map = {}

    def get_record(service_key: Optional[str], service_name: Optional[str]) -> Dict[str, Any]:
        key_lower = service_key.lower() if isinstance(service_key, str) else None
        name_lower = service_name.lower() if isinstance(service_name, str) else None
        for record in services:
            existing_key = record.get("service_key")
            if key_lower and isinstance(existing_key, str) and existing_key.lower() == key_lower:
                if service_name and not record.get("service_name"):
                    record["service_name"] = service_name
                return record
            existing_name = record.get("service_name")
            if name_lower and isinstance(existing_name, str) and existing_name.lower() == name_lower:
                if service_key and not record.get("service_key"):
                    record["service_key"] = service_key
                return record
        record = {
            "service_key": service_key,
            "service_name": service_name,
            "tags": [],
        }
        services.append(record)
        return record

    def _iter_current_status_lists(container: Any) -> Iterable[List[Any]]:
        if isinstance(container, dict):
            for status_key, tags_list in container.items():
                if str(status_key) != "0":
                    continue
                if isinstance(tags_list, list):
                    yield tags_list
        elif isinstance(container, list):
            yield container

    statuses_map = tags_section.get("service_keys_to_statuses_to_tags")
    if isinstance(statuses_map, dict):
        for service_key, status_map in statuses_map.items():
            record = get_record(service_key if isinstance(service_key, str) else None, names_map.get(service_key))
            for tags_list in _iter_current_status_lists(status_map):
                for tag in tags_list:
                    normalized = _normalize_tag(tag)
                    if normalized:
                        record["tags"].append(normalized)

    ignored_keys = {
        "service_keys_to_statuses_to_tags",
        "service_keys_to_statuses_to_display_tags",
        "service_keys_to_display_friendly_tags",
        "service_keys_to_names",
        "tag_display_types_to_namespaces",
        "namespace_display_string_lookup",
        "tag_display_decoration_colour_lookup",
    }

    for key, service in tags_section.items():
        if key in ignored_keys:
            continue
        if isinstance(service, dict):
            service_key = service.get("service_key") or (key if isinstance(key, str) else None)
            service_name = service.get("service_name") or service.get("name") or names_map.get(service_key)
            record = get_record(service_key if isinstance(service_key, str) else None, service_name)
            storage = service.get("storage_tags") or service.get("statuses_to_tags") or service.get("tags")
            if isinstance(storage, dict):
                for tags_list in _iter_current_status_lists(storage):
                    for tag in tags_list:
                        normalized = _normalize_tag(tag)
                        if normalized:
                            record["tags"].append(normalized)
            elif isinstance(storage, list):
                for tag in storage:
                    normalized = _normalize_tag(tag)
                    if normalized:
                        record["tags"].append(normalized)

    for record in services:
        record["tags"] = _dedup_tags_by_namespace(record["tags"], keep_first=True)
    return services


def _select_primary_tags(
    services: List[Dict[str, Any]],
    aggregated: List[str],
    prefer_service: Optional[str]
) -> Tuple[Optional[str], List[str]]:
    prefer_lower = prefer_service.lower() if isinstance(prefer_service, str) else None
    if prefer_lower:
        for record in services:
            name = record.get("service_name")
            if isinstance(name, str) and name.lower() == prefer_lower and record["tags"]:
                return record.get("service_key"), record["tags"]
    for record in services:
        if record["tags"]:
            return record.get("service_key"), record["tags"]
    return None, aggregated


def _derive_title(
    tags_primary: List[str],
    tags_aggregated: List[str],
    entry: Dict[str, Any]
) -> Optional[str]:
    for source in (tags_primary, tags_aggregated):
        for tag in source:
            namespace, sep, value = tag.partition(":")
            if sep and namespace and namespace.lower() == "title":
                cleaned = value.strip()
                if cleaned:
                    return cleaned
    for key in (
        "title",
        "display_name",
        "pretty_name",
        "original_display_filename",
        "original_filename",
    ):
        raw_val = entry.get(key)
        if isinstance(raw_val, str):
            cleaned = raw_val.strip()
            if cleaned:
                return cleaned
    return None


def _derive_clip_time(
    tags_primary: List[str],
    tags_aggregated: List[str],
    entry: Dict[str, Any]
) -> Optional[str]:
    namespaces = {"clip", "clip_time", "cliptime"}
    for source in (tags_primary, tags_aggregated):
        for tag in source:
            namespace, sep, value = tag.partition(":")
            if sep and namespace and namespace.lower() in namespaces:
                cleaned = value.strip()
                if cleaned:
                    return cleaned
    clip_value = entry.get("clip_time")
    if isinstance(clip_value, str):
        cleaned_clip = clip_value.strip()
        if cleaned_clip:
            return cleaned_clip
    return None


def _summarize_hydrus_entry(
    entry: Dict[str, Any],
    prefer_service: Optional[str]
) -> Tuple[Dict[str, Any], List[str], Optional[str], Optional[str], Optional[str]]:
    services = _extract_tag_services(entry)
    aggregated: List[str] = []
    seen: Set[str] = set()
    for record in services:
        for tag in record["tags"]:
            if tag not in seen:
                seen.add(tag)
                aggregated.append(tag)
    service_key, primary_tags = _select_primary_tags(services, aggregated, prefer_service)
    title = _derive_title(primary_tags, aggregated, entry)
    clip_time = _derive_clip_time(primary_tags, aggregated, entry)
    summary = dict(entry)
    if title and not summary.get("title"):
        summary["title"] = title
    if clip_time and not summary.get("clip_time"):
        summary["clip_time"] = clip_time
    summary["tag_service_key"] = service_key
    summary["has_current_file_service"] = _has_current_file_service(entry)
    if "is_local" not in summary:
        summary["is_local"] = bool(entry.get("is_local"))
    return summary, primary_tags, service_key, title, clip_time


def _looks_like_hash(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    candidate = value.strip().lower()
    return len(candidate) == 64 and all(ch in "0123456789abcdef" for ch in candidate)


def _collect_relationship_hashes(payload: Any, accumulator: Set[str]) -> None:
    if isinstance(payload, dict):
        for value in payload.values():
            _collect_relationship_hashes(value, accumulator)
    elif isinstance(payload, (list, tuple, set)):
        for value in payload:
            _collect_relationship_hashes(value, accumulator)
    elif isinstance(payload, str) and _looks_like_hash(payload):
        accumulator.add(payload)


def _generate_hydrus_url_variants(url: str) -> List[str]:
    seen: Set[str] = set()
    variants: List[str] = []

    def push(candidate: Optional[str]) -> None:
        if not candidate:
            return
        text = candidate.strip()
        if not text or text in seen:
            return
        seen.add(text)
        variants.append(text)

    push(url)
    try:
        parsed = urlsplit(url)
    except Exception:
        return variants

    if parsed.scheme in {"http", "https"}:
        alternate_scheme = "https" if parsed.scheme == "http" else "http"
        push(urlunsplit((alternate_scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)))

    normalized_netloc = parsed.netloc.lower()
    if normalized_netloc and normalized_netloc != parsed.netloc:
        push(urlunsplit((parsed.scheme, normalized_netloc, parsed.path, parsed.query, parsed.fragment)))

    if parsed.path:
        trimmed_path = parsed.path.rstrip("/")
        if trimmed_path != parsed.path:
            push(urlunsplit((parsed.scheme, parsed.netloc, trimmed_path, parsed.query, parsed.fragment)))
        else:
            push(urlunsplit((parsed.scheme, parsed.netloc, parsed.path + "/", parsed.query, parsed.fragment)))
        unquoted_path = unquote(parsed.path)
        if unquoted_path != parsed.path:
            push(urlunsplit((parsed.scheme, parsed.netloc, unquoted_path, parsed.query, parsed.fragment)))

    if parsed.query or parsed.fragment:
        push(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
        if parsed.path:
            unquoted_path = unquote(parsed.path)
            push(urlunsplit((parsed.scheme, parsed.netloc, unquoted_path, "", "")))

    return variants


def generate_hydrus_url_variants(url: str) -> List[str]:
    """Public wrapper for Hydrus URL variant generation.

    Core callers that still need Hydrus-specific URL matching should import this
    public helper instead of reaching into the plugin module's private helpers.
    """
    return _generate_hydrus_url_variants(url)


def _build_hydrus_query(
    hashes: Optional[Sequence[str]],
    file_ids: Optional[Sequence[int]],
    include_relationships: bool,
    minimal: bool,
) -> Dict[str, str]:
    query: Dict[str, str] = {}
    if hashes:
        query["hashes"] = json.dumps([_normalize_hash(h) for h in hashes])
    if file_ids:
        query["file_ids"] = json.dumps([int(fid) for fid in file_ids])
    if not query:
        raise ValueError("hashes or file_ids must be provided")
    query["include_service_keys_to_tags"] = json.dumps(True)
    query["include_tag_services"] = json.dumps(True)
    query["include_file_services"] = json.dumps(True)
    if include_relationships:
        query["include_file_relationships"] = json.dumps(True)
    if not minimal:
        extras = (
            "include_url",
            "include_size",
            "include_width",
            "include_height",
            "include_duration",
            "include_mime",
            "include_has_audio",
            "include_is_trashed",
        )
        for key in extras:
            query[key] = json.dumps(True)
    return query


def _fetch_hydrus_entries(
    client: "HydrusNetwork",
    hashes: Optional[Sequence[str]],
    file_ids: Optional[Sequence[int]],
    include_relationships: bool,
    minimal: bool,
) -> List[Dict[str, Any]]:
    if not hashes and not file_ids:
        return []
    spec = HydrusRequestSpec(
        method="GET",
        endpoint="/get_files/file_metadata",
        query=_build_hydrus_query(hashes, file_ids, include_relationships, minimal),
    )
    response = client._perform_request(spec)
    metadata = response.get("metadata") if isinstance(response, dict) else None
    if isinstance(metadata, list):
        return [entry for entry in metadata if isinstance(entry, dict)]
    return []


def _has_current_file_service(entry: Dict[str, Any]) -> bool:
    services = entry.get("file_services")
    if not isinstance(services, dict):
        return False
    current = services.get("current")
    if isinstance(current, dict):
        for value in current.values():
            if value:
                return True
        return False
    if isinstance(current, list):
        return len(current) > 0
    return False


def _compute_file_flags(entry: Dict[str, Any]) -> Tuple[bool, bool, bool]:
    mime = entry.get("mime")
    mime_lower = mime.lower() if isinstance(mime, str) else ""
    is_video = mime_lower.startswith("video/")
    is_audio = mime_lower.startswith("audio/")
    is_deleted = bool(entry.get("is_trashed"))
    file_services = entry.get("file_services")
    if not is_deleted and isinstance(file_services, dict):
        deleted = file_services.get("deleted")
        if isinstance(deleted, dict) and deleted:
            is_deleted = True
    return is_video, is_audio, is_deleted


def _fetch_hydrus_metadata_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    hash_hex = None
    raw_hash_value = payload.get("hash")
    if raw_hash_value is not None:
        hash_hex = _normalize_hash(raw_hash_value)
    file_ids: List[int] = []
    raw_file_ids = payload.get("file_ids")
    if isinstance(raw_file_ids, (list, tuple, set)):
        for value in raw_file_ids:
            try:
                file_ids.append(int(value))
            except (TypeError, ValueError):
                continue
    elif raw_file_ids is not None:
        try:
            file_ids.append(int(raw_file_ids))
        except (TypeError, ValueError):
            file_ids = []
    raw_file_id = payload.get("file_id")
    if raw_file_id is not None:
        try:
            coerced = int(raw_file_id)
        except (TypeError, ValueError):
            coerced = None
        if coerced is not None and coerced not in file_ids:
            file_ids.append(coerced)
    base_url = str(payload.get("api_url") or "").strip()
    if not base_url:
        raise ValueError("Hydrus api_url is required")
    access_key = str(payload.get("access_key") or "").strip()
    options_raw = payload.get("options")
    options = options_raw if isinstance(options_raw, dict) else {}
    prefer_service = options.get("prefer_service_name")
    if isinstance(prefer_service, str):
        prefer_service = prefer_service.strip()
    else:
        prefer_service = None
    include_relationships = bool(options.get("include_relationships"))
    minimal = bool(options.get("minimal"))
    timeout = float(options.get("timeout") or 60.0)
    client = HydrusNetwork(base_url, access_key, timeout)
    hashes: Optional[List[str]] = None
    if hash_hex:
        hashes = [hash_hex]
    if not hashes and not file_ids:
        raise ValueError("Hydrus hash or file id is required")
    try:
        entries = _fetch_hydrus_entries(
            client,
            hashes,
            file_ids or None,
            include_relationships,
            minimal
        )
    except HydrusRequestError as exc:
        raise RuntimeError(str(exc))
    if not entries:
        response: Dict[str, Any] = {
            "hash": hash_hex,
            "metadata": {},
            "tags": [],
            "warnings": [f"No Hydrus metadata for {hash_hex or file_ids}"],
            "error": "not_found",
        }
        if file_ids:
            response["file_id"] = file_ids[0]
        return response
    entry = entries[0]
    if not hash_hex:
        entry_hash = entry.get("hash")
        if isinstance(entry_hash, str) and entry_hash:
            hash_hex = entry_hash
            hashes = [hash_hex]
    summary, primary_tags, service_key, title, clip_time = _summarize_hydrus_entry(entry, prefer_service)
    is_video, is_audio, is_deleted = _compute_file_flags(entry)
    has_current_file_service = _has_current_file_service(entry)
    is_local = bool(entry.get("is_local"))
    size_bytes = entry.get("size") or entry.get("file_size")
    filesize_mb = None
    if isinstance(size_bytes, (int, float)) and size_bytes > 0:
        filesize_mb = float(size_bytes) / (1024.0 * 1024.0)
    duration = entry.get("duration")
    if duration is None and isinstance(entry.get("duration_ms"), (int, float)):
        duration = float(entry["duration_ms"]) / 1000.0
    warnings_list: List[str] = []
    if not primary_tags:
        warnings_list.append("No tags returned for preferred service")
    relationships = None
    relationship_metadata: Dict[str, Dict[str, Any]] = {}
    if include_relationships and hash_hex:
        try:
            rel_spec = HydrusRequestSpec(
                method="GET",
                endpoint="/manage_file_relationships/get_file_relationships",
                query={"hash": hash_hex},
            )
            relationships = client._perform_request(rel_spec)
        except HydrusRequestError as exc:
            warnings_list.append(f"Relationship lookup failed: {exc}")
            relationships = None
        if isinstance(relationships, dict):
            related_hashes: Set[str] = set()
            _collect_relationship_hashes(relationships, related_hashes)
            related_hashes.discard(hash_hex)
            if related_hashes:
                try:
                    related_entries = _fetch_hydrus_entries(
                        client,
                        sorted(related_hashes),
                        None,
                        False,
                        True
                    )
                except HydrusRequestError as exc:
                    warnings_list.append(f"Relationship metadata fetch failed: {exc}")
                else:
                    for rel_entry in related_entries:
                        rel_hash = rel_entry.get("hash")
                        if not isinstance(rel_hash, str):
                            continue
                        rel_summary, rel_tags, _, rel_title, rel_clip = _summarize_hydrus_entry(rel_entry, prefer_service)
                        rel_summary["tags"] = rel_tags
                        if rel_title:
                            rel_summary["title"] = rel_title
                        if rel_clip:
                            rel_summary["clip_time"] = rel_clip
                        relationship_metadata[rel_hash] = rel_summary
    result: Dict[str, Any] = {
        "hash": entry.get("hash") or hash_hex,
        "metadata": summary,
        "tags": primary_tags,
        "tag_service_key": service_key,
        "title": title,
        "clip_time": clip_time,
        "duration": duration,
        "filesize_mb": filesize_mb,
        "is_video": is_video,
        "is_audio": is_audio,
        "is_deleted": is_deleted,
        "is_local": is_local,
        "has_current_file_service": has_current_file_service,
        "matched_hash": entry.get("hash") or hash_hex,
        "swap_recommended": False,
    }
    file_id_value = entry.get("file_id")
    if isinstance(file_id_value, (int, float)):
        result["file_id"] = int(file_id_value)
    if relationships is not None:
        result["relationships"] = relationships
    if relationship_metadata:
        result["relationship_metadata"] = relationship_metadata
    if warnings_list:
        result["warnings"] = warnings_list
    return result


def fetch_hydrus_metadata_by_url(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_url = payload.get("url") or payload.get("source_url")
    url = str(raw_url or "").strip()
    if not url:
        raise ValueError("URL is required to fetch Hydrus metadata by URL")
    base_url = str(payload.get("api_url") or "").strip()
    if not base_url:
        raise ValueError("Hydrus api_url is required")
    access_key = str(payload.get("access_key") or "").strip()
    options_raw = payload.get("options")
    options = options_raw if isinstance(options_raw, dict) else {}
    timeout = float(options.get("timeout") or 60.0)
    client = HydrusNetwork(base_url, access_key, timeout)
    hashes: Optional[List[str]] = None
    file_ids: Optional[List[int]] = None
    matched_url = None
    normalized_reported = None
    seen: Set[str] = set()
    queue: deque[str] = deque()
    for variant in _generate_hydrus_url_variants(url):
        queue.append(variant)
    if not queue:
        queue.append(url)
    tried_variants: List[str] = []
    while queue:
        candidate = queue.popleft()
        candidate = str(candidate or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        tried_variants.append(candidate)
        spec = HydrusRequestSpec(
            method="GET",
            endpoint="/add_urls/get_url_files",
            query={"url": candidate},
        )
        try:
            response = client._perform_request(spec)
        except HydrusRequestError as exc:
            raise RuntimeError(str(exc))
        response_hashes_list: List[str] = []
        response_file_ids_list: List[int] = []
        if isinstance(response, dict):
            normalized_value = response.get("normalized_url")
            if isinstance(normalized_value, str):
                trimmed = normalized_value.strip()
                if trimmed:
                    normalized_reported = normalized_reported or trimmed
                    if trimmed not in seen:
                        queue.append(trimmed)
            for redirect_key in ("redirect_url", "url"):
                redirect_value = response.get(redirect_key)
                if isinstance(redirect_value, str):
                    redirect_trimmed = redirect_value.strip()
                    if redirect_trimmed and redirect_trimmed not in seen:
                        queue.append(redirect_trimmed)
            raw_hashes = response.get("hashes") or response.get("file_hashes")
            if isinstance(raw_hashes, list):
                for item in raw_hashes:
                    try:
                        norm_hash = _normalize_hash(item)
                    except ValueError:
                        continue
                    if norm_hash:
                        response_hashes_list.append(norm_hash)
            raw_ids = response.get("file_ids") or response.get("file_id")
            if isinstance(raw_ids, list):
                for item in raw_ids:
                    try:
                        response_file_ids_list.append(int(item))
                    except (TypeError, ValueError):
                        continue
            elif raw_ids is not None:
                try:
                    response_file_ids_list.append(int(raw_ids))
                except (TypeError, ValueError):
                    pass
            statuses = response.get("url_file_statuses")
            if isinstance(statuses, list):
                for entry in statuses:
                    if not isinstance(entry, dict):
                        continue
                    status_hash = entry.get("hash") or entry.get("file_hash")
                    if status_hash:
                        norm_status: Optional[str] = None
                        try:
                            norm_status = _normalize_hash(status_hash)
                        except ValueError:
                            pass
                        if norm_status:
                            response_hashes_list.append(norm_status)
                    status_id = entry.get("file_id") or entry.get("fileid")
                    if status_id is not None:
                        try:
                            response_file_ids_list.append(int(status_id))
                        except (TypeError, ValueError):
                            pass
            if not hashes and response_hashes_list:
                hashes = response_hashes_list
            if not file_ids and response_file_ids_list:
                file_ids = response_file_ids_list
        if hashes or file_ids:
            matched_url = candidate
            break
    if not hashes and not file_ids:
        raise RuntimeError(
            "No Hydrus matches for URL variants: "
            + ", ".join(tried_variants)
        )
    followup_payload = {
        "api_url": base_url,
        "access_key": access_key,
        "hash": hashes[0] if hashes else None,
        "file_ids": file_ids,
        "options": {"timeout": timeout, "minimal": True},
    }
    result = _fetch_hydrus_metadata_payload(followup_payload)
    result["matched_url"] = matched_url or url
    result["normalized_url"] = normalized_reported or matched_url or url
    result["tried_urls"] = tried_variants
    return result


def _build_hydrus_context(payload: Dict[str, Any]) -> Tuple["HydrusNetwork", str, str, float, Optional[str]]:
    base_url = str(payload.get("api_url") or "").strip()
    if not base_url:
        raise ValueError("Hydrus api_url is required")
    access_key = str(payload.get("access_key") or "").strip()
    options_raw = payload.get("options")
    options = options_raw if isinstance(options_raw, dict) else {}
    timeout = float(options.get("timeout") or payload.get("timeout") or 60.0)
    prefer_service = payload.get("prefer_service_name") or options.get("prefer_service_name")
    if isinstance(prefer_service, str):
        prefer_service = prefer_service.strip() or None
    else:
        prefer_service = None
    client = HydrusNetwork(base_url, access_key, timeout)
    return client, base_url, access_key, timeout, prefer_service


def _refetch_hydrus_summary(
    base_url: str,
    access_key: str,
    hash_hex: str,
    timeout: float,
    prefer_service: Optional[str]
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "hash": hash_hex,
        "api_url": base_url,
        "access_key": access_key,
        "options": {
            "minimal": True,
            "include_relationships": False,
            "timeout": timeout,
        },
    }
    if prefer_service:
        payload["options"]["prefer_service_name"] = prefer_service
    return _fetch_hydrus_metadata_payload(payload)


def apply_hydrus_tag_mutation(
    payload: Dict[str, Any],
    add: Iterable[Any],
    remove: Iterable[Any]
) -> Dict[str, Any]:
    client, base_url, access_key, timeout, prefer_service = _build_hydrus_context(payload)
    hash_hex = _normalize_hash(payload.get("hash"))
    add_list = [_normalize_tag(tag) for tag in add if _normalize_tag(tag)]
    remove_list = [_normalize_tag(tag) for tag in remove if _normalize_tag(tag)]
    if not add_list and not remove_list:
        raise ValueError("No tag changes supplied")
    service_key = payload.get("service_key") or payload.get("tag_service_key")
    summary = None
    if not service_key:
        summary = _refetch_hydrus_summary(base_url, access_key, hash_hex, timeout, prefer_service)
        service_key = summary.get("tag_service_key")
    if not isinstance(service_key, str) or not service_key:
        raise RuntimeError("Unable to determine Hydrus tag service key")
    actions: Dict[str, List[str]] = {}
    if add_list:
        actions["0"] = [tag for tag in add_list if tag]
    if remove_list:
        actions["1"] = [tag for tag in remove_list if tag]
    if not actions:
        raise ValueError("Tag mutation produced no actionable changes")
    request_payload = {
        "hashes": [hash_hex],
        "service_keys_to_actions_to_tags": {
            service_key: actions,
        },
    }
    try:
        tag_spec = HydrusRequestSpec(
            method="POST",
            endpoint="/add_tags/add_tags",
            data=request_payload,
        )
        client._perform_request(tag_spec)
    except HydrusRequestError as exc:
        raise RuntimeError(str(exc))
    summary_after = _refetch_hydrus_summary(base_url, access_key, hash_hex, timeout, prefer_service)
    result = dict(summary_after)
    result["added_tags"] = actions.get("0", [])
    result["removed_tags"] = actions.get("1", [])
    result["tag_service_key"] = summary_after.get("tag_service_key")
    return result
