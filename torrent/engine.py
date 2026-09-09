from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from SYS.utils import sanitize_filename, unique_path


def _lt():
    import libtorrent as lt

    return lt


@dataclass
class TorrentJob:
    job_id: str
    title: str
    magnet: str
    save_path: Path
    handle: Any = None
    paused: bool = False
    status: str = "queued"
    progress: float = 0.0
    download_rate: int = 0
    upload_rate: int = 0
    peers: int = 0
    total_wanted: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)

    def snapshot(self) -> Dict[str, Any]:
        pct = max(0.0, min(100.0, float(self.progress) * 100.0))
        return {
            "id": self.job_id,
            "title": self.title,
            "status": "paused" if self.paused and self.status != "done" else self.status,
            "progress": f"{pct:.1f}%",
            "down": _fmt_rate(self.download_rate),
            "up": _fmt_rate(self.upload_rate),
            "peers": str(self.peers),
            "size": _fmt_size(self.total_wanted),
            "path": str(self.save_path),
            "magnet": self.magnet,
            "error": self.error,
            "plugin": "torrent",
            "table": "torrent.jobs",
            "_selection_action": [".torrent"],
            "_selection_args": ["-id", self.job_id],
        }


def _fmt_rate(bps: int) -> str:
    try:
        n = float(bps or 0)
    except Exception:
        return "0 B/s"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB/s"
    if n >= 1024:
        return f"{n / 1024:.1f} KB/s"
    return f"{n:.0f} B/s"


def _fmt_size(n: int) -> str:
    try:
        size = float(n or 0)
    except Exception:
        return ""
    if size >= 1024 ** 3:
        return f"{size / (1024 ** 3):.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / (1024 ** 2):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size:.0f} B"


class TorrentEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, TorrentJob] = {}
        self._order: List[str] = []
        self._ses: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._seq = 0

    def _session(self) -> Any:
        if self._ses is not None:
            return self._ses
        lt = _lt()
        ses = lt.session()
        try:
            settings = {
                "listen_interfaces": "0.0.0.0:6881,[::]:6881",
                "enable_dht": True,
                "enable_lsd": True,
                "enable_upnp": True,
                "enable_natpmp": True,
            }
            if hasattr(ses, "apply_settings"):
                ses.apply_settings(settings)
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
        self._ses = ses
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="torrent-engine", daemon=True)
        self._thread.start()
        return ses

    def add(self, magnet: str, output_dir: Path, title: str) -> TorrentJob:
        lt = _lt()
        ses = self._session()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        save_dir = unique_path(output_dir / sanitize_filename(title or "torrent"))
        save_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(lt, "parse_magnet_uri") and hasattr(ses, "add_torrent"):
            params = lt.parse_magnet_uri(magnet)
            params.save_path = str(save_dir)
            handle = ses.add_torrent(params)
        else:
            handle = lt.add_magnet_uri(ses, magnet, {"save_path": str(save_dir)})
        with self._lock:
            self._seq += 1
            job_id = str(self._seq)
            job = TorrentJob(
                job_id=job_id,
                title=title or "torrent",
                magnet=magnet,
                save_path=save_dir,
                handle=handle,
                status="metadata",
            )
            self._jobs[job_id] = job
            self._order.append(job_id)
            return job

    def jobs(self) -> List[TorrentJob]:
        with self._lock:
            return [self._jobs[i] for i in self._order if i in self._jobs]

    def get(self, job_id: str) -> Optional[TorrentJob]:
        with self._lock:
            return self._jobs.get(str(job_id or "").strip())

    def pause(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.handle is None:
            return False
        try:
            job.handle.pause()
            job.paused = True
            job.status = "paused"
            return True
        except Exception:
            return False

    def resume(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.handle is None:
            return False
        try:
            job.handle.resume()
            job.paused = False
            job.status = "downloading"
            return True
        except Exception:
            return False

    def remove(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        ses = self._ses
        try:
            if ses is not None and job.handle is not None:
                ses.remove_torrent(job.handle)
        except Exception:
            pass
        with self._lock:
            self._jobs.pop(str(job_id), None)
            self._order = [i for i in self._order if i != str(job_id)]
        return True

    def _loop(self) -> None:
        while not self._stop.wait(0.8):
            with self._lock:
                jobs = list(self._jobs.values())
            for job in jobs:
                handle = job.handle
                if handle is None:
                    continue
                try:
                    st = handle.status()
                except Exception as exc:
                    job.error = str(exc)
                    job.status = "error"
                    continue
                job.progress = float(getattr(st, "progress", 0) or 0)
                job.download_rate = int(getattr(st, "download_rate", 0) or 0)
                job.upload_rate = int(getattr(st, "upload_rate", 0) or 0)
                job.peers = int(getattr(st, "num_peers", 0) or 0)
                job.total_wanted = int(getattr(st, "total_wanted", 0) or 0)
                if job.paused:
                    job.status = "paused"
                    continue
                has_meta = bool(getattr(st, "has_metadata", False))
                try:
                    if hasattr(handle, "has_metadata"):
                        has_meta = bool(handle.has_metadata())
                except Exception:
                    pass
                if bool(getattr(st, "is_seeding", False)) or job.progress >= 0.999:
                    job.status = "done"
                    job.progress = 1.0
                elif not has_meta:
                    job.status = "metadata"
                else:
                    job.status = "downloading"


_ENGINE: Optional[TorrentEngine] = None


def get_engine() -> TorrentEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = TorrentEngine()
    return _ENGINE
