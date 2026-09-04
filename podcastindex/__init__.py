from __future__ import annotations

import sys
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PluginCore.base import Plugin, SearchResult
from SYS.logger import log
from SYS.utils import format_bytes


def _get_podcastindex_credentials(config: Dict[str, Any]) -> Tuple[str, str]:
    plugin_cfg = config.get("plugin")
    if not isinstance(plugin_cfg, dict):
        return "", ""

    entry = plugin_cfg.get("podcastindex")
    if not isinstance(entry, dict):
        return "", ""

    key = entry.get("key") or entry.get("Key") or entry.get("api_key")
    secret = entry.get("secret") or entry.get("Secret") or entry.get("api_secret")

    key_str = str(key or "").strip()
    secret_str = str(secret or "").strip()
    return key_str, secret_str


class PodcastIndex(Plugin):
    """Search provider for PodcastIndex.org."""

    SUPPORTED_CMDLETS = frozenset({"search-file"})
    TABLE_AUTO_STAGES = {
        "podcastindex": ["download-file"],
        "podcastindex.episodes": ["download-file"],
    }

    @staticmethod
    def _format_duration(value: Any) -> str:
        def _to_seconds(v: Any) -> Optional[int]:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                try:
                    return max(0, int(v))
                except Exception:
                    return None
            if isinstance(v, str):
                text = v.strip()
                if not text:
                    return None
                if text.isdigit():
                    try:
                        return max(0, int(text))
                    except Exception:
                        return None
                # Accept common clock formats too.
                if ":" in text:
                    parts = [p.strip() for p in text.split(":") if p.strip()]
                    if len(parts) == 2 and all(p.isdigit() for p in parts):
                        m, s = parts
                        return max(0, int(m) * 60 + int(s))
                    if len(parts) == 3 and all(p.isdigit() for p in parts):
                        h, m, s = parts
                        return max(0, int(h) * 3600 + int(m) * 60 + int(s))
            return None

        total = _to_seconds(value)
        if total is None:
            return "" if value is None else str(value).strip()

        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        if h > 0:
            return f"{h:d}h{m:d}m{s:d}s"
        if m > 0:
            return f"{m:d}m{s:d}s"
        return f"{s:d}s"

    @staticmethod
    def _format_bytes(value: Any) -> str:
        """Format bytes using centralized utility."""
        return format_bytes(value)

    @staticmethod
    def _format_date_from_epoch(value: Any) -> str:
        if value is None:
            return ""
        try:
            import datetime

            ts = int(value)
            if ts <= 0:
                return ""
            return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            return ""

    @staticmethod
    def _extract_episode_categories(ep: Dict[str, Any]) -> List[str]:
        cats = ep.get("categories") or ep.get("category")
        out: List[str] = []

        if isinstance(cats, dict):
            for v in cats.values():
                if isinstance(v, str):
                    t = v.strip()
                    if t:
                        out.append(t)
        elif isinstance(cats, list):
            for v in cats:
                if isinstance(v, str):
                    t = v.strip()
                    if t:
                        out.append(t)
        elif isinstance(cats, str):
            t = cats.strip()
            if t:
                out.append(t)

        # Keep the table readable.
        dedup: List[str] = []
        seen: set[str] = set()
        for t in out:
            low = t.lower()
            if low in seen:
                continue
            seen.add(low)
            dedup.append(t)
        return dedup

    @staticmethod
    def _looks_like_episode(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        md = item.get("full_metadata")
        if not isinstance(md, dict):
            return False
        enc = md.get("enclosureUrl") or md.get("enclosure_url")
        if isinstance(enc, str) and enc.strip().startswith("http"):
            return True
        # Some pipelines may flatten episode fields.
        enc2 = item.get("enclosureUrl") or item.get("url")
        return isinstance(enc2, str) and enc2.strip().startswith("http")

    @staticmethod
    def _compute_sha256(filepath: Path) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any,
    ) -> bool:
        if not stage_is_last:
            return False
        if not selected_items:
            return False

        # Episode selection (terminal): download episodes to temp/output dir.
        if all(self._looks_like_episode(x) for x in selected_items):
            return self._handle_episode_download_selection(selected_items, ctx)

        # Podcast selection (terminal): expand into episode list.
        return self._handle_podcast_expand_selection(selected_items, ctx)

    def _handle_podcast_expand_selection(self, selected_items: List[Any], ctx: Any) -> bool:
        chosen: List[Dict[str, Any]] = [x for x in (selected_items or []) if isinstance(x, dict)]
        if not chosen:
            return False

        key, secret = _get_podcastindex_credentials(self.config or {})
        if not key or not secret:
            return False

        # Resolve feed id/url from the selected podcast row.
        item0 = chosen[0]
        feed_md = item0.get("full_metadata") if isinstance(item0.get("full_metadata"), dict) else {}
        feed_title = str(item0.get("title") or feed_md.get("title") or "Podcast").strip() or "Podcast"
        feed_id = None
        try:
            feed_id = int(feed_md.get("id")) if feed_md.get("id") is not None else None
        except Exception:
            feed_id = None
        feed_url = str(feed_md.get("url") or item0.get("path") or "").strip()

        try:
            from plugins.podcastindex.api import PodcastIndexClient

            client = PodcastIndexClient(key, secret)
            if feed_id:
                episodes = client.episodes_byfeedid(feed_id, max_results=200)
            else:
                episodes = client.episodes_byfeedurl(feed_url, max_results=200)
        except Exception as exc:
            log(f"[podcastindex] episode lookup failed: {exc}", file=sys.stderr)
            return True

        try:
            from SYS.result_table import Table
            from SYS.rich_display import stdout_console
        except Exception:
            return True

        table = Table(f"PodcastIndex Episodes: {feed_title}")._perseverance(True)
        table.set_table("podcastindex.episodes")
        try:
            table.set_value_case("preserve")
        except Exception:
            pass

        results_payload: List[Dict[str, Any]] = []
        for ep in episodes or []:
            if not isinstance(ep, dict):
                continue

            ep_title = str(ep.get("title") or "").strip() or "Unknown"
            enc_url = str(ep.get("enclosureUrl") or "").strip()
            page_url = str(ep.get("link") or "").strip()
            audio_url = enc_url or page_url
            if not audio_url:
                continue

            duration = ep.get("duration")
            size_bytes = ep.get("enclosureLength") or ep.get("enclosure_length")
            published = ep.get("datePublished") or ep.get("datePublishedPretty")
            published_text = self._format_date_from_epoch(published) or str(published or "").strip()

            sr = SearchResult(
                table="podcastindex",
                title=ep_title,
                path=audio_url,
                detail=feed_title,
                media_kind="audio",
                size_bytes=int(size_bytes) if str(size_bytes or "").isdigit() else None,
                columns=[
                    ("Title", ep_title),
                    ("Date", published_text),
                    ("Duration", self._format_duration(duration)),
                    ("Size", self._format_bytes(size_bytes)),
                    ("Url", audio_url),
                ],
                full_metadata={
                    **dict(ep),
                    "_feed": dict(feed_md) if isinstance(feed_md, dict) else {},
                },
            )

            table.add_result(sr)
            results_payload.append(sr.to_dict())

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

    def _handle_episode_download_selection(self, selected_items: List[Any], ctx: Any) -> bool:
        key, secret = _get_podcastindex_credentials(self.config or {})
        if not key or not secret:
            return False

        try:
            from SYS.config import resolve_output_dir
            output_dir = resolve_output_dir(self.config or {})
        except Exception:
            import tempfile
            output_dir = Path(tempfile.gettempdir())

        try:
            output_dir = Path(output_dir).expanduser()
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        try:
            from API.HTTP import download_direct_file
        except Exception:
            return True

        payloads: List[Dict[str, Any]] = []
        downloaded = 0

        for item in selected_items:
            if not isinstance(item, dict):
                continue
            md = item.get("full_metadata") if isinstance(item.get("full_metadata"), dict) else {}
            enc_url = str(md.get("enclosureUrl") or item.get("url") or item.get("path") or "").strip()
            if not enc_url or not enc_url.startswith("http"):
                continue

            title_hint = str(item.get("title") or md.get("title") or "episode").strip() or "episode"

            try:
                result_obj = download_direct_file(
                    enc_url,
                    Path(output_dir),
                    quiet=False,
                    suggested_filename=title_hint,
                )
            except Exception as exc:
                log(f"[podcastindex] download failed: {exc}", file=sys.stderr)
                continue

            downloaded_path = None
            try:
                downloaded_path = getattr(result_obj, "filepath", None)
            except Exception:
                downloaded_path = None
            if downloaded_path is None:
                try:
                    downloaded_path = getattr(result_obj, "file_path", None)
                except Exception:
                    downloaded_path = None
            if downloaded_path is None:
                try:
                    downloaded_path = getattr(result_obj, "path", None)
                except Exception:
                    downloaded_path = None

            try:
                local_path = Path(str(downloaded_path))
            except Exception:
                local_path = None
            if local_path is None or not local_path.exists():
                continue

            sha256 = ""
            try:
                sha256 = self._compute_sha256(local_path)
            except Exception:
                sha256 = ""

            tags: List[str] = []
            tags.append(f"title:{title_hint}")
            cats = self._extract_episode_categories(md) if isinstance(md, dict) else []
            for c in cats[:10]:
                tags.append(f"tag:{c}")

            payload: Dict[str, Any] = {
                "path": str(local_path),
                "hash": sha256,
                "title": title_hint,
                "action": "plugin:podcastindex.selector",
                "download_mode": "file",
                "store": "local",
                "media_kind": "audio",
                "tag": tags,
                "plugin": "podcastindex",
                "url": enc_url,
            }
            if isinstance(md, dict) and md:
                payload["full_metadata"] = dict(md)

            payloads.append(payload)
            downloaded += 1

        try:
            if payloads and hasattr(ctx, "set_last_result_items_only"):
                ctx.set_last_result_items_only(payloads)
        except Exception:
            pass

        if downloaded <= 0:
            return True

        try:
            from SYS.rich_display import stdout_console

            stdout_console().print(f"Downloaded {downloaded} episode(s) -> {output_dir}")
        except Exception:
            pass
        return True

    def validate(self) -> bool:
        key, secret = _get_podcastindex_credentials(self.config or {})
        return bool(key and secret)

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        _ = filters
        _ = kwargs

        key, secret = _get_podcastindex_credentials(self.config or {})
        if not key or not secret:
            return []

        try:
            from plugins.podcastindex.api import PodcastIndexClient

            client = PodcastIndexClient(key, secret)
            feeds = client.search_byterm(query, max_results=limit)
        except Exception as exc:
            log(f"[podcastindex] search failed: {exc}", file=sys.stderr)
            return []

        results: List[SearchResult] = []
        for feed in feeds[: max(0, int(limit))]:
            if not isinstance(feed, dict):
                continue

            title = str(feed.get("title") or "").strip() or "Unknown"
            author = str(feed.get("author") or feed.get("ownerName") or "").strip()
            feed_url = str(feed.get("url") or "").strip()
            site_url = str(feed.get("link") or "").strip()
            language = str(feed.get("language") or "").strip()

            episode_count_val = feed.get("episodeCount")
            episode_count = ""
            if episode_count_val is not None:
                try:
                    episode_count = str(int(episode_count_val))
                except Exception:
                    episode_count = str(episode_count_val).strip()

            path = feed_url or site_url or str(feed.get("id") or "").strip()

            columns = [
                ("Title", title),
                ("Author", author),
                ("Episodes", episode_count),
                ("Lang", language),
                ("Feed", feed_url),
            ]

            results.append(
                SearchResult(
                    table="podcastindex",
                    title=title,
                    path=path,
                    detail=author,
                    media_kind="audio",
                    columns=columns,
                    full_metadata=dict(feed),
                )
            )

        return results
