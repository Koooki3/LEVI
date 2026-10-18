"""Subprocess entry for test_owned_supervisor.py (not a test module).

Runs the real `levi.live.owned_supervisor.main` in a real CPU process. Only
three things are faked: the cgroup filesystem (the module's `Path` is pointed
at a temporary tree), the model workers (real CPU `python` children that the
fake launcher "enrols" by writing the fake leaf files) and the factories.
The driver evidence (SCHEDULER.json, flock, /proc ancestry) is real.
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from levi.live import owned_runtime as owned
from levi.live import owned_supervisor as sup

CHILD = r"""
import signal, sys, time
events, role, mode = sys.argv[1:4]
def log(text):
    with open(events, "a") as f:
        f.write(text + "\n")
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.05)
if mode == "early":
    time.sleep(0.4)
    log("died:" + role)
    sys.exit(3)
sys.stdin.readline()
time.sleep(0.2)
log("exited:" + role)
"""


def log(path, text):
    with open(path, "a") as handle:
        handle.write(text + "\n")


class FakeWorker(owned.OwnedModelWorker):
    def __init__(
        self, role, factory, proc, leaf, events, driver_observer, group_scope, mode
    ):
        self.role, self.factory, self.proc, self.leaf = role, factory, proc, leaf
        self.worker = owned.process_identity(proc.pid)
        self.events, self.mode = events, mode
        self.driver_observer, self.group_scope = driver_observer, group_scope
        self._pending, self._idle = None, object()

    def stop(self, epoch):
        log(self.events, "stop:" + self.role)
        try:
            self.proc.stdin.write(b"stop\n")
            self.proc.stdin.flush()
        except OSError:
            pass
        return True

    def idle(self):
        return True

    def exited(self):
        if self.mode == "noconfirm" or self.proc.poll() is None:
            return False
        (self.leaf / "cgroup.procs").write_text("")
        (self.leaf / "cgroup.events").write_text("populated 0\n")
        return True


def main():
    config, scenario = sys.argv[1], json.loads(Path(sys.argv[2]).read_text())
    sys.stdin.buffer.read(1)  # gate: the driver registers this child first
    fake_root, self_cgroup = Path(scenario["fake_root"]), Path(scenario["self_cgroup"])
    real_path = owned.Path

    def fake_path(value):
        text = str(value)
        if text == "/sys/fs/cgroup":
            return real_path(fake_root)
        if text == f"/proc/{os.getpid()}/cgroup":
            return real_path(self_cgroup)
        return real_path(value)

    owned.Path = fake_path
    events = scenario["events"]
    modes = scenario["modes"]

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
        timeout_s=1.0,
    ):
        driver_observer.require_held()
        group_scope.require_supervisor()
        scope.require_members(())
        if modes.get(role) == "launch_fail":
            raise RuntimeError("fake launch failure")
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD, events, role, modes.get(role, "normal")],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        (scope.path / "cgroup.procs").write_text(str(proc.pid))
        (scope.path / "cgroup.events").write_text("populated 1\n")
        worker = FakeWorker(
            role,
            factory,
            proc,
            scope.path,
            events,
            driver_observer,
            group_scope,
            modes.get(role, "normal"),
        )
        group_scope.reserve_launch(role, worker)
        log(events, "launched:" + role)
        return worker

    def setup(context):
        return sup.SupervisorPlan(
            broker=None,
            lease=None,
            factories={
                r.role: SimpleNamespace(capability_id=r.capability_id)
                for r in context.roles
            },
            state=context.state_dir,
            launcher=launcher,
            on_ready=lambda supervisor: log(events, "on_ready"),
        )

    signal.signal(signal.SIGUSR1, lambda *_: None)
    code = sup.main(["--config", config], setup=setup)
    Path(scenario["modules"]).write_text(
        json.dumps({m: m in sys.modules for m in ("torch", "vllm", "jax")})
    )
    log(events, "supervisor-exit")
    sys.exit(code)


if __name__ == "__main__":
    main()
