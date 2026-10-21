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


def _hosted_jobs(workspace=None) -> list[dict]:
    """The terminal D that the marvel0 page hosts (a robot session), read from its
    run record on disk and never through the page's supervisor: this runs in
    ``levi stop`` and other processes, where taking a D over would signal it."""
    from .marvel0 import aeri_activity

    return aeri_activity.job_rows(workspace)


# Job sources that are not LEVI's own child workers. ``levi stop``, the core's
# stop route and the R6 restart read ``running_jobs``, so a robot session hosted
# by the page must be in it. A source that fails is listed as a job (not
# silently dropped); ``marvel0.aeri_activity.hosted_visible_to_activity`` refuses
# a start when this tuple has lost the source.
EXTRA_SOURCES = (_hosted_jobs,)


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


def running_jobs(workspace=None) -> list[dict]:
    """Running job workers: kind, id, since when, progress when known; then what
    the ``EXTRA_SOURCES`` add (``workspace``: the one they read, default the
    process-wide one)."""
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
    for source in EXTRA_SOURCES:
        out.extend(source(workspace))
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
        who = f" (pid {job['pid']})" if job.get("pid") else ""
        lines.append(f"  - {job['kind']} {job['id']}{who}{detail}")
    return "\n".join(lines)
