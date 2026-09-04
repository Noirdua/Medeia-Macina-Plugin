"""AllDebrid API integration for converting free links to direct downloads.

AllDebrid is a debrid service that unlocks free file hosters and provides direct download links.
API docs: https://docs.alldebrid.com/#general-informations
"""

from __future__ import annotations

import json
import logging
import sys
import time

from typing import Any, Dict, Optional, Set, List, Sequence, Tuple
from urllib.parse import urlparse

from SYS.logger import log, debug
from API.HTTP import HTTPClient

logger = logging.getLogger(__name__)


class AllDebridError(Exception):
    """Raised when AllDebrid API request fails."""

    pass


# Cache for supported hosters (domain -> host info)
_SUPPORTED_HOSTERS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_CACHE_TIMESTAMP: float = 0
_CACHE_DURATION: float = 3600  # 1 hour

# Cache for init-time connectivity checks (api_key fingerprint -> (ok, reason))
_INIT_CHECK_CACHE: Dict[str,
                        Tuple[bool,
                              Optional[str]]] = {}


def _ping_alldebrid(base_url: str) -> Tuple[bool, Optional[str]]:
    """Ping the AllDebrid API base URL (no API key required)."""
    try:
        url = str(base_url or "").rstrip("/") + "/ping"
        with HTTPClient(timeout=10.0,
                        headers={
                            "User-Agent": "downlow/1.0"
                        }) as client:
            response = client.get(url)
            data = json.loads(response.content.decode("utf-8"))
            if data.get("status") == "success" and data.get("data",
                                                            {}).get("ping") == "pong":
                return True, None
            return False, "Invalid API response"
    except Exception as exc:
        return False, str(exc)


class AllDebridClient:
    """Client for AllDebrid API."""

    # Default to v4 for most endpoints.
    # Some endpoints have a newer /v4.1/ variant (e.g., magnet/status, user/hosts, pin/get).
    BASE_URL = "https://api.alldebrid.com/v4"
    BASE_URL_V41 = "https://api.alldebrid.com/v4.1"

    # Endpoints documented as POST in v4 API. Since 2025-01-15 the API defaults
    # to POST for data-carrying endpoints (agent/version params were removed).
    _POST_ENDPOINTS: Set[str] = {
        "pin/check",
        "user/verif",
        "user/verif/resend",
        "user/notification/clear",
        "link/infos",
        "link/redirector",
        "link/unlock",
        "link/streaming",
        "link/delayed",
        "magnet/upload",
        "magnet/upload/file",
        "magnet/status",  # v4.1 variant exists; method stays POST
        "magnet/files",
        "magnet/delete",
        "magnet/restart",
        "user/links/save",
        "user/links/delete",
        "user/history/delete",
        "voucher/get",
        "voucher/generate",
    }

    def __init__(self, api_key: str):
        """Initialize AllDebrid client with API key.

        Args:
            api_key: AllDebrid API key from config
        """
        self.api_key = api_key.strip()
        if not self.api_key:
            raise AllDebridError("AllDebrid API key is empty")
        self.base_url = self.BASE_URL  # Start with v4

        # Init-time availability validation (cached per process)
        fingerprint = f"base:{self.base_url}"  # /ping does not require the api key
        cached = _INIT_CHECK_CACHE.get(fingerprint)
        if cached is None:
            ok, reason = _ping_alldebrid(self.base_url)
            _INIT_CHECK_CACHE[fingerprint] = (ok, reason)
        else:
            ok, reason = cached

        if not ok:
            raise AllDebridError(reason or "AllDebrid unavailable")

    def _request(
        self,
        endpoint: str,
        params: Optional[Dict[str,
                              Any]] = None,
        *,
        method: Optional[str] = None,
    ) -> Dict[str,
              Any]:
        """Make a request to AllDebrid API.

        Args:
            endpoint: API endpoint (e.g., "user/profile", "link/unlock")
            params: Query parameters

        Returns:
            Parsed JSON response

        Raises:
            AllDebridError: If request fails or API returns error
        """
        if params is None:
            params = {}

        # Determine HTTP method (v4 docs default to POST for most write/unlock endpoints).
        if method is None:
            method = "POST" if endpoint in self._POST_ENDPOINTS else "GET"
        method = str(method).upper().strip() or "GET"

        # Auth header is the preferred mechanism per v4.1 docs.
        # Keep apikey in params too for backward compatibility.
        request_params: Dict[str, Any] = dict(params)
        request_params["apikey"] = self.api_key

        url = f"{self.base_url}/{endpoint}"

        # Avoid logging full URLs with query params (can leak apikey).
        logger.debug(f"[AllDebrid] {method} {endpoint} @ {self.base_url}")

        try:
            headers = {
                "User-Agent": "downlow/1.0",
                "Authorization": f"Bearer {self.api_key}",
            }
            # Pass timeout to HTTPClient init.
            with HTTPClient(timeout=30.0, headers=headers) as client:
                try:
                    if method == "POST":
                        response = client.post(url, data=request_params)
                    else:
                        response = client.get(url, params=request_params)
                    response.raise_for_status()
                except Exception as req_err:
                    # Log detailed error info
                    logger.error(
                        f"[AllDebrid] Request error to {endpoint}: {req_err}",
                        exc_info=True
                    )
                    if hasattr(req_err,
                               "response"
                               ) and req_err.response is not None:  # type: ignore
                        try:
                            error_body = req_err.response.content.decode(
                                "utf-8"
                            )  # type: ignore
                            logger.error(
                                f"[AllDebrid] Response body: {error_body[:200]}"
                            )
                        except Exception:
                            pass
                    raise

                data = json.loads(response.content.decode("utf-8"))
                logger.debug(f"[AllDebrid] Response status: {response.status_code}")

                # Check for API errors
                if data.get("status") == "error":
                    error_msg = data.get("error",
                                         {}).get("message",
                                                 "Unknown error")
                    logger.error(f"[AllDebrid] API error: {error_msg}")
                    raise AllDebridError(f"AllDebrid API error: {error_msg}")

                return data
        except AllDebridError:
            raise
        except Exception as exc:
            error_msg = f"AllDebrid request failed: {exc}"
            logger.error(f"[AllDebrid] {error_msg}", exc_info=True)
            raise AllDebridError(error_msg)

    def unlock_link(self, link: str) -> Optional[str]:
        """Unlock a restricted link and get direct download URL.

        Args:
            link: Restricted link to unlock

        Returns:
            Direct download URL, or None if already unrestricted

        Raises:
            AllDebridError: If unlock fails
        """
        if not link.startswith(("http://", "https://")):
            raise AllDebridError(f"Invalid URL: {link}")

        try:
            response = self._request("link/unlock",
                                     {
                                         "link": link
                                     })

            # Check if unlock was successful
            if response.get("status") == "success":
                data = response.get("data",
                                    {})

                # AllDebrid returns the download info in 'link' field
                if "link" in data:
                    return data["link"]

                # Alternative: check for 'file' field
                if "file" in data:
                    return data["file"]

                # If no direct link, return the input link
                return link

            return None
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to unlock link: {exc}")

    def _link_delayed(self, delayed_id: int) -> Dict[str, Any]:
        """Poll delayed link status."""

        try:
            resp = self._request("link/delayed", {"id": int(delayed_id)})
            if resp.get("status") != "success":
                raise AllDebridError("link/delayed returned error status")
            data = resp.get("data") or {}
            return data if isinstance(data, dict) else {}
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to poll delayed link: {exc}")

    def resolve_unlock_link(
        self,
        link: str,
        *,
        poll: bool = True,
        max_wait_seconds: int = 30,
        poll_interval_seconds: int = 5,
    ) -> Optional[str]:
        """Unlock a link and handle delayed links by polling link/delayed."""

        try:
            resp = self._request("link/unlock", {"link": link})
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to unlock link: {exc}")

        if resp.get("status") != "success":
            return None

        data = resp.get("data") or {}
        if not isinstance(data, dict):
            return None

        # Immediate link ready
        for key in ("link", "file"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

        delayed_id = data.get("delayed")
        if not poll or delayed_id is None:
            return None

        try:
            delayed_int = int(delayed_id)
        except Exception:
            return None

        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            time.sleep(max(1, poll_interval_seconds))
            status_data = self._link_delayed(delayed_int)
            status = status_data.get("status")
            if status == 2:
                link_val = status_data.get("link")
                if isinstance(link_val, str) and link_val.strip():
                    return link_val.strip()
                return None
            if status == 3:
                raise AllDebridError("Delayed link generation failed")

        return None
    def check_host(self, hostname: str) -> Dict[str, Any]:
        """Check if a host is supported by AllDebrid.

        Args:
            hostname: Hostname to check (e.g., "uploadhaven.com")

        Returns:
            Host information dict with support status

        Raises:
            AllDebridError: If request fails
        """
        # The v4 API does not expose a `/host` endpoint. Use `/hosts/domains` and
        # check membership.
        if not hostname:
            return {}

        try:
            host = str(hostname).strip().lower()
            if host.startswith("www."):
                host = host[4:]

            domains = self.get_supported_hosters()
            if not domains:
                return {}

            for category in ("hosts", "streams", "redirectors"):
                values = domains.get(category)
                if isinstance(values,
                              list) and any(str(d).lower() == host for d in values):
                    return {
                        "supported": True,
                        "category": category,
                        "domain": host
                    }

            return {
                "supported": False,
                "domain": host
            }
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to check host: {exc}")

    def get_user_info(self) -> Dict[str, Any]:
        """Get current user account information.

        Returns:
            User information dict

        Raises:
            AllDebridError: If request fails
        """
        try:
            # v4 endpoint is `/user`
            response = self._request("user")

            if response.get("status") == "success":
                return response.get("data",
                                    {})

            return {}
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to get user info: {exc}")

    def get_supported_hosters(self) -> Dict[str, Dict[str, Any]]:
        """Get list of all supported hosters from AllDebrid API.

        Returns:
            Dict with keys `hosts`, `streams`, `redirectors` each containing an array
            of domains.

        Raises:
            AllDebridError: If request fails
        """
        try:
            response = self._request("hosts/domains")

            if response.get("status") == "success":
                data = response.get("data",
                                    {})
                return data if isinstance(data,
                                          dict) else {}

            return {}
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to get supported hosters: {exc}")

    def magnet_add(self, magnet_uri: str) -> Dict[str, Any]:
        """Submit a magnet link or torrent hash to AllDebrid for processing.

        AllDebrid will download the torrent content and store it in the account.
        Processing time varies based on torrent size and availability.

        Args:
            magnet_uri: Magnet URI (magnet:?xt=urn:btih:...) or torrent hash

        Returns:
            Dict with magnet info:
                - id: Magnet ID (int) - needed for status checks
                - name: Torrent name
                - hash: Torrent hash
                - size: Total file size (bytes)
                - ready: Boolean - True if already available

        Raises:
            AllDebridError: If submit fails (requires premium, invalid magnet, etc)
        """
        if not magnet_uri:
            raise AllDebridError("Magnet URI is empty")

        try:
            # API endpoint: POST /v4/magnet/upload
            # Format: /magnet/upload?apikey=key&magnets[]=magnet:?xt=...
            response = self._request("magnet/upload",
                                     {
                                         "magnets[]": magnet_uri
                                     })

            if response.get("status") == "success":
                data = response.get("data",
                                    {})
                magnets = data.get("magnets", [])

                if magnets and len(magnets) > 0:
                    magnet_info = magnets[0]

                    # Check for errors in the magnet response
                    if "error" in magnet_info:
                        error = magnet_info["error"]
                        error_msg = error.get("message", "Unknown error")
                        raise AllDebridError(f"Magnet error: {error_msg}")

                    return magnet_info

                raise AllDebridError("No magnet data in response")

            raise AllDebridError(f"API error: {response.get('error', 'Unknown')}")
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to submit magnet: {exc}")

    def magnet_status(self,
                      magnet_id: int,
                      include_files: bool = False) -> Dict[str,
                                                           Any]:
        """Get status of a magnet currently being processed or stored.

        Status codes:
            0-3: Processing (in queue, downloading, compressing, uploading)
            4: Ready (files available for download)
            5-15: Error (upload failed, not downloaded in 20min, too big, etc)

        Args:
            magnet_id: Magnet ID from magnet_add()
            include_files: If True, includes file list in response

        Returns:
            Dict with status info:
                - id: Magnet ID
                - filename: Torrent name
                - size: Total size (bytes)
                - status: Human-readable status
                - statusCode: Numeric code (0-15)
                - downloaded: Bytes downloaded so far
                - uploaded: Bytes uploaded so far
                - seeders: Number of seeders
                - downloadSpeed: Current speed (bytes/sec)
                - uploadSpeed: Current speed (bytes/sec)
                - files: (optional) Array of file objects when include_files=True
                    Each file: {n: name, s: size, l: download_link}

        Raises:
            AllDebridError: If status check fails
        """
        if not isinstance(magnet_id, int) or magnet_id <= 0:
            raise AllDebridError(f"Invalid magnet ID: {magnet_id}")

        try:
            # Use v4.1 endpoint for better response format
            # Temporarily override base_url for this request
            old_base = self.base_url
            self.base_url = self.BASE_URL_V41

            try:
                response = self._request("magnet/status",
                                         {
                                             "id": str(magnet_id)
                                         })
            finally:
                self.base_url = old_base

            if response.get("status") == "success":
                data = response.get("data",
                                    {})
                magnets = data.get("magnets",
                                   {})

                # Handle both list and dict responses
                if isinstance(magnets, list) and len(magnets) > 0:
                    return magnets[0]
                elif isinstance(magnets, dict) and magnets:
                    return magnets

                raise AllDebridError(f"No magnet found with ID {magnet_id}")

            raise AllDebridError(f"API error: {response.get('error', 'Unknown')}")
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to get magnet status: {exc}")

    def magnet_list(self) -> List[Dict[str, Any]]:
        """List magnets stored in the AllDebrid account.

        The AllDebrid API returns an array of magnets when calling the status
        endpoint without an id.

        Returns:
            List of magnet objects.
        """
        try:
            # Use v4.1 endpoint for better response format
            old_base = self.base_url
            self.base_url = self.BASE_URL_V41
            try:
                response = self._request("magnet/status")
            finally:
                self.base_url = old_base

            if response.get("status") != "success":
                return []

            data = response.get("data",
                                {})
            magnets = data.get("magnets", [])

            if isinstance(magnets, list):
                return [m for m in magnets if isinstance(m, dict)]

            # Some API variants may return a dict.
            if isinstance(magnets, dict):
                # If it's a single magnet dict, wrap it; if it's an id->magnet mapping, return values.
                if "id" in magnets:
                    return [magnets]
                return [m for m in magnets.values() if isinstance(m, dict)]

            return []
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to list magnets: {exc}")

    def magnet_status_live(
        self,
        magnet_id: int,
        session: Optional[int] = None,
        counter: int = 0
    ) -> Dict[str,
              Any]:
        """Get live status of a magnet using delta sync mode.

        The live mode endpoint provides real-time progress by only sending
        deltas (changed fields) instead of full status on each call. This
        reduces bandwidth and server load compared to regular polling.

        Note: The "live" designation refers to the delta-sync mode where you
        maintain state locally and apply diffs from the API, not a streaming
        endpoint. Regular magnet_status() polling is simpler for single magnets.

        Docs: https://docs.alldebrid.com/#get-status-live-mode

        Args:
            magnet_id: Magnet ID from magnet_add()
            session: Session ID (use same ID across multiple calls). If None, will query current status
            counter: Counter value from previous response (starts at 0)

        Returns:
            Dict with magnet status. May contain only changed fields if counter > 0.
            For single-magnet tracking, use magnet_status() instead.

        Raises:
            AllDebridError: If request fails
        """
        if not isinstance(magnet_id, int) or magnet_id <= 0:
            raise AllDebridError(f"Invalid magnet ID: {magnet_id}")

        try:
            # v4.1 is the up-to-date endpoint for magnet/status.
            old_base = self.base_url
            self.base_url = self.BASE_URL_V41
            try:
                payload: Dict[str,
                              Any] = {
                                  "id": str(magnet_id)
                              }
                if session is not None:
                    payload["session"] = str(int(session))
                    payload["counter"] = str(int(counter))
                response = self._request("magnet/status", payload)
            finally:
                self.base_url = old_base

            if response.get("status") == "success":
                data = response.get("data",
                                    {})
                magnets = data.get("magnets", [])

                # For specific magnet id, return the first match from the array.
                if isinstance(magnets, list) and len(magnets) > 0:
                    return magnets[0]

                # Some API variants may return a dict.
                if isinstance(magnets, dict) and magnets:
                    return magnets

                raise AllDebridError(f"No magnet found with ID {magnet_id}")

            raise AllDebridError(f"API error: {response.get('error', 'Unknown')}")
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to get magnet live status: {exc}")

    def magnet_links(self, magnet_ids: list) -> Dict[str, Any]:
        """Get files and download links for one or more magnets.

        Use this after magnet_status shows statusCode == 4 (Ready).
        Returns the file tree structure with direct download links.

        Args:
            magnet_ids: List of magnet IDs to get files for

        Returns:
            Dict mapping magnet_id (as string) -> magnet_info:
                - id: Magnet ID
                - files: Array of file/folder objects
                    File: {n: name, s: size, l: direct_download_link}
                    Folder: {n: name, e: [sub_items]}

        Raises:
            AllDebridError: If request fails
        """
        if not magnet_ids:
            raise AllDebridError("No magnet IDs provided")

        try:
            # Build parameter: id[]=123&id[]=456 style
            params = {}
            for i, magnet_id in enumerate(magnet_ids):
                params[f"id[{i}]"] = str(magnet_id)

            response = self._request("magnet/files", params)

            if response.get("status") == "success":
                data = response.get("data",
                                    {})
                magnets = data.get("magnets", [])

                # Convert list to dict keyed by ID (as string) for easier access
                result = {}
                for magnet_info in magnets:
                    magnet_id = magnet_info.get("id")
                    if magnet_id:
                        result[str(magnet_id)] = magnet_info

                return result

            raise AllDebridError(f"API error: {response.get('error', 'Unknown')}")
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to get magnet files: {exc}")

    def instant_available(self, magnet_hash: str) -> Optional[List[Dict[str, Any]]]:
        """Check if magnet is available for instant streaming without downloading.

        AllDebrid's "instant" feature checks if a magnet can be streamed directly
        without downloading all the data. Returns available video/audio files.

        Args:
            magnet_hash: Torrent hash (with or without magnet: prefix)

        Returns:
            List of available files for streaming, or None if not available
            Each file: {n: name, s: size, e: extension, t: type}
            Returns empty list if torrent not found or not available

        Raises:
            AllDebridError: If API request fails
        """
        try:
            # Parse magnet hash if needed
            if magnet_hash.startswith("magnet:"):
                # Extract hash from magnet URI
                import re

                match = re.search(r"xt=urn:btih:([a-fA-F0-9]+)", magnet_hash)
                if not match:
                    return None
                hash_value = match.group(1)
            else:
                hash_value = magnet_hash.strip()

            if not hash_value or len(hash_value) < 32:
                return None

            response = self._request("magnet/instant",
                                     {
                                         "magnet": hash_value
                                     })

            if response.get("status") == "success":
                data = response.get("data",
                                    {})
                # Returns 'files' array if available, or empty
                return data.get("files", [])

            # Not available is not an error, just return empty list
            return []

        except AllDebridError:
            raise
        except Exception as exc:
            logger.debug(f"[AllDebrid] instant_available check failed: {exc}")
            return None

    def magnet_delete(self, magnet_id: int) -> bool:
        """Delete a magnet from the AllDebrid account.

        Args:
            magnet_id: Magnet ID to delete

        Returns:
            True if deletion was successful

        Raises:
            AllDebridError: If deletion fails
        """
        if not isinstance(magnet_id, int) or magnet_id <= 0:
            raise AllDebridError(f"Invalid magnet ID: {magnet_id}")

        try:
            response = self._request("magnet/delete",
                                     {
                                         "id": str(magnet_id)
                                     })

            if response.get("status") == "success":
                return True

            raise AllDebridError(f"API error: {response.get('error', 'Unknown')}")
        except AllDebridError:
            raise
        except Exception as exc:
            raise AllDebridError(f"Failed to delete magnet: {exc}")


def _get_cached_supported_hosters(api_key: str) -> Set[str]:
    """Get cached list of supported hoster domains.

    Uses AllDebrid API to fetch the list once per hour,
    caching the result to avoid repeated API calls.

    Args:
        api_key: AllDebrid API key

    Returns:
        Set of supported domain names (lowercased)
    """
    global _SUPPORTED_HOSTERS_CACHE, _CACHE_TIMESTAMP

    now = time.time()

    # Return cached result if still valid
    if _SUPPORTED_HOSTERS_CACHE is not None and (now -
                                                 _CACHE_TIMESTAMP) < _CACHE_DURATION:
        return set(_SUPPORTED_HOSTERS_CACHE.keys())

    # Fetch fresh list from API
    try:
        client = AllDebridClient(api_key)
        hosters_dict = client.get_supported_hosters()

        if hosters_dict:
            # API returns: hosts (list), streams (list), redirectors (list)
            # Combine all into a single set
            all_domains: Set[str] = set()

            # Add hosts
            if "hosts" in hosters_dict and isinstance(hosters_dict["hosts"], list):
                all_domains.update(hosters_dict["hosts"])

            # Add streams
            if "streams" in hosters_dict and isinstance(hosters_dict["streams"], list):
                all_domains.update(hosters_dict["streams"])

            # Add redirectors
            if "redirectors" in hosters_dict and isinstance(hosters_dict["redirectors"],
                                                            list):
                all_domains.update(hosters_dict["redirectors"])

            # Cache as dict for consistency
            _SUPPORTED_HOSTERS_CACHE = {
                domain: {}
                for domain in all_domains
            }
            _CACHE_TIMESTAMP = now

            if all_domains:
                debug(f"✓ Cached {len(all_domains)} supported hosters")

            return all_domains
    except Exception as exc:
        log(f"⚠ Failed to fetch supported hosters: {exc}", file=sys.stderr)
        # Return any cached hosters even if expired
        if _SUPPORTED_HOSTERS_CACHE:
            return set(_SUPPORTED_HOSTERS_CACHE.keys())

    # Fallback: empty set if no cache available
    return set()


def is_link_restrictable_hoster(url: str, api_key: str) -> bool:
    """Check if a URL is from a hoster that AllDebrid can unlock.

    Intelligently queries the AllDebrid API to detect if the URL is
    from a supported restricted hoster.

    Args:
        url: URL to check
        api_key: AllDebrid API key

    Returns:
        True if URL is from a supported restrictable hoster
    """
    if not url or not api_key:
        return False

    try:
        # Extract domain from URL
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # Remove www. prefix for comparison
        if domain.startswith("www."):
            domain = domain[4:]

        # Get supported hosters (cached)
        supported = _get_cached_supported_hosters(api_key)

        if not supported:
            # API check failed, fall back to manual detection
            # Check for common restricted hosters
            common_hosters = {
                "uploadhaven.com",
                "uploaded.to",
                "uploaded.net",
                "datafile.com",
                "rapidfile.io",
                "nitroflare.com",
                "1fichier.com",
                "mega.nz",
                "mediafire.com",
            }
            return any(host in url.lower() for host in common_hosters)

        # Check if domain is in supported list
        # Need to check exact match and with/without www
        return domain in supported or f"www.{domain}" in supported
    except Exception as exc:
        log(f"⚠ Hoster detection failed: {exc}", file=sys.stderr)
        return False


def convert_link_with_debrid(link: str, api_key: str) -> Optional[str]:
    """Convert a restricted link to a direct download URL using AllDebrid.

    Args:
        link: Restricted link
        api_key: AllDebrid API key

    Returns:
        Direct download URL, or original link if already unrestricted
    """
    if not api_key:
        return None

    try:
        client = AllDebridClient(api_key)
        direct_link = client.unlock_link(link)

        if direct_link and direct_link != link:
            debug(f"✓ Converted link: {link[:60]}... → {direct_link[:60]}...")
            return direct_link

        return None
    except AllDebridError as exc:
        log(f"⚠ Failed to convert link: {exc}", file=sys.stderr)
        return None
    except Exception as exc:
        log(f"⚠ Unexpected error: {exc}", file=sys.stderr)
        return None


def is_magnet_link(uri: str) -> bool:
    """Check if a URI is a magnet link.

    Magnet links start with 'magnet:?xt=urn:btih:' or just 'magnet:'

    Args:
        uri: URI to check

    Returns:
        True if URI is a magnet link
    """
    if not uri:
        return False
    return uri.lower().startswith("magnet:")


def is_torrent_hash(text: str) -> bool:
    """Check if text looks like a torrent hash (40 or 64 hex characters).

    Common formats:
        - Info hash v1: 40 hex chars (SHA-1)
        - Info hash v2: 64 hex chars (SHA-256)

    Args:
        text: Text to check

    Returns:
        True if text matches torrent hash format
    """
    if not text or not isinstance(text, str):
        return False

    text = text.strip()

    # Check if it's 40 hex chars (SHA-1) or 64 hex chars (SHA-256)
    if len(text) not in (40, 64):
        return False

    try:
        # Try to parse as hex
        int(text, 16)
        return True
    except ValueError:
        return False


def is_torrent_file(path: str) -> bool:
    """Check if a file path is a .torrent file.

    Args:
        path: File path to check

    Returns:
        True if file has .torrent extension
    """
    if not path:
        return False
    return path.lower().endswith(".torrent")


def parse_magnet_or_hash(uri: str) -> Optional[str]:
    """Parse a magnet URI or hash into a format for AllDebrid API.

    AllDebrid's magnet/upload endpoint accepts:
        - Full magnet URIs: magnet:?xt=urn:btih:...
        - Info hashes: 40 or 64 hex characters

    Args:
        uri: Magnet URI or hash

    Returns:
        Normalized input for AllDebrid API, or None if invalid
    """
    if not uri:
        return None

    uri = uri.strip()

    # Already a magnet link - just return it
    if is_magnet_link(uri):
        return uri

    # Check if it's a valid hash
    if is_torrent_hash(uri):
        return uri

    # Not a recognized format
    return None


# ============================================================================
# Cmdlet: unlock_link
# ============================================================================


def unlock_link_cmdlet(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    """Unlock a restricted link using AllDebrid.

    Converts free hosters and restricted links to direct download url.

    Usage:
        unlock-link <link>
        unlock-link                    # Uses URL from pipeline result

    Requires:
        - AllDebrid API key in config under plugin.alldebrid.api_key

    Args:
        result: Pipeline result object
        args: Command arguments
        config: Configuration dictionary

    Returns:
        0 on success, 1 on failure
    """

    def _extract_link_from_args_or_result(result_obj: Any,
                                          argv: Sequence[str]) -> Optional[str]:
        # Prefer an explicit URL in args.
        for a in argv or []:
            if isinstance(a, str) and a.startswith(("http://", "https://")):
                return a.strip()

        # Fall back to common pipeline fields.
        if isinstance(result_obj, dict):
            for key in ("url", "source_url", "path", "target"):
                v = result_obj.get(key)
                if isinstance(v, str) and v.startswith(("http://", "https://")):
                    return v.strip()
        return None

    def _get_alldebrid_api_key_from_config(cfg: Dict[str, Any]) -> Optional[str]:
        try:
            from SYS.config import get_debrid_api_key

            api_key = get_debrid_api_key(cfg, service="All-debrid")
            if isinstance(api_key, str) and api_key.strip():
                return api_key.strip()
        except Exception:
            return None

        return None

    def _add_direct_link_to_result(
        result_obj: Any,
        direct_link: str,
        original_link: str
    ) -> None:
        if not isinstance(direct_link, str) or not direct_link.strip():
            return
        if isinstance(result_obj, dict):
            # Keep original and promote unlocked link to the fields commonly used downstream.
            result_obj.setdefault("source_url", original_link)
            result_obj["url"] = direct_link
            result_obj["path"] = direct_link

    # Get link from args or result
    link = _extract_link_from_args_or_result(result, args)

    if not link:
        log("No valid URL provided", file=sys.stderr)
        return 1

    # Get AllDebrid API key from config
    api_key = _get_alldebrid_api_key_from_config(config)

    if not api_key:
        log(
            "AllDebrid API key not configured. Use .config to set it.",
            file=sys.stderr
        )
        return 1

    # Try to unlock the link
    debug(f"Unlocking: {link}")
    direct_link = convert_link_with_debrid(link, api_key)

    if direct_link:
        debug(f"✓ Direct link: {direct_link}")

        # Update result with direct link
        _add_direct_link_to_result(result, direct_link, link)

        # Return the updated result via pipeline context
        # Note: The cmdlet wrapper will handle emitting to pipeline
        return 0
    else:
        log("❌ Failed to unlock link or already unrestricted", file=sys.stderr)
        return 1


# ============================================================================
# Cmdlet Registration
# ============================================================================


def _register_unlock_link():
    """Register unlock-link command with cmdlet registry if available."""
    try:
        from cmdlet import register

        @register(["unlock-link"])
        def unlock_link_wrapper(
            result: Any,
            args: Sequence[str],
            config: Dict[str,
                         Any]
        ) -> int:
            """Wrapper to make unlock_link_cmdlet available as cmdlet."""
            from SYS import pipeline as ctx

            ret_code = unlock_link_cmdlet(result, args, config)

            # If successful, emit the result
            if ret_code == 0:
                ctx.emit(result)

            return ret_code

        return unlock_link_wrapper
    except ImportError:
        # If cmdlet module not available, just return None
        return None


# Register when module is imported
_unlock_link_registration = _register_unlock_link()
