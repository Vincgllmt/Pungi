from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import deezer, spotify_data
from .deezer import DeezerError
from .duplicates import Duplicates
from .jobs import DOWNLOAD_DIR, Job, JobManager
from .library import Busy, Library
from .spotify import SpotifyError

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Pungi")
manager = JobManager()
library = Library(DOWNLOAD_DIR)
duplicates = Duplicates(DOWNLOAD_DIR)
export: spotify_data.Export | None = None  # dernier export Spotify importé


class JobRequest(BaseModel):
    url: str


class ApplyRequest(BaseModel):
    paths: list[str]


class ImportJobsRequest(BaseModel):
    sources: list[str]
    min_plays: int = 0


def _job(job_id: str) -> Job:
    job = manager.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Téléchargement inconnu.")
    return job


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/jobs")
async def create_job(req: JobRequest):
    try:
        job = await run_in_threadpool(manager.create, req.url)
    except (SpotifyError, DeezerError) as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Impossible de lire ce lien : {exc}")
    return job.to_dict()


@app.get("/api/search")
def search(q: str):
    if not q.strip():
        raise HTTPException(400, "Recherche vide.")
    try:
        return deezer.search(q.strip())
    except Exception as exc:
        raise HTTPException(502, f"Recherche Deezer impossible : {exc}")


@app.get("/api/artists/{artist_id}")
def artist(artist_id: int):
    try:
        return deezer.artist_page(artist_id)
    except DeezerError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Deezer injoignable : {exc}")


@app.get("/api/jobs")
def list_jobs():
    return [j.to_dict() | {"tracks": []} for j in reversed(manager.jobs.values())]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job(job_id).to_dict()


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str):
    return {"retried": manager.retry_failed(_job(job_id))}


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    return {"cancelled": manager.cancel(_job(job_id))}


@app.get("/api/jobs/{job_id}/tracks/{index}")
def get_track(job_id: str, index: int):
    job = _job(job_id)
    if not 0 <= index < len(job.tracks):
        raise HTTPException(404)
    state = job.tracks[index]
    if state.status != "done" or not state.file or not state.file.exists():
        raise HTTPException(409, "Piste pas encore prête.")
    return FileResponse(state.file, media_type="audio/mpeg", filename=state.file.name)


@app.get("/api/jobs/{job_id}/zip")
async def get_zip(job_id: str):
    job = _job(job_id)
    if not any(s.status == "done" for s in job.tracks):
        raise HTTPException(409, "Aucune piste prête.")
    path = await run_in_threadpool(manager.build_zip, job)
    return FileResponse(path, media_type="application/zip", filename=path.name.split(" [")[0] + ".zip")


@app.get("/api/library")
def get_library(items: bool = False):
    return library.to_dict(items)


@app.post("/api/library/scan")
def scan_library():
    try:
        library.scan()
    except Busy as exc:
        raise HTTPException(409, str(exc))
    return library.to_dict()


@app.post("/api/library/apply")
def apply_library(req: ApplyRequest):
    try:
        library.apply(req.paths)
    except Busy as exc:
        raise HTTPException(409, str(exc))
    return library.to_dict()


@app.get("/api/duplicates")
def get_duplicates(groups: bool = False):
    return duplicates.to_dict(groups)


@app.post("/api/duplicates/scan")
def scan_duplicates():
    try:
        duplicates.scan()
    except Busy as exc:
        raise HTTPException(409, str(exc))
    return duplicates.to_dict()


@app.post("/api/duplicates/move")
def move_duplicates(req: ApplyRequest):
    try:
        duplicates.move(req.paths)
    except Busy as exc:
        raise HTTPException(409, str(exc))
    return duplicates.to_dict()


@app.get("/api/import")
def get_import():
    return export.to_dict() if export else None


@app.post("/api/import")
async def upload_import(request: Request):
    """Corps : l'archive .zip de l'export, ou {"files": {nom: contenu}} pour des .json."""
    global export
    body = await request.body()
    try:
        if request.headers.get("content-type", "").startswith("application/zip"):
            files = spotify_data.read_zip(body)
        else:
            files = (await request.json()).get("files") or {}
        export = await run_in_threadpool(spotify_data.parse, files)
    except spotify_data.ExportError as exc:
        raise HTTPException(400, str(exc))
    except ValueError:
        raise HTTPException(400, "Fichiers illisibles.")
    return export.to_dict()


@app.post("/api/import/jobs")
def start_import(req: ImportJobsRequest):
    if not export:
        raise HTTPException(409, "Aucun export Spotify importé.")
    collections = export.collections(req.sources, req.min_plays)
    if not collections:
        raise HTTPException(400, "Rien à télécharger dans cette sélection.")
    manager.owned.reset()  # le dossier a pu changer depuis le dernier import
    return [manager.add(f"spotify-export:{c.spotify_id}", c, imported=True).to_dict() | {"tracks": []}
            for c in collections]
