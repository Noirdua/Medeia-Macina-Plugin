from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from SYS.cmdlet_spec import Cmdlet, CmdletArg
from SYS.command_parsing import has_flag as _has_flag, normalize_to_list as _normalize_to_list
from SYS.logger import log
from SYS.result_table import Table
from SYS import pipeline as ctx
from PluginCore.registry import get_plugin

_TELEGRAM_PENDING_ITEMS_KEY = "telegram_pending_items"


def _get_telegram_provider(config: Dict[str, Any]) -> Any:
    provider = get_plugin("telegram", config)
    if provider is None:
        raise RuntimeError("Telegram plugin is not registered")
    return provider

def _extract_chat_id(chat_obj: Any) -> Optional[int]:
    try:
        if isinstance(chat_obj, dict):
            maybe_id = chat_obj.get("id")
            if maybe_id is not None:
                return int(maybe_id)
            extra = chat_obj.get("extra")
            if isinstance(extra, dict):
                v = extra.get("id")
                if v is not None:
                    return int(v)
                v = extra.get("chat_id")
                if v is not None:
                    return int(v)
        # PipeObject stores unknown fields in .extra
        if hasattr(chat_obj, "extra"):
            extra = getattr(chat_obj, "extra")
            if isinstance(extra, dict):
                v = extra.get("id")
                if v is not None:
                    return int(v)
                v = extra.get("chat_id")
                if v is not None:
                    return int(v)
        if hasattr(chat_obj, "id"):
            maybe_id = getattr(chat_obj, "id")
            if maybe_id is not None:
                return int(maybe_id)
    except Exception:
        return None
    return None


def _extract_chat_username(chat_obj: Any) -> str:
    try:
        if isinstance(chat_obj, dict):
            u = chat_obj.get("username")
            return str(u or "").strip()
        if hasattr(chat_obj, "extra"):
            extra = getattr(chat_obj, "extra")
            if isinstance(extra, dict):
                u = extra.get("username")
                if isinstance(u, str) and u.strip():
                    return u.strip()
        if hasattr(chat_obj, "username"):
            return str(getattr(chat_obj, "username") or "").strip()
    except Exception:
        return ""
    return ""


def _extract_title(item: Any) -> str:
    try:
        if isinstance(item, dict):
            return str(item.get("title") or "").strip()
        if hasattr(item, "title"):
            return str(getattr(item, "title") or "").strip()
        # PipeObject stores some fields in .extra
        if hasattr(item, "extra"):
            extra = getattr(item, "extra")
            if isinstance(extra, dict):
                v = extra.get("title")
                if isinstance(v, str) and v.strip():
                    return v.strip()
    except Exception:
        return ""
    return ""


def _extract_file_path(item: Any) -> Optional[str]:

    def _maybe(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.startswith("http://") or text.startswith("https://"):
            return None
        try:
            p = Path(text).expanduser()
            if p.exists():
                return str(p)
        except Exception:
            return None
        return None

    try:
        if hasattr(item, "path"):
            found = _maybe(getattr(item, "path"))
            if found:
                return found
        if hasattr(item, "file_path"):
            found = _maybe(getattr(item, "file_path"))
            if found:
                return found
        if isinstance(item, dict):
            for key in ("path", "file_path", "target"):
                found = _maybe(item.get(key))
                if found:
                    return found
    except Exception:
        return None
    return None


def _run(_result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    try:
        provider = _get_telegram_provider(config)
    except Exception as exc:
        log(f"Telegram not available: {exc}", file=sys.stderr)
        return 1

    if _has_flag(args, "-login"):
        ok = False
        try:
            ok = provider.ensure_session(prompt=True)
        except Exception:
            ok = False
        if not ok:
            err = getattr(provider, "_last_login_error", None)
            if isinstance(err, str) and err.strip():
                log(f"Telegram login failed: {err}", file=sys.stderr)
            else:
                log("Telegram login failed (no session created).", file=sys.stderr)
            return 1
        log("Telegram login OK (authorized session ready).", file=sys.stderr)
        return 0

    # Internal stage: send previously selected pipeline items to selected chats.
    if _has_flag(args, "-send"):
        # Ensure we don't keep showing the picker table on the send stage.
        try:
            if hasattr(ctx, "set_last_result_table_overlay"):
                ctx.set_last_result_table_overlay(None, None, None)
        except Exception:
            pass
        try:
            if hasattr(ctx, "set_current_stage_table"):
                ctx.set_current_stage_table(None)
        except Exception:
            pass

        selected_chats = _normalize_to_list(_result)
        chat_ids: List[int] = []
        chat_usernames: List[str] = []
        for c in selected_chats:
            cid = _extract_chat_id(c)
            if cid is not None:
                chat_ids.append(cid)
            else:
                u = _extract_chat_username(c)
                if u:
                    chat_usernames.append(u)

        # De-dupe chat identifiers (preserve order).
        try:
            chat_ids = list(dict.fromkeys([int(x) for x in chat_ids]))
        except Exception:
            pass
        try:
            chat_usernames = list(
                dict.fromkeys(
                    [str(u).strip() for u in chat_usernames if str(u).strip()]
                )
            )
        except Exception:
            pass

        if not chat_ids and not chat_usernames:
            log(
                "No Telegram chat selected (use @N on the Telegram table)",
                file=sys.stderr
            )
            return 1

        pending_items = ctx.load_value(_TELEGRAM_PENDING_ITEMS_KEY, default=[])
        items = _normalize_to_list(pending_items)
        if not items:
            log("No pending items to send (use: @N | .telegram)", file=sys.stderr)
            return 1

        file_jobs: List[Dict[str, str]] = []
        any_failed = False
        for item in items:
            p = _extract_file_path(item)
            if not p:
                any_failed = True
                log(
                    "Telegram send requires local file path(s) on the piped item(s)",
                    file=sys.stderr,
                )
                continue
            title = _extract_title(item)
            file_jobs.append({
                "path": p,
                "title": title
            })

        # De-dupe file paths (preserve order).
        try:
            seen: set[str] = set()
            unique_jobs: List[Dict[str, str]] = []
            for j in file_jobs:
                k = str(j.get("path") or "").strip().lower()
                if not k or k in seen:
                    continue
                seen.add(k)
                unique_jobs.append(j)
            file_jobs = unique_jobs
        except Exception:
            pass

        if not file_jobs:
            return 1

        try:
            provider.send_files_to_chats(
                chat_ids=chat_ids,
                usernames=chat_usernames,
                files=file_jobs
            )
        except Exception as exc:
            log(f"Telegram send failed: {exc}", file=sys.stderr)
            any_failed = True

        ctx.store_value(_TELEGRAM_PENDING_ITEMS_KEY, [])
        return 1 if any_failed else 0

    selected_items = _normalize_to_list(_result)
    if selected_items:
        ctx.store_value(_TELEGRAM_PENDING_ITEMS_KEY, selected_items)
    else:
        # Avoid stale sends if the user just wants to browse chats.
        try:
            ctx.store_value(_TELEGRAM_PENDING_ITEMS_KEY, [])
        except Exception:
            pass
        try:
            if hasattr(ctx, "clear_pending_pipeline_tail"):
                ctx.clear_pending_pipeline_tail()
        except Exception:
            pass

    # Default: list available chats/channels (requires an existing session or bot_token).
    try:
        rows = provider.list_chats(limit=200)
    except Exception as exc:
        log(f"Failed to list Telegram chats: {exc}", file=sys.stderr)
        return 1

    # Only show dialogs you can typically post to.
    try:
        rows = [
            r for r in (rows or [])
            if str(r.get("type") or "").strip().lower() in {"group", "user"}
        ]
    except Exception:
        pass

    if not rows:
        log(
            "No Telegram groups/users available (or not logged in). Run: .telegram -login",
            file=sys.stderr,
        )
        return 0

    table = Table("Telegram Chats")
    table.set_table("telegram")
    table.set_source_command(".telegram", [])

    chat_items: List[Dict[str, Any]] = []
    for item in rows:
        row = table.add_row()
        title = str(item.get("title") or "").strip()
        username = str(item.get("username") or "").strip()
        chat_id = item.get("id")
        kind = str(item.get("type") or "").strip()
        row.add_column("Type", kind)
        row.add_column("Title", title)
        row.add_column("Username", username)
        row.add_column("Id", str(chat_id) if chat_id is not None else "")
        chat_items.append(
            {
                **item,
                "store": "telegram",
                "title": title or username or str(chat_id) or "Telegram",
            }
        )

    # Overlay table: ensures @N selection targets this Telegram picker, not a previous table.
    ctx.set_last_result_table_overlay(table, chat_items)
    ctx.set_current_stage_table(table)
    if selected_items:
        ctx.set_pending_pipeline_tail([[".telegram", "-send"]], ".telegram")
    return 0


CMDLET = Cmdlet(
    name=".telegram",
    alias=["telegram"],
    summary="Telegram login and chat listing",
    usage="@N | .telegram  (pick a chat, then send piped files)",
    arg=[
        CmdletArg(
            name="login",
            type="bool",
            description="Create/refresh a Telegram session (prompts)",
            required=False,
        ),
        CmdletArg(
            name="send",
            type="bool",
            description="(internal) Send to selected chat(s)",
            required=False,
        ),
    ],
    exec=_run,
)

COMMANDS = [CMDLET]
