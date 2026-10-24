"""Lancement : python -m pungi [--port 8001] [--no-browser]"""

import argparse
import os
import threading
import webbrowser
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="pungi", description="Télécharge un lien Spotify en MP3.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    load_dotenv(Path.cwd() / ".env")

    import uvicorn  # après le .env : la config est lue à l'import de l'app

    url = f"http://{args.host}:{args.port}"
    print(f"Pungi → {url}")
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, [url]).start()
    uvicorn.run("pungi.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
