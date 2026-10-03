"""Follow rollout directories and mirror finished demos into the workspace.

Each ``<group>/<task_folder>`` under a watched root becomes one LEVI dataset,
named ``<group>__<task_folder>`` (``resolve_name``: the same task under another
root gets ``<name>__at__<root mark>``); its raw capture lives in
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


ROOT_MARK = "__at__"  # <name>__at__<root mark>: the same task under another root
MARK_MAX = 20  # characters of the root's last folder name kept in a mark
NAME_MAX = 140  # what the live API accepts of a dataset name


def root_mark(root) -> str:
    """A short, filename-safe label for a rollout root: its last folder name
    (``models`` for ``.../online_rollout_data/models``)."""
    last = Path(str(root)).name
    mark = re.sub(r"[^A-Za-z0-9.-]+", "_", last).strip("_").lstrip(".")
    return mark[:MARK_MAX] or "root"


def same_root(a, b) -> bool:
    """Do two recorded roots name one folder? A state without a recorded root
    (written before roots were recorded) counts as matching."""
    if not a or not b:
        return True
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except OSError:
        return str(a) == str(b)


def resolve_name(config, root, group: str, task: str, claimed=None) -> str:
    """The dataset a ``(root, group, task)`` is, by name.

    One ``(group, task)`` under two roots is two datasets: the name alone
    (``dataset_name``) would make the second inherit the first's state, source
    and capture. The plain name stays with the root whose state already holds
    it (names already in use are never changed or moved); another root gets
    ``<name>__at__<root mark>``, and ``-<hash of the root>`` after that when two
    roots share a last folder name. A state that already belongs to this root
    is always found again, whichever candidate it sits under, so a restart
    resolves the same way. ``claimed`` (``{name: root}``) is the names taken
    earlier in one scan by roots that have no state yet."""
    import hashlib

    base = dataset_name(group, task)
    short = base[: NAME_MAX - len(ROOT_MARK) - MARK_MAX - 8]
    mark = f"{short}{ROOT_MARK}{root_mark(root)}"
    digest = hashlib.sha256(os.path.realpath(str(root)).encode()).hexdigest()[:6]
    candidates = [base, mark, f"{mark}-{digest}"]
    states = [(name, load_state(config, name)) for name in candidates]
    for name, state in states:
        if state is not None and same_root(state.get("root"), root):
            return name
    for name, state in states:
        owner = (claimed or {}).get(name)
        if state is None and (owner is None or same_root(owner, root)):
            return name
    return candidates[-1]


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


def empty_state(config, key, cutoff: float, name: str | None = None) -> dict:
    root, group, task = key
    name = name or dataset_name(group, task)
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
        st = path.stat()
        # Inode and size as well as the time: two writes in one clock tick
        # must not read as the same file.
        stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
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
    """Per-state demo counts of a dataset state. An excluded episode
    (``levi/live/exclusion.py``) is not counted in any state."""
    out = {
        "mirrored": 0,
        "annotating": 0,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "rejected": 0,
        "stuck": 0,
    }
    for row in (state.get("demos") or {}).values():
        if row.get("excluded"):
            continue
        key = row.get("state")
        if key in out:
            out[key] += 1
        elif key == "skipped_human":
            out["skipped"] += 1
    return out


def waiting_demos(state: dict, max_attempts: int) -> list:
    """The mirrored demos still to label: not out of attempts and not
    excluded by a person."""
    return [
        d
        for d, row in (state.get("demos") or {}).items()
        if row.get("state") == "mirrored"
        and row.get("attempts", 0) < max_attempts
        and not row.get("excluded")
    ]


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
    stuck: int = 0
    stuck_marks: dict = field(default_factory=dict)
    last_seen: float = 0.0
    available: bool = True


class Scanner:
    """Stat-only follower of the watched roots."""

    def __init__(self, config):
        self.config = config
        self._tasks: dict = {}
        self._mtimes: dict = {}
        self._aborts: dict = {}
        self._names: dict = {}  # (root, group, task) -> dataset name, once chosen
        self._claimed: dict = {}  # name -> root, for roots that have no state yet
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

    def name_of(self, key) -> str:
        """The dataset name of ``(root, group, task)``, chosen once per
        scanner (``resolve_name``) so it cannot change under a running scan."""
        name = self._names.get(key)
        if name is None:
            root, group, task = key
            name = resolve_name(self.config, root, group, task, self._claimed)
            self._names[key] = name
            self._claimed.setdefault(name, root)
        return name

    def known_name(self, key) -> str:
        """``name_of`` for a reader (status, fault lookups): the chosen name
        if there is one, else what ``resolve_name`` says, without taking it."""
        return self._names.get(key) or resolve_name(self.config, *key)

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
                found = self._scan_task(
                    key, path, sessions.get((str(root), group, task)), now
                )
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

    @staticmethod
    def _stuck_moved(scan, path) -> bool:
        """A demo set aside as stuck changed since: look at the folder again.
        (Files inside a demo changing does not change the task folder's own
        time, so this is checked by the few stuck demos' own times.)"""
        return any(
            criteria.newest_mtime(Path(path) / name) != mark
            for name, mark in scan.stuck_marks.items()
        )

    def _cutoff(self, session):
        """Demos finished before this are backlog."""
        if self.config.watch.backlog == "process":
            return 0.0
        cutoff = self.epoch
        if session and session.started_at and session.levi_enabled is not False:
            cutoff = min(cutoff, session.started_at)
        return cutoff

    def _scan_task(self, key, path, session, now):
        name = self.name_of(key)
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
            and not self._stuck_moved(previous, path)
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
        stuck = []
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
            row = known.get(entry.name)
            if row is not None:
                if row.get("state") != "stuck":
                    continue
                mark = criteria.newest_mtime(entry.path)
                if mark == row.get("mtime"):
                    scan.stuck += 1
                    scan.stuck_marks[entry.name] = mark
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
                mark = criteria.newest_mtime(entry.path)
                if now - mark > w.stuck_s:
                    # Never going to finish (a leftover raw capture, a client
                    # that died mid-write): not "waiting" any more, so it no
                    # longer keeps the service active or the folder re-listed.
                    scan.stuck += 1
                    scan.stuck_marks[entry.name] = mark
                    stuck.append((entry.name, done.reason, mark))
                else:
                    scan.waiting.append(entry.name)
                    scan.waiting_reasons[entry.name] = done.reason
        scan.rejected = len(rejected)
        if (
            state is not None
            or scan.ready
            or scan.waiting
            or rejected
            or stuck
            or scan.incomplete
            or scan.discarded
        ):
            self._record(key, name, cutoff, scan, rejected, state, stuck)
        # Remembered either way, so an unchanged folder is not listed again.
        self._tasks[key] = scan
        self._mtimes[key] = stamp
        return scan

    def _record(self, key, name, cutoff, scan, rejected, state, stuck=()):
        """Persist what a scan learned that nothing else will: rejected demos
        and the incomplete/discarded counts. Creates the state on first need."""

        def change(value):
            if not value:
                value = empty_state(self.config, key, cutoff, name)
            for demo, reason, at in rejected:
                value["demos"].setdefault(
                    demo,
                    {"state": "rejected", "reason": reason, "completed_at": at},
                )
            for demo, reason, mark in stuck:
                value["demos"][demo] = {
                    "state": "stuck",
                    "reason": reason,
                    "mtime": mark,
                    "since": time.time(),
                }
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


def source_signature(src: Path) -> dict:
    """Inode and mtime of the two files a finished demo's identity rests on:
    the completion marker and ``metadata.json``. A demo deleted and written
    again under the same number, or a metadata file replaced after the marker,
    changes it."""
    out = {}
    for name in (".complete", "metadata.json"):
        try:
            st = os.stat(Path(src) / name)
            out[name] = [st.st_ino, st.st_mtime_ns]
        except OSError:
            out[name] = None
    return out


def verify_sources(config, name: str) -> list:
    """Flag (``source_changed``) every mirrored demo whose source is now a
    different file than the one linked; returns the newly flagged demos. A
    source that is gone is not a change (the mirror keeps its own copy)."""
    state = load_state(config, name)
    if not state:
        return []
    source = Path(state["source"])
    changed = []
    for demo, row in (state.get("demos") or {}).items():
        sig = row.get("sig")
        if not sig or row.get("source_changed"):
            continue
        now = source_signature(source / demo)
        if any(v is not None for v in now.values()) and now != sig:
            changed.append(demo)
    if changed:

        def flag(value):
            for demo in changed:
                value["demos"][demo]["source_changed"] = time.time()
            return value

        jsonio.update(state_path(config, name), flag, default=dict)
    return changed


def source_problem(state) -> str | None:
    """Why this dataset's source cannot supply demos, or None: its folder is
    gone, is not a folder, or holds nothing."""
    source = Path(str((state or {}).get("source") or ""))
    try:
        with os.scandir(source) as entries:
            if next(entries, None) is None:
                return f"the source folder {source} is empty"
    except OSError:
        return f"the source folder {source} is missing"
    return None


def set_available(config, name: str, reason: str | None) -> None:
    """Mark a dataset's source unavailable (with the reason) or available
    again; the queue skips an unavailable dataset until its source returns."""

    def change(value):
        if reason is None:
            value.pop("unavailable", None)
        else:
            value["unavailable"] = {"reason": reason, "since": time.time()}
        return value

    if load_state(config, name) is not None:
        jsonio.update(state_path(config, name), change, default=dict)


def is_available(config, name: str, state=None) -> bool:
    """False while a dataset is marked unavailable and its source has not come
    back (which clears the mark)."""
    state = state if state is not None else load_state(config, name)
    if not (state or {}).get("unavailable"):
        return True
    if source_problem(state) is None:
        set_available(config, name, None)
        return True
    return False


def refresh_changed(config, name: str) -> list:
    """Mirror again the demos whose source changed and that nobody has
    annotated yet; one that already carries annotations keeps its flag (what
    was annotated is the old content, a person decides)."""
    state = load_state(config, name)
    if not state:
        return []
    capture, source = Path(state["capture"]), Path(state["source"])
    again = [
        d
        for d, row in state["demos"].items()
        if row.get("source_changed")
        and row.get("state") == "mirrored"
        # Left as it is: a person took it out, and its mirror is kept.
        and not row.get("excluded")
    ]
    done = []
    for demo in again:
        if criteria.check(source / demo, now=time.time(), legacy_quiet_s=0.0).ok:
            shutil.rmtree(capture / demo, ignore_errors=True)
            if (
                mirror_demo(source / demo, capture, now=time.time())["status"]
                == "mirrored"
            ):
                done.append(demo)

    def clear(value):
        for demo in done:
            row = value["demos"][demo]
            row.pop("source_changed", None)
            row["sig"] = source_signature(source / demo)
        return value

    if done:
        jsonio.update(state_path(config, name), clear, default=dict)
    return done


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
                if row.get("state") in (None, "rejected", "stuck"):
                    meta = jsonio.read(source / demo / "metadata.json") or {}
                    value["demos"][demo] = {
                        "state": "mirrored",
                        "sig": source_signature(source / demo),
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
