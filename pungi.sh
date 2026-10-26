#!/bin/sh
# Équivalent Linux de pungi.bat. Premier lancement : environnement Python, dépendances
# et raccourci « Pungi » dans le menu des applications. Ensuite, le raccourci suffit.
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python

if [ -d .venv ] && [ ! -x "$PY" ]; then
  echo "Le dossier .venv vient d'un autre système (Windows ?) : supprime-le puis relance ./pungi.sh." >&2
  exit 1
fi
if [ ! -x "$PY" ]; then
  command -v python3 >/dev/null || { echo "Python 3 introuvable : installe python3 et python3-venv." >&2; exit 1; }
  # --system-site-packages : la fenêtre utilise GTK + WebKit du système (PyGObject
  # s'installe mal avec pip). Sans eux, Pungi s'ouvre dans le navigateur.
  python3 -m venv --system-site-packages .venv
fi
if ! "$PY" -c "import webview, yt_dlp, fastapi" 2>/dev/null; then
  "$PY" -m pip install -r requirements.txt
  "$PY" -m pungi --shortcut
fi
exec "$PY" -m pungi "$@"
