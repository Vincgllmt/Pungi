"""File de téléchargements : un job par lien Spotify, résultat de recherche ou source de
l'export Spotify, pistes traitées en parallèle."""

from __future__ import annotations

import os
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from mutagen.id3 import ID3

from . import deezer, spotify
from .downloader import Downloader, _norm, safe_name
from .library import _FILENAME_RE

DOWNLOAD_DIR = Path(os.getenv("PUNGI_DOWNLOAD_DIR", Path.cwd() / "downloads")).resolve()
CONCURRENCY = max(1, int(os.getenv("PUNGI_CONCURRENCY", "4")))


@dataclass
class TrackState:
    track: spotify.Track
    status: str = "pending"  # pending | searching | downloading | converting | done | error | cancelled
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
    imported: bool = False  # pistes de l'export Spotify : titres déjà présents ignorés
    cancelled: bool = False

    @property
    def finished(self) -> bool:
        return all(s.status in ("done", "error", "cancelled") for s in self.tracks)

    def to_dict(self) -> dict:
        c = self.collection
        done = sum(s.status == "done" for s in self.tracks)
        failed = sum(s.status == "error" for s in self.tracks)
        return {
            "id": self.id, "url": self.url, "kind": c.kind, "name": c.name, "subtitle": c.subtitle,
            "cover": c.cover_url, "truncated": c.truncated, "folder": str(self.folder),
            "total": len(self.tracks), "done": done, "failed": failed, "finished": self.finished,
            "imported": self.imported, "owned": sum(s.source == "owned" for s in self.tracks),
            "cancelled": sum(s.status == "cancelled" for s in self.tracks),
            "progress": sum(s.progress for s in self.tracks) / max(1, len(self.tracks)),
            "tracks": [{
                "index": i, "title": s.track.title, "artists": s.track.artists,
                "duration_ms": s.track.duration_ms, "status": s.status,
                "progress": round(s.progress, 3), "error": s.error, "source": s.source,
            } for i, s in enumerate(self.tracks)],
        }


class Owned:
    """Titres déjà présents dans le dossier de musique, par (titre, artiste) normalisés.
    Construit au premier besoin, complété au fil des téléchargements."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._files: dict[tuple[str, str], Path] | None = None

    def reset(self) -> None:
        with self._lock:
            self._files = None

    def _build(self) -> dict[tuple[str, str], Path]:
        files: dict[tuple[str, str], Path] = {}
        if not self.root.is_dir():
            return files
        for p in self.root.rglob("*.mp3"):
            rel = p.relative_to(self.root)
            if any(part.startswith(".") for part in rel.parts) or p.name.endswith(".part.mp3"):
                continue
            try:
                tags = ID3(p)
                title = str(tags["TIT2"].text[0]) if "TIT2" in tags else ""
                artists = [str(a) for a in tags["TPE1"].text] if "TPE1" in tags else []
            except Exception:  # tags illisibles : on tente le nom du fichier
                title, artists = "", []
            if not title or not artists:  # repli sur le nom « NN - Artiste - Titre.mp3 »
                m = _FILENAME_RE.match(p.stem)
                if not m:
                    continue
                title, artists = title or m.group(2), artists or [m.group(1)]
            for a in artists:
                files.setdefault((_norm(title), _norm(a)), p)
        return files

    def find(self, t: spotify.Track) -> Path | None:
        with self._lock:
            if self._files is None:
                self._files = self._build()
            for a in t.artists:
                p = self._files.get((_norm(t.title), _norm(a)))
                if p and p.exists():
                    return p
        return None

    def add(self, t: spotify.Track, path: Path) -> None:
        with self._lock:
            if self._files is not None:
                for a in t.artists:
                    self._files.setdefault((_norm(t.title), _norm(a)), path)


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="pungi")
        self.downloader = Downloader()
        self.owned = Owned(DOWNLOAD_DIR)

    def create(self, url: str) -> Job:
        collection = deezer.fetch(url) if deezer.is_ref(url) else spotify.fetch(url)
        if not collection.tracks:
            raise spotify.SpotifyError("Aucune piste trouvée.")
        return self.add(url, collection)

    def add(self, url: str, collection: spotify.Collection, imported: bool = False) -> Job:
        folder = DOWNLOAD_DIR if collection.kind == "track" else DOWNLOAD_DIR / safe_name(collection.name)
        job = Job(uuid.uuid4().hex[:12], url, collection, folder,
                  [TrackState(t) for t in collection.tracks], imported=imported)
        self.jobs[job.id] = job
        for state in job.tracks:
            self.pool.submit(self._run, job, state)
        return job

    def retry_failed(self, job: Job) -> int:
        job.cancelled = False
        failed = [s for s in job.tracks if s.status in ("error", "cancelled")]
        for s in failed:
            s.status, s.progress, s.error, s.attempts = "pending", 0.0, None, 0
            self.pool.submit(self._run, job, s)
        return len(failed)

    def cancel(self, job: Job) -> int:
        """Les pistes en attente sont annulées ; celles en cours se terminent."""
        job.cancelled = True
        pending = [s for s in job.tracks if s.status == "pending"]
        for s in pending:
            s.status = "cancelled"
        return len(pending)

    def _run(self, job: Job, state: TrackState) -> None:
        def progress(status: str, value: float) -> None:
            state.status, state.progress = status, value

        if job.cancelled or state.status == "cancelled":
            state.status = "cancelled"
            return
        try:
            if job.imported:
                if self._skip_owned(state):
                    return
                state.status, state.progress = "searching", 0.02
                self._complete(state.track)
            elif not state.track.cover_url and state.track.spotify_id:
                try:
                    spotify.enrich_from_embed(state.track)
                except Exception:
                    pass
            state.attempts += 1
            state.file, state.source = self.downloader.download(
                state.track, job.folder, progress, numbered=not job.imported)
            if job.imported:
                self.owned.add(state.track, state.file)
        except Exception as exc:
            if "403" in str(exc) and state.attempts < 2:
                # YouTube bride les rafales : on remet la piste en file un peu plus tard
                state.status, state.progress = "pending", 0.0
                threading.Timer(20, self.pool.submit, [self._run, job, state]).start()
                return
            state.status, state.error = "error", _short_error(exc)

    def _skip_owned(self, state: TrackState) -> bool:
        path = self.owned.find(state.track)
        if path:
            state.file, state.source, state.status, state.progress = path, "owned", "done", 1.0
        return bool(path)

    @staticmethod
    def _complete(track: spotify.Track) -> None:
        """Pistes de l'export : Deezer d'abord (quota large), Spotify seulement si Deezer
        ne connaît pas le même album. Sans métadonnées, le téléchargement se fait quand même."""
        try:
            if deezer.enrich(track):
                return
        except Exception:
            pass
        if track.spotify_id:
            try:
                spotify.enrich_from_embed(track)
            except Exception:
                pass

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
