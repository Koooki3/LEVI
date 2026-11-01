"""A FAKE robot server whose arm LAGS its target, for the chunk-acceleration end-to-end tests. No hardware.

franka_control/tests/live/mock_fr3_live_server.py (loaded read-only, by path) is the HTTP surface the client talks to,
with its fault injection; in its mode "none" every pose arrives instantly (its docstring), which hides everything the
phase clock is about. This subclass keeps its routes, counters and faults and replaces what the arm does with them:

  * /pose only sets the target. The measured pose is integrated lazily, from the time of the previous call to now
    (no thread): the target, delayed by a dead time, goes through a first-order low-pass and then a 2.65 Hz mode with
    damping 0.17, so the total lag behind a ramp is d_alpha (0.27 s: tracking_real/README.md:39 at alpha 0.005).
  * rotation lags the same way, as a rotation vector relative to the start orientation.
  * the gripper starts to move 0.2 s after a command and takes 0.5 s to get there (fr3_env.py: fingers move ~0.2 s
    after the command), so /getstate's gripper_pos is what the post-close settle test of the phase clock watches.
  * the "freeze" fault is kept; ``stuck_offset`` makes it a "pushed away and held there" fault: the frozen pose is
    the plant pose plus the offset, so the last target is further from the measured pose than the leash allows.

Ports: only 5071-5079, and never 5000, 5001, 5100, 7470, 8000, 8010. The server binds 127.0.0.1.

    python3 lag_robot_server.py --port 5071 [--d-alpha 0.27] [--start-euler X Y Z RX RY RZ]

Standard library plus numpy.
"""

import argparse
import bisect
import importlib.util
import math
import os
import socket
import sys
import time
from pathlib import Path

import numpy as np

# franka_control/tests/live of the FR3 workspace: named by the environment, never by a default (a developer path in a
# shipped file fails tests/test_live_portable.py). Without it the server class is unavailable and the tests skip.
FAKES = Path(os.environ.get("CHUNK_ACCEL_FAKES_DIR") or "chunk-accel-fakes-dir-not-set")
FORBIDDEN_PORTS = (5000, 5001, 5100, 7470, 8000, 8010)
ROBOT_PORTS = tuple(range(5071, 5080))
# The start pose tests use with `--home-pose`: tool pointing down, rx a little under pi (the wrap), as the client's own.
DEFAULT_START_EULER = (0.45, 0.0, 0.18, 3.119139, 0.008288, -0.883962)


def assert_port(port):
    if int(port) in FORBIDDEN_PORTS or int(port) not in ROBOT_PORTS:
        raise SystemExit(
            f"refusing port {port}: the fake robot server uses {ROBOT_PORTS[0]}-{ROBOT_PORTS[-1]} only, never "
            f"{FORBIDDEN_PORTS}"
        )
    return int(port)


def bindable(port, host="127.0.0.1"):
    """True when nobody holds ``port``, found by binding it (a connect probe would poke somebody's service)."""
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def pick_port():
    for port in ROBOT_PORTS:
        assert_port(port)
        if bindable(port):
            return port
    raise RuntimeError(f"no free port among {ROBOT_PORTS}")


def _load_mock():
    path = FAKES / "mock_fr3_live_server.py"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} (the read-only base of the lag robot server) is missing"
        )
    spec = importlib.util.spec_from_file_location("mock_fr3_live_server_readonly", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---- quaternions (x, y, z, w) -------------------------------------------------------------------------------------


def qmul(a, b):
    x1, y1, z1, w1 = a
    x2, y2, z2, w2 = b
    return np.array(
        [
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        ]
    )


def qconj(q):
    return np.array([-q[0], -q[1], -q[2], q[3]])


def qexp(rv):
    angle = float(np.linalg.norm(rv))
    if angle < 1e-12:
        return np.array([0.5 * rv[0], 0.5 * rv[1], 0.5 * rv[2], 1.0]) / math.hypot(
            1.0, 0.5 * angle
        )
    axis = np.asarray(rv) / angle
    return np.array([*(axis * math.sin(angle / 2)), math.cos(angle / 2)])


def qlog(q):
    q = np.asarray(q, dtype=float)
    if q[3] < 0:
        q = -q
    s = float(np.linalg.norm(q[:3]))
    if s < 1e-12:
        return 2.0 * q[:3]
    return q[:3] / s * 2.0 * math.atan2(s, q[3])


def euler_xyz_to_quat(rx, ry, rz):
    """scipy's Rotation.from_euler("xyz", ...) (extrinsic x, y, z): q = qz * qy * qx."""

    def axis(i, angle):
        v = np.zeros(4)
        v[i], v[3] = math.sin(angle / 2), math.cos(angle / 2)
        return v

    return qmul(axis(2, rz), qmul(axis(1, ry), axis(0, rx)))


# ---- the arm ------------------------------------------------------------------------------------------------------


class LagPlant:
    """Six axes (xyz and a rotation vector) tracking a target: dead time, first-order low-pass, damped 2nd-order mode."""

    SUBSTEP_S = 0.002
    MAX_ADVANCE_S = 10.0

    def __init__(
        self, pose7, d_alpha=0.27, dead_s=0.02, mode_hz=2.65, zeta=0.17, now=None
    ):
        self.d_alpha, self.dead_s, self.zeta = (
            float(d_alpha),
            float(dead_s),
            float(zeta),
        )
        self.wn = 2 * math.pi * float(mode_hz)
        # the mode's own lag on a ramp is 2 zeta / wn; the low-pass takes up the rest of d_alpha
        self.tau = max(self.d_alpha - 2 * self.zeta / self.wn - self.dead_s, 0.02)
        self.q_ref = np.asarray(pose7[3:7], dtype=float)
        self.q_ref = self.q_ref / np.linalg.norm(self.q_ref)
        self.t = time.monotonic() if now is None else now
        vec = self._vec(pose7)
        self.u, self.x, self.v = vec.copy(), vec.copy(), np.zeros(6)
        self.times, self.targets = [self.t], [vec]

    def _vec(self, pose7):
        q = np.asarray(pose7[3:7], dtype=float)
        q = q / np.linalg.norm(q)
        return np.concatenate(
            [np.asarray(pose7[:3], dtype=float), qlog(qmul(qconj(self.q_ref), q))]
        )

    def _target_at(self, t):
        i = bisect.bisect_right(self.times, t) - 1
        return self.targets[max(i, 0)]

    def advance(self, now):
        dt = min(now - self.t, self.MAX_ADVANCE_S)
        if dt <= 0:
            return
        n = max(1, math.ceil(dt / self.SUBSTEP_S))
        h = dt / n
        for i in range(n):
            ts = self.t + (i + 1) * h
            self.u += h * (self._target_at(ts - self.dead_s) - self.u) / self.tau
            self.v += h * (
                self.wn**2 * (self.u - self.x) - 2 * self.zeta * self.wn * self.v
            )
            self.x += h * self.v
        self.t = now
        # keep the history that a delayed lookup can still reach
        keep = bisect.bisect_right(self.times, now - self.dead_s - 1.0) - 1
        if keep > 0:
            del self.times[:keep], self.targets[:keep]

    def set_target(self, pose7, now):
        self.advance(now)
        self.times.append(now)
        self.targets.append(self._vec(pose7))

    def pose7(self):
        q = qmul(self.q_ref, qexp(self.x[3:6]))
        return [float(v) for v in (*self.x[:3], *q)]

    def target_error(self, pose7):
        """(metres, radians) between a commanded pose and where the arm is now (not advanced)."""
        t = self._vec(pose7)
        return float(np.linalg.norm(t[:3] - self.x[:3])), float(
            np.max(np.abs(t[3:] - self.x[3:]))
        )


class GripperModel:
    def __init__(self, delay_s=0.2, travel_s=0.5, closed_pos=0.0):
        self.delay_s, self.travel_s, self.closed_pos = delay_s, travel_s, closed_pos
        self.pos0 = self.goal = 1.0
        self.t0 = -1e9

    def pos(self, now):
        f = min(max((now - self.t0 - self.delay_s) / self.travel_s, 0.0), 1.0)
        return self.pos0 + (self.goal - self.pos0) * f

    def command(self, value, now):
        goal = 1.0 if value >= 0.5 else self.closed_pos
        self.pos0, self.goal, self.t0 = self.pos(now), goal, now

    def reset(self):
        self.pos0 = self.goal = 1.0
        self.t0 = -1e9


def make_server_class():
    mock = _load_mock()

    class LagRobotServer(mock.MockFr3Live):
        """MockFr3Live with an arm that lags. ``pose`` and ``gripper_pos`` are properties: the base class's own
        handlers assign the commanded values to them and read the measured ones back."""

        def __init__(
            self,
            port,
            host="127.0.0.1",
            start_euler=DEFAULT_START_EULER,
            d_alpha=0.27,
            dead_s=0.02,
            mode_hz=2.65,
            zeta=0.17,
            grip_delay_s=0.2,
            grip_travel_s=0.5,
            grip_closed_pos=0.0,
        ):
            assert_port(port)
            x, y, z, rx, ry, rz = start_euler
            self.start_pose = [x, y, z, *euler_xyz_to_quat(rx, ry, rz)]
            self.plant_kwargs = {
                "d_alpha": d_alpha,
                "dead_s": dead_s,
                "mode_hz": mode_hz,
                "zeta": zeta,
            }
            self.gripper = GripperModel(grip_delay_s, grip_travel_s, grip_closed_pos)
            self.stuck_offset = None  # (dx, dy, dz): the "freeze" fault also moves the frozen pose by this
            self._plant = None
            self.target_err_m, self.target_err_rad = (
                [],
                [],
            )  # at every /pose call: new target vs the arm right then
            self.grip_commands = []  # (monotonic time, "open" | "close")
            super().__init__(port, host)

        # -- the arm
        @property
        def pose(self):
            self._plant.advance(time.monotonic())
            return self._plant.pose7()

        @pose.setter
        def pose(self, new):
            now = time.monotonic()
            if self._plant is None:
                self._plant = LagPlant(new, now=now, **self.plant_kwargs)
                return
            self._plant.advance(now)
            err_m, err_rad = self._plant.target_error(new)
            self.target_err_m.append(err_m)
            self.target_err_rad.append(err_rad)
            self._plant.set_target(new, now)

        @property
        def gripper_pos(self):
            return self.gripper.pos(time.monotonic())

        @gripper_pos.setter
        def gripper_pos(self, value):
            self.gripper.command(float(value), time.monotonic())
            self.grip_commands.append(
                (time.monotonic(), "open" if value >= 0.5 else "close")
            )

        def reset(self):
            super().reset()
            with self.lock:
                self._plant = LagPlant(self.start_pose, **self.plant_kwargs)
                self.gripper.reset()
                self.target_err_m, self.target_err_rad, self.grip_commands = [], [], []

        def _arm(self, mode):
            super()._arm(mode)
            if mode == "freeze" and self.stuck_offset is not None and self.frozen_pose:
                fp = list(self.frozen_pose)
                for i in range(3):
                    fp[i] += float(self.stuck_offset[i])
                self.frozen_pose = fp

        # -- what the tests read back (in-process)
        def lag_stats(self):
            with self.lock:
                return {
                    "poses": len(self.target_err_m),
                    "max_target_err_m": max(self.target_err_m, default=0.0),
                    "max_target_err_rad": max(self.target_err_rad, default=0.0),
                    "grip_commands": [kind for _, kind in self.grip_commands],
                    "d_alpha": self._plant.d_alpha,
                }

    return LagRobotServer


try:
    LagRobotServer = make_server_class()
except FileNotFoundError as _missing:  # the tests skip on this
    LagRobotServer, MISSING = None, str(_missing)
else:
    MISSING = None


def main():
    if LagRobotServer is None:
        raise SystemExit(MISSING)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--port", type=int, default=None, help="5071-5079; default: the first free one"
    )
    ap.add_argument("--d-alpha", type=float, default=0.27)
    ap.add_argument(
        "--start-euler", type=float, nargs=6, default=list(DEFAULT_START_EULER)
    )
    ap.add_argument(
        "--fault",
        choices=["none", "503", "motion503", "stale", "freeze", "down"],
        default="none",
    )
    ap.add_argument("--after-pose-calls", type=int, default=None)
    ap.add_argument("--stuck-offset", type=float, nargs=3, default=None)
    a = ap.parse_args()
    srv = LagRobotServer(
        a.port if a.port is not None else pick_port(),
        start_euler=a.start_euler,
        d_alpha=a.d_alpha,
    )
    if a.stuck_offset:
        srv.stuck_offset = a.stuck_offset
    if a.fault != "none":
        srv.set_fault(a.fault, a.after_pose_calls)
    srv.start()
    print(
        f"FAKE lag robot server on http://127.0.0.1:{srv.port}/ (no hardware is touched)",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
