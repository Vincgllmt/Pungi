# Pungi

Cherche un artiste, un album ou un titre, ou colle un lien Spotify (titre, album, playlist ou artiste) : Pungi récupère les morceaux en **MP3 320 kbps** avec tous les tags : titre, artistes, album, numéro de piste, année et pochette.

## Lancer

Double-clique sur `pungi.bat`. Au premier lancement, il crée l'environnement Python et installe les dépendances. Ensuite il ouvre http://127.0.0.1:8001.

Ou à la main :

```sh
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pungi            # --port 8001 --no-browser
```

Aucun outil externe n'est nécessaire : ffmpeg est fourni par `imageio-ffmpeg`. Node.js est utilisé par yt-dlp s'il est installé.

## Recherche

Taper du texte dans le champ lance une recherche sur Deezer (API publique, sans clé) : artistes, albums et titres. Chaque album ou titre a son bouton de téléchargement. Cliquer sur un artiste affiche ses sorties (albums, EP, singles) avec un bouton « Toute la discographie ».

Les métadonnées viennent alors de Deezer : album, artiste de l'album, numéro de piste, année et pochette 1000×1000 sont toujours remplis. La discographie complète d'un artiste est disponible sans clé API (compilations exclues, doublons de titre écartés en gardant la version de l'album original). Le téléchargement audio, lui, passe par YouTube comme pour un lien Spotify.

Coller un lien Spotify lance directement le téléchargement, comme avant.

## Importer ses données Spotify

Sur Spotify : Paramètres du compte → Confidentialité → « Télécharger vos données » (« Données de compte »). Dans la carte **Importer mes données**, choisis l'archive `.zip` reçue, ou les fichiers `.json` qu'elle contient. Pungi propose alors :

- les **titres likés** et chaque **playlist** (une case par source, un dossier par source) ;
- **les plus écoutés**, tirés de l'historique d'écoute (écoutes de 30 s ou plus), avec un seuil de 3, 5, 10 ou 20 écoutes ;
- les **albums sauvegardés** (un clic les télécharge) et les **artistes suivis** (un clic lance la recherche de leur discographie).

Ménager Spotify :

- Les titres sont lus dans les fichiers de l'export : **aucune requête à Spotify** pour les lister.
- Il leur manque durée, pochette, année et numéro de piste : Pungi les cherche sur **Deezer** au moment de télécharger chaque titre (environ 3 sur 4 y sont trouvés sur le même album). Spotify n'est interrogé que pour les autres, une requête à la fois.
- Toutes les requêtes Spotify passent par un limiteur commun (une toutes les 0,5 s). Si Spotify répond 429, tout s'arrête le temps demandé ; si l'attente dépasse 2 min, Pungi n'insiste pas et télécharge sans ces infos.

Les titres déjà présents dans le dossier de musique (même titre et même artiste, d'après les tags ou le nom du fichier) sont **ignorés** et marqués « Déjà là ». Un gros import peut être **annulé** (les titres en cours se terminent), puis repris.

L'export contient des données personnelles : le dossier `Spotify Account Data/` est ignoré par git.

## Fonctionnement

1. **Métadonnées Spotify** : elles viennent des pages « embed » publiques, donc sans aucune clé. Si `SPOTIFY_CLIENT_ID` et `SPOTIFY_CLIENT_SECRET` sont définis dans `.env`, Pungi passe par l'API officielle.
2. **Source audio** : Pungi cherche d'abord sur YouTube Music, dans la rubrique « titres » (versions officielles). Les candidats sont vérifiés sur le titre, l'artiste et la durée (±7 %). Si rien ne convient, il se rabat sur YouTube classique et écarte les versions live, remix, cover, etc.
3. **Téléchargement** : `yt-dlp` récupère la meilleure piste audio, ffmpeg la convertit en MP3 320k, puis mutagen écrit les tags ID3.
4. **Parallélisme** : 4 pistes à la fois (`PUNGI_CONCURRENCY`). En cas de 403 de YouTube (bridage), Pungi attend puis réessaie, et remet la piste en file une fois.

Les fichiers vont dans `downloads/<nom de l'album ou de la playlist>/`. On peut les récupérer un par un ou en `.zip` depuis la page. Un fichier déjà présent n'est pas re-téléchargé.

## Compléter les tags de la bibliothèque

La carte **Bibliothèque** de la page scanne tous les MP3 de `PUNGI_DOWNLOAD_DIR` (sous-dossiers compris) et cherche chaque titre sur Deezer (API publique, sans clé) d'après son titre, son artiste et sa durée. Pour chaque fichier, elle propose de remplir les tags **vides** : album, artiste de l'album, numéro de piste, disque, année, genre et pochette. On coche ce qu'on garde, puis « Appliquer ».

- Un tag déjà rempli n'est jamais modifié, et les fichiers ne sont ni renommés ni déplacés.
- Si le titre est trouvé sur un autre album que celui du fichier, seul le genre est proposé (pour ne pas mélanger piste, année ou pochette de deux éditions).
- Les fichiers dont la correspondance n'est pas fiable sont listés à part, sans proposition.

## Doublons

La carte **Doublons** repère les MP3 d'un même titre : titre identique (mentions « Remastered », « Album Version »… ignorées), au moins un artiste en commun et durée proche (±4 s ou 3 %). Les versions live, remix, instrumentales, etc. ne sont pas confondues avec l'original.

- Pour chaque titre en double, une version est **gardée** : celle d'un album plutôt que d'un single (hors compilation), puis le meilleur débit, la pochette et les tags les plus complets. Les autres sont cochées, on ajuste puis « Mettre de côté ».
- Un single est un fichier sans album, dont l'album porte le nom du titre, contient « Single » ou est noté piste 1/1. L'option « Compter comme doublon un single présent dans un album » (activée par défaut) peut être décochée pour ne pas les signaler.
- Rien n'est supprimé : les fichiers écartés sont déplacés dans `.doublons/` (même arborescence) à la racine du dossier, ignoré par les scans. Les dossiers devenus vides sont supprimés. Au moins un exemplaire de chaque titre reste toujours en place.

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
  deezer.py      recherche et métadonnées Deezer (aussi utilisé par la bibliothèque)
  downloader.py  recherche YouTube, téléchargement, conversion, tags
  jobs.py        file de téléchargements parallèles, ZIP
  library.py     scan de la bibliothèque et complétion des tags via Deezer
  duplicates.py  détection des doublons et mise de côté dans .doublons/
  app.py         API HTTP (FastAPI)
  static/        interface web
```

Usage personnel uniquement : respecte les droits d'auteur et les conditions d'utilisation de Spotify et YouTube.
