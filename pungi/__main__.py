"""Lancement : python -m pungi [--port 8001] [--browser | --no-browser] [--shortcut] [--log]

Par défaut, Pungi s'ouvre dans sa propre fenêtre (pywebview) et fermer la fenêtre arrête
le serveur. --browser ouvre un onglet du navigateur à la place, --no-browser n'ouvre rien.
--shortcut crée les raccourcis « Pungi » : bureau et menu Démarrer sous Windows, menu des
applications (et bureau s'il existe) sous Linux.

Lancé par le raccourci, il n'y a pas de console : la sortie va dans pungi.log
(automatique avec pythonw sous Windows, --log sous Linux).

Sous Linux, la fenêtre a besoin de GTK + WebKit (paquets du système, d'où le venv créé avec
--system-site-packages par pungi.sh) ou de Qt ; sinon Pungi s'ouvre dans le navigateur.
"""

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
ICON = STATIC / ("pungi.ico" if sys.platform == "win32" else "pungi.png")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LINUX_HINT = ("pour une fenêtre, installe GTK et WebKit pour Python : "
              "sudo apt install python3-gi gir1.2-webkit2-4.1 (Debian, Ubuntu), "
              "sudo dnf install python3-gobject webkit2gtk4.1 (Fedora), "
              "sudo pacman -S python-gobject webkit2gtk-4.1 (Arch)")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def update_ytdlp(notify=print) -> None:
    """YouTube change souvent et seule la dernière version de yt-dlp suit : on la vérifie
    sur PyPI à chaque démarrage (une requête), et pip ne tourne que s'il y a plus récent."""
    try:
        from importlib.metadata import version

        import httpx

        latest = httpx.get("https://pypi.org/pypi/yt-dlp/json", timeout=5).json()["info"]["version"]
        current = version("yt-dlp")
        if _version(latest) <= _version(current):
            return
        notify(f"Mise à jour de yt-dlp {current} → {latest}…")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                        f"yt-dlp=={latest}"], check=True, capture_output=True, timeout=180,
                       creationflags=NO_WINDOW)
    except Exception as exc:
        print(f"Mise à jour de yt-dlp impossible ({exc}) : la version installée est gardée.")


def _data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.getenv("LOCALAPPDATA") or Path.home()) / "Pungi"
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "pungi"


def create_shortcuts() -> None:
    if sys.platform == "win32":
        _windows_shortcuts()
    elif sys.platform.startswith("linux"):
        _linux_shortcuts()
    else:
        print("Raccourcis non gérés sur ce système : lance ./pungi.sh.")


def _linux_shortcuts() -> None:
    """Entrée .desktop dans le menu des applications, copiée sur le bureau s'il existe."""
    entry = "\n".join([
        "[Desktop Entry]", "Type=Application", "Name=Pungi", "Comment=Musique en MP3",
        f'Exec="{sys.executable}" -m pungi --log', f"Path={ROOT}", f"Icon={ICON}",
        "Terminal=false", "Categories=AudioVideo;Audio;", "",
    ])
    apps = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
    targets = [apps / "pungi.desktop"]
    try:
        desktop = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True).stdout.strip()
    except OSError:
        desktop = ""
    if desktop and Path(desktop).is_dir() and Path(desktop) != Path.home():
        targets.append(Path(desktop) / "pungi.desktop")
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry, encoding="utf-8")
        target.chmod(0o755)
        if target.parent != apps:
            # GNOME n'ouvre un lanceur du bureau que s'il est marqué « de confiance »
            subprocess.run(["gio", "set", str(target), "metadata::trusted", "true"], capture_output=True)
        print(f"Raccourci créé : {target}")


def _windows_shortcuts() -> None:
    """Raccourcis vers pythonw (pas de console) avec l'icône de Pungi."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = f"""
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {{
  $lnk = $shell.CreateShortcut((Join-Path $dir 'Pungi.lnk'))
  $lnk.TargetPath = '{pythonw}'
  $lnk.Arguments = '-m pungi'
  $lnk.WorkingDirectory = '{ROOT}'
  $lnk.IconLocation = '{ICON},0'
  $lnk.Description = 'Pungi : musique en MP3'
  $lnk.Save()
  Write-Output $lnk.FullName
}}"""
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode:
        raise SystemExit(f"Création des raccourcis impossible : {r.stderr.strip()}")
    for line in r.stdout.splitlines():
        print(f"Raccourci créé : {line}")


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex((host, port)) != 0


def _is_pungi(url: str) -> bool:
    import httpx
    try:
        return "<title>Pungi</title>" in httpx.get(url, timeout=2).text
    except httpx.HTTPError:
        return False


def _free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


SPLASH = """<!doctype html><meta charset="utf-8"><style>
  body { margin: 0; height: 100vh; display: grid; place-content: center; text-align: center; gap: 6px;
    font: 15px/1.5 system-ui, "Segoe UI", sans-serif; background: #f6f5f2; color: #6d6b66; }
  h1 { margin: 0; font-size: 34px; letter-spacing: -.02em; color: #1b1b1a; }
  @media (prefers-color-scheme: dark) { body { background: #121211; color: #9d9b94; } h1 { color: #f0efea; } }
</style><h1>Pungi</h1><p>%s</p>"""


def _window_missing() -> str | None:
    """Raison pour laquelle pywebview ne pourra pas ouvrir de fenêtre (Linux), sinon None."""
    if not sys.platform.startswith("linux"):
        return None
    if not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
        return "pas d'affichage graphique"
    from importlib.util import find_spec
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        for webkit in ("4.1", "4.0"):
            try:
                gi.require_version("WebKit2", webkit)
                return None
            except ValueError:
                pass
    except (ImportError, ValueError):
        pass
    if find_spec("qtpy") and (find_spec("PyQt6") or find_spec("PySide6")):
        return None
    return LINUX_HINT


def run_window(url: str, boot=None) -> None:
    """Fenêtre Pungi. `boot(status)` prépare le serveur pendant que la fenêtre affiche
    un écran d'attente (`status(message)` le met à jour) ; sans `boot`, la fenêtre se
    branche directement sur un Pungi déjà lancé."""
    import html

    import webview

    if sys.platform == "win32":
        # sinon la barre des tâches regroupe la fenêtre avec Python et affiche son icône
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Pungi.App")

    webview.settings["ALLOW_DOWNLOADS"] = True  # liens « MP3 ↓ » et .zip
    window = webview.create_window("Pungi", url=None if boot else url,
                                   html=SPLASH % "Démarrage…" if boot else None,
                                   width=1000, height=880, min_size=(420, 480),
                                   background_color="#f6f5f2", text_select=True)

    def closing():
        if boot is None:
            return True
        from .app import manager
        if all(j.finished for j in manager.jobs.values()):
            return True
        return window.create_confirmation_dialog(
            "Quitter Pungi ?", "Des téléchargements sont en cours : ils seront interrompus.")

    def start():
        try:
            boot(lambda msg: (print(msg), window.load_html(SPLASH % html.escape(msg))))
            window.load_url(url)
        except Exception as exc:
            window.load_html(SPLASH % html.escape(f"Démarrage impossible : {exc}"))

    window.events.closing += closing
    storage = _data_dir() / "webview"
    # private_mode=False : la page garde ses préférences (localStorage) d'un lancement à l'autre
    webview.start(start if boot else None, icon=str(ICON), private_mode=False, storage_path=str(storage))


def stop(server) -> None:
    """Fenêtre fermée : on vide la file (sinon le processus, invisible, continuerait
    les téléchargements en attente) et on quitte sans attendre les pistes en cours."""
    from .app import manager
    for job in manager.jobs.values():
        manager.cancel(job)
    manager.pool.shutdown(wait=False, cancel_futures=True)
    server.should_exit = True
    time.sleep(1)
    os._exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pungi", description="Télécharge de la musique en MP3.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--browser", action="store_true", help="ouvrir dans le navigateur plutôt qu'une fenêtre")
    mode.add_argument("--no-browser", action="store_true", help="ne rien ouvrir (serveur seul)")
    parser.add_argument("--no-update", action="store_true", help="ne pas mettre à jour yt-dlp")
    parser.add_argument("--shortcut", action="store_true", help="créer les raccourcis puis quitter")
    parser.add_argument("--log", action="store_true", help="écrire la sortie dans pungi.log")
    args = parser.parse_args()

    if sys.stdout is None or args.log:  # pythonw (Windows) ou raccourci Linux : pas de console
        log = open(ROOT / "pungi.log", "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    else:
        # sortie redirigée vers un fichier : encodage Windows (cp1252) sans « → »
        sys.stdout.reconfigure(errors="replace")

    if args.shortcut:
        create_shortcuts()
        return

    os.chdir(ROOT)  # .env, downloads/ : relatifs au projet, d'où que vienne le lancement
    load_dotenv(ROOT / ".env")

    window = not args.browser and not args.no_browser
    if window:
        try:
            import webview  # noqa: F401
        except ImportError:
            print("pywebview absent (pip install -r requirements.txt) : ouverture dans le navigateur.")
            window, args.browser = False, True
    if window and (reason := _window_missing()):
        print(f"Pas de fenêtre ({reason}) : ouverture dans le navigateur.")
        window, args.browser = False, True

    url = f"http://{args.host}:{args.port}"
    if not _port_free(args.host, args.port):
        if _is_pungi(url):  # déjà lancé : on ouvre juste une fenêtre de plus dessus
            print(f"Pungi tourne déjà → {url}")
            if window:
                run_window(url)
            else:
                webbrowser.open(url)
            return
        args.port = _free_port(args.host)
        url = f"http://{args.host}:{args.port}"

    def boot(status=print) -> None:
        if not args.no_update:
            update_ytdlp(status)  # avant d'importer l'app, qui charge yt-dlp
        status("Démarrage…")
        print(f"Pungi → {url}")
        threading.Thread(target=server.run, daemon=True, name="pungi-server").start()
        for _ in range(600):  # l'import de l'app (yt-dlp…) prend quelques secondes
            if server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("le serveur ne répond pas, voir pungi.log")

    import uvicorn  # après le .env : la config est lue à l'import de l'app

    server = uvicorn.Server(uvicorn.Config("pungi.app:app", host=args.host, port=args.port,
                                           log_level="warning"))
    if not window:
        if not args.no_update:
            update_ytdlp()
        print(f"Pungi → {url}")
        if args.browser:
            threading.Timer(1.2, webbrowser.open, [url]).start()
        server.run()
        return

    try:
        run_window(url, boot)
    finally:
        stop(server)


if __name__ == "__main__":
    main()
