"""In-chunk acceleration for the FR3 policy client (experimental).

A chunk the arm can safely hurry through -- straight, no gripper change anywhere in it, well after the last gripper
change -- is executed as a short ramp from its first to its last executed action instead of one action per tick:

    m = clip(ceil(max(D / (v dt), angle / (w dt), (tau / dt) ln(D / gap))), 2, n)    ticks instead of n

D is the chord, angle the rotation over the executed actions, v / w the speed caps, tau the controller's setpoint
low-pass (0.2 s) and gap how close the arm should be to the last action when the next observation is taken. Along the
ramp the target leads by tau times the ramp speed (the inverse of that low-pass), clamped to the ramp, so the target
never leaves the segment between the first and the last action. After a ramp, actions of the next chunk that lie
behind the ramp's end along its direction are dropped (at most `max_skip`), so a policy that does not see velocity
does not pull the target back.

Pure numpy. Every action this returns still passes the client's own clamps and jump checks; nothing here talks to the
robot, the server or a camera.
"""

from __future__ import annotations

import math

import numpy as np

SPEED = 0.25  # m/s along a ramp
ANG_SPEED = 0.8  # rad/s
DEV = 0.005  # m: how far the executed positions may stray from the chord
ROT_MAX = 0.15  # rad over the executed actions
GAP = 0.005  # m: how close the arm should get to the ramp's end before the next observation
TAU = 0.2  # s: the controller's setpoint low-pass (filter_params_ 0.005 at 1 kHz): the target's lead
# s: how fast the arm closes in on a held target. Not the filter's: with the filter at 0.1 s the lightly damped arm
# (damping ratio ~0.17) still rings, and a settle term on 0.1 s left it 24 mm (p90) from the target at the next
# observation, with 0.46-0.50 m/s peaks (open-loop replay of the 2026-10 evaluations, 2026-10-31)
SETTLE_TAU = 0.2
# s of executed time after a gripper change before a chunk may be hurried
GRIP_HOLD_S = 1.0
MAX_SKIP = 3  # actions of a new chunk that may be dropped as behind the last ramp
PLAN_LOST = 0.03  # m: the arm this far from the last action handed out did not execute that plan to its end
SAME_WAY = 0.9  # cosine between a new chunk's heading and the last ramp's for dropping actions behind it
MAX_ROT_JUMP = 0.15  # rad between two consecutive targets (the client refuses 0.35 rad from the measured pose)
MAX_JUMP = 0.05  # m between two consecutive targets (the client refuses a target 0.10 m from the measured pose)


def wrap(a):
    return (np.asarray(a, dtype=np.float64) + np.pi) % (2 * np.pi) - np.pi


def _closed(g) -> bool:
    return float(g) < 0.5


class ChunkAccel:
    def __init__(
        self,
        horizon: int,
        hz: float = 10.0,
        *,
        speed: float = SPEED,
        ang_speed: float = ANG_SPEED,
        dev: float = DEV,
        rot_max: float = ROT_MAX,
        gap: float = GAP,
        tau: float = TAU,
        grip_hold_s: float = GRIP_HOLD_S,
        max_skip: int = MAX_SKIP,
        max_jump: float = MAX_JUMP,
        settle_tau: float = SETTLE_TAU,
        max_rot_jump: float = MAX_ROT_JUMP,
    ):
        if (
            horizon < 1
            or hz <= 0
            or speed <= 0
            or ang_speed <= 0
            or gap <= 0
            or tau < 0
            or settle_tau <= 0
        ):
            raise ValueError("chunk acceleration needs horizon >= 1 and positive rates")
        self.horizon, self.dt = int(horizon), 1.0 / float(hz)
        self.speed, self.ang_speed, self.dev, self.rot_max = (
            speed,
            ang_speed,
            dev,
            rot_max,
        )
        self.settle_tau, self.max_rot_jump = settle_tau, max_rot_jump
        self.gap, self.tau, self.max_skip, self.max_jump = (
            gap,
            tau,
            int(max_skip),
            max_jump,
        )
        self.grip_hold = math.ceil(grip_hold_s * float(hz) - 1e-9)
        self.reset()

    def reset(self) -> None:
        """A new episode, or the client threw the last plan away (rejected jump, failed step)."""
        self.last = None  # the last action handed out
        self.direction = None  # unit direction of the last ramp, None if the last chunk was not hurried
        self.since_grip = (
            self.grip_hold
        )  # actions handed out since the last gripper change (the start counts as old)
        self.stats = {
            "chunks": 0,
            "hurried": 0,
            "ticks_planned": 0,
            "ticks_saved": 0,
            "dropped_behind": 0,
            "why_not": {
                "gripper": 0,
                "after_gripper": 0,
                "curved": 0,
                "rotation": 0,
                "no_gain": 0,
            },
        }

    def summary(self) -> dict:
        s = self.stats
        return {
            **s,
            "why_not": dict(s["why_not"]),
            "speed_m_s": self.speed,
            "gap_m": self.gap,
            "tau_s": self.tau,
            "settle_tau_s": self.settle_tau,
            "grip_hold_ticks": self.grip_hold,
        }

    # ---- the rule ----
    @staticmethod
    def _gripper_free(chunk: np.ndarray, prev_g) -> bool:
        """No gripper change anywhere in the chunk, and none against the last value sent (if known)."""
        g = [_closed(v) for v in chunk[:, 6]]
        return all(v == g[0] for v in g) and (prev_g is None or _closed(prev_g) == g[0])

    def _why_not(
        self, chunk: np.ndarray, seg: np.ndarray, m: int, prev_g
    ) -> str | None:
        if not self._gripper_free(chunk, prev_g):
            return "gripper"
        if self.since_grip < self.grip_hold:
            return "after_gripper"
        p0, p1 = seg[0, :3], seg[-1, :3]
        chord = p1 - p0
        d = float(np.linalg.norm(chord))
        rel = seg[:, :3] - p0
        if d > 1e-9:
            u = chord / d
            off = rel - np.outer(rel @ u, u)
        else:
            off = rel
        if float(np.max(np.linalg.norm(off, axis=1))) > self.dev:
            return "curved"
        if float(np.max(np.abs(wrap(seg[:, 3:6] - seg[0, 3:6])))) > self.rot_max:
            return "rotation"
        if m >= len(seg):
            return "no_gain"
        return None

    def _ticks(self, seg: np.ndarray) -> int:
        d = float(np.linalg.norm(seg[-1, :3] - seg[0, :3]))
        ang = float(np.max(np.abs(wrap(seg[-1, 3:6] - seg[0, 3:6]))))
        settle = (self.settle_tau / self.dt) * math.log(max(d, self.gap) / self.gap)
        m = math.ceil(
            max(d / (self.speed * self.dt), ang / (self.ang_speed * self.dt), settle)
            - 1e-9
        )
        return max(2, min(len(seg), m))

    def _too_steep(self, ramp: np.ndarray) -> bool:
        step = np.diff(ramp[:, :6].astype(np.float64), axis=0)
        return bool(
            np.max(np.linalg.norm(step[:, :3], axis=1)) > self.max_jump
            or np.max(np.abs(wrap(step[:, 3:6]))) > self.max_rot_jump
        )

    def _ramp(self, seg: np.ndarray, m: int) -> np.ndarray:
        a, b = seg[0].astype(np.float64), seg[-1].astype(np.float64)
        s = np.arange(m) / (m - 1)
        s = np.minimum(
            1.0, s + self.tau / ((m - 1) * self.dt)
        )  # lead tau x ramp speed, clamped to the ramp
        out = np.empty((m, seg.shape[1]))
        out[:, :3] = a[:3] + np.outer(s, b[:3] - a[:3])
        out[:, 3:6] = a[3:6] + np.outer(
            s, wrap(b[3:6] - a[3:6])
        )  # in the policy's own angle convention (no wrap)
        out[:, 6:] = a[6:]
        out[0] = a  # the first action as the policy gave it
        out[-1] = b  # the last action as the policy gave it
        return out.astype(seg.dtype, copy=False)

    def plan(self, chunk, state=None) -> np.ndarray:
        """The actions the client executes for this chunk, in order (the client re-queries after the last one).
        ``state``: the measured pose the chunk was inferred from. Far (> PLAN_LOST m) from the last action handed out,
        the last plan was not executed to its end (a rejected jump, a failed step, a skipped step): it is forgotten
        and nothing is dropped as behind it."""
        chunk = np.asarray(chunk)
        if chunk.ndim != 2 or chunk.shape[1] < 7 or len(chunk) == 0:
            raise ValueError("expected an action chunk of shape (N, >=7)")
        if (
            state is not None
            and self.last is not None
            and float(np.linalg.norm(np.asarray(state[:3], np.float64) - self.last[:3]))
            > PLAN_LOST
        ):
            self.direction = None
        # the gripper value the arm was last sent: the last action handed out, else the measured state's (episode start)
        prev_g = (
            self.last[6]
            if self.last is not None
            else (float(state[6]) if state is not None and len(state) >= 7 else None)
        )
        n_end = min(
            len(chunk), self.horizon
        )  # the client's open-loop horizon is never extended
        start = 0
        if self.direction is not None and self.last is not None and n_end > 1:
            pos = chunk[:, :3].astype(np.float64)
            heading = pos[n_end - 1] - pos[0]
            length = float(np.linalg.norm(heading))
            # only for a gripper-free chunk that keeps going the same way (a turn or a reversal keeps every action),
            # and never to a first target further than max_jump from the last one sent
            if (
                length > 1e-6
                and float(heading @ self.direction) / length >= SAME_WAY
                and self._gripper_free(chunk, prev_g)
            ):
                ahead = (pos - self.last[:3]) @ self.direction
                while (
                    start < min(self.max_skip, n_end - 1)
                    and ahead[start] < 0.0
                    and float(np.linalg.norm(pos[start + 1] - self.last[:3]))
                    <= self.max_jump
                ):
                    start += 1
        seg = chunk[start:n_end]
        m = self._ticks(seg)
        while m < len(seg) and self._too_steep(self._ramp(seg, m)):
            m += 1  # the lead makes big steps on a short ramp
        why = self._why_not(chunk, seg, m, prev_g)
        self.stats["chunks"] += 1
        self.stats["dropped_behind"] += start
        if why is None:
            out = self._ramp(seg, m)
            chord = seg[-1, :3].astype(np.float64) - seg[0, :3]
            d = float(np.linalg.norm(chord))
            self.direction = chord / d if d > 1e-6 else None
            self.stats["hurried"] += 1
            self.stats["ticks_saved"] += len(seg) - m
        else:
            out = seg
            self.direction = None
            self.stats["why_not"][why] += 1
        self.stats["ticks_planned"] += len(out)
        changed = prev_g is not None and _closed(prev_g) != _closed(out[0, 6])
        self.since_grip = 0 if changed else self.since_grip
        flips = sum(
            _closed(out[i, 6]) != _closed(out[i - 1, 6]) for i in range(1, len(out))
        )
        if flips:
            last_flip = max(
                i
                for i in range(1, len(out))
                if _closed(out[i, 6]) != _closed(out[i - 1, 6])
            )
            self.since_grip = len(out) - last_flip
        else:
            self.since_grip += len(out)
        self.last = np.asarray(out[-1], dtype=np.float64).copy()
        return out
