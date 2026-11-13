"""Yield ownership uses only a fake owned tree; no real process/GPU probe."""

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from levi.live import gpumgr
from levi.live.config import Config
from levi.live.controller import Controller

LEADER = 4_000_000
CHILD = LEADER + 1
FOREIGN = LEADER + 100
IDENTITY = {"boot": "fake-boot", "start_ticks": "100"}


def children(root, pid, kids=()):
    task = root / str(pid) / "task" / str(pid)
    task.mkdir(parents=True)
    (task / "children").write_text(" ".join(map(str, kids)))


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / "kernel"
    children(root, LEADER, [CHILD])
    children(root, CHILD)
    ctl = object.__new__(Controller)
    ctl.config = Config()
    ctl.config.gpu.yield_.enabled = True
    ctl.config.gpu.yield_.foreign_gpu_mib = 2000
    ctl.config.gpu.lock_file = ""
    ctl.own_view = gpumgr.OwnUnits(root)
    ctl.vllm = SimpleNamespace(
        _record=lambda: {"pid": LEADER, "identity": IDENTITY},
        _pid=lambda: LEADER,
    )
    ctl.sessions = {}
    ctl._ports_now, ctl._ports_error = set(), ""
    ctl._apps = (0, None, "")
    ctl.probes = SimpleNamespace(apps=list, policy_pids=lambda *a, **kw: set())

    def state(pid, identity):
        assert (pid, identity) == (LEADER, IDENTITY)
        return "alive"

    monkeypatch.setattr(gpumgr, "process_state", state)
    return ctl, root


def codes(ctl):
    return [r.code for r in ctl._yield_reasons(10, True)]


def test_unrelated_stat_failure_does_not_pollute_the_owned_tree(owned, monkeypatch):
    ctl, root = owned
    scan, read = os.scandir, Path.read_text
    scanned, foreign_reads = [], []

    def scan_owned(path):
        scanned.append(os.fspath(path))
        # Reproduce the old scan's unrelated process read deterministically.
        return scan(root) if os.fspath(path) == "/proc" else scan(path)

    def read_owned(path, *args, **kwargs):
        if str(path) in {f"/proc/{LEADER}/stat", f"/proc/{CHILD}/stat"}:
            return f"{path.parent.name} (model) S 1 {LEADER} 0"
        if str(path) == f"/proc/{FOREIGN}/stat":
            foreign_reads.append(str(path))
            raise PermissionError(errno.EACCES, "fake unrelated stat denied")
        assert not str(path).startswith("/proc/"), path
        return read(path, *args, **kwargs)

    (root / str(FOREIGN)).mkdir()
    monkeypatch.setattr(os, "scandir", scan_owned)
    monkeypatch.setattr(Path, "read_text", read_owned)
    with pytest.raises(PermissionError):
        gpumgr._group_alive(LEADER)
    assert foreign_reads
    scanned.clear()
    foreign_reads.clear()

    ctl.probes.apps = lambda: [{"pid": CHILD, "name": "model", "memory_mib": 9000}]
    assert codes(ctl) == []
    assert ctl._our_gpu_pids() == {os.getpid(), LEADER, CHILD}
    assert "/proc" not in scanned and not foreign_reads
    assert all(path.startswith(str(root)) for path in scanned)


def test_unreadable_proc_root_does_not_pollute_owned_probe(owned, monkeypatch):
    ctl, _ = owned
    scan = os.scandir
    scanned = []

    def denied_root(path):
        scanned.append(os.fspath(path))
        if os.fspath(path) == "/proc":
            raise PermissionError(errno.EACCES, "unrelated proc root denied")
        return scan(path)

    monkeypatch.setattr(os, "scandir", denied_root)
    with pytest.raises(PermissionError):
        gpumgr._group_alive(LEADER)
    scanned.clear()
    ctl.probes.apps = lambda: [{"pid": CHILD, "name": "model", "memory_mib": 9000}]
    assert codes(ctl) == []
    assert ctl._our_gpu_pids() == {os.getpid(), LEADER, CHILD}
    assert scanned and "/proc" not in scanned


@pytest.mark.parametrize("memory", [9000, None])
def test_unknown_gpu_process_is_still_foreign(owned, memory):
    ctl, _ = owned
    ctl.probes.apps = lambda: [
        {"pid": CHILD, "name": "model", "memory_mib": 9000},
        {"pid": FOREIGN, "name": "unknown", "memory_mib": memory},
    ]
    assert codes(ctl) == ["foreign_gpu"]
    assert FOREIGN not in ctl._our_gpu_pids()


def test_unseen_deep_and_orphan_gpu_processes_stay_foreign(owned):
    ctl, root = owned
    # The existing depth=4 traversal finds through LEADER+4, never +5.
    for offset in range(1, 5):
        task = root / str(LEADER + offset) / "task" / str(LEADER + offset)
        task.mkdir(parents=True, exist_ok=True)
        (task / "children").write_text(str(LEADER + offset + 1))
    children(root, LEADER + 5)
    children(root, FOREIGN)  # orphan: not in the recorded leader's children
    ours = ctl._our_gpu_pids()
    assert LEADER + 4 in ours and LEADER + 5 not in ours and FOREIGN not in ours
    ctl.probes.apps = lambda: [
        {"pid": LEADER + 5, "name": "deep", "memory_mib": 9000},
        {"pid": FOREIGN, "name": "orphan", "memory_mib": 9000},
    ]
    assert codes(ctl) == ["foreign_gpu"]


@pytest.mark.parametrize("failure", ["missing", "malformed", "permission"])
def test_unreadable_owned_children_fail_closed(owned, monkeypatch, failure):
    ctl, root = owned
    path = root / str(CHILD) / "task" / str(CHILD) / "children"
    if failure == "missing":
        path.unlink()
    elif failure == "malformed":
        path.write_text("not-a-pid")
    else:
        read = Path.read_text

        def denied(entry, *args, **kwargs):
            if entry == path:
                raise PermissionError(errno.EACCES, "private diagnostic must not escape")
            return read(entry, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    reasons = ctl._yield_reasons(10, True)
    assert [r.code for r in reasons] == ["probe_error"]
    if failure == "permission":
        assert "PermissionError, errno=13" in reasons[0].text
        assert "private diagnostic" not in reasons[0].text


@pytest.mark.parametrize("states", [("unknown",), ("alive", "alive", "gone")])
def test_unknown_or_changed_leader_identity_fails_closed(owned, monkeypatch, states):
    ctl, _ = owned
    values = iter(states)
    monkeypatch.setattr(gpumgr, "process_state", lambda *a: next(values))
    assert codes(ctl) == ["probe_error"]


def test_changed_pidfile_fails_closed(owned):
    ctl, _ = owned
    ctl.vllm._pid = lambda: FOREIGN
    assert codes(ctl) == ["probe_error"]


@pytest.mark.parametrize("record", [None, {"pid": LEADER, "identity": IDENTITY}])
def test_absent_or_gone_owner_exempts_no_model_gpu(owned, monkeypatch, record):
    ctl, _ = owned
    ctl.vllm._record = lambda: record
    monkeypatch.setattr(gpumgr, "process_state", lambda *a: "gone")
    ctl.probes.apps = lambda: [{"pid": CHILD, "name": "unknown", "memory_mib": 9000}]
    assert ctl._our_gpu_pids() == {os.getpid()}
    assert codes(ctl) == ["foreign_gpu"]
