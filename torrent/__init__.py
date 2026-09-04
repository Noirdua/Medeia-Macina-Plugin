from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from API.requests_client import get_requests_session
from PluginCore.base import Plugin, SearchResult
from SYS.logger import debug, log
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
    def __init__(self) -> None:
        super().__init__("1337x.to", "https://1337x.to")

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

    def __init__(self) -> None:
        super().__init__("yts.mx", "https://yts.mx/api/v2")
        self.headers = {}

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
        return f"magnet:?xt=urn:btih:{info_hash}&dn={requests.utils.quote(name)}"

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
    TABLE_AUTO_STAGES = {"torrent": ["download-file"]}
    SUPPORTED_CMDLETS = frozenset({"search-file"})

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
