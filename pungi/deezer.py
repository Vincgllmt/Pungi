"""API publique Deezer, sans clé : recherche (titres, albums, artistes) et métadonnées
complètes d'un titre, d'un album ou de la discographie d'un artiste.

Sert aussi à la bibliothèque pour compléter les tags.
"""

from __future__ import annotations

import re
import threading
import time

import httpx

from .spotify import USER_AGENT, Collection, Track

_REF_RE = re.compile(r"^deezer:(track|album|artist):(\d+)$")
# types d'albums gardés pour une discographie (les compilations sont écartées)
_DISCOGRAPHY_TYPES = ("album", "ep", "single")


class DeezerError(Exception):
    pass


class Deezer:
    """Quota Deezer : 50 requêtes / 5 s par IP, on reste en dessous."""
    BASE = "https://api.deezer.com"
    MIN_INTERVAL = 0.12

    def __init__(self) -> None:
        self.http = httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
        self._rate_lock = threading.Lock()
        self._next = 0.0
        self._albums: dict[int, dict] = {}

    def get(self, path: str, **params) -> dict:
        url = path if path.startswith("http") else self.BASE + path
        for _ in range(5):
            with self._rate_lock:
                wait = self._next - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._next = time.monotonic() + self.MIN_INTERVAL
            data = self.http.get(url, params=params or None).raise_for_status().json()
            err = data.get("error") if isinstance(data, dict) else None
            if not err:
                return data
            if err.get("code") == 4:  # quota dépassé
                time.sleep(5)
                continue
            if err.get("code") == 800:
                raise DeezerError("Contenu Deezer introuvable.")
            raise DeezerError(err.get("message") or "Erreur Deezer")
        raise DeezerError("Trop de requêtes vers Deezer.")

    def pages(self, path: str, **params):
        page = self.get(path, **params)
        while page:
            yield from page.get("data") or []
            page = self.get(page["next"]) if page.get("next") else None

    def search(self, query: str, kind: str = "", limit: int = 10) -> list[dict]:
        return self.get(f"/search/{kind}" if kind else "/search", q=query, limit=limit).get("data") or []

    def track(self, track_id: int) -> dict:
        return self.get(f"/track/{track_id}")

    def album(self, album_id: int) -> dict:
        if album_id not in self._albums:
            self._albums[album_id] = self.get(f"/album/{album_id}")
        return self._albums[album_id]

    def artist(self, artist_id: int) -> dict:
        return self.get(f"/artist/{artist_id}")

    def artist_albums(self, artist_id: int) -> list[dict]:
        albums = [a for a in self.pages(f"/artist/{artist_id}/albums", limit=100)
                  if a.get("record_type") in _DISCOGRAPHY_TYPES]
        return sorted(albums, key=lambda a: a.get("release_date") or "", reverse=True)


client = Deezer()  # partagé : le quota est par IP


# --------------------------------------------------------------------------- recherche

def _album_summary(a: dict) -> dict:
    return {"id": a["id"], "title": a["title"], "artist": (a.get("artist") or {}).get("name", ""),
            "cover": a.get("cover_medium"), "type": a.get("record_type", "album"),
            "nb_tracks": a.get("nb_tracks"), "year": (a.get("release_date") or "")[:4] or None}


def search(query: str) -> dict:
    tracks = client.search(query, "track", 10)
    albums = client.search(query, "album", 8)
    artists = client.search(query, "artist", 5)
    return {
        "tracks": [{"id": t["id"], "title": t["title"], "artist": t["artist"]["name"],
                    "album": (t.get("album") or {}).get("title", ""),
                    "cover": (t.get("album") or {}).get("cover_small"),
                    "duration_ms": t.get("duration", 0) * 1000,
                    "explicit": t.get("explicit_lyrics", False)} for t in tracks],
        "albums": [_album_summary(a) for a in albums],
        "artists": [{"id": a["id"], "name": a["name"], "picture": a.get("picture_medium"),
                     "nb_album": a.get("nb_album"), "nb_fan": a.get("nb_fan")} for a in artists],
    }


def artist_page(artist_id: int) -> dict:
    ar = client.artist(artist_id)
    albums = [_album_summary(a | {"artist": ar}) for a in client.artist_albums(artist_id)]
    return {"id": ar["id"], "name": ar["name"], "picture": ar.get("picture_medium"), "albums": albums}


# --------------------------------------------------------------------------- collections

def is_ref(ref: str) -> bool:
    return bool(_REF_RE.match(ref.strip()))


def _album_tracks(al: dict) -> list[Track]:
    artist = (al.get("artist") or {}).get("name")
    tracks = []
    for t in client.pages(f"/album/{al['id']}/tracks", limit=500):
        tracks.append(Track(
            spotify_id="", title=t["title"], artists=[t["artist"]["name"]],
            duration_ms=t.get("duration", 0) * 1000, album=al["title"], album_artist=artist,
            cover_url=al.get("cover_xl") or al.get("cover_big"),
            year=(al.get("release_date") or "")[:4] or None,
            track_number=t.get("track_position"), explicit=t.get("explicit_lyrics", False)))
    return tracks


def fetch(ref: str) -> Collection:
    """`ref` est de la forme deezer:<track|album|artist>:<id>."""
    m = _REF_RE.match(ref.strip())
    if not m:
        raise DeezerError("Référence Deezer invalide.")
    kind, did = m.group(1), int(m.group(2))

    if kind == "track":
        t = client.track(did)
        al = client.album(t["album"]["id"])
        artists = [c["name"] for c in t.get("contributors") or []] or [t["artist"]["name"]]
        track = Track(
            spotify_id="", title=t["title"], artists=artists, duration_ms=t.get("duration", 0) * 1000,
            album=al["title"], album_artist=(al.get("artist") or {}).get("name"),
            cover_url=al.get("cover_xl") or al.get("cover_big"),
            year=(t.get("release_date") or al.get("release_date") or "")[:4] or None,
            track_number=t.get("track_position"), explicit=t.get("explicit_lyrics", False))
        return Collection("track", str(did), track.title, ", ".join(artists), track.cover_url, [track])

    if kind == "album":
        al = client.album(did)
        return Collection("album", str(did), al["title"], (al.get("artist") or {}).get("name", ""),
                          al.get("cover_xl") or al.get("cover_big"), _album_tracks(al))

    # artiste : discographie (albums, EP, singles), sans doublons de titre
    ar = client.artist(did)
    tracks, seen = [], set()
    # albums d'abord, du plus ancien au plus récent : la version gardée est celle de l'album original
    albums = sorted(client.artist_albums(did),
                    key=lambda a: (_DISCOGRAPHY_TYPES.index(a["record_type"]), a.get("release_date") or ""))
    for al in albums:
        for t in _album_tracks(al | {"artist": ar}):
            key = t.title.lower()
            if key not in seen:
                seen.add(key)
                tracks.append(t)
    return Collection("artist", str(did), ar["name"], "Discographie",
                      ar.get("picture_xl") or ar.get("picture_big"), tracks)
