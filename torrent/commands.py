from __future__ import annotations

from typing import Any, Dict, List, Sequence

from SYS.cmdlet_spec import Cmdlet, CmdletArg
from cmdlet._shared import display_and_persist_items, should_show_help

CMDLET = Cmdlet(
    name=".torrent",
    summary="List and control local libtorrent downloads",
    usage=".torrent [-list] [-pause] [-resume] [-remove] [-id ID]",
    alias=["torrent-jobs"],
    arg=[
        CmdletArg("-list", type="flag", required=False, description="Show active torrent jobs"),
        CmdletArg("-pause", type="flag", required=False, description="Pause selected or -id job"),
        CmdletArg("-resume", type="flag", required=False, description="Resume selected or -id job"),
        CmdletArg("-remove", type="flag", required=False, description="Remove selected or -id job"),
        CmdletArg("-id", type="string", required=False, description="Job id"),
    ],
    examples=[
        ".torrent",
        "@1 | .torrent -pause",
        ".torrent -resume -id 1",
        ".torrent -remove -id 2",
    ],
    detail=[
        "Requires Plugins / Torrent / Downloader = libtorrent.",
        "Selecting a torrent search row queues a background download.",
    ],
)


def _job_ids(result: Any, args: Sequence[str]) -> List[str]:
    from SYS.plugin_jobs import extract_job_ids

    return extract_job_ids(result, list(args or []))


def _publish_jobs() -> int:
    from SYS import plugin_jobs
    from SYS.result_table import Table
    from SYS import pipeline as ctx
    from SYS.result_publication import publish_result_table

    rows = plugin_jobs.list_jobs("torrent")
    table = Table("Torrent downloads")
    table.set_table("torrent.jobs")
    for snap in rows:
        table.add_result(
            {
                **snap,
                "columns": [
                    ("Title", snap.get("title") or ""),
                    ("Status", snap.get("status") or ""),
                    ("Progress", snap.get("progress") or ""),
                    ("Down", snap.get("down") or ""),
                    ("Up", snap.get("up") or ""),
                    ("Seeds", snap.get("seeds") or ""),
                    ("Leechers", snap.get("leechers") or ""),
                    ("Size", snap.get("size") or ""),
                ],
            }
        )
    if not rows:
        table.add_result(
            {
                "title": "(none)",
                "status": "idle",
                "progress": "0%",
                "down": "",
                "peers": "",
                "size": "",
                "columns": [
                    ("Title", "(none)"),
                    ("Status", "idle"),
                    ("Progress", "—"),
                    ("Down", "—"),
                    ("Up", "—"),
                    ("Seeds", "—"),
                    ("Leechers", "—"),
                    ("Size", "—"),
                ],
            }
        )
    setattr(table, "_items_added", True)
    publish_result_table(ctx, table, rows, overlay=False)
    display_and_persist_items(
        rows or [{"title": "(none)", "status": "idle"}],
        title="Torrent downloads",
        subject=rows,
        display_type="custom",
        table=table,
    )
    return 0


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    if should_show_help(args):
        return 0
    from SYS import plugin_jobs
    from SYS.logger import log
    import sys

    tokens = [str(t or "").strip().lower() for t in (args or [])]
    pause = "-pause" in tokens or "--pause" in tokens
    resume = "-resume" in tokens or "--resume" in tokens
    remove = "-remove" in tokens or "--remove" in tokens
    ids = _job_ids(result, args)
    if pause or resume or remove:
        if not ids:
            log(".torrent: no job id (use @N or -id)", file=sys.stderr)
            return 1
        ok = True
        for job_id in ids:
            if pause:
                ok = plugin_jobs.pause(job_id) and ok
            elif resume:
                ok = plugin_jobs.resume(job_id) and ok
            elif remove:
                ok = plugin_jobs.cancel(job_id) and ok
        _publish_jobs()
        return 0 if ok else 1
    return _publish_jobs()


CMDLET.exec = _run
COMMANDS = [CMDLET]
