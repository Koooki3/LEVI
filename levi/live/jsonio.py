"""Small JSON state files shared by the supervisor, its worker and the API.

Reads never block. Writes are atomic (temp file + rename) and, for
read-modify-write, serialised by an ``flock`` on a sibling ``.lock`` file, so
two processes updating one file cannot lose each other's change.
"""

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write(path, value, *, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False)
        )
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


@contextlib.contextmanager
def locked(path):
    """Exclusive cross-process lock named after ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def update(path, change, default=None):
    """Read, apply ``change(value) -> value|None`` and write back under the
    lock; returns the stored value. ``change`` may edit in place and return
    nothing."""
    with locked(path):
        value = read(path, default() if callable(default) else default)
        result = change(value)
        value = value if result is None else result
        write(path, value, indent=1)
        return value


def append_line(path, record, *, max_bytes=None, keep=3):
    """Append one JSON line (an audit or event log). With ``max_bytes`` the
    file rotates to ``.1``, ``.2``... so it never grows without bound."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if max_bytes and path.exists() and path.stat().st_size > max_bytes:
        for n in range(keep - 1, 0, -1):
            older = path.with_name(f"{path.name}.{n}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{n + 1}"))
        os.replace(path, path.with_name(path.name + ".1"))
        with contextlib.suppress(OSError):
            path.with_name(f"{path.name}.{keep + 1}").unlink()
    line = json.dumps(record, ensure_ascii=False, allow_nan=False, default=str)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(line + "\n")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
