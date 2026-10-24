"""Bibliothèque : scanne PUNGI_DOWNLOAD_DIR et complète les tags ID3 manquants des MP3
(album, artiste de l'album, piste, disque, année, genre, pochette) à partir de Deezer.

Deux temps : le scan propose des changements sans rien écrire, puis on applique
ceux qui sont retenus. Un tag déjà rempli n'est jamais modifié.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TPE2, TPOS, TRCK, ID3NoHeaderError
from mutagen.mp3 import MP3

from . import deezer
from .downloader import _norm, _similar

# champ → trame ID3 ; « cover » correspond à la trame APIC
FRAMES = {"album": TALB, "album_artist": TPE2, "track": TRCK, "disc": TPOS, "year": TDRC, "genre": TCON}
# champs qui n'ont de sens que si l'album trouvé est bien celui du fichier
_ALBUM_FIELDS = ("album", "album_artist", "track", "disc", "year", "cover")
_FILENAME_RE = re.compile(r"^(?:\d+\s*-\s*)?(.+?)\s+-\s+(.+)$")


class Busy(Exception):
    pass


@dataclass
class Item:
    path: Path
    rel: str
    title: str = ""
    artists: list[str] = field(default_factory=list)
    album: str = ""
    status: str = "pending"  # pending | complete | proposed | unmatched | error | applied
    changes: dict[str, str] = field(default_factory=dict)
    match: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {"path": self.rel, "title": self.title, "artists": self.artists, "album": self.album,
                "status": self.status, "changes": self.changes, "match": self.match, "error": self.error}


def _text(tags: ID3, frame: str) -> str:
    f = tags.get(frame)
    return str(f.text[0]).strip() if f and f.text else ""


def _read_tags(path: Path) -> tuple[ID3, float]:
    audio = MP3(path)
    return audio.tags if audio.tags is not None else ID3(), audio.info.length


def _genre(album: dict) -> str:
    return next((g["name"] for g in (album.get("genres") or {}).get("data") or []), "")


def _missing(tags: ID3) -> set[str]:
    missing = {name for name, frame in FRAMES.items() if not _text(tags, frame.__name__)}
    if not tags.getall("APIC"):
        missing.add("cover")
    missing.discard("disc")  # ajouté seulement si Deezer indique un disque > 1
    return missing


class Task:
    """Travail de fond avec progression. Un seul à la fois, toutes tâches confondues :
    on ne déplace pas un fichier pendant qu'on écrit ses tags."""

    _lock = threading.Lock()
    _running: Task | None = None

    def __init__(self, root: Path, workers: int = 4) -> None:
        self.root = root
        self.workers = workers
        self.status = "idle"
        self.done = 0
        self.total = 0
        self.error: str | None = None

    def _start(self, status: str, target, *args) -> None:
        with Task._lock:
            if Task._running is not None:
                raise Busy("Un scan ou une mise à jour est déjà en cours.")
            Task._running = self
            self.status, self.error = status, None
        threading.Thread(target=self._run, args=(target, *args), daemon=True,
                         name=f"pungi-{type(self).__name__.lower()}-{status}").start()

    def _tick(self) -> None:
        with Task._lock:
            self.done += 1

    def _run(self, target, *args) -> None:
        try:
            target(*args)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
        finally:
            self.status = "ready"
            with Task._lock:
                Task._running = None

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            raise FileNotFoundError(f"Dossier introuvable : {self.root}")
        files = []
        for p in self.root.rglob("*.mp3"):
            rel = p.relative_to(self.root)
            # .zips, .doublons, dossiers cachés et fichiers en cours de conversion
            if any(part.startswith(".") for part in rel.parts) or p.name.endswith(".part.mp3"):
                continue
            files.append(p)
        return sorted(files, key=lambda p: str(p).lower())


class Library(Task):
    def __init__(self, root: Path, workers: int = 4) -> None:
        super().__init__(root, workers)
        self.deezer = deezer.client
        self.status = "idle"  # idle | scanning | ready | applying
        self.items: list[Item] = []

    def to_dict(self, items: bool = False) -> dict:
        counts: dict[str, int] = {}
        for it in self.items:
            counts[it.status] = counts.get(it.status, 0) + 1
        d = {"root": str(self.root), "status": self.status, "done": self.done, "total": self.total,
             "counts": counts, "error": self.error}
        if items:
            d["items"] = [it.to_dict() for it in self.items]
        return d

    # ------------------------------------------------------------------ scan

    def scan(self) -> None:
        self._start("scanning", self._scan)

    def _scan(self) -> None:
        files = self._files()
        self.items = [Item(p, p.relative_to(self.root).as_posix()) for p in files]
        self.done, self.total = 0, len(self.items)

        def work(it: Item) -> None:
            try:
                self._propose(it)
            except Exception as exc:
                it.status, it.error = "error", str(exc)[:200] or type(exc).__name__
            self._tick()

        with ThreadPoolExecutor(self.workers, thread_name_prefix="pungi-library") as pool:
            list(pool.map(work, self.items))

    def _propose(self, it: Item) -> None:
        tags, length = _read_tags(it.path)
        it.title = _text(tags, "TIT2")
        artist = tags.get("TPE1")
        it.artists = [str(a) for a in artist.text if str(a).strip()] if artist else []
        it.album = _text(tags, "TALB")
        if not it.title or not it.artists:  # repli sur le nom « NN - Artiste - Titre.mp3 »
            m = _FILENAME_RE.match(it.path.stem)
            if m:
                it.artists = it.artists or [m.group(1).strip()]
                it.title = it.title or m.group(2).strip()

        missing = _missing(tags)
        if not missing:
            it.status = "complete"
            return
        if not it.title:
            it.status, it.error = "unmatched", "Titre inconnu (ni tag ni nom de fichier exploitable)."
            return

        hits = self._find(it, length)
        if not hits:
            it.status, it.error = "unmatched", "Aucune correspondance fiable sur Deezer."
            return
        hit = hits[0]

        album = self.deezer.album(hit["album"]["id"])
        same_album = not it.album or _similar(it.album, album.get("title", "")) >= 0.8
        it.match = {"title": hit.get("title"), "artist": (hit.get("artist") or {}).get("name"),
                    "album": album.get("title"), "link": hit.get("link"),
                    "cover": album.get("cover_medium"), "same_album": same_album}

        values: dict[str, str] = {
            "album": album.get("title") or "",
            "album_artist": (album.get("artist") or {}).get("name") or "",
            "year": (album.get("release_date") or "")[:4],
            "genre": _genre(album),
            "cover": album.get("cover_xl") or album.get("cover_big") or "",
        }
        if "genre" in missing and not values["genre"]:
            # album sans genre sur Deezer : on le prend sur une autre édition du même titre
            for other in hits[1:4]:
                values["genre"] = _genre(self.deezer.album(other["album"]["id"]))
                if values["genre"]:
                    break
        if same_album and "track" in missing:
            details = self.deezer.track(hit["id"])
            values["track"] = str(details.get("track_position") or "")
            if (details.get("disk_number") or 1) > 1:
                values["disc"] = str(details["disk_number"])
                missing.add("disc")

        it.changes = {k: v for k, v in values.items()
                      if k in missing and v and (same_album or k not in _ALBUM_FIELDS)}
        it.status = "proposed" if it.changes else "unmatched"
        if not it.changes:
            it.error = ("Deezer ne connaît pas les tags manquants de ce titre." if same_album else
                        f"Trouvé sur un autre album (« {album.get('title')} ») : rien à compléter sans risque.")

    def _find(self, it: Item, length: float) -> list[dict]:
        """Résultats Deezer au titre et à l'artiste concordants et de durée compatible, du meilleur au moins bon."""
        main = it.artists[0]
        queries = [f"{main} {it.title}"]
        if _norm(f"{main} {it.title}") != f"{main} {it.title}".lower():
            queries.append(_norm(f"{main} {it.title}"))
        for q in queries:
            scored = []
            for r in self.deezer.search(q):
                title_sim = max(_similar(r.get("title", ""), it.title), _similar(r.get("title_short", ""), it.title))
                name = (r.get("artist") or {}).get("name", "")
                artist_ok = any(_norm(a) and (_norm(a) in _norm(name) or _norm(name) in _norm(a))
                                for a in it.artists) or _similar(name, main) > 0.6
                if title_sim < 0.8 or not artist_ok:
                    continue
                score = title_sim * 2
                if length and r.get("duration"):
                    diff = abs(r["duration"] - length)
                    if diff > max(10, length * 0.07):
                        continue
                    score -= diff / 30
                if it.album and _similar(it.album, (r.get("album") or {}).get("title", "")) >= 0.8:
                    score += 1
                scored.append((score, r))
            if scored:
                return [r for _, r in sorted(scored, key=lambda x: -x[0])]
        return []

    # ---------------------------------------------------------------- application

    def apply(self, paths: list[str]) -> None:
        wanted = set(paths)
        targets = [it for it in self.items if it.rel in wanted and it.status == "proposed"]
        self._start("applying", self._apply, targets)

    def _apply(self, targets: list[Item]) -> None:
        self.done, self.total = 0, len(targets)

        def work(it: Item) -> None:
            try:
                self._write(it)
                it.status = "applied"
            except Exception as exc:
                it.status, it.error = "error", str(exc)[:200] or type(exc).__name__
            self._tick()

        with ThreadPoolExecutor(self.workers, thread_name_prefix="pungi-library") as pool:
            list(pool.map(work, targets))

    def _write(self, it: Item) -> None:
        try:
            tags = ID3(it.path)
        except ID3NoHeaderError:
            tags = ID3()
        # relu au moment d'écrire : un tag rempli entre-temps n'est pas écrasé
        missing = _missing(tags) | ({"disc"} if not _text(tags, "TPOS") else set())
        for name, value in it.changes.items():
            if name not in missing:
                continue
            if name == "cover":
                img = self.deezer.http.get(value, follow_redirects=True)
                img.raise_for_status()
                tags.add(APIC(encoding=3, mime=img.headers.get("content-type", "image/jpeg"),
                              type=3, desc="Cover", data=img.content))
            else:
                tags.add(FRAMES[name](encoding=3, text=value))
        tags.save(it.path, v2_version=3)
