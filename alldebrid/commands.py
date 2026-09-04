from __future__ import annotations

from typing import Any, Dict, Optional, Sequence
from urllib.parse import unquote, urlparse

from SYS.cmdlet_spec import Cmdlet, CmdletArg
from cmdlet._shared import should_show_help
from plugins.alldebrid.api import convert_link_with_debrid

CMDLET = Cmdlet(
    name="unlock-link",
    summary="Unlock a hoster link through AllDebrid",
    usage="unlock-link <url>",
    alias=["unlock"],
    arg=[
        CmdletArg(
            "url",
            type="string",
            description="Restricted hoster URL (or pipe a row with url/path)",
        ),
    ],
    examples=[
        "unlock-link https://hoster.example/file",
        "@1 | unlock-link",
        "file -search -plugin alldebrid * | @1 | unlock-link",
    ],
    detail=[
        "Requires AllDebrid API Key in .config (Plugins / Alldebrid).",
        "On success the piped result's url and path become the direct download link.",
        "Create a key at https://alldebrid.com/apikeys",
    ],
)


def _extract_link(result: Any, args: Sequence[str]) -> Optional[str]:
    for token in args or []:
        text = str(token or "").strip()
        if text.startswith(("http://", "https://")):
            return text
    if isinstance(result, dict):
        for key in ("url", "source_url", "path", "target"):
            value = result.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value.strip()
    extra = getattr(result, "extra", None)
    if isinstance(extra, dict):
        for key in ("url", "source_url", "path"):
            value = extra.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value.strip()
    return None


def _filename_from_url(url: str) -> str:
    try:
        name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    except Exception:
        name = ""
    return name or url


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    if should_show_help(args):
        return 0
    from SYS.logger import log
    from SYS.config import get_debrid_api_key
    from cmdlet._shared import display_and_persist_items
    import sys

    link = _extract_link(result, args)
    if not link:
        log("No valid URL provided", file=sys.stderr)
        return 1
    api_key = ""
    try:
        api_key = str(get_debrid_api_key(config, service="All-debrid") or "").strip()
    except Exception:
        api_key = ""
    if not api_key:
        log("AllDebrid API key not configured. Use .config to set it.", file=sys.stderr)
        return 1
    direct = convert_link_with_debrid(link, api_key)
    if not direct:
        log("Failed to unlock link", file=sys.stderr)
        return 1
    payload = {
        "title": _filename_from_url(direct),
        "url": direct,
        "path": direct,
        "plugin": "alldebrid",
        "source_url": link,
    }
    if isinstance(result, dict):
        result.update(payload)
        payload = result
    display_and_persist_items([payload], title="Unlocked")
    return 0


CMDLET.exec = _run
COMMANDS = [CMDLET]
