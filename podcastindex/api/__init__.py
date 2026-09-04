"""PodcastIndex.org API integration.

Docs: https://podcastindex-org.github.io/docs-api/

Authentication headers required for most endpoints:
- User-Agent
- X-Auth-Key
- X-Auth-Date
- Authorization (sha1(apiKey + apiSecret + unixTime))
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from API.base import API, ApiError


class PodcastIndexError(ApiError):
    pass


def build_auth_headers(
    api_key: str,
    api_secret: str,
    *,
    unix_time: Optional[int] = None,
    user_agent: str = "downlow/1.0",
) -> Dict[str, str]:
    """Build PodcastIndex auth headers.

    The API expects X-Auth-Date to be the current UTC unix epoch time
    (integer string), and Authorization to be the SHA-1 hex digest of
    `api_key + api_secret + X-Auth-Date`.
    """

    key = str(api_key or "").strip()
    secret = str(api_secret or "").strip()
    if not key or not secret:
        raise PodcastIndexError("PodcastIndex api key/secret are required")

    ts = int(unix_time if unix_time is not None else time.time())
    ts_str = str(ts)

    token = hashlib.sha1((key + secret + ts_str).encode("utf-8")).hexdigest()

    return {
        "User-Agent": str(user_agent or "downlow/1.0"),
        "X-Auth-Key": key,
        "X-Auth-Date": ts_str,
        "Authorization": token,
    }


class PodcastIndexClient(API):
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = "https://api.podcastindex.org/api/1.0",
        user_agent: str = "downlow/1.0",
        timeout: float = 30.0,
    ):
        super().__init__(base_url=base_url, timeout=timeout)
        self.api_key = str(api_key or "").strip()
        self.api_secret = str(api_secret or "").strip()
        self.user_agent = str(user_agent or "downlow/1.0")

        if not self.api_key or not self.api_secret:
            raise PodcastIndexError("PodcastIndex api key/secret are required")

    def _get(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = build_auth_headers(
            self.api_key,
            self.api_secret,
            user_agent=self.user_agent,
        )
        return self._get_json(path, params=params, headers=headers)

    def search_byterm(self, query: str, *, max_results: int = 10) -> List[Dict[str, Any]]:
        q = str(query or "").strip()
        if not q:
            return []

        max_int = int(max_results)
        if max_int < 1:
            max_int = 1
        if max_int > 1000:
            max_int = 1000

        data = self._get(
            "search/byterm",
            params={
                "q": q,
                "max": max_int,
            },
        )

        feeds = data.get("feeds")
        return feeds if isinstance(feeds, list) else []

    def episodes_byfeedid(self, feed_id: int | str, *, max_results: int = 50) -> List[Dict[str, Any]]:
        """List recent episodes for a feed by its PodcastIndex feed id."""
        try:
            feed_id_int = int(feed_id)
        except Exception:
            feed_id_int = None
        if feed_id_int is None or feed_id_int <= 0:
            return []

        max_int = int(max_results)
        if max_int < 1:
            max_int = 1
        if max_int > 1000:
            max_int = 1000

        data = self._get(
            "episodes/byfeedid",
            params={
                "id": feed_id_int,
                "max": max_int,
            },
        )

        items = data.get("items")
        if isinstance(items, list):
            return items
        episodes = data.get("episodes")
        return episodes if isinstance(episodes, list) else []

    def episodes_byfeedurl(self, feed_url: str, *, max_results: int = 50) -> List[Dict[str, Any]]:
        """List recent episodes for a feed by its RSS URL."""
        url = str(feed_url or "").strip()
        if not url:
            return []

        max_int = int(max_results)
        if max_int < 1:
            max_int = 1
        if max_int > 1000:
            max_int = 1000

        data = self._get(
            "episodes/byfeedurl",
            params={
                "url": url,
                "max": max_int,
            },
        )

        items = data.get("items")
        if isinstance(items, list):
            return items
        episodes = data.get("episodes")
        return episodes if isinstance(episodes, list) else []
