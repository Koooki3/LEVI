"""In-chunk acceleration for the FR3 policy client (experimental).

Rule v1 (the default). A chunk the arm can safely hurry through -- straight, no gripper change anywhere in it, well
after the last gripper change -- is executed as a short ramp from its first to its last executed action instead of
one action per tick:

    m = clip(ceil(max(D / (v dt), angle / (w dt), (tau / dt) ln(D / gap))), 2, n)    ticks instead of n

D is the chord, angle the rotation over the executed actions, v / w the speed caps, tau the controller's setpoint
low-pass (0.2 s) and gap how close the arm should be to the last action when the next observation is taken. Along the
ramp the target leads by tau times the ramp speed (the inverse of that low-pass), clamped to the ramp, so the target
never leaves the segment between the first and the last action. After a ramp, actions of the next chunk that lie
behind the ramp's end along its direction are dropped (at most `max_skip`), so a policy that does not see velocity
does not pull the target back.

Rule v2 (``rule="v2"``; the "V4g" of the offline study fr3_accel_test/variable_speed/rule_v2, 2026-10-31) adds:

- a curved chunk (one that fails the straightness test, still within the rotation limit) is re-timed along its own
  path: the polyline through the executed actions keeps its shape and gets a speed profile limited by v, by the
  corner the setpoint filter would cut (v <= sqrt(a_lat / kappa), a_lat = 2 CUT_MAX / tau^2: the filter cuts a curve
  by about 0.5 a_lat tau^2), by the angular rate, and by A_TAN along the path, at rest on the last action; the target
  leads by tau times the speed along the path, never off it; straight chunks keep v1's chord;
- the executed actions before the first gripper change (at least 2) may be hurried the same way when they pass the
  same tests, at least 1 s after the previous change. The hurried prefix ends on the pre-gripper action, sized to get
  within GRIP_GAP of it, and holds it GRIP_EXTRA more ticks, so the gripper command goes out with the arm settled;
  from the gripper action on, the policy's own pace. A change only past the executed part makes the whole executed
  part such a prefix (the next chunk may open with the gripper action);
- a hurried plan gets more ticks until no target is predicted more than GAP_GUARD from the arm (a first-order
  approach with SETTLE_TAU from the measured pose).

Pure numpy. Every action this returns still passes the client's own clamps and jump checks; nothing here talks to the
robot, the server or a camera. The client reports every action it accepts (`executed`) or refuses (`rejected`): the
gripper hold counts executed actions only, so a plan the client threw away never shortens it.
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

RULES = ("v1", "v2")
# rule v2 (study V4g, fr3_accel_test/variable_speed/rule_v2/README.md section 5)
GRIP_GAP = 0.005  # m: the hurried prefix is sized to get the arm this close to the pre-gripper action
GRIP_EXTRA = 2  # ticks the pre-gripper action is held after that: the gripper command goes out with the arm settled
A_TAN = 1.5  # m/s^2 along a re-timed path (the 1 cm error clip allows about 2.4 m/s^2)
CUT_MAX = 0.002  # m: corner cut the setpoint filter may add on a re-timed path
CURV_LEN = 0.015  # m of arc on each side of a vertex over which its curvature is measured (the filter averages
#                   the target over tau x v = 2-5 cm, so a turn between two 5 mm actions is not a corner)
GAP_GUARD = (
    0.06  # m: no target of a hurried plan predicted further than this from the arm
)
SUB = 24  # sub-samples per path segment for the time integral
PREFIX_WHY = (
    "prefix_after_gripper",
    "prefix_short",
    "prefix_curved",
    "prefix_rotation",
    "prefix_no_gain",
)


def wrap(a):
    return (np.asarray(a, dtype=np.float64) + np.pi) % (2 * np.pi) - np.pi


def _closed(g) -> bool:
    # as the client binarizes it (1.0 if g >= 0.5 else 0.0): NaN is closed
    return not float(g) >= 0.5


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
        rule: str = "v1",
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
        if rule not in RULES:
            raise ValueError(f"rule must be one of {RULES}, got {rule!r}")
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
        self.rule = rule
        v2 = rule == "v2"
        self.prefix = v2  # hurry the executed actions before a gripper change
        self.geometry = (
            "auto" if v2 else "chord"
        )  # auto: the chord where straight, the path elsewhere
        self.gap_guard = GAP_GUARD if v2 else None
        self.grip_gap, self.grip_extra = GRIP_GAP, GRIP_EXTRA
        self.a_tan, self.cut_max, self.curv_len = A_TAN, CUT_MAX, CURV_LEN
        self.a_lat = 2.0 * self.cut_max / max(self.tau, 0.02) ** 2
        self.reset()

    def reset(self) -> None:
        """A new episode."""
        self.last = None  # the last action handed out
        self.direction = None  # unit direction of the last ramp, None if the last chunk was not hurried
        # actions the client accepted since the last gripper change, the change included (the start counts as old)
        self.since_grip = self.grip_hold
        self.exec_g = None  # the gripper (closed or not) of the last accepted action
        self.issued_g = None  # the measured state's gripper (the last command issued) at the previous plan
        self._arm = None  # the measured position the current plan starts from (rule v2's gap guard)
        self.stats = {
            "chunks": 0,
            "hurried": 0,
            "ticks_planned": 0,
            "ticks_saved": 0,
            "dropped_behind": 0,
            "rejected": 0,
            "hurried_prefix": 0,  # rule v2: chunks whose actions before a gripper change were hurried
            "prefix_ticks_saved": 0,
            "hurried_path": 0,  # rule v2: hurried chunks or prefixes re-timed along their path
            "why_not": {
                "gripper": 0,
                "after_gripper": 0,
                "curved": 0,
                "rotation": 0,
                "no_gain": 0,
                "not_finite": 0,
                **{k: 0 for k in PREFIX_WHY},
            },
        }

    def executed(self, action) -> None:
        """The client accepted ``action`` (it passed the jump checks) and sends it this tick."""
        g = _closed(np.asarray(action)[6])
        changed = self.exec_g is not None and g != self.exec_g
        self.since_grip = 1 if changed else self.since_grip + 1
        self.exec_g = g

    def rejected(self) -> None:
        """The client refused an action (a jump) and threw the rest of the plan away: nothing ran, nothing counts."""
        self.direction = None
        self.stats["rejected"] += 1

    def summary(self) -> dict:
        s = self.stats
        return {
            **s,
            "why_not": dict(s["why_not"]),
            "rule": self.rule,
            "speed_m_s": self.speed,
            "gap_m": self.gap,
            "tau_s": self.tau,
            "settle_tau_s": self.settle_tau,
            "grip_hold_ticks": self.grip_hold,
        }

    # ---- the tests a segment must pass ----
    @staticmethod
    def _gripper_free(chunk: np.ndarray, prev_g) -> bool:
        """No gripper change anywhere in the chunk, and none against the last value sent (if known)."""
        g = [_closed(v) for v in chunk[:, 6]]
        return all(v == g[0] for v in g) and (prev_g is None or _closed(prev_g) == g[0])

    @staticmethod
    def _first_change(chunk: np.ndarray, prev_g) -> int:
        """Index of the first action whose gripper differs from the one before it (the last value sent for the
        first action); len(chunk) if none."""
        flags = [_closed(v) for v in chunk[:, 6]]
        ref = _closed(prev_g) if prev_g is not None else flags[0]
        for j, fl in enumerate(flags):
            if fl != ref:
                return j
            ref = fl
        return len(flags)

    def _shape_why(self, seg: np.ndarray) -> str | None:
        p0, p1 = seg[0, :3].astype(np.float64), seg[-1, :3].astype(np.float64)
        chord = p1 - p0
        d = float(np.linalg.norm(chord))
        rel = seg[:, :3].astype(np.float64) - p0
        if d > 1e-9:
            u = chord / d
            # to the SEGMENT, not the infinite line: a press-and-retract along the chord is not straight
            off = rel - np.outer(np.clip(rel @ u, 0.0, d), u)
        else:
            off = rel
        if float(np.max(np.linalg.norm(off, axis=1))) > self.dev:
            return "curved"
        return self._rot_why(seg)

    def _rot_why(self, seg: np.ndarray) -> str | None:
        if float(np.max(np.abs(wrap(seg[:, 3:6] - seg[0, 3:6])))) > self.rot_max:
            return "rotation"
        return None

    def _ticks(self, seg: np.ndarray, gap: float) -> int:
        d = float(np.linalg.norm(seg[-1, :3] - seg[0, :3]))
        ang = float(np.max(np.abs(wrap(seg[-1, 3:6] - seg[0, 3:6]))))
        settle = (self.settle_tau / self.dt) * math.log(max(d, gap) / gap)
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

    def _gap_risk(self, out: np.ndarray) -> bool:
        """Rule v2: a target predicted more than gap_guard from the arm (first-order approach from the measured pose)."""
        if self.gap_guard is None or self._arm is None:
            return False
        x, k = self._arm.copy(), math.exp(-self.dt / self.settle_tau)
        for u in out[:, :3].astype(np.float64):
            if float(np.linalg.norm(u - x)) > self.gap_guard:
                return True
            x = u + (x - u) * k
        return False

    # ---- geometries: (plan or None, why not, direction of its end) ----
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

    @staticmethod
    def _hold(out: np.ndarray, extra: int) -> np.ndarray:
        return np.vstack([out, np.repeat(out[-1:], extra, axis=0)]) if extra else out

    def _chord(self, seg: np.ndarray, gap: float, extra: int):
        why = self._shape_why(seg)
        if why:
            return None, why, None
        m = self._ticks(seg, gap)
        while m < len(seg) and (
            self._too_steep(self._ramp(seg, m)) or self._gap_risk(self._ramp(seg, m))
        ):
            m += 1  # the lead makes big steps on a short ramp
        if m + extra >= len(seg):
            return None, "no_gain", None
        chord = seg[-1, :3].astype(np.float64) - seg[0, :3]
        d = float(np.linalg.norm(chord))
        return (
            self._hold(self._ramp(seg, m), extra),
            None,
            chord / d if d > 1e-6 else None,
        )

    def _path(self, seg: np.ndarray, gap: float, extra: int):
        """Rule v2: re-time the polyline through the executed actions (see the module docstring)."""
        why = self._rot_why(seg)
        if why:
            return None, why, None
        P = seg[:, :3].astype(np.float64)
        n = len(P)
        dP = np.diff(P, axis=0)
        ln = np.linalg.norm(dP, axis=1)
        S = float(ln.sum())
        if S < 1e-6 or n < 3:
            return self._chord(
                seg, gap, extra
            )  # a point or one segment: the path is the chord
        # rotation along the path, unwrapped from the first action (the policy's own angle convention)
        E = seg[0, 3:6].astype(np.float64) + np.vstack(
            [np.zeros(3), np.cumsum(wrap(np.diff(seg[:, 3:6], axis=0)), axis=0)]
        )
        dE = np.max(np.abs(np.diff(E, axis=0)), axis=1)
        cap = np.full(n - 1, float(self.speed))
        nz = dE > 1e-12
        cap[nz] = np.minimum(
            cap[nz], self.ang_speed * np.maximum(ln[nz], 1e-9) / dE[nz]
        )
        good = ln > 1e-9
        dirs = np.zeros_like(dP)
        dirs[good] = dP[good] / ln[good, None]
        for j in range(1, n - 1):  # a zero-length segment keeps the previous direction
            if not good[j]:
                dirs[j] = dirs[j - 1]
        cum = np.concatenate([[0.0], np.cumsum(ln)])

        def at(su):
            j = np.clip(np.searchsorted(cum, su, side="right") - 1, 0, n - 2)
            fr = np.where(ln[j] > 1e-12, (su - cum[j]) / np.maximum(ln[j], 1e-12), 0.0)
            return P[j] + fr[:, None] * dP[j], E[j] + fr[:, None] * (E[j + 1] - E[j])

        V = np.empty(n)
        V[0] = min(cap[0], ln[0] / self.dt)  # the policy's own pace at the start
        for i in range(1, n - 1):
            # turning angle between the chords to the points curv_len away, over their mean length
            pa, pb = at(
                np.array(
                    [max(0.0, cum[i] - self.curv_len), min(S, cum[i] + self.curv_len)]
                )
            )[0]
            va, vb = P[i] - pa, pb - P[i]
            la, lb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
            if la < 1e-9 or lb < 1e-9:
                kappa = 0.0
            else:
                kappa = math.acos(float(np.clip(va @ vb / (la * lb), -1.0, 1.0))) / (
                    0.5 * (la + lb)
                )
            V[i] = min(
                cap[i - 1],
                cap[i],
                math.sqrt(self.a_lat / kappa) if kappa > 1e-9 else math.inf,
            )
        V[-1] = 0.0  # at rest on the last action
        a = self.a_tan
        for i in range(1, n):
            V[i] = min(V[i], math.sqrt(V[i - 1] ** 2 + 2 * a * ln[i - 1]))
        for i in range(n - 2, -1, -1):
            V[i] = min(V[i], math.sqrt(V[i + 1] ** 2 + 2 * a * ln[i]))
        # time table along the path
        s_tab, t_tab, v_tab = [0.0], [0.0], [V[0]]
        s0 = t0 = 0.0
        f = np.linspace(0.0, 1.0, SUB + 1)
        for j in range(n - 1):
            if not good[j]:
                continue
            s = f * ln[j]
            sm = 0.5 * (s[1:] + s[:-1])
            vm = np.minimum(
                cap[j],
                np.minimum(
                    np.sqrt(V[j] ** 2 + 2 * a * sm),
                    np.sqrt(V[j + 1] ** 2 + 2 * a * (ln[j] - sm)),
                ),
            )
            vn = np.minimum(
                cap[j],
                np.minimum(
                    np.sqrt(V[j] ** 2 + 2 * a * s),
                    np.sqrt(V[j + 1] ** 2 + 2 * a * (ln[j] - s)),
                ),
            )
            tt = t0 + np.cumsum(np.diff(s) / np.maximum(vm, 1e-6))
            s_tab.extend((s0 + s[1:]).tolist())
            t_tab.extend(tt.tolist())
            v_tab.extend(vn[1:].tolist())
            s0 += ln[j]
            t0 = float(tt[-1])
        s_tab, t_tab, v_tab = np.array(s_tab), np.array(t_tab), np.array(v_tab)
        T = float(t_tab[-1])
        settle = (self.settle_tau / self.dt) * math.log(max(S, gap) / gap)
        m = max(2, math.ceil(max(T / self.dt, settle) - 1e-9))
        stretched = True  # a profile shorter than the ticks is slowed to fill them, like the chord ramp
        while True:
            if m + extra >= len(seg):
                return None, "no_gain", None
            Tp = max(T, (m - 1) * self.dt) if stretched else T
            lam = (
                T / Tp if Tp > 0 else 1.0
            )  # profile time = lam x real time (lam <= 1: slower)
            tp = np.minimum(T, lam * self.dt * np.arange(m))
            sj = np.interp(tp, t_tab, s_tab)
            vj = lam * np.interp(tp, t_tab, v_tab)
            pos, ang = at(
                np.minimum(S, sj + self.tau * vj)
            )  # lead tau x speed, along the path
            out = np.empty((m, seg.shape[1]))
            out[:, :3], out[:, 3:6], out[:, 6:] = pos, ang, seg[0, 6:]
            out[0], out[-1] = seg[0], seg[-1]
            if (self._too_steep(out) or self._gap_risk(out)) and m < len(seg):
                m += 1
                continue
            break
        last = np.nonzero(ln > 1e-4)[0]
        dvec = dirs[last[-1]] if len(last) else None
        return self._hold(out.astype(seg.dtype, copy=False), extra), None, dvec

    def _hurry(self, seg: np.ndarray, gap: float, extra: int):
        if self.geometry == "auto" and self._shape_why(seg) is not None:
            return self._path(seg, gap, extra) + ("path",)
        return self._chord(seg, gap, extra) + ("chord",)

    def _drop_behind(self, chunk: np.ndarray, n_end: int, prev_g) -> int:
        """Leading actions of a gripper-free chunk that keeps going the way the last ramp went and lie behind its end
        (a turn or a reversal keeps every action); never to a first target further than max_jump from the last one."""
        start = 0
        if self.direction is None or self.last is None or n_end <= 1:
            return 0
        pos = chunk[:, :3].astype(np.float64)
        heading = pos[n_end - 1] - pos[0]
        length = float(np.linalg.norm(heading))
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
                and float(np.max(np.abs(wrap(chunk[start + 1, 3:6] - self.last[3:6]))))
                <= self.max_rot_jump
            ):
                start += 1
        return start

    # ---- the rule ----
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
        # The gripper value the arm was last sent. The measured state carries the command fr3_env actually issued; it
        # beats the last action handed out, which the client may have thrown away (a rejected jump) or not sent yet
        # (the gripper rate limit). A change seen there restarts the hold.
        issued = float(state[6]) if state is not None and len(state) >= 7 else None
        if (
            issued is not None
            and self.issued_g is not None
            and _closed(issued) != _closed(self.issued_g)
        ):
            self.since_grip = 0
        if issued is not None:
            self.issued_g = issued
        prev_g = (
            issued
            if issued is not None
            else (self.last[6] if self.last is not None else None)
        )
        self._arm = np.asarray(state[:3], np.float64) if state is not None else None
        n_end = min(
            len(chunk), self.horizon
        )  # the client's open-loop horizon is never extended
        # a NaN or inf pose in the executed part: the client gets the policy's actions exactly as without the switch
        # (a NaN gripper is not a pose: the client binarizes it as closed, and the gripper test does the same)
        finite = bool(np.isfinite(np.asarray(chunk[:n_end, :6], np.float64)).all())
        start = self._drop_behind(chunk, n_end, prev_g) if finite else 0
        seg = chunk[start:n_end]
        out = dvec = geo = kind = None
        if not finite:
            why = "not_finite"
        elif self._gripper_free(chunk, prev_g):
            if self.since_grip < self.grip_hold:
                why = "after_gripper"
            else:
                out, why, dvec, geo = self._hurry(seg, self.gap, 0)
                kind = "full"
        elif not self.prefix:
            why = "gripper"
        elif self.since_grip < self.grip_hold:
            why = "prefix_after_gripper"
        else:
            # nothing was dropped (start is 0 for a gripper chunk): the prefix is the executed actions before the change
            kk = min(self._first_change(chunk, prev_g), len(seg))
            if kk < 2:
                why = "prefix_short"
            else:
                r, w, _, geo = self._hurry(seg[:kk], self.grip_gap, self.grip_extra)
                if r is None:
                    why = "prefix_" + w
                else:
                    out, why, kind = (
                        np.vstack([r, seg[kk:]]).astype(seg.dtype, copy=False),
                        None,
                        "prefix",
                    )
        self.stats["chunks"] += 1
        self.stats["dropped_behind"] += start
        if out is not None:
            saved = len(seg) - len(out)
            self.stats["ticks_saved"] += saved
            if kind == "full":
                self.stats["hurried"] += 1
                self.direction = dvec
            else:
                self.stats["hurried_prefix"] += 1
                self.stats["prefix_ticks_saved"] += saved
                self.direction = None  # nothing is dropped after a gripper action
            self.stats["hurried_path"] += geo == "path"
        else:
            out = seg
            self.direction = None
            self.stats["why_not"][why] += 1
        self.stats["ticks_planned"] += len(out)
        # the gripper hold is advanced by executed(), called by the client for every action it sends
        self.last = np.asarray(out[-1], dtype=np.float64).copy()
        return out
