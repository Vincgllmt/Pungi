"""Doublons : repère dans PUNGI_DOWNLOAD_DIR les MP3 d'un même titre (titre, artiste et durée
concordants) et met de côté ceux qu'on ne garde pas.

La version gardée par défaut est celle d'un album plutôt que d'un single, puis la plus complète
(débit, pochette, tags). Rien n'est supprimé : les fichiers écartés sont déplacés dans
.doublons/ (même arborescence), que le scan ignore et qu'on peut vider à la main.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from .downloader import _BAD_WORDS, _norm, _similar
from .library import _FILENAME_RE, Task, _text

TRASH = ".doublons"
# mentions d'édition qui ne changent pas l'enregistrement
_EDITION_RE = re.compile(r"[(\[][^)\]]*(?:remaster|single version|album version|explicit|clean|mono|stereo)[^)\]]*[)\]]", re.I)
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:[,;/&]|\bfeat\.?|\bft\.?|\bx\b)\s*", re.I)
_VARIOUS = {"various artists", "various", "artistes divers", "multi interpretes", "va", "compilation"}


@dataclass
class File:
    path: Path
    rel: str
    title: str = ""
    artists: list[str] = field(default_factory=list)
    album: str = ""
    album_artist: str = ""
    year: str = ""
    track_total: str = ""
    compilation: bool = False
    cover: bool = False
    filled: int = 0  # nombre de tags remplis, pour départager
    length: float = 0.0
    bitrate: int = 0
    size: int = 0
    status: str = "ok"  # ok | moved | error
    error: str | None = None

    @property
    def key(self) -> str:
        return _norm(_EDITION_RE.sub(" ", self.title))

    @property
    def album_key(self) -> tuple[str, str]:
        return _norm(self.album), _norm(self.album_artist)

    @property
    def single(self) -> bool:
        """Sortie en single : pas d'album, album du nom du titre, « - Single » ou piste 1/1."""
        return (not self.album or _norm(self.album) == self.key or _similar(self.album, self.title) >= 0.9
                or bool(re.search(r"\bsingle\b", self.album, re.I)) or self.track_total == "1")

    def to_dict(self) -> dict:
        return {"path": self.rel, "title": self.title, "artists": self.artists, "album": self.album,
                "year": self.year, "single": self.single, "compilation": self.compilation, "cover": self.cover,
                "duration": round(self.length), "bitrate": self.bitrate // 1000, "size": self.size,
                "status": self.status, "error": self.error}


def _artists(f: File) -> set[str]:
    return {n for a in f.artists for part in _ARTIST_SPLIT_RE.split(a) if (n := _norm(part))}


def _same_recording(a: File, b: File) -> bool:
    if a.key != b.key:
        return False
    ta, tb = a.title.lower(), b.title.lower()
    if any((w in ta) != (w in tb) for w in _BAD_WORDS):  # « Titre » et « Titre - Live »
        return False
    if a.length and b.length and abs(a.length - b.length) > max(4, min(a.length, b.length) * 0.03):
        return False
    na, nb = _artists(a), _artists(b)
    if not na or not nb:
        return True  # artiste inconnu d'un côté : le titre et la durée suffisent
    return bool(na & nb) or any(x in y or y in x for x in na for y in nb)


class Duplicates(Task):
    def __init__(self, root: Path, workers: int = 4) -> None:
        super().__init__(root, workers)
        self.status = "idle"  # idle | scanning | ready | moving
        self.groups: list[list[File]] = []
        self.unreadable: list[File] = []
        self.moved = 0
        self._album_sizes: Counter = Counter()

    def to_dict(self, groups: bool = False) -> dict:
        d = {"root": str(self.root), "trash": str(self.root / TRASH), "status": self.status, "done": self.done,
             "total": self.total, "error": self.error, "moved": self.moved, "group_count": len(self.groups)}
        if groups:
            d["groups"] = [{"keep": self._best(g).rel, "files": [f.to_dict() for f in g]} for g in self.groups]
            d["unreadable"] = [f.to_dict() for f in self.unreadable]
        return d

    # ------------------------------------------------------------------ scan

    def scan(self) -> None:
        self._start("scanning", self._scan)

    def _scan(self) -> None:
        files = [File(p, p.relative_to(self.root).as_posix()) for p in self._files()]
        self.groups, self.unreadable, self.moved = [], [], 0
        self.done, self.total = 0, len(files)

        def work(f: File) -> None:
            try:
                self._read(f)
            except Exception as exc:
                f.status, f.error = "error", str(exc)[:200] or type(exc).__name__
            self._tick()

        with ThreadPoolExecutor(self.workers, thread_name_prefix="pungi-duplicates") as pool:
            list(pool.map(work, files))
        self.unreadable = [f for f in files if f.status == "error"]

        # taille de chaque album dans la bibliothèque : un « album » d'un seul fichier est sans doute un single
        self._album_sizes = Counter(f.album_key for f in files if f.album and f.status == "ok")
        buckets: dict[str, list[File]] = {}
        for f in files:
            if f.status == "ok" and f.key:
                buckets.setdefault(f.key, []).append(f)
        groups = []
        for bucket in buckets.values():
            clusters: list[list[File]] = []
            for f in bucket:
                target = next((c for c in clusters if all(_same_recording(f, o) for o in c)), None)
                if target:
                    target.append(f)
                else:
                    clusters.append([f])
            groups += [sorted(c, key=self._score, reverse=True) for c in clusters if len(c) > 1]
        self.groups = sorted(groups, key=lambda g: (_norm(g[0].artists[0]) if g[0].artists else "", g[0].key))

    def _read(self, f: File) -> None:
        audio = MP3(f.path)
        tags = audio.tags if audio.tags is not None else ID3()
        f.length, f.bitrate = audio.info.length, audio.info.bitrate
        f.title = _text(tags, "TIT2")
        artist = tags.get("TPE1")
        f.artists = [str(a) for a in artist.text if str(a).strip()] if artist else []
        if not f.title or not f.artists:  # repli sur le nom « NN - Artiste - Titre.mp3 »
            m = _FILENAME_RE.match(f.path.stem)
            if m:
                f.artists = f.artists or [m.group(1).strip()]
                f.title = f.title or m.group(2).strip()
        f.title = f.title or f.path.stem
        f.album, f.album_artist = _text(tags, "TALB"), _text(tags, "TPE2")
        f.year = _text(tags, "TDRC")[:4]
        trck = _text(tags, "TRCK")
        f.track_total = trck.split("/", 1)[1].strip() if "/" in trck else ""
        f.compilation = _text(tags, "TCMP") == "1" or _norm(f.album_artist) in _VARIOUS
        f.cover = bool(tags.getall("APIC"))
        f.filled = sum(bool(_text(tags, fr)) for fr in ("TALB", "TPE2", "TRCK", "TDRC", "TCON"))
        f.size = f.path.stat().st_size

    def _score(self, f: File) -> tuple:
        """Du meilleur au moins bon : album plutôt que single, hors compilation, album le plus
        fourni dans la bibliothèque, puis meilleur débit, pochette, tags remplis, chemin le plus court."""
        album_size = 0 if f.single else self._album_sizes.get(f.album_key, 0)
        return (not f.single, not f.compilation, album_size, f.bitrate, f.cover, f.filled, -len(f.rel))

    def _best(self, group: list[File]) -> File:
        return next((f for f in group if f.status == "ok"), group[0])

    # ------------------------------------------------------------- mise de côté

    def move(self, paths: list[str]) -> None:
        wanted = set(paths)
        targets = []
        for g in self.groups:
            chosen = [f for f in g if f.rel in wanted and f.status == "ok"]
            # on garde toujours au moins un exemplaire du titre
            if chosen and len(chosen) < sum(f.status == "ok" for f in g):
                targets += chosen
        self._start("moving", self._move, targets)

    def _move(self, targets: list[File]) -> None:
        self.done, self.total = 0, len(targets)
        for f in targets:
            try:
                dest = self.root / TRASH / f.rel
                n = 2
                while dest.exists():
                    dest = dest.with_name(f"{Path(f.rel).stem} ({n}){dest.suffix}")
                    n += 1
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(f.path, dest)
                f.status = "moved"
                self.moved += 1
                self._prune(f.path.parent)
            except Exception as exc:
                f.status, f.error = "error", str(exc)[:200] or type(exc).__name__
            self._tick()

    def _prune(self, folder: Path) -> None:
        """Supprime les dossiers devenus vides, sans jamais toucher à la racine."""
        while folder != self.root and self.root in folder.parents:
            try:
                folder.rmdir()  # échoue si le dossier n'est pas vide
            except OSError:
                return
            folder = folder.parent
