from __future__ import annotations

import mimetypes
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from API.requests_client import get_requests_session
from SYS.utils import ffprobe as probe_media_metadata

from PluginCore.base import Plugin, SearchResult
from SYS.plugin_helpers import TablePluginMixin
from SYS.logger import log

_MATRIX_INIT_CHECK_CACHE: Dict[str,
                               Tuple[bool,
                                     Optional[str]]] = {}


def _sniff_mime_from_header(path: Path) -> Optional[str]:
    """Best-effort MIME sniffing from file headers.

    Used when the file has no/unknown extension (common for exported/temp files).
    Keeps dependencies to stdlib only.
    """
    try:
        if not path.exists() or not path.is_file():
            return None
        with open(path, "rb") as handle:
            header = handle.read(512)
        if not header:
            return None

        # Images
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
            return "image/gif"
        if header.startswith(b"BM"):
            return "image/bmp"
        if header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WEBP":
            return "image/webp"

        # Audio
        if header.startswith(b"fLaC"):
            return "audio/flac"
        if header.startswith(b"OggS"):
            # Could be audio or video; treat as audio unless extension suggests video.
            return "audio/ogg"
        if header.startswith(b"ID3"):
            return "audio/mpeg"
        if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
            return "audio/mpeg"
        if header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WAVE":
            return "audio/wav"

        # Video
        if header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"AVI ":
            return "video/x-msvideo"
        if header.startswith(b"\x1a\x45\xdf\xa3"):
            # EBML container: Matroska/WebM.
            return "video/x-matroska"
        if len(header) >= 12 and header[4:8] == b"ftyp":
            # ISO BMFF: mp4/mov/m4a. Default to mp4; extension can refine.
            return "video/mp4"
        # MPEG-TS / M2TS (sync byte every 188 bytes)
        try:
            if path.stat().st_size >= 188 * 2 and header[0] == 0x47:
                with open(path, "rb") as handle:
                    handle.seek(188)
                    b = handle.read(1)
                    if b == b"\x47":
                        return "video/mp2t"
        except Exception:
            pass

        return None
    except Exception:
        return None


def _classify_matrix_upload(path: Path,
                            *,
                            explicit_mime_type: Optional[str] = None) -> Tuple[str,
                                                                               str]:
    """Return (mime_type, msgtype) for Matrix uploads."""
    mime_type = str(explicit_mime_type or "").strip() or None

    if not mime_type:
        # `mimetypes.guess_type` expects a string/URL; Path can return None on some platforms.
        mime_type, _ = mimetypes.guess_type(str(path))

    if not mime_type:
        mime_type = _sniff_mime_from_header(path)

    # Refinements based on extension for ambiguous containers.
    ext = path.suffix.lower()
    if ext in {".m4a",
               ".aac"}:
        mime_type = mime_type or "audio/mp4"
    if ext in {".mkv",
               ".webm"}:
        mime_type = mime_type or "video/x-matroska"
    if ext in {".ogv"}:
        mime_type = mime_type or "video/ogg"

    msgtype = "m.file"
    if mime_type:
        mt = mime_type.casefold()
        if mt.startswith("image/"):
            msgtype = "m.image"
        elif mt.startswith("audio/"):
            msgtype = "m.audio"
        elif mt.startswith("video/"):
            msgtype = "m.video"

    # Final fallback for unknown MIME types.
    if msgtype == "m.file":
        audio_exts = {
            ".mp3",
            ".flac",
            ".wav",
            ".m4a",
            ".aac",
            ".ogg",
            ".opus",
            ".wma",
            ".mka",
            ".alac",
        }
        video_exts = {
            ".mp4",
            ".mkv",
            ".webm",
            ".mov",
            ".avi",
            ".flv",
            ".mpg",
            ".mpeg",
            ".ts",
            ".m4v",
            ".wmv",
            ".m2ts",
            ".mts",
            ".3gp",
            ".ogv",
        }
        image_exts = {".jpg",
                      ".jpeg",
                      ".png",
                      ".gif",
                      ".webp",
                      ".bmp",
                      ".tiff"}
        if ext in audio_exts:
            msgtype = "m.audio"
        elif ext in video_exts:
            msgtype = "m.video"
        elif ext in image_exts:
            msgtype = "m.image"

    return (mime_type or "application/octet-stream"), msgtype


def _build_matrix_media_info(path: Path, *, mime_type: str, msgtype: str) -> Dict[str, Any]:
    """Build Matrix `info` payload with dimensions/duration when available.

    Matrix clients use `info.w`/`info.h` to size image/video placeholders. Supplying
    these fields keeps the preview aspect ratio aligned with the actual media.
    """
    info: Dict[str, Any] = {
        "mimetype": str(mime_type or "application/octet-stream"),
        "size": int(path.stat().st_size),
    }

    metadata: Dict[str, Any] = {}
    try:
        metadata = probe_media_metadata(str(path)) or {}
    except Exception:
        metadata = {}

    width: Optional[int] = None
    height: Optional[int] = None

    try:
        raw_w = metadata.get("width") if isinstance(metadata, dict) else None
        if isinstance(raw_w, (int, float)) and raw_w > 0:
            width = int(raw_w)
    except Exception:
        width = None

    try:
        raw_h = metadata.get("height") if isinstance(metadata, dict) else None
        if isinstance(raw_h, (int, float)) and raw_h > 0:
            height = int(raw_h)
    except Exception:
        height = None

    # Fallback for images when ffprobe metadata is unavailable.
    if msgtype == "m.image" and (width is None or height is None):
        try:
            from PIL import Image  # type: ignore

            with Image.open(path) as img:
                iw, ih = img.size
            if isinstance(iw, int) and iw > 0:
                width = iw
            if isinstance(ih, int) and ih > 0:
                height = ih
        except Exception:
            pass

    if msgtype in {"m.image", "m.video"}:
        if width is not None:
            info["w"] = width
        if height is not None:
            info["h"] = height

    if msgtype in {"m.video", "m.audio"}:
        try:
            raw_duration = metadata.get("duration") if isinstance(metadata, dict) else None
            if isinstance(raw_duration, (int, float)) and raw_duration > 0:
                info["duration"] = int(round(float(raw_duration) * 1000.0))
        except Exception:
            pass

    return info


def _normalize_homeserver(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith("http"):
        text = f"https://{text}"
    return text.rstrip("/")


def _matrix_health_check(*,
                         homeserver: str,
                         access_token: Optional[str]) -> Tuple[bool,
                                                               Optional[str]]:
    """Lightweight Matrix reachability/auth validation.

    - Always checks `/versions` (no auth).
    - If `access_token` is present, also checks `/whoami`.
    """
    try:
        base = _normalize_homeserver(homeserver)
        if not base:
            return False, "Matrix homeserver missing"

        resp = get_requests_session().get(f"{base}/_matrix/client/versions", timeout=5)
        if resp.status_code != 200:
            return False, f"Homeserver returned {resp.status_code}"

        if access_token:
            headers = {
                "Authorization": f"Bearer {access_token}"
            }
            resp = get_requests_session().get(
                f"{base}/_matrix/client/v3/account/whoami",
                headers=headers,
                timeout=5
            )
            if resp.status_code != 200:
                return False, f"Authentication failed: {resp.status_code}"

        return True, None
    except Exception as exc:
        return False, str(exc)


class Matrix(TablePluginMixin, Plugin):
    """Matrix (Element) room provider with file uploads and selection.
    
    This provider uses the new table system (strict ResultTable adapter pattern) for
    consistent room listing and selection. It exposes Matrix joined rooms as selectable
    rows with metadata enrichment for:
    - room_id: Unique Matrix room identifier
    - room_name: Human-readable room display name
    - _selection_args: For @N expansion control and upload routing
    
    KEY FEATURES:
    - Table system: Using ResultTable adapter for strict column/metadata handling
    - Room discovery: search() or list_rooms() to enumerate joined rooms
    - Selection integration: @N selection triggers upload_to_room() via selector()
    - Deferred uploads: Files can be queued for upload to multiple rooms
    - MIME detection: Automatic content type classification for Matrix msgtype
    
    SELECTION FLOW:
    1. User runs: search-file -plugin matrix "room" (or .matrix -list-rooms)
    2. Results show available joined rooms
    3. User selects rooms: @1 @2 (or @1,2)
    4. Selection triggers upload of pending files to selected rooms
    """

    EXPOSE_AS_FILE_PROVIDER = False
    MULTI_INSTANCE = True
    SUPPORTED_CMDLETS = frozenset({"add-file", "search-file"})

    @classmethod
    def config_schema(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "homeserver",
                "label": "Homeserver URL",
                "default": "https://matrix.org",
                "required": True
            },
            {
                "key": "access_token",
                "label": "Access Token",
                "default": "",
                "secret": True
            },
        ]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._init_ok: Optional[bool] = None
        self._init_reason: Optional[str] = None

    def _active_instance_config(self, instance_name: Optional[str] = None) -> Dict[str, Any]:
        _resolved, cfg = self.resolve_plugin_instance(instance_name)
        return cfg if isinstance(cfg, dict) else {}

    @staticmethod
    def _instance_has_credentials(cfg: Dict[str, Any]) -> bool:
        homeserver = str((cfg or {}).get("homeserver") or "").strip()
        access_token = str((cfg or {}).get("access_token") or "").strip()
        return bool(homeserver and access_token)

    def validate(self) -> bool:
        if not self.config:
            return False
        for cfg in self.plugin_instance_configs().values():
            if self._instance_has_credentials(cfg):
                return True
        return False

    def status_summary(self) -> Dict[str, Any]:
        instances = self.plugin_instance_configs()
        ready: List[str] = []
        homeserver = ""
        for name, cfg in instances.items():
            if not self._instance_has_credentials(cfg):
                continue
            label = str(name or "").strip() or "default"
            ready.append(label)
            if not homeserver:
                homeserver = str(cfg.get("homeserver") or "").strip()
        enabled = bool(ready)
        detail = homeserver
        if ready:
            detail = (detail + (" " if detail else "")) + ",".join(ready)
        return {
            "status": "ENABLED" if enabled else "DISABLED",
            "name": self.label,
            "plugin": self.name,
            "instance": ", ".join(ready),
            "detail": detail or ("Connected" if enabled else "Not configured"),
        }

    @classmethod
    def config_header_lines(cls, *, instance_name: Optional[str] = None) -> List[str]:
        if instance_name:
            return [
                "Homeserver is the https:// URL (example: https://matrix.example).",
                "Access token: Element → Settings → Help & About → Access Token.",
                "After both are set this instance enables. List rooms with .matrix, or file -add -plugin matrix -query \"instance:%s\"."
                % (instance_name,),
            ]
        return [
            "Each Matrix instance is one homeserver + access token (and optional default rooms).",
            "Open an instance with @N. Add one with @N | .config <name> on Add Instance.",
        ]

    def search(
        self,
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[SearchResult]:
        """Search/list joined Matrix rooms.
        
        If query is empty or "*", returns all joined rooms.
        Otherwise, filters rooms by name/ID matching the query.
        """
        try:
            rooms = self.list_rooms()
        except Exception as exc:
            log(f"[matrix] Failed to list rooms: {exc}", file=sys.stderr)
            return []
        
        q = (query or "").strip().lower()
        needle = "" if q in {"*", "all", "list"} else q
        
        results: List[SearchResult] = []
        for room in rooms:
            if len(results) >= limit:
                break
            
            room_id = room.get("room_id") or ""
            room_name = room.get("name") or ""
            
            # Filter by query if provided
            if needle:
                match_text = f"{room_name} {room_id}".lower()
                if needle not in match_text:
                    continue
            
            if not room_id:
                continue
            
            display_name = room_name or room_id
            results.append(
                SearchResult(
                    table="matrix",
                    title=display_name,
                    path=f"matrix:room:{room_id}",
                    detail=room_id if room_name else "",
                    annotations=["room"],
                    media_kind="folder",
                    columns=[
                        ("Room", display_name),
                        ("ID", room_id),
                    ],
                    full_metadata={
                        "room_id": room_id,
                        "room_name": room_name,
                        "plugin": "matrix",
                        # Selection metadata for table system and @N expansion
                        "_selection_args": ["-room-id", room_id],
                    },
                )
            )
        
        return results


    def _get_homeserver_and_token(self, instance_name: Optional[str] = None) -> Tuple[str, str]:
        matrix_conf = self._active_instance_config(instance_name)
        homeserver = matrix_conf.get("homeserver")
        access_token = matrix_conf.get("access_token")
        if not homeserver:
            raise Exception("Matrix homeserver missing")
        if not access_token:
            raise Exception("Matrix access_token missing")
        base = _normalize_homeserver(str(homeserver))
        if not base:
            raise Exception("Matrix homeserver missing")
        return base, str(access_token)

    def list_joined_room_ids(self) -> List[str]:
        """Return joined room IDs for the current user.

        Uses `GET /_matrix/client/v3/joined_rooms`.
        """
        base, token = self._get_homeserver_and_token()
        headers = {
            "Authorization": f"Bearer {token}"
        }
        resp = get_requests_session().get(
            f"{base}/_matrix/client/v3/joined_rooms",
            headers=headers,
            timeout=10
        )
        if resp.status_code != 200:
            raise Exception(f"Matrix joined_rooms failed: {resp.text}")
        data = resp.json() or {}
        rooms = data.get("joined_rooms") or []
        out: List[str] = []
        for rid in rooms:
            if not isinstance(rid, str) or not rid.strip():
                continue
            out.append(rid.strip())
        return out

    def list_rooms(self,
                   *,
                   room_ids: Optional[List[str]] = None) -> List[Dict[str,
                                                                      Any]]:
        """Return joined rooms, optionally limited to a subset.

        Performance note: room names require additional per-room HTTP requests.
        If `room_ids` is provided, only those rooms will have name lookups.
        """
        base, token = self._get_homeserver_and_token()
        headers = {
            "Authorization": f"Bearer {token}"
        }

        joined = self.list_joined_room_ids()
        if room_ids:
            allowed = {str(v).strip().casefold()
                       for v in room_ids if str(v).strip()}
            if allowed:
                # Accept either full IDs (!id:hs) or short IDs (!id).
                def _is_allowed(rid: str) -> bool:
                    r = str(rid or "").strip()
                    if not r:
                        return False
                    rc = r.casefold()
                    if rc in allowed:
                        return True
                    short = r.split(":", 1)[0].strip().casefold()
                    return bool(short) and short in allowed

                joined = [rid for rid in joined if _is_allowed(rid)]

        out: List[Dict[str, Any]] = []
        for room_id in joined:
            name = ""
            # Best-effort room name lookup (safe to fail).
            try:
                encoded = quote(room_id, safe="")
                name_resp = get_requests_session().get(
                    f"{base}/_matrix/client/v3/rooms/{encoded}/state/m.room.name",
                    headers=headers,
                    timeout=5,
                )
                if name_resp.status_code == 200:
                    payload = name_resp.json() or {}
                    maybe = payload.get("name")
                    if isinstance(maybe, str):
                        name = maybe
            except Exception:
                pass
            out.append({
                "room_id": room_id,
                "name": name
            })
        return out

    def upload_to_room(self, file_path: str, room_id: str, **kwargs: Any) -> str:
        """Upload a file and send it to a specific room."""
        from SYS.models import ProgressFileReader

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if not room_id:
            raise Exception("Matrix room_id missing")

        base, token = self._get_homeserver_and_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        }

        mime_type, msgtype = _classify_matrix_upload(
            path, explicit_mime_type=kwargs.get("mime_type")
        )
        headers["Content-Type"] = mime_type

        filename = path.name

        # Upload media
        upload_url = f"{base}/_matrix/media/v3/upload"
        with open(path, "rb") as handle:
            wrapped = ProgressFileReader(
                handle,
                total_bytes=int(path.stat().st_size),
                label="upload"
            )
            resp = get_requests_session().post(
                upload_url,
                headers=headers,
                data=wrapped,
                params={
                    "filename": filename
                }
            )
        if resp.status_code != 200:
            raise Exception(f"Matrix upload failed: {resp.text}")
        content_uri = (resp.json() or {}).get("content_uri")
        if not content_uri:
            raise Exception("No content_uri returned")

        # Build a fragment-free URL suitable for storage backends.
        # `matrix.to` links use fragments (`#/...`) which some backends normalize away.
        download_url_for_store = ""
        try:
            curi = str(content_uri or "").strip()
            if curi.startswith("mxc://"):
                rest = curi[len("mxc://"):]
                if "/" in rest:
                    server_name, media_id = rest.split("/", 1)
                    server_name = str(server_name).strip()
                    media_id = str(media_id).strip()
                    if server_name and media_id:
                        download_url_for_store = f"{base}/_matrix/media/v3/download/{quote(server_name, safe='')}/{quote(media_id, safe='')}"
        except Exception:
            download_url_for_store = ""

        info = _build_matrix_media_info(path, mime_type=mime_type, msgtype=msgtype)
        payload = {
            "msgtype": msgtype,
            "body": filename,
            "url": content_uri,
            "info": info
        }

        # Correct Matrix client API send endpoint requires a transaction ID.
        txn_id = f"mm_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        encoded_room = quote(str(room_id), safe="")
        send_url = f"{base}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}"
        send_headers = {
            "Authorization": f"Bearer {token}"
        }
        send_resp = get_requests_session().put(send_url, headers=send_headers, json=payload)
        if send_resp.status_code != 200:
            raise Exception(f"Matrix send message failed: {send_resp.text}")

        event_id = (send_resp.json() or {}).get("event_id")
        link = (
            f"https://matrix.to/#/{room_id}/{event_id}"
            if event_id else f"https://matrix.to/#/{room_id}"
        )

        # Optional: if a PipeObject is provided and it already has store+hash,
        # attach the uploaded URL back to the stored file.
        try:
            pipe_obj = kwargs.get("pipe_obj")
            if pipe_obj is not None:
                from PluginCore.backend_registry import BackendRegistry

                # Prefer the direct media download URL for storage backends.
                BackendRegistry(
                    self.config,
                    suppress_debug=True
                ).try_add_url_for_pipe_object(
                    pipe_obj,
                    download_url_for_store or link,
                )
        except Exception:
            pass

        return link

    def send_text_to_room(self, text: str, room_id: str) -> str:
        """Send a plain text message to a specific room."""
        message = str(text or "").strip()
        if not message:
            return ""
        if not room_id:
            raise Exception("Matrix room_id missing")

        base, token = self._get_homeserver_and_token()
        encoded_room = quote(str(room_id), safe="")
        txn_id = f"mm_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        send_url = f"{base}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}"
        send_headers = {
            "Authorization": f"Bearer {token}"
        }
        payload = {
            "msgtype": "m.text",
            "body": message
        }
        send_resp = get_requests_session().put(send_url, headers=send_headers, json=payload)
        if send_resp.status_code != 200:
            raise Exception(f"Matrix send text failed: {send_resp.text}")

        event_id = (send_resp.json() or {}).get("event_id")
        return (
            f"https://matrix.to/#/{room_id}/{event_id}"
            if event_id else f"https://matrix.to/#/{room_id}"
        )

    def upload(self, file_path: str, **kwargs: Any) -> str:
        matrix_conf = self.config.get("plugin",
                                      {}).get("matrix",
                                              {})
        room_id = matrix_conf.get("room_id")
        if not room_id:
            raise Exception("Matrix room_id missing")
        return self.upload_to_room(file_path, str(room_id))

    def selector(
        self,
        selected_items: List[Any],
        *,
        ctx: Any,
        stage_is_last: bool = True,
        **_kwargs: Any
    ) -> bool:
        """Handle Matrix room selection via `@N`.

        If the CLI has a pending upload stash, selecting a room triggers an upload.
        """
        if not stage_is_last:
            return False

        pending = None
        try:
            pending = ctx.load_value("matrix_pending_uploads", default=None)
        except Exception:
            pending = None

        pending_list = list(pending) if isinstance(pending, list) else []
        if not pending_list:
            return False

        room_ids: List[str] = []
        for item in selected_items or []:
            rid = None
            if isinstance(item, dict):
                rid = item.get("room_id") or item.get("id")
            else:
                rid = getattr(item, "room_id", None) or getattr(item, "id", None)
            if rid and str(rid).strip():
                room_ids.append(str(rid).strip())

        if not room_ids:
            print("No Matrix room selected\n")
            return True

        any_failed = False
        for room_id in room_ids:
            for payload in pending_list:
                try:
                    file_path = ""
                    delete_after = False
                    pipe_obj = None
                    if isinstance(payload, dict):
                        file_path = str(payload.get("path") or "")
                        delete_after = bool(payload.get("delete_after", False))
                        pipe_obj = payload.get("pipe_obj")
                    else:
                        file_path = str(getattr(payload, "path", "") or "")
                    if not file_path:
                        any_failed = True
                        continue

                    media_path = Path(file_path)
                    if not media_path.exists():
                        any_failed = True
                        print(f"Matrix upload file missing: {file_path}")
                        continue

                    link = self.upload_to_room(
                        str(media_path),
                        str(room_id),
                        pipe_obj=pipe_obj
                    )
                    if link:
                        print(link)

                    if delete_after:
                        try:
                            media_path.unlink(missing_ok=True)  # type: ignore[arg-type]
                        except TypeError:
                            try:
                                if media_path.exists():
                                    media_path.unlink()
                            except Exception:
                                pass
                except Exception as exc:
                    any_failed = True
                    print(f"Matrix upload failed: {exc}")

        try:
            ctx.store_value("matrix_pending_uploads", [])
        except Exception:
            pass

        if any_failed:
            print("\nOne or more Matrix uploads failed\n")
        return True
