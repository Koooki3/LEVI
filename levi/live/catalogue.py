"""Register live captures and make them browsable without a model or a core.

The controller starts one CPU-only process for a reconciliation pass. Heavy
format detection and view jobs stay here; the controller only polls this
process. One dataset is built at a time, under the same management lock as
physical deletion and a whole labelling batch. New arrivals are coalesced by
``views.request`` and a later pass repairs missing or stale views.

The module's import path is standard-library only. Read-only callers can use
``browse_info`` against another workspace without rebinding LEVI's globals.
"""

import argparse
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as live_config
from . import jsonio, mirror

POLL_S = 0.25
ACTIVE_JOBS = {"planned", "queued", "running"}


def start(config) -> dict:
    """Start or reuse one CPU catalogue pass, including from the product API.

    Always scan every eligible mirror: two prepare requests for different
    datasets coalesce into the same pass without dropping either target.
    The subprocess is tracked in this workspace, never the caller's default.
    """
    _, value = _start(config)
    return value


def running(config) -> bool:
    """A tracked CPU pass reserves intake before it acquires its first lock."""
    from levi import children

    record = jsonio.read(config.live_dir / "catalogue-process.json", {})
    if not isinstance(record, dict):
        return False
    pid, who = record.get("pid"), record.get("identity")
    return bool(
        isinstance(pid, int) and pid > 1 and who and children.identity(pid) == who
    )


def prepare(config, name) -> dict:
    """Explicitly prepare one safe mirror, retrying an unchanged failed view.

    A running CPU pass is refused rather than claiming a request it may have
    already passed. The process record serialises this check and the spawn.
    """
    _, value = _start(config, prepare_name=name)
    return value


def _prepare_request(config, name):
    from levi.dataset_management import Busy, ManagementError, read_json

    with dataset_lock(config, name) as acquired:
        if not acquired:
            raise Busy(
                "Dataset preparation or another task is in progress; try again later"
            )
        path = mirror.state_path(config, name)
        with jsonio.locked(path):
            state = read_json(path, None)
            if (
                not isinstance(state, dict)
                or state.get("archived")
                or not state.get("capture")
            ):
                raise ManagementError(404, "Unknown live dataset")
            if state.get("current") and not _unplanned_paused(config, name, state):
                raise Busy("An annotation batch references this dataset")
            if not _jobs_idle(config, name, state):
                raise Busy(
                    "A task references this dataset; try again after it finishes"
                )
            capture = Path(state["capture"])
            if not capture.is_dir() or not any(
                not row.get("deleted")
                and row.get("state") in ("mirrored", "done", "failed", "skipped_human")
                for row in (state.get("demos") or {}).values()
            ):
                raise Busy("No finished mirrored episodes are available to prepare")
            entry = _entry(config, state)
            recorded = state.get("catalogue") or {}
            root = Path(entry.get("view") or entry.get("path") or "")
            status = entry.get(
                "view_status", "ready" if entry.get("kind") == "lerobot" else "missing"
            )
            if (
                status == "ready"
                and recorded.get("input_revision") == _input_revision(state)
                and (root / "meta/info.json").is_file()
            ):
                return {"started": False, "running": False, "reason": "ready"}
            for key in ("input_revision", "error", "view_job"):
                recorded.pop(key, None)
            recorded["retry"] = True
            state["catalogue"] = recorded
            jsonio.write(path, state)
    return None


def _start(config, *, popen=subprocess.Popen, reap=True, prepare_name=None):
    from levi import children

    from . import resources

    record = config.live_dir / "catalogue-process.json"
    state_dir = config.workspace / "outputs/LEVI/workbench"
    with jsonio.locked(record):
        previous = jsonio.read(record, {})
        pid, who = previous.get("pid"), previous.get("identity")
        if pid and who and children.identity(pid) == who:
            if prepare_name is not None:
                from levi.dataset_management import Busy

                raise Busy(
                    "Dataset preparation is already in progress; try again later"
                )
            return None, {"pid": pid, "running": True, "started": False}
        if prepare_name is not None:
            no_op = _prepare_request(config, prepare_name)
            if no_op is not None:
                return None, no_op
        effective = config.live_dir / "effective.toml"
        effective.write_text(live_config.render(config))
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        log = config.logs_dir / "catalogue.log"
        resources.rotate_file(
            log, config.resources.log_max_mb, config.resources.log_backups
        )
        env = resources.service_env(config)
        env["LEVI_CPU_ONLY"] = "1"
        argv = [sys.executable, "-m", "levi.live.catalogue", "--config", str(effective)]
        if prepare_name is not None:
            argv.extend(["--dataset", prepare_name])
        with log.open("ab") as sink:
            proc = popen(
                argv,
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdout=sink,
                stderr=sink,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        children.track(proc, "live-catalogue", state=state_dir)
        who = children.identity(proc.pid)
        jsonio.write(
            record, {"pid": proc.pid, "identity": who, "started_at": time.time()}
        )

    # API callers do not keep a Popen. Reap their child and forget its tracked
    # group promptly; Controller.poll/untrack may do this too, harmlessly.
    if reap and isinstance(proc, subprocess.Popen):

        def reaped():
            proc.wait()
            children.untrack(proc.pid, state=state_dir)

        threading.Thread(
            target=reaped, name="live-catalogue-reaper", daemon=True
        ).start()
    value = {"pid": proc.pid, "running": proc.poll() is None, "started": True}
    if prepare_name is not None:
        value["reason"] = "preparing"
    return proc, value


def _reclaim_jobs(config):
    """Reclaimed conversion groups cannot leave their jobs forever running."""
    from levi import children

    state_dir = config.workspace / "outputs/LEVI/workbench"
    # A catalogue's conversion group has its own session: reclaim its parent
    # first, then anything whose owner was killed during that reclamation.
    orphaned = []
    for _ in range(2):
        orphaned.extend(
            row for row in children.listed(state=state_dir) if not row["owner_running"]
        )
        children.reclaim(state=state_dir)
    for row in orphaned:
        job_id = row.get("label")
        if row.get("kind") != "conversion" or not job_id or Path(job_id).name != job_id:
            continue
        path = state_dir / "jobs" / f"{job_id}.json"
        job = jsonio.read(path, {})
        if job.get("status") in ACTIVE_JOBS:
            job.update(
                status="interrupted",
                error="Catalogue owner exited; rebuild on the next pass",
            )
            jsonio.write(path, job)


def _input_revision(state):
    """Cheap intake revision: mirrors change only under the management lock."""
    try:
        stamp = Path(state["capture"]).stat().st_mtime_ns
    except (OSError, KeyError):
        stamp = None
    rows = sorted(
        (demo, row.get("sig"), bool(row.get("source_changed")))
        for demo, row in (state.get("demos") or {}).items()
        if not row.get("deleted")
        and row.get("state")
        in ("mirrored", "annotating", "done", "failed", "skipped_human")
    )
    return hashlib.sha256(
        json.dumps([stamp, rows], sort_keys=True).encode()
    ).hexdigest()


def dataset_lock(config, name, *, blocking=False):
    from levi.dataset_management import lock_for

    return lock_for(config.workspace, name, blocking=blocking)


def busy(config, name) -> bool:
    """A build, a labelling batch or a removal owns this dataset."""
    with dataset_lock(config, name) as acquired:
        return not acquired


def _unplanned_paused(config, name, state, *, check_jobs=False) -> bool:
    """Only an unstarted batch with an explicitly dead worker may be prepared.

    The caller additionally holds the management lock before building. Older
    workers did not take it, so a missing/ambiguous worker record is refused.
    Any run reference already freezes episode indices, even while paused.
    """
    batch = state.get("current")
    if not isinstance(batch, dict) or any(
        batch.get(k) for k in ("temporal", "anchored", "done")
    ):
        return False
    progress = jsonio.read(config.live_dir / "worker.json")
    if not isinstance(progress, dict) or progress.get("dataset") != name:
        return False
    pid = progress.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except OSError:
        return False  # Permission denied is not proof of an exited worker.
    else:
        return False
    return not check_jobs or _jobs_idle(config, name, state)


def _jobs_idle(config, name, state) -> bool:
    """Read-only, fail-closed job check for historical unstarted batches."""
    import sqlite3

    from levi.dataset_management import (
        ACTIVE,
        ManagementError,
        _assert_segmentation_idle,
        _matches,
        read_json,
    )

    state_dir = config.workspace / "outputs/LEVI/workbench"
    entry = _entry(config, state)
    base = entry.get("name") or name
    dataset_names = {name, base}
    roots = [Path(state["capture"])]
    if entry.get("view"):
        roots.append(Path(entry["view"]))
    try:
        items = read_json(state_dir / "datasets.json", {})
        if not isinstance(items, dict):
            return False
        for key, item in items.items():
            if not isinstance(item, dict):
                return False
            if item.get("base") == base:
                dataset_names.add(key)
                dataset_names.add(item.get("name") or key)
        folders = [state_dir / "jobs", config.workspace / "pool/jobs"]
        for parent in ("datasets", "recap_values", "segmentation"):
            folders.extend(state_dir / parent / key / "jobs" for key in dataset_names)
        for folder in folders:
            for path in folder.glob("*.json"):
                if path.name.endswith(".progress.json"):
                    continue
                job = read_json(path, {})
                if not isinstance(job, dict):
                    return False
                if job.get("status") in ACTIVE and (
                    folder not in folders[:2] or _matches(job, dataset_names, roots)
                ):
                    return False
        # The common overlay check reads in-memory sessions and durable plans;
        # its imports are standard-library modules, not model/numeric workers.
        _assert_segmentation_idle(state_dir, dataset_names)
        db_path = state_dir / "agent/workbench.sqlite3"
        if db_path.is_file():
            with contextlib.closing(
                sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            ) as db:
                for (body,) in db.execute("SELECT body FROM records WHERE kind='runs'"):
                    run = json.loads(body)
                    if not isinstance(run, dict):
                        return False
                    # A paused or approval-waiting run already references
                    # indices. It is unsafe even though no request is running.
                    if run.get("status") not in (
                        "succeeded",
                        "partially_succeeded",
                        "failed",
                        "cancelled",
                    ) and _matches(run, dataset_names, roots):
                        return False
    except (ManagementError, OSError, sqlite3.Error, ValueError):
        return False
    return True


def names(config) -> list[str]:
    """Changed mirrors, including old done rows lacking a catalog entry.

    Only a few state/metadata stats here: stable captures never cause another
    heavy process to be imported just for an unchanged browsing view.
    """
    pending = []
    for name, state in mirror.list_states(config).items():
        if state.get("archived") or not state.get("capture"):
            continue
        if state.get("current") and not _unplanned_paused(config, name, state):
            continue
        if not Path(state["capture"]).is_dir() or not any(
            not row.get("deleted")
            and row.get("state") in ("mirrored", "done", "failed", "skipped_human")
            for row in (state.get("demos") or {}).values()
        ):
            continue
        recorded = state.get("catalogue") or {}
        if recorded.get("input_revision") != _input_revision(state):
            pending.append(name)
            continue
        entry = _entry(config, state)
        status = entry.get(
            "view_status", "ready" if entry.get("kind") == "lerobot" else "missing"
        )
        root = Path(entry.get("view") or entry.get("path") or "")
        if (
            not entry
            or status not in ("ready", "failed")
            or (status == "ready" and not (root / "meta/info.json").is_file())
        ):
            pending.append(name)
    return pending


def _entry(config, state) -> dict:
    items = jsonio.read(config.workspace / "outputs/LEVI/workbench/datasets.json", {})
    if not isinstance(items, dict):
        return {}
    capture = state.get("capture")
    return next(
        (
            entry
            for entry in items.values()
            if isinstance(entry, dict)
            and not entry.get("base")
            and entry.get("path") == capture
        ),
        {},
    )


def browse_info(config, name, session_id=None) -> dict:
    """A stable dataset id and the first visible episode of this session.

    No numerical libraries or global catalog paths: the product's live page
    reads the live workspace directly. A missing session mapping stays None
    instead of silently opening episode zero of a different session.
    """
    empty = {"repo_id": None, "view_status": "missing", "first_episode_index": None}
    state = mirror.load_state(config, name)
    if not state or state.get("archived"):
        return empty
    entry = _entry(config, state)
    if not entry:
        return {
            **empty,
            "view_status": (state.get("catalogue") or {}).get("view_status", "missing"),
        }
    status = entry.get(
        "view_status", "ready" if entry.get("kind") == "lerobot" else "missing"
    )
    root = Path(entry.get("view") or entry.get("path") or "")
    if status == "ready" and not (root / "meta/info.json").is_file():
        status = "missing"
    indices = [
        row["episode_index"]
        for row in (state.get("demos") or {}).values()
        if not row.get("deleted")
        and not row.get("excluded")
        and isinstance(row.get("episode_index"), int)
        and (session_id is None or row.get("run_id") == session_id)
    ]
    return {
        "repo_id": entry.get("id"),
        "view_status": status,
        "first_episode_index": min(indices) if status == "ready" and indices else None,
    }


def episode_map(entry) -> tuple[dict, dict, dict]:
    """Demo → index and duration from published provenance, never demo numbers."""
    view = Path(entry.get("view") or entry["path"])
    fps = float(jsonio.read(view / "meta/info.json", {}).get("fps") or 10.0)
    indices, lengths = {}, {}
    path = view / "meta/episodes.jsonl"
    if path.is_file():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            demo = (
                str(row.get("source_demo") or "").replace("\\", "/").rsplit("/", 1)[-1]
            )
            if demo:
                indices[demo] = int(row["episode_index"])
                lengths[demo] = max(0.0, (row.get("length", 1) - 1) / fps)
    excluded = jsonio.read(view / "meta/levi_view.json", {}).get("excluded") or {}
    return indices, lengths, {str(k).rsplit("/", 1)[-1]: v for k, v in excluded.items()}


def _record(config, name, entry, *, error=None):
    value = {
        "repo_id": entry.get("id"),
        "dataset_name": entry.get("name"),
        "view_status": entry.get(
            "view_status", "ready" if entry.get("kind") == "lerobot" else "missing"
        ),
        "view_job": entry.get("view_job"),
    }
    if error:
        value["error"] = str(error)[:300]

    def change(state):
        if not state or state.get("archived"):
            return state
        value["input_revision"] = _input_revision(state)
        previous = {
            k: v for k, v in (state.get("catalogue") or {}).items() if k != "updated_at"
        }
        if previous != value:
            state["catalogue"] = {**value, "updated_at": time.time()}
        return state

    return jsonio.update(mirror.state_path(config, name), change, default=dict)


def map_view(config, name, entry):
    """Synchronise only episode indices; operator and automatic labels survive.

    Call while holding the dataset management lock, after publication. An
    excluded, invalid or deleted demo may disappear from a rebuilt view; its
    old index must then be cleared rather than pointing at another episode.
    """
    index, lengths, excluded = episode_map(entry)

    def change(state):
        if not state or state.get("archived"):
            return state
        for demo, row in (state.get("demos") or {}).items():
            if row.get("deleted") or demo not in index:
                row.pop("episode_index", None)
            else:
                row["episode_index"] = index[demo]
        return state

    jsonio.update(mirror.state_path(config, name), change, default=dict)
    _record(config, name, entry)
    return index, lengths, excluded


def ensure_dataset(
    config, name, *, allow_current=False, check_stop=None, heartbeat=None, timeout=3600
):
    """Keep owned view jobs inside the management lock, including on stop."""
    from levi import jobs

    try:
        return _ensure_dataset(
            config,
            name,
            allow_current=allow_current,
            check_stop=check_stop,
            heartbeat=heartbeat,
            timeout=timeout,
        )
    except BaseException:
        jobs.stop_workers()
        raise
    finally:
        if jobs.wait_idle(10.0):
            jobs.stop_workers()
            jobs.wait_idle(10.0)


def _ensure_dataset(
    config, name, *, allow_current=False, check_stop=None, heartbeat=None, timeout=3600
):
    """Register and wait for one capture under the caller's management lock.

    A live core is optional: jobs started here are owned by this CPU process,
    which remains alive until publication. At most three successive builds
    absorb arrivals during a pass; further arrivals are handled on the next
    pass instead of holding the dataset forever.
    """
    from levi import catalog, jobs, views
    from levi.conversion import registry
    from levi.conversion.engine import fingerprint

    state = mirror.load_state(config, name)
    if (
        not state
        or state.get("archived")
        or (
            state.get("current")
            and not allow_current
            and not _unplanned_paused(config, name, state)
        )
        or (not allow_current and not _jobs_idle(config, name, state))
    ):
        return None
    force_retry = bool((state.get("catalogue") or {}).get("retry"))
    capture = Path(state["capture"])
    if not capture.is_dir():
        return None
    fmt = registry.detect(capture)
    if fmt is None:
        raise ValueError("No supported dataset format found in the finished capture")
    if not fmt.viewable:
        entry = catalog.register(str(capture))
        map_view(config, name, entry)
        return entry
    options = {"workers": config.resources.view_workers}
    deadline = time.monotonic() + timeout
    for _ in range(3):
        if check_stop:
            check_stop()
        signature = fingerprint(capture)
        existing = _entry(config, state)
        if (
            existing.get("view_status") == "failed"
            and existing.get("view_failed_fingerprint") == signature
            and not force_retry
        ):
            _record(config, name, existing, error=existing.get("view_error"))
            raise RuntimeError(
                "view build failed: " + str(existing.get("view_error"))[:300]
            )
        entry = views.request(capture, options)
        _record(config, name, entry)
        while True:
            if check_stop:
                check_stop()
            if heartbeat:
                heartbeat(name)
            entry = catalog.datasets().get(entry["name"]) or entry
            view = Path(entry["view"]) if entry.get("view") else None
            if (
                entry.get("view_status") == "ready"
                and view is not None
                and views.is_view(view)
            ):
                if jsonio.read(view / "meta/levi_view.json", {}).get(
                    "source_fingerprint"
                ) == fingerprint(capture):
                    map_view(config, name, entry)
                    return entry
                break  # More finished demos arrived during the build.
            job = catalog.read(
                jobs.STATE / "jobs" / f"{entry.get('view_job')}.json", {}
            )
            if entry.get("view_status") == "failed":
                if job.get("source_fingerprint") != fingerprint(capture):
                    break
                _record(config, name, entry, error=entry.get("view_error"))
                raise RuntimeError(
                    "view build failed: " + str(entry.get("view_error"))[:300]
                )
            if job.get("status") not in ACTIVE_JOBS:
                break  # Missing/interrupted jobs can be resumed without a core.
            if time.monotonic() > deadline:
                raise RuntimeError("view build did not finish in an hour")
            time.sleep(POLL_S)
    # A busy capture will be reconsidered on the next bounded pass.
    return None


class Stopped(Exception):
    pass


def reconcile(
    config, *, selected=None, check_stop=None, heartbeat=None, log=print
) -> int:
    """One serial pass, skipping a running batch or a removal."""
    failures = 0
    for name in selected if selected is not None else names(config):
        if check_stop:
            check_stop()
        with dataset_lock(config, name) as acquired:
            if not acquired:
                continue
            try:
                ensure_dataset(config, name, check_stop=check_stop, heartbeat=heartbeat)
            except Stopped:
                raise
            except Exception as exc:  # noqa: BLE001 - another task can still be registered
                failures += 1
                log(f"{name}: catalogue failed: {type(exc).__name__}: {str(exc)[:300]}")
                _record(
                    config,
                    name,
                    _entry(config, mirror.load_state(config, name) or {}),
                    error=exc,
                )
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m levi.live.catalogue")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dataset")
    args = parser.parse_args(argv)
    config = live_config.load(args.config)
    # Set before any heavy import. No GPU policy, provider or model is used.
    from . import resources

    os.environ.update(resources.service_env(config))
    os.environ["LEVI_CPU_ONLY"] = "1"
    resources.apply(config)
    from levi import children, jobs, paths

    paths.configure()
    _reclaim_jobs(config)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    def check_stop():
        if stopping:
            raise Stopped

    def heartbeat(name):
        jsonio.write(
            config.live_dir / "catalogue.json",
            {"pid": os.getpid(), "dataset": name, "updated_at": time.time()},
        )

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return int(
            bool(
                reconcile(
                    config,
                    selected=[args.dataset] if args.dataset else None,
                    check_stop=check_stop,
                    heartbeat=heartbeat,
                )
            )
        )
    except Stopped:
        return 0
    finally:
        # A daemon job thread must not outlive its capture lock or workspace.
        jobs.stop_workers()
        jobs.wait_idle(10.0)
        children.stop_owned()
        with contextlib.suppress(OSError):
            (config.live_dir / "catalogue.json").unlink()


if __name__ == "__main__":
    raise SystemExit(main())
