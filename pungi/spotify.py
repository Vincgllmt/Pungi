"""Récupération des métadonnées Spotify (piste, album, playlist, artiste).

Deux sources :
- l'API Web officielle si SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET sont définis
  (pas de limite sur la taille des playlists, discographie complète d'un artiste) ;
- sinon les pages « embed » publiques, sans aucune clé
  (playlists limitées à 100 pistes, artiste = ses 10 titres populaires).

Toutes les requêtes passent par un limiteur commun : Spotify bride vite les rafales
(429 + Retry-After), et un blocage long ne doit pas figer les téléchargements.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
KINDS = ("track", "album", "playlist", "artist")

_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}(?:-[A-Za-z]{2})?/)?(?:embed/)?"
    r"(track|album|playlist|artist)/([A-Za-z0-9]{22})"
)
_URI_RE = re.compile(r"spotify:(track|album|playlist|artist):([A-Za-z0-9]{22})")
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


class SpotifyError(Exception):
    pass


class _Throttle:
    """Espace les requêtes Spotify (tous threads confondus) et applique les Retry-After."""
    MIN_INTERVAL = 0.5
    MAX_WAIT = 120  # au-delà, on abandonne plutôt que de bloquer les threads

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._next - time.monotonic()
            if delay > self.MAX_WAIT:
                raise SpotifyError(f"Spotify limite les requêtes : réessaie dans {int(delay / 60) + 1} min.")
            if delay > 0:
                time.sleep(delay)
            self._next = time.monotonic() + self.MIN_INTERVAL

    def retry_after(self, r: httpx.Response, attempt: int) -> None:
        """429 reçu : met en pause toutes les requêtes Spotify."""
        try:
            delay = float(r.headers.get("Retry-After", ""))
        except ValueError:
            delay = 2.0 * 2 ** attempt
        with self._lock:
            self._next = max(self._next, time.monotonic() + delay + 1)
        if delay > self.MAX_WAIT:
            raise SpotifyError(f"Spotify limite les requêtes : réessaie dans {int(delay / 60) + 1} min.")


_throttle = _Throttle()


@dataclass
class Track:
    spotify_id: str
    title: str
    artists: list[str]
    duration_ms: int = 0
    album: str | None = None
    album_artist: str | None = None
    cover_url: str | None = None
    year: str | None = None
    track_number: int | None = None
    explicit: bool = False

    @property
    def main_artist(self) -> str:
        return self.artists[0] if self.artists else ""


@dataclass
class Collection:
    kind: str
    spotify_id: str
    name: str
    subtitle: str = ""
    cover_url: str | None = None
    tracks: list[Track] = field(default_factory=list)
    truncated: bool = False  # True si la source embed a coupé la liste


def parse_url(url: str) -> tuple[str, str]:
    url = url.strip()
    if "spotify.link" in url or "spotify.app.link" in url:
        url = str(httpx.get(url, follow_redirects=True, timeout=15,
                            headers={"User-Agent": USER_AGENT}).url)
    m = _URL_RE.search(url) or _URI_RE.search(url)
    if not m:
        raise SpotifyError("Lien Spotify invalide (attendu : piste, album, playlist ou artiste).")
    return m.group(1), m.group(2)


def fetch(url: str) -> Collection:
    kind, sid = parse_url(url)
    if os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET"):
        try:
            return _Api().fetch(kind, sid)
        except Exception as exc:  # repli sur l'embed si l'API refuse
            print(f"[pungi] API Spotify indisponible ({exc}), repli sur l'embed.")
    return _fetch_embed(kind, sid)


# --------------------------------------------------------------------------- embed

def _embed_entity(kind: str, sid: str) -> dict:
    for attempt in range(4):
        _throttle.wait()
        r = httpx.get(f"https://open.spotify.com/embed/{kind}/{sid}",
                      headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20)
        if r.status_code != 429:
            break
        _throttle.retry_after(r, attempt)
    else:
        raise SpotifyError("Trop de requêtes vers Spotify.")
    if r.status_code == 404:
        raise SpotifyError("Contenu Spotify introuvable (privé ou supprimé ?).")
    r.raise_for_status()
    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        raise SpotifyError("Réponse Spotify inattendue (page embed sans données).")
    try:
        return json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError) as exc:
        raise SpotifyError("Contenu Spotify introuvable.") from exc


def _best_image(entity: dict) -> str | None:
    images = (entity.get("visualIdentity") or {}).get("image") or entity.get("coverArt", {}).get("sources") or []
    if not images:
        return None
    return max(images, key=lambda i: i.get("maxWidth") or i.get("width") or 0).get("url")


def _year(entity: dict) -> str | None:
    iso = (entity.get("releaseDate") or {}).get("isoString")
    return iso[:4] if iso else None


def _split_artists(subtitle: str) -> list[str]:
    return [a.strip() for a in re.split(r",\s*", subtitle.replace("\xa0", " ")) if a.strip()]


def enrich_from_embed(track: Track) -> None:
    """Complète pochette / année / durée d'une piste (utile pour playlists et artistes)."""
    ent = _embed_entity("track", track.spotify_id)
    track.duration_ms = track.duration_ms or ent.get("duration", 0)
    track.cover_url = track.cover_url or _best_image(ent)
    track.year = track.year or _year(ent)
    if ent.get("artists"):
        track.artists = [a["name"] for a in ent["artists"]]


def _fetch_embed(kind: str, sid: str) -> Collection:
    ent = _embed_entity(kind, sid)
    cover = _best_image(ent)

    if kind == "track":
        artists = [a["name"] for a in ent.get("artists", [])]
        t = Track(sid, ent.get("title") or ent["name"], artists, ent.get("duration", 0),
                  cover_url=cover, year=_year(ent), explicit=ent.get("isExplicit", False))
        return Collection("track", sid, t.title, ", ".join(artists), cover, [t])

    tracks = []
    for i, it in enumerate(ent.get("trackList") or [], 1):
        tid = it["uri"].rsplit(":", 1)[-1]
        t = Track(tid, it["title"], _split_artists(it.get("subtitle", "")), it.get("duration", 0),
                  explicit=it.get("isExplicit", False))
        if kind == "album":
            t.album, t.album_artist, t.cover_url, t.track_number = ent["name"], ent.get("subtitle"), cover, i
        tracks.append(t)

    if kind == "album" and tracks:
        try:  # l'année n'est exposée que sur la page d'une piste
            year = _year(_embed_entity("track", tracks[0].spotify_id))
            for t in tracks:
                t.year = year
        except Exception:
            pass

    subtitle = ent.get("subtitle", "")
    if kind == "artist":
        subtitle = "Titres populaires"
    return Collection(kind, sid, ent["name"], subtitle, cover, tracks,
                      truncated=(kind == "playlist" and len(tracks) >= 100) or kind == "artist")


# --------------------------------------------------------------------------- API

class _Api:
    BASE = "https://api.spotify.com/v1"
    _token: tuple[str, float] | None = None

    def __init__(self) -> None:
        self.http = httpx.Client(timeout=20)

    def _auth(self) -> dict:
        tok = _Api._token
        if not tok or tok[1] < time.time() + 30:
            _throttle.wait()
            r = self.http.post("https://accounts.spotify.com/api/token",
                               data={"grant_type": "client_credentials"},
                               auth=(os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]))
            r.raise_for_status()
            d = r.json()
            _Api._token = tok = (d["access_token"], time.time() + d["expires_in"])
        return {"Authorization": f"Bearer {tok[0]}"}

    def get(self, path: str, **params) -> dict:
        url = path if path.startswith("http") else self.BASE + path
        for attempt in range(5):
            headers = self._auth()
            _throttle.wait()
            r = self.http.get(url, params=params or None, headers=headers)
            if r.status_code == 429:
                _throttle.retry_after(r, attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise SpotifyError("Trop de requêtes vers l'API Spotify.")

    def pages(self, path: str, **params):
        page = self.get(path, **params)
        while page:
            yield from page.get("items", [])
            page = self.get(page["next"]) if page.get("next") else None

    @staticmethod
    def _track(t: dict, album: dict | None = None) -> Track:
        album = album or t.get("album") or {}
        images = album.get("images") or []
        return Track(
            spotify_id=t["id"], title=t["name"],
            artists=[a["name"] for a in t.get("artists", [])],
            duration_ms=t.get("duration_ms", 0),
            album=album.get("name"),
            album_artist=", ".join(a["name"] for a in album.get("artists", [])) or None,
            cover_url=images[0]["url"] if images else None,
            year=(album.get("release_date") or "")[:4] or None,
            track_number=t.get("track_number"),
            explicit=t.get("explicit", False),
        )

    def fetch(self, kind: str, sid: str) -> Collection:
        if kind == "track":
            t = self._track(self.get(f"/tracks/{sid}"))
            return Collection(kind, sid, t.title, ", ".join(t.artists), t.cover_url, [t])

        if kind == "album":
            al = self.get(f"/albums/{sid}")
            tracks = [self._track(t, al) for t in self.pages(f"/albums/{sid}/tracks", limit=50)]
            return Collection(kind, sid, al["name"], ", ".join(a["name"] for a in al["artists"]),
                              (al.get("images") or [{}])[0].get("url"), tracks)

        if kind == "playlist":
            pl = self.get(f"/playlists/{sid}", fields="name,owner(display_name),images")
            tracks = []
            for it in self.pages(f"/playlists/{sid}/tracks", limit=100):
                t = it.get("track") or it.get("item")
                if t and t.get("id") and t.get("type", "track") == "track":
                    tracks.append(self._track(t))
            return Collection(kind, sid, pl["name"], (pl.get("owner") or {}).get("display_name", ""),
                              (pl.get("images") or [{}])[0].get("url"), tracks)

        # artiste : discographie (albums + singles), sans doublons de titre
        ar = self.get(f"/artists/{sid}")
        tracks, seen = [], set()
        for al in self.pages(f"/artists/{sid}/albums", include_groups="album,single", limit=50):
            full = self.get(f"/albums/{al['id']}")
            for t in self.pages(f"/albums/{al['id']}/tracks", limit=50):
                key = (t["name"].lower(), tuple(a["name"] for a in t["artists"]))
                if key not in seen:
                    seen.add(key)
                    tracks.append(self._track(t, full))
        return Collection(kind, sid, ar["name"], "Discographie",
                          (ar.get("images") or [{}])[0].get("url"), tracks)
