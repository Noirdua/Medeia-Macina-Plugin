"""Library of Congress (LoC) API helpers.

This module currently focuses on the LoC JSON API endpoint for the
Chronicling America collection.

Docs:
- https://www.loc.gov/apis/
- https://www.loc.gov/apis/json-and-yaml/

The LoC JSON API does not require an API key.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from API.base import API, ApiError


class LOCError(ApiError):
    pass


class LOCClient(API):
    """Minimal client for the public LoC JSON API."""

    def __init__(self, *, base_url: str = "https://www.loc.gov", timeout: float = 20.0):
        super().__init__(base_url=base_url, timeout=timeout)

    def search_chronicling_america(
        self,
        query: str,
        *,
        start: int = 1,
        count: int = 25,
        extra_params: Optional[Dict[str,
                                    Any]] = None,
    ) -> Dict[str,
              Any]:
        """Search the Chronicling America collection via LoC JSON API.

        Args:
            query: Free-text query.
            start: 1-based start index (LoC uses `sp`).
            count: Results per page (LoC uses `c`).
            extra_params: Additional LoC API params (facets, filters, etc.).

        Returns:
            Parsed JSON response.
        """

        q = str(query or "").strip()
        if not q:
            return {
                "results": []
            }

        params: Dict[str,
                     Any] = {
                         "q": q,
                         "fo": "json",
                         "c": int(count) if int(count) > 0 else 25,
                         "sp": int(start) if int(start) > 0 else 1,
                     }
        if extra_params:
            for k, v in extra_params.items():
                if v is None:
                    continue
                params[str(k)] = v

        return self._get_json("/collections/chronicling-america/", params)
