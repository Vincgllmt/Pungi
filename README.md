# Pungi

Colle un lien Spotify (titre, album, playlist ou artiste), Pungi récupère les morceaux en **MP3 320 kbps** avec tous les tags : titre, artistes, album, numéro de piste, année et pochette.

## Lancer

Double-clique sur `pungi.bat`. Au premier lancement, il crée l'environnement Python et installe les dépendances. Ensuite il ouvre http://127.0.0.1:8000.

Ou à la main :

```sh
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pungi            # --port 8000 --no-browser
```

Aucun outil externe n'est nécessaire : ffmpeg est fourni par `imageio-ffmpeg`. Node.js est utilisé par yt-dlp s'il est installé.

## Fonctionnement

1. **Métadonnées Spotify** : elles viennent des pages « embed » publiques, donc sans aucune clé. Si `SPOTIFY_CLIENT_ID` et `SPOTIFY_CLIENT_SECRET` sont définis dans `.env`, Pungi passe par l'API officielle.
2. **Source audio** : Pungi cherche d'abord sur YouTube Music, dans la rubrique « titres » (versions officielles). Les candidats sont vérifiés sur le titre, l'artiste et la durée (±7 %). Si rien ne convient, il se rabat sur YouTube classique et écarte les versions live, remix, cover, etc.
3. **Téléchargement** : `yt-dlp` récupère la meilleure piste audio, ffmpeg la convertit en MP3 320k, puis mutagen écrit les tags ID3.
4. **Parallélisme** : 4 pistes à la fois (`PUNGI_CONCURRENCY`). En cas de 403 de YouTube (bridage), Pungi attend puis réessaie, et remet la piste en file une fois.

Les fichiers vont dans `downloads/<nom de l'album ou de la playlist>/`. On peut les récupérer un par un ou en `.zip` depuis la page. Un fichier déjà présent n'est pas re-téléchargé.

## Limites

| Sans clés API | Avec clés API |
|---|---|
| Playlist : 100 premiers titres | Playlist complète |
| Artiste : 10 titres populaires | Discographie (albums + singles) |
| Pas de nom d'album pour les titres de playlist (repris de YouTube Music quand c'est possible) | Album complet |

Titres soumis à vérification d'âge : Pungi cherche d'abord une autre version non restreinte. Sinon, il utilise les cookies YouTube définis par `PUNGI_COOKIES_FILE` (fichier cookies.txt, fonctionne navigateur ouvert) ou par `PUNGI_COOKIES_BROWSER` (Firefox, ou un navigateur Chromium fermé ; Opera GX est détecté avec `opera`).

Configuration : voir `.env.example`.

## Structure

```
pungi/
  spotify.py     lecture des liens et métadonnées (embed / API)
  downloader.py  recherche YouTube, téléchargement, conversion, tags
  jobs.py        file de téléchargements parallèles, ZIP
  app.py         API HTTP (FastAPI)
  static/        interface web
```

Usage personnel uniquement : respecte les droits d'auteur et les conditions d'utilisation de Spotify et YouTube.
