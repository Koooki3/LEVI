"""Clearing job records and deleting the exports they made.

Two actions per job, any state but running:

- **clear record**: removes the job's record, log, progress and error files.
  The exported dataset stays.
- **delete record and files**: also removes the export directory and the
  ``.partial`` leftover *of that job*.

The directory is only removed when every rule holds, checked again right
before the first file goes (nothing changes between check and deletion that
the check did not see):

1. it is the job's recorded target (or its ``.partial``), and the job owns it:
   the export's ``pool_export.json`` names the job that wrote it, a partial's
   journal likewise. A failed job never deletes the successful re-run's
   directory of the same name;
2. it carries a pool marker (``pool_export.json``, or the partial's
   ``resume.json``), and its file count matches what the export recorded;
   anything else means someone changed it: refused unless ``force``;
3. it lies inside ``LEVI_EXPORT_ROOTS`` or the workspace's export folder;
4. it is not inside, and does not contain, a pool source dataset or root;
5. it is not a symbolic link and resolves to its own name inside its parent
   (no traversal); symlinks inside it are unlinked, never followed;
6. no push of it and no other live job uses it.

Every removal is appended to ``<workspace>/pool/deleted.jsonl`` (time, job,
path, bytes, files, who and how); nothing else of a deleted export is kept.
"""

import contextlib
import getpass
import json
import os
import stat
import time
from pathlib import Path

from ..catalog import read
from . import cleanup, joblog, jobs, journal, settings

FILE_COUNT_SLACK = 0  # recorded episodes must all be there
_SIZES: dict[str, tuple[int, int, float]] = {}
SIZE_TTL = 600.0


class DeleteRefused(ValueError):
    """A deletion the safety rules do not allow (the message says which)."""


def deleted_log() -> Path:
    return settings.pool_dir() / "deleted.jsonl"


def _append_log(entry: dict) -> None:
    path = deleted_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": joblog.now_iso(), **entry}, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_deleted(limit: int = 200) -> list[dict]:
    path = deleted_log()
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def size_of(path: Path) -> int:
    """Bytes under ``path`` (``du``), cached while the folder's mtime holds."""
    path = Path(path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return 0
    cached = _SIZES.get(str(path))
    now = time.time()
    if cached and cached[0] == stamp and now - cached[2] < SIZE_TTL:
        return cached[1]
    total = cleanup.tree_bytes(path)
    _SIZES[str(path)] = (stamp, total, now)
    return total


def _records() -> list[tuple[Path, dict]]:
    return cleanup._records()


def _push_jobs_for(target: Path) -> list[dict]:
    out = []
    for _path, job in _records():
        if job.get("kind") == "push" and str(job.get("source")) == str(target):
            status = jobs.effective_status(job, settings.job_path(job["id"]))
            out.append(
                {
                    "job": job["id"],
                    "remote": (job.get("remote") or {}).get("name"),
                    "status": status,
                    "at": job.get("finished_at"),
                    "dry_run": bool(job.get("dry_run")),
                }
            )
    return out


def _sources() -> list[Path]:
    found = set(settings.pool_roots())
    with contextlib.suppress(Exception):
        from . import index

        found |= {Path(s["path"]) for s in index.sources(show_archive=True)}
    for _path, job in _records():
        found |= {Path(s) for s in job.get("sources") or []}
    return [Path(p).resolve() for p in found]


def _owner_of_output(target: Path, job: dict) -> str | None:
    """The job that wrote a finished export: its ``pool_export.json`` says;
    an older export that does not is taken to be the only done job that
    recorded it as its result."""
    record = read(target / "pool_export.json", None) or {}
    if record.get("job_id"):
        return record["job_id"]
    claimants = [
        j["id"]
        for _p, j in _records()
        if j.get("kind") == "export"
        and jobs.normalize(j.get("status")) in ("done", "done_with_errors")
        and (j.get("result") or {}).get("dataset_path") == str(target)
    ]
    return claimants[0] if len(claimants) == 1 else None


def _owner_of_partial(partial: Path) -> str | None:
    header = read(partial / journal.HEADER, None) or {}
    return header.get("job_id")


def _episode_count(target: Path, record: dict | None) -> int | None:
    if record and record.get("counts"):
        return record["counts"].get("episodes")
    return None


def _found_episodes(target: Path, record: dict) -> int | None:
    """How many episodes the folder holds now (to compare with the record)."""
    fmt = record.get("format")
    if fmt in ("lerobot_v21", "recap_value"):
        return len(list(target.glob("data/chunk-*/episode_*.parquet")))
    if fmt == "raw_capture":
        return len([p for p in target.glob("*/demo_*") if p.is_dir()])
    return None


def _inspect_dir(job: dict, role: str, path: Path) -> dict:
    """Everything the rules and the confirm dialog need about one directory."""
    job_id = job["id"]
    marker_name = "pool_export.json" if role == "output" else journal.HEADER
    info: dict = {
        "role": role,
        "path": str(path),
        "exists": path.is_dir() and not path.is_symlink(),
        "refusals": [],
        "needs_force": [],
        "owner": None,
        "owned": False,
        "pushed": [],
        "shared_with": [],
    }
    if path.is_symlink():
        info["exists"] = False
        info["symlink"] = True
        info["refusals"].append(
            "It is a symbolic link; LEVI never follows or deletes one"
        )
        return info
    if not path.is_dir():
        return info
    resolved = path.resolve()
    if resolved != path.parent.resolve() / path.name:
        info["refusals"].append("It does not resolve to its own name inside its folder")
    roots = [*settings.export_roots(), settings.default_export_parent()]
    if not settings.inside_any(resolved, [Path(r).resolve() for r in roots]):
        info["refusals"].append(
            "It lies outside LEVI_EXPORT_ROOTS and the workspace's export folder"
        )
    for source in _sources():
        if resolved == source or resolved.is_relative_to(source):
            info["refusals"].append(f"It lies inside the pool source {source}")
            break
        if source.is_relative_to(resolved):
            info["refusals"].append(f"It contains the pool source {source}")
            break
    expected = (
        Path(job["target"])
        if role == "output"
        else Path(job["target"]).parent / f".{Path(job['target']).name}.partial"
    )
    if path != expected:
        info["refusals"].append("It is not the job's recorded target")
    marker = path / marker_name
    info["marker"] = marker.is_file()
    record = read(marker, None) if role == "output" and marker.is_file() else None
    header = read(marker, None) if role == "partial" and marker.is_file() else None
    if role == "output":
        info["owner"] = _owner_of_output(path, job)
        info["format"] = (record or {}).get("format") or (job.get("options") or {}).get(
            "format"
        )
        info["episodes"] = _episode_count(path, record)
        info["created_at"] = (record or {}).get("created_at")
    else:
        info["owner"] = (header or {}).get("job_id")
        info["format"] = (job.get("options") or {}).get("format")
        info["episodes"] = None
        info["created_at"] = None
    info["owned"] = info["owner"] == job_id
    if not info["marker"]:
        info["needs_force"].append(
            f"It has no {marker_name}: something other than LEVI changed it"
        )
    elif role == "output" and record:
        found, wanted = _found_episodes(path, record), _episode_count(path, record)
        if (
            found is not None
            and wanted is not None
            and abs(found - wanted) > FILE_COUNT_SLACK
        ):
            info["needs_force"].append(
                f"It holds {found} episode(s) but the export recorded {wanted}: "
                "it was changed after the export"
            )
    info["bytes"] = size_of(path)
    if role == "output":
        info["pushed"] = _push_jobs_for(path)
        if any(p["status"] in jobs.LIVE for p in info["pushed"]):
            info["refusals"].append("A push of this export is running; cancel it first")
    for _p, other in _records():
        if other["id"] == job_id or other.get("kind") != "export":
            continue
        t = Path(other.get("target") or "")
        if (role == "output" and t == path) or (
            role == "partial" and t.parent / f".{t.name}.partial" == path
        ):
            info["shared_with"].append(other["id"])
            if (
                jobs._brief(other).get("status")
                and jobs.effective_status(other, settings.job_path(other["id"]))
                in jobs.LIVE
            ):
                info["refusals"].append(f"Another running job ({other['id']}) uses it")
    return info


def plan(job_id: str, files: bool = False) -> dict:
    """What clearing or deleting this job would do (nothing is changed)."""
    path = settings.job_path(job_id)
    job = read(path, None)
    if not job:
        raise KeyError(job_id)
    status = jobs.effective_status(job, path)
    companions = [p for p in joblog.companions(path) if p.exists()]
    out = {
        "id": job_id,
        "kind": job.get("kind"),
        "status": status,
        "running": status in jobs.LIVE,
        "record_files": len(companions),
        "record_bytes": sum(cleanup.tree_bytes(p) for p in companions),
        "outputs": [],
        "files": files,
        "refused": None,
        "needs_force": False,
        "freed_bytes": 0,
    }
    if out["running"]:
        out["refused"] = "The job is running; cancel it first"
        return out
    if job.get("kind") == "export" and job.get("target"):
        target = Path(job["target"])
        for role, directory in (
            ("output", target),
            ("partial", target.parent / f".{target.name}.partial"),
        ):
            if directory.is_dir() or directory.is_symlink():
                item = _inspect_dir(job, role, directory)
                item["will_delete"] = bool(
                    files and item["exists"] and item["owned"] and not item["refusals"]
                )
                item["kept_because"] = (
                    None
                    if item["will_delete"] or not files
                    else (
                        f"It belongs to job {item['owner']}, not to this one"
                        if item["exists"] and not item["owned"] and not item["refusals"]
                        else "; ".join(item["refusals"]) or None
                    )
                )
                out["outputs"].append(item)
    hard = [
        f"{i['path']}: {r}"
        for i in out["outputs"]
        if files and (i["owned"] or i.get("symlink"))
        for r in i["refusals"]
    ]
    if hard:
        out["refused"] = "; ".join(hard)
    out["needs_force"] = any(
        i["needs_force"] and i["will_delete"] for i in out["outputs"]
    )
    out["freed_bytes"] = out["record_bytes"] + sum(
        i["bytes"] for i in out["outputs"] if i["will_delete"]
    )
    return out


def _remove_tree(root: Path) -> dict:
    """Remove ``root`` and everything in it, never following a symlink,
    collecting errors instead of stopping at the first."""
    if root.is_symlink() or not root.is_dir():
        raise DeleteRefused(f"{root} is not a plain directory any more")
    size = count = 0
    errors: list[str] = []
    for current, dirs, names in os.walk(root, topdown=False, followlinks=False):
        for name in names:
            full = os.path.join(current, name)
            try:
                st = os.lstat(full)
                os.unlink(full)
                count += 1
                if not stat.S_ISLNK(st.st_mode):
                    size += st.st_size
            except OSError as exc:
                errors.append(f"{full}: {exc}")
        for name in dirs:
            full = os.path.join(current, name)
            try:
                if os.path.islink(full):
                    os.unlink(full)
                else:
                    os.rmdir(full)
            except OSError as exc:
                errors.append(f"{full}: {exc}")
    try:
        os.rmdir(root)
    except OSError as exc:
        errors.append(f"{root}: {exc}")
    _SIZES.pop(str(root), None)
    return {"bytes": size, "files": count, "errors": errors}


def delete(
    job_id: str,
    files: bool = False,
    force: bool = False,
    how: str = "api",
    only_partials: bool = False,
) -> dict:
    """Clear a job's record; with ``files`` also the directories it owns.
    Refused (``DeleteRefused``) while the job runs, when a rule forbids a
    directory it owns, or when the folder was changed and ``force`` is not
    given. Returns what was removed and the bytes freed."""
    wanted = plan(job_id, files)
    if wanted["refused"]:
        raise DeleteRefused(wanted["refused"])
    if wanted["needs_force"] and not force:
        raise DeleteRefused(
            "Changed since the export: "
            + "; ".join(
                f"{i['path']}: {n}"
                for i in wanted["outputs"]
                if i["will_delete"]
                for n in i["needs_force"]
            )
            + ". Delete it anyway with force"
        )
    who = f"{getpass.getuser()} via {how}"
    removed, errors, freed = [], [], 0
    for item in wanted["outputs"]:
        if not item["will_delete"]:
            continue
        if only_partials and item["role"] != "partial":
            continue
        path = Path(item["path"])
        # Look again right before deleting: what the plan saw may have changed.
        job = read(settings.job_path(job_id), {}) or {}
        again = _inspect_dir(job, item["role"], path)
        if not again["exists"] or again["refusals"] or not again["owned"]:
            errors.append(
                f"{path}: changed while deleting ({'; '.join(again['refusals']) or 'no longer this job’s'})"
            )
            continue
        if again["needs_force"] and not force:
            errors.append(f"{path}: changed while deleting: {again['needs_force'][0]}")
            continue
        result = _remove_tree(path)
        errors += result["errors"]
        freed += result["bytes"]
        removed.append({"role": item["role"], "path": str(path), **result})
        _append_log(
            {
                "job_id": job_id,
                "what": item["role"],
                "path": str(path),
                "bytes": result["bytes"],
                "files": result["files"],
                "forced": bool(force and item["needs_force"]),
                "errors": result["errors"][:20],
                "by": who,
            }
        )
    record_cleared = False
    if not errors:
        record_path = settings.job_path(job_id)
        companions = [p for p in joblog.companions(record_path) if p.exists()]
        record_bytes = sum(cleanup.tree_bytes(p) for p in companions)
        for extra in companions:
            with contextlib.suppress(OSError):
                extra.unlink()
        record_cleared = not record_path.exists()
        freed += record_bytes
        _append_log(
            {
                "job_id": job_id,
                "what": "record",
                "path": str(record_path),
                "bytes": record_bytes,
                "files": len(companions),
                "forced": False,
                "errors": [],
                "by": who,
            }
        )
    return {
        "id": job_id,
        "record_cleared": record_cleared,
        "removed": removed,
        "kept": [
            {"path": i["path"], "why": i["kept_because"]}
            for i in wanted["outputs"]
            if files and not i["will_delete"] and i.get("kept_because")
        ],
        "errors": errors,
        "freed_bytes": freed,
    }


def delete_many(
    ids: list[str], files: bool = False, force: bool = False, how: str = "api"
) -> dict:
    results, refused = [], []
    for job_id in ids:
        try:
            results.append(delete(job_id, files, force, how))
        except (DeleteRefused, KeyError) as exc:
            refused.append({"id": job_id, "reason": str(exc)})
    return {
        "results": results,
        "refused": refused,
        "freed_bytes": sum(r["freed_bytes"] for r in results),
    }


STOPPED_STATES = ("failed", "interrupted", "cancelled")


def clear_failed(how: str = "api") -> dict:
    """Remove the records of failed, interrupted and cancelled jobs and what
    they left behind (their ``.partial`` folders). Done exports and running
    jobs are never touched."""
    ids = []
    for path, job in _records():
        status = jobs.effective_status(job, path)
        if status in STOPPED_STATES:
            ids.append(job["id"])
    results, refused = [], []
    for job_id in ids:
        try:
            results.append(delete(job_id, True, False, how, only_partials=True))
        except (DeleteRefused, KeyError) as exc:
            refused.append({"id": job_id, "reason": str(exc)})
    return {
        "results": results,
        "refused": refused,
        "cleared": sum(1 for r in results if r["record_cleared"]),
        "freed_bytes": sum(r["freed_bytes"] for r in results),
    }
