"""Example plugin template for use as a starter kit.

This minimal plugin demonstrates the typical hooks a plugin may implement:
- `validate()` to assert it's usable
- `search()` to return `SearchResult` items
- `download()` to persist a sample file (useful for local tests)

See `docs/plugin_guide.md` for authoring guidance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from PluginCore.base import Plugin, SearchResult


class Hello(Plugin):
    """Very small example plugin suitable as a template.

    - Table name: `hello`
    - Usage: `search-file -plugin hello "query"`
    - Selecting a row and piping into `download-file` will call `download()`.
    """

    PLUGIN_NAME = "hello"
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_AUTHOR = "Medeia"
    PLUGIN_DESCRIPTION = "Minimal example plugin for authors."
    SUPPORTED_CMDLETS = frozenset({"download-file", "search-file"})
    CONFIG_HELP = (
        "No credentials required. Try: file -search -plugin hello demo",
    )
    URL = ("hello:",)
    URL_DOMAINS = ()

    def validate(self) -> bool:
        # No configuration required; always available for testing/demo purposes.
        return True

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        q = (query or "").strip()
        results: List[SearchResult] = []
        if not q or q in {"*", "all", "list"}:
            q = "example"

        # Emit up to `limit` tiny example results.
        n = min(max(1, int(limit)), 3)
        for i in range(1, n + 1):
            title = f"{q} sample {i}"
            path = f"https://example.org/{q}/{i}"
            sr = SearchResult(
                table="hello",
                title=title,
                path=path,
                detail="Example provider result",
                media_kind="file",
                columns=[("Example", "yes")],
                full_metadata={"example_index": i},
            )
            results.append(sr)

        return results[: max(0, int(limit))]

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        """Create a small text file to simulate a download.

        This keeps the example self-contained (no network access required) and
        makes it straightforward to test provider behavior with `pytest`.
        """
        from SYS.utils import ensure_directory, sanitize_filename, unique_path

        ensure_directory(Path(output_dir))
        title = str(getattr(result, "title", "hello") or "hello").strip()
        safe = sanitize_filename(title, fallback="hello")
        if not safe.lower().endswith(".txt"):
            safe = f"{safe}.txt"
        dest = unique_path(Path(output_dir) / safe)
        try:
            dest.write_text(f"Hello from Hello\nsource: {result.path}\n", encoding="utf-8")
            return dest
        except Exception:
            return None

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any,
    ) -> bool:
        """Present a simple details table when a Hello row is selected.

        This demonstrates how providers can implement custom `@N` selection
        behavior by constructing a `ResultTable`, populating it with
        provider-specific rows, and instructing the CLI to show the table.
        """
        if not stage_is_last:
            return False

        def _as_payload(item: Any) -> Dict[str, Any]:
            if isinstance(item, dict):
                return dict(item)
            try:
                if hasattr(item, "to_dict"):
                    maybe = item.to_dict()
                    if isinstance(maybe, dict):
                        return maybe
            except Exception:
                pass
            payload: Dict[str, Any] = {}
            try:
                payload = {
                    "title": getattr(item, "title", None),
                    "path": getattr(item, "path", None),
                    "table": getattr(item, "table", None),
                    "annotations": getattr(item, "annotations", None),
                    "media_kind": getattr(item, "media_kind", None),
                    "full_metadata": getattr(item, "full_metadata", None),
                }
            except Exception:
                payload = {}
            return payload

        chosen: List[Dict[str, Any]] = []
        for item in selected_items or []:
            payload = _as_payload(item)
            meta = payload.get("full_metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            idx = meta.get("example_index")
            if idx is None:
                continue
            title = str(payload.get("title") or payload.get("path") or "").strip() or f"hello-{idx}"
            chosen.append({"index": idx, "title": title, "path": payload.get("path")})

        if not chosen:
            return False

        target = chosen[0]
        idx = target.get("index")
        title = target.get("title") or f"hello-{idx}"

        try:
            from SYS.rich_display import prepare_result_table, publish_result_table, result_payload
        except Exception:
            # If ResultTable isn't available, consider selection handled
            return True

        table = prepare_result_table(
            f"Hello Details: {title}",
            "hello",
            metadata={"plugin": "hello", "view": "details", "example_index": idx},
            source_command="download-file",
        )

        results_payload: List[Dict[str, Any]] = []
        for part in ("a", "b"):
            file_title = f"{title} - part {part}"
            file_path = f"{target.get('path')}/{part}"
            sr = SearchResult(
                table="hello",
                title=file_title,
                path=file_path,
                detail=f"Part {part}",
                media_kind="file",
                columns=[("Part", part)],
                full_metadata={"part": part, "example_index": idx},
            )
            table.add_result(sr)
            results_payload.append(result_payload(sr, table="hello"))

        publish_result_table(ctx, table, results_payload)

        return True
