"""Shared fakes for the 2026-10-13 real-robot regression tests (no I/O)."""

from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
from aeri_harness import fake

from levi.automatic import fr3_bridge as B
from levi.automatic import state_machine as sm
from levi.automatic.journal import Journal

HOME = [0.4, 0.0, 0.2, 0.0, 0.0, 0.0]
TOLERANCE_MM = 5


class FakeEnv:
    """Pose and gripper readings are functions of the seconds since the last
    pose command, so a lagging arm is a function, not a sleep."""

    def __init__(self, clock):
        self.clock = clock
        self.cmd_ns = None
        self.reads = 0
        self.routes = []
        self.replies = {}
        self.gripper_command = None  # (route,) sent with the next step
        self.pose_fn = lambda since: np.array([*HOME, 1.0])
        self.grip_fn = lambda since: 1.0
        self.on_read = None
        self.guarded = True

    def _since(self):
        return None if self.cmd_ns is None else (self.clock.now() - self.cmd_ns) / 1e9

    def _post(self, route, payload=None):
        self.routes.append(route)
        if route in self.replies:
            return self.replies[route]
        if route == "getstate":
            return {"ok": True, "gripper_pos": self.grip_fn(self._since())}
        return {
            "clearerr": "Clear",
            "pose": "Moved",
            "open_gripper": "Opened",
            "close_gripper": "Closed",
        }.get(route, {"ok": True})

    def get_observation(self):
        self.reads += 1
        self._post("getstate")
        if self.on_read is not None:
            self.on_read(self.reads)
        since = self._since()
        return {
            "image": {},
            "robot_state": {
                "cartesian_position": self.pose_fn(since),
                "gripper_position": self.grip_fn(since),
            },
        }

    def step(self, action):
        clearerr = self._post("clearerr")
        pose = self._post("pose")
        gripper = None
        if self.gripper_command is not None:
            route = self.gripper_command
            gripper = {"command": route, "response": self._post(route)}
        self.cmd_ns = self.clock.now()
        return {
            "clearerr_response": clearerr,
            "pose_response": pose,
            "gripper_response": gripper,
        }


class Guard:
    def __init__(self):
        self.tripped = None
        self.status = "ok"

    def check(self, force=False):
        return NS(
            status=self.status, reasons=["fake red"] if self.status != "ok" else []
        )


def build(**kw):
    clock = fake.FakeClock()
    env = FakeEnv(clock)
    guard = Guard()
    fence = sm.MotionFence()
    client = NS(
        _clamp_action=lambda a, cur, args: (np.asarray(a, dtype=float), []),
        _go_home=lambda e, args: e.step(np.array([*args.home_pose, 1.0])),
        _resolve_home=lambda args: np.array([*args.home_pose, 1.0]),
    )
    args = NS(home_pose=HOME, binarize_gripper=False)
    robot = B.Robot(
        env,
        guard,
        client,
        args,
        clock,
        fence,
        tolerance_mm=TOLERANCE_MM,
        emit=lambda text: None,
        **kw,
    )
    return NS(clock=clock, env=env, guard=guard, fence=fence, robot=robot)


def token(r, kind):
    return r.fence.issue(
        sm.MotionToken(
            run_id="r",
            transaction_id="tx",
            control_epoch=1,
            kind=kind,
            episode_id="r.forward.0001",
            policy_epoch=1,
            expires_mono_ns=r.clock.now() + 600_000_000_000,
        )
    )


def lagging(offset_m, tau_s):
    """x error decays linearly to zero tau_s after the last command."""

    def pose(since):
        x = HOME[0] + offset_m * (1.0 if since is None else max(0.0, 1 - since / tau_s))
        return np.array([x, *HOME[1:], 1.0])

    return pose


def run_dir(r):
    return Path(r.job["rollout_root"]) / ".aeri" / "runs" / "real-test"


def offset_start(r, monkeypatch, dx):
    env_class = r.modules[2].RobotEnv
    init = env_class.__init__

    def init2(self, **kw):
        init(self, **kw)
        self.pose = np.array([HOME[0] + dx, *HOME[1:], 1.0])

    monkeypatch.setattr(env_class, "__init__", init2)


def notes(r):
    return {
        e.note.code: e.note.detail
        for e in Journal.read(run_dir(r)).events
        if e.record == "note"
    }


def step_with(r, route, reply, *, grip=lambda since: 1.0):
    r.env.gripper_command = route
    r.env.replies[route] = reply
    r.env.grip_fn = grip
    return r.robot.step(np.array([*HOME, 1.0]), token=token(r, "policy_steps"))
