"""Local HTTP service for datasets, review, conversion, diagnostics and annotations."""

import os
import re
import shutil
from contextlib import asynccontextmanager
from html import escape
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import jobs
from .auth import hub_token, token
from .catalog import (
    DEMOS,
    aliases,
    atomic,
    canonical_id,
    datasets,
    display_name,
    local_root,
    read,
    register,
    review_path,
)
from .conversion import registry
from .conversion.options import Options
from .describe import describe
from .diagnostics import CHECKS, diagnose
from .paths import CACHE, PROJECT, ROOT, STATE, configure, inside

configure()
from backend.app import app as annotation_app


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


app = FastAPI(title="LEVI", version="0.3.0", lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def api_landing():
    # A fixed launcher-provided destination avoids trusting Host/forwarded headers.
    target = os.environ.get("LEVI_FRONTEND_URL", "http://127.0.0.1:7860")
    parsed = urlsplit(target)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        target = "http://127.0.0.1:7860"
    target = escape(target, quote=True)
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LEVI · API service</title><link rel="icon" href="/favicon.ico">
<style>body{{margin:0;background:#10211c;color:#eeeadd;font:17px/1.65 system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}}
main{{max-width:680px;margin:24px;padding:36px;border:1px solid #496153;border-radius:20px}}
a{{display:inline-block;background:#c4ec78;color:#10211c;padding:12px 20px;border-radius:10px;overflow-wrap:anywhere}}
code{{color:#c4ec78}}small{{color:#bdc9bc}}</style></head>
<body><main><small>LEVI / ROBOT DATA ATELIER</small>
<h1>This is the LEVI API service</h1><p>这是 LEVI 的内部 API 服务，不是数据集工作台。</p>
<p>Open the Web UI to browse, annotate and convert datasets:<br>请打开网页入口浏览、标注和转换数据：</p>
<a href="{target}">Open LEVI / 打开 LEVI · {target}</a>
<p>Start both services / 完整启动：<code>uv run levi</code></p>
<small>For remote access, forward the Web UI port, not the API port.<br>
远程访问请将网页端口转发到前端，而非 API 端口。</small>
</main></body></html>""")


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
def api_icon():
    return FileResponse(PROJECT / "src/app/icon.svg", media_type="image/svg+xml")


@app.get("/api/levi/health")
def health():
    return {"service": "levi-api", "status": "ok"}


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
    fps: float = Field(default=10, ge=1, le=240)
    source_fps: float = Field(default=30, ge=1, le=240)
    options: Options = Field(default_factory=Options)
    # Optional caller-chosen output directory (still confined to
    # LEVI_WORKSPACE); omit for `<source>_<target>_<timestamp>/` directly
    # under LEVI_WORKSPACE, like every other dataset.
    output: str | None = None


class Diagnostic(BaseModel):
    repo_id: str
    max_episodes: int = Field(default=20, ge=0, le=10000)
    checks: list[str] = Field(default_factory=lambda: CHECKS.copy())
    decode_video: bool = False


@app.get("/api/levi/catalog")
def catalog():
    return {
        "demos": DEMOS,
        # Each entry with what it is and what LEVI can do with it.
        "local": [{**item, "format": describe(item)} for item in datasets().values()],
        # Legacy hash ids → current names, so the UI can carry over state
        # (e.g. flagged episodes) stored under an old id.
        "aliases": aliases(),
        "workspace": str(ROOT),
        "conversion_available": bool(
            shutil.which("ffmpeg") and shutil.which("ffprobe")
        ),
        "conversion_engine": "levi.builtin.v1",
        "stages": jobs.STAGES,
    }


@app.post("/api/levi/catalog")
def add_dataset(payload: Register):
    """A LeRobot dataset is registered directly; a raw capture gets a
    browsing view built in the background (``view_status``)."""
    root = inside(payload.path)
    if (root / "meta/info.json").exists():
        return register(payload.path)
    from .views import request

    return request(root)


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
        # Only what the caller set, so the target's defaults fill the rest.
        payload.options.model_dump(exclude_unset=True),
        payload.output,
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
        try:
            value = read(path, {})
        except ValueError:
            continue  # a just-reserved id whose plan isn't written yet
        progress = path.with_suffix(".progress.json")
        if progress.exists():
            try:
                value["progress"] = read(progress, None)
            except ValueError:
                pass  # replaced atomically; a failed read is only a race
        log = path.with_suffix(".log")
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 32000))
                value["log"] = stream.read().decode("utf-8", errors="replace")
        result.append(value)
    return result


@app.get("/api/levi/convert/formats")
def convert_formats():
    """Supported inputs, outputs, their combinations, and known gaps."""
    return registry.capabilities()


class Inspect(BaseModel):
    source: str
    options: Options = Field(default_factory=Options)


@app.post("/api/levi/convert/inspect")
def convert_inspect(payload: Inspect):
    """Start an inspection job: format detection, the requirement checklist
    and per-target compatibility, with progress (hundreds of ffprobe calls
    on a large capture). Poll /api/levi/jobs for the result."""
    options = payload.options.model_dump(exclude_unset=True)
    job = jobs.plan(
        "inspect",
        payload.source,
        payload.options.fps,
        payload.options.source_fps,
        options,
    )
    atomic(STATE / "jobs" / (job["id"] + ".json"), job)
    return jobs.launch(job)


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
    report["repo_id"] = canonical_id(payload.repo_id)
    # One report per dataset, named after it and overwritten on re-run.
    slug = display_name(payload.repo_id, None)
    atomic(STATE / "diagnostics" / (slug + ".json"), report)
    return report


app.mount("/annotations", annotation_app)
