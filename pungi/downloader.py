"""Recherche de la source audio (YouTube Music, puis YouTube), téléchargement,
conversion MP3 et écriture des tags."""

from __future__ import annotations

import difflib
import os
import random
import re
import subprocess
import tempfile
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Callable

import httpx
import imageio_ffmpeg
import yt_dlp
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError

from .spotify import USER_AGENT, Track

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
BITRATE = "320k"

# Versions qu'on ne veut pas, sauf si le titre Spotify les mentionne lui-même
_BAD_WORDS = ("live", "cover", "karaoke", "instrumental", "remix", "sped up", "slowed",
              "nightcore", "8d", "reverb", "acapella", "a cappella", "lyrics video", "1 hour")

Progress = Callable[[str, float], None]


class NotFound(Exception):
    pass


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\((?:feat|ft|with)[^)]*\)|\[(?:feat|ft|with)[^]]*\]", " ", s)
    s = re.sub(r"\s-\s.*(remaster|version|edit|mono|stereo).*$", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _has_bad_word(candidate: str, reference: str) -> bool:
    c, r = candidate.lower(), reference.lower()
    return any(w in c and w not in r for w in _BAD_WORDS)


def safe_name(s: str, maxlen: int = 150) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip().rstrip(".")
    return (s[:maxlen].rstrip() or "sans titre")


def track_filename(t: Track) -> str:
    prefix = f"{t.track_number:02d} - " if t.track_number else ""
    return safe_name(f"{prefix}{t.main_artist} - {t.title}") + ".mp3"


class Downloader:
    def __init__(self) -> None:
        self.base_opts = {
            "quiet": True, "no_warnings": True, "noprogress": True,
            "js_runtimes": {"node": {}, "deno": {}},
            "retries": 5, "fragment_retries": 5,
        }
        # ex. PUNGI_COOKIES_BROWSER=firefox : permet les titres soumis à vérification d'âge
        if browser := os.getenv("PUNGI_COOKIES_BROWSER"):
            self.base_opts["cookiesfrombrowser"] = (browser,)

    # ------------------------------------------------------------------ recherche

    def _flat(self, query_url: str, n: int) -> list[dict]:
        with yt_dlp.YoutubeDL({**self.base_opts, "extract_flat": True, "playlistend": n}) as y:
            info = y.extract_info(query_url, download=False)
        return [e for e in (info or {}).get("entries") or [] if e and e.get("id")]

    def _duration_ok(self, t: Track, seconds: float | None) -> bool:
        if not seconds or not t.duration_ms:
            return True
        target = t.duration_ms / 1000
        return abs(seconds - target) <= max(10, target * 0.07)

    def candidates(self, t: Track, ydl: yt_dlp.YoutubeDL):
        """Génère, du meilleur au moins bon, les infos yt-dlp complètes des sources plausibles."""
        artists = " ".join(t.artists[:2])
        tried: set[str] = set()

        def full(url: str) -> dict | None:
            try:
                return ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError:  # vidéo bloquée, soumise à l'âge, etc.
                return None

        # 1) YouTube Music, rubrique « titres » : versions officielles (chaînes « - Topic »)
        ytm = self._flat("https://music.youtube.com/search?q="
                         + urllib.parse.quote(f"{artists} {t.title}") + "#songs", 5)
        for e in sorted(ytm, key=lambda e: -_similar(e.get("title", ""), t.title))[:3]:
            if _similar(e.get("title", ""), t.title) < 0.5 or _has_bad_word(e.get("title", ""), t.title):
                continue
            tried.add(e["id"])
            info = full(f"https://music.youtube.com/watch?v={e['id']}")
            if not info:
                continue
            yt_artists = " ".join(info.get("artists") or [info.get("artist") or info.get("channel") or ""])
            artist_ok = not t.artists or any(_norm(a) and _norm(a) in _norm(yt_artists) for a in t.artists)                 or _similar(yt_artists, artists) > 0.5
            if artist_ok and self._duration_ok(t, info.get("duration")):
                yield info

        # 2) YouTube classique, classé sur durée + titre + chaîne
        scored = []
        for e in self._flat(f"ytsearch8:{t.main_artist} - {t.title} audio", 8):
            title = e.get("title") or ""
            if e["id"] in tried or _has_bad_word(title, t.title) or not self._duration_ok(t, e.get("duration")):
                continue
            score = (1 - _similar(title, f"{t.main_artist} {t.title}")) * 30
            if e.get("duration") and t.duration_ms:
                score += abs(e["duration"] - t.duration_ms / 1000)
            if t.main_artist and _norm(t.main_artist) in _norm(e.get("channel") or e.get("uploader") or ""):
                score -= 8
            scored.append((score, e["id"]))
        for _, vid in sorted(scored)[:3]:
            info = full(f"https://www.youtube.com/watch?v={vid}")
            if info:
                yield info

    # ---------------------------------------------------------------- téléchargement

    def download(self, t: Track, out_dir: Path, progress: Progress) -> tuple[Path, str]:
        """Télécharge une piste en MP3 taggé. Renvoie (fichier, url source)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / track_filename(t)
        if dest.exists() and dest.stat().st_size > 0:
            progress("done", 1.0)
            return dest, ""

        with tempfile.TemporaryDirectory(prefix="pungi-") as tmp:
            def hook(d: dict) -> None:
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    if total:
                        progress("downloading", 0.1 + 0.7 * d.get("downloaded_bytes", 0) / total)

            opts = {**self.base_opts, "format": "bestaudio/best",
                    "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s"), "progress_hooks": [hook]}
            with yt_dlp.YoutubeDL(opts) as ydl:
                progress("searching", 0.05)
                info, last_error = None, None
                for attempt, cand in enumerate(self.candidates(t, ydl)):
                    if attempt >= 3:
                        break
                    progress("downloading", 0.1)
                    info, last_error = self._fetch_with_backoff(ydl, cand)
                    if info:
                        break
                if info is None:
                    raise last_error or NotFound("Aucune source audio correspondante trouvée.")
                src = Path(info["requested_downloads"][0]["filepath"])

            if not t.album and info.get("album"):
                t.album = info["album"]

            progress("converting", 0.85)
            part = Path(tmp) / "out.mp3"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-vn",
                            "-c:a", "libmp3lame", "-b:a", BITRATE, str(part)],
                           check=True, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._tag(part, t)
            part.replace(dest)

        progress("done", 1.0)
        return dest, info.get("webpage_url", "")

    @staticmethod
    def _fetch_with_backoff(ydl: yt_dlp.YoutubeDL, cand: dict) -> tuple[dict | None, Exception | None]:
        """YouTube renvoie des 403 quand on enchaîne les téléchargements : on patiente,
        on régénère les URLs du flux et on réessaie la même source."""
        for delay in (0, 3, 8):
            try:
                if delay:
                    time.sleep(delay + random.random() * 2)
                    cand = ydl.extract_info(cand["webpage_url"], download=False)
                return ydl.process_ie_result(cand, download=True), None
            except yt_dlp.utils.DownloadError as exc:
                if "403" not in str(exc):
                    return None, exc  # vidéo indisponible, etc. → source suivante
                last = exc
        return None, last

    @staticmethod
    def _tag(path: Path, t: Track) -> None:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=t.title))
        tags.add(TPE1(encoding=3, text=t.artists or [""]))
        tags.add(TPE2(encoding=3, text=t.album_artist or t.main_artist))
        if t.album:
            tags.add(TALB(encoding=3, text=t.album))
        if t.track_number:
            tags.add(TRCK(encoding=3, text=str(t.track_number)))
        if t.year:
            tags.add(TDRC(encoding=3, text=t.year))
        if t.cover_url:
            try:
                img = httpx.get(t.cover_url, headers={"User-Agent": USER_AGENT}, timeout=15)
                img.raise_for_status()
                tags.add(APIC(encoding=3, mime=img.headers.get("content-type", "image/jpeg"),
                              type=3, desc="Cover", data=img.content))
            except httpx.HTTPError:
                pass
        tags.save(path, v2_version=3)
