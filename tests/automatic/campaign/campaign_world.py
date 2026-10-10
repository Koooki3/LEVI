"""A fake world for the conductor: policy host, run launcher, a person's
answers and the C2 session. Its state lives in files (appended with
fsync), so a crash test's child process and the test share it."""

import json
import os
import secrets
from pathlib import Path

from levi.automatic.campaign.conductor import (
    ChildStatus,
    Confirmation,
    HostResult,
    LaunchResult,
    PauseRequest,
    Readiness,
)


def _append(path: Path, value) -> None:
    with open(path, "a") as handle:
        handle.write(json.dumps(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _lines(path: Path) -> list:
    try:
        return [json.loads(x) for x in Path(path).read_text().splitlines() if x]
    except FileNotFoundError:
        return []


class World:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.host_log = self.folder / "host.jsonl"
        self.launch_log = self.folder / "launches.jsonl"
        self.status_file = self.folder / "status.json"

    # what happened
    def host_ops(self) -> list:
        return _lines(self.host_log)

    def launches(self) -> list:
        return [x["run_id"] for x in _lines(self.launch_log)]

    def active(self):
        active = None
        for op in self.host_ops():
            if op["op"] == "stop":
                active = None
            elif op["op"] == "start":
                active = op["arm"]
        return active

    # what the test sets
    def set_status(self, run_id: str, state: str, **counts) -> None:
        """What the child run reports once launched (``present=True``: even
        before, as a run someone else started)."""
        found = self._statuses()
        found[run_id] = {"state": state, **counts}
        self.status_file.write_text(json.dumps(found))

    def _statuses(self) -> dict:
        try:
            return json.loads(self.status_file.read_text())
        except FileNotFoundError:
            return {}


class FakeHost:
    def __init__(self, world: World, *, start_result=None, ready="ready"):
        self.world = world
        self.start_result = start_result
        self.ready = ready
        self.checks = 0

    def stop(self, arms):
        _append(self.world.host_log, {"op": "stop", "arms": [a.arm for a in arms]})
        return HostResult("yes", "stopped")

    def start(self, arm):
        if self.start_result is not None:
            return self.start_result
        _append(self.world.host_log, {"op": "start", "arm": arm.arm})
        return HostResult("yes", "started")

    def passive_ready(self, arm):
        self.checks += 1
        if self.ready != "ready":
            return Readiness(self.ready, "fake")
        if self.world.active() == arm.arm:
            return Readiness("ready")
        return Readiness("not_ready", f"active {self.world.active()}")


class FakeLauncher:
    def __init__(self, world: World, *, result="yes", trials=2, busy=None):
        self.world = world
        self.result = result
        self.trials = trials
        self.busy = busy
        self.expected = {}

    def launch(self, job_path, run_id, expected_plan_sha256):
        assert Path(job_path).is_file(), job_path
        self.expected[run_id] = expected_plan_sha256
        if self.result == "yes":
            _append(self.world.launch_log, {"run_id": run_id})
        return LaunchResult(
            self.result, "started" if self.result == "yes" else "refused"
        )

    def status(self, run_id):
        override = dict(self.world._statuses().get(run_id) or {})
        present = override.pop("present", False)
        launched = run_id in self.world.launches()
        if not launched and not present:
            return ChildStatus("absent")
        if override:
            return ChildStatus(**override)
        return ChildStatus("completed", episodes_complete=self.trials)

    def robot_busy(self):
        return self.busy


class Person:
    """Answers questions. ``auto``: confirm every scene and resume (or
    ``decision``) every wait, overriding stop rules when ``override``."""

    def __init__(self, auto=True, decision="resume", override=True):
        self.auto = auto
        self.decision = decision
        self.override = override
        self.asked = []
        self.withdrawn = []
        self.queue = []  # explicit answers (Confirmation or callables)
        self.pause = None

    def ask(self, request):
        self.asked.append(request)

    def withdraw(self, request):
        self.withdrawn.append(request)

    def answer(self, request, decision, **checks):
        return Confirmation(
            request_id=request.request_id,
            nonce=request.nonce,
            command_id=f"cmd-{secrets.token_hex(6)}",
            principal_id="op-1",
            decision=decision,
            checks=checks,
        )

    def take(self, request):
        if self.queue:
            found = self.queue.pop(0)
            return found(request) if callable(found) else found
        if not self.auto:
            return None
        if request.kind == "env_confirm":
            return self.answer(request, "confirm", arm_still=True, layout_ready=True)
        return self.answer(request, self.decision, override_stop_rule=self.override)

    def pause_requested(self):
        return self.pause

    def request_pause(self):
        self.pause = PauseRequest(f"pause-{secrets.token_hex(4)}", "op-1")


class Session:
    def __init__(self):
        self.calls = []

    def waiting_reset(self, campaign_id, segment):
        self.calls.append(("waiting_reset", segment))

    def release(self, campaign_id):
        self.calls.append(("release",))


def deps(world, **over):
    found = {
        "host": FakeHost(world),
        "launcher": FakeLauncher(world),
        "confirmations": Person(),
        "session": Session(),
    }
    found.update(over)
    return found
