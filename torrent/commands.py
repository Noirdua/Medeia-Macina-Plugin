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
    ids: List[str] = []
    tokens = [str(t or "").strip() for t in (args or [])]
    for idx, tok in enumerate(tokens):
        if tok.lower() in {"-id", "--id"} and idx + 1 < len(tokens):
            ids.append(tokens[idx + 1])
        elif tok.lower().startswith("-id="):
            ids.append(tok.split("=", 1)[1].strip())
    items = result if isinstance(result, list) else ([result] if result is not None else [])
    for item in items:
        if item is None:
            continue
        if isinstance(item, dict):
            for key in ("id", "job_id"):
                val = str(item.get(key) or "").strip()
                if val:
                    ids.append(val)
            extra = item.get("extra")
            if isinstance(extra, dict):
                val = str(extra.get("id") or extra.get("job_id") or "").strip()
                if val:
                    ids.append(val)
            continue
        val = str(getattr(item, "id", "") or "").strip()
        if val:
            ids.append(val)
    seen: set[str] = set()
    out: List[str] = []
    for job_id in ids:
        if job_id and job_id not in seen:
            seen.add(job_id)
            out.append(job_id)
    return out


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
                    ("Title", snap["title"]),
                    ("Status", snap["status"]),
                    ("Progress", snap["progress"]),
                    ("Down", snap["down"]),
                    ("Peers", snap["peers"]),
                    ("Size", snap["size"]),
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
                    ("Peers", "—"),
                    ("Size", "—"),
                ],
            }
        )
    publish_result_table(ctx, table, rows, overlay=False)
    display_and_persist_items(
        rows or [{"title": "(none)", "status": "idle"}],
        title="Torrent downloads",
        subject=rows,
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
