@echo off
rem Premier lancement : environnement Python, dépendances et raccourcis « Pungi ».
rem Ensuite, le raccourci du bureau ou du menu Démarrer suffit (pas de console).
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
)
.venv\Scripts\python -c "import webview, yt_dlp, fastapi" 2>nul || (
  .venv\Scripts\python -m pip install -r requirements.txt
  .venv\Scripts\python -m pungi --shortcut
)
.venv\Scripts\python -m pungi %*
