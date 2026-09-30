"""Follow rollout directories and mirror finished demos into the workspace.

Each ``<group>/<task_folder>`` under a watched root becomes one LEVI dataset,
named ``<group>__<task_folder>``; its raw capture lives in
``<workspace>/captures/<name>/demo_NNNN/``. The source tree is never written.

**Scanning** is stat-only. A task folder whose modification time has not
changed and that has no demo still being written is not listed again; a
finished demo is classified once (criteria.py) and remembered in the dataset's
state file. Rollouts finished before the service (or the task's evaluation
session) began are *backlog*: recognised from the demo directory's mtime
without opening anything, counted, and never touched unless
``watch.backlog = "process"``.

**Mirroring** happens only when the dataset is between batches, so a run never
sees its source move under it: a finished demo is hard-linked file by file
into ``.partial-demo_NNNN`` and renamed into place. Across file systems the
files are copied instead. The link costs no disk space; because the demo is
finished before it is linked, no half-written file can be shared.

Standard library only.
"""

import fnmatch
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import criteria, jsonio

SCHEMA = "levi.live.dataset.v1"
PARTIAL = ".partial-"


def dataset_name(group: str, task: str) -> str:
    """A stable, URL- and filename-safe catalog name for one group/task."""
    raw = f"{group}__{task}"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_").lstrip(".")[:120]
    if name != raw:
        # Sanitising is lossy: two raw names must not share a dataset.
        import hashlib

        name = f"{name or 'task'}-{hashlib.sha256(raw.encode()).hexdigest()[:6]}"
    return name


def datasets_dir(config) -> Path:
    return config.live_dir / "datasets"


def state_path(config, name: str) -> Path:
    return datasets_dir(config) / f"{name}.json"


def service_epoch(config) -> float:
    """When the service first ran (the default backlog cutoff), persisted."""
    from .sessions import parse_time

    if config.watch.backlog == "process":
        return 0.0
    if config.watch.since:
        parsed = parse_time(config.watch.since)
        if parsed is not None:
            return parsed
    path = config.live_dir / "service.json"
    value = jsonio.read(path, None)
    if not isinstance(value, dict) or "first_started_at" not in value:
        value = jsonio.update(
            path,
            lambda v: (
                v
                if isinstance(v, dict) and "first_started_at" in v
                else {"first_started_at": time.time()}
            ),
            default=dict,
        )
    return float(value["first_started_at"])


def empty_state(config, key, cutoff: float) -> dict:
    root, group, task = key
    name = dataset_name(group, task)
    return {
        "schema": SCHEMA,
        "name": name,
        "group": group,
        "task_folder": task,
        "root": str(root),
        "source": str(Path(root) / group / task),
        "capture": str(config.captures_dir / name),
        "task_text": "",
        "cutoff": cutoff,
        "created_at": time.time(),
        "demos": {},
        "incomplete": {"count": 0, "fr3_fault": 0},
        "discarded": 0,
        "current": None,
        "last_batch": None,
        "last_error": "",
        "last_processed_at": 0.0,
    }


_STATE_CACHE: dict = {}


def load_state(config, name: str):
    """A dataset's state (cached by file stamp), or None."""
    path = state_path(config, name)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        _STATE_CACHE.pop(str(path), None)
        return None
    cached = _STATE_CACHE.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    value = jsonio.read(path)
    if isinstance(value, dict):
        _STATE_CACHE[str(path)] = (stamp, value)
        return value
    return None


def list_states(config) -> dict:
    try:
        names = sorted(p.stem for p in datasets_dir(config).glob("*.json"))
    except OSError:
        return {}
    return {n: s for n in names if (s := load_state(config, n))}


def counts(state: dict) -> dict:
    """Per-state demo counts of a dataset state."""
    out = {
        "mirrored": 0,
        "annotating": 0,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "rejected": 0,
    }
    for row in (state.get("demos") or {}).values():
        key = row.get("state")
        if key in out:
            out[key] += 1
        elif key == "skipped_human":
            out["skipped"] += 1
    return out


@dataclass
class TaskScan:
    key: tuple  # (root, group, task_folder)
    name: str
    source: str
    ready: list = field(default_factory=list)  # finished, not yet mirrored
    waiting: list = field(default_factory=list)  # still being written
    waiting_reasons: dict = field(default_factory=dict)
    backlog: int = 0
    incomplete: int = 0
    fr3_fault: int = 0
    abort_reasons: dict = field(default_factory=dict)
    discarded: int = 0
    rejected: int = 0
    last_seen: float = 0.0
    available: bool = True


class Scanner:
    """Stat-only follower of the watched roots."""

    def __init__(self, config):
        self.config = config
        self._tasks: dict = {}
        self._mtimes: dict = {}
        self._aborts: dict = {}
        self.epoch = None
        self.errors: list = []

    # --- listing --------------------------------------------------------------

    def _selected(self, group, task) -> bool:
        label = f"{group}/{task}"
        w = self.config.watch
        if w.include and not any(fnmatch.fnmatch(label, p) for p in w.include):
            return False
        return not any(fnmatch.fnmatch(label, p) for p in w.exclude)

    @staticmethod
    def _subdirs(path):
        try:
            with os.scandir(path) as entries:
                return [
                    e
                    for e in entries
                    if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)
                ]
        except OSError:
            return []

    def tasks(self):
        """(root, group, task_folder, path) for every selected task folder."""
        for root in self.config.watch.roots:
            root = Path(root).expanduser()
            for group in self._subdirs(root):
                for task in self._subdirs(group.path):
                    if self._selected(group.name, task.name):
                        yield root, group.name, task.name, task.path

    # --- one pass ---------------------------------------------------------------

    def scan(self, sessions=None, now=None) -> list:
        now = time.time() if now is None else now
        sessions = sessions or {}
        if self.epoch is None:
            self.epoch = service_epoch(self.config)
        result = []
        seen = set()
        for root, group, task, path in self.tasks():
            key = (str(root), group, task)
            seen.add(key)
            try:
                found = self._scan_task(key, path, sessions.get((group, task)), now)
            except OSError as exc:  # a folder vanishing mid-scan is not fatal
                self.errors = [f"{path}: {exc}"][:5]
                continue
            if found is not None:
                result.append(found)
        for key in list(self._tasks):
            if key not in seen:
                self._tasks[key].available = False
                result.append(self._tasks[key])
        return result

    def _cutoff(self, session):
        """Demos finished before this are backlog."""
        if self.config.watch.backlog == "process":
            return 0.0
        cutoff = self.epoch
        if session and session.started_at and session.levi_enabled is not False:
            cutoff = min(cutoff, session.started_at)
        return cutoff

    def _scan_task(self, key, path, session, now):
        _root, group, task = key
        name = dataset_name(group, task)
        if self.config.watch.require_session and not (session and session.levi_enabled):
            return None
        if session is not None and session.levi_enabled is False:
            # The operator answered "no" to background annotation.
            return self._tasks.get(key)
        state = load_state(self.config, name)
        known = (state or {}).get("demos") or {}
        stamp = os.stat(path).st_mtime_ns
        previous = self._tasks.get(key)
        if (
            previous is not None
            and self._mtimes.get(key) == stamp
            and not previous.waiting
            and previous.available
        ):
            previous.ready = [d for d in previous.ready if d not in known]
            return previous
        scan = TaskScan(key=key, name=name, source=str(path), last_seen=now)
        cutoff = (state or {}).get("cutoff")
        if cutoff is None or self.config.watch.backlog == "process":
            # A state keeps the cutoff it was created with, but asking for the
            # backlog later reaches back to the beginning.
            cutoff = self._cutoff(session)
        w = self.config.watch
        try:
            entries = [e for e in os.scandir(path)]
        except OSError:
            scan.available = False
            return scan
        rejected = []
        for entry in sorted(entries, key=lambda e: e.name):
            kind = criteria.kind_of(entry.name)
            if kind is None or not entry.is_dir(follow_symlinks=False):
                continue
            if kind == "discarded":
                scan.discarded += 1
                continue
            if kind == "incomplete":
                scan.incomplete += 1
                reason = self._aborts.get(entry.path)
                if reason is None:
                    reason = criteria.abort_reason(entry.path) or ""
                    self._aborts[entry.path] = reason
                scan.abort_reasons[reason or "unknown"] = (
                    scan.abort_reasons.get(reason or "unknown", 0) + 1
                )
                if reason == "fr3_fault":
                    scan.fr3_fault += 1
                continue
            if entry.name in known:
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                scan.backlog += 1
                continue
            done = criteria.check(
                entry.path,
                now=now,
                legacy_quiet_s=60.0,
                settle_s=w.settle_s,
            )
            if done.ok:
                scan.ready.append(entry.name)
            elif done.state == "rejected":
                rejected.append((entry.name, done.reason, done.completed_at))
            else:
                scan.waiting.append(entry.name)
                scan.waiting_reasons[entry.name] = done.reason
        scan.rejected = len(rejected)
        if (
            state is not None
            or scan.ready
            or scan.waiting
            or rejected
            or scan.incomplete
            or scan.discarded
        ):
            self._record(key, name, cutoff, scan, rejected, state)
        # Remembered either way, so an unchanged folder is not listed again.
        self._tasks[key] = scan
        self._mtimes[key] = stamp
        return scan

    def _record(self, key, name, cutoff, scan, rejected, state):
        """Persist what a scan learned that nothing else will: rejected demos
        and the incomplete/discarded counts. Creates the state on first need."""

        def change(value):
            if not value:
                value = empty_state(self.config, key, cutoff)
            for demo, reason, at in rejected:
                value["demos"].setdefault(
                    demo,
                    {"state": "rejected", "reason": reason, "completed_at": at},
                )
            value["incomplete"] = {
                "count": scan.incomplete,
                "fr3_fault": scan.fr3_fault,
                "reasons": dict(scan.abort_reasons),
            }
            value["discarded"] = scan.discarded
            return value

        jsonio.update(state_path(self.config, name), change, default=dict)


# --- mirroring ---------------------------------------------------------------


def link_tree(src: Path, dst: Path) -> dict:
    """Hard-link every file of ``src`` into a new directory ``dst`` (copy when
    linking is impossible, e.g. across file systems). Symbolic links are
    skipped. Returns ``{"linked": n, "copied": n, "copied_bytes": n}``."""
    out = {"linked": 0, "copied": 0, "copied_bytes": 0}
    dst.mkdir(parents=True)
    with os.scandir(src) as entries:
        for entry in entries:
            target = dst / entry.name
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                sub = link_tree(Path(entry.path), target)
                for k in out:
                    out[k] += sub[k]
                continue
            try:
                os.link(entry.path, target)
                out["linked"] += 1
            except OSError:
                shutil.copy2(entry.path, target)
                out["copied"] += 1
                out["copied_bytes"] += target.stat().st_size
    return out


def clear_partials(capture: Path):
    try:
        for child in capture.iterdir():
            if child.name.startswith(PARTIAL):
                shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass


def mirror_demo(src: Path, capture: Path, *, now: float, settle_s=0.0) -> dict:
    """Take one finished demo into ``capture``; idempotent.

    ``status``: ``mirrored``, ``exists`` (already there), ``vanished`` (the
    source disappeared), or ``changed`` (no longer finished once linked).
    """
    src, capture = Path(src), Path(capture)
    dst = capture / src.name
    if dst.exists():
        return {"status": "exists"}
    capture.mkdir(parents=True, exist_ok=True)
    temp = capture / f"{PARTIAL}{src.name}"
    shutil.rmtree(temp, ignore_errors=True)
    try:
        stats = link_tree(src, temp)
    except FileNotFoundError:
        shutil.rmtree(temp, ignore_errors=True)
        return {"status": "vanished"}
    done = criteria.check(temp, now=now, legacy_quiet_s=0.0, settle_s=0.0)
    if not done.ok:
        shutil.rmtree(temp, ignore_errors=True)
        return {"status": "changed", "reason": done.reason}
    os.rename(temp, dst)
    return {"status": "mirrored", "completed_at": done.completed_at, **stats}


def task_text(source_task: Path, demo: Path | None = None) -> str:
    """The instruction of a task: its ``task_description.txt``, else the
    ``task_description`` of a demo's metadata, else the folder name."""
    try:
        text = (
            (source_task / "task_description.txt").read_text(encoding="utf-8").strip()
        )
        if text:
            return text
    except OSError:
        pass
    if demo is not None:
        meta = jsonio.read(Path(demo) / "metadata.json")
        if isinstance(meta, dict) and str(meta.get("task_description") or "").strip():
            return str(meta["task_description"]).strip()
    return source_task.name.replace("_", " ")


def write_task_text(capture: Path, text: str):
    """The capture's ``task_description.txt`` (what LEVI reads as the task)."""
    target = capture / "task_description.txt"
    if target.exists() and target.read_text(encoding="utf-8").strip() == text:
        return
    capture.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")


def mirror_dataset(config, state: dict, names, *, now: float | None = None) -> dict:
    """Mirror ``names`` (finished demos of one task) and record them in the
    dataset's state. Returns ``{demo: result}``."""
    now = time.time() if now is None else now
    name = state["name"]
    capture = Path(state["capture"])
    source = Path(state["source"])
    clear_partials(capture)
    results = {}
    for demo in names:
        result = mirror_demo(source / demo, capture, now=now)
        results[demo] = result
    text = task_text(source, source / names[0] if names else None)
    if any(r["status"] in ("mirrored", "exists") for r in results.values()):
        write_task_text(capture, text)

    def change(value):
        value["task_text"] = text
        for demo, result in results.items():
            row = value["demos"].get(demo) or {}
            if result["status"] in ("mirrored", "exists"):
                if row.get("state") in (None, "rejected"):
                    meta = jsonio.read(source / demo / "metadata.json") or {}
                    value["demos"][demo] = {
                        "state": "mirrored",
                        "run_id": (meta.get("eval") or {}).get("run_id"),
                        "outcome_recorded": (meta.get("eval") or {}).get("outcome"),
                        "mirrored_at": now,
                        "completed_at": result.get("completed_at"),
                        "attempts": 0,
                        "copied": result.get("copied", 0),
                    }
            elif result["status"] == "changed":
                value["demos"][demo] = {
                    "state": "rejected",
                    "reason": result.get("reason", "changed while mirroring"),
                }
        return value

    jsonio.update(state_path(config, name), change, default=dict)
    return results
