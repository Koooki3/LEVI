"""Immutable plans for the bundled conversion worker; bounded concurrency."""

import contextlib
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .annotations.outcomes import labels_for_source
from .catalog import atomic, read, register
from .conversion import registry
from .conversion.engine import fingerprint
from .conversion.options import STAGES, Options
from .conversion.raw import check_tree
from .naming import catalog_name, timestamp_id
from .paths import PROJECT, ROOT, STATE, inside

LOCK = threading.Lock()
WORKERS = threading.BoundedSemaphore(2)
ACTIVE = {}


TARGET_LABELS = {"lerobot_v21": "lerobot", "recap_value": "recap"}


def output_label(stage: str, target: str = "lerobot_v21") -> str:
    """Short, readable name for what a stage produces, used in output dirs."""
    if stage == "pipeline":
        return TARGET_LABELS.get(target, target)
    return "lerobot" if stage == "convert" else stage


def plan(stage, source, fps=10, source_fps=30, options=None, output=None):
    if stage not in STAGES:
        raise ValueError("Unsupported conversion stage")
    source_path = inside(source)
    if not source_path.is_dir():
        raise ValueError("Input directory does not exist")
    check_tree(source_path)
    options = dict(options or {})
    if stage in ("pipeline", "inspect") and "outcome_labels" not in options:
        # Snapshot the human labels now: the plan stays reproducible even if
        # someone relabels while it runs.
        options["outcome_labels"] = labels_for_source(source_path)
    settings = Options.model_validate({**options, "fps": fps, "source_fps": source_fps})
    if stage == "pipeline":
        # The target's defaults (e.g. RECAP keeps every step) unless the
        # caller chose otherwise; its own options validated now, not mid-run.
        fmt = registry.detect(source_path)
        out = registry.output_format(settings.target)
        if fmt is None or fmt.id not in out.inputs:
            raise ValueError(
                f"{out.label} cannot be produced from "
                f"{fmt.label if fmt else 'an unrecognized folder'}"
            )
        settings = registry.with_defaults(settings, settings.target, set(options))
        settings = settings.model_copy(
            update={
                "target_options": out.target_options(
                    settings.target_options
                ).model_dump()
            }
        )
    # A timestamp, claimed by creating jobs/<id>.json (the service then
    # overwrites it with the plan) — unique even across LEVI processes.
    job_id = timestamp_id(STATE / "jobs", ".json")
    try:
        return _plan(job_id, stage, source_path, settings, output)
    except BaseException:
        # Release the reservation so no empty job record is left behind.
        (STATE / "jobs" / f"{job_id}.json").unlink(missing_ok=True)
        raise


def _plan(job_id, stage, source_path, settings, output):
    # A caller-chosen output directory, still confined to LEVI_WORKSPACE by
    # `inside()` — the CLI (`--output`) already allowed this; expose the same
    # freedom to the web UI/API instead of always auto-naming by job ID.
    # Auto-named runs land directly under LEVI_WORKSPACE as
    # `<source name>_<target>_<timestamp>/` — the same flat layout as every
    # registered dataset, readable, and never hash-suffixed.
    # `base=ROOT` is passed explicitly (not left to inside()'s own default
    # parameter, which is bound once when paths.py is first imported and
    # can't be redirected afterward) so tests can monkeypatch this module's
    # own `ROOT` to keep auto-named job outputs inside an isolated tmp_path
    # instead of the real, live LEVI_WORKSPACE.
    label = output_label(stage, settings.target)
    default = f"{catalog_name(source_path.name)}_{label}_{job_id}"
    target = inside(output or ROOT / default, base=ROOT)
    if target.is_relative_to(source_path):
        raise ValueError("Output cannot be nested inside source")
    if target.exists():
        raise ValueError(f"Output directory already exists: {target}")
    option_path = STATE / "jobs" / (job_id + ".options.json")
    result_path = STATE / "jobs" / (job_id + ".result.json")
    progress_path = STATE / "jobs" / (job_id + ".progress.json")
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
        "--progress",
        str(progress_path),
    ]
    if settings.keep_intermediates:
        argv += ["--intermediate", str(STATE / "jobs" / job_id / "intermediate")]
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
                if job["status"] == "succeeded" and job["stage"] == "view":
                    from .views import publish

                    entry = publish(Path(job["source"]), Path(result["dataset_path"]))
                    job["dataset"] = entry["id"]
                    job["output"] = entry["view"]
                elif job["status"] == "succeeded" and result.get("dataset_path"):
                    entry = register(result["dataset_path"])
                    job["dataset"] = entry["id"]
                    job["output"] = result["dataset_path"]
                    if job["stage"] == "pipeline":
                        job["carryover"] = carry_over_for(
                            Path(job["source"]), Path(job["output"]), entry["name"]
                        )
                if job["status"] == "failed" and job["stage"] == "view":
                    job.setdefault("error", result.get("error") or "View build failed")
                    _view_failed(job)
                job["output_exists"] = Path(job["output"]).is_dir()
            except Exception as exc:  # noqa: BLE001
                job["status"] = "failed"
                job["error"] = str(exc)
                if job["stage"] == "view":
                    _view_failed(job)
            finally:
                with LOCK:
                    ACTIVE.pop(job["id"], None)
                atomic(path, job)

    threading.Thread(target=run, daemon=True).start()
    return job


def carry_over_for(source: Path, output: Path, name: str) -> dict | None:
    """Annotations made on the source (a raw capture's view, or a dataset)
    follow into the new dataset; never fails the finished conversion."""
    from .annotations.carryover import carry_over
    from .catalog import datasets, name_for_path

    source_name = name_for_path(source)
    if not source_name:
        return None
    entry = datasets().get(source_name, {})
    browse = Path(entry.get("view") or entry.get("path") or source)
    try:
        return carry_over(source_name, browse, output, name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _view_failed(job):
    from .catalog import add_entry

    with contextlib.suppress(Exception):  # the job record keeps the error
        add_entry(
            Path(job["source"]),
            {
                "view_status": "failed",
                "view_error": job.get("error"),
                # Not retried by the sync until the capture changes again.
                "view_failed_fingerprint": job.get("source_fingerprint"),
            },
        )


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
    # Only the job records themselves (<id>.json), never their
    # .options/.result/.progress companions.
    for path in (STATE / "jobs").glob("*.json"):
        if path.name.count(".") != 1:
            continue
        value = read(path, {})
        if value.get("status") in ("running", "queued"):
            value.update(
                status="interrupted",
                error="Service restarted; inspect retained output before retrying",
            )
            atomic(path, value)
