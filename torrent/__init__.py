from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from API.requests_client import get_requests_session
from PluginCore.base import Plugin, SearchResult
from SYS.logger import debug, log
from SYS.utils import sanitize_filename, unique_path
try:  # Preferred HTML parser
    from lxml import html as lxml_html
except Exception:  # pragma: no cover - optional
    lxml_html = None  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class TorrentInfo:
    name: str
    url: str
    seeders: int
    leechers: int
    size: str
    source: str
    category: Optional[str] = None
    uploader: Optional[str] = None
    magnet: Optional[str] = None


@dataclass
class SearchParams:
    name: str
    category: Optional[str] = None
    order_column: Optional[str] = None
    order_ascending: bool = False


_MAGNET_RE = re.compile(r"^magnet", re.IGNORECASE)
_DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
)


class Scraper:
    def __init__(self, name: str, base_url: str, timeout: float = 10.0) -> None:
        self.name = name
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
            )
        }
        self.params: Optional[SearchParams] = None

    def find(self, params: SearchParams, pages: int = 1) -> List[TorrentInfo]:
        self.params = params
        results: List[TorrentInfo] = []
        for page in range(1, max(1, pages) + 1):
            try:
                results.extend(self._get_page(page))
            except Exception as exc:
                debug(f"[{self.name}] page fetch failed: {exc}")
        return results

    def _get_page(self, page: int) -> List[TorrentInfo]:
        url, payload = self._request_data(page)
        try:
            resp = get_requests_session().get(
                url,
                params=payload,
                headers=self.headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return self._parse_search(resp)
        except Exception as exc:
            debug(f"[{self.name}] request failed: {exc}")
            return []

    def _request_data(self, page: int) -> tuple[str, Dict[str, Any]]:
        return self.base, {}

    def _parse_search(self, response: requests.Response) -> List[TorrentInfo]:  # pragma: no cover - interface
        raise NotImplementedError

    def _parse_detail(self, url: str) -> Optional[str]:  # optional override
        try:
            resp = get_requests_session().get(url, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            return self._parse_detail_response(resp)
        except Exception:
            return None

    def _parse_detail_response(self, response: requests.Response) -> Optional[str]:  # pragma: no cover - interface
        return None

    @staticmethod
    def _int_from_text(value: Any) -> int:
        try:
            return int(str(value).strip().replace(",", ""))
        except Exception:
            return 0


class NyaaScraper(Scraper):
    def __init__(self) -> None:
        super().__init__("nyaa.si", "https://nyaa.si")

    def _request_data(self, page: int) -> tuple[str, Dict[str, Any]]:
        params = self.params or SearchParams(name="")
        payload = {
            "p": page,
            "q": params.name,
            "c": params.category or "0_0",
            "f": "0",
        }
        if params.order_column:
            payload["s"] = params.order_column
            payload["o"] = "asc" if params.order_ascending else "desc"
        return f"{self.base}/", payload

    def _parse_search(self, response: requests.Response) -> List[TorrentInfo]:
        if lxml_html is None:
            return []
        doc = lxml_html.fromstring(response.text)
        rows = doc.xpath("//table//tbody/tr")
        results: List[TorrentInfo] = []
        for row in rows:
            cells = row.xpath("./td")
            if len(cells) < 7:
                continue
            category_cell, name_cell, links_cell, size_cell, _, seed_cell, leech_cell, *_ = cells

            name_links = name_cell.xpath("./a")
            name_tag = name_links[1] if len(name_links) > 1 else (name_links[0] if name_links else None)
            if name_tag is None:
                continue

            name = name_tag.get("title") or (name_tag.text_content() or "").strip()
            url = name_tag.get("href") or ""

            magnet_link = None
            magnet_candidates = links_cell.xpath('.//a[starts-with(@href,"magnet:")]/@href')
            if magnet_candidates:
                magnet_link = magnet_candidates[0]

            category_title = None
            cat_titles = category_cell.xpath(".//a/@title")
            if cat_titles:
                category_title = cat_titles[0]

            results.append(
                TorrentInfo(
                    name=name,
                    url=f"{self.base}{url}",
                    seeders=self._int_from_text(seed_cell.text_content()),
                    leechers=self._int_from_text(leech_cell.text_content()),
                    size=(size_cell.text_content() or "").strip(),
                    source=self.name,
                    category=category_title,
                    magnet=magnet_link,
                )
            )
        return results


class X1337Scraper(Scraper):
    MIRRORS = (
        "https://1337x.to",
        "https://www.1337x.to",
        "https://1337x.st",
        "https://x1337x.ws",
    )

    def __init__(self) -> None:
        super().__init__("1337x.to", self.MIRRORS[0])

    def _get_page(self, page: int) -> List[TorrentInfo]:
        last_exc: Optional[Exception] = None
        for base in self.MIRRORS:
            self.base = base
            try:
                url, payload = self._request_data(page)
                resp = get_requests_session().get(
                    url,
                    params=payload or None,
                    headers=self.headers,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                parsed = self._parse_search(resp)
                if parsed:
                    return parsed
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            debug(f"[{self.name}] request failed: {last_exc}")
        return []

    def _request_data(self, page: int) -> tuple[str, Dict[str, Any]]:
        params = self.params or SearchParams(name="")
        order = None
        if params.order_column:
            direction = "asc" if params.order_ascending else "desc"
            order = f"{params.order_column}/{direction}"

        category = params.category
        name = requests.utils.quote(params.name)

        if order and category:
            path = f"/sort-category-search/{name}/{category}/{order}"
        elif category:
            path = f"/category-search/{name}/{category}"
        elif order:
            path = f"/sort-search/{name}/{order}"
        else:
            path = f"/search/{name}"

        url = f"{self.base}{path}/{page}/"
        return url, {}

    def _parse_search(self, response: requests.Response) -> List[TorrentInfo]:
        if lxml_html is None:
            return []
        doc = lxml_html.fromstring(response.text)
        rows = doc.xpath("//table//tbody/tr")
        results: List[TorrentInfo] = []
        for row in rows:
            cells = row.xpath("./td")
            if len(cells) < 6:
                continue
            name_cell, seeds_cell, leech_cell, _, size_cell, uploader_cell = cells

            links = name_cell.xpath(".//a")
            if len(links) < 2:
                continue

            torrent_path = links[1].get("href")
            torrent_url = f"{self.base}{torrent_path}" if torrent_path else ""

            info = TorrentInfo(
                name=(links[1].text_content() or "").strip(),
                url=torrent_url,
                seeders=self._int_from_text(seeds_cell.text_content()),
                leechers=self._int_from_text(leech_cell.text_content()),
                size=(size_cell.text_content() or "").strip().replace(",", ""),
                source=self.name,
                uploader=(uploader_cell.text_content() or "").strip() if uploader_cell is not None else None,
            )

            if not info.magnet:
                info.magnet = self._parse_detail(info.url)
            results.append(info)
        return results

    def _parse_detail_response(self, response: requests.Response) -> Optional[str]:
        if lxml_html is None:
            return None
        doc = lxml_html.fromstring(response.text)
        links = doc.xpath("//main//a[starts-with(@href,'magnet:')]/@href")
        return links[0] if links else None


class YTSScraper(Scraper):
    TRACKERS = "&tr=".join(
        [
            "udp://open.demonii.com:1337/announce",
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://tracker.leechers-paradise.org:6969",
        ]
    )
    API_BASES = (
        "https://yts.mx/api/v2",
        "https://yts.lt/api/v2",
        "https://yts.ag/api/v2",
    )

    def __init__(self) -> None:
        super().__init__("yts.mx", self.API_BASES[0])
        self.headers = {}

    def _get_page(self, page: int) -> List[TorrentInfo]:
        params = self.params or SearchParams(name="")
        payload = {
            "limit": 50,
            "page": page,
            "query_term": params.name,
            "sort_by": "seeds",
            "order_by": "desc" if not params.order_ascending else "asc",
        }
        last_exc: Optional[Exception] = None
        for base in self.API_BASES:
            try:
                resp = get_requests_session().get(
                    f"{base}/list_movies.json",
                    params=payload,
                    headers=self.headers,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                self.base = base
                return self._parse_search(resp)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            debug(f"[{self.name}] request failed: {last_exc}")
        return []

    def _request_data(self, page: int) -> tuple[str, Dict[str, Any]]:
        params = self.params or SearchParams(name="")
        payload = {
            "limit": 50,
            "page": page,
            "query_term": params.name,
            "sort_by": "seeds",
            "order_by": "desc" if not params.order_ascending else "asc",
        }
        return f"{self.base}/list_movies.json", payload

    def _parse_search(self, response: requests.Response) -> List[TorrentInfo]:
        results: List[TorrentInfo] = []
        data = response.json()
        if data.get("status") != "ok":
            return results
        movies = (data.get("data") or {}).get("movies") or []
        for movie in movies:
            torrents = movie.get("torrents") or []
            if not torrents:
                continue
            tor = max(torrents, key=lambda t: t.get("seeds", 0))
            name = movie.get("title") or "unknown"
            info = TorrentInfo(
                name=name,
                url=str(movie.get("id") or ""),
                seeders=int(tor.get("seeds", 0) or 0),
                leechers=int(tor.get("peers", 0) or 0),
                size=str(tor.get("size") or ""),
                source=self.name,
                category=(movie.get("genres") or [None])[0],
                magnet=self._build_magnet(tor, name),
            )
            results.append(info)
        return results

    def _build_magnet(self, torrent: Dict[str, Any], name: str) -> str:
        return (
            f"magnet:?xt=urn:btih:{torrent.get('hash')}"
            f"&dn={requests.utils.quote(name)}&tr={self.TRACKERS}"
        )


class ApiBayScraper(Scraper):
    """Scraper for apibay.org (The Pirate Bay API clone)."""

    def __init__(self) -> None:
        super().__init__("apibay.org", "https://apibay.org")

    def _request_data(self, page: int) -> tuple[str, Dict[str, Any]]:
        _ = page  # single-page API
        params = self.params or SearchParams(name="")
        return f"{self.base}/q.php", {"q": params.name}

    def _parse_search(self, response: requests.Response) -> List[TorrentInfo]:
        results: List[TorrentInfo] = []
        try:
            data = response.json()
        except Exception:
            return results
        if not isinstance(data, list):
            return results

        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            info_hash = str(item.get("info_hash") or "").strip()
            if not name or not info_hash:
                continue

            magnet = self._build_magnet(info_hash, name)
            seeders = self._int_from_text(item.get("seeders"))
            leechers = self._int_from_text(item.get("leechers"))
            size_raw = str(item.get("size") or "").strip()
            size_fmt = self._format_size(size_raw)

            results.append(
                TorrentInfo(
                    name=name,
                    url=f"{self.base}/description.php?id={item.get('id')}",
                    seeders=seeders,
                    leechers=leechers,
                    size=size_fmt,
                    source=self.name,
                    category=str(item.get("category") or ""),
                    uploader=str(item.get("username") or ""),
                    magnet=magnet,
                )
            )
        return results

    @staticmethod
    def _build_magnet(info_hash: str, name: str) -> str:
        return (
            f"magnet:?xt=urn:btih:{info_hash}"
            f"&dn={requests.utils.quote(name)}"
            f"&tr={'&tr='.join(_DEFAULT_TRACKERS)}"
        )

    @staticmethod
    def _format_size(size_raw: str) -> str:
        try:
            size_int = int(size_raw)
            if size_int <= 0:
                return size_raw
            gb = size_int / (1024 ** 3)
            if gb >= 1:
                return f"{gb:.1f} GB"
            mb = size_int / (1024 ** 2)
            return f"{mb:.1f} MB"
        except Exception:
            return size_raw


class Torrent(Plugin):
    PLUGIN_NAME = "torrent"
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_AUTHOR = "Medeia"
    PLUGIN_DESCRIPTION = "Torrent site search and BitTorrent download (The Pirate Bay, YTS, Nyaa, 1337x)."
    PLUGIN_REQUIRES = ("libtorrent",)
    TABLE_AUTO_STAGES = {"torrent": ["download-file"]}
    SUPPORTED_CMDLETS = frozenset({"search-file", "download-file"})
    prefers_transfer_progress = True

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "downloader",
                "label": "Downloader",
                "type": "enum",
                "choices": ["alldebrid", "libtorrent"],
                "default": "alldebrid",
                "help": "alldebrid sends magnets to AllDebrid. libtorrent downloads locally.",
            },
        ]

    def config_helper_text(self) -> str:
        return "Choose AllDebrid for debrid unlocks, or libtorrent for a local BitTorrent download."

    def _downloader_mode(self) -> str:
        root = {}
        try:
            root = self.plugin_config_root()
        except Exception:
            root = {}
        if not root and isinstance(self.config, dict):
            plugin_cfg = self.config.get("plugin")
            if isinstance(plugin_cfg, dict):
                entry = plugin_cfg.get("torrent")
                if isinstance(entry, dict):
                    root = entry
        raw = str((root or {}).get("downloader") or "alldebrid").strip().lower()
        if raw in {"libtorrent", "local", "internal", "bittorrent"}:
            return "libtorrent"
        return "alldebrid"

    @property
    def preserve_order(self) -> bool:
        return True

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.scrapers: List[Scraper] = []
        # JSON APIs (no lxml dependency)
        self.scrapers.append(ApiBayScraper())
        self.scrapers.append(YTSScraper())
        # HTML scrapers require lxml
        if lxml_html is not None:
            self.scrapers.append(NyaaScraper())
            self.scrapers.append(X1337Scraper())
        else:
            log("[torrent] lxml not installed; skipping Nyaa/1337x scrapers", file=None)

    def validate(self) -> bool:
        return bool(self.scrapers)

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> List[SearchResult]:
        q = str(query or "").strip()
        if not q:
            return []

        params = SearchParams(name=q, order_column="seeders", order_ascending=False)
        results: List[TorrentInfo] = []

        for scraper in self.scrapers:
            try:
                scraped = scraper.find(params, pages=1)
                results.extend(scraped)
            except Exception as exc:
                debug(f"[torrent] scraper {scraper.name} failed: {exc}")
                continue

        results = sorted(results, key=lambda r: r.seeders, reverse=True)
        if limit and limit > 0:
            results = results[:limit]

        out: List[SearchResult] = []
        for item in results:
            path = item.magnet or item.url
            columns = [
                ("TITLE", item.name),
                ("Seeds", str(item.seeders)),
                ("Leechers", str(item.leechers)),
                ("Size", item.size or ""),
                ("Source", item.source),
            ]
            if item.uploader:
                columns.append(("Uploader", item.uploader))

            md = {
                "magnet": item.magnet,
                "url": item.url,
                "source": item.source,
                "seeders": item.seeders,
                "leechers": item.leechers,
                "size": item.size,
            }
            if item.uploader:
                md["uploader"] = item.uploader

            out.append(
                SearchResult(
                    table="torrent",
                    title=item.name,
                    path=path,
                    detail=f"Seeds:{item.seeders} | Size:{item.size}",
                    annotations=[item.source],
                    media_kind="other",
                    columns=columns,
                    full_metadata=md,
                    tag={"torrent"},
                )
            )
        return out

    @staticmethod
    def _magnet_from_result(result: SearchResult) -> str:
        path = str(getattr(result, "path", "") or "").strip()
        if path.lower().startswith("magnet:"):
            return path
        md = getattr(result, "full_metadata", None) or {}
        if isinstance(md, dict):
            magnet = str(md.get("magnet") or "").strip()
            if magnet.lower().startswith("magnet:"):
                return magnet
        return ""

    @staticmethod
    def _with_trackers(magnet: str) -> str:
        text = str(magnet or "").strip()
        if not text.lower().startswith("magnet:"):
            return text
        if "&tr=" in text or "?tr=" in text:
            return text
        extra = "&".join(f"tr={requests.utils.quote(tr, safe='')}" for tr in _DEFAULT_TRACKERS)
        sep = "&" if "?" in text else "?"
        return f"{text}{sep}{extra}"

    def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
        if self._downloader_mode() == "alldebrid":
            return None
        magnet = self._with_trackers(self._magnet_from_result(result))
        if not magnet:
            return None
        title = str(getattr(result, "title", "") or "torrent").strip() or "torrent"
        try:
            return _download_with_libtorrent(
                magnet,
                Path(output_dir),
                title=title,
                progress=(self.config or {}).get("_pipeline_progress") if isinstance(self.config, dict) else None,
            )
        except Exception as exc:
            debug(f"[torrent] libtorrent download failed: {exc}")
            return None


def _download_with_libtorrent(
    magnet: str,
    output_dir: Path,
    *,
    title: str,
    progress: Any = None,
    metadata_timeout: int = 90,
    stall_timeout: int = 120,
    overall_timeout: int = 1800,
) -> Optional[Path]:
    try:
        import libtorrent as lt
    except Exception:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    save_dir = unique_path(output_dir / sanitize_filename(title or "torrent"))
    save_dir.mkdir(parents=True, exist_ok=True)

    ses = lt.session()
    try:
        settings = {
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "alert_mask": 0,
        }
        apply_settings = getattr(ses, "apply_settings", None)
        if callable(apply_settings):
            apply_settings(settings)
        else:
            ses.set_settings(settings)
    except Exception:
        pass
    for host, port in (
        ("router.bittorrent.com", 6881),
        ("dht.transmissionbt.com", 6881),
        ("router.utorrent.com", 6881),
    ):
        try:
            ses.add_dht_router(host, port)
        except Exception:
            continue
    try:
        ses.start_dht()
    except Exception:
        pass

    handle = None
    try:
        if hasattr(lt, "parse_magnet_uri") and hasattr(ses, "add_torrent"):
            params = lt.parse_magnet_uri(magnet)
            params.save_path = str(save_dir)
            handle = ses.add_torrent(params)
        else:
            handle = lt.add_magnet_uri(ses, magnet, {"save_path": str(save_dir)})
    except Exception as exc:
        debug(f"[torrent] add magnet failed: {exc}")
        return None

    def _status():
        return handle.status()

    def _has_metadata() -> bool:
        try:
            if hasattr(handle, "has_metadata"):
                return bool(handle.has_metadata())
            return bool(_status().has_metadata)
        except Exception:
            return False

    def _is_done() -> bool:
        try:
            st = _status()
            if bool(getattr(st, "is_seeding", False)):
                return True
            if float(getattr(st, "progress", 0) or 0) >= 0.999:
                return True
            state = getattr(st, "state", None)
            finished = getattr(lt.torrent_status, "finished", None)
            seeding = getattr(lt.torrent_status, "seeding", None)
            return state in {finished, seeding} and state is not None
        except Exception:
            return False

    started = time.monotonic()
    last_progress = 0.0
    last_change = started
    got_metadata = False
    transfer_started = False
    label = title or "torrent"
    try:
        while time.monotonic() - started < overall_timeout:
            now = time.monotonic()
            if not got_metadata:
                if _has_metadata():
                    got_metadata = True
                    last_change = now
                elif now - started > metadata_timeout:
                    return None
            elif _is_done():
                break
            else:
                st = _status()
                pct = float(getattr(st, "progress", 0) or 0)
                if pct > last_progress + 0.001:
                    last_progress = pct
                    last_change = now
                elif now - last_change > stall_timeout:
                    return None
                total = int(getattr(st, "total_wanted", 0) or 0) or None
                completed = int(round((pct * total))) if total else None
                if progress is not None:
                    try:
                        if not transfer_started and hasattr(progress, "begin_transfer"):
                            progress.begin_transfer(label=label, total=total)
                            transfer_started = True
                        if hasattr(progress, "update_transfer"):
                            progress.update_transfer(
                                label=label,
                                completed=completed,
                                total=total,
                            )
                    except Exception:
                        pass
            time.sleep(0.5)
        else:
            return None

        files: List[Path] = []
        try:
            info = handle.torrent_file() if hasattr(handle, "torrent_file") else handle.get_torrent_info()
            storage = getattr(info, "files", None)
            fs = storage() if callable(storage) else info.files()
            count = fs.num_files() if hasattr(fs, "num_files") else len(fs)
            for idx in range(int(count)):
                name = fs.file_path(idx) if hasattr(fs, "file_path") else fs[idx].path
                candidate = save_dir / str(name)
                if candidate.is_file():
                    files.append(candidate)
        except Exception:
            files = [p for p in save_dir.rglob("*") if p.is_file()]

        if not files:
            files = [p for p in save_dir.rglob("*") if p.is_file()]
        if not files:
            return None
        if len(files) == 1:
            return files[0]
        return max(files, key=lambda p: p.stat().st_size if p.exists() else 0)
    finally:
        if progress is not None and transfer_started and hasattr(progress, "finish_transfer"):
            try:
                progress.finish_transfer(label=label)
            except Exception:
                pass
        try:
            if handle is not None:
                ses.remove_torrent(handle)
        except Exception:
            pass
        try:
            ses.pause()
        except Exception:
            pass
