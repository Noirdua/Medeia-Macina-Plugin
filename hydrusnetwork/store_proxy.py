from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PluginCore.backend_base import BackendBase

from plugins.hydrusnetwork.store_backend import HydrusStoreOperations


class HydrusStoreProxy(BackendBase):
    """Thin configured-backend adapter for the Hydrus plugin.

    Generic backend infrastructure still expects a BackendBase-compatible object
    for configured instances. This proxy keeps that surface small while the real Hydrus behavior
    remains internal to the plugin package.
    """

    STORE_TYPE = "hydrusnetwork"

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return HydrusStoreOperations.config_schema()

    @property
    def is_remote(self) -> bool:
        return True

    @property
    def prefer_defer_tags(self) -> bool:
        return True

    @property
    def supports_url_association(self) -> bool:
        return True

    @property
    def supports_note_association(self) -> bool:
        return True

    @property
    def supports_relationship_association(self) -> bool:
        return True

    def __init__(
        self,
        instance_name: Optional[str] = None,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
        *,
        NAME: Optional[str] = None,
        API: Optional[str] = None,
        URL: Optional[str] = None,
    ) -> None:
        if instance_name is None and NAME is not None:
            instance_name = str(NAME)
        if api_key is None and API is not None:
            api_key = str(API)
        if url is None and URL is not None:
            url = str(URL)

        if not instance_name or not api_key or not url:
            raise ValueError("HydrusStoreProxy requires NAME, API, and URL")

        self.NAME = str(instance_name)
        self.API = str(api_key)
        self.URL = str(url).rstrip("/")
        self._operations_instance: Optional[HydrusStoreOperations] = None

    def _operations(self) -> HydrusStoreOperations:
        operations = self.__dict__.get("_operations_instance")
        if operations is None:
            client = self.__dict__.get("_client")
            if client is not None:
                operations = object.__new__(HydrusStoreOperations)
                operations.NAME = self.NAME
                operations.API = self.API
                operations.URL = self.URL
                operations._client = client
                operations._service_key_cache = dict(self.__dict__.get("_service_key_cache", {}) or {})
                operations.total_count = self.__dict__.get("total_count")
            else:
                operations = HydrusStoreOperations(
                    NAME=self.NAME,
                    API=self.API,
                    URL=self.URL,
                )
            self._operations_instance = operations
        return operations

    def __getattr__(self, name: str) -> Any:
        if name == "_operations_instance":
            raise AttributeError(name)
        try:
            return getattr(self._operations(), name)
        except RuntimeError as exc:
            if "unavailable" in str(exc).lower():
                raise AttributeError(name) from exc
            raise

    def name(self) -> str:
        return self.NAME

    def get_name(self) -> str:
        return self.NAME

    def add_file(self, file_path: Path, **kwargs: Any) -> str:
        return self._operations().add_file(file_path, **kwargs)

    def search(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]:
        return self._operations().search(query, **kwargs)

    def find_hashes_by_url(self, url_value: str) -> List[str]:
        return self._operations().find_hashes_by_url(url_value)

    def fetch_files_metadata(self, file_hashes: Sequence[str], **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self._operations().fetch_files_metadata(file_hashes, **kwargs)

    def get_file(self, file_hash: str, **kwargs: Any) -> Path | str | None:
        return self._operations().get_file(file_hash, **kwargs)

    def file_url(self, file_hash: str, **kwargs: Any) -> Optional[str]:
        url = self._operations().build_file_url(file_hash)
        return str(url) if url else None

    def get_metadata(self, file_hash: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self._operations().get_metadata(file_hash, **kwargs)

    def get_tag(self, file_identifier: str, **kwargs: Any) -> Tuple[List[str], str]:
        return self._operations().get_tag(file_identifier, **kwargs)

    def add_tag(self, file_identifier: str, tags: List[str], **kwargs: Any) -> bool:
        return self._operations().add_tag(file_identifier, tags, **kwargs)

    def delete_tag(self, file_identifier: str, tags: List[str], **kwargs: Any) -> bool:
        return self._operations().delete_tag(file_identifier, tags, **kwargs)

    def add_tags_bulk(self, items: List[Any], **kwargs: Any) -> bool:
        return self._operations().add_tags_bulk(items, **kwargs)

    def delete_tags_bulk(self, items: List[Any], **kwargs: Any) -> bool:
        return self._operations().delete_tags_bulk(items, **kwargs)

    def delete_files_bulk(self, file_identifiers: Sequence[str], **kwargs: Any) -> bool:
        return self._operations().delete_files_bulk(file_identifiers, **kwargs)

    def get_url(self, file_identifier: str, **kwargs: Any) -> List[str]:
        return self._operations().get_url(file_identifier, **kwargs)

    def add_url(self, file_identifier: str, url: List[str], **kwargs: Any) -> bool:
        return self._operations().add_url(file_identifier, url, **kwargs)

    def delete_url(self, file_identifier: str, url: List[str], **kwargs: Any) -> bool:
        return self._operations().delete_url(file_identifier, url, **kwargs)

    def get_note(self, file_identifier: str, **kwargs: Any) -> Dict[str, str]:
        return self._operations().get_note(file_identifier, **kwargs)

    def set_note(self, file_identifier: str, name: str, text: str, **kwargs: Any) -> bool:
        return self._operations().set_note(file_identifier, name, text, **kwargs)

    def delete_note(self, file_identifier: str, name: str, **kwargs: Any) -> bool:
        return self._operations().delete_note(file_identifier, name, **kwargs)