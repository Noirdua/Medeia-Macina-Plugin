from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from PluginCore.base import Plugin
from SYS.logger import log


def _pick_provider_config(config: Any) -> Dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    plugin_cfg = config.get("plugin")
    if not isinstance(plugin_cfg, dict):
        return {}
    for key in ("fileio", "file.io"):
        entry = plugin_cfg.get(key)
        if isinstance(entry, dict):
            return entry
    return {}


def _extract_link(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("link", "url", "downloadLink", "download_url"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
                return val.strip()
        for nested_key in ("data", "file", "result"):
            nested = payload.get(nested_key)
            found = _extract_link(nested)
            if found:
                return found
    return None


def _extract_key(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("key", "id", "uuid"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for nested_key in ("data", "file", "result"):
            nested = payload.get(nested_key)
            found = _extract_key(nested)
            if found:
                return found
    return None


class FileIO(Plugin):
    """File provider for file.io."""
    PLUGIN_NAME = "fileio"
    PLUGIN_ALIASES = ("file.io",)
    SUPPORTED_CMDLETS = frozenset({"add-file"})

    @property
    def label(self) -> str:
        return "file.io"

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "label": "API Key",
                "default": "",
                "secret": True
            },
            {
                "key": "expires",
                "label": "Default Expiration (e.g. 1w)",
                "default": "1w"
            },
            {
                "key": "maxDownloads",
                "label": "Max Downloads",
                "default": 1
            },
            {
                "key": "autoDelete",
                "label": "Auto Delete",
                "default": True
            }
        ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        conf = _pick_provider_config(self.config)
        self._base_url = str(conf.get("base_url")
                             or "https://file.io").strip().rstrip("/")
        self._api_key = conf.get("api_key")
        self._default_expires = conf.get("expires")
        self._default_max_downloads = conf.get("maxDownloads")
        if self._default_max_downloads is None:
            self._default_max_downloads = conf.get("max_downloads")
        self._default_auto_delete = conf.get("autoDelete")
        if self._default_auto_delete is None:
            self._default_auto_delete = conf.get("auto_delete")

    def validate(self) -> bool:
        return True

    def upload(self, file_path: str, **kwargs: Any) -> str:
        from API.HTTP import HTTPClient
        from SYS.models import ProgressFileReader

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        data: Dict[str,
                   Any] = {}
        expires = kwargs.get("expires", self._default_expires)
        max_downloads = kwargs.get(
            "maxDownloads",
            kwargs.get("max_downloads",
                       self._default_max_downloads)
        )
        auto_delete = kwargs.get(
            "autoDelete",
            kwargs.get("auto_delete",
                       self._default_auto_delete)
        )

        if expires not in (None, ""):
            data["expires"] = expires
        if max_downloads not in (None, ""):
            data["maxDownloads"] = max_downloads
        if auto_delete not in (None, ""):
            data["autoDelete"] = auto_delete

        headers: Dict[str,
                      str] = {
                          "User-Agent": "Medeia-Macina/1.0",
                          "Accept": "application/json"
                      }
        if isinstance(self._api_key, str) and self._api_key.strip():
            # Some file.io plans use bearer tokens; keep optional.
            headers["Authorization"] = f"Bearer {self._api_key.strip()}"

        try:
            with HTTPClient(headers=headers) as client:
                with open(file_path, "rb") as handle:
                    filename = os.path.basename(file_path)
                    try:
                        total = os.path.getsize(file_path)
                    except Exception:
                        total = None
                    wrapped = ProgressFileReader(
                        handle,
                        total_bytes=total,
                        label="upload"
                    )
                    response = client.request(
                        "POST",
                        f"{self._base_url}/upload",
                        data=data or None,
                        files={
                            "file": (filename,
                                     wrapped)
                        },
                        follow_redirects=True,
                        raise_for_status=False,
                    )

            if response.status_code >= 400:
                location = response.headers.get("location"
                                                ) or response.headers.get("Location")
                ct = response.headers.get("content-type"
                                          ) or response.headers.get("Content-Type")
                raise Exception(
                    f"Upload failed: {response.status_code} (content-type={ct}, location={location}) - {response.text}"
                )

            payload: Any
            try:
                payload = response.json()
            except Exception:
                payload = None

            # If the server ignored our Accept header and returned HTML, this is almost
            # certainly the wrong endpoint or an upstream block.
            ct = (
                response.headers.get("content-type")
                or response.headers.get("Content-Type") or ""
            ).lower()
            if (payload is None) and ("text/html" in ct):
                raise Exception(
                    "file.io returned HTML instead of JSON; expected API response from /upload"
                )

            if isinstance(payload, dict) and payload.get("success") is False:
                reason = payload.get("message"
                                     ) or payload.get("error") or payload.get("status")
                raise Exception(str(reason or "Upload failed"))

            uploaded_url = _extract_link(payload)
            if not uploaded_url:
                # Some APIs may return the link as plain text.
                text = str(response.text or "").strip()
                if text.startswith(("http://", "https://")):
                    uploaded_url = text

            if not uploaded_url:
                key = _extract_key(payload)
                if key:
                    uploaded_url = f"{self._base_url}/{key.lstrip('/')}"

            if not uploaded_url:
                try:
                    snippet = (response.text or "").strip()
                    if len(snippet) > 300:
                        snippet = snippet[:300] + "..."
                except Exception:
                    snippet = "<unreadable response>"
                raise Exception(
                    f"Upload succeeded but response did not include a link (response: {snippet})"
                )

            try:
                pipe_obj = kwargs.get("pipe_obj")
                if pipe_obj is not None:
                    from PluginCore.backend_registry import BackendRegistry

                    BackendRegistry(
                        self.config,
                        suppress_debug=True
                    ).try_add_url_for_pipe_object(pipe_obj,
                                                  uploaded_url)
            except Exception:
                pass

            return uploaded_url

        except Exception as exc:
            log(f"[file.io] Upload error: {exc}", file=sys.stderr)
            raise
