from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .jobs import DOWNLOAD_DIR, Job, JobManager
from .library import Busy, Library
from .spotify import SpotifyError

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Pungi")
manager = JobManager()
library = Library(DOWNLOAD_DIR)


class JobRequest(BaseModel):
    url: str


class ApplyRequest(BaseModel):
    paths: list[str]


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
    except SpotifyError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Impossible de lire ce lien Spotify : {exc}")
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs():
    return [j.to_dict() | {"tracks": []} for j in reversed(manager.jobs.values())]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job(job_id).to_dict()


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str):
    return {"retried": manager.retry_failed(_job(job_id))}


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
