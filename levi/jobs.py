"""Immutable plans for the bundled conversion worker; bounded concurrency."""

import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .catalog import atomic, read, register
from .conversion.engine import fingerprint
from .conversion.options import STAGES, Options
from .conversion.raw import check_tree
from .paths import PROJECT, ROOT, STATE, inside

LOCK = threading.Lock()
WORKERS = threading.BoundedSemaphore(2)
ACTIVE = {}


def plan(stage, source, fps=10, source_fps=30, options=None):
    if stage not in STAGES:
        raise ValueError("Unsupported conversion stage")
    source_path = inside(source)
    if not source_path.is_dir():
        raise ValueError("Input directory does not exist")
    check_tree(source_path)
    settings = Options.model_validate(
        {**(options or {}), "fps": fps, "source_fps": source_fps}
    )
    job_id = (
        datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    target = inside(ROOT / "datasets" / ("levi_" + job_id))
    if target.is_relative_to(source_path):
        raise ValueError("Output cannot be nested inside source")
    option_path = STATE / "jobs" / (job_id + ".options.json")
    result_path = STATE / "jobs" / (job_id + ".result.json")
    signature = fingerprint(source_path)
    argv = [
        sys.executable,
        "-m",
        "levi.conversion",
        stage,
        "--source",
        str(source_path),
        "--output",
        str(target),
        "--options",
        str(option_path),
        "--result",
        str(result_path),
        "--expected-source",
        signature,
    ]
    return {
        "id": job_id,
        "engine": "levi.builtin.v1",
        "stage": stage,
        "source": str(source_path),
        "source_fingerprint": signature,
        "output": str(target),
        "argv": argv,
        "options": settings.model_dump(),
        "status": "planned",
    }


def launch(job):
    path = STATE / "jobs" / (job["id"] + ".json")
    with LOCK:
        existing = read(path, None)
        if not existing or existing["status"] != "planned":
            raise ValueError("Job already submitted or plan missing")
        if job.get("engine") != "levi.builtin.v1":
            raise ValueError("Legacy external plan; create a new built-in plan")
        job["status"] = "queued"
        atomic(path, job)

    def run():
        with WORKERS:
            job["status"] = "running"
            atomic(path, job)
            try:
                source = inside(job["source"])
                check_tree(source)
                if fingerprint(source) != job["source_fingerprint"]:
                    raise ValueError(
                        "Capture changed after planning; create a new plan"
                    )
                atomic(STATE / "jobs" / (job["id"] + ".options.json"), job["options"])
                log = path.with_suffix(".log")
                with log.open("w") as handle:
                    proc = subprocess.Popen(
                        job["argv"],
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        cwd=PROJECT,
                        start_new_session=True,
                    )
                    with LOCK:
                        ACTIVE[job["id"]] = proc
                    try:
                        code = proc.wait(timeout=24 * 3600)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                        raise ValueError("Conversion timed out") from None
                job["exit_code"] = code
                result = read(STATE / "jobs" / (job["id"] + ".result.json"), {})
                job["result"] = result
                job["status"] = (
                    "succeeded" if code == 0 and result.get("ok") else "failed"
                )
                if job["status"] == "succeeded" and result.get("dataset_path"):
                    job["dataset"] = register(result["dataset_path"])["id"]
                    job["output"] = result["dataset_path"]
                job["output_exists"] = Path(job["output"]).is_dir()
            except Exception as exc:  # noqa: BLE001
                job["status"] = "failed"
                job["error"] = str(exc)
            finally:
                with LOCK:
                    ACTIVE.pop(job["id"], None)
                atomic(path, job)

    threading.Thread(target=run, daemon=True).start()
    return job


def stop_workers():
    with LOCK:
        processes = list(ACTIVE.values())
    for proc in processes:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for proc in processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def recover_interrupted():
    for path in (STATE / "jobs").glob("*.json"):
        value = read(path, {})
        if value.get("status") in ("running", "queued"):
            value.update(
                status="interrupted",
                error="Service restarted; inspect retained output before retrying",
            )
            atomic(path, value)
