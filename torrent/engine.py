from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from SYS.utils import format_bytes, sanitize_filename, unique_path


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
    seeds: int = 0
    leechers: int = 0
    total_wanted: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)

    def snapshot(self) -> Dict[str, Any]:
        pct = max(0.0, min(100.0, float(self.progress) * 100.0))
        status = "paused" if self.paused and self.status != "done" else self.status
        progress = f"{pct:.1f}%"
        down = _fmt_rate(self.download_rate)
        up = _fmt_rate(self.upload_rate)
        size = format_bytes(self.total_wanted)
        extras = [
            ("Status", status, "Title"),
            ("Progress", progress, "Title"),
            ("Down", down, "Title"),
            ("Up", up, "Title"),
            ("Seeds", str(self.seeds), "Title"),
            ("Leechers", str(self.leechers), "Title"),
            ("Peers", str(self.peers), "Title"),
        ]
        if self.error:
            extras.append(("Error", self.error, "Title"))
        return {
            "id": self.job_id,
            "title": self.title,
            "status": status,
            "progress": progress,
            "down": down,
            "up": up,
            "peers": str(self.peers),
            "seeds": str(self.seeds),
            "leechers": str(self.leechers),
            "size": size,
            "path": str(self.save_path),
            "magnet": self.magnet,
            "error": self.error,
            "plugin": "torrent",
            "instance": "jobs",
            "table": "torrent.jobs",
            "_detail_extras": extras,
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


def plugin_root() -> Path:
    return Path(__file__).resolve().parent


def default_dirs() -> tuple[Path, Path, Path]:
    root = plugin_root()
    incomplete = root / "incomplete"
    complete = root / "complete"
    state = root / "state"
    for path in (incomplete, complete, state):
        path.mkdir(parents=True, exist_ok=True)
    return incomplete, complete, state


class TorrentEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, TorrentJob] = {}
        self._order: List[str] = []
        self._ses: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._seq = 0
        self._incomplete, self._complete, self._state = default_dirs()
        self._last_resume = 0.0
        self._auto_resume = False

    def set_dirs(self, incomplete: Path, complete: Path) -> None:
        self._incomplete = Path(incomplete)
        self._complete = Path(complete)
        self._incomplete.mkdir(parents=True, exist_ok=True)
        self._complete.mkdir(parents=True, exist_ok=True)

    def set_auto_resume(self, enabled: bool) -> None:
        self._auto_resume = bool(enabled)

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
        self._restore()
        self._thread = threading.Thread(target=self._loop, name="torrent-engine", daemon=True)
        self._thread.start()
        return ses

    def _state_file(self) -> Path:
        self._state.mkdir(parents=True, exist_ok=True)
        return self._state / "jobs.json"

    def _resume_file(self, job_id: str) -> Path:
        self._state.mkdir(parents=True, exist_ok=True)
        return self._state / f"{job_id}.resume"

    def _persist(self) -> None:
        rows = []
        with self._lock:
            for job_id in self._order:
                job = self._jobs.get(job_id)
                if job is None:
                    continue
                rows.append(
                    {
                        "job_id": job.job_id,
                        "title": job.title,
                        "magnet": job.magnet,
                        "save_path": str(job.save_path),
                        "paused": job.paused,
                        "status": job.status,
                        "progress": job.progress,
                        "total_wanted": job.total_wanted,
                        "seeds": job.seeds,
                        "leechers": job.leechers,
                        "peers": job.peers,
                    }
                )
        try:
            self._state_file().write_text(json.dumps(rows, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _write_resume(self, job: TorrentJob) -> None:
        handle = job.handle
        if handle is None:
            return
        try:
            lt = _lt()
            if hasattr(handle, "write_resume_data") and hasattr(lt, "write_resume_data_buf"):
                payload = lt.write_resume_data_buf(handle.write_resume_data())
                self._resume_file(job.job_id).write_bytes(payload)
                return
            if hasattr(handle, "save_resume_data"):
                handle.save_resume_data()
        except Exception:
            pass

    def _add_handle(self, magnet: str, save_dir: Path, resume_path: Optional[Path] = None) -> Any:
        lt = _lt()
        ses = self._session()
        save_dir.mkdir(parents=True, exist_ok=True)
        if resume_path is not None and resume_path.is_file() and hasattr(lt, "read_resume_data"):
            try:
                atp = lt.read_resume_data(resume_path.read_bytes())
                atp.save_path = str(save_dir)
                return ses.add_torrent(atp)
            except Exception:
                pass
        if hasattr(lt, "parse_magnet_uri") and hasattr(ses, "add_torrent"):
            params = lt.parse_magnet_uri(magnet)
            params.save_path = str(save_dir)
            return ses.add_torrent(params)
        return lt.add_magnet_uri(ses, magnet, {"save_path": str(save_dir)})

    def _restore(self) -> None:
        if self._jobs:
            return
        path = self._state_file()
        if not path.is_file():
            return
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(rows, list):
            return
        max_id = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            job_id = str(row.get("job_id") or "").strip()
            magnet = str(row.get("magnet") or "").strip()
            title = str(row.get("title") or "torrent").strip() or "torrent"
            save_path = Path(str(row.get("save_path") or ""))
            if not job_id or not magnet:
                continue
            handle = None
            try:
                handle = self._add_handle(magnet, save_path, self._resume_file(job_id))
            except Exception:
                handle = None
            job = TorrentJob(
                job_id=job_id,
                title=title,
                magnet=magnet,
                save_path=save_path if str(save_path) else self._incomplete / sanitize_filename(title),
                handle=handle,
                paused=bool(row.get("paused")),
                status=str(row.get("status") or "queued"),
                progress=float(row.get("progress") or 0),
                total_wanted=int(row.get("total_wanted") or 0),
                seeds=int(row.get("seeds") or 0),
                leechers=int(row.get("leechers") or 0),
                peers=int(row.get("peers") or 0),
            )
            saved_paused = bool(row.get("paused"))
            status = str(row.get("status") or "queued")
            pause_now = saved_paused or (not self._auto_resume and status != "done")
            if handle is not None and (pause_now or (not self._auto_resume and status == "done")):
                try:
                    handle.pause()
                except Exception:
                    pass
            job.paused = pause_now
            if pause_now and status != "done":
                job.status = "paused"
            with self._lock:
                self._jobs[job_id] = job
                if job_id not in self._order:
                    self._order.append(job_id)
            try:
                from SYS import plugin_jobs

                plugin_jobs.submit(
                    "torrent",
                    title,
                    job_id=job_id,
                    pause=lambda jid=job_id: self.pause(jid),
                    resume=lambda jid=job_id: self.resume(jid),
                    cancel=lambda jid=job_id: self.remove(jid),
                    snapshot=job.snapshot,
                )
            except Exception:
                pass
            try:
                max_id = max(max_id, int(job_id))
            except Exception:
                pass
        self._seq = max(self._seq, max_id)

    def _maybe_complete(self, job: TorrentJob) -> None:
        if job.status != "done" or job.handle is None:
            return
        try:
            incomplete_root = self._incomplete.resolve()
            current = Path(job.save_path).resolve()
            if incomplete_root not in current.parents and current != incomplete_root:
                if self._complete.resolve() in current.parents or current.parent == self._complete.resolve():
                    return
        except Exception:
            pass
        dest = unique_path(self._complete / Path(job.save_path).name)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            job.handle.move_storage(str(dest))
            job.save_path = dest
            self._persist()
        except Exception:
            pass

    def add(self, magnet: str, output_dir: Path, title: str) -> TorrentJob:
        output_dir = Path(output_dir) if output_dir else self._incomplete
        output_dir.mkdir(parents=True, exist_ok=True)
        save_dir = unique_path(output_dir / sanitize_filename(title or "torrent"))
        save_dir.mkdir(parents=True, exist_ok=True)
        handle = self._add_handle(magnet, save_dir)
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
        self._persist()
        try:
            from SYS import plugin_jobs

            plugin_jobs.submit(
                "torrent",
                title or "torrent",
                job_id=job.job_id,
                pause=lambda: self.pause(job.job_id),
                resume=lambda: self.resume(job.job_id),
                cancel=lambda: self.remove(job.job_id),
                snapshot=job.snapshot,
            )
        except Exception:
            pass
        return job

    def jobs(self) -> List[TorrentJob]:
        with self._lock:
            return [self._jobs[i] for i in self._order if i in self._jobs]

    def get(self, job_id: str) -> Optional[TorrentJob]:
        with self._lock:
            return self._jobs.get(str(job_id or "").strip())

    def is_complete(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        if float(job.progress or 0) >= 0.999:
            return True
        return str(job.status or "").strip().lower() in {"done", "finished", "seeding"}

    def list_files(self, job_id: str) -> List[Dict[str, Any]]:
        job = self.get(job_id)
        if job is None:
            return []
        rows: List[Dict[str, Any]] = []
        seen: set[str] = set()
        root = Path(job.save_path)

        def _add(rel: str, size: int, done: Optional[int] = None) -> None:
            rel_text = str(rel or "").replace("\\", "/").strip("/")
            if not rel_text:
                return
            full = (root / rel_text) if not Path(rel_text).is_absolute() else Path(rel_text)
            key = str(full)
            if key in seen:
                return
            seen.add(key)
            name = Path(rel_text).name
            size_int = int(size or 0)
            pct = ""
            if done is not None and size_int > 0:
                pct = f"{min(100.0, float(done) / float(size_int) * 100.0):.1f}%"
            elif full.is_file():
                pct = "100.0%"
            rows.append(
                {
                    "title": rel_text,
                    "name": name,
                    "path": str(full),
                    "size_bytes": size_int,
                    "size": format_bytes(size_int),
                    "exists": full.is_file(),
                    "plugin": "torrent",
                    "table": "torrent.files",
                    "job_id": job.job_id,
                    "progress": pct,
                }
            )

        handle = job.handle
        ti = None
        if handle is not None:
            try:
                if hasattr(handle, "torrent_file"):
                    ti = handle.torrent_file()
                elif hasattr(handle, "get_torrent_info"):
                    ti = handle.get_torrent_info()
            except Exception:
                ti = None
        if ti is not None:
            try:
                fs = ti.files()
                count = int(fs.num_files())
                prog = None
                try:
                    prog = list(handle.file_progress())
                except Exception:
                    prog = None
                for idx in range(count):
                    if hasattr(fs, "file_path"):
                        rel = fs.file_path(idx)
                    else:
                        rel = fs.at(idx).path
                    if hasattr(fs, "file_size"):
                        size = int(fs.file_size(idx))
                    else:
                        size = int(fs.at(idx).size)
                    done = int(prog[idx]) if prog is not None and idx < len(prog) else None
                    _add(str(rel), size, done)
            except Exception:
                rows.clear()
                seen.clear()
        if not rows and root.exists():
            if root.is_file():
                _add(root.name, int(root.stat().st_size))
            else:
                for path in sorted(p for p in root.rglob("*") if p.is_file()):
                    try:
                        rel = str(path.relative_to(root))
                    except Exception:
                        rel = path.name
                    try:
                        size = int(path.stat().st_size)
                    except Exception:
                        size = 0
                    _add(rel, size)
        return rows

    def pause(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.handle is None:
            return False
        try:
            job.handle.pause()
            job.paused = True
            job.status = "paused"
            self._persist()
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
            self._persist()
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
        try:
            self._resume_file(str(job_id)).unlink(missing_ok=True)
        except Exception:
            pass
        self._persist()
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
                job.seeds = int(
                    getattr(st, "num_seeds", 0)
                    or getattr(st, "num_complete", 0)
                    or 0
                )
                job.leechers = int(
                    getattr(st, "num_incomplete", 0)
                    or max(0, job.peers - job.seeds)
                )
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
                    self._maybe_complete(job)
                elif not has_meta:
                    job.status = "metadata"
                else:
                    job.status = "downloading"
            now = time.monotonic()
            if now - self._last_resume > 15:
                self._last_resume = now
                for job in jobs:
                    self._write_resume(job)
                self._persist()

    def ensure(self) -> None:
        try:
            from SYS.config import load_config
            from SYS.utils import coerce_bool

            cfg = load_config()
            torrent_cfg = ((cfg.get("plugin") or {}).get("torrent") or {})
            if isinstance(torrent_cfg.get("default"), dict):
                torrent_cfg = torrent_cfg.get("default") or torrent_cfg
            self._auto_resume = coerce_bool(torrent_cfg.get("auto_resume"), False)
        except Exception:
            pass
        try:
            self._session()
        except Exception:
            try:
                self._restore()
            except Exception:
                pass


_ENGINE: Optional[TorrentEngine] = None


def get_engine() -> TorrentEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = TorrentEngine()
    _ENGINE.ensure()
    return _ENGINE
