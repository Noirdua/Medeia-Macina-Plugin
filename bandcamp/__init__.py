from __future__ import annotations

import sys
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

from PluginCore.base import Plugin, SearchResult
from SYS.logger import log, debug, debug_panel

class Bandcamp(Plugin):
    """Search provider for Bandcamp."""

    PLUGIN_NAME = "bandcamp"
    PLUGIN_DEPENDS = ("playwright",)
    SUPPORTED_CMDLETS = frozenset({"search-file"})
    TABLE_AUTO_STAGES = {
        "bandcamp": ["download-file"],
    }
    AUTO_STAGE_USE_SELECTION_ARGS = True

    @staticmethod
    def _download_selection_args(target_url: str, media_type: str) -> Optional[List[str]]:
        target = str(target_url or "").strip()
        kind = str(media_type or "").strip().lower()
        if not target or kind == "artist":
            return None
        return ["-url", target]

    @staticmethod
    def _base_url(raw_url: str) -> str:
        """Normalize a Bandcamp URL down to scheme://netloc."""
        text = str(raw_url or "").strip()
        if not text:
            return ""
        try:
            parsed = urlparse(text)
            if not parsed.scheme or not parsed.netloc:
                return text
            return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return text

    @classmethod
    def _discography_url(cls, raw_url: str) -> str:
        base = cls._base_url(raw_url)
        if not base:
            return ""
        # Bandcamp discography lives under /music.
        return base.rstrip("/") + "/music"

    def _scrape_artist_page(self,
                            page: Any,
                            artist_url: str,
                            limit: int = 50) -> List[SearchResult]:
        """Scrape an artist page for albums/tracks (discography)."""
        base = self._base_url(artist_url)
        discography_url = self._discography_url(artist_url)
        if not base or not discography_url:
            return []

        debug_panel(
            "bandcamp artist scrape",
            [
                ("url", discography_url),
                ("limit", limit),
            ],
            border_style="cyan",
        )
        page.goto(discography_url)
        page.wait_for_load_state("domcontentloaded")

        results: List[SearchResult] = []
        cards = page.query_selector_all("li.music-grid-item") or []
        if not cards:
            # Fallback selector
            cards = page.query_selector_all(".music-grid-item") or []

        for item in cards[:limit]:
            try:
                link = item.query_selector("a")
                if not link:
                    continue

                href = link.get_attribute("href") or ""
                href = str(href).strip()
                if not href:
                    continue

                if href.startswith("/"):
                    target = base.rstrip("/") + href
                elif href.startswith("http://") or href.startswith("https://"):
                    target = href
                else:
                    target = base.rstrip("/") + "/" + href

                title_node = item.query_selector("p.title"
                                                 ) or item.query_selector(".title")
                title = title_node.inner_text().strip() if title_node else ""
                if title:
                    title = " ".join(title.split())
                if not title:
                    title = target.rsplit("/", 1)[-1]

                kind = (
                    "album" if "/album/" in target else
                    ("track" if "/track/" in target else "item")
                )
                selection_args = self._download_selection_args(target, kind)
                selection_action = (["download-file"] + selection_args) if selection_args else None

                results.append(
                    SearchResult(
                        table="bandcamp",
                        title=title,
                        path=target,
                        detail="",
                        annotations=[kind],
                        media_kind="audio",
                        columns=[
                            ("Title",
                             title),
                            ("Type",
                             kind),
                            ("Url",
                             target),
                        ],
                        full_metadata={
                            "type": kind,
                            "url": target,
                            "artist_url": base,
                        },
                        selection_args=selection_args,
                        selection_action=selection_action,
                    )
                )
            except Exception as exc:
                debug(f"[bandcamp] Error parsing artist item: {exc}")

        return results

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any
    ) -> bool:
        """Handle Bandcamp `@N` selection.

        If the selected item is an ARTIST result, selecting it auto-expands into
        a discography table by scraping the artist URL.
        """
        if not stage_is_last:
            return False

        # Playwright is required; proceed to handle artist selection

        # Only handle artist selections.
        chosen: List[Dict[str, Any]] = []
        for item in selected_items or []:
            payload: Dict[str,
                          Any] = {}
            if isinstance(item, dict):
                payload = item
            else:
                try:
                    if hasattr(item, "to_dict"):
                        payload = item.to_dict()  # type: ignore[assignment]
                except Exception:
                    payload = {}
                if not payload:
                    try:
                        payload = {
                            "title": getattr(item,
                                             "title",
                                             None),
                            "url": getattr(item,
                                           "url",
                                           None),
                            "path": getattr(item,
                                            "path",
                                            None),
                            "metadata": getattr(item,
                                                "metadata",
                                                None),
                            "extra": getattr(item,
                                             "extra",
                                             None),
                        }
                    except Exception:
                        payload = {}

            meta = payload.get("metadata") or payload.get("full_metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            extra = payload.get("extra")
            if isinstance(extra, dict):
                meta = {
                    **meta,
                    **extra
                }

            type_val = str(meta.get("type") or "").strip().lower()
            if type_val != "artist":
                continue

            title = str(payload.get("title") or "").strip()
            url_val = str(
                payload.get("url") or payload.get("path") or meta.get("url") or ""
            ).strip()
            base = self._base_url(url_val)
            if not base:
                continue

            chosen.append(
                {
                    "title": title,
                    "url": base,
                    "location": str(meta.get("artist") or "").strip()
                }
            )

        if not chosen:
            return False

        # Build a new table from artist discography.
        try:
            from SYS.result_table import Table
            from SYS.rich_display import stdout_console
        except Exception:
            return False

        artist_title = chosen[0].get("title") or "artist"
        artist_url = chosen[0].get("url") or ""

        try:
            from plugins.playwright import PlaywrightTool

            tool = PlaywrightTool({})
            tool.require()
            with tool.open_page(headless=True) as page:
                discography = self._scrape_artist_page(page, artist_url, limit=50)
        except Exception as exc:
            print(f"bandcamp artist lookup failed: {exc}\n")
            return True

        table = Table(f"Bandcamp: artist:{artist_title}")._perseverance(True)
        table.set_table("bandcamp")
        try:
            table.set_value_case("preserve")
        except Exception:
            pass

        results_payload: List[Dict[str, Any]] = []
        for r in discography:
            table.add_result(r)
            try:
                results_payload.append(r.to_dict())
            except Exception:
                results_payload.append(
                    {
                        "table": "bandcamp",
                        "title": getattr(r,
                                         "title",
                                         ""),
                        "path": getattr(r,
                                        "path",
                                        ""),
                    }
                )

        try:
            ctx.set_last_result_table(table, results_payload)
            ctx.set_current_stage_table(table)
        except Exception:
            pass

        try:
            stdout_console().print()
            stdout_console().print(table)
        except Exception:
            pass

        return True

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str,
                               Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        try:
            from plugins.playwright import PlaywrightTool

            tool = PlaywrightTool({})
            tool.require()
            with tool.open_page(headless=True) as page:
                if query.strip().lower().startswith("artist:"):
                    artist_name = query[7:].strip().strip('"')
                    search_url = f"https://bandcamp.com/search?q={artist_name}&item_type=b"
                else:
                    search_url = f"https://bandcamp.com/search?q={query}&item_type=a"

                results = self._scrape_url(page, search_url, limit)
                return results

        except Exception as exc:
            log(f"[bandcamp] Search error: {exc}", file=sys.stderr)
            return []

    def _scrape_url(self, page: Any, url: str, limit: int) -> List[SearchResult]:
        debug_panel(
            "bandcamp search",
            [
                ("url", url),
                ("limit", limit),
            ],
            border_style="cyan",
        )

        page.goto(url)
        page.wait_for_load_state("domcontentloaded")

        results: List[SearchResult] = []

        search_results = page.query_selector_all(".searchresult")
        if not search_results:
            return results

        for item in search_results[:limit]:
            try:
                heading = item.query_selector(".heading")
                if not heading:
                    continue

                link = heading.query_selector("a")
                if not link:
                    continue

                title = link.inner_text().strip()
                target_url = link.get_attribute("href")
                base_url = self._base_url(str(target_url or ""))

                subhead = item.query_selector(".subhead")
                artist = subhead.inner_text().strip() if subhead else "Unknown"

                itemtype = item.query_selector(".itemtype")
                media_type = itemtype.inner_text().strip() if itemtype else "album"
                selection_args = self._download_selection_args(str(target_url or ""), media_type)
                selection_action = (["download-file"] + selection_args) if selection_args else None

                results.append(
                    SearchResult(
                        table="bandcamp",
                        title=title,
                        path=target_url,
                        detail=f"By: {artist}",
                        annotations=[media_type],
                        media_kind="audio",
                        columns=[
                            ("Title",
                             title),
                            ("Location",
                             artist),
                            ("Type",
                             media_type),
                            ("Url",
                             str(target_url or "")),
                        ],
                        full_metadata={
                            "artist": artist,
                            "type": media_type,
                            "url": str(target_url or ""),
                            "artist_url": base_url,
                        },
                        selection_args=selection_args,
                        selection_action=selection_action,
                    )
                )

            except Exception as exc:
                debug(f"[bandcamp] Error parsing result: {exc}")

        return results

    def validate(self) -> bool:
        # Playwright is required for the provider to function
        return True
