from __future__ import annotations

import os
import sys
from typing import Any

from PluginCore.base import Plugin
from SYS.logger import log


class ZeroXZero(Plugin):
    """File provider for 0x0.st."""

    PLUGIN_NAME = "0x0"
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_AUTHOR = "Medeia"
    PLUGIN_DESCRIPTION = "Upload files to 0x0.st."
    PLUGIN_ALIASES = ("zeroxzero",)
    SUPPORTED_CMDLETS = frozenset({"add-file"})

    def upload(self, file_path: str, **kwargs: Any) -> str:
        from API.HTTP import HTTPClient
        from SYS.models import ProgressFileReader

        from SYS.utils import require_existing_file

        require_existing_file(file_path)

        try:
            headers = {
                "User-Agent": "Medeia-Macina/1.0"
            }
            with HTTPClient(headers=headers) as client:
                with open(file_path, "rb") as handle:
                    try:
                        total = os.path.getsize(file_path)
                    except Exception:
                        total = None
                    wrapped = ProgressFileReader(
                        handle,
                        total_bytes=total,
                        label="upload"
                    )
                    response = client.post(
                        "https://0x0.st",
                        files={
                            "file": wrapped
                        }
                    )

                if response.status_code == 200:
                    uploaded_url = response.text.strip()
                    from PluginCore.backend_registry import remember_uploaded_url

                    remember_uploaded_url(self.config, kwargs.get("pipe_obj"), uploaded_url)

                    return uploaded_url

                raise Exception(
                    f"Upload failed: {response.status_code} - {response.text}"
                )

        except Exception as exc:
            log(f"[0x0] Upload error: {exc}", file=sys.stderr)
            raise

    def validate(self) -> bool:
        return True
