"""``levi.performance.budget.DiskBudget``: limits, LRU, TTL, versions, atomic writes, crash recovery."""

import errno
import json
import multiprocessing
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from levi.performance import budget as B
from levi.performance.budget import BudgetRejected, DiskBudget

NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s=1.0):
        self.t += s
        return self.t


def make(tmp_path, **kw):
    clock = kw.setdefault("clock", Clock())
    kw.setdefault("max_bytes", 100)
    kw.setdefault("durable", False)
    return DiskBudget(tmp_path / "cache", **kw), clock


def names(b):
    return sorted(p.name for p in b.root.iterdir())


# ---------------------------------------------------------------- basics


def test_put_get_read_roundtrip(tmp_path):
    b, _ = make(tmp_path)
    e = b.put("k", b"hello")
    assert e.size == 5 and e.key == "k"
    assert b.read("k") == b"hello"
    assert b.get("k").read_bytes() == b"hello"
    assert b.get("missing") is None and b.read("missing") is None
    assert b.stats()["bytes"] == 5 and b.stats()["entries"] == 1


def test_put_same_key_replaces(tmp_path):
    b, _ = make(tmp_path)
    b.put("k", b"aaaa")
    b.put("k", b"bb")
    assert b.read("k") == b"bb"
    assert b.stats() | {} == b.stats() and b.stats()["bytes"] == 2 and b.stats()["entries"] == 1


def test_cap_gb_is_gib_like_the_janitor_quota():
    assert B.GIB == 2**30
    assert B.DEFAULT_CAP_GB == 20 and B.DEFAULT_TTL_S == 14 * 86400 and B.DEFAULT_STALE_GRACE_S == 7 * 86400


def test_from_cap_gb(tmp_path):
    b = DiskBudget.from_cap_gb(tmp_path / "c", 0.5, durable=False)
    assert b.max_bytes == 2**29


def test_directory_entries(tmp_path):
    b, _ = make(tmp_path)
    with b.writing("ev") as tmp:
        tmp.mkdir()
        (tmp / "a.png").write_bytes(b"x" * 10)
        (tmp / "sub").mkdir()
        (tmp / "sub" / "b.png").write_bytes(b"y" * 5)
    e = b.entries()[0]
    assert e.is_dir and e.size == 15
    assert (b.get("ev") / "sub" / "b.png").read_bytes() == b"y" * 5
    assert b.read("ev") is None  # a directory has no bytes
    b.put("ev", b"z")  # replaced by a file, the directory is gone
    assert b.stats()["bytes"] == 1


def test_foreign_names_are_not_managed(tmp_path):
    b, _ = make(tmp_path)
    (b.root / "README").write_text("not ours")
    b.put("k", b"1")
    b.maintain()
    assert (b.root / "README").exists() and b.stats()["entries"] == 1


# ------------------------------------------------------------------ limits


def test_lru_eviction_order(tmp_path):
    b, clock = make(tmp_path, max_bytes=30)
    for k in "abc":
        b.put(k, b"x" * 10)
        clock.tick()
    clock.tick()
    assert b.read("a") == b"x" * 10  # a is now the most recent
    clock.tick()
    b.put("d", b"x" * 10)  # needs room: b is the least recently used
    assert b.read("b") is None
    assert {k for k in "acd" if b.read(k) is not None} == set("acd")
    assert b.stats()["bytes"] == 30


def test_eviction_frees_enough_for_a_large_write(tmp_path):
    b, clock = make(tmp_path, max_bytes=30)
    for k in "abc":
        b.put(k, b"x" * 10)
        clock.tick()
    b.put("big", b"y" * 25)
    assert b.stats()["bytes"] <= 30
    assert b.read("big") == b"y" * 25
    assert b.stats()["entries"] == 1  # 25 + 10 would exceed 30


def test_entry_limit(tmp_path):
    b, clock = make(tmp_path, max_bytes=1000, max_entries=2)
    for k in "abc":
        b.put(k, b"1")
        clock.tick()
    assert b.stats()["entries"] == 2
    assert b.read("a") is None and b.read("c") == b"1"


def test_too_large_is_refused_and_evicts_nothing(tmp_path):
    b, _ = make(tmp_path, max_bytes=30)
    b.put("a", b"x" * 10)
    with pytest.raises(BudgetRejected) as err:
        b.put("huge", b"x" * 31)
    assert err.value.reason == "too_large"
    assert b.read("a") == b"x" * 10
    assert not [n for n in names(b) if n.startswith(".tmp-")]
    with pytest.raises(BudgetRejected), b.writing("w", expected_bytes=31):
        raise AssertionError("must be refused before the body runs")


def test_too_large_after_the_write_leaves_nothing(tmp_path):
    b, _ = make(tmp_path, max_bytes=30)
    b.put("a", b"x" * 10)
    with pytest.raises(BudgetRejected) as err, b.writing("w") as tmp:
        tmp.write_bytes(b"x" * 50)
    assert err.value.reason == "too_large"
    assert b.stats()["entries"] == 1 and not [n for n in names(b) if n.startswith(".tmp-")]


def test_writer_exception_leaves_nothing(tmp_path):
    b, _ = make(tmp_path)
    with pytest.raises(RuntimeError), b.writing("w") as tmp:
        tmp.write_bytes(b"partial")
        raise RuntimeError("boom")
    assert b.stats()["entries"] == 0 and not [n for n in names(b) if n.startswith(".tmp-")]


def test_writer_that_creates_nothing_is_refused(tmp_path):
    b, _ = make(tmp_path)
    with pytest.raises(BudgetRejected) as err, b.writing("w"):
        pass
    assert err.value.reason == "no_output"


# -------------------------------------------------------------- disk full


def test_enospc_from_the_writer_is_a_refusal(tmp_path):
    b, _ = make(tmp_path)
    b.put("a", b"1")
    with pytest.raises(BudgetRejected) as err, b.writing("w") as tmp:
        tmp.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")
    assert err.value.reason == "no_space"
    assert b.read("a") == b"1" and not [n for n in names(b) if n.startswith(".tmp-")]


def test_other_oserror_is_not_swallowed(tmp_path):
    b, _ = make(tmp_path)
    with pytest.raises(PermissionError), b.writing("w") as tmp:
        tmp.write_bytes(b"x")
        raise PermissionError(errno.EACCES, "no")


def test_enospc_in_put_is_a_refusal(tmp_path, monkeypatch):
    b, _ = make(tmp_path)
    real = open

    def full(file, mode="r", *a, **k):
        if "w" in str(mode) and Path(file).name.startswith(".tmp-") and not str(file).endswith("-index"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(file, mode, *a, **k)

    monkeypatch.setattr("builtins.open", full)
    with pytest.raises(BudgetRejected) as err:
        b.put("k", b"data")
    monkeypatch.undo()
    assert err.value.reason == "no_space"
    assert b.stats()["entries"] == 0 and not [n for n in names(b) if n.startswith(".tmp-")]


def test_free_space_floor_refuses(tmp_path, monkeypatch):
    b, _ = make(tmp_path, min_free_bytes=1000)
    monkeypatch.setattr(B, "_free_bytes", lambda path: 1005)
    b.put("ok", b"12345")  # 1005 - 5 = 1000: exactly at the floor
    with pytest.raises(BudgetRejected) as err:
        b.put("no", b"123456")
    assert err.value.reason == "no_space"
    assert b.read("ok") == b"12345" and b.read("no") is None


def test_free_space_floor_counts_what_eviction_gives_back(tmp_path, monkeypatch):
    b, clock = make(tmp_path, max_bytes=20, min_free_bytes=1000)
    monkeypatch.setattr(B, "_free_bytes", lambda path: 1000)
    # empty folder: 1000 free, floor 1000, anything refused
    with pytest.raises(BudgetRejected):
        b.put("a", b"x")
    monkeypatch.setattr(B, "_free_bytes", lambda path: 1010)
    b.put("a", b"x" * 10)
    clock.tick()
    b.put("b", b"y" * 10)
    clock.tick()
    monkeypatch.setattr(B, "_free_bytes", lambda path: 1000)  # a replacement of "a" gives its 10 bytes back
    b.put("a", b"z" * 10)
    assert b.read("a") == b"z" * 10


# --------------------------------------------------------------------- TTL


def test_ttl_expires_idle_entries(tmp_path):
    b, clock = make(tmp_path, ttl_s=100)
    b.put("k", b"1")
    clock.tick(99)
    assert b.read("k") == b"1"  # a use renews it
    clock.tick(99)
    assert b.read("k") == b"1"
    clock.tick(101)
    assert b.read("k") is None
    assert b.stats()["entries"] == 0  # and it is gone, not just hidden


def test_per_entry_ttl_overrides(tmp_path):
    b, clock = make(tmp_path, ttl_s=1000)
    b.put("short", b"1", ttl_s=10)
    b.put("long", b"2")
    clock.tick(50)
    assert b.read("short") is None and b.read("long") == b"2"


def test_expired_entries_are_dropped_by_maintain_and_by_put(tmp_path):
    b, clock = make(tmp_path, ttl_s=100, max_bytes=20)
    b.put("a", b"x" * 10)
    clock.tick(50)
    b.put("b", b"y" * 10)
    clock.tick(60)  # a idle 110 (expired), b idle 60
    b.put("c", b"z" * 10)  # fits only because the expired a is dropped, not b evicted
    assert b.read("b") == b"y" * 10 and b.read("c") == b"z" * 10 and b.read("a") is None
    clock.tick(200)
    assert b.maintain()["expired"] == 2 and b.stats()["entries"] == 0


def test_no_ttl(tmp_path):
    b, clock = make(tmp_path, ttl_s=None)
    b.put("k", b"1")
    clock.tick(10**9)
    assert b.read("k") == b"1"


# ------------------------------------------------------------------ versions


def test_other_version_is_a_miss(tmp_path):
    clock = Clock()
    old, _ = make(tmp_path, version="schema-1", clock=clock)
    old.put("k", b"old")
    new = DiskBudget(old.root, max_bytes=100, version="schema-2", durable=False, clock=clock)
    assert new.read("k") is None and new.get("k") is None
    assert old.read("k") == b"old"  # the old reader still sees its own
    assert new.stats()["stale"] == 1


def test_stale_versions_go_first_then_after_grace(tmp_path):
    clock = Clock()
    old, _ = make(tmp_path, version="v1", max_bytes=30, clock=clock)
    old.put("old-newest", b"x" * 10)
    clock.tick()
    new = DiskBudget(old.root, max_bytes=30, version="v2", durable=False, clock=clock, stale_grace_s=1000)
    new.put("a", b"a" * 10)
    clock.tick()
    new.put("b", b"b" * 10)
    clock.tick()
    new.put("c", b"c" * 10)  # 40 > 30: the stale entry goes, not a valid one
    assert new.stats()["stale"] == 0 and {k for k in "abc" if new.read(k)} == set("abc")
    # stale entry younger than the grace stays when there is room, and is dropped after it
    big = DiskBudget(old.root, max_bytes=1000, version="v3", durable=False, clock=clock, stale_grace_s=1000)
    clock.tick(500)
    assert big.maintain()["stale"] == 0 and big.stats()["stale"] == 3
    clock.tick(600)
    assert big.maintain()["stale"] == 3 and big.stats()["entries"] == 0


def test_stale_evicted_before_a_fresher_valid_entry(tmp_path):
    clock = Clock()
    old, _ = make(tmp_path, version="v1", max_bytes=20, clock=clock)
    new = DiskBudget(old.root, max_bytes=20, version="v2", durable=False, clock=clock)
    new.put("valid", b"v" * 10)  # older than the stale entry below
    clock.tick()
    old.put("stale", b"s" * 10)  # written by the older reader, more recent
    clock.tick()
    new.put("another", b"a" * 10)
    assert new.read("valid") == b"v" * 10 and new.read("another") == b"a" * 10
    assert new.stats()["stale"] == 0


def test_purge_stale_and_rewrite_other_version_replaces(tmp_path):
    clock = Clock()
    b1, _ = make(tmp_path, version="v1", clock=clock)
    b1.put("k", b"1")
    b2 = DiskBudget(b1.root, max_bytes=100, version="v2", durable=False, clock=clock)
    b2.put("k", b"22")  # the same key under the new version replaces the old entry
    assert b2.stats()["entries"] == 1 and b2.stats()["stale"] == 0
    b1.put("j", b"1")  # an entry of the old version appears again
    assert b2.purge_stale() == 1
    assert b2.read("k") == b"22" and b2.stats()["entries"] == 1


def test_per_call_version(tmp_path):
    b, _ = make(tmp_path)
    b.put("k", b"1", version="x")
    assert b.read("k") is None and b.read("k", version="x") == b"1"


# ------------------------------------------------------------------- discard


def test_discard_all_versions(tmp_path):
    b, _ = make(tmp_path)
    b.put("k", b"1", version="a")
    b.put("k2", b"2")
    assert b.discard("k") == 1 and b.read("k2") == b"2" and b.stats()["entries"] == 1


# ------------------------------------------------------------ index and rebuild


def test_index_missing_corrupt_or_wrong_is_rebuilt_from_the_folder(tmp_path):
    b, clock = make(tmp_path, ttl_s=100)
    b.put("a", b"1", ttl_s=5000)
    b.put("b", b"22")
    index = b.root / B.INDEX_NAME
    for damage in (lambda: index.unlink(), lambda: index.write_text("{not json"), lambda: index.write_text("[]"),
                   lambda: index.write_text(json.dumps({"format": B.INDEX_FORMAT, "entries": {"0" * 32 + ".0" * 1: {}}}))):
        damage()
        fresh = DiskBudget(b.root, max_bytes=100, ttl_s=100, durable=False, clock=clock)
        assert fresh.stats()["entries"] == 2 and fresh.stats()["bytes"] == 3
        assert fresh.read("a") == b"1" and fresh.read("b") == b"22"
        assert "0" * 32 + ".0" not in json.loads(index.read_text())["entries"]


def test_files_win_over_the_index(tmp_path):
    b, _ = make(tmp_path)
    b.put("a", b"1")
    changed = b.root / B.entry_name("a", "")
    changed.write_bytes(b"123456")  # changed behind the index's back
    ghost = b.root / B.entry_name("g", "")
    ghost.write_bytes(b"ghost")  # never indexed
    for p in (changed, ghost):
        os.utime(p, (NOW, NOW))  # the fake clock is ahead of the real one
    st = b.stats()
    assert st["entries"] == 2 and st["bytes"] == 11
    b.maintain()
    idx = json.loads((b.root / B.INDEX_NAME).read_text())["entries"]
    assert set(idx) == {B.entry_name("a", ""), B.entry_name("g", "")}
    assert idx[B.entry_name("a", "")]["size"] == 6


def test_index_entry_for_a_deleted_file_is_dropped(tmp_path):
    b, _ = make(tmp_path)
    b.put("a", b"1")
    (b.root / B.entry_name("a", "")).unlink()
    b.maintain()
    assert json.loads((b.root / B.INDEX_NAME).read_text())["entries"] == {}


def test_janitor_style_touch_and_delete_are_respected(tmp_path):
    """The janitor ranks ``<entry>`` by mtime and deletes whole entries; both are what this class reads."""
    b, clock = make(tmp_path, max_bytes=20)
    b.put("a", b"x" * 10)
    clock.tick()
    b.put("b", b"y" * 10)
    old = clock() - 1000
    os.utime(b.root / B.entry_name("b", ""), (old, old))  # b looks idler than a
    b.put("c", b"z" * 10)
    assert b.read("b") is None and b.read("a") == b"x" * 10


# ---------------------------------------------------------- crash consistency


def test_open_removes_orphan_scratch_of_dead_processes(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    dead = _dead_pid()
    (root / f".tmp-{dead}-abc").write_bytes(b"half")
    (root / f".tmp-{dead}-dir").mkdir()
    (root / f".tmp-{dead}-dir" / "f").write_bytes(b"half")
    (root / f".trash-{dead}-ab").mkdir()
    (root / f".tmp-{dead}-abc-index").write_text("{")
    mine = root / f".tmp-{os.getpid()}-beef"  # a write in progress in a live process
    mine.write_bytes(b"in flight")
    b = DiskBudget(root, max_bytes=100, durable=False)
    assert names(b) == sorted([mine.name, B.LOCK_NAME])
    # a live process's scratch older than the grace period is garbage too
    clock = Clock(time.time() + 2 * 3600)
    DiskBudget(root, max_bytes=100, durable=False, clock=clock)
    assert not mine.exists()


def _dead_pid():
    p = multiprocessing.get_context("fork").Process(target=lambda: None)
    p.start()
    p.join()
    return p.pid


def _slow_writer(root, started):
    b = DiskBudget(root, max_bytes=1000, durable=False)
    with b.writing("victim") as tmp, open(tmp, "wb") as f:
        f.write(b"x" * 100)
        f.flush()
        Path(started).write_text("go")
        time.sleep(60)


def test_sigkill_in_the_middle_of_a_write_recovers_consistent(tmp_path):
    root = tmp_path / "cache"
    b = DiskBudget(root, max_bytes=1000, durable=False)
    b.put("kept", b"keep me")
    started = tmp_path / "started"
    ctx = multiprocessing.get_context("fork")
    child = ctx.Process(target=_slow_writer, args=(str(root), str(started)))
    child.start()
    deadline = time.time() + 20
    while not started.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert started.exists()
    assert any(n.startswith(".tmp-") for n in os.listdir(root))
    os.kill(child.pid, signal.SIGKILL)
    child.join()
    # the half-written scratch is there, no entry for it is
    assert b.read("victim") is None and b.stats()["entries"] == 1
    out = b.maintain()
    assert out["orphans"] == 1
    assert [n for n in os.listdir(root) if n.startswith(".tmp-")] == []
    assert b.read("kept") == b"keep me"
    b.put("after", b"ok")  # and it keeps working
    assert b.stats()["entries"] == 2


def _hold_lock(root, started):
    b = DiskBudget(root, max_bytes=1000, durable=False)
    with b._lock():
        Path(started).write_text("go")
        time.sleep(60)


def test_sigkill_while_holding_the_lock_does_not_deadlock(tmp_path):
    root = tmp_path / "cache"
    b = DiskBudget(root, max_bytes=1000, durable=False, lock_timeout_s=0.3)
    started = tmp_path / "started"
    child = multiprocessing.get_context("fork").Process(target=_hold_lock, args=(str(root), str(started)))
    child.start()
    deadline = time.time() + 20
    while not started.exists() and time.time() < deadline:
        time.sleep(0.01)
    with pytest.raises(BudgetRejected) as err:  # while the child is alive the lock is really held
        b.put("blocked", b"x")
    assert err.value.reason == "lock_timeout"
    os.kill(child.pid, signal.SIGKILL)
    child.join()
    b.put("free", b"x")
    assert b.read("free") == b"x"


def test_crash_between_trash_rename_and_removal(tmp_path):
    """A directory entry that was being deleted must not come back as a valid, half-empty entry."""
    b, _ = make(tmp_path)
    with b.writing("d") as tmp:
        tmp.mkdir()
        (tmp / "a").write_bytes(b"1")
        (tmp / "b").write_bytes(b"2")
    entry = b.entries()[0]
    trash = b.root / f".trash-{_dead_pid()}-1"
    os.rename(entry.path, trash)  # the rename happened, the rmtree did not
    (trash / "a").unlink()
    fresh = DiskBudget(b.root, max_bytes=100, durable=False)
    assert fresh.stats()["entries"] == 0 and not trash.exists()


# ----------------------------------------------------------------- concurrency


def test_threads_writing_the_same_key_leave_one_whole_entry(tmp_path):
    b, _ = make(tmp_path, max_bytes=10_000)
    payloads = [bytes([i]) * 500 for i in range(16)]
    errors = []

    def work(p):
        try:
            for _ in range(5):
                b.put("same", p)
                got = b.read("same")
                assert got is None or got in payloads
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert b.stats()["entries"] == 1 and b.read("same") in payloads
    assert not [n for n in names(b) if n.startswith((".tmp-", ".trash-"))]


def _writer_process(root, i):
    b = DiskBudget(root, max_bytes=2000, durable=False)
    for n in range(10):
        b.put(f"k{i}-{n}", bytes([i]) * 100)
        b.put("shared", bytes([i]) * 100)


def test_processes_share_one_limit(tmp_path):
    root = tmp_path / "cache"
    DiskBudget(root, max_bytes=2000, durable=False)
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_writer_process, args=(str(root), i)) for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    b = DiskBudget(root, max_bytes=2000, durable=False)
    st = b.stats()
    assert st["bytes"] <= 2000 and st["entries"] <= 20
    assert b.read("shared") in {bytes([i]) * 100 for i in range(4)}
    assert not [n for n in os.listdir(root) if n.startswith((".tmp-", ".trash-"))]
    idx = json.loads((root / B.INDEX_NAME).read_text())["entries"]
    assert set(idx) == {e.name for e in b.entries()}  # index and folder agree


def test_the_limit_is_never_exceeded_by_a_series(tmp_path):
    b, clock = make(tmp_path, max_bytes=95)
    import random

    rnd = random.Random(7)
    for i in range(200):
        clock.tick()
        try:
            b.put(f"k{rnd.randrange(30)}", b"x" * rnd.randrange(1, 40))
        except BudgetRejected:
            raise AssertionError("every write here fits on its own") from None
        assert b.stats()["bytes"] <= 95


# ------------------------------------------------------------- policy unit


def test_matches_janitor_policy_if_available():
    """The units and defaults agree with ``perf-cache`` in the maintainer's ``janitor_policy.py``."""
    import importlib.util

    path = Path(__file__).resolve().parents[1]
    while path.name != "lab" and path != path.parent:
        path = path.parent
    policy = path / "levi-hub" / "janitor_policy.py"
    if not policy.exists():
        pytest.skip("janitor_policy.py is only in the maintainer's workspace")
    spec = importlib.util.spec_from_file_location("janitor_policy_for_test", policy)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        entry = next(e for e in mod.POLICY if e["name"] == "perf-cache")
    except Exception:  # noqa: BLE001 - a layout this test does not know
        pytest.skip("janitor_policy.py has a different layout")
    assert entry["cap_gb"] == B.DEFAULT_CAP_GB
    assert entry["age_h"] * 3600 == B.DEFAULT_TTL_S


def test_durable_mode_works_for_files_and_directories(tmp_path):
    b = DiskBudget(tmp_path / "cache", max_bytes=100)  # fsync on, the default
    b.put("f", b"data")
    with b.writing("d") as tmp:
        tmp.mkdir()
        (tmp / "x").write_bytes(b"1")
    assert b.read("f") == b"data" and b.stats()["entries"] == 2
