"""Pool scans and exports as tracked worker processes with progress.

The same pattern as conversion jobs (``levi/jobs.py``): an immutable plan in
``<workspace>/pool/jobs/<id>.json`` — for an export the frozen selection —
then a worker process (``python -m levi.pool run-job``) in its own process
group, registered in ``children.py`` so it never outlives the service. The
worker writes ``<id>.progress.json`` and ``<id>.result.json``.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import children
from ..catalog import atomic, read
from ..naming import timestamp_id
from ..paths import PROJECT
from . import settings

LOCK = threading.Lock()
ACTIVE: dict[str, subprocess.Popen] = {}
CANCELLED: set[str] = set()
_THREADS: set[threading.Thread] = set()
TIMEOUT = 24 * 3600


def jobs_dir() -> Path:
    return settings.pool_dir() / "jobs"


def _path(job_id: str) -> Path:
    if not job_id.replace("-", "").isalnum():
        raise ValueError("Invalid job ID")
    return jobs_dir() / f"{job_id}.json"


def plan_scan(rehash: bool = False) -> dict:
    roots = settings.require_enabled()
    job_id = timestamp_id(jobs_dir(), ".json", prefix="scan-")
    job = {
        "id": f"scan-{job_id}",
        "kind": "scan",
        "rehash": rehash,
        "pool_roots": [str(r) for r in roots],
        "heldout_lists": [str(p) for p in settings.heldout_files()],
        "heldout_disabled": settings.heldout_disabled(),
        "status": "planned",
        "planned_at": time.time(),
    }
    atomic(_path(job["id"]), job)
    return job


def plan_export(recipe, options) -> dict:
    from . import export

    settings.require_enabled()
    frozen = export.plan(recipe, options)
    job_id = timestamp_id(jobs_dir(), ".json", prefix="export-")
    job = {
        "id": f"export-{job_id}",
        "kind": "export",
        **frozen,
        "status": "planned",
        "planned_at": time.time(),
    }
    atomic(_path(job["id"]), job)
    return job


def plan_push(source: str, target_name: str, dry_run: bool = False) -> dict:
    """A push of a finished export to a registered remote target."""
    from . import remote

    folder = remote.check_source(source)
    target = remote.get(target_name)
    job_id = timestamp_id(jobs_dir(), ".json", prefix="push-")
    job = {
        "id": f"push-{job_id}",
        "kind": "push",
        "source": str(folder),
        "remote": target.model_dump(),
        "destination": remote.destination(target, folder),
        "dry_run": dry_run,
        "status": "planned",
        "planned_at": time.time(),
    }
    atomic(_path(job["id"]), job)
    return job


def discard(job_id: str) -> None:
    """Drop a plan that was only a dry run."""
    _path(job_id).unlink(missing_ok=True)


def execute(job: dict, progress_path: Path | None = None) -> dict:
    """Run a planned job in this process (the worker, or the CLI)."""
    if job["kind"] == "scan":
        from .scanner import scan

        return {"ok": True, "summary": scan(progress_path, rehash=job.get("rehash"))}
    if job["kind"] == "export":
        from .export import run

        return run(job, progress_path)
    if job["kind"] == "push":
        from . import remote

        return remote.push(
            Path(job["source"]),
            remote.Target.model_validate(job["remote"]),
            bool(job.get("dry_run")),
            progress_path,
        )
    raise ValueError(f"Unknown pool job kind: {job['kind']}")


def _environment(job: dict) -> dict:
    env = dict(os.environ)
    env["LEVI_POOL_ROOTS"] = ",".join(job.get("pool_roots") or [])
    env["LEVI_POOL_HELDOUT"] = ",".join(job.get("heldout_lists") or []) or (
        "none" if job.get("heldout_disabled") else ""
    )
    workspace = settings.workspace()
    env["LEVI_WORKSPACE"] = str(workspace)
    # Folders the worker's ``configure`` requires inside its workspace; one
    # inherited from another workspace would stop it before it starts.
    for name in ("LEVI_SAM3_CHECKPOINT_DIR", "LEVI_RECAP_VALUE_CHECKPOINT_DIR"):
        value = env.get(name)
        if value and not Path(value).resolve().is_relative_to(workspace):
            env.pop(name)
    return env


def launch(job_id: str) -> dict:
    path = _path(job_id)
    with LOCK:
        job = read(path, None)
        if not job or job.get("status") != "planned":
            raise ValueError("Job already submitted or plan missing")
        job["status"] = "running"
        job["started_at"] = time.time()
        atomic(path, job)

    def work():
        result_path = path.with_suffix(".result.json")
        log = path.with_suffix(".log")
        try:
            with log.open("w") as handle:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "levi.pool", "run-job", str(path)],
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    cwd=PROJECT,
                    env=_environment(job),
                    start_new_session=True,
                )
                with LOCK:
                    ACTIVE[job["id"]] = proc
                children.track(proc, "pool", job["id"])
                try:
                    code = proc.wait(timeout=TIMEOUT)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise ValueError("Pool job timed out") from None
                finally:
                    children.untrack(proc.pid)
            result = read(result_path, {}) if result_path.exists() else {}
            job["exit_code"] = code
            job["result"] = result
            job["status"] = "succeeded" if code == 0 and result.get("ok") else "failed"
            if job["id"] in CANCELLED:
                job["status"] = "cancelled"
                job["error"] = "Cancelled"
            elif job["status"] == "failed":
                job["error"] = result.get("error") or f"Worker exited with {code}"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = str(exc)
        finally:
            with LOCK:
                ACTIVE.pop(job["id"], None)
                CANCELLED.discard(job["id"])
            job["finished_at"] = time.time()
            atomic(path, job)

    def tracked():
        try:
            work()
        finally:
            _THREADS.discard(threading.current_thread())

    thread = threading.Thread(target=tracked, daemon=True)
    _THREADS.add(thread)
    thread.start()
    return job


def wait_idle(timeout: float = 60.0) -> list:
    deadline = time.monotonic() + timeout
    for thread in list(_THREADS):
        thread.join(max(0.0, deadline - time.monotonic()))
    return [t for t in _THREADS if t.is_alive()]


def cancel(job_id: str) -> dict:
    """Stop a running job: SIGTERM to its process group (the worker and, for
    a push, rsync). An export removes its ``.partial`` folder on the way out;
    a push keeps what arrived (``--partial``), so pushing again resumes."""
    path = _path(job_id)
    with LOCK:
        proc = ACTIVE.get(job_id)
        if proc is None or proc.poll() is not None:
            raise ValueError("The job is not running")
        CANCELLED.add(job_id)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    job = read(path, {})
    return {**_brief(job), "status": "cancelling"}


def _terminated(signum, frame):
    # Unwind (``finally`` / ``except BaseException`` blocks clean up).
    raise SystemExit(128 + signum)


def worker(path: Path) -> int:
    """``python -m levi.pool run-job <plan>``: the worker process."""
    signal.signal(signal.SIGTERM, _terminated)
    job = json.loads(Path(path).read_text())
    result_path = Path(path).with_suffix(".result.json")
    try:
        result = execute(job, Path(path).with_suffix(".progress.json"))
    except Exception as exc:  # noqa: BLE001
        atomic(result_path, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        print(f"ERROR: {exc}", flush=True)
        return 1
    atomic(result_path, result)
    return 0


def _brief(job: dict) -> dict:
    """A job without its frozen episode list (kept on disk)."""
    out = {k: v for k, v in job.items() if k not in ("episodes", "excluded")}
    if "episodes" in job:
        out["planned_episodes"] = len(job["episodes"])
        out["planned_excluded"] = len(job.get("excluded") or [])
    return out


def get(job_id: str) -> dict:
    path = _path(job_id)
    job = read(path, None)
    if not job:
        raise KeyError(job_id)
    out = _brief(job)
    progress = path.with_suffix(".progress.json")
    if progress.exists():
        try:
            out["progress"] = read(progress, None)
        except ValueError:
            pass
    return out


def listing(limit: int = 50) -> list[dict]:
    folder = jobs_dir()
    if not folder.is_dir():
        return []
    paths = [p for p in folder.glob("*.json") if p.name.count(".") == 1]
    out = []
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(get(path.stem))
        except (KeyError, ValueError):
            continue
    return out


def stop_workers():
    with LOCK:
        processes = list(ACTIVE.values())
    for proc in processes:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def recover_interrupted():
    folder = jobs_dir()
    if not folder.is_dir():
        return
    for path in folder.glob("*.json"):
        if path.name.count(".") != 1:
            continue
        try:
            value = read(path, {})
        except ValueError:
            continue
        if value.get("status") in ("running", "queued"):
            value.update(
                status="interrupted",
                error="Service restarted; an unfinished push resumes when started again"
                if value.get("kind") == "push"
                else "Service restarted; an unfinished export leaves only its "
                ".partial folder, which can be deleted",
            )
            atomic(path, value)
