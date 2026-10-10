"""A cache folder with a hard size limit (``DiskBudget``). Documented in docs/PERFORMANCE.md.

Why: a cache that only grows eventually fills the disk. One full benchmark over a few
thousand episodes writes tens of GB of evidence. ``DiskBudget`` owns one folder and keeps
it inside byte and entry limits by itself, so the limit does not depend on an outside
cleaner running in time.

What it does, standard library only, no other LEVI module:

- limits: total bytes and optionally the number of entries. The limit covers what is
  committed *and* what writers have reserved: a writer asks for room before it writes
  (``put`` reserves ``len(data)``, ``writing`` the ``reserve`` it is given), least recently
  used entries are evicted to make it, and a write that cannot be given room is refused
  with ``BudgetRejected`` before any data is written. Writes through ``open_file`` stop at
  the reserve; raw writes into the scratch folder are caught at commit (``charge`` checks
  on demand). So the folder never holds more than ``max_bytes`` plus a few KiB of
  bookkeeping (lock, writer records), whatever the number of writers;
- an entry is a directory ``<key hash>.<version hash>.<generation>.<size>`` holding the
  payload and a small ``.meta`` file. The name carries everything the accounting needs,
  so there is no index to go out of step: the folder is the truth. Entries are immutable
  once committed; a rewrite of a key is a new generation, and the older generation stays
  until nobody reads it any more;
- LRU: an entry's last use is the modification time of the entry directory and of its
  ``.meta`` file (``get`` refreshes both, so a tool that looks only at files sees the use);
- TTL: an entry idle for longer than ``ttl_s`` is expired and never returned;
- versions: an entry is written under a version string (a schema, model or prompt
  version). Entries of any other version are stale: never returned, evicted first, deleted
  once idle for ``stale_grace_s``;
- reads: ``get`` returns a ``Lease`` (a shared ``flock`` on the entry). A leased entry is
  never evicted, replaced or deleted by this class, so a reader sees one immutable entry
  for as long as it holds the lease;
- atomic writes: data is written in a private folder under ``.budget/w`` and renamed into
  place once; a reader sees the whole entry or none. ``durable=True`` (default) fsyncs the
  data first and a failing fsync aborts the commit;
- crash safety: every writer holds an exclusive ``flock`` on its record for as long as it
  lives, so a record whose lock can be taken belongs to a dead writer (also after a
  SIGKILL, and immune to pid reuse and to clock or mtime games) and is reclaimed, with its
  data. A live writer is never reclaimed. Deletion renames to ``.budget/trash`` first, so a
  crash mid-delete never leaves a half-empty entry that looks valid; an entry whose
  ``.meta`` is missing (deleted in place by some other tool) is dropped;
- concurrency: every change runs under an exclusive ``flock`` on the cache folder itself;
  threads and processes exclude each other, the kernel drops the lock when a process dies.
  There is no lock file a cleaner could delete;

Layout: ``<root>/<entry>/…`` for entries and ``<root>/.budget/`` (writer records, trash) for
everything else. A cleaner that works on the cache folder must skip names starting with
``.``. Sizes are bytes; limits given in GiB (``from_cap_gb``) are 2**30.

Limits of the design. Cost grows with the number of entries (every write and read lists the
folder): fine for thousands of entries (one per episode), not for one per frame. Raw writes
into the scratch folder are not metered while they happen (use ``open_file``/``charge`` when
the size is not known). ``min_free_bytes`` is the only guard against other users of the same
disk. Local file systems only (it relies on ``flock`` and ``rename``). Creating an instance scans the whole
folder once (``recover=True``); short-lived processes can pass ``recover=False`` and leave ``maintain()`` to one
long-lived process. A forked child drops its copies of lock descriptors at once, so it never keeps a lease or a
writer alive.

Idle time is enforced when the cache is used (any call, or ``maintain()``). A cache that nobody opens any more is
not touched by this class; ``claim_abandoned``/``is_abandoned`` let an outside cleaner find out safely whether the
whole folder can be reclaimed.
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
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Self

GIB = 2**30
# The quota of the performance evidence cache: 20 GiB, idle 14 days, stale versions deleted after 7 days.
DEFAULT_CAP_GB = 20
DEFAULT_TTL_S = 14 * 24 * 3600
DEFAULT_STALE_GRACE_S = 7 * 24 * 3600
DEFAULT_HOUSEKEEPING_S = 600
META_ALLOWANCE = 256  # a typical ``.meta`` size: only used to size defaults, reservations use the real size
META_MAX = 4096  # longest ``.meta`` accepted (key and version are stored whole)

BUDGET_DIR = ".budget"
META_NAME = ".meta"
_ENTRY = re.compile(r"^([0-9a-f]{32})\.([0-9a-f]{16})\.([0-9a-f]{20})\.([0-9a-f]+)$")
_FULL = {errno.ENOSPC, errno.EDQUOT}


class BudgetRejected(Exception):
    """A write was refused. ``reason``: ``too_large`` (could never fit), ``busy`` (does not fit while other
    writers or readers hold their share), ``no_space`` (the disk is or would be too full), ``no_output`` (nothing
    or something unusable was written), ``reclaimed`` (the scratch folder was removed under the writer),
    ``unavailable`` (the folder cannot be used), ``lock_timeout``."""

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
    key_hash: str
    version_hash: str
    generation: str
    stale: bool  # written under another version
    superseded: bool  # an older generation of a key that has a newer one
    expired: bool  # idle longer than the TTL
    key: str | None = None  # from ``.meta``; only filled by ``DiskBudget.entries()``
    version: str | None = None

    @property
    def dead(self) -> bool:
        """Never returned by ``get``."""
        return self.stale or self.superseded or self.expired


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def version_hash(version: str) -> str:
    return hashlib.sha256(version.encode("utf-8")).hexdigest()[:16]


def meta_bytes(key: str, version: str) -> bytes:
    """The content of an entry's ``.meta``: the whole key and version string, UTF-8."""
    return json.dumps({"key": key, "version": version}, ensure_ascii=False).encode("utf-8")


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _tree_size(path: Path) -> int:
    """Bytes under ``path`` (a directory). Symbolic links are refused: they would take bytes outside the budget."""
    total = 0
    stack = [path]
    while stack:
        with os.scandir(stack.pop()) as it:
            for e in it:
                if e.is_symlink():
                    raise BudgetRejected("no_output", f"symbolic link in the entry: {e.name}")
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                else:
                    total += e.stat(follow_symlinks=False).st_size
    return total


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _fsync(path: Path, *, directory: bool) -> None:
    """fsync a file (any error is raised) or a directory (only 'not supported' is ignored)."""
    flags = os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if directory else 0)
    fd = os.open(path, flags)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if not (directory and exc.errno in (errno.EINVAL, errno.EBADF, errno.ENOTSUP)):
                raise
    finally:
        os.close(fd)


def _fsync_tree(path: Path) -> None:
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            _fsync(Path(dirpath) / f, directory=False)
        _fsync(Path(dirpath), directory=True)


_open_fds: set[int] = set()  # descriptors that carry a lock, closed in a forked child (see _after_fork)


def _track(fd: int) -> int:
    _open_fds.add(fd)
    return fd


def _untrack_close(fd: int) -> None:
    _open_fds.discard(fd)
    os.close(fd)


def _after_fork_in_child() -> None:
    """A child must not inherit a lock: the lock would stay held until the child exits. Closing the child's copy
    of the descriptor does not release the parent's lock."""
    for fd in list(_open_fds):
        with contextlib.suppress(OSError):
            os.close(fd)
    _open_fds.clear()


os.register_at_fork(after_in_child=_after_fork_in_child)


class Lease:
    """A shared read lock on one committed entry. While it is held the entry is not evicted, replaced or deleted
    by ``DiskBudget``. ``path`` is the entry folder; ``close()`` (or ``with``) releases it."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self._fd = fd

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            _untrack_close(fd)  # closing the descriptor releases the flock

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:  # a forgotten lease must not block eviction for ever
        with contextlib.suppress(Exception):
            self.close()


class _Metered:
    """A binary file inside a writer's scratch folder; refuses bytes beyond the writer's reservation."""

    def __init__(self, writer: _Writer, raw):
        self._writer = writer
        self._raw = raw

    def write(self, data) -> int:
        n = memoryview(data).nbytes
        self._writer.charge_bytes(n)
        return self._raw.write(data)

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self._raw.close()


class _Writer:
    """One reservation plus its scratch folder. The lock file ``w/<id>.lock`` is held exclusively until ``close``."""

    def __init__(self, budget: DiskBudget, key: str, version: str, reserve: int, meta: bytes):
        self.budget = budget
        self.key = key
        self.version = version
        self.reserve = reserve  # payload bytes asked for
        self.meta = meta  # what ``.meta`` will hold; its size is part of the reservation
        self.id = f"{os.getpid()}-{uuid.uuid4().hex}"
        self.container = budget._w / self.id
        self.path = self.container / "p"
        self.lock_path = budget._w / f"{self.id}.lock"
        self.fd = -1
        self.metered = 0
        self._mutex = threading.Lock()

    @property
    def allowed(self) -> int:
        return self.reserve + len(self.meta)

    def charge_bytes(self, n: int) -> None:
        with self._mutex:
            if self.metered + n > self.reserve:
                raise BudgetRejected("too_large", f"{self.metered + n} bytes written > reserved {self.reserve}")
            self.metered += n

    def close(self) -> None:
        """Remove the data first, then the record: a record without data is harmless, data without a record is
        garbage that the next housekeeping removes."""
        with self.budget._writers_mutex:
            self.budget._writers.pop(self.id, None)
        _remove(self.container)
        with contextlib.suppress(OSError):
            self.lock_path.unlink()
        if self.fd >= 0:
            _untrack_close(self.fd)
            self.fd = -1


class _Locked:
    """What a ``with self._lock()`` block collects: trash folders to remove once the lock is released."""

    def __init__(self):
        self.trash: list[Path] = []


class DiskBudget:
    """One cache folder kept inside ``max_bytes`` / ``max_entries``. See the module docstring."""

    # A test seam: called with the name of a step of commit/delete (the tests kill the process there).
    _hook: Callable[[str], None] = staticmethod(lambda point: None)

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
        default_reserve: int | None = None,
        housekeeping_s: float = DEFAULT_HOUSEKEEPING_S,
        lock_timeout_s: float = 60.0,
        durable: bool = True,
        clock: Callable[[], float] = time.time,
        recover: bool = True,
    ):
        if max_bytes <= META_ALLOWANCE:
            raise ValueError(f"max_bytes must exceed {META_ALLOWANCE}")
        if max_entries is not None and max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self.version = version
        self.stale_grace_s = stale_grace_s
        self.min_free_bytes = int(min_free_bytes)
        # ``writing()`` without ``reserve``: a quarter of the budget, so one unsized writer cannot starve the rest.
        self.default_reserve = (
            int(default_reserve) if default_reserve is not None else max(1, (self.max_bytes - META_ALLOWANCE) // 4)
        )
        self.housekeeping_s = housekeeping_s
        self.lock_timeout_s = lock_timeout_s
        self.durable = durable
        self.clock = clock
        self._meta = self.root / BUDGET_DIR
        self._w = self._meta / "w"
        self._trash = self._meta / "trash"
        self._writers: dict[str, _Writer] = {}
        self._writers_mutex = threading.Lock()
        self._last_housekeeping = float("-inf")
        self._ensure_dirs()
        if recover:
            self.maintain()

    @classmethod
    def from_cap_gb(cls, root: str | os.PathLike[str], cap_gb: float = DEFAULT_CAP_GB, **kwargs) -> DiskBudget:
        """The limit in GiB (2**30 bytes)."""
        return cls(root, max_bytes=int(cap_gb * GIB), **kwargs)

    def _ensure_dirs(self) -> None:
        try:
            self._w.mkdir(parents=True, exist_ok=True)
            self._trash.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BudgetRejected("unavailable", f"{self.root}: {exc}") from exc

    # ------------------------------------------------------------------ lock

    @contextlib.contextmanager
    def _lock(self) -> Iterator[_Locked]:
        """The exclusive lock is a ``flock`` on the cache folder itself, not on a file inside it: a cleaner may
        delete files and subfolders, but not the folder the cache lives in."""
        ctx = _Locked()
        deadline = time.monotonic() + self.lock_timeout_s
        fd = -1
        try:
            while True:
                self._ensure_dirs()
                try:
                    fd = _track(os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
                except OSError as exc:
                    raise BudgetRejected("unavailable", f"{self.root}: {exc}") from exc
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    _untrack_close(fd)
                    fd = -1
                    if time.monotonic() >= deadline:
                        raise BudgetRejected("lock_timeout", str(self.root)) from None
                    time.sleep(0.005)
                    continue
                # The folder may have been deleted and recreated while we waited: a lock on the old one excludes
                # nobody, so lock the one that is there now.
                try:
                    same = os.fstat(fd).st_ino == os.stat(self.root).st_ino
                except OSError:
                    same = False
                if same:
                    break
                _untrack_close(fd)
                fd = -1
            yield ctx
        finally:
            if fd >= 0:
                _untrack_close(fd)  # closing the descriptor releases the flock
            for path in ctx.trash:
                _remove(path)

    # ------------------------------------------------------------------ scan

    def _scan(self, *, now: float | None = None) -> list[Entry]:
        now_ns = int((self.clock() if now is None else now) * 1e9)
        current = version_hash(self.version)
        raw = []
        try:
            it = list(os.scandir(self.root))
        except OSError as exc:
            raise BudgetRejected("unavailable", f"{self.root}: {exc}") from exc
        for e in it:
            m = _ENTRY.match(e.name)
            if not m or not e.is_dir(follow_symlinks=False):  # a plain file with an entry's name is not ours
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            raw.append((e, m, st))
        newest: dict[tuple[str, str], str] = {}
        for _e, m, _st in raw:
            k = (m[1], m[2])
            newest[k] = max(newest.get(k, ""), m[3])
        out = []
        for e, m, st in raw:
            idle = (now_ns - st.st_mtime_ns) / 1e9
            out.append(
                Entry(
                    name=e.name,
                    path=Path(e.path),
                    size=int(m[4], 16),
                    mtime_ns=st.st_mtime_ns,
                    key_hash=m[1],
                    version_hash=m[2],
                    generation=m[3],
                    stale=m[2] != current,
                    superseded=newest[(m[1], m[2])] != m[3],
                    expired=self.ttl_s is not None and idle > self.ttl_s,
                )
            )
        return out

    def _live_writers(self) -> list[tuple[str, int]]:
        """(id, reserved bytes incl. allowance) of every writer whose lock is held; dead ones are reclaimed."""
        out = []
        for e in list(os.scandir(self._w)):
            if not e.name.endswith(".lock"):
                continue
            wid = e.name[: -len(".lock")]
            try:
                fd = os.open(e.path, os.O_RDWR)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    try:
                        reserved = int(json.loads(os.pread(fd, 4096, 0).decode() or "{}").get("reserved", 0))
                    except (ValueError, TypeError):
                        reserved = 0
                    out.append((wid, reserved))
                    continue
                # the writer is gone: its data and its record go with it
                _remove(self._w / wid)
                with contextlib.suppress(OSError):
                    os.unlink(e.path)
            finally:
                os.close(fd)
        # scratch folders nobody has a record for (the record was deleted from outside)
        records = {e.name for e in os.scandir(self._w) if e.name.endswith(".lock")}
        for e in list(os.scandir(self._w)):
            if not e.name.endswith(".lock") and f"{e.name}.lock" not in records:
                _remove(Path(e.path))
        return out

    def _trash_bytes(self) -> int:
        """Bytes still on disk in the trash (entries renamed away whose removal has not finished)."""
        total = 0
        for e in os.scandir(self._trash):
            with contextlib.suppress(ValueError, IndexError):
                total += int(e.name.rsplit(".", 1)[1], 16)
        return total

    # ---------------------------------------------------------------- removal

    def _try_delete(self, entry: Entry, ctx: _Locked) -> bool:
        """Delete an entry unless somebody holds a lease on it. Renamed to trash first (removed after the lock)."""
        try:
            fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            trash = self._trash / f"{uuid.uuid4().hex}.{entry.size:x}"
            try:
                os.rename(entry.path, trash)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            ctx.trash.append(trash)
            self._hook("delete:after_trash_rename")
            return True
        finally:
            os.close(fd)

    def _housekeep(self, ctx: _Locked, entries: list[Entry] | None = None, *, force: bool = False) -> dict[str, int]:
        """Dead writers, orphan trash, expired entries, stale ones past the grace period, replaced generations."""
        counts = {"writers": 0, "trash": 0, "expired": 0, "stale": 0, "superseded": 0, "damaged": 0}
        if not force and time.monotonic() - self._last_housekeeping < self.housekeeping_s:
            return counts
        self._last_housekeeping = time.monotonic()
        before = len(list(self._w.glob("*.lock")))
        self._live_writers()
        counts["writers"] = before - len(list(self._w.glob("*.lock")))
        for e in list(os.scandir(self._trash)):  # under the lock: nobody is mid-delete, so all of it is garbage
            _remove(Path(e.path))
            counts["trash"] += 1
        now = self.clock()
        for e in entries if entries is not None else self._scan():
            if e.stale and not e.expired and (now - e.mtime_ns / 1e9) <= self.stale_grace_s:
                continue
            if not (e.expired or e.stale or e.superseded):
                continue
            if self._try_delete(e, ctx):
                counts["expired" if e.expired else "stale" if e.stale else "superseded"] += 1
        return counts

    # --------------------------------------------------------------- reserve

    def _reclaim(
        self,
        ctx: _Locked,
        entries: list[Entry],
        reserved: int,
        reserved_n: int,
        need: int,
        need_n: int,
        *,
        first: str | None = None,
        floor_check: bool = False,
    ) -> None:
        """Delete entries (dead ones, entries of ``first``'s key, then least recently used) until ``need`` bytes
        and ``need_n`` entries fit next to what is kept and ``reserved``. Leased entries stay. Raises
        ``BudgetRejected`` (``busy``/``no_space``) when it cannot be done."""
        order = sorted(
            entries,
            key=lambda e: (not (e.dead or (first is not None and e.key_hash == first)), e.mtime_ns, e.name),
        )
        total = sum(e.size for e in entries) + reserved + need + self._trash_bytes()
        count = len(entries) + reserved_n + need_n

        def fits() -> bool:
            return total <= self.max_bytes and (self.max_entries is None or count <= self.max_entries)

        victims = []
        left_total, left_count = total, count
        for e in order:
            if left_total <= self.max_bytes and (self.max_entries is None or left_count <= self.max_entries):
                break
            victims.append(e)
            left_total -= e.size
            left_count -= 1
        if floor_check and self.min_free_bytes:
            free = _free_bytes(self.root)
            # other writers have reserved bytes they may not have written yet: leave those free too
            back = sum(e.size for e in victims)
            if free + back - reserved - need < self.min_free_bytes:
                raise BudgetRejected(
                    "no_space",
                    f"free {free} + reclaimable {back} - others' reservations {reserved} - {need} < floor "
                    f"{self.min_free_bytes}",
                )
        for e in order:
            if fits():
                break
            if self._try_delete(e, ctx):  # False: leased, it stays and the next one goes instead
                total -= e.size
                count -= 1
        if not fits():
            raise BudgetRejected("busy", "other writers' reservations and readers' leases hold the rest of the budget")

    def _begin(self, key: str, version: str, reserve: int) -> _Writer:
        if reserve < 0:
            raise ValueError("reserve must not be negative")
        meta = meta_bytes(key, version)
        if len(meta) > META_MAX:
            raise BudgetRejected("too_large", f"key and version take {len(meta)} bytes > {META_MAX}")
        need = reserve + len(meta)
        if need > self.max_bytes:
            raise BudgetRejected("too_large", f"{need} bytes > limit {self.max_bytes}")
        w = _Writer(self, key, version, reserve, meta)
        with self._lock() as ctx:
            entries = self._scan()
            self._housekeep(ctx, entries)
            entries = self._scan()
            others = self._live_writers()
            self._reclaim(
                ctx,
                entries,
                sum(r for _, r in others),
                len(others),
                need,
                1,
                first=key_hash(key),
                floor_check=True,
            )
            # register: the lock file first (and held), then the data folder
            w.fd = _track(os.open(w.lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
            try:
                fcntl.flock(w.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(w.fd, json.dumps({"reserved": need, "pid": os.getpid()}).encode())
                w.path.mkdir(parents=True)
            except BaseException:
                w.close()
                raise
        with self._writers_mutex:
            self._writers[w.id] = w
        return w

    # ---------------------------------------------------------------- commit

    def _commit(self, w: _Writer) -> Entry:
        if not w.path.is_dir():
            raise BudgetRejected("no_output" if w.container.is_dir() else "reclaimed", "scratch folder is gone")
        (w.path / META_NAME).write_bytes(w.meta)
        size = _tree_size(w.path)
        if size > w.allowed:
            raise BudgetRejected("too_large", f"{size} bytes written > reserved {w.allowed}")
        if self.durable:
            _fsync_tree(w.path)
        kh, vh = key_hash(w.key), version_hash(w.version)
        with self._lock() as ctx:
            now = self.clock()
            entries = self._scan()
            # generations order the rewrites of a key; never let one go backwards, whatever the clocks say
            newest = max((int(e.generation[:16], 16) for e in entries if e.key_hash == kh), default=0)
            gen = f"{max(int(now * 1e9), newest + 1):016x}{uuid.uuid4().hex[:4]}"
            mine = [e for e in entries if e.key_hash == kh]
            others = [(i, r) for i, r in self._live_writers() if i != w.id]
            for e in mine:
                self._try_delete(e, ctx)  # a leased older generation stays, as "superseded"
            kept = [e for e in entries if e.path.exists()]  # what is left: kept by a lease, or not ours
            self._reclaim(ctx, kept, sum(r for _, r in others), len(others), size, 1)
            dest = self.root / f"{kh}.{vh}.{gen}.{size:x}"
            self._hook("commit:before_rename")
            os.rename(w.path, dest)
            self._hook("commit:after_rename")
            ns = int(now * 1e9)
            for p in (dest / META_NAME, dest):
                os.utime(p, ns=(ns, ns))
            if self.durable:
                try:
                    _fsync(self.root, directory=True)
                except OSError:  # not known to be durable: do not leave it visible while reporting failure
                    trash = self._trash / f"{uuid.uuid4().hex}.{size:x}"
                    with contextlib.suppress(OSError):
                        os.rename(dest, trash)
                        ctx.trash.append(trash)
                    raise
            return Entry(dest.name, dest, size, ns, kh, vh, gen, False, False, False, w.key, w.version)

    @contextlib.contextmanager
    def _writing(self, key: str, version: str | None, reserve: int | None) -> Iterator[tuple[_Writer, list]]:
        version = self.version if version is None else version
        w = self._begin(key, version, self.default_reserve if reserve is None else reserve)
        box: list[Entry] = []
        try:
            try:
                yield w, box
                box.append(self._commit(w))
            except OSError as exc:
                if exc.errno in _FULL:
                    raise BudgetRejected("no_space", str(exc)) from exc
                if isinstance(exc, FileNotFoundError) and not w.container.exists():
                    raise BudgetRejected("reclaimed", "the scratch folder was removed under the writer") from exc
                raise
        finally:
            w.close()

    @contextlib.contextmanager
    def writing(self, key: str, *, version: str | None = None, reserve: int | None = None) -> Iterator[Path]:
        """Write one entry. ``reserve`` is the payload bytes you may write (default: a quarter of the budget). The
        block gets an empty folder; put files in it (``open_file`` stops at the reserve, anything else is measured
        at commit or by ``charge``). On a clean exit the folder is renamed into place as the entry. Any exception
        leaves nothing behind. Raises ``BudgetRejected`` when room cannot be made before writing."""
        with self._writing(key, version, reserve) as (w, _box):
            yield w.path

    def put(self, key: str, data: bytes, *, version: str | None = None) -> Entry:
        """Store ``data`` under ``key`` (file ``data`` in the entry). Raises ``BudgetRejected`` when it cannot fit
        or the disk is full."""
        with (
            self._writing(key, version, len(data)) as (w, box),
            open(w.path / "data", "wb") as f,
        ):
            f.write(data)
        return box[0]

    def _writer_of(self, path: str | os.PathLike[str]) -> _Writer:
        p = os.fspath(path)
        with self._writers_mutex:
            for w in self._writers.values():
                if p == str(w.path) or p.startswith(str(w.path) + os.sep):
                    return w
        raise ValueError(f"{p} is not inside a scratch folder of this DiskBudget")

    def open_file(self, path: str | os.PathLike[str], mode: str = "wb") -> _Metered:
        """Open a file inside a scratch folder from ``writing`` for binary writing; bytes beyond the writer's
        ``reserve`` raise ``BudgetRejected("too_large")`` before they are written."""
        if mode not in ("wb", "xb", "ab"):
            raise ValueError("binary write modes only")
        return _Metered(self._writer_of(path), open(path, mode))

    def charge(self, scratch: str | os.PathLike[str]) -> int:
        """Measure what is in the scratch folder now; raises ``BudgetRejected("too_large")`` beyond the reserve.
        For writers that do not use ``open_file``. Returns the bytes."""
        w = self._writer_of(scratch)
        size = _tree_size(w.path)
        if size > w.reserve:
            raise BudgetRejected("too_large", f"{size} bytes written > reserved {w.reserve}")
        return size

    # ------------------------------------------------------------------- read

    def get(self, key: str, *, version: str | None = None) -> Lease | None:
        """A lease on the newest live entry of ``key`` (``None``: missing, expired, another version, or damaged),
        counted as a use. Hold it while you read; release with ``close()`` or ``with``."""
        version = self.version if version is None else version
        kh, vh = key_hash(key), version_hash(version)
        with self._lock() as ctx:
            entries = self._scan()
            self._housekeep(ctx, entries)
            cands = sorted((e for e in entries if e.key_hash == kh and e.version_hash == vh), key=lambda e: e.generation)
            if not cands:
                return None
            e = cands[-1]
            if e.expired:
                self._try_delete(e, ctx)
                return None
            try:
                fd = _track(os.open(e.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)))
            except OSError:
                return None
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # a deleter holds it only while we hold the lock
                ns = int(self.clock() * 1e9)
                try:
                    os.utime(e.path / META_NAME, ns=(ns, ns))
                except FileNotFoundError:  # deleted in place by another tool: not a valid entry any more
                    _untrack_close(fd)
                    fd = -1
                    self._try_delete(e, ctx)
                    return None
                os.utime(e.path, ns=(ns, ns))
            except BlockingIOError:
                _untrack_close(fd)
                return None
            except BaseException:
                if fd >= 0:
                    _untrack_close(fd)
                raise
            return Lease(e.path, fd)

    @contextlib.contextmanager
    def reading(self, key: str, *, version: str | None = None) -> Iterator[Path | None]:
        """``get`` as a block: yields the entry folder (or ``None``) and releases the lease afterwards."""
        lease = self.get(key, version=version)
        try:
            yield None if lease is None else lease.path
        finally:
            if lease is not None:
                lease.close()

    def read(self, key: str, *, version: str | None = None) -> bytes | None:
        """The bytes ``put`` stored under ``key``, or ``None``."""
        with self.reading(key, version=version) as path:
            if path is None:
                return None
            try:
                return (path / "data").read_bytes()
            except FileNotFoundError:
                return None

    def discard(self, key: str) -> int:
        """Delete every generation and version of ``key`` that nobody is reading; returns how many went."""
        kh = key_hash(key)
        with self._lock() as ctx:
            return sum(self._try_delete(e, ctx) for e in self._scan() if e.key_hash == kh)

    # ------------------------------------------------------------------ views

    def entries(self) -> list[Entry]:
        """Every entry, least recently used first, with its key and version string when ``.meta`` can be read."""
        with self._lock():
            out = []
            for e in sorted(self._scan(), key=lambda e: (e.mtime_ns, e.name)):
                try:
                    meta = json.loads((e.path / META_NAME).read_text("utf-8"))
                    out.append(
                        Entry(**{**e.__dict__, "key": meta.get("key"), "version": meta.get("version")})
                    )
                except (OSError, ValueError):
                    out.append(e)
            return out

    def purge_stale(self) -> int:
        """Delete every entry of another version now (the grace period is only for the automatic path)."""
        with self._lock() as ctx:
            return sum(self._try_delete(e, ctx) for e in self._scan() if e.stale)

    def stats(self) -> dict[str, int]:
        with self._lock():
            entries = self._scan()
            writers = self._live_writers()
            trash = self._trash_bytes()
        return {
            "bytes": sum(e.size for e in entries),
            "entries": len(entries),
            "max_bytes": self.max_bytes,
            "max_entries": self.max_entries or 0,
            "stale": sum(e.stale for e in entries),
            "expired": sum(e.expired for e in entries),
            "writers": len(writers),
            "reserved_bytes": sum(r for _, r in writers),
            "trash_bytes": trash,
        }

    # --------------------------------------------------------------- maintain

    def _resize(self, e: Entry, actual: int) -> bool:
        """Rename an unleased entry that has grown to the name that carries its real size."""
        new = e.path.with_name(f"{e.key_hash}.{e.version_hash}.{e.generation}.{actual:x}")
        try:
            fd = os.open(e.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.rename(e.path, new)
            return True
        except OSError:  # includes BlockingIOError: a reader holds it, try again next time
            return False
        finally:
            os.close(fd)

    def maintain(self) -> dict[str, int]:
        """Startup check, safe to call any time: reclaim dead writers and orphan trash, drop expired, long-stale,
        replaced and damaged entries, correct entry sizes, and prune to the current limits (a smaller
        ``max_bytes`` than the last run's takes effect here). Returns counts."""
        with self._lock() as ctx:
            counts = self._housekeep(ctx, force=True)
            resized = 0
            for e in self._scan():
                if not (e.path / META_NAME).exists():  # deleted in place by some other tool, or never ours
                    counts["damaged"] += self._try_delete(e, ctx)
                    continue
                try:
                    actual = _tree_size(e.path)
                except (BudgetRejected, OSError):
                    counts["damaged"] += self._try_delete(e, ctx)
                    continue
                if actual < e.size:  # entries never shrink: part of it was deleted from outside
                    counts["damaged"] += self._try_delete(e, ctx)
                elif actual > e.size:
                    resized += self._resize(e, actual)
            counts["resized"] = resized
            entries = self._scan()
            others = self._live_writers()
            with contextlib.suppress(BudgetRejected):
                self._reclaim(ctx, entries, sum(r for _, r in others), len(others), 0, 0)
            counts["pruned"] = len(entries) - len(self._scan())
            return counts


# ------------------------------------------------------------- abandoned caches


def _idle_enough(root: Path, idle_s: float, now: float) -> bool:
    if not (root / BUDGET_DIR).is_dir():
        return False  # not a cache this class manages
    w = root / BUDGET_DIR / "w"
    if w.is_dir():
        for e in os.scandir(w):
            if not e.name.endswith(".lock"):
                continue
            try:
                fd = os.open(e.path, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False  # a live writer
            finally:
                os.close(fd)
    newest = root.stat().st_mtime
    for e in os.scandir(root):
        if e.name.startswith(".") or not e.is_dir(follow_symlinks=False):
            continue
        try:
            fd = os.open(e.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False  # somebody is reading it
        finally:
            os.close(fd)
        newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
        with contextlib.suppress(OSError):
            newest = max(newest, os.stat(os.path.join(e.path, META_NAME)).st_mtime)
    return now - newest > idle_s


@contextlib.contextmanager
def claim_abandoned(root: str | os.PathLike[str], idle_s: float, *, now: float | None = None) -> Iterator[bool]:
    """For an outside cleaner that wants to remove a whole cache: yields ``True`` while holding the cache's lock
    when the folder is a ``DiskBudget`` cache that is safe to reclaim, ``False`` otherwise. Safe means: the lock
    could be taken, no writer is alive, no entry is leased, and nothing in it (the folder, every entry folder, every
    ``.meta``) was touched for ``idle_s`` seconds. Inside the ``with`` block nobody else can change the cache;
    rename the folder to a sibling name (one ``rename``) and delete that. A ``DiskBudget`` that uses the cache
    afterwards recreates it."""
    root = Path(root)
    fd = -1
    ok = False
    try:
        try:
            fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ok = _idle_enough(root, idle_s, time.time() if now is None else now)
        except OSError:  # includes BlockingIOError: in use
            ok = False
        yield ok
    finally:
        if fd >= 0:
            os.close(fd)


def is_abandoned(root: str | os.PathLike[str], idle_s: float, *, now: float | None = None) -> bool:
    """``claim_abandoned`` without keeping the lock: a hint, not a guarantee, by the time you act on it."""
    with claim_abandoned(root, idle_s, now=now) as ok:
        return ok
