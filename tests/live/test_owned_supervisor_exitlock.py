"""Real CPU supervisor, inherited flock and controlled fake containment."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from levi.live import owned_supervisor as sup

HARNESS = r"""
import json, os, signal, subprocess, sys, threading
from pathlib import Path
from types import SimpleNamespace
from levi.live import owned_runtime as owned
from levi.live import owned_supervisor as sup
base, mode = Path(sys.argv[1]), sys.argv[2]
os.fstat(int(sys.argv[3]))
root = base / "cg"
group_path, leaf = root / "sup", root / "sup" / "forward"
leaf.mkdir(parents=True)
(root / "cgroup.controllers").write_text("cpu memory\n")
(group_path / "cgroup.procs").write_text(str(os.getpid()))
(group_path / "cgroup.events").write_text("populated 1\n")
(leaf / "cgroup.procs").write_text("")
(leaf / "cgroup.events").write_text("populated 0\n")
self_cg = base / "self_cgroup"
self_cg.write_text("0::/sup\n")
real_path = owned.Path
def fake_path(value):
    if str(value) == "/sys/fs/cgroup":
        return real_path(root)
    if str(value) == f"/proc/{os.getpid()}/cgroup":
        return real_path(self_cg)
    return real_path(value)
owned.Path = fake_path
identity = owned.process_identity(os.getpid())
scope = owned.CgroupScope(leaf)
group = owned.CgroupGroupScope(group_path, identity, {"forward": scope})
leader = subprocess.Popen(
    [sys.executable, "-c", "import sys; sys.stdin.readline()"],
    stdin=subprocess.PIPE, start_new_session=True,
)
leader_identity = owned.process_identity(leader.pid)
survivor = subprocess.Popen([
    sys.executable, "-c",
    "import sys,time; from pathlib import Path; p=Path(sys.argv[1]); "
    "exec('while not p.exists(): time.sleep(0.01)')",
    str(base / "release_leaf"),
], start_new_session=True)
survivor_identity = owned.process_identity(survivor.pid)
class Worker(owned.OwnedModelWorker):
    def __init__(self):
        self.worker = leader_identity
        self.scope = scope
        self._known_members = (leader_identity, survivor_identity)
        self.sent = False
    def stop(self, epoch):
        if not self.sent:
            leader.stdin.close()
            self.sent = True
        return True
    def exited(self):
        members = [p.pid for p in (leader, survivor) if p.poll() is None]
        (leaf / "cgroup.procs").write_text(" ".join(map(str, members)))
        (leaf / "cgroup.events").write_text(
            "populated " + ("1" if members else "0") + "\n"
        )
        if mode in ("unknown", "error") and not (base / "release_evidence").exists():
            if mode == "error":
                raise OSError("controlled exit evidence unreadable")
            return None
        return super().exited()
worker = Worker()
if mode == "containment":
    (group_path / "cgroup.events").write_text("populated unknown\n")
config = SimpleNamespace(
    group_id="exit-lock", state_dir=base, poll_s=0.03,
    stop_grace_s=0.1, kill_wait_s=0.1,
)
supervisor = sup.Supervisor(config, None, identity, group, None)
supervisor.workers = {"forward": worker}
if mode == "launch_receipt_none":
    # Real early OwnedLaunchError receipt: Popen never supplied a process identity.
    receipt = object.__new__(owned.OwnedModelWorker)
    receipt.worker = receipt._process = None
    receipt._closed = threading.Event()
    receipt._lock = threading.Lock()
    receipt._pending = ("ready", 0)
    receipt.driver_observer = SimpleNamespace(require_held=lambda: None)
    supervisor.workers["failed"] = receipt
for signum in (signal.SIGTERM, signal.SIGINT):
    signal.signal(signum, lambda *_: supervisor.stop_requested.set())
(base / "started.json").write_text(json.dumps({
    "leader": leader_identity.__dict__, "leaf": survivor_identity.__dict__
}))
code = supervisor.shutdown(sup.EX_OK, "controlled stop")
os.close(int(sys.argv[3]))  # Returning releases the production descriptor.
leader.wait(timeout=2)
survivor.wait(timeout=2)
sys.exit(code)
"""


def wait_json(proc, path, predicate=lambda value: True, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists():
            value = json.loads(path.read_text())
            if predicate(value):
                return value
        assert proc.poll() is None, "supervisor returned before exit was proven"
        time.sleep(0.01)
    raise AssertionError("timed out waiting for receipt: " + str(path))


def assert_lock_busy(path):
    with path.open("r+") as handle, pytest.raises(BlockingIOError):
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize(
    "mode", ["leaf", "unknown", "error", "containment", "launch_receipt_none"]
)
def test_unconfirmed_shutdown_retains_lock_until_complete_proof(tmp_path, mode):
    lock_path = tmp_path / "test.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with (tmp_path / "stderr").open("w") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-c", HARNESS, str(tmp_path), mode, str(fd)],
            pass_fds=(fd,),
            stderr=stderr,
        )
    os.close(fd)  # Only the CPU supervisor retains the inherited open description.
    try:
        started = wait_json(proc, tmp_path / "started.json")
        receipt_path = tmp_path / "stop-receipt.json"
        holding = wait_json(proc, receipt_path)
        time.sleep(0.05)  # Observe lock release if the old shutdown returned.
        assert_lock_busy(lock_path)
        assert holding["phase"] == "holding_exit"
        assert holding["code"] == sup.EX_STOP_UNCONFIRMED
        expected = {"forward": False}
        if mode == "launch_receipt_none":
            expected["failed"] = False
        assert holding["exited"] == expected
        assert_lock_busy(lock_path)
        proc.send_signal(signal.SIGTERM)
        proc.send_signal(signal.SIGINT)
        newer = wait_json(
            proc,
            receipt_path,
            lambda value: (
                value.get("observed_monotonic_ns", 0) > holding["observed_monotonic_ns"]
            ),
        )
        assert newer["phase"] == "holding_exit"
        assert_lock_busy(lock_path)
        from levi.live import owned_runtime as owned
        from levi.live.runtime_ownership import WorkerIdentity

        assert owned.incarnation_state(WorkerIdentity(**started["leader"])) == "gone"
        assert owned.incarnation_state(WorkerIdentity(**started["leaf"])) == "alive"
        (tmp_path / "release_leaf").touch()
        if mode == "launch_receipt_none":
            wait_json(proc, receipt_path, lambda value: value["exited"]["forward"])
            assert proc.poll() is None
            assert_lock_busy(lock_path)
            # The unknown receipt cannot become an exit proof merely because
            # the earlier leaf emptied; terminate only this controlled supervisor.
            proc.kill()
            proc.wait()
            return
        if mode in ("unknown", "error"):
            time.sleep(0.1)
            assert proc.poll() is None
            assert_lock_busy(lock_path)
            (tmp_path / "release_evidence").touch()
        if mode == "containment":
            wait_json(
                proc, receipt_path, lambda value: value["exited"] == {"forward": True}
            )
            assert proc.poll() is None
            assert_lock_busy(lock_path)
            (tmp_path / "cg" / "sup" / "cgroup.events").write_text("populated 1\n")
        assert proc.wait(timeout=5) == sup.EX_STOP_UNCONFIRMED
        final = json.loads(receipt_path.read_text())
        assert final["phase"] == "exit_confirmed"
        assert final["exited"] == {"forward": True}
        assert final["containment_confirmed"] is True
        with lock_path.open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        (tmp_path / "release_leaf").touch()
        (tmp_path / "release_evidence").touch()
        cg_events = tmp_path / "cg" / "sup" / "cgroup.events"
        if cg_events.exists():
            cg_events.write_text("populated 1\n")
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
