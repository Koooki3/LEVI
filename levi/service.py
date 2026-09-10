"""Local HTTP service for datasets, review, conversion, diagnostics and annotations."""

import re
import os
import shutil
from contextlib import asynccontextmanager
from .conversion.options import Options
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from .paths import ROOT, STATE, CACHE, configure, inside
from .catalog import DEMOS, atomic, datasets, local_root, read, register, review_path
from . import jobs
from .auth import hub_token, token
from .diagnostics import CHECKS, diagnose

configure()
from backend.app import app as annotation_app  # noqa: E402


@asynccontextmanager
async def lifespan(app):
    jobs.recover_interrupted()
    marker = STATE / "server.pid"
    marker.write_text(str(os.getpid()))
    try:
        yield
    finally:
        jobs.stop_workers()
        if marker.exists() and marker.read_text() == str(os.getpid()):
            marker.unlink()


app = FastAPI(title="LEVI", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def local_request(request: Request, call_next):
    # Single-user localhost service. Reject browser cross-origin write attempts.
    origin = request.headers.get("origin")
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and origin
        and urlsplit(origin).hostname not in ("127.0.0.1", "localhost")
    ):
        return JSONResponse(
            {"detail": "Cross-origin writes are disabled"}, status_code=403
        )
    context = hub_token.set(request.cookies.get("hf_access_token"))
    try:
        return await call_next(request)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    finally:
        hub_token.reset(context)


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


class Register(BaseModel):
    path: str


class Review(BaseModel):
    repo_id: str
    flagged: list[int] = Field(default_factory=list, max_length=100000)
    notes: str = Field(default="", max_length=20000)


class JobPlan(BaseModel):
    stage: str
    source: str
    fps: int = Field(default=10, ge=1, le=240)
    source_fps: int = Field(default=30, ge=1, le=240)
    options: Options = Field(default_factory=Options)


class Diagnostic(BaseModel):
    repo_id: str
    max_episodes: int = Field(default=20, ge=0, le=10000)
    checks: list[str] = Field(default_factory=lambda: CHECKS.copy())
    decode_video: bool = False


@app.get("/api/levi/catalog")
def catalog():
    return {
        "demos": DEMOS,
        "local": list(datasets().values()),
        "workspace": str(ROOT),
        "conversion_available": bool(
            shutil.which("ffmpeg") and shutil.which("ffprobe")
        ),
        "conversion_engine": "levi.builtin.v1",
        "stages": jobs.STAGES,
    }


@app.post("/api/levi/catalog")
def add_dataset(payload: Register):
    return register(payload.path)


@app.api_route("/api/levi/files/{slug}/{path:path}", methods=["GET", "HEAD"])
def dataset_file(slug: str, path: str):
    root = local_root("local/" + slug)
    full = inside(path, root)
    if not path.startswith(
        ("meta/", "data/", "videos/", "images/")
    ) or full.suffix not in (
        ".json",
        ".jsonl",
        ".parquet",
        ".mp4",
        ".png",
        ".jpg",
        ".jpeg",
    ):
        raise HTTPException(403, "Not a dataset asset")
    if not full.is_file():
        raise HTTPException(404, "Asset not found")
    # Starlette FileResponse implements HEAD and byte Range requests.
    return FileResponse(full)


@app.get("/api/levi/review")
def get_review(repo_id: str):
    return read(review_path(repo_id), {"repo_id": repo_id, "flagged": [], "notes": ""})


@app.post("/api/levi/review")
def save_review(payload: Review):
    if any(ep < 0 for ep in payload.flagged):
        raise ValueError("Episode IDs must be non-negative")
    value = payload.model_dump()
    value["flagged"] = sorted(set(value["flagged"]))
    atomic(review_path(payload.repo_id), value)
    return value


@app.get("/api/levi/review/export")
def export_review(repo_id: str):
    value = get_review(repo_id)
    value["schema"] = "levi.review.v1"
    value["excluded_episode_ids"] = value["flagged"]
    return JSONResponse(
        value,
        headers={"Content-Disposition": 'attachment; filename="levi-review.json"'},
    )


@app.post("/api/levi/jobs/plan")
def plan(payload: JobPlan):
    job = jobs.plan(
        payload.stage,
        payload.source,
        payload.fps,
        payload.source_fps,
        payload.options.model_dump(),
    )
    atomic(STATE / "jobs" / (job["id"] + ".json"), job)
    return job


@app.post("/api/levi/jobs/{job_id}/run")
def run_job(job_id: str):
    if not re.fullmatch(r"[\w-]+", job_id):
        raise ValueError("Invalid job ID")
    job = read(STATE / "jobs" / (job_id + ".json"), None)
    if not job:
        raise HTTPException(404, "Plan not found")
    return jobs.launch(job)


@app.get("/api/levi/jobs")
def list_jobs():
    result = []
    paths = [p for p in (STATE / "jobs").glob("*.json") if p.name.count(".") == 1]
    for path in sorted(paths, reverse=True)[:50]:
        value = read(path, {})
        log = path.with_suffix(".log")
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 32000))
                value["log"] = stream.read().decode("utf-8", errors="replace")
        result.append(value)
    return result


@app.post("/api/levi/diagnostics")
def diagnostics(payload: Diagnostic):
    if set(payload.checks) - set(CHECKS):
        raise ValueError("Unknown diagnostic check")
    root = local_root(payload.repo_id)
    if root is None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", payload.repo_id):
            raise ValueError("Invalid Hub dataset ID")
        from huggingface_hub import snapshot_download

        root = CACHE / payload.repo_id.replace("/", "__")
        # Metadata plus parquet; video download is explicit and can be large.
        patterns = ["meta/**", "data/**/*.parquet"]
        if payload.decode_video:
            patterns.append("videos/**")
        snapshot_download(
            payload.repo_id,
            token=token(),
            repo_type="dataset",
            local_dir=root,
            allow_patterns=patterns,
        )
    report = diagnose(root, payload.max_episodes, payload.checks, payload.decode_video)
    report["repo_id"] = payload.repo_id
    atomic(
        STATE / "diagnostics" / (payload.repo_id.replace("/", "__") + ".json"), report
    )
    return report


app.mount("/annotations", annotation_app)
