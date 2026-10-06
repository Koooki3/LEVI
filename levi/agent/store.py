"""SQLite task journal and immutable artifact/bundle publication.

A SQLite COMMIT publishes the annotation pointer and its idempotency receipt
in the SAME transaction. Prepared directories are never visible to readers.
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .schema import RunStatus

_CONNECTIONS = threading.local()


class _ThreadConnections(OrderedDict):
    """One thread's cached connections, closed when the thread goes away.

    ``threading.local`` drops a finished thread's values, but a dropped
    ``sqlite3.Connection`` is not closed promptly: it sits in a reference cycle
    with its statement cache and, once it has run statements, the file handle
    stays open after the object is collected. Every finished run thread, episode
    worker and heartbeat thread then left a ``workbench.sqlite3`` descriptor
    (and its ``-wal``) behind. Closing explicitly when the cache dies releases
    them."""

    def close(self):
        while self:
            _, conn = self.popitem()
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def __del__(self):
        self.close()


def close_thread_connections():
    """Close this thread's cached connections now (a thread that is about to
    finish, or a test). The next ``Store.connect`` opens a fresh one."""
    cache = getattr(_CONNECTIONS, "items", None)
    if cache is not None:
        cache.close()


_PIN: ContextVar[dict | None] = ContextVar("agent_bundle", default=None)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class Conflict(ValueError):
    pass


NEW_CALL = "new-call"


class Store:
    def __init__(self, state: Path):
        self.state = state
        self.root = state / "agent"
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect():
            pass

    @contextmanager
    def connect(self):
        # Bounded thread-local connections avoid a WAL checkpoint on every
        # polling request. A thread's connections are closed when it exits
        # (``_ThreadConnections``); none crosses a FastAPI worker-thread boundary.
        if not hasattr(_CONNECTIONS, "items"):
            _CONNECTIONS.items = _ThreadConnections()
        cache = _CONNECTIONS.items
        path = str((self.root / "workbench.sqlite3").resolve())
        db = cache.get(path)
        if db is None:
            db = sqlite3.connect(path, timeout=30)
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, body TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_run ON events(run_id,seq);
            CREATE TABLE IF NOT EXISTS heads(dataset TEXT PRIMARY KEY, revision TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY, request TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, owner TEXT, expires REAL);
            """)
            cache[path] = db
        cache.move_to_end(path)
        while len(cache) > 8:
            stale = next(
                (
                    key
                    for key, conn in cache.items()
                    if key != path and not conn.in_transaction
                ),
                None,
            )
            if stale is None:
                break
            cache.pop(stale).close()
        if db.in_transaction:
            # Re-entrant reads from validation must NOT commit an outer edit.
            yield db
        else:
            with db:
                yield db

    def get(self, kind, id):
        with self.connect() as db:
            row = db.execute(
                "SELECT body FROM records WHERE kind=? AND id=?", (kind, id)
            ).fetchone()
        if row is None:
            raise KeyError(id)
        return json.loads(row[0])

    def list(self, kind):
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM records WHERE kind=? ORDER BY rowid DESC", (kind,)
                )
            ]

    def put(self, kind, id, value):
        with self.connect() as db:
            self.save(db, kind, id, value)

    @staticmethod
    def save(db, kind, id, value):
        db.execute(
            "INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, id, dumps(value))
        )

    def ids(self, kind):
        """Record keys for a kind. The key is not always inside the body."""
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT id FROM records WHERE kind=? ORDER BY id", (kind,)
                )
            ]

    def drop(self, kind, ids):
        """Remove records by id. Only explicit, confirmed maintenance calls this."""
        ids = list(ids)
        if not ids:
            return 0
        with self.connect() as db:
            marks = ",".join("?" * len(ids))
            return db.execute(
                f"DELETE FROM records WHERE kind=? AND id IN ({marks})", (kind, *ids)
            ).rowcount

    def drop_events(self, run_ids):
        ids = list(run_ids)
        if not ids:
            return 0
        with self.connect() as db:
            marks = ",".join("?" * len(ids))
            return db.execute(
                f"DELETE FROM events WHERE run_id IN ({marks})", ids
            ).rowcount

    def drop_receipts(self, change_ids):
        """Publication receipts ("<channel>:<changeset>:...") of these
        changesets: without their changesets they only block key reuse."""
        ids = set(change_ids)
        if not ids:
            return 0
        with self.connect() as db:
            keys = [
                key
                for (key,) in db.execute("SELECT key FROM receipts")
                if ids & set(str(key).split(":"))
            ]
            for key in keys:
                db.execute("DELETE FROM receipts WHERE key=?", (key,))
        return len(keys)

    def drop_head(self, dataset):
        with self.connect() as db:
            return db.execute("DELETE FROM heads WHERE dataset=?", (dataset,)).rowcount

    def mutate(self, kind, id, fn):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM records WHERE kind=? AND id=?", (kind, id)
            ).fetchone()
            if not row:
                raise KeyError(id)
            value = json.loads(row[0])
            fn(value)
            self.save(db, kind, id, value)
            return value

    def event(self, run_id, type, **data):
        with self.connect() as db:
            event = {"type": type, "time": time.time(), **data}
            cursor = db.execute(
                "INSERT INTO events(run_id,body) VALUES(?,?)", (run_id, dumps(event))
            )
            if event.get("call_id") == NEW_CALL:
                # A call is named by the journal position of its first event:
                # unique across processes and readable, assigned in the same
                # transaction as the insert.
                event["call_id"] = f"call-{cursor.lastrowid}"
                db.execute(
                    "UPDATE events SET body=? WHERE seq=?",
                    (dumps(event), cursor.lastrowid),
                )
            return {"seq": cursor.lastrowid, **event}

    def events(self, run_id, after=0):
        with self.connect() as db:
            return [
                {"seq": seq, **json.loads(body)}
                for seq, body in db.execute(
                    "SELECT seq,body FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 500",
                    (run_id, after),
                )
            ]

    def head(self, dataset, db=None):
        if db is None:
            with self.connect() as conn:
                return self.head(dataset, conn)
        row = db.execute(
            "SELECT revision FROM heads WHERE dataset=?", (dataset,)
        ).fetchone()
        return row[0] if row else "legacy"

    def bundle(self, dataset, revision=None):
        if not re.fullmatch(r"[\w.-]+", dataset) or dataset in {".", ".."}:
            raise ValueError("Invalid dataset key")
        revision = revision or self.head(dataset)
        if not re.fullmatch(r"[\w.-]+", revision) or revision in {".", ".."}:
            raise ValueError("Invalid bundle revision")
        return self.root / "datasets" / dataset / "revisions" / revision

    def run_dir(self, id):
        run = self.get("runs", id)
        name = run["dataset_key"]
        if not re.fullmatch(r"[\w.-]+", id) or id in {".", ".."}:
            raise ValueError("Invalid run ID")
        return self.root / "datasets" / name / "runs" / id

    def prepare(self, dataset, kind=None):
        from .runtime import new_id

        # A revision directory names the dataset's folder it lives in, the
        # kind of work, and the second it was published.
        existing = {p.name for p in (self.root / "datasets" / dataset).glob("*")}
        revisions = self.root / "datasets" / dataset / "revisions"
        if revisions.is_dir():
            existing |= {p.name for p in revisions.iterdir()}
        stamp = new_id({name.split("-", 1)[-1] for name in existing})
        revision = f"{kind}-{stamp}" if kind else stamp
        target = self.bundle(dataset, revision)
        previous = self.head(dataset)
        if previous == "legacy":
            target.mkdir(parents=True)
            for category in ("annotations", "object_annotations"):
                source = self.state / category / dataset
                if source.exists():
                    shutil.copytree(
                        source,
                        target / category,
                        ignore=shutil.ignore_patterns("staging"),
                    )
            review = self.state / "reviews" / (dataset + ".json")
            if review.exists():
                shutil.copy2(review, target / "review.json")
        else:
            shutil.copytree(self.bundle(dataset, previous), target)
        return previous, revision, target

    def publish(self, dataset, base, revision, key, request, receipt, *, change=None):
        # Flush the prepared files before exposing their directory. All runtime
        # data stays on the same workspace filesystem. Never hardlink mutable files.
        folder = self.bundle(dataset, revision)
        for path in folder.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        for directory in [
            folder,
            *[p for p in folder.rglob("*") if p.is_dir()],
            folder.parent,
        ]:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT request,body FROM receipts WHERE key=?", (key,)
            ).fetchone()
            if old:
                if old[0] != request:
                    raise Conflict("Idempotency key reused for different arguments")
                return json.loads(old[1])
            if self.head(dataset, db) != base:
                raise Conflict(
                    "Annotation revision changed; refresh and review the diff"
                )
            db.execute("INSERT OR REPLACE INTO heads VALUES(?,?)", (dataset, revision))
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?)", (key, request, dumps(receipt))
            )
            if change:
                self.save(db, "changes", change["id"], change)
        return receipt

    def receipt(self, key, request):
        with self.connect() as db:
            row = db.execute(
                "SELECT request,body FROM receipts WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        if row[0] != request:
            raise Conflict("Idempotency key reused for different arguments")
        return json.loads(row[1])

    def claim(self, id, owner, ttl=180):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner,expires FROM leases WHERE id=?", (id,)
            ).fetchone()
            if row and row[1] > time.time() and row[0] != owner:
                return False
            db.execute(
                "INSERT OR REPLACE INTO leases VALUES(?,?,?)",
                (id, owner, time.time() + ttl),
            )
            return True

    def release(self, id, owner):
        with self.connect() as db:
            db.execute("DELETE FROM leases WHERE id=? AND owner=?", (id, owner))

    def recover(self):
        for run in self.list("runs"):
            if run["status"] not in {RunStatus.RUNNING, RunStatus.QUEUED}:
                continue
            with self.connect() as db:
                lease = db.execute(
                    "SELECT expires FROM leases WHERE id=?", (run["id"],)
                ).fetchone()
            if not lease or lease[0] < time.time():
                self.mutate(
                    "runs",
                    run["id"],
                    lambda value: value.update(
                        status=RunStatus.INTERRUPTED,
                        reason="Execution interrupted; resume from the last completed shard",
                    ),
                )
        for job in self.list("object_jobs"):
            if job["status"] not in {"running", "queued"}:
                continue
            with self.connect() as db:
                lease = db.execute(
                    "SELECT expires FROM leases WHERE id=?",
                    ("objects:" + job["run_id"],),
                ).fetchone()
            if not lease or lease[0] < time.time():
                self.mutate(
                    "object_jobs",
                    job["id"],
                    lambda value: value.update(
                        status="interrupted",
                        reason="Worker interrupted; create a new bounded object plan",
                    ),
                )


class StoreUnavailable(RuntimeError):
    """Another workspace's store could not be read right now (it is being
    written, or is damaged). Never answered as an empty store: a reader that
    took "cannot read" for "no annotations" would show the wrong ones."""


_SNAPSHOTS: dict[str, tuple] = {}


def _stamp(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _snapshot(source: Path) -> Path:
    """A private copy of another workspace's store (its database and, while a
    writer has one, its ``-wal`` file), to read from. Opening the original,
    even read-only, can make ``-shm`` and ``-wal`` files beside it, and which
    open is safe depends on whether a writer is there at that moment: a copy
    needs no such decision and writes nothing into that workspace. Reused
    while the originals are unchanged."""
    from levi import paths

    wal = source.with_name(source.name + "-wal")
    stamp = (_stamp(source), _stamp(wal))
    folder = (
        paths.CACHE
        / "linked-stores"
        / hashlib.sha256(str(source).encode()).hexdigest()[:16]
    )
    copy = folder / "workbench.sqlite3"
    cached = _SNAPSHOTS.get(str(copy))
    if cached and cached == stamp and copy.is_file():
        return copy
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        (folder / f"workbench.sqlite3{suffix}").unlink(missing_ok=True)
    shutil.copyfile(source, copy)
    if wal.is_file():
        try:
            shutil.copyfile(wal, folder / "workbench.sqlite3-wal")
        except FileNotFoundError:
            pass  # a checkpoint just emptied it: the database holds it all
    _SNAPSHOTS[str(copy)] = stamp
    return copy


class ReadStore(Store):
    """An existing store of another workspace, read from a private snapshot
    (``levi/links.py``): that workspace may be read and must never be created
    in or changed. A store that is not there reads as empty (every dataset is
    ``legacy``); one that cannot be read raises ``StoreUnavailable``."""

    def __init__(self, state: Path):
        self.state = Path(state)
        self.root = self.state / "agent"

    @contextmanager
    def connect(self):
        source = self.root / "workbench.sqlite3"
        if not source.is_file():
            raise FileNotFoundError(source)
        last = None
        for attempt in range(4):
            try:
                db = sqlite3.connect(
                    f"file:{_snapshot(source)}?mode=ro", uri=True, timeout=30
                )
                db.execute("SELECT 1 FROM sqlite_master LIMIT 1")
                break
            except (OSError, sqlite3.Error) as exc:
                last = exc
                _SNAPSHOTS.clear()
                time.sleep(0.05 * (attempt + 1))
        else:
            raise StoreUnavailable(str(last)) from last
        try:
            yield db
        finally:
            db.close()

    def head(self, dataset, db=None):
        try:
            return super().head(dataset, db)
        except FileNotFoundError:
            return "legacy"

    def list(self, kind):
        try:
            return super().list(kind)
        except FileNotFoundError:
            return []

    def get(self, kind, id):
        try:
            return super().get(kind, id)
        except FileNotFoundError:
            raise KeyError(id) from None

    def events(self, run_id, after=0):
        try:
            return super().events(run_id, after)
        except FileNotFoundError:
            return []


def resolve_readonly(state: Path, dataset: str, category: str):
    """``resolve`` for a workspace that is only read: the same folder a
    reader there would use, found without opening a writable store."""
    store = ReadStore(state)
    head = store.head(dataset)
    if head != "legacy":
        folder = store.bundle(dataset, head)
        return folder / ("review.json" if category == "reviews" else category)
    return (
        Path(state)
        / category
        / (dataset + ".json" if category == "reviews" else dataset)
    )


@contextmanager
def pin(state, dataset, folder):
    key = (str(state), dataset)
    token = _PIN.set({**(_PIN.get() or {}), key: folder})
    try:
        yield
    finally:
        _PIN.reset(token)


def resolve(state: Path, dataset: str, category: str):
    """Shared resolver for legacy readers, exports, carry-over and Agent tools."""
    folder = (_PIN.get() or {}).get((str(state), dataset))
    database = state / "agent/workbench.sqlite3"
    if folder is None and database.exists():
        store = Store(state)
        head = store.head(dataset)
        if head != "legacy":
            folder = store.bundle(dataset, head)
    if folder is not None:
        return folder / ("review.json" if category == "reviews" else category)
    return state / category / (dataset + ".json" if category == "reviews" else dataset)


def current_pin(state, dataset):
    return (_PIN.get() or {}).get((str(state), dataset))


def annotation_digest(state, dataset):
    """Content signature for the pre-versioned workspace, never a stat-only check."""
    result = {}
    for category in ("annotations", "object_annotations", "reviews"):
        root = resolve(state, dataset, category)
        files = [root] if root.is_file() else root.rglob("*") if root.exists() else []
        for path in files:
            if path.is_file() and not any(
                p in {"jobs", "plans", "staging"} for p in path.parts
            ):
                result[
                    f"{category}/{path.relative_to(root) if path != root else 'review'}"
                ] = file_hash(path)
    return digest(result)


@contextmanager
def dataset_lock(state, dataset):
    """Cross-process writer serialization; only called from synchronous handlers."""
    import fcntl

    if not re.fullmatch(r"[\w.-]+", dataset) or dataset in {".", ".."}:
        raise ValueError("Invalid lock name")
    folder = state / "agent/locks"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / (dataset + ".lock")).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
