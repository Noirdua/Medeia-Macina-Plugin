from __future__ import annotations

from typing import Any, Dict, Sequence

from SYS.cmdlet_spec import Cmdlet, CmdletArg
from cmdlet._shared import should_show_help
from plugins.alldebrid.api import unlock_link_cmdlet

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


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    if should_show_help(args):
        return 0
    from SYS import pipeline as ctx

    code = unlock_link_cmdlet(result, args, config)
    if code == 0:
        ctx.emit(result)
    return code


CMDLET.exec = _run
COMMANDS = [CMDLET]
