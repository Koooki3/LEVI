"""Which long jobs are running right now (for ``levi stop``).

Every worker LEVI starts is recorded in ``children.py`` with its kind and
job id. The ones that are jobs (a pool scan/export/push, a conversion, a RECAP
run, a segmentation or SAM3 run) are what stopping the service would kill;
this lists them with their progress so a person can wait, or decide.
"""

import json
import time
from pathlib import Path

from . import children
from .paths import STATE

JOB_KINDS = ("pool", "conversion", "recap_value", "sam3")


def _is_job(kind: str) -> bool:
    return kind in JOB_KINDS or (
        kind.startswith("segmentation-") and kind != "segmentation-live"
    )


def _progress(kind: str, job_id: str) -> dict | None:
    if kind == "pool":
        from .pool import settings

        path = settings.pool_dir() / "jobs" / f"{job_id}.progress.json"
    elif kind == "conversion":
        path = STATE / "jobs" / f"{job_id}.progress.json"
    else:
        return None
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return {
        "stage": value.get("stage"),
        "done": value.get("done"),
        "total": value.get("total"),
        "eta_seconds": value.get("eta_seconds"),
        "elapsed_seconds": value.get("elapsed_seconds"),
    }


def running_jobs() -> list[dict]:
    """Running job workers: kind, id, since when, progress when known."""
    now = time.time()
    out = []
    for row in children.listed():
        if not row.get("running") or not _is_job(row["kind"]):
            continue
        out.append(
            {
                "kind": row["kind"],
                "id": row.get("label"),
                "pid": row["pid"],
                "running_seconds": round(now - float(row.get("started_at") or now)),
                "progress": _progress(row["kind"], row.get("label") or ""),
            }
        )
    return out


def describe(jobs: list[dict]) -> str:
    """The list as lines a person reads."""
    lines = []
    for job in jobs:
        progress = job.get("progress") or {}
        detail = ""
        if progress.get("total"):
            detail = f"  {progress.get('stage')}: {progress.get('done')}/{progress.get('total')}"
            if progress.get("eta_seconds") is not None:
                detail += f", about {round(progress['eta_seconds'] / 60)} min left"
        lines.append(f"  - {job['kind']} {job['id']} (pid {job['pid']}){detail}")
    return "\n".join(lines)
