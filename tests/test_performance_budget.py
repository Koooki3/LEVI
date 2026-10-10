"""``levi.performance.budget.DiskBudget``: limits and reservations, LRU, TTL, versions, read leases, atomic
writes, crash recovery."""

import errno
import multiprocessing
import os
import random
import signal
import stat
import threading
import time
from pathlib import Path

import pytest

from levi.performance import budget as B
from levi.performance.budget import BudgetRejected, DiskBudget

NOW = time.time()
P = 1000  # a payload; an entry is this plus a small .meta file
MAX3 = 3500  # room for three such entries, not four


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
    kw.setdefault("max_bytes", MAX3)
    kw.setdefault("durable", False)
    return DiskBudget(tmp_path / "cache", **kw), clock


def top(b):
    return sorted(p.name for p in b.root.iterdir())


def du(path):
    """Bytes of regular files under ``path``, tolerant of files that vanish while walking."""
    total = 0
    for d, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return total


def scratch_left(b):
    return [p.name for p in (b.root / ".budget" / "w").iterdir()] + [
        p.name for p in (b.root / ".budget" / "trash").iterdir()
    ]


def assert_consistent(b):
    """No leftovers; every entry has its .meta; every name carries the entry's real size."""
    assert scratch_left(b) == []
    assert all(B._ENTRY.match(n) or n == ".budget" for n in top(b))
    for e in b.entries():
        assert (e.path / B.META_NAME).exists()
        assert B._tree_size(e.path) == e.size


def put_many(b, clock, keys, size=P):
    for k in keys:
        b.put(k, b"x" * size)
        clock.tick()


# ---------------------------------------------------------------- basics


def test_put_get_read_roundtrip(tmp_path):
    b, _ = make(tmp_path)
    e = b.put("k", b"hello")
    assert e.key == "k" and e.size == B._tree_size(e.path)
    assert b.read("k") == b"hello"
    with b.get("k") as lease:
        assert (lease.path / "data").read_bytes() == b"hello"
    assert b.get("missing") is None and b.read("missing") is None
    assert b.stats()["entries"] == 1 and b.stats()["bytes"] == e.size


def test_layout_is_entries_plus_one_hidden_folder(tmp_path):
    b, _ = make(tmp_path)
    b.put("a", b"1")
    assert [n for n in top(b) if n.startswith(".")] == [".budget"]
    assert {p.name for p in (b.root / ".budget").iterdir()} == {"w", "trash"}


def test_put_same_key_replaces(tmp_path):
    b, _ = make(tmp_path)
    b.put("k", b"aaaa")
    b.put("k", b"bb")
    assert b.read("k") == b"bb"
    assert b.stats()["entries"] == 1


def test_cap_gb_is_gib():
    assert B.GIB == 2**30
    assert (B.DEFAULT_CAP_GB, B.DEFAULT_TTL_S, B.DEFAULT_STALE_GRACE_S) == (20, 14 * 86400, 7 * 86400)


def test_from_cap_gb(tmp_path):
    assert DiskBudget.from_cap_gb(tmp_path / "c", 0.5, durable=False).max_bytes == 2**29


def test_max_bytes_must_leave_room_for_bookkeeping(tmp_path):
    with pytest.raises(ValueError):
        DiskBudget(tmp_path / "c", max_bytes=100)


def test_directory_entries(tmp_path):
    b, _ = make(tmp_path)
    with b.writing("ev", reserve=500) as tmp:
        (tmp / "sub").mkdir()
        (tmp / "a.png").write_bytes(b"x" * 100)
        (tmp / "sub" / "b.png").write_bytes(b"y" * 50)
    e = b.entries()[0]
    assert e.size >= 150
    with b.get("ev") as lease:
        assert (lease.path / "sub" / "b.png").read_bytes() == b"y" * 50
    assert b.read("ev") is None  # no "data" file


def test_foreign_names_are_not_managed(tmp_path):
    b, _ = make(tmp_path)
    (b.root / "README").write_text("not ours")
    b.put("k", b"1")
    b.maintain()
    assert (b.root / "README").exists() and b.stats()["entries"] == 1


def test_symlink_in_an_entry_is_refused(tmp_path):
    b, _ = make(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"x" * 5000)
    with pytest.raises(BudgetRejected) as err, b.writing("l", reserve=100) as tmp:
        os.symlink(outside, tmp / "link")
    assert err.value.reason == "no_output"
    assert b.stats()["entries"] == 0 and scratch_left(b) == []


# ------------------------------------------------------------------ limits


def test_lru_eviction_order(tmp_path):
    b, clock = make(tmp_path)
    put_many(b, clock, "abc")
    clock.tick()
    assert b.read("a") is not None  # a is now the most recent
    clock.tick()
    b.put("d", b"x" * P)  # needs room: b is the least recently used
    assert b.read("b") is None
    assert all(b.read(k) is not None for k in "acd")
    assert b.stats()["bytes"] <= MAX3


def test_eviction_frees_enough_for_a_large_write(tmp_path):
    b, clock = make(tmp_path)
    put_many(b, clock, "abc")
    b.put("big", b"y" * 2200)
    assert b.stats()["bytes"] <= MAX3 and b.read("big") == b"y" * 2200
    assert b.stats()["entries"] == 2  # big + one old; the reservation was larger than the data


def test_entry_limit(tmp_path):
    b, clock = make(tmp_path, max_bytes=100_000, max_entries=2)
    put_many(b, clock, "abc", size=10)
    assert b.stats()["entries"] == 2
    assert b.read("a") is None and b.read("c") is not None


def test_too_large_is_refused_before_anything_happens(tmp_path):
    b, clock = make(tmp_path)
    put_many(b, clock, "a")
    with pytest.raises(BudgetRejected) as err:
        b.put("huge", b"x" * MAX3)
    assert err.value.reason == "too_large"
    assert b.read("a") is not None and scratch_left(b) == []

    def body():
        with b.writing("w", reserve=MAX3):
            raise AssertionError("must be refused before the body runs")

    with pytest.raises(BudgetRejected):
        body()


def test_raw_write_beyond_the_reserve_is_caught_at_commit(tmp_path):
    b, clock = make(tmp_path)
    put_many(b, clock, "a")
    with pytest.raises(BudgetRejected) as err, b.writing("w", reserve=100) as tmp:
        (tmp / "f").write_bytes(b"x" * 600)
    assert err.value.reason == "too_large"
    assert b.stats()["entries"] == 1 and scratch_left(b) == []


def test_writer_exception_leaves_nothing(tmp_path):
    b, _ = make(tmp_path)
    with pytest.raises(RuntimeError), b.writing("w", reserve=100) as tmp:
        (tmp / "f").write_bytes(b"partial")
        raise RuntimeError("boom")
    assert b.stats()["entries"] == 0 and b.stats()["writers"] == 0 and scratch_left(b) == []


# ------------------------------------- I1: the limit covers data being written


def test_metered_writes_stop_at_the_reserve_and_the_disk_never_exceeds_the_limit(tmp_path):
    b, _ = make(tmp_path, max_bytes=2000)
    peak = 0
    with (
        pytest.raises(BudgetRejected) as err,
        b.writing("k", reserve=1000) as tmp,
        b.open_file(tmp / "f") as f,
    ):
        for _ in range(500):
            f.write(b"x" * 100)
            f.flush()
            peak = max(peak, du(b.root))
    assert err.value.reason == "too_large"
    assert peak <= 1000 + 64  # the reserve, plus the writer's record
    assert scratch_left(b) == []


def test_charge_measures_unmetered_writes(tmp_path):
    b, _ = make(tmp_path)
    with pytest.raises(BudgetRejected), b.writing("k", reserve=300) as tmp:
        (tmp / "f").write_bytes(b"x" * 200)
        assert b.charge(tmp) == 200
        (tmp / "g").write_bytes(b"x" * 200)
        b.charge(tmp)


def test_open_file_outside_a_scratch_folder_is_refused(tmp_path):
    b, _ = make(tmp_path)
    with pytest.raises(ValueError):
        b.open_file(tmp_path / "elsewhere")


def test_reservations_count_against_the_limit(tmp_path):
    b, clock = make(tmp_path, max_bytes=3000)
    put_many(b, clock, "a", size=500)
    with b.writing("w1", reserve=1500):
        st = b.stats()
        assert st["writers"] == 1 and st["reserved_bytes"] == 1500 + B.META_ALLOWANCE
        with pytest.raises(BudgetRejected) as err, b.writing("w2", reserve=1500):
            raise AssertionError
        assert err.value.reason == "busy"
        with pytest.raises(BudgetRejected):
            b.put("p", b"x" * 1500)
        b.put("small", b"x" * 100)  # what is left still fits
    assert b.stats()["writers"] == 0


def test_a_writer_makes_room_before_it_writes(tmp_path):
    b, clock = make(tmp_path)
    put_many(b, clock, "abc")
    with b.writing("w", reserve=2000):  # evicts while reserving, not at commit
        assert b.stats()["entries"] <= 1
        assert b.stats()["bytes"] + b.stats()["reserved_bytes"] <= MAX3


def test_an_unsized_writer_gets_a_quarter_of_the_budget(tmp_path):
    b, _ = make(tmp_path, max_bytes=10_000)
    with b.writing("w"):
        assert b.stats()["reserved_bytes"] == (10_000 - B.META_ALLOWANCE) // 4 + B.META_ALLOWANCE


def _bounded_writer(root, i, rounds):
    b = DiskBudget(root, max_bytes=6000, durable=False)
    for n in range(rounds):
        try:
            b.put(f"k{i}-{n % 3}", bytes([i]) * 2000)
        except BudgetRejected as exc:
            assert exc.reason == "busy"


def test_many_processes_never_exceed_the_limit_on_disk(tmp_path):
    root = tmp_path / "cache"
    DiskBudget(root, max_bytes=6000, durable=False)
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_bounded_writer, args=(str(root), i, 25)) for i in range(8)]
    for p in procs:
        p.start()
    peak = 0
    while any(p.is_alive() for p in procs):
        # a rename between two directories can be counted twice by a walk: only a value seen three times counts
        peak = max(peak, min(du(root) for _ in range(3)))
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    assert peak <= 6000 + 256, peak
    b = DiskBudget(root, max_bytes=6000, durable=False)
    assert b.stats()["bytes"] <= 6000
    assert_consistent(b)


# ---------------------------------------------- I2: live writers are never reclaimed


def test_a_live_writer_is_not_reclaimed_however_old_its_files_look(tmp_path):
    clock = Clock(time.time())
    b, _ = make(tmp_path, clock=clock, max_bytes=100_000)
    with b.writing("k", reserve=1000) as tmp:
        (tmp / "sub").mkdir()
        os.utime(tmp, (clock() - 10**6, clock() - 10**6))
        clock.tick(10**6)
        other = DiskBudget(b.root, max_bytes=100_000, durable=False, clock=clock)  # opens, runs maintain
        assert other.maintain()["writers"] == 0
        (tmp / "sub" / "f").write_bytes(b"data")
    assert b.stats()["entries"] == 1


def _write_slowly(root, started):
    b = DiskBudget(root, max_bytes=100_000, durable=False)
    with b.writing("victim", reserve=1000) as tmp:
        (tmp / "f").write_bytes(b"x" * 100)
        Path(started).write_text("go")
        time.sleep(60)


def wait_for(path, timeout=20):
    deadline = time.time() + timeout
    while not Path(path).exists() and time.time() < deadline:
        time.sleep(0.01)
    assert Path(path).exists()


def test_sigkill_in_the_middle_of_a_write_is_reclaimed_by_a_running_instance(tmp_path):
    root = tmp_path / "cache"
    b = DiskBudget(root, max_bytes=100_000, durable=False, housekeeping_s=0)
    b.put("kept", b"keep me")
    started = tmp_path / "started"
    child = multiprocessing.get_context("fork").Process(target=_write_slowly, args=(str(root), str(started)))
    child.start()
    wait_for(started)
    assert b.stats()["writers"] == 1 and b.stats()["reserved_bytes"] > 0
    os.kill(child.pid, signal.SIGKILL)
    child.join()
    # a long-running instance reclaims on its next write, without being reopened
    b.put("after", b"ok")
    assert scratch_left(b) == [] and b.stats()["writers"] == 0
    assert b.read("kept") == b"keep me" and b.read("victim") is None
    assert_consistent(b)


def test_a_writer_whose_scratch_was_removed_gets_a_refusal_not_a_crash(tmp_path):
    b, _ = make(tmp_path)
    with pytest.raises(BudgetRejected) as err, b.writing("k", reserve=100) as tmp:
        import shutil

        shutil.rmtree(tmp.parent)  # an outside cleaner removed it
        (tmp / "sub" / "f").write_bytes(b"x")
    assert err.value.reason == "reclaimed"
    with pytest.raises(BudgetRejected) as err, b.writing("k2", reserve=100) as tmp:
        import shutil

        shutil.rmtree(tmp.parent)
    assert err.value.reason == "reclaimed"


# ------------------------------------------------------------ I3: read leases


def make_dir_entry(b, key, n=2):
    with b.writing(key, reserve=1000) as tmp:
        for i in range(n):
            (tmp / f"{i}.png").write_bytes(bytes([i]) * 10)


def test_a_lease_holds_one_immutable_entry_while_it_is_rewritten(tmp_path):
    b, _ = make(tmp_path, max_bytes=100_000)
    make_dir_entry(b, "k")
    lease = b.get("k")
    assert (lease.path / "0.png").read_bytes() == bytes([0]) * 10
    other = DiskBudget(b.root, max_bytes=100_000, durable=False)
    other.put("k", b"new")  # the rewrite is a new generation
    assert (lease.path / "1.png").read_bytes() == bytes([1]) * 10  # still the old, whole
    assert other.read("k") == b"new"
    assert b.stats()["entries"] == 2  # the old generation is kept for the reader and still counted
    lease.close()
    assert other.maintain()["superseded"] == 0 or other.stats()["entries"] == 1
    assert other.stats()["entries"] == 1 and other.read("k") == b"new"


def test_a_leased_entry_is_not_evicted(tmp_path):
    b, clock = make(tmp_path)
    put_many(b, clock, "abc")
    lease = b.get("a")  # also the most recently used now
    clock.tick()
    b.put("d", b"x" * P)
    b.put("e", b"x" * P)
    assert (lease.path / "data").exists()
    lease.close()
    clock.tick()
    assert b.stats()["bytes"] <= MAX3


def test_leases_can_make_a_write_busy_and_releasing_them_helps(tmp_path):
    b, clock = make(tmp_path)
    put_many(b, clock, "abc")
    leases = [b.get(k) for k in "abc"]
    with pytest.raises(BudgetRejected) as err:
        b.put("d", b"x" * P)
    assert err.value.reason == "busy"
    assert b.stats()["entries"] == 3
    for lease in leases:
        lease.close()
    b.put("d", b"x" * P)
    assert b.read("d") is not None


def test_a_leased_entry_survives_discard_and_expiry(tmp_path):
    b, clock = make(tmp_path, ttl_s=100)
    b.put("k", b"1")
    lease = b.get("k")
    assert b.discard("k") == 0
    clock.tick(500)
    assert b.maintain()["expired"] == 0 and (lease.path / "data").exists()
    lease.close()
    assert b.discard("k") == 1


def test_reading_context_releases_the_lease(tmp_path):
    b, _ = make(tmp_path)
    b.put("k", b"1")
    with b.reading("k") as path:
        assert (path / "data").read_bytes() == b"1"
        assert b.discard("k") == 0
    with b.reading("nope") as path:
        assert path is None
    assert b.discard("k") == 1


def test_a_forgotten_lease_does_not_block_for_ever(tmp_path):
    b, _ = make(tmp_path)
    b.put("k", b"1")
    b.get("k")  # dropped at once
    import gc

    gc.collect()
    assert b.discard("k") == 1


# ------------------------------------- I4: lock, layout, use visible to other tools


def test_cleaning_the_hidden_folder_does_not_split_the_exclusion(tmp_path):
    import shutil

    b, _ = make(tmp_path, lock_timeout_s=0.3)
    inside, release = threading.Event(), threading.Event()

    def hold():
        with b._lock():
            inside.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    assert inside.wait(5)
    shutil.rmtree(b.root / ".budget")  # a cleaner took everything it could match while the lock is held
    try:
        with pytest.raises(BudgetRejected) as err:
            b.put("x", b"1")
        assert err.value.reason == "lock_timeout"
    finally:
        release.set()
        t.join()
    b.put("x", b"1")  # the hidden folder is recreated on demand
    assert b.read("x") == b"1"


def test_a_use_is_visible_to_a_tool_that_only_looks_at_files(tmp_path):
    b, _ = make(tmp_path, max_bytes=100_000, clock=time.time)
    make_dir_entry(b, "k")
    entry = b.entries()[0].path
    old = time.time() - 100
    for p in (entry / "0.png", entry / "1.png", entry / B.META_NAME):
        os.utime(p, (old, old))
    os.utime(entry, (old, old))

    def newest_file():
        return max(os.lstat(p).st_mtime for p in entry.rglob("*") if p.is_file())

    assert newest_file() <= old + 1
    b.get("k").close()
    assert newest_file() > old + 50  # .meta carries the use
    assert os.lstat(entry).st_mtime > old + 50


def test_an_entry_half_deleted_in_place_is_dropped(tmp_path):
    b, _ = make(tmp_path)
    make_dir_entry(b, "k")
    entry = b.entries()[0].path
    (entry / B.META_NAME).unlink()  # some other tool started deleting it, .meta went first
    (entry / "0.png").unlink()
    assert b.get("k") is None
    assert b.stats()["entries"] == 0 and top(b) == [".budget"]
    # also found by maintenance when nobody reads it
    make_dir_entry(b, "j")
    (b.entries()[0].path / B.META_NAME).unlink()
    assert b.maintain()["damaged"] == 1


def test_the_hidden_folder_is_never_an_entry(tmp_path):
    b, _ = make(tmp_path)
    assert b.stats()["entries"] == 0 and b.maintain()["pruned"] == 0


# -------------------------------------------------------- I5: fsync failures


def test_fsync_failure_aborts_the_commit(tmp_path, monkeypatch):
    b, _ = make(tmp_path, durable=True)

    def eio(fd):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fsync", eio)
    with pytest.raises(OSError) as err:
        b.put("k", b"data")
    assert not isinstance(err.value, BudgetRejected) and err.value.errno == errno.EIO
    monkeypatch.undo()
    assert b.read("k") is None and b.stats()["entries"] == 0 and scratch_left(b) == []


def test_fsync_enospc_is_a_refusal(tmp_path, monkeypatch):
    b, _ = make(tmp_path, durable=True)

    def full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(BudgetRejected) as err:
        b.put("k", b"data")
    monkeypatch.undo()
    assert err.value.reason == "no_space" and b.stats()["entries"] == 0


def test_directory_fsync_not_supported_is_ignored(tmp_path, monkeypatch):
    b, _ = make(tmp_path, durable=True)
    real = os.fsync

    def picky(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        real(fd)

    monkeypatch.setattr(os, "fsync", picky)
    b.put("k", b"data")
    monkeypatch.undo()
    assert b.read("k") == b"data"


def test_durable_mode_works_for_files_and_directories(tmp_path):
    b = DiskBudget(tmp_path / "cache", max_bytes=100_000)
    b.put("f", b"data")
    make_dir_entry(b, "d")
    assert b.read("f") == b"data" and b.stats()["entries"] == 2


def test_other_oserror_is_not_swallowed(tmp_path):
    b, _ = make(tmp_path)
    with pytest.raises(PermissionError), b.writing("w", reserve=10) as tmp:
        (tmp / "f").write_bytes(b"x")
        raise PermissionError(errno.EACCES, "no")


def test_enospc_from_the_writer_is_a_refusal(tmp_path):
    b, _ = make(tmp_path)
    b.put("a", b"1")
    with pytest.raises(BudgetRejected) as err, b.writing("w", reserve=10) as tmp:
        (tmp / "f").write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")
    assert err.value.reason == "no_space"
    assert b.read("a") == b"1" and scratch_left(b) == []


# ------------------------------------------------------- I6: free-space floor


def test_free_space_floor_counts_the_data_once_and_checks_before_writing(tmp_path, monkeypatch):
    b, _ = make(tmp_path, max_bytes=100_000, min_free_bytes=5000)
    disk = 10_000

    monkeypatch.setattr(B, "_free_bytes", lambda path: disk - du(b.root))
    b.put("k", b"x" * 3000)  # 10000 - 3000 = 7000 >= 5000
    assert b.read("k") is not None
    ran = []
    with (
        pytest.raises(BudgetRejected) as err,
        b.writing("big", reserve=2500) as tmp,  # 7000 - 2500 < 5000 once the existing entry stays
    ):
        ran.append(tmp)
    assert err.value.reason == "no_space"
    assert ran == []  # refused before the body ran, not after the write


def test_free_space_floor_counts_what_eviction_gives_back(tmp_path, monkeypatch):
    b, clock = make(tmp_path, max_bytes=2600, min_free_bytes=1000)
    monkeypatch.setattr(B, "_free_bytes", lambda path: 1000)
    with pytest.raises(BudgetRejected):
        b.put("a", b"x")  # nothing to give back: free 1000 - 1 < floor
    monkeypatch.setattr(B, "_free_bytes", lambda path: 3000)
    put_many(b, clock, "ab")
    monkeypatch.setattr(B, "_free_bytes", lambda path: 1300)
    # a rewrite of "a" evicts the old "a" first, which gives its bytes back to the disk: 1300 + 1000 - 1256 >= 1000
    b.put("a", b"z" * P)
    assert b.read("a") == b"z" * P


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
    assert b.stats()["entries"] == 0  # gone, not just hidden


def test_expired_entries_are_dropped_to_make_room_and_by_maintain(tmp_path):
    b, clock = make(tmp_path, ttl_s=100, housekeeping_s=0)
    b.put("a", b"x" * P)
    clock.tick(50)
    b.put("b", b"y" * P)
    clock.tick(60)  # a idle 110 (expired), b idle 60
    b.put("c", b"z" * P)
    b.put("d", b"z" * P)
    assert b.read("a") is None and b.read("b") is not None  # a went as expired, b was not evicted for it
    clock.tick(500)
    assert b.maintain()["expired"] == 3 and b.stats()["entries"] == 0


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
    new = DiskBudget(old.root, max_bytes=MAX3, version="schema-2", durable=False, clock=clock)
    assert new.read("k") is None and new.get("k") is None
    assert old.read("k") == b"old"
    assert new.stats()["stale"] == 1


def test_stale_entries_go_first_and_after_the_grace_period(tmp_path):
    clock = Clock()
    old, _ = make(tmp_path, version="v1", clock=clock)
    old.put("o", b"x" * P)
    clock.tick()
    new = DiskBudget(old.root, max_bytes=MAX3, version="v2", durable=False, clock=clock, stale_grace_s=1000)
    put_many(new, clock, "ab")
    new.put("c", b"c" * P)  # three valid + one stale: the stale one goes, not a valid one
    assert new.stats()["stale"] == 0 and all(new.read(k) for k in "abc")
    big = DiskBudget(old.root, max_bytes=100_000, version="v3", durable=False, clock=clock, stale_grace_s=1000)
    clock.tick(500)
    assert big.maintain()["stale"] == 0 and big.stats()["stale"] == 3
    clock.tick(600)
    assert big.maintain()["stale"] == 3 and big.stats()["entries"] == 0


def test_stale_is_evicted_before_a_less_recently_used_valid_entry(tmp_path):
    clock = Clock()
    old, _ = make(tmp_path, version="v1", clock=clock)
    new = DiskBudget(old.root, max_bytes=2300, version="v2", durable=False, clock=clock)
    new.put("valid", b"v" * P)
    clock.tick()
    old.put("stale", b"s" * P)  # more recent, but of another version
    clock.tick()
    new.put("another", b"a" * P)
    assert new.read("valid") and new.read("another") and new.stats()["stale"] == 0


def test_purge_stale_and_rewrite_under_the_new_version(tmp_path):
    clock = Clock()
    b1, _ = make(tmp_path, version="v1", clock=clock)
    b1.put("k", b"1")
    b2 = DiskBudget(b1.root, max_bytes=MAX3, version="v2", durable=False, clock=clock)
    b2.put("k", b"22")  # the same key under the new version replaces the old entry
    assert b2.stats()["entries"] == 1 and b2.stats()["stale"] == 0
    b1.put("j", b"1")
    assert b2.purge_stale() == 1
    assert b2.read("k") == b"22" and b2.stats()["entries"] == 1


def test_per_call_version(tmp_path):
    b, _ = make(tmp_path)
    b.put("k", b"1", version="x")
    assert b.read("k") is None and b.read("k", version="x") == b"1"


def test_discard_all_versions(tmp_path):
    b, _ = make(tmp_path)
    b.put("k", b"1", version="a")
    b.put("k2", b"2")
    assert b.discard("k") == 1 and b.read("k2") == b"2" and b.stats()["entries"] == 1


# ------------------------------------------------------------- maintenance


def test_maintain_corrects_a_size_that_changed_behind_its_back(tmp_path):
    b, _ = make(tmp_path)
    b.put("a", b"1")
    entry = b.entries()[0]
    (entry.path / "extra").write_bytes(b"x" * 700)
    assert b.stats()["bytes"] == entry.size
    assert b.maintain()["resized"] == 1
    assert b.stats()["bytes"] == entry.size + 700
    assert b.read("a") == b"1"


def test_maintain_prunes_to_a_smaller_limit(tmp_path):
    b, clock = make(tmp_path, max_bytes=10_000)
    put_many(b, clock, "abcde")
    small = DiskBudget(b.root, max_bytes=2200, durable=False, clock=clock)  # opens, maintains
    assert small.stats()["bytes"] <= 2200 and small.stats()["entries"] == 2
    assert small.read("e") is not None  # the most recent ones stay


def test_an_outside_cleaner_deleting_whole_entries_is_just_a_miss(tmp_path):
    import shutil

    b, clock = make(tmp_path)
    put_many(b, clock, "ab")
    shutil.rmtree(b.entries()[0].path)
    assert b.stats()["entries"] == 1
    b.put("c", b"x" * P)
    assert b.stats()["entries"] == 2


def test_the_folder_vanishing_does_not_crash(tmp_path):
    import shutil

    b, _ = make(tmp_path)
    b.put("a", b"1")
    shutil.rmtree(b.root)
    assert b.read("a") is None
    b.put("b", b"2")
    assert b.read("b") == b"2"


def test_housekeeping_runs_in_a_long_lived_instance(tmp_path):
    clock = Clock()
    b, _ = make(tmp_path, ttl_s=100, housekeeping_s=0, clock=clock)
    b.put("a", b"1")
    clock.tick(500)
    b.put("b", b"2")  # no reopen, no maintain(): the idle entry still goes
    assert b.read("a") is None and b.stats()["entries"] == 1


# ---------------------------------------------------------- crash consistency


def _dead_pid():
    p = multiprocessing.get_context("fork").Process(target=lambda: None)
    p.start()
    p.join()
    return p.pid


def test_open_removes_orphan_writers_and_trash(tmp_path):
    root = tmp_path / "cache"
    DiskBudget(root, max_bytes=MAX3, durable=False)
    dead = _dead_pid()
    w = root / ".budget" / "w"
    (w / f"{dead}-aa.lock").write_text('{"reserved": 99999}')  # a record nobody holds
    (w / f"{dead}-aa" / "p").mkdir(parents=True)
    (w / f"{dead}-aa" / "p" / "f").write_bytes(b"half")
    (root / ".budget" / "trash" / "zz").mkdir()
    (root / ".budget" / "trash" / "zz" / "f").write_bytes(b"half")
    again = DiskBudget(root, max_bytes=MAX3, durable=False)
    assert again.stats()["writers"] == 0 and again.stats()["reserved_bytes"] == 0
    assert scratch_left(again) == []


def _crash_at(root, point, started):
    def hook(p):
        if p == point:
            Path(started).write_text("go")
            time.sleep(60)

    DiskBudget._hook = staticmethod(hook)
    b = DiskBudget(root, max_bytes=MAX3, durable=False)
    for k in "abc":
        b.put(k, b"x" * 1000)
    b.put("a", b"y" * 1000)  # a rewrite that evicts: touches every step


@pytest.mark.parametrize("point", ["commit:before_rename", "commit:after_rename", "delete:after_trash_rename"])
def test_sigkill_at_each_step_of_commit_and_delete_recovers_consistent(tmp_path, point):
    root = tmp_path / "cache"
    started = tmp_path / "started"
    child = multiprocessing.get_context("fork").Process(target=_crash_at, args=(str(root), point, str(started)))
    child.start()
    wait_for(started)
    os.kill(child.pid, signal.SIGKILL)
    child.join()
    b = DiskBudget(root, max_bytes=MAX3, durable=False)  # opens: reclaims
    assert_consistent(b)
    assert b.stats()["bytes"] <= 3300 and b.stats()["writers"] == 0
    for e in b.entries():
        assert b.read(e.key) is not None  # whatever survived is whole
    b.put("after", b"ok")
    assert b.read("after") == b"ok"
    assert_consistent(b)


def _hold_lock(root, started):
    b = DiskBudget(root, max_bytes=MAX3, durable=False)
    with b._lock():
        Path(started).write_text("go")
        time.sleep(60)


def test_sigkill_while_holding_the_lock_does_not_deadlock(tmp_path):
    root = tmp_path / "cache"
    b = DiskBudget(root, max_bytes=MAX3, durable=False, lock_timeout_s=0.3)
    started = tmp_path / "started"
    child = multiprocessing.get_context("fork").Process(target=_hold_lock, args=(str(root), str(started)))
    child.start()
    wait_for(started)
    with pytest.raises(BudgetRejected) as err:  # while the child is alive the lock is really held
        b.put("blocked", b"x")
    assert err.value.reason == "lock_timeout"
    os.kill(child.pid, signal.SIGKILL)
    child.join()
    b.put("free", b"x")
    assert b.read("free") == b"x"


# ----------------------------------------------------------------- concurrency


def test_threads_writing_the_same_key_leave_one_whole_entry(tmp_path):
    b, _ = make(tmp_path, max_bytes=100_000)
    payloads = [bytes([i]) * 500 for i in range(16)]
    errors = []

    def work(p):
        try:
            for _ in range(5):
                try:
                    b.put("same", p)
                except BudgetRejected as exc:
                    assert exc.reason == "busy"
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
    b.maintain()
    assert b.stats()["entries"] == 1 and b.read("same") in payloads
    assert_consistent(b)


def _mixed_process(root, i):
    b = DiskBudget(root, max_bytes=20_000, durable=False)
    for n in range(20):
        try:
            b.put(f"k{i}-{n}", bytes([i]) * 700)
            b.put("shared", bytes([i]) * 700)
        except BudgetRejected as exc:
            assert exc.reason == "busy"


def test_processes_share_one_limit(tmp_path):
    root = tmp_path / "cache"
    DiskBudget(root, max_bytes=20_000, durable=False)
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_mixed_process, args=(str(root), i)) for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    b = DiskBudget(root, max_bytes=20_000, durable=False)
    assert b.stats()["bytes"] <= 20_000
    assert b.read("shared") in {bytes([i]) * 700 for i in range(4)}
    assert_consistent(b)


def test_the_limit_is_never_exceeded_by_a_series(tmp_path):
    b, clock = make(tmp_path, max_bytes=4000)
    rnd = random.Random(7)
    for _ in range(150):
        clock.tick()
        b.put(f"k{rnd.randrange(30)}", b"x" * rnd.randrange(1, 900))
        assert b.stats()["bytes"] <= 4000
