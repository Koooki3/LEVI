"""Real CPU supervisor, inherited flock and controlled fake containment."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

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
    signal.signal(signum, supervisor.request_stop_from_signal)
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


def test_signal_handler_returns_while_stop_event_lock_is_held(tmp_path):
    # A regression to Event.set in the real handler deadlocks this child;
    # subprocess.run kills and reaps only that child when the bound expires.
    script = r"""
import json, signal
from types import SimpleNamespace
from levi.live import owned_supervisor as sup
supervisor = sup.Supervisor(SimpleNamespace(poll_s=600), None, None, None, None)
for number in (signal.SIGTERM, signal.SIGINT):
    signal.signal(number, supervisor.request_stop_from_signal)
with supervisor.stop_requested._cond:
    signal.raise_signal(signal.SIGTERM)
    signal.raise_signal(signal.SIGINT)
    assert supervisor.stop_is_requested()
    assert not supervisor.stop_requested.is_set()
assert supervisor._wait_for_stop() is True
print(json.dumps({"returned": True, "stop_requested": supervisor.stop_is_requested()}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"returned": True, "stop_requested": True}


@pytest.mark.parametrize("source", ["signal", "event", "healthy"])
def test_stop_wait_bounds_signal_latency_without_accelerating_monitor(
    monkeypatch, source
):
    supervisor = sup.Supervisor(SimpleNamespace(poll_s=600), None, None, None, None)
    clock, waits, monitored = [0.0], [], []

    class Event:
        flag = False

        def is_set(self):
            return self.flag

        def set(self):
            self.flag = True

        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                if source == "signal":
                    supervisor.request_stop_from_signal(signal.SIGTERM, None)
                elif source == "event":
                    self.set()
            if self.flag:
                return True
            clock[0] += timeout
            return False

    def monitor():
        monitored.append(clock[0])
        return sup.EX_WORKER_DIED, "controlled monitor result"

    supervisor.stop_requested = Event()
    monkeypatch.setattr(sup.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(supervisor, "monitor_once", monitor)
    code, reason = supervisor.run_until_stop()
    assert waits and 0 < min(waits) <= max(waits) <= 0.05
    if source == "healthy":
        assert monitored == pytest.approx([600])
        assert sum(waits) == pytest.approx(600)
        assert (code, reason) == (sup.EX_WORKER_DIED, "controlled monitor result")
    else:
        assert monitored == []
        assert clock[0] <= 0.05
        assert (code, reason) == (sup.EX_OK, "stop requested")


@pytest.mark.parametrize("source", ["signal", "event"])
@pytest.mark.parametrize("phase", ["before", "during"])
def test_launch_honors_both_stop_sources_without_launching_next_role(
    tmp_path, source, phase
):
    roles = [SimpleNamespace(role=name, capability_id=name) for name in ("a", "b")]
    config = SimpleNamespace(roles=roles, launch_timeout_s=1)
    launched = []

    def request_stop():
        if source == "signal":
            supervisor.request_stop_from_signal(signal.SIGTERM, None)
        else:
            supervisor.stop_requested.set()

    def launcher(
        factory,
        scope,
        driver_observer,
        broker,
        lease,
        role,
        group_scope,
        state,
        *,
        timeout_s,
    ):
        launched.append(role)
        if phase == "during":
            request_stop()
        return object()

    plan = sup.SupervisorPlan(
        None, None, {role.role: role for role in roles}, tmp_path, launcher=launcher
    )
    group = SimpleNamespace(scopes={role.role: object() for role in roles})
    supervisor = sup.Supervisor(config, None, None, group, plan)
    if phase == "before":
        request_stop()
    supervisor.launch_all()
    assert launched == ([] if phase == "before" else ["a"])
    assert set(supervisor.workers) == set(launched)


@pytest.mark.parametrize("source", ["signal", "event"])
def test_main_routes_launch_stop_to_shutdown_before_ready(
    tmp_path, monkeypatch, source
):
    config = SimpleNamespace(roles=(), state_dir=tmp_path)
    plan = sup.SupervisorPlan(None, None, {}, tmp_path)
    registrations, shutdowns = {}, []
    monkeypatch.setattr(sup, "verify_launch_evidence", lambda *_: (None, None))
    monkeypatch.setattr(sup, "build_containment", lambda *_: (None, None))
    monkeypatch.setattr(
        sup.signal,
        "signal",
        lambda number, handler: registrations.setdefault(number, handler),
    )

    def launch(supervisor):
        if source == "signal":
            registrations[signal.SIGTERM](signal.SIGTERM, None)
        else:
            supervisor.stop_requested.set()

    def shutdown(supervisor, code, reason):
        shutdowns.append((code, reason))
        return code

    def not_ready(supervisor):
        pytest.fail("launch stop must not reach ready publication")

    monkeypatch.setattr(sup.Supervisor, "launch_all", launch)
    monkeypatch.setattr(sup.Supervisor, "shutdown", shutdown)
    monkeypatch.setattr(sup.Supervisor, "group_observer", not_ready)
    assert sup.run(config, {}, setup=lambda _: plan) == sup.EX_OK
    assert shutdowns == [(sup.EX_OK, "stop requested during launch")]
    assert not (tmp_path / "ready.json").exists()
