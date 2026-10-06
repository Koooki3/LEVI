"""Pool scans, exports and pushes as tracked worker processes.

An immutable plan in ``<workspace>/pool/jobs/<id>.json`` — for an export the
frozen selection — then a worker (``python -m levi.pool run-job`` from the
service, or this process when ``levi pool export`` runs it inline), started in
its own process group and registered in ``children.py`` so it never outlives
the service by accident.

**The worker owns its record.** It writes ``running`` with its process
identity, keeps a heartbeat (``<id>.beat.json``) and progress
(``<id>.progress.json``), and writes the final state before it exits, so the
outcome survives the service. States:

- ``planned``, ``running``; ``stalled`` (alive, but nothing moved for
  ``LEVI_POOL_STALL_SECONDS``); ``cancelling``;
- ``done`` (ok), ``done_with_errors`` (ok, some episodes failed and were left
  out: see ``errors.jsonl``);
- ``failed`` (a fatal error: ``error.json`` says what and how to fix it);
- ``cancelled``: the user asked to stop; the partial output is removed;
- ``interrupted``: the service stopped, the process was killed, the machine
  went down. Unlike a cancel, the ``.partial`` folder and its journal stay:
  ``resume`` continues from the finished units.

Cancel is told apart from a stop by a marker file written before the signal;
the shutdown path writes another (``<id>.stopping``) so the record says why.
"""

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import children
from ..catalog import atomic, read
from ..conversion import progress as progress_mod
from ..naming import timestamp_id
from ..paths import PROJECT
from . import joblog, settings

LOCK = threading.Lock()
ACTIVE: dict[str, subprocess.Popen] = {}
_THREADS: set[threading.Thread] = set()
TIMEOUT = 24 * 3600
ESCALATE_SECONDS = 15.0  # a cancelled worker that ignores SIGTERM is killed
GRACE = 30.0  # a launched job whose worker has not registered yet is alive

ALIASES = {"succeeded": "done", "queued": "running"}
LIVE = ("running", "stalled", "cancelling")
STOPPED = ("interrupted", "failed")  # what resume continues
FINISHED = ("done", "done_with_errors", "failed", "cancelled", "interrupted")


def jobs_dir() -> Path:
    return settings.pool_dir() / "jobs"


def _path(job_id: str) -> Path:
    return settings.job_path(job_id)


def normalize(status: str | None) -> str:
    return ALIASES.get(status or "", status or "planned")


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


def read_job(job_id: str) -> dict:
    """The stored record, as written (no derived state)."""
    job = read(_path(job_id), None)
    if not job:
        raise KeyError(job_id)
    return job


def discard(job_id: str) -> None:
    """Drop a plan that was only a dry run."""
    _path(job_id).unlink(missing_ok=True)


def execute(job: dict, progress_path: Path | None = None, resume: bool = False) -> dict:
    """Run a planned job in this process, without the worker's record keeping
    (tests, and the worker itself)."""
    if job["kind"] == "scan":
        from .scanner import scan

        return {"ok": True, "summary": scan(progress_path, rehash=job.get("rehash"))}
    if job["kind"] == "export":
        from .export import run

        return run(job, progress_path, resume=resume)
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


# ------------------------------------------------------------------ worker


def _terminated(signum, frame):
    # Unwind (``finally`` / ``except BaseException`` blocks clean up); a second
    # signal must not interrupt that cleanup.
    joblog.TERMINATING.set()
    signal.signal(signum, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def _install_signals() -> dict:
    """Route stop signals into the worker's cleanup; returns the handlers to
    restore (the CLI and tests run a worker inside a longer-lived process)."""
    joblog.TERMINATING.clear()
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous = {}
    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        previous[number] = signal.signal(number, _terminated)
    return previous


def _stop_reason(job_path: Path, code: int) -> str:
    files = joblog.paths(job_path)
    if files["cancel"].exists():
        return "cancelled"
    if files["stopping"].exists():
        return "service stopped"
    if code == 128 + signal.SIGINT:
        return "interrupted from the terminal (Ctrl+C)"
    if code == 128 + signal.SIGHUP:
        return "the terminal or session closed (SIGHUP)"
    return "stopped by a signal (SIGTERM)"


def _stage_of(path: Path) -> str:
    progress = read(joblog.paths(path)["progress"], {}) or {}
    return str(progress.get("stage") or "")


def _cleanup_after_cancel(job: dict) -> None:
    if job.get("kind") == "export":
        from . import cleanup

        cleanup.remove_partial(job)


def run_worker(path: Path) -> int:
    """Run the job at ``path`` in this process: the service's worker, or the
    CLI. Writes the record's states; returns the process exit code."""
    path = Path(path)
    previous = _install_signals()
    try:
        return _run_worker(path)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        joblog.TERMINATING.clear()


def _run_worker(path: Path) -> int:
    job = json.loads(path.read_text())
    files = joblog.paths(path)
    log = joblog.JobLog(files["log"])
    resume = bool(job.get("resume_requested"))
    now = time.time()
    job.update(
        status="running",
        worker={
            "pid": os.getpid(),
            "identity": children.identity(os.getpid()),
            "host": socket.gethostname(),
        },
    )
    job.setdefault("started_at", now)
    if resume:
        job["resumed_at"] = now
        job["resumes"] = int(job.get("resumes") or 0) + 1
    for key in ("error", "error_info", "interrupted_at", "reason", "finished_at"):
        job.pop(key, None)
    files["error"].unlink(missing_ok=True)
    files["cancel"].unlink(missing_ok=True)
    files["stopping"].unlink(missing_ok=True)
    files["result"].unlink(missing_ok=True)
    atomic(path, job)
    log.info(f"{'resuming' if resume else 'starting'} {job['kind']} job {job['id']}")
    code = 0
    with joblog.Heartbeat(path, activity=progress_mod.activity):
        try:
            result = execute(job, files["progress"], resume=resume)
            atomic(files["result"], result)
            job.update(
                status="done_with_errors" if result.get("errors") else "done",
                result=result,
                exit_code=0,
            )
            log.info(f"job {job['status']}")
        except (SystemExit, KeyboardInterrupt) as stop:
            code = stop.code if isinstance(stop, SystemExit) else 130
            code = code if isinstance(code, int) else 1
            reason = _stop_reason(path, code)
            if reason == "cancelled":
                _cleanup_after_cancel(job)
                job.update(status="cancelled", error="Cancelled", exit_code=code)
                log.info("cancelled")
            else:
                job.update(
                    status="interrupted",
                    interrupted_at=time.time(),
                    reason=reason,
                    error=f"Interrupted: {reason}. What finished is kept; resume continues.",
                    error_info=joblog.describe_exit(code),
                    exit_code=code,
                )
                log.warning(f"interrupted: {reason}")
        except Exception as exc:  # noqa: BLE001
            code = 1
            info = joblog.describe_exception(exc, _stage_of(path))
            joblog.write_error(path, info)
            atomic(files["result"], {"ok": False, "error": info["message"]})
            job.update(
                status="failed", error=info["message"], error_info=info, exit_code=1
            )
            log.trace(exc, info["stage"])
            print(f"ERROR: {exc}", flush=True)
        finally:
            job["finished_at"] = time.time()
            atomic(path, job)
    return code


def worker(path: Path) -> int:
    """``python -m levi.pool run-job <plan>``."""
    return run_worker(Path(path))


# ----------------------------------------------------------------- service


def launch(job_id: str, resume: bool = False) -> dict:
    """Start (or, with ``resume``, continue) a job in a worker process."""
    path = _path(job_id)
    with LOCK:
        job = read(path, None)
        if not job:
            raise ValueError("Pool job plan missing")
        status = normalize(job.get("status"))
        if resume:
            if status not in STOPPED:
                raise ValueError("Only an interrupted or failed job can resume")
        elif status != "planned":
            raise ValueError("Job already submitted or plan missing")
        job["status"] = "running"
        job["launched_at"] = time.time()
        job["resume_requested"] = resume
        job.pop("worker", None)  # the previous run's process is not this one
        atomic(path, job)
        for stale in ("cancel", "stopping", "beat"):
            joblog.paths(path)[stale].unlink(missing_ok=True)

    def work():
        files = joblog.paths(path)
        code = None
        try:
            with files["stdio"].open("w") as handle:
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
                    code = None
                    raise ValueError("Pool job timed out") from None
                finally:
                    children.untrack(proc.pid)
            _after_exit(path, code)
        except Exception as exc:  # noqa: BLE001
            _finish_abnormal(path, code, str(exc))
        finally:
            with LOCK:
                ACTIVE.pop(job["id"], None)

    def tracked():
        try:
            work()
        finally:
            _THREADS.discard(threading.current_thread())

    thread = threading.Thread(target=tracked, daemon=True)
    _THREADS.add(thread)
    thread.start()
    return job


def _after_exit(path: Path, code: int | None) -> None:
    """The worker exited: its own final record stands; one that never wrote
    it (killed, out of memory) is closed here."""
    job = read(path, {}) or {}
    if normalize(job.get("status")) in LIVE + ("planned",):
        _finish_abnormal(path, code, None)
    else:
        _sweep_quietly()


def _finish_abnormal(path: Path, code: int | None, message: str | None) -> None:
    """A worker that died without a final record: cancelled when asked to be,
    else interrupted (a signal took it) or failed (it exited on its own)."""
    job = read(path, {}) or {}
    files = joblog.paths(path)
    with LOCK:
        if normalize(job.get("status")) not in LIVE + ("planned",):
            return
        job["finished_at"] = time.time()
        job["exit_code"] = code
        if files["cancel"].exists():
            _cleanup_after_cancel(job)
            job.update(status="cancelled", error="Cancelled")
        else:
            info = joblog.describe_exit(code)
            if message:
                info = {**info, "message": message}
            reason = (
                "service stopped" if files["stopping"].exists() else info["message"]
            )
            if joblog.signal_of(code) or code is None:
                job.update(
                    status="interrupted",
                    interrupted_at=time.time(),
                    reason=reason,
                    error=f"Interrupted: {reason}. What finished is kept; resume continues.",
                    error_info=info,
                )
            else:
                job.update(status="failed", error=info["message"], error_info=info)
                joblog.write_error(path, {**info, "stage": _stage_of(path)})
        atomic(path, job)
    for key in ("cancel", "stopping"):
        files[key].unlink(missing_ok=True)
    _sweep_quietly()


def _sweep_quietly() -> None:
    with contextlib.suppress(Exception):  # housekeeping never fails a job
        from . import cleanup

        cleanup.sweep()


def wait_idle(timeout: float = 60.0) -> list:
    deadline = time.monotonic() + timeout
    for thread in list(_THREADS):
        thread.join(max(0.0, deadline - time.monotonic()))
    return [t for t in _THREADS if t.is_alive()]


# --------------------------------------------------------------- liveness


def worker_alive(job: dict) -> bool:
    """Whether the process that runs this job still exists (its identity is
    checked, so a reused PID does not count)."""
    proc = ACTIVE.get(job.get("id", ""))
    if proc is not None and proc.poll() is None:
        return True
    worker = job.get("worker") or {}
    if worker.get("pid") and worker.get("identity"):
        return children.identity(worker["pid"]) == worker["identity"]
    # Launched a moment ago and not yet registered.
    return time.time() - float(job.get("launched_at") or 0) < GRACE


def timing(job: dict, path: Path) -> dict:
    """Seconds since the worker's last heartbeat / progress write, and since
    its work last moved."""
    now = time.time()
    files = joblog.paths(path)
    beat = joblog.read_beat(path) or {}
    progress = read(files["progress"], {}) or {}
    last = max(
        float(beat.get("at") or 0),
        float(progress.get("updated_at") or 0),
        float(job.get("resumed_at") or job.get("started_at") or 0),
    )
    activity = max(
        float(beat.get("activity_at") or 0),
        float(progress.get("updated_at") or 0),
        float(job.get("resumed_at") or job.get("started_at") or 0),
    )
    return {
        "updated_at": last or None,
        "age_seconds": round(now - last, 1) if last else None,
        "idle_seconds": round(now - activity, 1) if activity else None,
    }


def effective_status(job: dict, path: Path) -> str:
    """The state as it is now: a stored ``running`` is ``interrupted`` when
    its worker is gone, ``cancelling`` after a cancel, ``stalled`` when alive
    but silent."""
    status = normalize(job.get("status"))
    if status not in ("running", "stalled", "cancelling"):
        return status
    if not worker_alive(job):
        return "interrupted"
    if joblog.paths(path)["cancel"].exists():
        return "cancelling"
    seen = timing(job, path)
    quiet = max(seen["age_seconds"] or 0, seen["idle_seconds"] or 0)
    return "stalled" if quiet > settings.stall_seconds() else "running"


def partial_of(job: dict) -> Path | None:
    if job.get("kind") != "export" or not job.get("target"):
        return None
    target = Path(job["target"])
    return target.parent / f".{target.name}.partial"


def resumable(job: dict, status: str) -> bool:
    if status not in STOPPED:
        return False
    kind = job.get("kind")
    if kind == "export":
        from . import journal

        partial = partial_of(job)
        return bool(partial and journal.Journal.exists(partial))
    return kind in ("push", "scan")


def _mark_dead(path: Path) -> None:
    """A stored ``running`` whose worker is gone becomes final now."""
    job = read(path, {}) or {}
    if normalize(job.get("status")) in LIVE and not worker_alive(job):
        _finish_abnormal(path, None, None)


def get(job_id: str) -> dict:
    path = _path(job_id)
    job = read(path, None)
    if not job:
        raise KeyError(job_id)
    if normalize(job.get("status")) in LIVE and not worker_alive(job):
        _mark_dead(path)
        job = read(path, None) or job
    status = effective_status(job, path)
    out = {**_brief(job), "status": status}
    files = joblog.paths(path)
    progress = files["progress"]
    if progress.exists():
        try:
            out["progress"] = read(progress, None)
        except ValueError:
            pass
    out.update(timing(job, path))
    out["resumable"] = resumable(job, status)
    out["rerunnable"] = status in (
        *STOPPED,
        "cancelled",
        "done_with_errors",
        "stalled",
    ) and job.get("kind") in ("export", "push", "scan")
    if job.get("kind") == "export" and (partial := partial_of(job)):
        out["partial"] = str(partial) if partial.exists() else None
    if status in STOPPED and not out.get("error_info"):
        out["error_info"] = joblog.read_error(path)
    if files["errors"].is_file():
        out["failures"] = len(joblog.read_failures(path))
    if files["log"].is_file():
        out["log_bytes"] = files["log"].stat().st_size
    return out


def log_tail(job_id: str, kilobytes: int = 64) -> str:
    path = _path(job_id)
    if not path.is_file():
        raise KeyError(job_id)
    return joblog.tail(path, kilobytes)


def error_report(job_id: str, kilobytes: int = 32) -> dict:
    """What to paste into a bug report: the job's state, its structured error,
    the failed episodes and the tail of its log."""
    path = _path(job_id)
    job = get(job_id)
    for key in ("episodes", "excluded", "recipe"):
        job.pop(key, None)
    return {
        "job": job,
        "error": joblog.read_error(path),
        "failures": joblog.read_failures(path)[:50],
        "log_tail": joblog.tail(path, kilobytes),
        "levi_commit": _commit(),
        "at": joblog.now_iso(),
    }


def _commit():
    try:
        from ..recap.jobs import _levi_commit

        return _levi_commit()
    except Exception:  # noqa: BLE001
        return None


def _brief(job: dict) -> dict:
    """A job without its frozen episode list (kept on disk)."""
    out = {
        k: v
        for k, v in job.items()
        if k not in ("episodes", "excluded", "bridge_records")
    }
    out["status"] = normalize(out.get("status"))
    if job.get("bridge_records"):
        out["planned_bridge_records"] = len(job["bridge_records"])
    if "episodes" in job:
        out["planned_episodes"] = len(job["episodes"])
        out["planned_excluded"] = len(job.get("excluded") or [])
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


# ---------------------------------------------------------- cancel / resume


def cancel(job_id: str) -> dict:
    """Stop a job for good. A running one gets SIGTERM to its group (the
    worker and, for a push, rsync) after a marker that tells it this is a
    cancel: an export removes its ``.partial`` folder on the way out; a push
    keeps what arrived (``--partial``). A job that already stopped
    (interrupted or failed) becomes cancelled and its partial is removed."""
    path = _path(job_id)
    job = read(path, None)
    if not job:
        raise KeyError(job_id)
    files = joblog.paths(path)
    status = effective_status(job, path)
    if status in ("running", "stalled", "cancelling"):
        files["cancel"].write_text(str(time.time()))
        with LOCK:
            proc = ACTIVE.get(job_id)
        group = proc.pid if proc is not None and proc.poll() is None else None
        worker = job.get("worker") or {}
        group = group or (
            worker.get("pid") if children.identity(worker.get("pid")) else None
        )
        if group is None:
            raise ValueError("The job is not running")
        try:
            os.killpg(os.getpgid(group), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(group, signal.SIGTERM)
        threading.Timer(ESCALATE_SECONDS, _escalate, (job_id, group)).start()
        return {**get(job_id), "status": "cancelling"}
    if status in STOPPED or status == "planned":
        _cleanup_after_cancel(job)
        job.update(status="cancelled", error="Cancelled", finished_at=time.time())
        atomic(path, job)
        _sweep_quietly()
        return get(job_id)
    raise ValueError("The job is not running")


def _escalate(job_id: str, group: int) -> None:
    """A cancelled worker still alive after ``ESCALATE_SECONDS`` is killed."""
    path = _path(job_id)
    job = read(path, {}) or {}
    if normalize(job.get("status")) in LIVE and worker_alive(job):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(group), signal.SIGKILL)


def verify_resume(job: dict) -> None:
    """Refuse (``ResumeRefused``, with the reason) unless the unfinished
    output can be trusted: same plan, same folder, sources unchanged,
    held-out lists as planned."""
    from . import export, journal

    if job.get("kind") != "export":
        return
    partial = partial_of(job)
    if not partial or not journal.Journal.exists(partial):
        raise journal.ResumeRefused(
            "There is no unfinished output to resume (it was removed, or the export "
            "never got as far as writing a journal); run it again"
        )
    try:
        settings.require_enabled()
        settings.check_export_target(job["target"], job["sources"], allow_partial=True)
        journal.Journal.check(partial, job)
        settings.require_heldout()
        episodes = job["episodes"]
        export.refuse_heldout(
            episodes,
            [Path(p) for p in job["pool_roots"]],
            [Path(p) for p in job["heldout_lists"]],
        )
        export.refuse_heldout_groups(episodes)
        export.refuse_removed(episodes)
        export._unchanged(episodes)
    except journal.ResumeRefused:
        raise
    except (ValueError, PermissionError) as exc:
        raise journal.ResumeRefused(f"{exc}") from exc


def prepare_resume(job_id: str) -> dict:
    """Check that a stopped job may continue and mark the record so the next
    worker resumes (the CLI, or ``resume``)."""
    path = _path(job_id)
    job = read(path, None)
    if not job:
        raise KeyError(job_id)
    status = effective_status(job, path)
    if status not in STOPPED:
        raise ValueError(
            f"Only an interrupted or failed job can resume (this one is {status})"
        )
    if job["kind"] == "push":
        from . import remote

        remote.check_source(job["source"])
    verify_resume(job)
    job["resume_requested"] = True
    atomic(path, job)
    return job


def resume(job_id: str) -> dict:
    """Continue an interrupted or failed job. An export continues from its
    journal; a push runs rsync again (``--partial`` keeps what arrived); a
    scan starts over as a new job (it is cheap and replaces the index
    atomically)."""
    job = prepare_resume(job_id)
    if job["kind"] == "scan":
        fresh = plan_scan(bool(job.get("rehash")))
        job["superseded_by"] = fresh["id"]
        atomic(_path(job_id), job)
        return launch(fresh["id"])
    return launch(job_id, resume=True)


def rerun(job_id: str) -> dict:
    """Plan the job again from what it saved (the recipe and options of an
    export, the source and target of a push) and start it. The unfinished
    output of the old export is removed first: this is the way out when a
    resume is refused."""
    path = _path(job_id)
    job = read(path, None)
    if not job:
        raise KeyError(job_id)
    status = effective_status(job, path)
    if status in ("running", "stalled", "cancelling", "planned"):
        raise ValueError("The job is still running; cancel it first")
    kind = job["kind"]
    if kind == "scan":
        fresh = plan_scan(bool(job.get("rehash")))
    elif kind == "push":
        fresh = plan_push(
            job["source"], job["remote"]["name"], bool(job.get("dry_run"))
        )
    else:
        from .export import ExportOptions
        from .recipe import Recipe

        options = ExportOptions.model_validate(job["options"])
        recipe = Recipe.model_validate(job["recipe"])
        _cleanup_after_cancel(job)
        fresh = plan_export(recipe, options)
    job["superseded_by"] = fresh["id"]
    atomic(path, job)
    return launch(fresh["id"])


# ---------------------------------------------------------------- service


def stop_workers(wait: float = 10.0):
    """The service is shutting down: tell each worker why (so its record says
    ``service stopped``), signal it, and give it a moment to write that."""
    with LOCK:
        running = list(ACTIVE.items())
    for job_id, proc in running:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                joblog.paths(_path(job_id))["stopping"].write_text(str(time.time()))
                os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + wait
    for _, proc in running:
        try:
            proc.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass


def recover_interrupted():
    """At service start: a job recorded as running whose worker is gone is
    ``interrupted`` (not failed), with the reason and the time; a cancel that
    never finished is finished. ``LEVI_POOL_AUTO_RESUME=1`` resumes the
    interrupted exports; the sweeper then removes what has expired."""
    folder = jobs_dir()
    if not folder.is_dir():
        return
    stopped = []
    for path in folder.glob("*.json"):
        if path.name.count(".") != 1:
            continue
        try:
            value = read(path, {})
        except ValueError:
            continue
        if normalize(value.get("status")) in LIVE and not worker_alive(value):
            _finish_abnormal(path, None, None)
            value = read(path, {}) or value
            if value.get("status") == "interrupted":
                value["reason"] = (
                    value["reason"]
                    if value.get("reason") == "service stopped"
                    else "service restarted (or the process was killed)"
                )
                value["error"] = (
                    "Interrupted: the service stopped or restarted while this job ran. "
                    "What finished is kept; resume continues."
                )
                atomic(path, value)
                stopped.append(value)
    _sweep_quietly()
    if settings.auto_resume():
        for value in stopped:
            if value.get("kind") != "export":
                continue
            try:
                resume(value["id"])
            except Exception as exc:  # noqa: BLE001
                joblog.JobLog(joblog.paths(_path(value["id"]))["log"]).error(
                    f"auto-resume refused: {exc}"
                )
