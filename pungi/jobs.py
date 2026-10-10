"""File de téléchargements : un job par lien Spotify, pistes traitées en parallèle."""

from __future__ import annotations

import os
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import spotify
from .downloader import Downloader, safe_name

DOWNLOAD_DIR = Path(os.getenv("PUNGI_DOWNLOAD_DIR", Path.cwd() / "downloads")).resolve()
CONCURRENCY = max(1, int(os.getenv("PUNGI_CONCURRENCY", "4")))


@dataclass
class TrackState:
    track: spotify.Track
    status: str = "pending"  # pending | searching | downloading | converting | done | error
    progress: float = 0.0
    error: str | None = None
    file: Path | None = None
    source: str = ""
    attempts: int = 0


@dataclass
class Job:
    id: str
    url: str
    collection: spotify.Collection
    folder: Path
    tracks: list[TrackState] = field(default_factory=list)
    zip_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def finished(self) -> bool:
        return all(s.status in ("done", "error") for s in self.tracks)

    def to_dict(self) -> dict:
        c = self.collection
        done = sum(s.status == "done" for s in self.tracks)
        failed = sum(s.status == "error" for s in self.tracks)
        return {
            "id": self.id, "url": self.url, "kind": c.kind, "name": c.name, "subtitle": c.subtitle,
            "cover": c.cover_url, "truncated": c.truncated, "folder": str(self.folder),
            "total": len(self.tracks), "done": done, "failed": failed, "finished": self.finished,
            "progress": sum(s.progress for s in self.tracks) / max(1, len(self.tracks)),
            "tracks": [{
                "index": i, "title": s.track.title, "artists": s.track.artists,
                "duration_ms": s.track.duration_ms, "status": s.status,
                "progress": round(s.progress, 3), "error": s.error, "source": s.source,
            } for i, s in enumerate(self.tracks)],
        }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="pungi")
        self.downloader = Downloader()

    def create(self, url: str) -> Job:
        collection = spotify.fetch(url)
        if not collection.tracks:
            raise spotify.SpotifyError("Aucune piste trouvée pour ce lien.")
        folder = DOWNLOAD_DIR if collection.kind == "track" else DOWNLOAD_DIR / safe_name(collection.name)
        job = Job(uuid.uuid4().hex[:12], url, collection, folder,
                  [TrackState(t) for t in collection.tracks])
        self.jobs[job.id] = job
        for state in job.tracks:
            self.pool.submit(self._run, job, state)
        return job

    def retry_failed(self, job: Job) -> int:
        failed = [s for s in job.tracks if s.status == "error"]
        for s in failed:
            s.status, s.progress, s.error, s.attempts = "pending", 0.0, None, 0
            self.pool.submit(self._run, job, s)
        return len(failed)

    def _run(self, job: Job, state: TrackState) -> None:
        def progress(status: str, value: float) -> None:
            state.status, state.progress = status, value

        try:
            if not state.track.cover_url:
                try:
                    spotify.enrich_from_embed(state.track)
                except Exception:
                    pass
            state.attempts += 1
            state.file, state.source = self.downloader.download(state.track, job.folder, progress)
        except Exception as exc:
            if "403" in str(exc) and state.attempts < 2:
                # YouTube bride les rafales : on remet la piste en file un peu plus tard
                state.status, state.progress = "pending", 0.0
                threading.Timer(20, self.pool.submit, [self._run, job, state]).start()
                return
            state.status, state.error = "error", _short_error(exc)

    def build_zip(self, job: Job) -> Path:
        files = [s.file for s in job.tracks if s.status == "done" and s.file and s.file.exists()]
        target = DOWNLOAD_DIR / ".zips" / f"{safe_name(job.collection.name)} [{job.id}].zip"
        with job.zip_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            # MP3 déjà compressé : ZIP_STORED, quasi instantané
            with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
                for f in files:
                    zf.write(f, f.name)
        return target


def _short_error(exc: Exception) -> str:
    msg = str(exc).strip().splitlines()[-1] if str(exc).strip() else type(exc).__name__
    msg = msg.removeprefix("ERROR: ")
    return msg[:200]
