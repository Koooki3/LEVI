"""A cache folder with a hard size limit (``DiskBudget``). Documented in docs/PERFORMANCE.md.

Why: a cache that only grows eventually fills the disk. One full benchmark over the
robotiq episodes writes about 40 GB of evidence, three schema versions about 120 GB.
``DiskBudget`` owns one folder and keeps it inside byte and entry limits by itself, so
the limit does not depend on an outside cleaner running in time.

What it does, standard library only, no other LEVI module:

- limits: total bytes (hard) and optionally the number of entries; a write that would
  go over is made room for by evicting the least recently used entries, or is refused
  with ``BudgetRejected`` (never a crash, never a partial entry left behind);
- LRU: the file system modification time of an entry is its last use (``get`` touches
  it), the same clock ``janitor.py``'s ``lru_cap`` finder reads;
- TTL: an entry idle for longer than ``ttl_s`` is expired and never returned;
- versions: an entry is written under a version string (a schema, model or prompt
  version). Entries of any other version are stale: never returned, evicted first,
  deleted once idle for ``stale_grace_s``;
- atomic writes: data goes to a ``.tmp-<pid>-<random>`` name and is renamed into
  place; a reader sees the whole entry or none;
- crash safety: the folder itself is the truth. ``.budget.index.json`` only holds
  extras (original key, version string, per-entry TTL) and is rebuilt from the folder
  when it is missing, corrupt or disagrees. Unfinished ``.tmp-*`` and ``.trash-*``
  names of dead processes are removed on open;
- concurrency: every change runs under an exclusive ``flock`` on ``.budget.lock``
  (each call opens its own descriptor, so threads and processes exclude each other; the
  kernel drops the lock when a process dies, even by SIGKILL).

An entry is a file or a directory. Names are ``<sha256(key)[:32]>.<sha256(version)[:8]>``;
other names in the folder (dot names, foreign files) are not managed. Sizes are bytes;
the limits are given in GiB (2**30) like ``cap_gb`` in ``janitor_policy.py``.
Not covered: bytes of writes still in progress count only when they are committed, so
the folder can exceed the limit by what is being written; and nothing here knows about
other users of the same disk (``min_free_bytes`` is the only guard for that).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

GIB = 2**30
# The janitor's ``perf-cache`` entry (janitor_policy.py): 20 GB, idle 14 days; R8 audit §5: stale versions
# are deleted 7 days after the change.
DEFAULT_CAP_GB = 20
DEFAULT_TTL_S = 14 * 24 * 3600
DEFAULT_STALE_GRACE_S = 7 * 24 * 3600
DEFAULT_TMP_GRACE_S = 3600

LOCK_NAME = ".budget.lock"
INDEX_NAME = ".budget.index.json"
INDEX_FORMAT = "levi.performance.budget.v1"
_ENTRY = re.compile(r"^[0-9a-f]{32}\.[0-9a-f]{8}$")
_SCRATCH = re.compile(r"^\.(?:tmp|trash)-(\d+)-[0-9a-f]+")
_FULL = {errno.ENOSPC, errno.EDQUOT}


class BudgetRejected(Exception):
    """A write was refused. ``reason`` is one of ``too_large``, ``no_space``, ``no_output``, ``lock_timeout``."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Entry:
    name: str
    path: Path
    size: int
    mtime_ns: int
    is_dir: bool
    key: str | None  # None when the index lost it (the file name only has a hash)
    version: str | None  # likewise
    stale: bool
    expired: bool


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def version_hash(version: str) -> str:
    return hashlib.sha256(version.encode("utf-8")).hexdigest()[:8]


def entry_name(key: str, version: str) -> str:
    return f"{key_hash(key)}.{version_hash(version)}"


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _tree_size(path: Path) -> int:
    """Bytes of a file, or of everything under a directory (symlinks are counted, not followed)."""
    try:
        st = path.lstat()
    except OSError:
        return 0
    if not path.is_dir() or path.is_symlink():
        return st.st_size
    total = 0
    stack = [path]
    while stack:
        with contextlib.suppress(OSError), os.scandir(stack.pop()) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                else:
                    with contextlib.suppress(OSError):
                        total += e.stat(follow_symlinks=False).st_size
    return total


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _fsync_path(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) if path.is_dir() else os.O_RDONLY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                p = Path(dirpath) / f
                if not p.is_symlink():
                    _fsync_path(p)
            _fsync_path(Path(dirpath))
    else:
        _fsync_path(path)


class DiskBudget:
    """One cache folder kept inside ``max_bytes`` / ``max_entries``. See the module docstring."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_bytes: int,
        max_entries: int | None = None,
        ttl_s: float | None = DEFAULT_TTL_S,
        version: str = "",
        stale_grace_s: float = DEFAULT_STALE_GRACE_S,
        min_free_bytes: int = 0,
        tmp_grace_s: float = DEFAULT_TMP_GRACE_S,
        lock_timeout_s: float = 60.0,
        durable: bool = True,
        clock: Callable[[], float] = time.time,
        recover: bool = True,
    ):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_entries is not None and max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self.version = version
        self.stale_grace_s = stale_grace_s
        self.min_free_bytes = int(min_free_bytes)
        self.tmp_grace_s = tmp_grace_s
        self.lock_timeout_s = lock_timeout_s
        self.durable = durable
        self.clock = clock
        self.root.mkdir(parents=True, exist_ok=True)
        if recover:
            self.maintain()

    @classmethod
    def from_cap_gb(cls, root: str | os.PathLike[str], cap_gb: float = DEFAULT_CAP_GB, **kwargs) -> DiskBudget:
        """The limit in GiB, the unit of ``cap_gb`` in ``janitor_policy.py``."""
        return cls(root, max_bytes=int(cap_gb * GIB), **kwargs)

    # ------------------------------------------------------------------ lock

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        fd = os.open(self.root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.lock_timeout_s
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BudgetRejected("lock_timeout", str(self.root / LOCK_NAME)) from None
                    time.sleep(0.005)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the flock

    # ----------------------------------------------------------------- index

    def _load_index(self) -> dict[str, dict]:
        try:
            doc = json.loads((self.root / INDEX_NAME).read_text("utf-8"))
            items = doc["entries"]
            if doc.get("format") != INDEX_FORMAT or not isinstance(items, dict):
                return {}
            return {k: v for k, v in items.items() if isinstance(v, dict)}
        except (OSError, ValueError, KeyError, TypeError):
            return {}  # missing or corrupt: rebuilt from the folder

    def _save_index(self, index: dict[str, dict]) -> None:
        tmp = self.root / f".tmp-{os.getpid()}-{uuid.uuid4().hex}-index"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"format": INDEX_FORMAT, "entries": index}, f, sort_keys=True)
                f.flush()
                if self.durable:
                    os.fsync(f.fileno())
            os.replace(tmp, self.root / INDEX_NAME)
            if self.durable:
                _fsync_path(self.root)
        except OSError:
            _remove(tmp)  # the index is only a convenience: losing a write loses nothing

    # ------------------------------------------------------------------ scan

    def _scan(self, index: dict[str, dict]) -> list[Entry]:
        """The managed entries as they are on disk, joined with what the index remembers about them."""
        now_ns = int(self.clock() * 1e9)
        current = version_hash(self.version)
        out = []
        try:
            names = [e for e in os.scandir(self.root) if _ENTRY.match(e.name)]
        except OSError:
            return out
        for e in names:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            meta = index.get(e.name, {})
            if is_dir:
                size = meta["size"] if isinstance(meta.get("size"), int) else _tree_size(Path(e.path))
            else:
                size = st.st_size
            ttl = meta.get("ttl_s", self.ttl_s)
            if ttl is not None and not isinstance(ttl, (int, float)):
                ttl = self.ttl_s
            expired = ttl is not None and (now_ns - st.st_mtime_ns) / 1e9 > ttl
            out.append(
                Entry(
                    name=e.name,
                    path=Path(e.path),
                    size=size,
                    mtime_ns=st.st_mtime_ns,
                    is_dir=is_dir,
                    key=meta.get("key") if isinstance(meta.get("key"), str) else None,
                    version=meta.get("version") if isinstance(meta.get("version"), str) else None,
                    stale=e.name.split(".", 1)[1] != current,
                    expired=expired,
                )
            )
        return out

    def _index_of(self, entries: list[Entry], old: dict[str, dict]) -> dict[str, dict]:
        new = {}
        for e in entries:
            meta = {"size": e.size}
            for field in ("key", "version"):
                if getattr(e, field) is not None:
                    meta[field] = getattr(e, field)
            if "ttl_s" in old.get(e.name, {}):
                meta["ttl_s"] = old[e.name]["ttl_s"]
            new[e.name] = meta
        return new

    # ---------------------------------------------------------------- removal

    def _delete(self, entry: Entry) -> None:
        """Rename away first: a crash while a directory is half deleted must not leave a valid-looking entry."""
        trash = self.root / f".trash-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            os.rename(entry.path, trash)
        except FileNotFoundError:
            return
        except OSError:
            _remove(entry.path)
            return
        _remove(trash)

    def _clean_scratch(self) -> int:
        """Remove unfinished writes and half-deleted entries whose process is gone (or that are very old)."""
        removed = 0
        now = self.clock()
        for e in list(os.scandir(self.root)):
            m = _SCRATCH.match(e.name)
            if not m:
                continue
            try:
                age = now - e.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if _alive(int(m.group(1))) and age <= self.tmp_grace_s:
                continue
            _remove(Path(e.path))
            removed += 1
        return removed

    # --------------------------------------------------------------- maintain

    def maintain(self) -> dict[str, int]:
        """Startup check, safe to call any time: remove dead writes, expired and long-stale entries, and make the
        index agree with the folder. Returns counts."""
        with self._lock():
            orphans = self._clean_scratch()
            index = self._load_index()
            entries = self._scan(index)
            keep, expired, stale = [], 0, 0
            for e in entries:
                if e.expired:
                    expired += 1
                    self._delete(e)
                elif e.stale and (self.clock() - e.mtime_ns / 1e9) > self.stale_grace_s:
                    stale += 1
                    self._delete(e)
                else:
                    keep.append(e)
            new = self._index_of(keep, index)
            rebuilt = new != index
            if rebuilt:
                self._save_index(new)
            return {"orphans": orphans, "expired": expired, "stale": stale, "index_rebuilt": int(rebuilt)}

    # ------------------------------------------------------------------ write

    def _fits(self, entries: list[Entry], size: int) -> list[Entry]:
        """Entries to delete, stale before valid and oldest use first, so that ``size`` more bytes (and one more
        entry) fit. Raises ``BudgetRejected`` when even an empty folder could not hold it."""
        if size > self.max_bytes:
            raise BudgetRejected("too_large", f"{size} bytes > limit {self.max_bytes}")
        order = sorted(entries, key=lambda e: (not e.stale, e.mtime_ns, e.name))
        total = sum(e.size for e in entries)
        count = len(entries)
        victims = []
        for e in order:
            if total + size <= self.max_bytes and (self.max_entries is None or count + 1 <= self.max_entries):
                break
            victims.append(e)
            total -= e.size
            count -= 1
        return victims

    def _commit(self, key: str, tmp: Path, version: str, ttl_s: float | None) -> Entry:
        if not tmp.exists() and not tmp.is_symlink():
            raise BudgetRejected("no_output", "the writer created nothing")
        if self.durable:
            _fsync_tree(tmp)
        size = _tree_size(tmp)
        name = entry_name(key, version)
        kh = name.split(".", 1)[0]
        with self._lock():
            index = self._load_index()
            entries = self._scan(index)
            replaced = [e for e in entries if e.name.split(".", 1)[0] == kh]
            others = [e for e in entries if e not in replaced]
            doomed = [e for e in others if e.expired]
            others = [e for e in others if not e.expired]
            victims = self._fits(others, size)
            if self.min_free_bytes:
                reclaim = sum(e.size for e in (*replaced, *doomed, *victims))
                free = _free_bytes(self.root)
                if free + reclaim - size < self.min_free_bytes:
                    raise BudgetRejected(
                        "no_space", f"free {free} + reclaimable {reclaim} - {size} < floor {self.min_free_bytes}"
                    )
            for e in (*replaced, *doomed, *victims):
                self._delete(e)
            dest = self.root / name
            os.rename(tmp, dest)
            if self.durable:
                _fsync_path(self.root)
            now = self.clock()
            os.utime(dest, ns=(int(now * 1e9), int(now * 1e9)), follow_symlinks=False)
            keep = [e for e in others if e not in victims]
            meta = self._index_of(keep, index)
            meta[name] = {"size": size, "key": key, "version": version}
            if ttl_s is not None:
                meta[name]["ttl_s"] = ttl_s
            self._save_index(meta)
            return Entry(name, dest, size, int(now * 1e9), dest.is_dir(), key, version, False, False)

    @contextlib.contextmanager
    def _scratch(self, expected_bytes: int | None) -> Iterator[Path]:
        if expected_bytes is not None and expected_bytes > self.max_bytes:
            raise BudgetRejected("too_large", f"{expected_bytes} bytes > limit {self.max_bytes}")
        tmp = self.root / f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            yield tmp
        except OSError as exc:
            if exc.errno in _FULL:
                raise BudgetRejected("no_space", str(exc)) from exc
            raise
        finally:
            _remove(tmp)  # after a successful rename there is nothing at this name

    @contextlib.contextmanager
    def writing(
        self, key: str, *, version: str | None = None, ttl_s: float | None = None, expected_bytes: int | None = None
    ) -> Iterator[Path]:
        """Write one entry: the block gets a scratch path that does not exist yet; create a file or a directory
        there. On a clean exit it is made room for and renamed into place atomically, replacing every older entry of
        the same key. Any exception (or ``BudgetRejected``) leaves nothing behind. ``expected_bytes`` refuses an
        oversized write before it starts."""
        version = self.version if version is None else version
        with self._scratch(expected_bytes) as tmp:
            yield tmp
            self._commit(key, tmp, version, ttl_s)

    def put(self, key: str, data: bytes, *, version: str | None = None, ttl_s: float | None = None) -> Entry:
        """Store ``data`` under ``key``. Raises ``BudgetRejected`` when it cannot fit or the disk is full."""
        version = self.version if version is None else version
        with self._scratch(len(data)) as tmp:
            with open(tmp, "wb") as f:
                f.write(data)
            return self._commit(key, tmp, version, ttl_s)

    # ------------------------------------------------------------------- read

    def get(self, key: str, *, version: str | None = None) -> Path | None:
        """The path of a live entry, or ``None`` (missing, expired or of another version). A hit counts as a use.
        The path can be evicted by another process afterwards; ``read`` returns the bytes safely."""
        version = self.version if version is None else version
        path = self.root / entry_name(key, version)
        with self._lock():
            return self._touch(path)

    def _touch(self, path: Path) -> Path | None:
        try:
            st = path.lstat()
        except OSError:
            return None
        ttl = self.ttl_s
        meta = self._load_index().get(path.name, {})
        if isinstance(meta.get("ttl_s"), (int, float)):
            ttl = meta["ttl_s"]
        now = self.clock()
        if ttl is not None and now - st.st_mtime_ns / 1e9 > ttl:
            self._delete(
                Entry(path.name, path, 0, st.st_mtime_ns, path.is_dir(), None, None, False, True)
            )
            return None
        os.utime(path, ns=(int(now * 1e9), int(now * 1e9)), follow_symlinks=False)
        return path

    def read(self, key: str, *, version: str | None = None) -> bytes | None:
        """The bytes of a file entry, or ``None``. The descriptor is opened under the lock, so an eviction right
        afterwards cannot tear the read."""
        version = self.version if version is None else version
        path = self.root / entry_name(key, version)
        with self._lock():
            if self._touch(path) is None or path.is_dir():
                return None
            f = open(path, "rb")  # noqa: SIM115 - closed below, outside the lock
        with f:
            return f.read()

    def discard(self, key: str) -> int:
        """Delete every version of ``key``; returns how many entries went."""
        kh = key_hash(key)
        with self._lock():
            index = self._load_index()
            entries = self._scan(index)
            gone = [e for e in entries if e.name.split(".", 1)[0] == kh]
            for e in gone:
                self._delete(e)
            self._save_index(self._index_of([e for e in entries if e not in gone], index))
            return len(gone)

    # ------------------------------------------------------------------ views

    def entries(self) -> list[Entry]:
        with self._lock():
            return sorted(self._scan(self._load_index()), key=lambda e: (e.mtime_ns, e.name))

    def purge_stale(self) -> int:
        """Delete every entry of another version now (the grace period is only for the automatic path)."""
        with self._lock():
            index = self._load_index()
            entries = self._scan(index)
            gone = [e for e in entries if e.stale]
            for e in gone:
                self._delete(e)
            self._save_index(self._index_of([e for e in entries if e not in gone], index))
            return len(gone)

    def stats(self) -> dict[str, int]:
        with self._lock():
            entries = self._scan(self._load_index())
        scratch = sum(1 for e in os.scandir(self.root) if e.name.startswith(".tmp-"))
        return {
            "bytes": sum(e.size for e in entries),
            "entries": len(entries),
            "max_bytes": self.max_bytes,
            "max_entries": self.max_entries or 0,
            "stale": sum(e.stale for e in entries),
            "expired": sum(e.expired for e in entries),
            "writes_in_progress": scratch,
        }
