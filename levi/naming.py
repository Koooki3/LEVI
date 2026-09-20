"""Human-readable, hash-free names for everything LEVI writes.

Two rules (see .state.md): artifacts kept one-per-dataset and overwritten in
place use the bare dataset name; artifacts created once per run use a
timestamp. A timestamp suffix is appended to a dataset name only when two
different things would otherwise collide.
"""

import os
import re
from datetime import UTC, datetime
from pathlib import Path

# Minute precision, with the reservation counter for a same-minute clash.
# Finer digits read as an opaque suffix and tell a person nothing; several
# artifacts made in one minute are told apart by -2, -3… which is readable.
TIMESTAMP_PATTERN = r"\d{8}-\d{4}(?:-\d+)?"
LEGACY_TIMESTAMP_PATTERN = r"\d{8}-\d{6}(?:-\d{3})?(?:-\d+)?"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M")


def timestamp_id(
    directory: Path, suffix: str = "", *, prefix: str = "", create_dir: bool = False
) -> str:
    """A fresh ``YYYYmmdd-HHMM`` id, reserved on disk.

    Several LEVI processes may share one workspace, so an in-process counter
    can't prevent two runs picking the same minute. The id is claimed by
    exclusively creating ``<directory>/<prefix><id><suffix>`` (a file, or a
    directory when ``create_dir``); on a clash ``-1``, ``-2``… is appended.
    Returns only the id part.
    """
    directory.mkdir(parents=True, exist_ok=True)
    base = _now()
    for attempt in range(1000):
        value = base if attempt == 0 else f"{base}-{attempt}"
        path = directory / f"{prefix}{value}{suffix}"
        try:
            if create_dir:
                path.mkdir()
            else:
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            continue
        return value
    raise RuntimeError(f"Could not reserve a unique id in {directory}")


def catalog_name(text: str) -> str:
    """A URL- and filename-safe dataset name: ASCII letters, digits, ``._-``.

    Non-ASCII characters (e.g. a Chinese folder name) become ``_`` — safe in
    ``/api/levi/files/<name>/…`` and Next.js route params — and a name made
    only of dots, or starting with one, is rejected so it can never mean a
    relative path.
    """
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    name = name.lstrip(".")
    return name or "dataset"


def unique_name(base: str, taken) -> str:
    """``base`` if free, else ``base_<timestamp>`` — only on a real clash."""
    if base not in taken:
        return base
    candidate = f"{base}_{_now()}"
    attempt = 0
    while candidate in taken:
        attempt += 1
        candidate = f"{base}_{_now()}-{attempt}"
    return candidate


def is_timestamp_id(value: str) -> bool:
    return bool(re.fullmatch(TIMESTAMP_PATTERN, value))


def hub_cache_directory(
    cache_root: Path, repo_id: str, revision: str, scope: str
) -> Path:
    """Account-isolated cache with readable paths; fingerprints stay in metadata.

    Existing digest-named caches are deliberately neither deleted nor shared.
    Slots are allocated under a cross-process lock, with no token stored.
    """
    import fcntl
    import json

    root = cache_root / repo_id.replace("/", "__")
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index = root / "cache-index.json"
        records = json.loads(index.read_text()) if index.exists() else []
        for row in records:
            if row["scope"] == scope and row["revision"] == revision:
                return root / row["directory"]
        scopes = list(dict.fromkeys(row["scope"] for row in records))
        account = scopes.index(scope) + 1 if scope in scopes else len(scopes) + 1
        version = sum(row["scope"] == scope for row in records) + 1
        directory = f"account-{account:04d}/revision-{version:04d}"
        records.append({"scope": scope, "revision": revision, "directory": directory})
        temporary = index.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, indent=2))
        os.replace(temporary, index)
        return root / directory
