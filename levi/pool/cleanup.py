"""What pool jobs leave behind, and how it goes away.

Inventory of everything an export, scan or push creates besides its result:

- the ``.<name>.partial`` folder of an export (staged files, the conversion
  ``.parts``, the resume journal) next to its target;
- a job's record, log, stdio, error files, heartbeat and progress in
  ``<workspace>/pool/jobs/``;
- half-written atomic-write temporaries (``*.tmp``, ``.index.*.parquet``).

Rules (``sweep``): a finished export leaves no partial (one found is
removed); a cancelled job's partial goes at once; an interrupted or failed
export keeps its partial for ``LEVI_POOL_PARTIAL_TTL`` (default 3 days) so it
can be resumed, then it goes; a finished job's log and files are kept for
``LEVI_POOL_JOB_TTL`` (default 30 days) and its record shrinks to a summary;
an old failed, cancelled or interrupted record goes with its files. A live
job's files are never touched (its worker's identity is checked), and neither
is a finished export. It runs at service start, after each job, and as
``levi pool clean [--dry-run] [--all-partials]``.
"""

import contextlib
import re
import shutil
import time
from pathlib import Path

from ..catalog import atomic, read
from . import joblog, journal, settings

STALE_TEMP = 3600.0
PARTIAL = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]*\.partial$")
SUMMARY_KEYS = (
    "id",
    "kind",
    "status",
    "options",
    "target",
    "recipe",
    "source",
    "destination",
    "planned_at",
    "started_at",
    "finished_at",
    "result",
    "error",
    "resumes",
)


def tree_bytes(path: Path) -> int:
    path = Path(path)
    if path.is_file() or path.is_symlink():
        with contextlib.suppress(OSError):
            return path.lstat().st_size
        return 0
    total = 0
    for item in path.rglob("*"):
        with contextlib.suppress(OSError):
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
    return total


def _records() -> list[tuple[Path, dict]]:
    folder = settings.pool_dir() / "jobs"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        if path.name.count(".") != 1:
            continue
        with contextlib.suppress(ValueError, OSError):
            value = read(path, None)
            if value:
                out.append((path, value))
    return out


def _live(job: dict) -> bool:
    from . import jobs

    return jobs.normalize(job.get("status")) in jobs.LIVE and jobs.worker_alive(job)


def _ended(job: dict, path: Path) -> float:
    """When the job stopped, for the age of what it left."""
    for key in ("finished_at", "interrupted_at", "planned_at"):
        if job.get(key):
            return float(job[key])
    return path.stat().st_mtime


def _partial_dirs() -> dict[Path, dict | None]:
    """Every ``.<name>.partial`` folder we know of: those of job records and
    those lying in the export folders."""
    found: dict[Path, dict | None] = {}
    for _path, job in _records():
        if job.get("kind") == "export" and job.get("target"):
            target = Path(job["target"])
            partial = target.parent / f".{target.name}.partial"
            if partial.is_dir():
                found[partial] = job
    roots = {settings.default_export_parent(), *settings.export_roots()}
    for root in roots:
        if not root.is_dir():
            continue
        for depth in ("*", "*/*"):
            for child in root.glob(depth):
                with contextlib.suppress(OSError):
                    if child.is_dir() and PARTIAL.match(child.name):
                        found.setdefault(child.resolve(), None)
    return found


def _inside_export_roots(path: Path) -> bool:
    roots = [*settings.export_roots(), settings.default_export_parent()]
    return settings.inside_any(path, [Path(r).resolve() for r in roots]) is not None


def disk(paths: list[Path] | None = None) -> list[dict]:
    """Free and total bytes of the volumes an export may write to."""
    volumes: dict[int, dict] = {}
    for path in paths or [*settings.export_roots(), settings.workspace()]:
        probe = Path(path)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        with contextlib.suppress(OSError):
            usage = shutil.disk_usage(probe)
            device = probe.stat().st_dev
            volumes.setdefault(
                device,
                {
                    "path": str(path),
                    "free_bytes": usage.free,
                    "total_bytes": usage.total,
                },
            )
    return list(volumes.values())


def inventory() -> dict:
    """What can be cleaned now, with sizes and ages (nothing is removed)."""
    now = time.time()
    ttl_partial, ttl_job = settings.partial_ttl_seconds(), settings.job_ttl_seconds()
    from . import jobs

    partials = []
    for partial, job in _partial_dirs().items():
        status = (
            jobs.effective_status(job, settings.job_path(job["id"])) if job else None
        )
        header = {}
        with contextlib.suppress(OSError, ValueError):
            header = read(partial / journal.HEADER, {}) or {}
        stopped = float(
            (job or {}).get("interrupted_at")
            or (job or {}).get("finished_at")
            or header.get("stopped_at")
            or header.get("updated_at")
            or partial.stat().st_mtime
        )
        live = status in jobs.LIVE
        partials.append(
            {
                "path": str(partial),
                "name": partial.name,
                "job": (job or {}).get("id"),
                "status": status,
                "live": live,
                "known": job is not None,
                "resumable": bool(
                    job and jobs.resumable(job, status or "") and not live
                ),
                "bytes": tree_bytes(partial),
                "age_seconds": round(now - stopped),
                "expires_in_seconds": None
                if live
                else max(0, round(stopped + ttl_partial - now)),
            }
        )
    old_jobs = []
    for path, job in _records():
        status = jobs.normalize(job.get("status"))
        if status in ("planned",) or _live(job):
            continue
        ended = _ended(job, path)
        size = sum(tree_bytes(p) for p in joblog.companions(path) if p.exists())
        old_jobs.append(
            {
                "id": job["id"],
                "kind": job.get("kind"),
                "status": status,
                "bytes": size,
                "age_seconds": round(now - ended),
                "expires_in_seconds": max(0, round(ended + ttl_job - now)),
            }
        )
    temps = [
        {"path": str(p), "bytes": tree_bytes(p), "age_seconds": round(now - _mtime(p))}
        for p in _stale_temps(now, 0.0)
    ]
    return {
        "partials": partials,
        "jobs": old_jobs,
        "temp": temps,
        "reclaimable_bytes": sum(
            p["bytes"]
            for p in partials
            if not p["live"] and p["expires_in_seconds"] == 0
        )
        + sum(t["bytes"] for t in temps),
        "removable_bytes": sum(p["bytes"] for p in partials if not p["live"])
        + sum(j["bytes"] for j in old_jobs if j["expires_in_seconds"] == 0)
        + sum(t["bytes"] for t in temps),
        "disk": disk(),
        "ttl": {"partial_seconds": ttl_partial, "job_seconds": ttl_job},
    }


def _mtime(path: Path) -> float:
    with contextlib.suppress(OSError):
        return path.stat().st_mtime
    return time.time()


def _stale_temps(now: float, age: float = STALE_TEMP) -> list[Path]:
    pool = settings.pool_dir()
    found = []
    for folder in (pool, pool / "jobs", pool / "recipes"):
        if not folder.is_dir():
            continue
        for child in folder.iterdir():
            if (
                child.name.endswith(".tmp")
                or re.match(r"^\.index\..*\.parquet$", child.name)
            ) and now - _mtime(child) >= max(age, STALE_TEMP):
                found.append(child)
    return found


def remove_partial(job_or_path) -> bool:
    """Remove one export's ``.partial`` folder (a job record, or the folder's
    path). Only a folder named ``.<name>.partial`` inside an export root, and
    never one whose worker is alive."""
    if isinstance(job_or_path, dict):
        target = job_or_path.get("target")
        if not target:
            return False
        partial = Path(target).parent / f".{Path(target).name}.partial"
        if _live(job_or_path):
            return False
    else:
        partial = Path(job_or_path)
    partial = partial.resolve() if partial.exists() else partial
    if not PARTIAL.match(partial.name) or not partial.is_dir():
        return False
    if partial.is_symlink() or not _inside_export_roots(partial):
        return False
    for _p, other in _records():
        if _live(other) and other.get("kind") == "export":
            t = Path(other.get("target") or "")
            if t.parent / f".{t.name}.partial" == partial:
                return False
    shutil.rmtree(partial, ignore_errors=True)
    return not partial.exists()


def _compact(path: Path, job: dict) -> None:
    summary = {k: job[k] for k in SUMMARY_KEYS if k in job}
    summary["compacted"] = True
    atomic(path, summary)
    for extra in joblog.companions(path):
        if extra != path:
            extra.unlink(missing_ok=True)


def sweep(*, dry_run: bool = False, all_partials: bool = False) -> dict:
    """Apply the rules above; returns what was (or, with ``dry_run``, would
    be) removed."""
    now = time.time()
    ttl_partial, ttl_job = settings.partial_ttl_seconds(), settings.job_ttl_seconds()
    from . import jobs

    removed: list[dict] = []
    freed = 0

    def take(kind: str, ident: str, size: int, why: str):
        nonlocal freed
        removed.append({"kind": kind, "id": ident, "bytes": size, "why": why})
        freed += size

    for partial, job in _partial_dirs().items():
        status = (
            jobs.effective_status(job, settings.job_path(job["id"])) if job else None
        )
        if status in jobs.LIVE:
            continue
        header = read(partial / journal.HEADER, {}) or {}
        stopped = float(
            (job or {}).get("interrupted_at")
            or (job or {}).get("finished_at")
            or header.get("stopped_at")
            or header.get("updated_at")
            or _mtime(partial)
        )
        if status in (*jobs.SUCCEEDED, "cancelled"):
            why = "the job finished or was cancelled"
        elif all_partials:
            why = "all partials requested"
        elif not job and not header:
            continue  # not ours to judge without a journal: only --all-partials
        elif now - stopped >= ttl_partial:
            why = "older than LEVI_POOL_PARTIAL_TTL"
        else:
            continue
        size = tree_bytes(partial)
        if dry_run or remove_partial(partial):
            take("partial", str(partial), size, why)
    for path, job in _records():
        status = jobs.normalize(job.get("status"))
        if status == "planned" or _live(job):
            continue
        if now - _ended(job, path) < ttl_job:
            continue
        size = sum(tree_bytes(p) for p in joblog.companions(path) if p.exists())
        if status in jobs.SUCCEEDED:
            if job.get("compacted") and not any(
                p.exists() for p in joblog.companions(path) if p != path
            ):
                continue
            take("job", job["id"], size, "older than LEVI_POOL_JOB_TTL (summary kept)")
            if not dry_run:
                _compact(path, job)
        else:
            take("job", job["id"], size, "older than LEVI_POOL_JOB_TTL")
            if not dry_run:
                for extra in joblog.companions(path):
                    extra.unlink(missing_ok=True)
    for temp in _stale_temps(now):
        size = tree_bytes(temp)
        take("temp", str(temp), size, "leftover of an interrupted write")
        if not dry_run:
            with contextlib.suppress(OSError):
                if temp.is_dir():
                    shutil.rmtree(temp)
                else:
                    temp.unlink()
    return {"dry_run": dry_run, "removed": removed, "bytes": freed}


def delete(paths: list[str] | None = None, jobs_: list[str] | None = None) -> dict:
    """The page's delete buttons: named partials and old jobs, refused when
    live. Only what the inventory lists can be deleted."""
    listed = inventory()
    partials = {p["path"]: p for p in listed["partials"]}
    old = {j["id"]: j for j in listed["jobs"]}
    removed, refused = [], []
    for path in paths or []:
        item = partials.get(str(Path(path)))
        if not item or item["live"] or not remove_partial(Path(path)):
            refused.append({"kind": "partial", "id": path})
        else:
            removed.append({"kind": "partial", "id": path, "bytes": item["bytes"]})
    for job_id in jobs_ or []:
        item = old.get(job_id)
        if not item:
            refused.append({"kind": "job", "id": job_id})
            continue
        job_path = settings.job_path(job_id)
        for extra in joblog.companions(job_path):
            with contextlib.suppress(OSError):
                if extra.exists():
                    extra.unlink()
        removed.append({"kind": "job", "id": job_id, "bytes": item["bytes"]})
    return {"removed": removed, "refused": refused}
