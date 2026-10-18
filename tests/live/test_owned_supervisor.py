"""Host supervisor entry: real CPU processes, real flock, fake cgroup tree.

The pytest process plays the resource-driver run wrapper: it holds the flock,
writes SCHEDULER.json, starts the supervisor with the lock descriptor inherited
(as scheduler.py does) and registers it as the lease child. Nothing creates a
real cgroup, touches a GPU, a model library or a robot port.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from levi.live import gpumgr
from levi.live import owned_runtime as owned
from levi.live import owned_supervisor as sup

HARNESS = Path(__file__).with_name("owned_supervisor_harness.py")
ROLES = [
    {"role": "forward", "capability_id": "cap-forward", "leaf": "forward"},
    {"role": "judge", "capability_id": "cap-judge", "leaf": "judge"},
]


def identity_row(pid):
    value = gpumgr.identity_strict(pid)
    return {"pid": pid, "start_time": value["start_ticks"], "boot_id": value["boot"]}


class Rig:
    def __init__(self, tmp_path):
        self.root = tmp_path / "hub"
        self.root.mkdir()
        self.cg = tmp_path / "cg"
        self.sup_cg = self.cg / "sup"
        self.events = tmp_path / "events.log"
        self.events.write_text("")
        self.stderr = tmp_path / "stderr.log"
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.modules = tmp_path / "modules.json"
        self.tmp = tmp_path
        self.proc = None
        self.lock_fd = None

    def build_cgroup(self, roles=ROLES):
        self.cg.mkdir(exist_ok=True)
        (self.cg / "cgroup.controllers").write_text("cpu memory\n")
        self.sup_cg.mkdir(exist_ok=True)
        (self.sup_cg / "cgroup.events").write_text("populated 1\n")
        for role in roles:
            leaf = self.sup_cg / role["leaf"]
            leaf.mkdir(exist_ok=True)
            (leaf / "cgroup.procs").write_text("")
            (leaf / "cgroup.events").write_text("populated 0\n")

    def start(
        self,
        *,
        phase="registered",
        row_id="lease-1",
        hold_flock=True,
        wrong_fd=False,
        owner_pid=None,
        extra_root_pid=None,
        busy_leaf=None,
        self_cgroup="0::/sup",
        modes=None,
        config_extra=None,
        roles=ROLES,
        state="active",
    ):
        self.build_cgroup(roles)
        lock = self.root / ".gpu.lock"
        self.lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        if hold_flock:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pass_fd = self.lock_fd
        if wrong_fd:
            other = self.root / "other.lock"
            pass_fd = os.open(other, os.O_CREAT | os.O_RDWR, 0o600)
        config = {
            "scheduler_root": str(self.root),
            "clock_domain": "test",
            "cgroup_root": str(self.sup_cg),
            "group_id": "group-1",
            "state_dir": str(self.state),
            "roles": roles,
            "poll_s": 0.05,
            "stop_grace_s": 1.5,
            "kill_wait_s": 1.0,
            **(config_extra or {}),
        }
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps(config))
        self_cg = self.tmp / "self_cgroup"
        scenario = {
            "fake_root": str(self.cg),
            "self_cgroup": str(self_cg),
            "events": str(self.events),
            "modes": modes or {},
            "modules": str(self.modules),
        }
        scenario_path = self.tmp / "scenario.json"
        scenario_path.write_text(json.dumps(scenario))
        env = dict(os.environ)
        env.update(
            LEVI_LEASE_ID="lease-1",
            LEVI_LEASE_GENERATION="1",
            LEVI_SESSION_ID="session-1",
            LEVI_GPU_LOCK_HELD="1",
            LEVI_GPU_LOCK_FD=str(pass_fd),
        )
        self.proc = subprocess.Popen(
            [sys.executable, str(HARNESS), str(config_path), str(scenario_path)],
            stdin=subprocess.PIPE,
            stderr=self.stderr.open("w"),
            env=env,
            pass_fds=(pass_fd,),
        )
        pid = self.proc.pid
        self_cg.write_text(self_cgroup + "\n")
        (self.sup_cg / "cgroup.procs").write_text(
            "\n".join(
                str(p) for p in [pid, *([extra_root_pid] if extra_root_pid else [])]
            )
        )
        if busy_leaf:
            leaf = self.sup_cg / busy_leaf
            (leaf / "cgroup.procs").write_text("424242")
            (leaf / "cgroup.events").write_text("populated 1\n")
        self.row = {
            "id": row_id,
            "generation": 1,
            "session": "session-1",
            "state": state,
            "resources": ["gpu"],
            "identity": identity_row(owner_pid or os.getpid()),
            "child": identity_row(pid),
            "launch_phase": phase,
        }
        self.write_row()
        self.proc.stdin.write(b"1")
        self.proc.stdin.close()
        return self.proc

    def write_row(self, **changes):
        self.row.update(changes)
        (self.root / "SCHEDULER.json").write_text(
            json.dumps({"schema": 1, "leases": [self.row]})
        )

    def wait_file(self, path, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if Path(path).exists():
                return json.loads(Path(path).read_text())
            assert self.proc.poll() is None, self.log()
            time.sleep(0.05)
        raise AssertionError("timeout waiting for " + str(path) + "\n" + self.log())

    def wait_exit(self, timeout=20):
        return self.proc.wait(timeout=timeout)

    def log(self):
        return self.stderr.read_text()

    def event_lines(self):
        return self.events.read_text().split()

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.lock_fd is not None:
            os.close(self.lock_fd)


@pytest.fixture
def rig(tmp_path):
    value = Rig(tmp_path)
    yield value
    value.close()


def refused(rig, code, text):
    assert rig.wait_exit() == code, rig.log()
    log = rig.log()
    assert "REFUSED" in log and text in log, log
    # Refusal happens before any model import or worker start.
    assert not (rig.state / "ready.json").exists()
    assert "launched:" not in rig.events.read_text()
    modules = json.loads(rig.modules.read_text())
    assert modules == {"torch": False, "vllm": False, "jax": False}


def test_ready_then_sigterm_stops_every_worker_before_exit(rig):
    rig.start()
    ready = rig.wait_file(rig.state / "ready.json")
    assert sorted(ready["workers"]) == ["forward", "judge"]
    rig.proc.send_signal(signal.SIGTERM)
    assert rig.wait_exit() == sup.EX_OK, rig.log()
    lines = rig.event_lines()
    assert lines[-1] == "supervisor-exit"
    for role in ("forward", "judge"):
        assert lines.index("stop:" + role) < lines.index("exited:" + role)
        assert lines.index("exited:" + role) < lines.index("supervisor-exit")
    receipt = json.loads((rig.state / "stop-receipt.json").read_text())
    assert receipt["exited"] == {"forward": True, "judge": True}
    assert receipt["code"] == sup.EX_OK
    # the leaves are empty again and the lock fd was held to the end
    for role in ROLES:
        assert (rig.sup_cg / role["leaf"] / "cgroup.procs").read_text() == ""


def test_stubborn_worker_is_killed_by_group_then_confirmed(rig):
    rig.start(modes={"judge": "stubborn"})
    rig.wait_file(rig.state / "ready.json")
    rig.proc.send_signal(signal.SIGTERM)
    assert rig.wait_exit() == sup.EX_OK, rig.log()
    assert "escalating SIGKILL" in rig.log()
    assert "exited:forward" in rig.event_lines()


def test_unconfirmable_exit_is_not_reported_as_clean(rig):
    rig.start(modes={"forward": "noconfirm"})
    rig.wait_file(rig.state / "ready.json")
    rig.proc.send_signal(signal.SIGTERM)
    receipt = rig.wait_file(rig.state / "stop-receipt.json")
    assert rig.proc.poll() is None, rig.log()
    assert receipt["phase"] == "holding_exit"
    assert receipt["code"] == sup.EX_STOP_UNCONFIRMED
    assert receipt["exited"]["forward"] is False


def test_worker_exiting_early_stops_the_rest_and_fails(rig):
    rig.start(modes={"judge": "early"})
    assert rig.wait_exit() == sup.EX_WORKER_DIED, rig.log()
    lines = rig.event_lines()
    assert "died:judge" in lines and "stop:forward" in lines
    assert lines.index("exited:forward") < lines.index("supervisor-exit")
    assert "worker judge" in rig.log()


def test_lease_lost_while_running_stops_workers(rig):
    rig.start()
    rig.wait_file(rig.state / "ready.json")
    rig.write_row(state="released")
    assert rig.wait_exit() == sup.EX_LEASE_LOST, rig.log()
    assert "exited:forward" in rig.event_lines()


def test_cancel_request_while_running_stops_workers(rig):
    rig.start()
    rig.wait_file(rig.state / "ready.json")
    rig.write_row(cancel_requested=True)
    assert rig.wait_exit() == sup.EX_LEASE_LOST, rig.log()


def test_launch_failure_stops_already_launched_workers(rig):
    rig.start(modes={"judge": "launch_fail"})
    assert rig.wait_exit() == sup.EX_LAUNCH, rig.log()
    lines = rig.event_lines()
    assert "launched:forward" in lines and "exited:forward" in lines


def test_missing_lease_row_is_refused(rig):
    rig.start(row_id="someone-else")
    refused(rig, sup.EX_EVIDENCE, "lease")


def test_launch_phase_not_registered_is_refused(rig):
    rig.start(phase="intent")
    refused(rig, sup.EX_EVIDENCE, "registered")


def test_flock_not_held_is_refused(rig):
    rig.start(hold_flock=False)
    refused(rig, sup.EX_EVIDENCE, "flock")


def test_lock_fd_that_is_not_the_gpu_lock_is_refused(rig):
    rig.start(wrong_fd=True)
    refused(rig, sup.EX_EVIDENCE, "GPU lock")


def test_flock_owner_that_is_not_the_driver_is_refused(rig):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        rig.start(owner_pid=other.pid)
        refused(rig, sup.EX_EVIDENCE, "descend")
    finally:
        other.kill()
        other.wait()


def test_inactive_lease_is_refused(rig):
    rig.start(state="queued")
    refused(rig, sup.EX_EVIDENCE, "active GPU driver")


def test_missing_environment_is_refused(rig):
    rig.build_cgroup()
    config = rig.tmp / "config.json"
    config.write_text(
        json.dumps(
            {
                "scheduler_root": str(rig.root),
                "clock_domain": "test",
                "cgroup_root": str(rig.sup_cg),
                "group_id": "group-1",
                "state_dir": str(rig.state),
                "roles": ROLES,
            }
        )
    )
    assert sup.main(["--config", str(config)], env={}) == sup.EX_EVIDENCE


def test_extra_process_in_group_root_is_refused(rig):
    rig.start(extra_root_pid=999999)
    refused(rig, sup.EX_CGROUP, "supervisor")


def test_non_empty_role_leaf_is_refused(rig):
    rig.start(busy_leaf="judge")
    refused(rig, sup.EX_CGROUP, "membership")


def test_supervisor_not_launched_into_the_delegated_root_is_refused(rig):
    rig.start(self_cgroup="0::/elsewhere")
    refused(rig, sup.EX_CGROUP, "delegated")


def test_bad_config_exits_with_usage_code(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"scheduler_root": "x", "unknown": 1}))
    assert sup.main(["--config", str(path)], env={}) == sup.EX_USAGE
    path.write_text("not json")
    assert sup.main(["--config", str(path)], env={}) == sup.EX_USAGE
    assert sup.main([], env={}) == sup.EX_USAGE


def test_duplicate_role_config_exits_with_usage_code(tmp_path):
    path = tmp_path / "config.json"
    role = {"role": "judge", "capability_id": "a", "leaf": "judge"}
    path.write_text(
        json.dumps(
            {
                "scheduler_root": "x",
                "clock_domain": "t",
                "cgroup_root": "y",
                "group_id": "g",
                "state_dir": "s",
                "roles": [role, {**role, "capability_id": "b", "leaf": "other"}],
            }
        )
    )
    assert sup.main(["--config", str(path)], env={}) == sup.EX_USAGE


def test_module_is_runnable_and_exposes_exit_proof_path():
    assert owned.OwnedGroupObserver.exit_proof  # the path the supervisor delegates to
    assert callable(sup.Supervisor.collect_exit_proof)
    result = subprocess.run(
        [sys.executable, "-m", "levi.live.owned_supervisor"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == sup.EX_USAGE
