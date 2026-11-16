"""Fakes for the GRASP_ASSIST takeover (no I/O, no robot, no GPU): an env that models
what ``openpi/examples/fr3_local/fr3_env.py`` does at the HTTP level (the server pose
is ``[x y z qx qy qz qw]``; the observation's ``cartesian_position`` is x y z, the
Euler angles of that quaternion and the LAST COMMANDED gripper value, never the
measured opening), the bridge ``Robot`` over it, and ``Fr3AssistPort`` over that.

The arm follows a commanded pose linearly over ``tau`` s on the fake clock; the gripper
moves towards the held object's opening (``0.2986``, an eggplant held 2026-11-14), the
empty gripper's (``0.01``) or fully open (1.0) over ``grip_s`` s."""

import math
import threading
from types import SimpleNamespace as NS

import numpy as np
from aeri_harness import fake

from levi.automatic import fr3_assist
from levi.automatic import fr3_bridge as B
from levi.automatic import grasp_assist as ga
from levi.automatic import state_machine as sm

BAND = "purple eggplant|0.15|0.4"
HELD = 0.2986
EMPTY = 0.01
START = (0.48, -0.011, 0.236)
EULER = (3.119139, 0.008288, -0.883962)
MIN_Z = 0.05325
MAX_XY = 0.85


# --- an independent rotation oracle (numpy matrices, no code of the module under test) --


def rot(rx, ry, rz):
    cx, sx, cy, sy, cz, sz = (
        math.cos(rx),
        math.sin(rx),
        math.cos(ry),
        math.sin(ry),
        math.cos(rz),
        math.sin(rz),
    )
    rx_ = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry_ = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz_ = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rz_ @ ry_ @ rx_


def matrix_to_quat_xyzw(m):
    """Shepperd's method; the quaternion is (x, y, z, w), w >= 0."""
    t = np.trace(m)
    if t > 0:
        s = math.sqrt(t + 1) * 2
        w, x, y, z = (
            s / 4,
            (m[2, 1] - m[1, 2]) / s,
            (m[0, 2] - m[2, 0]) / s,
            (m[1, 0] - m[0, 1]) / s,
        )
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w, x, y, z = (
            (m[2, 1] - m[1, 2]) / s,
            s / 4,
            (m[0, 1] + m[1, 0]) / s,
            (m[0, 2] + m[2, 0]) / s,
        )
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w, x, y, z = (
            (m[0, 2] - m[2, 0]) / s,
            (m[0, 1] + m[1, 0]) / s,
            s / 4,
            (m[1, 2] + m[2, 1]) / s,
        )
    else:
        s = math.sqrt(1 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w, x, y, z = (
            (m[1, 0] - m[0, 1]) / s,
            (m[0, 2] + m[2, 0]) / s,
            (m[1, 2] + m[2, 1]) / s,
            s / 4,
        )
    if w < 0:
        w, x, y, z = -w, -x, -y, -z
    return [x, y, z, w]


def quat_xyzw_to_matrix(q):
    x, y, z, w = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def matrix_to_euler(m):
    return [
        math.atan2(m[2, 1], m[2, 2]),
        math.asin(-m[2, 0]),
        math.atan2(m[1, 0], m[0, 0]),
    ]


class AssistEnv:
    """The env under the bridge ``Robot``: ``_post`` is what the bridge fences."""

    def __init__(
        self,
        clock,
        *,
        object_opening=HELD,
        tau=0.4,
        grip_s=0.6,
        start=START,
        euler=EULER,
    ):
        self.clock = clock
        self.object_opening, self.tau, self.grip_s = object_opening, tau, grip_s
        self.from_xyz = list(start)
        self.target_xyz = list(start)
        self.cmd_ns = None
        self.quat = [float(v) for v in matrix_to_quat_xyzw(rot(*euler))]
        self.last_gripper_cmd = 1.0
        self.grip_from, self.grip_to, self.grip_ns = 1.0, 1.0, None
        self.posts: list = []  # (route, payload) in order, as the bridge let them pass
        self.pose_targets: list = []  # the xyz of every pose POST
        self.pose_quats: list = []  # the quaternion of every pose POST
        self.blocked_z: float | None = None  # the arm cannot go below this
        self.stuck = False  # the arm ignores pose commands (never reaches)
        self.short_x = 0.0  # the arm settles this far short of its target in x
        self.fail_route: str | None = None  # a route whose reply is not an ack
        self.stale = False
        self.drop_pose_in_state = False
        self._lock = threading.Lock()

    # --- what the arm is doing now ---------------------------------------------------

    def _xyz(self):
        if self.cmd_ns is None or self.stuck:
            xyz = list(self.from_xyz)
        else:
            frac = min(1.0, (self.clock.now() - self.cmd_ns) / 1e9 / self.tau)
            xyz = [a + (b - a) * frac for a, b in zip(self.from_xyz, self.target_xyz)]
        if self.blocked_z is not None:
            xyz[2] = max(xyz[2], self.blocked_z)
        if self.cmd_ns is not None:
            xyz[0] -= self.short_x  # a steady-state error
        return xyz

    def _opening(self):
        if self.grip_ns is None:
            return self.grip_from
        frac = min(1.0, (self.clock.now() - self.grip_ns) / 1e9 / self.grip_s)
        return self.grip_from + (self.grip_to - self.grip_from) * frac

    def _grip(self, to):
        self.grip_from, self.grip_to, self.grip_ns = (
            self._opening(),
            to,
            self.clock.now(),
        )

    # --- the HTTP level ------------------------------------------------------------

    def _post(self, route, payload=None):
        self.posts.append((route, payload))
        if self.fail_route == route:
            return "refused"
        if route == "getstate":
            xyz = self._xyz()
            quat = [float(v) for v in self.quat]
            state = {
                "ok": True,
                "q": [0.0] * 7,
                "gripper_pos": self._opening(),
                "state_fresh": not self.stale,
            }
            if not self.drop_pose_in_state:
                state["pose"] = [*xyz, *quat]
            return state
        if route == "pose":
            arr = payload["arr"]
            self.from_xyz = self._xyz()
            self.target_xyz = [float(v) for v in arr[:3]]
            self.cmd_ns = self.clock.now()
            self.pose_targets.append(tuple(float(v) for v in arr[:3]))
            self.pose_quats.append(tuple(float(v) for v in arr[3:7]))
            return "Moved"
        if route == "clearerr":
            return "Clear"
        if route == "open_gripper":
            self._grip(1.0)
            return "Opened"
        if route == "close_gripper":
            self._grip(self.object_opening)
            return "Closed"
        if route == "close_gripper_slow":
            self._grip(self.object_opening)
            return "Closed"
        if route == "move_gripper":
            return "Moved Gripper"
        return {"ok": True}

    def _get_state(self):
        return self._post("getstate")

    def get_observation(self):
        state = self._get_state()
        quat = state["pose"][3:7] if "pose" in state else self.quat
        euler = matrix_to_euler(quat_xyzw_to_matrix(quat))
        xyz = state["pose"][:3] if "pose" in state else self._xyz()
        pixels = np.zeros((2, 2, 3), dtype=np.uint8)
        return {
            "image": {"view1": pixels, "hand": pixels},
            "robot_state": {
                # fr3_env._pose_state: the COMMAND, not the measured opening.
                "cartesian_position": np.array(
                    [*xyz, *euler, self.last_gripper_cmd], dtype=np.float32
                ),
                "joint_positions": np.zeros(7, dtype=np.float32),
                "gripper_position": float(state["gripper_pos"]),
            },
        }

    def step(self, action):
        action = np.asarray(action, dtype=np.float32)
        if action.shape != (7,):
            raise ValueError("Expected action shape (7,)")
        quat = matrix_to_quat_xyzw(rot(*[float(v) for v in action[3:6]]))
        gripper_pos = float(self._get_state()["gripper_pos"])
        clearerr = self._post("clearerr")
        pose = self._post("pose", {"arr": [*[float(v) for v in action[:3]], *quat]})
        gripper = self._update_gripper(float(np.clip(action[6], 0.0, 1.0)), gripper_pos)
        return {
            "clearerr_response": clearerr,
            "pose_response": pose,
            "gripper_response": gripper,
        }

    def _update_gripper(self, command, gripper_pos):
        if command >= 0.9 and gripper_pos >= 0.9:
            return None
        if command <= 0.05 and gripper_pos <= 0.05:
            return None
        if abs(command - gripper_pos) < 0.05:
            return None
        if command <= 0.05:
            response, name = self._post("close_gripper"), "close_gripper"
            self.last_gripper_cmd = 0.0
        else:
            response, name = self._post("open_gripper"), "open_gripper"
            self.last_gripper_cmd = 1.0
        return {"command": name, "response": response}


class Guard:
    def __init__(self):
        self.tripped = None
        self.status = "ok"

    def check(self, force=False):
        return NS(status=self.status, reasons=[])


def client_module():
    def clamp(action, current, args):
        action = np.asarray(action, dtype=np.float32).copy()
        notes = []
        if args.min_z > 0 and action[2] < args.min_z:
            notes.append(f"z {action[2]:.3f}->{args.min_z:.3f}")
            action[2] = args.min_z
        for i, axis in enumerate("xy"):
            if abs(action[i]) > args.max_xy:
                notes.append(f"{axis} out")
                action[i] = np.sign(action[i]) * args.max_xy
        if args.binarize_gripper:
            action[6] = 1.0 if float(action[6]) >= 0.5 else 0.0
        step = float(np.linalg.norm(np.asarray(action[:3], np.float64) - current[:3]))
        if step > args.max_step_m:
            notes.append(f"STEP {step:.3f}m")
        return action, notes

    return NS(
        _clamp_action=clamp,
        _go_home=lambda env, args: (_ for _ in ()).throw(AssertionError("home")),
        _resolve_home=lambda args: np.array([*START, *EULER, 1.0]),
    )


def build(
    *,
    clock=None,
    fence=None,
    env_kw=None,
    config_kw=None,
    limits=None,
    token_s=None,
    episode="r.reset.0001",
    band=BAND,
):
    """``NS(clock, env, guard, fence, robot, port, limits, cfg, band)``."""
    clock = clock or fake.FakeClock()
    env = AssistEnv(clock, **(env_kw or {}))
    guard = Guard()
    fence = fence or sm.MotionFence()
    args = NS(
        min_z=MIN_Z,
        max_xy=MAX_XY,
        max_step_m=0.1,
        binarize_gripper=True,
        home_pose=[*START, *EULER],
    )
    robot = B.Robot(
        env,
        guard,
        client_module(),
        args,
        clock,
        fence,
        tolerance_mm=10.0,
        emit=lambda text: None,
    )
    cfg = ga.GraspAssistConfig(enabled=True, holding_band=(band,), **(config_kw or {}))
    port = fr3_assist.Fr3AssistPort(
        robot,
        fence,
        clock,
        run_id="r",
        episode_id=lambda: episode,
        control_epoch=lambda: 1,
        policy_epoch=lambda: 1,
        **({"token_s": token_s} if token_s else {}),
    )
    limits = limits or ga.Limits(MIN_Z, MAX_XY)
    return NS(
        clock=clock,
        env=env,
        guard=guard,
        fence=fence,
        robot=robot,
        port=port,
        limits=limits,
        cfg=cfg,
        band=cfg.bands()[band.split("|")[0]],
    )


def session(r, **kw):
    """A takeover session on the rig (the policy gate is closed first, as the
    orchestrator does)."""
    r.port.begin()
    events: list = []
    s = ga.AssistSession(
        r.port,
        r.cfg,
        r.limits,
        r.band,
        run_id="r",
        sample_id="r:assist:1",
        policy_epoch=1,
        emit=lambda code, detail, sidecar=None: events.append((code, detail)),
        **kw,
    )
    s.events = events
    return s


def K(key):
    """The key's name from the byte the terminal sends (``x`` -> ``x+``)."""
    return ga.KEY_CHARS.get(key, key)


def press(s, key):
    """Propose and execute one key (a byte or a name)."""
    p = s.propose(K(key))
    return p, s.execute(p)
