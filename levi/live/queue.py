"""The background labelling queue: which live dataset is labelled, and how.

Every live dataset (one pipeline: ``<group>__<task>``) has a *labelling mode*,
registered in ``<workspace>/live/queue.json``:

``auto``
    Episodes that finish while the service runs are labelled (the default; a
    dataset nobody registered gets ``[queue] default_mode``).
``history``
    As ``auto``, and the history the service skipped (``watch.backlog =
    "skip"``: episodes finished before it first ran) is labelled too, newest
    first. The global ``watch.backlog`` is unchanged; this is an explicit
    exception for one dataset, and only if ``[queue] history`` allows it.
``paused``
    The queue is kept (episodes are still mirrored and counted), nothing is
    labelled until the mode changes.
``off``
    Nothing is labelled.

The file is written atomically and fsynced, under the same lock for every
writer (the supervisor registering a new dataset, a person's API call or CLI
command). A file that cannot be read is never overwritten: every write is
refused with ``Unreadable`` and the original stays for a person to repair;
readers treat each dataset as ``paused`` until then (nothing is labelled on a
guess). Only a person changes a mode (the API refuses agent credentials); the
supervisor only registers datasets it has not seen, with the default mode.

Standard library only (the scanner and the idle supervisor import this).
"""

import contextlib
import json
import os
import time
from pathlib import Path

from . import jsonio
from .config import LABELLING_MODES

SCHEMA = "levi.live.queue.v1"
FILE = "queue.json"
# Request ids remembered for replays (a retried call changes nothing twice).
REQUESTS_KEPT = 64
# Queue states of ``status.json`` (``queue[].state``).
STATES = ("queued", "running", "yielding", "paused", "done", "failed")
# Who may be named as the author of a change.
ACTORS = ("person", "cli", "default")


class Unreadable(Exception):
    """``queue.json`` exists but cannot be read; it is left as it is."""


class Conflict(Exception):
    """A request id that was used before for a different change."""


class Invalid(ValueError):
    """Not a labelling mode, or not a dataset name."""


def path(config) -> Path:
    return config.live_dir / FILE


def _empty() -> dict:
    return {"schema": SCHEMA, "updated_at": None, "datasets": {}, "requests": []}


def _valid(value) -> bool:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return False
    rows = value.get("datasets")
    if not isinstance(rows, dict):
        return False
    return all(
        isinstance(name, str)
        and isinstance(row, dict)
        and row.get("mode") in LABELLING_MODES
        for name, row in rows.items()
    ) and isinstance(value.get("requests", []), list)


def read_strict(file) -> dict:
    """The registry at ``file`` (empty when there is none). ``Unreadable`` when
    it exists and is not a valid registry."""
    try:
        text = Path(file).read_text()
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        raise Unreadable(f"{file}: {exc}") from exc
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise Unreadable(
            f"{file} is not valid JSON ({exc}); it was left as it is"
        ) from exc
    if not _valid(value):
        raise Unreadable(f"{file} is not a {SCHEMA} registry; it was left as it is")
    value.setdefault("requests", [])
    return value


def _write(file, value) -> None:
    """Temp file in the same folder, fsync, rename, fsync the folder: a crash
    leaves the old file or the new one, never half of either."""
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_name(f".{file.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temp.open("w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, file)
        with contextlib.suppress(OSError):
            fd = os.open(file.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


# --- reading (cheap: the scanner asks once per task scan) --------------------

_CACHE: dict = {}


def load(config) -> tuple[dict, str | None]:
    """``({dataset: mode}, problem)``. ``problem`` is None, or why the file
    could not be read (then every mode in the result is ``paused``, see the
    module text). Cached on the file's identity; a rename changes it."""
    file = path(config)
    try:
        st = file.stat()
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        _CACHE.pop(str(file), None)
        return {}, None
    except OSError as exc:
        return {}, f"{file}: {exc}"
    seen = _CACHE.get(str(file))
    if seen and seen[0] == stamp:
        return seen[1], seen[2]
    try:
        value = read_strict(file)
        modes = {n: r["mode"] for n, r in value["datasets"].items()}
        problem = None
    except Unreadable as exc:
        modes, problem = {}, str(exc)
    _CACHE[str(file)] = (stamp, modes, problem)
    return modes, problem


def mode_of(config, name: str) -> str:
    """The labelling mode in force for ``name``."""
    modes, problem = load(config)
    if problem:
        return "paused"
    return modes.get(name) or config.queue.default_mode


def labels_history(config, name: str) -> bool:
    """Is the skipped history of ``name`` labelled? Only for ``history`` mode,
    and only when ``[queue] history`` allows it."""
    return (
        config.queue.history == "registered_only" and mode_of(config, name) == "history"
    )


def processes(config, name: str) -> bool:
    """May ``name`` be labelled now (``auto`` or ``history``)?"""
    return mode_of(config, name) in ("auto", "history")


def rank(config, name: str) -> int:
    """Sorting rank among datasets with work: ``auto`` first (what arrives
    now), then ``history`` (the back catalogue)."""
    return {"auto": 0, "history": 1}.get(mode_of(config, name), 2)


def order_demos(config, name: str, demos) -> list:
    """``demos`` (episode folder names) in the order they are taken: newest
    first for a ``history`` dataset with ``[queue] order = newest_first``, the
    folder-name order otherwise (what the service has always done)."""
    ordered = sorted(demos)
    if labels_history(config, name) and config.queue.order == "newest_first":
        ordered.reverse()
    return ordered


# --- writing (a person's call, the supervisor registering) --------------------


def _stamp(value, now):
    value["updated_at"] = now
    return value


def register(config, names, *, now=None) -> list:
    """Register ``names`` that have no entry with ``[queue] default_mode``;
    returns the names added. Raises ``Unreadable`` (nothing written)."""
    names = [n for n in names if isinstance(n, str) and n]
    if not names:
        return []
    now = time.time() if now is None else now
    file = path(config)
    # Cheap exit without the lock: nothing to add.
    modes, problem = load(config)
    if not problem and all(n in modes for n in names):
        return []
    added: list = []
    with jsonio.locked(file):
        value = read_strict(file)
        for name in names:
            if name not in value["datasets"]:
                value["datasets"][name] = {
                    "mode": config.queue.default_mode,
                    "since": now,
                    "by": "default",
                }
                added.append(name)
        if added:
            _write(file, _stamp(value, now))
    return added


def set_mode(
    config, name: str, mode: str, *, by: str = "person", request_id=None, now=None
) -> dict:
    """Set ``name``'s mode. Idempotent: the same ``request_id`` again returns
    the first answer (``replayed``) and the same mode again changes nothing.
    ``Invalid`` for a bad mode or name, ``Conflict`` for a request id used for
    something else, ``Unreadable`` when the file is damaged."""
    if mode not in LABELLING_MODES:
        raise Invalid(f"mode must be one of {', '.join(LABELLING_MODES)}")
    if not isinstance(name, str) or not name or len(name) > 200:
        raise Invalid("not a dataset name")
    now = time.time() if now is None else now
    file = path(config)
    with jsonio.locked(file):
        value = read_strict(file)
        rows = value["datasets"]
        if request_id:
            for done in value["requests"]:
                if done.get("id") == request_id:
                    if done.get("dataset") != name or done.get("mode") != mode:
                        raise Conflict("request_id was already used for another change")
                    return {
                        "dataset": name,
                        "mode": mode,
                        "previous": done.get("previous"),
                        "changed": False,
                        "replayed": True,
                    }
        row = rows.get(name)
        previous = (row or {}).get("mode") or config.queue.default_mode
        changed = row is None or row.get("mode") != mode
        if changed:
            rows[name] = {
                "mode": mode,
                "since": now,
                "by": by if by in ACTORS else "person",
                **({"previous": previous} if row else {}),
            }
        if request_id:
            value["requests"] = [
                *value["requests"],
                {
                    "id": str(request_id),
                    "dataset": name,
                    "mode": mode,
                    "previous": previous,
                    "at": now,
                },
            ][-REQUESTS_KEPT:]
        if changed or request_id:
            _write(file, _stamp(value, now))
    return {
        "dataset": name,
        "mode": mode,
        "previous": previous,
        "changed": changed,
        "replayed": False,
    }


def listing(config) -> dict:
    """Everything a person asks for: the registry's rows and the default."""
    problem = load(config)[1]
    rows = {}
    if not problem:
        with contextlib.suppress(Unreadable):
            rows = read_strict(path(config))["datasets"]
    return {
        "default_mode": config.queue.default_mode,
        "history": config.queue.history,
        "order": config.queue.order,
        "problem": problem,
        "datasets": {
            n: {
                "mode": r["mode"],
                "since": r.get("since"),
                "by": r.get("by"),
            }
            for n, r in rows.items()
        },
    }


# --- the status file's ``queue`` ----------------------------------------------


def status_rows(
    config,
    *,
    names,
    waiting: dict,
    running,
    failed: dict,
    yielding: str | None,
    attention: str | None = None,
) -> list:
    """``[{dataset, mode, waiting, state, reason}]`` for ``names`` (sorted the
    way they are taken: ``auto`` before ``history``, then the rest).

    ``waiting`` maps a dataset to its episodes still to label; ``running`` is
    the dataset a worker is on; ``failed`` maps a dataset to the reason it is
    backing off; ``yielding`` is the text of the yield reason (None: not
    yielding); ``attention`` is a reason nothing is labelled for any dataset
    (vLLM failed repeatedly)."""
    modes, problem = load(config)
    rows = []
    for name in names:
        mode = "paused" if problem else (modes.get(name) or config.queue.default_mode)
        n = int(waiting.get(name) or 0)
        if problem:
            state, reason = "paused", "queue.json cannot be read"
        elif mode == "off":
            state, reason = "paused", "labelling is off for this dataset"
        elif mode == "paused":
            state, reason = "paused", "paused by a person; the queue is kept"
        elif name == running:
            state, reason = "running", ""
        elif name in failed:
            state, reason = "failed", failed[name]
        elif n == 0:
            state, reason = "done", ""
        elif yielding:
            state, reason = "yielding", yielding
        elif attention:
            state, reason = "queued", attention
        else:
            state, reason = "queued", ""
        rows.append(
            {
                "dataset": name,
                "mode": mode,
                "waiting": n,
                "state": state,
                "reason": reason[:200],
            }
        )
    rows.sort(
        key=lambda r: (
            {"auto": 0, "history": 1}.get(r["mode"], 2),
            r["state"] != "running",
            r["dataset"],
        )
    )
    return rows
