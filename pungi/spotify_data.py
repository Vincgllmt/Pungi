"""Import des données de compte Spotify (Paramètres → Confidentialité → « Télécharger vos
données ») : titres likés, playlists, albums et artistes sauvegardés, historique d'écoute.

Les titres sont lus dans les fichiers JSON de l'export : aucune requête Spotify n'est faite
pour les lister. Il leur manque durée, pochette, année et numéro de piste, complétés au moment
du téléchargement (Deezer d'abord, voir jobs.py) pour ménager le quota Spotify.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field

from .spotify import Collection, Track

# une écoute compte à partir de 30 s, comme pour Spotify
MIN_PLAY_MS = 30_000
PLAY_THRESHOLDS = (3, 5, 10, 20)


class ExportError(Exception):
    pass


@dataclass
class Source:
    id: str
    kind: str  # liked | playlist
    name: str
    tracks: list[Track]


@dataclass
class Export:
    sources: list[Source] = field(default_factory=list)
    albums: list[dict] = field(default_factory=list)
    artists: list[dict] = field(default_factory=list)
    # (piste, nombre d'écoutes), de la plus écoutée à la moins écoutée
    history: list[tuple[Track, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sources": [{"id": s.id, "kind": s.kind, "name": s.name, "count": len(s.tracks)}
                        for s in self.sources],
            "history": {str(n): sum(plays >= n for _, plays in self.history) for n in PLAY_THRESHOLDS},
            "albums": self.albums,
            "artists": self.artists,
        }

    def collections(self, ids: list[str], min_plays: int = 0) -> list[Collection]:
        wanted = set(ids)
        out = [Collection("playlist", s.id, s.name, "Import Spotify", tracks=_copy(s.tracks))
               for s in self.sources if s.id in wanted]
        if "history" in wanted and min_plays > 0:
            tracks = [t for t, plays in self.history if plays >= min_plays]
            out.append(Collection("playlist", "history", "Les plus écoutés",
                                  f"Écoutés au moins {min_plays} fois", tracks=_copy(tracks)))
        return [c for c in out if c.tracks]


def _copy(tracks: list[Track]) -> list[Track]:
    # chaque job complète ses pistes de son côté
    return [Track(t.spotify_id, t.title, list(t.artists), album=t.album) for t in tracks]


def _track_id(uri: str | None) -> str | None:
    return uri.rsplit(":", 1)[-1] if uri and uri.startswith("spotify:track:") else None


def _unique(tracks: list[Track]) -> list[Track]:
    seen, out = set(), []
    for t in tracks:
        key = t.spotify_id or (t.title.lower(), t.main_artist.lower())
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def read_zip(data: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return {n: zf.read(n) for n in zf.namelist() if n.lower().endswith(".json")}
    except zipfile.BadZipFile as exc:
        raise ExportError("Archive .zip illisible.") from exc


def parse(files: dict[str, str | bytes]) -> Export:
    """Les fichiers sont reconnus à leur contenu, pas à leur nom (qui change selon les exports)."""
    export = Export()
    liked: list[Track] = []
    playlists: list[dict] = []
    plays: dict[tuple[str, str], list] = {}  # (artiste, titre) → [piste, écoutes]

    for name in sorted(files):
        try:
            data = json.loads(files[name])
        except (ValueError, UnicodeDecodeError):
            continue

        if isinstance(data, dict) and isinstance(data.get("tracks"), list):  # YourLibrary.json
            for t in data["tracks"]:
                if tid := _track_id(t.get("uri")):
                    liked.append(Track(tid, t.get("track") or "", [t["artist"]] if t.get("artist") else [],
                                       album=t.get("album") or None))
            export.albums += [{"name": a.get("album"), "artist": a.get("artist"), "uri": a["uri"]}
                              for a in data.get("albums") or [] if a.get("uri")]
            export.artists += [{"name": a["name"], "uri": a.get("uri")}
                               for a in data.get("artists") or [] if a.get("name")]

        elif isinstance(data, dict) and isinstance(data.get("playlists"), list):  # PlaylistN.json
            playlists += data["playlists"]

        elif isinstance(data, list) and data and isinstance(data[0], dict):
            for e in data:
                # historique simple (StreamingHistory_music_N.json) ou étendu (Streaming_History_Audio_*.json)
                title = e.get("trackName") or e.get("master_metadata_track_name")
                artist = e.get("artistName") or e.get("master_metadata_album_artist_name")
                ms = e.get("msPlayed", e.get("ms_played")) or 0
                if not title or not artist or ms < MIN_PLAY_MS or e.get("episodeName"):
                    continue
                key = (artist.lower(), title.lower())
                if key not in plays:
                    plays[key] = [Track(_track_id(e.get("spotify_track_uri")) or "", title, [artist],
                                        album=e.get("master_metadata_album_album_name")), 0]
                plays[key][1] += 1

    if liked:
        export.sources.append(Source("liked", "liked", "Titres likés", _unique(liked)))
    for i, pl in enumerate(playlists):
        tracks = []
        for it in pl.get("items") or []:
            t = it.get("track") or {}  # épisodes, fichiers locaux, livres audio : ignorés
            if tid := _track_id(t.get("trackUri")):
                tracks.append(Track(tid, t.get("trackName") or "", [t["artistName"]] if t.get("artistName") else [],
                                    album=t.get("albumName") or None))
        if tracks:
            export.sources.append(Source(f"playlist-{i}", "playlist", pl.get("name") or "Playlist", _unique(tracks)))
    export.history = sorted(((t, n) for t, n in plays.values()), key=lambda x: -x[1])

    if not export.sources and not export.history and not export.albums:
        raise ExportError("Aucune donnée exploitable : attendu YourLibrary.json, PlaylistN.json "
                          "ou StreamingHistory*.json de l'export Spotify.")
    return export
