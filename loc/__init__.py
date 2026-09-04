from __future__ import annotations

from typing import Any, Dict, List, Optional

from plugins.loc.api import LOCClient
from PluginCore.base import Plugin, SearchResult
from SYS.cli_syntax import get_free_text, parse_query
from SYS.logger import log


class LOC(Plugin):
    """LoC search provider.

    Currently implements Chronicling America collection search via the LoC JSON API.
    """

    SUPPORTED_CMDLETS = frozenset({"search-file"})

    @property
    def preserve_order(self) -> bool:
        return True

    URL_DOMAINS = ["www.loc.gov"]
    URL = URL_DOMAINS

    def validate(self) -> bool:
        return True

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str,
                               Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        _ = kwargs
        parsed = parse_query(query or "")
        text = get_free_text(parsed).strip()
        fields = parsed.get("fields",
                            {}) if isinstance(parsed,
                                              dict) else {}

        # Allow explicit q: override.
        q = str(fields.get("q") or text or "").strip()
        if not q:
            return []

        # Pass through any extra filters supported by the LoC API.
        extra: Dict[str,
                    Any] = {}
        if isinstance(filters, dict):
            extra.update(filters)
        if isinstance(fields, dict):
            for k, v in fields.items():
                if k == "q":
                    continue
                extra[str(k)] = v

        client = LOCClient()

        results: List[SearchResult] = []
        start = 1
        page_size = 25
        try:
            if limit and limit > 0:
                page_size = max(1, min(int(limit), 50))

            while len(results) < max(0, int(limit)):
                payload = client.search_chronicling_america(
                    q,
                    start=start,
                    count=page_size,
                    extra_params=extra
                )
                items = payload.get("results")
                if not isinstance(items, list) or not items:
                    break

                for it in items:
                    if not isinstance(it, dict):
                        continue

                    title = str(it.get("title") or "").strip() or "(untitled)"
                    date = str(it.get("date") or "").strip()
                    url = str(it.get("url") or "").strip()
                    aka = it.get("aka")
                    if (not url) and isinstance(aka, list) and aka:
                        url = str(aka[0] or "").strip()

                    formats = it.get("online_format")
                    if isinstance(formats, list):
                        fmt_text = ", ".join([str(x) for x in formats if x])
                    else:
                        fmt_text = str(formats or "").strip()

                    partof = it.get("partof")
                    if isinstance(partof, list) and partof:
                        source = str(partof[-1] or "").strip()
                    else:
                        source = "Chronicling America"

                    detail_parts = []
                    if date:
                        detail_parts.append(date)
                    if source:
                        detail_parts.append(source)
                    detail = " — ".join(detail_parts)

                    annotations: List[str] = []
                    if date:
                        annotations.append(date)
                    if fmt_text:
                        annotations.append(fmt_text)

                    results.append(
                        SearchResult(
                            table="loc",
                            title=title,
                            path=url or title,
                            detail=detail,
                            annotations=annotations,
                            media_kind="document",
                            columns=[
                                ("Title",
                                 title),
                                ("Date",
                                 date),
                                ("Format",
                                 fmt_text),
                                ("URL",
                                 url),
                            ],
                            full_metadata=it,
                        )
                    )
                    if len(results) >= int(limit):
                        break

                # LoC API pagination uses sp (1-based start index).
                if len(items) < page_size:
                    break
                start += len(items)

        except Exception as exc:
            log(f"[loc] search failed: {exc}")
            return []

        return results
