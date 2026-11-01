"""Phase clock core (phase.py): the frozen interface, every PhaseParams default and derived value, the relaxation tags
(test 14, TESTGAPS) and the engine itself (design section 7, WP1 tests 1-13 plus a seeded fuzz of invariants I3-I10).

Pure numpy, no openpi, robot or GPU, about 3 s of CPU. A seeded master trajectory stands in for the policy; the arm is
either exact (PerfectArm) or a lag plant (first order D_alpha + 20 ms dead time + a 2.65 Hz / zeta 0.17 mode). A changed
default or a lost tag fails in TestParamsDefaults / TestRelaxedTags, naming the field.
"""

import dataclasses
import inspect
import itertools
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import phase
from phase import (
    ALPHAS,
    CLIENT_MAX_M,
    CLIENT_MAX_RAD,
    GRIP_CLOSED,
    GRIP_OPEN,
    RELAXED_TAG,
    Inflight,
    Measured,
    NonFiniteChunk,
    PhaseEngine,
    PhaseParams,
    Proposal,
    TickOut,
    wrap_to_pi,
)

# Section 5, every default. A field missing here, or a default that moved, fails test_defaults_pinned.
DEFAULTS = {
    "lambda_max": 2.0,
    "alpha": 0.005,
    "golden": False,
    "lead": False,
    "swap_reanchor": False,
    "w_pre": 3,
    "w_post": 3,
    "v_cap": None,
    "omega_cap": None,
    "a_lat": None,
    "d_alpha": None,
    "lag_safety": 0.94,
    "lag_offset_m": 0.004,
    "lag_offset_rad": 0.01,
    "d_meas_min": 0.10,
    "d_meas_max": 0.40,
    "d_meas_window": 30,
    "d_meas_speed": 0.03,
    "curv_len": 0.05,
    "curv_min_arc": 0.010,
    "rate_up": 0.8,
    "jerk": 0.45,
    "a_tan_up": 1.5,
    "d_dn_max": 0.5,
    "d_dn_gain": 0.9,
    "rate_dn_soft": 0.5,
    "soft_leash": True,
    "soft_m0": 0.05,
    "soft_m1": 0.08,
    "soft_rad0": 0.20,
    "soft_rad1": 0.28,
    "hard_leash": True,
    "hard_leash_m": 0.08,
    "hard_leash_rad": 0.28,
    "step_limit": True,
    "step_m": 0.05,
    "step_rad": 0.12,
    "hold_reset_after": 3,
    "bisect_iters": 8,
    "pause_dp": 0.0005,
    "pause_dr": 0.005,
    "blend": True,
    "blend_min_ticks": 2,
    "blend_max_ticks": 5,
    "blend_stretch_max_ticks": 8,
    "blend_step_m": 0.025,
    "blend_step_rad": 0.05,
    "blend_skip_m": 0.002,
    "blend_skip_rad": 0.01,
    "plain_reanchor": True,
    "plain_reanchor_rows": 4,
    "plain_reanchor_span": 5,
    "swap_reanchor_rows": 3,
    "swap_reanchor_span": 4,
    "reanchor_cos_min": 0.5,
    "lead_max_rows": 3.0,
    "grip_gate": True,
    "gate_dist_m": 0.008,
    "gate_speed": 0.05,
    "gate_max_ticks": 5,
    "grip_settle": True,
    "settle_min_s": 0.3,
    "settle_aperture_delta": 0.02,
    "settle_stable_ticks": 2,
    "settle_stable_eps": 0.01,
    "settle_timeout_s": 1.0,
    "settle_unknown_s": 0.8,
    "governor": True,
    "governor_margin_rows": 0.5,
    "early_trigger": True,
    "delay_history": 8,
}

# Section 5 / 15: the ledger numbers phase.py must carry (E8 is a tightening, E11 and E12 live in other files).
RELAXED_IN_PHASE = (1, 2, 3, 4, 5, 6, 7, 9, 10, 13, 14)


class TestParamsDefaults(unittest.TestCase):
    def test_defaults_pinned(self):
        got = PhaseParams().as_dict()
        self.assertEqual(
            set(got),
            set(DEFAULTS),
            "a PhaseParams field is missing from DEFAULTS or the reverse",
        )
        for name, want in DEFAULTS.items():
            self.assertEqual(got[name], want, f"PhaseParams.{name} default changed")
            self.assertIs(
                type(got[name]), type(want), f"PhaseParams.{name} changed type"
            )

    def test_frozen(self):
        p = PhaseParams()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.lambda_max = 3.0  # type: ignore[misc]

    def test_json_round_trip(self):
        p = PhaseParams.for_page(2.5, 0.01)
        text = json.dumps(p.as_dict(), allow_nan=False)
        self.assertEqual(PhaseParams.from_dict(json.loads(text)), p)
        json.dumps(p.derived(), allow_nan=False)
        self.assertEqual(PhaseParams.from_dict(PhaseParams().as_dict()), PhaseParams())

    def test_ints_and_floats_are_coerced(self):
        p = PhaseParams(lambda_max=2, w_pre=3.0)
        self.assertIs(type(p.lambda_max), float)
        self.assertIs(type(p.w_pre), int)
        self.assertEqual(PhaseParams(alpha=0.0050000000001).alpha, 0.005)


class TestDerived(unittest.TestCase):
    def test_alpha_0005(self):
        d = PhaseParams().derived()
        self.assertEqual(
            set(d),
            {
                "D_alpha",
                "v_cap",
                "omega_cap",
                "v_lag",
                "omega_lag",
                "a_lat",
                "g_lead",
                "d_dn",
            },
        )
        self.assertEqual(d["D_alpha"], 0.27)
        self.assertEqual(d["v_cap"], 0.30)
        self.assertEqual(d["omega_cap"], 0.8)
        self.assertEqual(d["a_lat"], 2.4)
        self.assertAlmostEqual(d["v_lag"], 0.94 * (0.080 - 0.004) / 0.27, places=12)
        self.assertAlmostEqual(d["v_lag"], 0.265, places=3)  # section 3.2: 0.265 m/s
        self.assertAlmostEqual(
            d["omega_lag"], 0.94, places=9
        )  # 0.94 * (0.28 - 0.01) / 0.27
        self.assertAlmostEqual(d["g_lead"], 2.7, places=9)
        self.assertEqual(d["d_dn"], 0.5)  # lead off

    def test_alpha_001(self):
        d = PhaseParams(alpha=0.01).derived()
        self.assertEqual(d["D_alpha"], 0.17)
        self.assertEqual(d["v_cap"], 0.35)
        self.assertEqual(d["omega_cap"], 1.0)
        self.assertEqual(d["a_lat"], 2.4)
        self.assertAlmostEqual(d["v_lag"], 0.42, places=2)  # then cut by v_cap
        self.assertAlmostEqual(d["omega_lag"], 1.49, places=2)
        self.assertAlmostEqual(d["g_lead"], 1.7, places=9)
        self.assertEqual(d["d_dn"], 0.5)

    def test_d_dn_with_lead(self):
        self.assertAlmostEqual(
            PhaseParams(lead=True).derived()["d_dn"], 0.9 / 2.7, places=9
        )  # 0.33
        self.assertEqual(
            PhaseParams(lead=True, alpha=0.01).derived()["d_dn"], 0.5
        )  # min(0.5, 0.9 / 1.7 = 0.53)

    def test_explicit_overrides_win(self):
        d = PhaseParams(v_cap=0.2, omega_cap=0.5, a_lat=0.3, d_alpha=0.3).derived()
        self.assertEqual(
            (d["v_cap"], d["omega_cap"], d["a_lat"], d["D_alpha"]), (0.2, 0.5, 0.3, 0.3)
        )
        self.assertAlmostEqual(d["g_lead"], 3.0, places=9)

    def test_lag_caps_follow_d_hat(self):
        p = PhaseParams()
        v, w = p.lag_caps(0.40)  # a slower arm than modelled: lower caps
        self.assertLess(v, p.derived()["v_lag"])
        self.assertLess(w, p.derived()["omega_lag"])

    def test_hz_scales_g_lead(self):
        self.assertAlmostEqual(PhaseParams().derived(hz=20.0)["g_lead"], 5.4, places=9)


class TestValidation(unittest.TestCase):
    def test_for_page_accepts_the_page_choices(self):
        for lam in (1.5, 2.0, 2.5):
            for alpha in ALPHAS:
                p = PhaseParams.for_page(lam, alpha)
                self.assertEqual((p.lambda_max, p.alpha), (lam, alpha))
        self.assertEqual(
            PhaseParams.for_page(3.0, 0.005).lambda_max, 3.0
        )  # offline only, but valid
        self.assertEqual(PhaseParams.for_page(1.0, 0.005).lambda_max, 1.0)

    def test_for_page_rejects(self):
        for lam in (0.99, 3.01, float("nan"), float("inf"), -1.0, "2.5", None, True):
            with self.assertRaises(ValueError, msg=f"lambda_max={lam!r}"):
                PhaseParams.for_page(lam, 0.005)
        for alpha in (0.0, 0.007, 0.02, float("nan"), "0.005", None):
            with self.assertRaises(ValueError, msg=f"alpha={alpha!r}"):
                PhaseParams.for_page(2.0, alpha)

    def test_section5_ranges(self):
        bad = {
            "w_pre": 7,
            "w_post": 11,
            "v_cap": 0.41,
            "omega_cap": 1.21,
            "a_lat": 2.41,
            "d_alpha": 0.09,
            "rate_up": 1.01,
            "jerk": 0.61,
            "a_tan_up": 2.01,
            "step_m": 0.0501,
            "step_rad": 0.1201,
            "hard_leash_m": CLIENT_MAX_M,
            "hard_leash_rad": CLIENT_MAX_RAD,
            "blend_max_ticks": 6,
            "blend_stretch_max_ticks": 9,
            "plain_reanchor_rows": 5,
            "swap_reanchor_rows": 4,
            "gate_max_ticks": 6,
            "gate_dist_m": 0.011,
            "settle_timeout_s": 1.1,
            "reanchor_cos_min": 0.4,
            "hold_reset_after": 4,
            "w_pre_float": None,
        }
        for name, value in bad.items():
            if name == "w_pre_float":
                with self.assertRaises(ValueError, msg=name):
                    PhaseParams(w_pre=2.5)
                continue
            with self.assertRaises(ValueError, msg=f"{name}={value}"):
                PhaseParams(**{name: value})
        PhaseParams(
            v_cap=0.40, omega_cap=1.2, a_lat=2.4, w_pre=6, w_post=10
        )  # the inclusive upper ends

    def test_hard_leash_stays_inside_the_client_limits(self):
        self.assertLess(PhaseParams().hard_leash_m, CLIENT_MAX_M)
        self.assertLess(PhaseParams().hard_leash_rad, CLIENT_MAX_RAD)
        self.assertEqual((CLIENT_MAX_M, CLIENT_MAX_RAD), (0.10, 0.35))

    def test_safety_layer_cannot_be_switched_off_outside_golden(self):
        for kwargs in ({"hard_leash": False}, {"step_limit": False}):
            with self.assertRaises(ValueError):
                PhaseParams(**kwargs)

    def test_inconsistent_pairs(self):
        for kwargs in (
            {"soft_m0": 0.08},
            {"soft_rad0": 0.28},
            {"d_meas_min": 0.35, "d_meas_max": 0.2},
            {"blend_min_ticks": 4, "blend_max_ticks": 3},
            {"blend_max_ticks": 5, "blend_stretch_max_ticks": 4},
            {"settle_min_s": 0.9, "settle_timeout_s": 0.5},
        ):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                PhaseParams(**kwargs)

    def test_error_names_the_field(self):
        with self.assertRaisesRegex(ValueError, "lambda_max"):
            PhaseParams(lambda_max=4.0)


class TestGolden(unittest.TestCase):
    def test_golden_params(self):
        g = PhaseParams.golden_params()
        self.assertTrue(g.golden)
        self.assertEqual(g.lambda_max, 1.0)
        for name in (
            "lead",
            "swap_reanchor",
            "soft_leash",
            "hard_leash",
            "step_limit",
            "blend",
            "plain_reanchor",
            "grip_gate",
            "grip_settle",
            "governor",
            "early_trigger",
        ):
            self.assertIs(getattr(g, name), False, name)
        self.assertEqual((g.w_pre, g.w_post), (0, 0))
        self.assertEqual(g.alpha, 0.005)

    def test_golden_is_a_master_switch(self):
        self.assertEqual(
            PhaseParams(golden=True, lambda_max=2.5, lead=True),
            PhaseParams.golden_params(),
        )

    def test_golden_json_safe(self):
        json.dumps(PhaseParams.golden_params().as_dict(), allow_nan=False)
        json.dumps(PhaseParams.golden_params().derived(), allow_nan=False)


class TestRelaxedTags(unittest.TestCase):
    def test_every_phase_tag_is_in_the_source(self):
        src = Path(phase.__file__).read_text()
        self.assertEqual(RELAXED_TAG, "RELAXED[2026-11-01]")
        for n in RELAXED_IN_PHASE:
            tag = f"{RELAXED_TAG} E{n} was: "
            self.assertIn(
                tag,
                src,
                f"phase.py lost its {tag.strip()} marker (LIMITS_LEDGER section E)",
            )

    def test_no_unknown_ledger_number(self):
        import re

        src = Path(phase.__file__).read_text()
        found = {int(n) for n in re.findall(re.escape(RELAXED_TAG) + r" E(\d+)", src)}
        self.assertLessEqual(found, set(RELAXED_IN_PHASE) | {11, 12})
        self.assertNotIn(8, found, "E8 is a tightening, not a relaxation")


class TestInterface(unittest.TestCase):
    """Section 6, signature by signature: WP2 to WP5 are written against these names."""

    def test_constants(self):
        self.assertEqual((GRIP_OPEN, GRIP_CLOSED), (1.0, 0.0))
        self.assertEqual(ALPHAS, (0.005, 0.01))

    def test_param_names(self):
        for name, params in {
            "for_page": ["lambda_max", "alpha"],
            "golden_params": [],
            "derived": ["hz"],
            "as_dict": [],
        }.items():
            sig = inspect.signature(getattr(PhaseParams, name))
            got = [p for p in sig.parameters if p != "self"]
            self.assertEqual(got, params, name)

    def test_engine_signatures(self):
        want = {
            "__init__": ["params", "hz"],
            "reset": [],
            "has_rows": [],
            "begin": ["m", "inflight"],
            "set_chunk": ["rows", "kind"],
            "swap": ["rows", "s_obs"],
            "append": ["rows"],
            "splice_plain": ["rows", "m", "h"],
            "preview": ["n"],
            "commit": [],
            "tick": ["m", "inflight"],
            "executed": ["sent"],
            "rejected": [],
            "prev_rows": [],
            "future_row": [],
            "summary": [],
            "drain_records": [],
        }
        for name, params in want.items():
            sig = inspect.signature(getattr(PhaseEngine, name))
            self.assertEqual([p for p in sig.parameters if p != "self"], params, name)
            if name != "__init__":
                self.assertTrue(
                    inspect.getdoc(getattr(PhaseEngine, name)),
                    f"PhaseEngine.{name} needs a docstring",
                )
        self.assertIsNone(
            inspect.signature(PhaseEngine.tick).parameters["inflight"].default
        )
        self.assertEqual(
            inspect.signature(PhaseEngine.__init__).parameters["hz"].default, 10.0
        )
        for prop in ("u", "s", "lam", "rows_left"):
            self.assertIsInstance(
                inspect.getattr_static(PhaseEngine, prop), property, prop
            )

    def test_engine_constructs(self):
        e = PhaseEngine(PhaseParams(), hz=10.0)
        self.assertEqual((e.hz, e.dt), (10.0, 0.1))
        self.assertIs(e.params.golden, False)
        with self.assertRaises(ValueError):
            PhaseEngine(PhaseParams(), hz=0.0)

    def test_additive_api(self):
        """Names added to the section 6 interface; schedulers rely on them."""
        for name, params in {
            "new_episode": [],
            "set_workspace": ["hits_clamp"],
            "bump": ["name", "n"],
            "note": ["record"],
            "note_value": ["name", "value"],
        }.items():
            sig = inspect.signature(getattr(PhaseEngine, name))
            self.assertEqual([p for p in sig.parameters if p != "self"], params, name)
            self.assertTrue(inspect.getdoc(getattr(PhaseEngine, name)), name)
        self.assertIs(
            TickOut(np.zeros(7, np.float32), 0.0, 0.0, 0.0, 1.0, 1.0).reset, False
        )
        self.assertEqual(len(phase.BINDINGS), 15)

    def test_dataclass_fields(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(Measured)], ["pose", "aperture", "t"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(Inflight)],
            ["age_ticks", "d_hat_ticks", "done"],
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(Proposal)][:3],
            ["lam", "s_cand", "past_end"],
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(TickOut)][:11],
            [
                "target",
                "s",
                "u",
                "e",
                "lam",
                "cap",
                "hold",
                "blend_w",
                "dp",
                "dr",
                "grip_event",
            ],
        )
        self.assertTrue(issubclass(NonFiniteChunk, ValueError))

    def test_measured_coerces_to_float32(self):
        m = Measured(pose=np.arange(7, dtype=np.float64), aperture=None, t=1.0)
        self.assertEqual(m.pose.dtype, np.float32)
        self.assertEqual(m.pose.shape, (7,))
        with self.assertRaises(ValueError):
            Measured(pose=np.zeros(6), aperture=None, t=0.0)
        hash(m)  # identity hash: usable in sets despite the array field
        t = TickOut(
            target=np.zeros(7, np.float32), s=0.0, u=0.0, e=0.0, lam=1.0, cap=1.0
        )
        self.assertFalse(t.hold)
        self.assertEqual(t.blend_w, 1.0)
        self.assertIsNone(t.grip_event)

    def test_tick_is_begin_plus_commit(self):
        calls = []

        class Spy(PhaseEngine):
            def begin(self, m, inflight):
                calls.append(("begin", m, inflight))
                return Proposal(1.0, 1.0, False)

            def commit(self):
                calls.append(("commit",))
                return "out"

        m = Measured(pose=np.zeros(7), aperture=None, t=0.0)
        self.assertEqual(Spy(PhaseParams()).tick(m), "out")
        self.assertEqual([c[0] for c in calls], ["begin", "commit"])
        self.assertIsNone(calls[0][2])


class TestWrap(unittest.TestCase):
    def test_wrap_is_the_clients_expression(self):
        x = np.array([np.pi, -np.pi, 3.5, -3.5, 0.0, 7.0])
        np.testing.assert_array_equal(wrap_to_pi(x), (x + np.pi) % (2 * np.pi) - np.pi)
        self.assertEqual(float(wrap_to_pi(np.pi)), -np.pi)  # [-pi, pi)


# ============================================================================================================
# Engine behaviour (design section 7, WP1 tests 1-13). Pure numpy; a seeded master trajectory stands in for the
# policy, a lag plant (first order D_alpha + 20 ms dead time + a 2.65 Hz / zeta 0.17 mode) for the arm.
# ============================================================================================================
import collections
import math
import time

DT = 0.1
D_ALPHA = 0.27


def b_of(g):
    return 1 if g >= 0.5 else 0


def make_master(n=600, seed=0, step=0.006, turn=0.04, flips=(), wrap_rx=False):
    """A smooth (n, 7) float32 trajectory in policy rows: heading random walk, slow euler drift, persistent gripper
    flips [(row, value), ...]. wrap_rx puts rx next to +pi so the raw rows jump by ~2pi."""
    rng = np.random.default_rng(seed)
    head = np.cumsum(rng.normal(0.0, turn, n))
    elev = 0.3 * np.cumsum(rng.normal(0.0, turn / 2, n))
    sp = step * (1.0 + 0.4 * np.sin(np.arange(n) / 7.0))
    d = np.column_stack(
        [np.cos(head) * np.cos(elev), np.sin(head) * np.cos(elev), np.sin(elev)]
    )
    p = np.cumsum(d * sp[:, None], axis=0) + np.array([0.45, 0.0, 0.30])
    base = (np.pi - 0.02) if wrap_rx else 3.1
    e = np.column_stack(
        [
            base + np.cumsum(rng.normal(0.0, 0.004, n)),
            0.01 + np.cumsum(rng.normal(0.0, 0.003, n)),
            -0.8 + np.cumsum(rng.normal(0.0, 0.004, n)),
        ]
    )
    e = wrap_to_pi(e)
    g = np.ones(n)
    for row, val in flips:
        g[row:] = val
    return np.column_stack([p, e, g]).astype(np.float32)


def interp_rows(rows, phi):
    """The plan interpolation of section 3.1 written independently (float64 out)."""
    rows = np.asarray(rows, np.float64)
    h = len(rows)
    phi = min(max(phi, 0.0), h - 1.0)
    j = min(math.floor(phi + 1e-9), h - 2) if h > 1 else 0
    f = phi - j
    if h == 1 or abs(f) < 1e-9:
        return rows[min(j + (1 if abs(f - 1) < 1e-9 else 0), h - 1)].copy()
    out = rows[j].copy()
    out[:3] += f * (rows[j + 1, :3] - rows[j, :3])
    out[3:6] += f * wrap_to_pi(rows[j + 1, 3:6] - rows[j, 3:6])
    return out


def chunk_at(master, phi0, h=10, offset=None):
    """The policy's chunk for an observation at master phase phi0: rows j = M(phi0 + j) (+ a position offset)."""
    out = np.array([interp_rows(master, phi0 + j) for j in range(h)])
    for j in range(h):  # the gripper is the floor row's, never interpolated
        out[j, 6] = master[min(math.floor(phi0 + j + 1e-9), len(master) - 1), 6]
    if offset is not None:
        out[:, : len(offset)] += offset
    return out.astype(np.float32)


class PerfectArm:
    """The measured pose is the last target (no lag): isolates the engine's own arithmetic."""

    def __init__(self, pose):
        self.p = np.asarray(pose, np.float32).copy()

    def pose(self):
        return self.p.copy()

    def step(self, target):
        self.p = np.asarray(target, np.float32).copy()
        self.p[6] = float(b_of(target[6]))


class LagArm:
    """First-order lag D_alpha + 20 ms dead time + a 2.65 Hz / zeta 0.17 mode excited by the acceleration."""

    def __init__(
        self,
        pose,
        tau=D_ALPHA,
        dead_steps=2,
        f=2.65,
        zeta=0.17,
        gain=0.5,
        sticky_gripper=None,
    ):
        self.y = np.asarray(pose, np.float64)[:6].copy()
        self.vy = np.zeros(3)
        self.z = np.zeros(3)
        self.zd = np.zeros(3)
        self.buf = collections.deque([self.y.copy()] * dead_steps)
        self.tau, self.w, self.zeta, self.gain = tau, 2 * np.pi * f, zeta, gain
        self.g = float(pose[6])
        self.stuck = False
        self.sticky = sticky_gripper  # ticks the issued gripper value lags the command (the 0.75 s throttle)
        self._pending = collections.deque()

    def pose(self):
        out = np.empty(7)
        out[:6] = self.y
        out[:3] += self.z
        out[6] = self.g
        return out.astype(np.float32)

    def step(self, target):
        T = np.asarray(target, np.float64)
        h = 0.01
        a = 1.0 - math.exp(-h / self.tau)
        for _ in range(10):
            self.buf.append(T[:6].copy())
            Td = self.buf.popleft()
            if self.stuck:
                Td = self.y.copy()
            d = Td - self.y
            d[3:] = wrap_to_pi(d[3:])
            ynew = self.y + a * d
            vnew = (ynew[:3] - self.y[:3]) / h
            acc = (vnew - self.vy) / h
            self.y, self.vy = ynew, vnew
            self.zd += h * (
                self.gain * acc - 2 * self.zeta * self.w * self.zd - self.w**2 * self.z
            )
            self.z += h * self.zd
        want = float(b_of(T[6]))
        if self.sticky:
            self._pending.append(want)
            if len(self._pending) > self.sticky:
                self.g = self._pending.popleft()
        else:
            self.g = want


def run_open(
    engine, arm, chunk, max_ticks=80, aperture=0.0, t0=0.0, stop_on_past_end=True
):
    """Load a chunk synchronously and tick until the chunk is used up. Returns [(u_before, TickOut, Measured)]."""
    engine.set_chunk(chunk, "sync")
    log = []
    for k in range(max_ticks):
        m = Measured(arm.pose(), aperture, t0 + k * DT)
        u_before = engine.u
        prop = engine.begin(m, None)
        if prop.past_end and stop_on_past_end:
            break
        out = engine.commit()
        arm.step(out.target)
        engine.executed(out.target)
        log.append((u_before, out, m))
        if out.reset:
            break
    return log


def expected_changes(chunk, g_init):
    bits = [b_of(g) for g in chunk[:, 6]]
    G = [j for j in range(1, len(bits)) if bits[j] != bits[j - 1]]
    if bits[0] != g_init:
        G.insert(0, 0)
    return G


class TestGoldenCore(unittest.TestCase):
    """Test 1: golden mode reproduces today's row for row, including euler angles next to +-pi (I2)."""

    def _golden(self):
        return PhaseEngine(PhaseParams.golden_params())

    def test_sync_rows_are_bit_identical(self):
        for seed, wrap in ((1, False), (2, True), (3, True)):
            master = make_master(40, seed=seed, wrap_rx=wrap)
            chunk = master[:10].copy()
            if wrap:
                chunk[4, 3] = np.float32(
                    -np.pi + 0.01
                )  # the raw rows really cross +-pi
                chunk[5, 3] = np.float32(np.pi - 0.01)
            eng = self._golden()
            arm = PerfectArm(chunk[0])
            log = run_open(eng, arm, chunk, max_ticks=10)
            self.assertEqual(len(log), 10)
            for i, (_, out, _) in enumerate(log):
                self.assertTrue(np.array_equal(out.target, chunk[i]), f"row {i}")
                self.assertEqual(out.target.dtype, np.float32)
                self.assertEqual(out.lam, 1.0)
                self.assertFalse(out.hold)
                self.assertEqual(out.u, float(i))

    def test_golden_ignores_the_measured_pose(self):
        """No leash in golden mode: today's client does not look at the pose either."""
        chunk = make_master(20, seed=4)[:10]
        eng = self._golden()
        eng.set_chunk(chunk, "sync")
        for i in range(10):
            far = np.full(7, 5.0, np.float32)
            out = eng.tick(Measured(far, None, i * DT))
            self.assertTrue(np.array_equal(out.target, chunk[i]))
            self.assertFalse(out.hold)

    def test_swap_origin_is_the_observation(self):
        """RTC: a reply for the observation at phase s_obs plays row (tick - obs_tick), like rtcc._set_chunk."""
        master = make_master(80, seed=5)
        a = master[:10].copy()
        eng = self._golden()
        eng.set_chunk(a, "sync")
        s_obs = None
        outs = []
        for k in range(12):
            m = Measured(a[0], None, k * DT)
            if k == 6:
                new = chunk_at(master, 40.0) + 0.01  # an unrelated chunk
                new = new.astype(np.float32)
                info = eng.swap(new, s_obs)
                self.assertEqual(info["d_rows"], 3.0)
            out = eng.tick(m)
            if k == 3:
                s_obs = eng.s
            outs.append(out.target)
        for k in range(6, 12):
            self.assertTrue(np.array_equal(outs[k], new[k - 3]), f"tick {k}")
        self.assertEqual(outs[5].tolist(), a[5].tolist())

    def test_past_end_and_append(self):
        """A chunk of H rows is used up after H ticks; VLASH appends the next chunk at origin + H."""
        master = make_master(60, seed=6)
        a, c = master[:10].copy(), master[10:20].copy()
        eng = self._golden()
        eng.set_chunk(a, "sync")
        m = Measured(a[0], None, 0.0)
        outs = []
        for k in range(10):
            outs.append(eng.tick(Measured(a[0], None, k * DT)).target)
        prop = eng.begin(Measured(a[0], None, 1.0), None)
        self.assertTrue(prop.past_end)
        eng.append(c)
        out = eng.tick(Measured(a[0], None, 1.0))
        self.assertTrue(np.array_equal(out.target, c[0]))
        self.assertEqual(out.u, 10.0)
        del m

    def test_plain_golden_returns_h_and_the_chunk(self):
        master = make_master(60, seed=7)
        chunk = master[:10].copy()
        eng = self._golden()
        m0 = Measured(chunk[0], None, 0.0)
        self.assertEqual(eng.splice_plain(chunk, m0, 6), 6)
        self.assertTrue(np.array_equal(eng.preview(6), chunk[:6]))
        self.assertEqual(eng.u, -1.0)
        for i in range(6):
            out = eng.tick(Measured(chunk[0], None, i * DT))
            self.assertTrue(np.array_equal(out.target, chunk[i]))
        self.assertTrue(
            eng.begin(Measured(chunk[0], None, 1.0), None).past_end
        )  # plain's plan ends at row h - 1: its loop re-queries after h ticks

    def test_golden_summary_counts_the_flips(self):
        master = make_master(40, seed=8, flips=[(4, 0.0), (8, 1.0)])
        eng = self._golden()
        arm = PerfectArm(master[0])
        run_open(eng, arm, master[:10], max_ticks=10)
        kinds = [e["kind"] for e in eng.summary()["grip_events"]]
        self.assertEqual(kinds, ["close", "open"])


class TestGripperExact(unittest.TestCase):
    """Test 2 (I3): 500 seeded random chunks, random lambda_max and caps. The gripper changes land exactly."""

    def test_500_random_chunks(self):
        rng = np.random.default_rng(20261101)
        n_landed = n_windows = 0
        for case in range(500):
            h = int(rng.integers(8, 15))
            step = float(rng.uniform(0.003, 0.012))
            n_flips = int(rng.integers(0, 4))
            rows = sorted(
                rng.choice(np.arange(1, h), size=min(n_flips, h - 1), replace=False)
            )
            flips, g = [], 1.0
            for r in rows:
                g = 1.0 - g
                flips.append((int(r), g))
            master = make_master(h + 4, seed=case, step=step, turn=0.2, flips=flips)
            g_init = int(rng.integers(0, 2))
            if g_init == 0 and not flips:
                master[:, 6] = 0.0
            chunk = master[:h].copy()
            lam_max = float(rng.uniform(1.0, 3.0))
            params = PhaseParams(
                lambda_max=lam_max,
                v_cap=float(rng.uniform(0.15, 0.40)),
                omega_cap=float(rng.uniform(0.3, 1.2)),
                a_lat=float(rng.uniform(0.2, 2.4)),
                w_pre=int(rng.choice([3, 4])),
                w_post=int(rng.choice([5, 6])),
            )
            pose0 = chunk[0].copy()
            pose0[6] = float(g_init)
            eng = PhaseEngine(params)
            log = run_open(eng, PerfectArm(pose0), chunk, max_ticks=80)
            G = expected_changes(chunk, g_init)
            # every change row is hit exactly once, at its exact row
            landed = {}
            for u_before, out, _ in log:
                for gi in G:
                    if abs(out.u - gi) < 1e-9:
                        landed.setdefault(gi, []).append(out)
                    self.assertFalse(
                        u_before + 1e-9 < gi < out.u - 1e-9,
                        f"case {case}: a tick jumped over change row {gi}",
                    )
            for gi in G:
                self.assertEqual(len(landed.get(gi, [])), 1, f"case {case}: row {gi}")
                out = landed[gi][0]
                self.assertTrue(
                    np.array_equal(out.target, chunk[gi]),
                    f"case {case}: row {gi} not exact",
                )
                self.assertIsNotNone(out.grip_event)
                self.assertEqual(
                    out.grip_event["kind"],
                    "close" if b_of(chunk[gi, 6]) == 0 else "open",
                )
                n_landed += 1
            self.assertEqual(
                sum(1 for _, o, _ in log if o.grip_event),
                len(G),
                f"case {case}: events",
            )
            # the window: lambda <= 1 for every tick whose move meets [G - w_pre, G + w_post)
            for u_before, out, _ in log:
                for gi in G:
                    if u_before < gi + params.w_post and out.u > gi - params.w_pre:
                        self.assertLessEqual(
                            out.lam, 1.0 + 1e-9, f"case {case}: window of {gi}"
                        )
                        n_windows += 1
            # the gripper channel is the floor row's raw value, never interpolated or blended
            for _, out, _ in log:
                self.assertEqual(out.target[6], chunk[math.floor(out.u + 1e-9), 6])
        self.assertGreater(n_landed, 300)
        self.assertGreater(n_windows, 300)

    def test_polarity_gate_only_for_closing(self):
        """1.0 = open, 0.0 = closed: the gate waits for 1 -> 0 and never for 0 -> 1."""
        for first, second, gated in ((1.0, 0.0, True), (0.0, 1.0, False)):
            master = make_master(20, seed=3, step=0.004, flips=[(5, second)])
            master[:5, 6] = first
            chunk = master[:10].copy()
            pose = chunk[0].copy()
            pose[6] = first
            eng = PhaseEngine(PhaseParams())
            arm = PerfectArm(pose)
            # the arm is held 30 mm away from every row: the gate (10 mm) can never be met
            eng.set_chunk(chunk, "sync")
            waits, ev = 0, None
            for k in range(14):
                x = arm.pose()
                x[2] += 0.03
                out = eng.tick(Measured(x, None, k * DT))
                arm.step(out.target)
                eng.executed(out.target)
                if out.binding == "gate":
                    waits += 1
                ev = out.grip_event or ev
            self.assertEqual(waits > 0, gated, f"{first}->{second}")
            self.assertIsNotNone(ev)
            self.assertEqual(ev["kind"], "close" if gated else "open")
            if gated:
                self.assertEqual(ev["gate_wait_ticks"], 5)
                self.assertTrue(ev["gate_timeout"])

    def test_row0_is_compared_with_the_commanded_value(self):
        master = make_master(20, seed=4)
        closed = master[:10].copy()
        closed[:, 6] = 0.0
        for g_init, expect_event in ((1, True), (0, False)):
            pose = closed[0].copy()
            pose[6] = float(g_init)
            eng = PhaseEngine(PhaseParams())
            log = run_open(eng, PerfectArm(pose), closed, max_ticks=6)
            events = [o.grip_event for _, o, _ in log if o.grip_event]
            self.assertEqual(bool(events), expect_event)
            if expect_event:
                self.assertEqual(events[0]["kind"], "close")
                self.assertEqual(log[0][1].u, 0.0)  # row 0 itself is the change row

    def test_no_change_means_no_event_and_no_window(self):
        master = make_master(30, seed=9, step=0.004)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        log = run_open(eng, PerfectArm(master[0]), master[:14], max_ticks=40)
        self.assertFalse(any(o.grip_event for _, o, _ in log))
        self.assertGreater(max(o.lam for _, o, _ in log), 1.5)


class TestCapsAndRates(unittest.TestCase):
    """Tests 3 and 4 (I8, I9): the caps, the floors, the rate limits, the braking pass."""

    def _run(self, params, seed, n=120, step=0.004, turn=0.15, flips=()):
        master = make_master(n + 20, seed=seed, step=step, turn=turn, flips=flips)
        eng = PhaseEngine(params)
        chunk = master[:n].copy()
        log = run_open(eng, PerfectArm(chunk[0]), chunk, max_ticks=n)
        return eng, master, chunk, log

    def test_speed_and_turn_caps_when_lambda_is_above_one(self):
        checked = 0
        for seed in range(6):
            for alpha in (0.005, 0.01):
                params = PhaseParams(lambda_max=3.0, alpha=alpha)
                d = params.derived()
                v_lim = min(d["v_cap"], d["v_lag"])
                w_lim = min(d["omega_cap"], d["omega_lag"])
                _eng, _, chunk, log = self._run(params, seed, step=0.004 + 0.002 * seed)
                prev = chunk[0].astype(np.float64)
                for _, out, _ in log:
                    t = out.target.astype(np.float64)
                    if out.lam > 1.0 + 1e-9 and out.blend_w == 1.0:
                        v = float(np.linalg.norm(t[:3] - prev[:3])) / DT
                        w = float(np.abs(wrap_to_pi(t[3:6] - prev[3:6])).max()) / DT
                        self.assertLessEqual(
                            v, v_lim * (1 + 1e-6), f"seed {seed} alpha {alpha}"
                        )
                        self.assertLessEqual(w, w_lim * (1 + 1e-6))
                        checked += 1
                    prev = t
        self.assertGreater(checked, 100)

    def test_floor_of_one_and_rates(self):
        params = PhaseParams(lambda_max=2.5)
        d = params.derived()
        for seed in range(6):
            _eng, _master, chunk, log = self._run(
                params, seed, step=0.003 + 0.002 * seed
            )
            lams = [1.0] + [o.lam for _, o, _ in log]
            for i, (u_before, out, _) in enumerate(log):
                lam, prev = lams[i + 1], lams[i]
                # I8: below one only for the leash, the governor, the gate, the settle, the landing and the end
                if lam < 1.0 - 1e-9:
                    self.assertTrue(
                        out.binding
                        in {
                            "landing",
                            "end",
                            "gate",
                            "post",
                            "soft_leash",
                            "gov",
                            "rate",
                        }
                        or prev < 1.0,
                        f"{out.binding} lam {lam}",
                    )
                # I9: rise <= rate_up per tick, jerk per tick^2, tangential acceleration
                rise = lam - prev
                self.assertLessEqual(rise, params.rate_up + 1e-9)
                if i >= 1:
                    self.assertLessEqual(
                        rise, max(lams[i] - lams[i - 1], 0.0) + params.jerk + 1e-9
                    )
                j = math.floor(u_before + 1e-9)
                if 0 <= j < len(chunk) - 1:
                    dp = float(
                        np.linalg.norm(
                            chunk[j + 1, :3].astype(np.float64) - chunk[j, :3]
                        )
                    )
                    if dp > 1e-9 and u_before >= 0:
                        self.assertLessEqual(
                            rise, params.a_tan_up * DT * DT / dp + 1e-9
                        )
                # soft causes fall by at most 0.5 per tick
                if out.binding in {"soft_leash", "gov"}:
                    self.assertGreaterEqual(lam, prev - 0.5 - 1e-9)
            self.assertLessEqual(max(lams), 2.5 + 1e-9)
        self.assertEqual(d["d_dn"], 0.5)

    def test_rise_from_one_to_the_cap_takes_three_ticks(self):
        _eng, _, _, log = self._run(
            PhaseParams(lambda_max=2.5), 1, step=0.003, turn=0.0
        )
        lams = [o.lam for _, o, _ in log]
        self.assertEqual(lams[0], 1.0)
        self.assertAlmostEqual(lams[1], 1.45, places=9)  # +jerk 0.45
        self.assertAlmostEqual(
            lams[2], 2.25, places=9
        )  # +rate_up 0.8 (jerk allows 0.9)
        self.assertAlmostEqual(lams[3], 2.5, places=9)  # capped by lambda_max

    def test_rise_with_the_spec_rates(self):
        """The 11-01 spec rates (0.6 per tick, jerk 0.3) still give 1.0, 1.3, 1.9, 2.5."""
        _eng, _, _, log = self._run(
            PhaseParams(lambda_max=2.5, rate_up=0.6, jerk=0.3, a_tan_up=1.0),
            1,
            step=0.003,
            turn=0.0,
        )
        lams = [o.lam for _, o, _ in log]
        self.assertEqual(lams[0], 1.0)
        self.assertAlmostEqual(lams[1], 1.3, places=9)
        self.assertAlmostEqual(lams[2], 1.9, places=9)
        self.assertAlmostEqual(lams[3], 2.5, places=9)

    def test_every_cap_can_bind(self):
        """A slow path binds v, a fast turn omega, a corner kappa, a gripper flip window."""
        base = {"lambda_max": 3.0}
        # v: 16 mm rows at v_cap 0.3 allow lambda 1.875
        master = make_master(40, seed=2, step=0.016, turn=0.0)
        eng = PhaseEngine(PhaseParams(**base, v_cap=0.3, alpha=0.01, a_lat=2.4))
        log = run_open(eng, PerfectArm(master[0]), master[:30], max_ticks=30)
        self.assertIn("v", {o.binding for _, o, _ in log})
        # omega: rotate 0.04 rad per row on a 3 mm path
        m2 = make_master(40, seed=2, step=0.003, turn=0.0)
        m2[:, 3] = 3.0 + 0.04 * np.arange(40)
        eng = PhaseEngine(PhaseParams(**base))
        log = run_open(eng, PerfectArm(m2[0]), m2[:30], max_ticks=30)
        self.assertIn("omega", {o.binding for _, o, _ in log})
        # kappa: a right-angle corner after 10 rows of 5 mm
        m3 = make_master(30, seed=2, step=0.005, turn=0.0)
        for j in range(20):
            m3[j, :3] = (
                [0.45 + 0.005 * j, 0.0, 0.3]
                if j < 10
                else [0.495, 0.005 * (j - 9), 0.3]
            )
        m3 = m3[:20].copy()
        # the 11-01 spec values: the 11-02 defaults (a_lat 2.4, 50 mm arc) take this small corner at speed
        eng = PhaseEngine(PhaseParams(**base, a_lat=0.6, curv_len=0.015))
        log = run_open(eng, PerfectArm(m3[0]), m3, max_ticks=30)
        self.assertIn("kappa", {o.binding for _, o, _ in log})
        # window
        m4 = make_master(60, seed=2, step=0.003, turn=0.0, flips=[(14, 0.0)])
        eng = PhaseEngine(PhaseParams(**base))
        log = run_open(eng, PerfectArm(m4[0]), m4[:24], max_ticks=30)
        self.assertIn("window", {o.binding for _, o, _ in log})

    def test_braking_pass_enters_the_window_at_one(self):
        """Test 4: from lambda 2.5 into a window: lambda <= 1 at G - W_pre, each tick's fall within the braking law
        lam_prev^2 - lam^2 <= 2 * d_dn per row crossed (d(lam^2)/du = 2 d(lam)/dt, about d_dn per tick)."""
        G = 18
        master = make_master(60, seed=3, step=0.003, turn=0.0, flips=[(G, 0.0)])
        params = PhaseParams(lambda_max=2.5)
        d_dn = params.derived()["d_dn"]
        eng = PhaseEngine(params)
        log = run_open(eng, PerfectArm(master[0]), master[:24], max_ticks=40)
        peak = max(o.lam for _, o, _ in log)
        self.assertAlmostEqual(peak, 2.5, places=9)  # it did get to 2.5 before braking
        ws = G - params.w_pre
        lams = [1.0] + [o.lam for _, o, _ in log]
        for i, (u_before, out, _) in enumerate(log):
            if u_before < G + params.w_post and out.u > ws:
                self.assertLessEqual(out.lam, 1.0 + 1e-9)  # at G - W_pre and inside
            prev, lam = lams[i], lams[i + 1]
            if prev > lam and out.binding in {"window", "rate"}:
                # d(lam^2)/du = 2 d_dn per row, and one tick spans at most ceil(lam) row boundaries
                self.assertLessEqual(
                    prev * prev - lam * lam,
                    2 * d_dn * math.ceil(prev) + 1e-9,
                    f"tick {i}",
                )
        entering = [o for u, o, _ in log if u < ws <= o.u + 1e-9]
        self.assertTrue(entering)
        self.assertLessEqual(entering[0].lam, 1.0 + 1e-9)


def accel_vertex_curvatures(points):
    """Per interior vertex: the curvature accel.py's _path computed, recovered from the sqrt(a_lat / kappa) it takes
    (read-only: only the module's `math` is swapped for a recording proxy for the duration of one call)."""
    import accel

    P = np.asarray(points, np.float64)
    seg = np.column_stack([P, np.full((len(P), 3), 3.0), np.ones(len(P))]).astype(
        np.float32
    )
    seg[:, :3] = P
    planner = accel.ChunkAccel(horizon=len(P))
    real_math, events = accel.math, []

    class Spy:
        def __getattr__(self, name):
            return getattr(real_math, name)

        @staticmethod
        def acos(x):
            events.append("acos")
            return real_math.acos(x)

        @staticmethod
        def sqrt(x):
            events.append(("sqrt", x))
            return real_math.sqrt(x)

    accel.math = Spy()
    try:
        planner._path(seg, planner.gap, 0)
    finally:
        accel.math = real_math
    out, i = [], 0
    while len(out) < len(P) - 2 and i < len(
        events
    ):  # the passes after the curvature loop are not curvature
        if events[i] == "acos":
            if i + 1 < len(events) and isinstance(events[i + 1], tuple):
                out.append(planner.a_lat / events[i + 1][1])
                i += 1
            else:
                out.append(0.0)
        i += 1
    return np.array(out)


class TestCurvature(unittest.TestCase):
    """Test 5: the vertex curvature is accel.py's estimator (read-only import)."""

    def test_matches_accel(self):
        import accel

        # same estimator as accel.py at the same arc; the v3 default arc is longer (E13, 50 mm against 15 mm)
        self.assertEqual(accel.CURV_LEN, 0.015)
        self.assertEqual(PhaseParams().curv_len, 0.05)
        for seed in range(8):
            P = make_master(24, seed=seed, step=0.004, turn=0.25)[:12, :3].astype(
                np.float64
            )
            mine = phase.vertex_curvature(P, accel.CURV_LEN, 0.010)
            theirs = accel_vertex_curvatures(P)
            self.assertEqual(len(theirs), 10)
            np.testing.assert_allclose(
                mine[1:-1], theirs, rtol=1e-9, atol=1e-9, err_msg=f"seed {seed}"
            )
            self.assertGreater(float(theirs.max()), 5.0)  # the samples really curve

    def test_straight_pause_and_short_arc(self):
        straight = np.column_stack([np.arange(8) * 0.005, np.zeros(8), np.zeros(8)])
        np.testing.assert_array_equal(phase.vertex_curvature(straight), np.zeros(8))
        still = np.zeros((6, 3))
        np.testing.assert_array_equal(phase.vertex_curvature(still), np.zeros(6))
        # a 90 degree corner between 1 mm steps: the arc is under 10 mm, no curvature is reported
        tiny = np.array([[0, 0, 0], [0.001, 0, 0], [0.001, 0.001, 0]], float)
        self.assertEqual(float(phase.vertex_curvature(tiny, 0.015, 0.010).max()), 0.0)
        corner = np.array([[0, 0, 0], [0.01, 0, 0], [0.01, 0.01, 0]], float)
        self.assertGreater(
            float(phase.vertex_curvature(corner, 0.015, 0.010).max()), 10.0
        )


def stuck_run(params, n_ticks, stick_at, master):
    eng = PhaseEngine(params)
    chunk = master[:30].copy()
    arm = LagArm(chunk[0])
    eng.set_chunk(chunk, "sync")
    outs = []
    for k in range(n_ticks):
        if k == stick_at:
            arm.stuck = True
        m = Measured(arm.pose(), 0.0, k * DT)
        if not eng.has_rows():
            break
        eng.begin(m, None)
        out = eng.commit()
        arm.step(out.target)
        eng.executed(out.target)
        outs.append((m, out))
    return eng, outs


class TestLeash(unittest.TestCase):
    """Test 6 (I4): against a lagging plant every non-hold tick is inside 0.08 m / 0.28 rad of the measured pose."""

    def _drive(self, tau, lam_max, seed, ticks=160, alpha=0.005):
        master = make_master(ticks * 3, seed=seed, step=0.012, turn=0.03)
        params = PhaseParams(lambda_max=lam_max, alpha=alpha)
        eng = PhaseEngine(params)
        arm = LagArm(master[0], tau=tau)
        outs = []
        eng.set_chunk(chunk_at(master, 1.0, 12), "sync")
        pending = None
        for k in range(ticks):
            m = Measured(arm.pose(), 0.0, k * DT)
            if pending is not None and pending[0] <= k:
                eng.swap(pending[1], pending[2])
                pending = None
            prop = eng.begin(m, None)
            if prop.past_end:
                eng.set_chunk(chunk_at(master, eng.s + 1.0, 12), "sync")
                prop = eng.begin(m, None)
            out = eng.commit()
            arm.step(out.target)
            eng.executed(out.target)
            outs.append((m, out))
            if pending is None and eng.u >= 5.0:
                pending = (k + 2, chunk_at(master, eng.s, 12), eng.s)
        return eng, outs

    def test_inside_the_leash_and_it_makes_progress(self):
        for tau, lam_max in ((0.27, 2.5), (0.27, 3.0), (0.45, 2.5)):
            for seed in (0, 1):
                eng, outs = self._drive(tau, lam_max, seed)
                for m, out in outs:
                    t = out.target.astype(np.float64)
                    if out.hold:
                        continue
                    dp = float(np.linalg.norm(t[:3] - m.pose[:3]))
                    dr = float(np.abs(wrap_to_pi(t[3:6] - m.pose[3:6])).max())
                    self.assertLessEqual(dp, 0.08 + 1e-12, f"tau {tau} lam {lam_max}")
                    self.assertLessEqual(dr, 0.28 + 1e-12)
                    self.assertLess(dp, 0.10)  # never the client's own refusal
                s = eng.summary()
                self.assertLessEqual(s["dp_max_m"], 0.08 + 1e-9)
                self.assertGreater(
                    s["rows_advanced"], 160 * 1.0, f"tau {tau}: progress"
                )
                if tau == 0.27:
                    self.assertLessEqual(
                        s["leash_holds"], 3, "the lag model is right for tau = D_alpha"
                    )
                    self.assertGreater(s["lambda_eff"], 1.25)

    def test_slower_arm_is_slowed_not_refused(self):
        _, fast = self._drive(0.27, 2.5, 0)
        eng, slow = self._drive(0.50, 2.5, 0)
        lam_fast = np.mean([o.lam for _, o in fast])
        lam_slow = np.mean([o.lam for _, o in slow])
        self.assertLess(lam_slow, lam_fast)
        self.assertGreater(
            eng.summary()["d_hat_s"], 0.27
        )  # D_meas raised the lag estimate

    def test_stuck_arm_holds_then_resets_after_three(self):
        master = make_master(120, seed=11, step=0.012, turn=0.0)
        eng, outs = stuck_run(PhaseParams(lambda_max=2.5), 80, 6, master)
        resets = [i for i, (_, o) in enumerate(outs) if o.reset]
        self.assertEqual(len(resets), 1)
        r = resets[0]
        run = outs[r - 2 : r + 1]
        self.assertTrue(
            all(o.hold for _, o in run), "three holds in a row end in the reset"
        )
        for (_, a), (_, b) in itertools.pairwise(run):
            self.assertTrue(
                np.array_equal(a.target, b.target), "a hold re-sends the same target"
            )
        self.assertFalse(run[0][1].reset or run[1][1].reset)
        self.assertFalse(eng.has_rows())
        for _, o in outs:  # a creep at the leash edge is a hold, not progress
            self.assertTrue(
                o.hold
                or o.lam >= 0.02 - 1e-12
                or o.binding in {"landing", "gate", "end"},
                o.lam,
            )
        s = eng.summary()
        self.assertEqual(s["hold_resets"], 1)
        self.assertGreaterEqual(s["leash_holds"], 3)
        # every tick that moved stayed inside the leash of ITS measurement, however the stuck arm rang
        for m, o in outs:
            if not o.hold:
                self.assertLessEqual(
                    float(np.linalg.norm(o.target[:3].astype(np.float64) - m.pose[:3])),
                    0.08 + 1e-9,
                )

    def test_no_plan_tick_holds_the_last_target(self):
        master = make_master(120, seed=11, step=0.012, turn=0.0)
        eng, outs = stuck_run(PhaseParams(lambda_max=2.5), 80, 6, master)
        self.assertFalse(eng.has_rows())
        m = Measured(outs[-1][0].pose, None, outs[-1][0].t + DT)
        out = eng.tick(m)
        self.assertTrue(out.hold)
        self.assertTrue(np.array_equal(out.target, outs[-1][1].target))
        self.assertEqual(eng.summary()["noplan_holds"], 1)
        # a new chunk and a free arm: normal service resumes, blended from the last target
        eng.set_chunk(master[40:60], "sync")
        arm = LagArm(master[40])
        out = eng.tick(Measured(arm.pose(), None, 100.0))
        self.assertFalse(out.hold)

    def test_stale_target_is_reanchored_to_the_measured_pose(self):
        """After a reset the last target may be far from where the arm is (stuck, or the environment reset)."""
        master = make_master(120, seed=11, step=0.012, turn=0.0)
        eng, outs = stuck_run(PhaseParams(lambda_max=2.5), 80, 6, master)
        far = outs[-1][0].pose.copy()
        far[0] += 0.5
        eng.set_chunk(master[40:60], "sync")
        out = eng.tick(Measured(far, None, 100.0))
        self.assertFalse(out.hold)
        self.assertLessEqual(out.dp, 0.08)
        self.assertEqual(eng.summary()["reanchored_to_measured"], 1)


class HeldArm:
    """An arm somebody holds at a pose: it reports that pose whatever the target says (pushed away, blocked)."""

    def __init__(self, pose):
        self.p = np.asarray(pose, np.float32).copy()

    def pose(self):
        return self.p.copy()


class TestLeashRecovery(unittest.TestCase):
    """A last target that the arm has left behind (pushed 9 cm away) is walked back by at most one step limit per tick
    (I5) until the normal bisection fits again; it must not hold the arm 9 cm off for ever (review finding)."""

    def _pushed(self, params, push, ticks=14, plain=False, rot=None):
        master = make_master(200, seed=4, step=0.006, turn=0.0)
        eng = PhaseEngine(params)
        arm = PerfectArm(master[0])
        eng.set_chunk(master[:40].copy(), "sync")
        for k in range(5):  # a few normal ticks first
            out = eng.tick(Measured(arm.pose(), 0.0, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        held = HeldArm(arm.pose())
        held.p[:3] += np.asarray(push, np.float32)
        if rot is not None:
            held.p[3:6] += np.asarray(rot, np.float32)
        outs = []
        for k in range(5, 5 + ticks):
            if not eng.has_rows():
                # a fresh chunk that starts where the arm is (the policy answers from the measured pose)
                fresh = master[60:100].copy()
                fresh[:, :3] += held.p[:3] - fresh[0, :3]
                fresh[:, 3:6] += held.p[3:6] - fresh[0, 3:6]
                eng.set_chunk(fresh, "sync")
            m = Measured(held.pose(), 0.0, k * DT)
            out = eng.tick(m)
            eng.executed(out.target)
            outs.append((m, out))
        return eng, outs

    def test_arm_pushed_9_cm_recovers_within_3_plus_2_ticks_and_the_phase_advances_again(
        self,
    ):
        for lam in (1.5, 2.5):
            eng, outs = self._pushed(PhaseParams(lambda_max=lam), [0.0, 0.09, 0.0])
            dps = [
                float(np.linalg.norm(o.target[:3].astype(np.float64) - m.pose[:3]))
                for m, o in outs
            ]
            self.assertGreater(dps[0] + 1e-9, 0.0)
            limit = 3 + math.ceil(0.09 / 0.05)
            self.assertLessEqual(dps[limit - 1], 0.08 + 1e-9, f"lam {lam}: {dps}")
            self.assertGreater(
                eng.s, outs[0][1].s + 3, f"lam {lam}: the phase did not advance again"
            )
            self.assertTrue(any(not o.hold for _, o in outs[limit - 1 :]), f"lam {lam}")
            # I5 on every tick, the recovery ticks included; the client's 0.10 m never reached
            prev = None
            for (m, o), dp in zip(outs, dps, strict=True):
                t = o.target.astype(np.float64)
                if prev is not None:
                    self.assertLessEqual(float(np.linalg.norm(t[:3] - prev[:3])), 0.05)
                self.assertLessEqual(dp, 0.10)
                prev = t
            self.assertGreaterEqual(eng.summary()["leash_recoveries"], 1)

    def test_recovery_distance_falls_every_tick(self):
        _eng, outs = self._pushed(
            PhaseParams(lambda_max=2.0), [0.0, 0.09, 0.0], ticks=6
        )
        first = outs[0][1]
        self.assertTrue(
            first.hold
        )  # the recovery tick is a hold (lambda 0): the plan does not move
        self.assertEqual(first.lam, 0.0)
        dp0 = float(
            np.linalg.norm(first.target[:3].astype(np.float64) - outs[0][0].pose[:3])
        )
        self.assertAlmostEqual(dp0, 0.04, delta=1e-4)  # 0.09 - 0.05

    def test_rotation_leash_is_walked_back_by_the_rotation_step(self):
        eng, outs = self._pushed(
            PhaseParams(lambda_max=2.0), [0.0, 0.0, 0.0], rot=[0.0, 0.0, 0.30], ticks=8
        )
        self.assertGreater(eng.summary()["leash_recoveries"], 0)
        prev = None
        for m, o in outs:
            t = o.target.astype(np.float64)
            if prev is not None:
                self.assertLessEqual(
                    float(np.abs(wrap_to_pi(t[3:6] - prev[3:6])).max()), 0.12
                )
            prev = t
        final = outs[-1]
        self.assertLessEqual(
            float(
                np.abs(
                    wrap_to_pi(
                        final[1].target[3:6].astype(np.float64) - final[0].pose[3:6]
                    )
                ).max()
            ),
            0.28,
        )

    def test_far_displacement_reanchors_the_target_to_the_arm(self):
        """0.2 m off: one step would still leave a target the client rejects (and rejecting keeps the old one in
        force), so the target is re-anchored to the measured pose."""
        eng, outs = self._pushed(PhaseParams(lambda_max=2.0), [0.0, 0.20, 0.0], ticks=3)
        m, o = outs[0]
        np.testing.assert_array_equal(o.target[:6], m.pose[:6])
        self.assertEqual(o.dp, 0.0)
        self.assertEqual(eng.summary()["recovery_reanchors"], 1)

    def test_a_tick_without_a_plan_recovers_too(self):
        master = make_master(120, seed=11, step=0.012, turn=0.0)
        eng, outs = stuck_run(PhaseParams(lambda_max=2.5), 80, 6, master)
        self.assertFalse(eng.has_rows())
        before = eng.summary()
        pose = outs[-1][1].target.copy()
        pose[1] += 0.09  # the arm is 9 cm from the last target
        out = eng.tick(Measured(pose, None, outs[-1][0].t + DT))
        self.assertAlmostEqual(out.dp, 0.04, delta=1e-4)
        eng.executed(out.target)
        out2 = eng.tick(Measured(pose, None, outs[-1][0].t + 2 * DT))
        self.assertLess(out2.dp, out.dp + 1e-9)
        self.assertLessEqual(out2.dp, 0.08)
        after = eng.summary()
        self.assertEqual(after["noplan_holds"], before["noplan_holds"] + 2)
        self.assertEqual(after["leash_recoveries"], before["leash_recoveries"] + 1)

    def test_rejected_recovery_rolls_back_the_target(self):
        master = make_master(200, seed=4, step=0.006, turn=0.0)
        eng = PhaseEngine(PhaseParams(lambda_max=2.0))
        arm = PerfectArm(master[0])
        eng.set_chunk(master[:40].copy(), "sync")
        for k in range(4):
            out = eng.tick(Measured(arm.pose(), 0.0, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        last = out.target.copy()
        pose = arm.pose()
        pose[1] += 0.09
        out = eng.tick(Measured(pose, 0.0, 0.5))
        self.assertFalse(np.array_equal(out.target, last))
        eng.rejected()
        out = eng.tick(Measured(pose, 0.0, 0.6))
        self.assertAlmostEqual(
            float(np.linalg.norm(out.target[:3].astype(np.float64) - last[:3])),
            0.05,
            delta=1e-4,
        )


class TestSoftLeash(unittest.TestCase):
    """Section 3.4 step 3: lambda above 1 is cut by gamma = min(clip((0.08 - eps) / 0.03), clip((0.28 - rho) / 0.08)),
    eps = |x_k - T_{k-1}|; the cut falls by at most 0.5 per tick (a soft reason)."""

    def _ready(self, **kw):
        master = make_master(80, seed=3, step=0.004, turn=0.0)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5, **kw))
        arm = PerfectArm(master[0])
        eng.set_chunk(master[:60].copy(), "sync")
        for k in range(6):
            out = eng.tick(Measured(arm.pose(), None, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        self.assertAlmostEqual(out.lam, 2.5)
        return eng, arm, master

    def _look(self, eng, arm, behind=0.0, rot=0.0):
        x = arm.pose()
        x[0] -= behind  # the arm trails the last target
        x[3] -= rot
        return eng.begin(Measured(x, None, 0.6), None)

    def test_cut_is_proportional_and_rate_limited(self):
        eng, arm, _ = self._ready()
        self.assertAlmostEqual(
            self._look(eng, arm, 0.04).lam, 2.5
        )  # eps 40 mm: untouched
        mid = self._look(
            eng, arm, 0.065
        )  # gamma 0.5 would give 1.75; the fall is limited to 0.5 per tick
        self.assertAlmostEqual(mid.lam, 2.0)
        self.assertIn(mid.binding, {"soft_leash", "rate"})
        full = self._look(eng, arm, 0.079)  # gamma ~ 0.03
        self.assertAlmostEqual(full.lam, 2.0)

    def test_two_ticks_reach_the_cut(self):
        eng, arm, _ = self._ready()
        lams = []
        for k in range(3):
            x = arm.pose()
            x[0] -= 0.065  # gamma 1/2: the lambda target is 1 + 1.5 / 2 = 1.75
            out = eng.tick(Measured(x, None, 0.6 + k * DT))
            arm.step(out.target)
            eng.executed(out.target)
            lams.append(out.lam)
        self.assertAlmostEqual(lams[0], 2.0)  # 2.5 - 0.5
        self.assertAlmostEqual(lams[1], 1.75, places=4)
        self.assertAlmostEqual(lams[2], 1.75, places=4)

    def test_creep_at_the_leash_edge_is_a_hold(self):
        """Hard leash: with 0.05 mm of margin the feasible advance is under 0.02 rows (a hold: the last target is
        re-sent and counted); with 0.5 mm it is real progress."""
        for margin, hold in ((0.00005, True), (0.0005, False)):
            eng, arm, _ = self._ready()
            T = eng._T_prev.astype(np.float64)
            x = arm.pose()
            x[0] = np.float32(
                T[0] - (0.08 - margin)
            )  # behind the last target, along its direction of travel
            before = eng.s
            out = eng.tick(Measured(x, None, 0.6))
            self.assertEqual(out.hold, hold, f"margin {margin}")
            if hold:
                self.assertTrue(
                    np.array_equal(out.target, eng._T_prev) or out.lam == 0.0
                )
                self.assertEqual((eng.s, eng.summary()["leash_holds"]), (before, 1))
            else:
                self.assertGreaterEqual(out.lam, 0.02)
                self.assertEqual(out.binding, "hard_leash")

    def test_rotation_part_cuts_too_and_switch_off(self):
        eng, arm, _ = self._ready()
        self.assertAlmostEqual(self._look(eng, arm, 0.0, rot=0.19).lam, 2.5, places=4)
        self.assertLess(self._look(eng, arm, 0.0, rot=0.27).lam, 2.5)
        eng, arm, _ = self._ready(soft_leash=False)
        self.assertAlmostEqual(self._look(eng, arm, 0.065).lam, 2.5)


class TestStepAndBlend(unittest.TestCase):
    """Test 7 (I5, section 3.7): switch jumps become min-jerk offsets of 2 to 5 ticks; steps stay <= 50 mm / 0.12 rad."""

    K0 = 5  # the reply is swapped in before this tick; the observation was taken after tick 2

    def _swap_run(self, offset, params=None, lag=False, ticks=20):
        master = make_master(300, seed=21, step=0.005, turn=0.0)
        eng = PhaseEngine(params or PhaseParams(lambda_max=2.5))
        arm = LagArm(master[0]) if lag else PerfectArm(master[0])
        eng.set_chunk(chunk_at(master, 1.0, 40), "sync")
        outs, s_obs, new = [], None, None
        for k in range(ticks):
            m = Measured(arm.pose(), 0.0, k * DT)
            if k == self.K0:
                new = chunk_at(master, s_obs, 40, offset=offset)
                eng.swap(new, s_obs)
            prop = eng.begin(m, None)
            if prop.past_end:
                break
            out = eng.commit()
            arm.step(out.target)
            eng.executed(out.target)
            outs.append(out)
            if k == 2:
                s_obs = eng.s
        return eng, new, s_obs, outs

    def test_jumps_46_75_120_mm(self):
        # at 25 mm per tick (the default; section 5 allows 30): 46 mm -> 3.45 -> 4 ticks, 75 mm -> 5.6 -> 6 (past the five
        # ticks of section 5, G-F), 120 mm would need 9 and stops at the stretch cap of 8
        for jump, n_expected in ((0.046, 4), (0.075, 6), (0.120, 8)):
            offset = np.array([0.0, jump, 0.0])
            _eng, new, _s_obs, outs = self._swap_run(offset)
            after = outs[self.K0 :]
            n_blend = next(i for i, o in enumerate(after) if o.blend_w >= 1.0) + 1
            self.assertEqual(n_blend, n_expected, f"jump {jump}")
            prev = None
            for o in outs:
                t = o.target.astype(np.float64)
                if prev is not None:
                    self.assertLessEqual(
                        float(np.linalg.norm(t[:3] - prev[:3])),
                        0.05 + 1e-6,
                        f"jump {jump}",
                    )
                    self.assertLessEqual(
                        float(np.abs(wrap_to_pi(t[3:6] - prev[3:6])).max()), 0.12 + 1e-6
                    )
                prev = t
            ws = [0.0] + [o.blend_w for o in after[:n_blend]]
            inc = max(ws[i + 1] - ws[i] for i in range(len(ws) - 1)) * jump
            # the offset itself moves at most 25 mm per tick (30 mm at the stretch cap, where 120 mm needs a 9th tick)
            self.assertLessEqual(
                inc, (0.025 if jump <= 0.1 else 0.03) + 1e-9, f"jump {jump}"
            )
            # the blend is over: the target is on the new path (the offset has gone)
            done = after[n_blend - 1]
            want = interp_rows(new, done.e)
            if done.hold or done.binding != "step":
                np.testing.assert_allclose(
                    done.target[:6], want[:6], atol=3e-6, err_msg=f"jump {jump}"
                )
            self.assertEqual(done.blend_w, 1.0)
            self.assertTrue(all(not o.hold for o in outs))

    def test_rotation_jump(self):
        offset = np.array([0.0, 0.0, 0.0, 0.25, 0.0, 0.0])
        _eng, _new, _s_obs, outs = self._swap_run(offset)
        prev = None
        for o in outs:
            t = o.target.astype(np.float64)
            if prev is not None:
                self.assertLessEqual(
                    float(np.abs(wrap_to_pi(t[3:6] - prev[3:6])).max()), 0.12 + 1e-6
                )
            prev = t
        # 0.25 rad at 0.05 rad per tick is 9.4 ticks: the stretch cap of 8 (7 partial, the 8th done), past the five of section 5
        self.assertEqual(len([o for o in outs[self.K0 :] if o.blend_w < 1.0]), 7)

    def test_no_deadlock_with_a_lagging_arm(self):
        _eng, _, _, outs = self._swap_run(np.array([0.0, 0.1, 0.0]), lag=True, ticks=24)
        advancing = [o for o in outs if not o.hold]
        self.assertGreaterEqual(len(advancing), len(outs) - 2)
        self.assertGreater(outs[-1].s, outs[0].s + 10)

    def test_a_second_switch_inside_a_blend_restarts_from_the_last_target(self):
        master = make_master(300, seed=21, step=0.005, turn=0.0)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = PerfectArm(master[0])
        eng.set_chunk(chunk_at(master, 1.0, 40), "sync")
        prev = None
        for k in range(14):
            m = Measured(arm.pose(), 0.0, k * DT)
            if k in (4, 6):
                eng.swap(
                    chunk_at(
                        master,
                        eng.s - 1.0,
                        20,
                        offset=np.array([0.0, 0.05 * (k - 3), 0.0]),
                    ),
                    eng.s - 1.0,
                )
            prop = eng.begin(m, None)
            if prop.past_end:
                break
            out = eng.commit()
            arm.step(out.target)
            eng.executed(out.target)
            t = out.target.astype(np.float64)
            if prev is not None:
                self.assertLessEqual(
                    float(np.linalg.norm(t[:3] - prev[:3])), 0.05 + 1e-6, f"tick {k}"
                )
            prev = t
        self.assertGreaterEqual(eng.summary()["blends"], 2)

    def test_a_big_jump_is_stretched_to_keep_the_increment_and_the_cap_restores_five_ticks(
        self,
    ):
        """G-F: a 100 mm offset in five ticks would step 37.5 mm per tick (> 25); it takes 8 ticks instead. With
        blend_stretch_max_ticks = blend_max_ticks the section 5 clip at five ticks is back."""
        offset = np.array([0.0, 0.10, 0.0])
        eng, _new, _s_obs, outs = self._swap_run(offset)
        after = outs[self.K0 :]
        n_blend = next(i for i, o in enumerate(after) if o.blend_w >= 1.0) + 1
        self.assertEqual(n_blend, 8)
        ws = [0.0] + [o.blend_w for o in after[:n_blend]]
        inc = max(ws[i + 1] - ws[i] for i in range(len(ws) - 1)) * 0.10
        self.assertLessEqual(inc, 0.025 + 1e-9)
        self.assertEqual(eng.summary()["blends_stretched"], 1)
        eng5, _new, _s_obs, outs5 = self._swap_run(
            offset, params=PhaseParams(lambda_max=2.5, blend_stretch_max_ticks=5)
        )
        after5 = outs5[self.K0 :]
        self.assertEqual(
            next(i for i, o in enumerate(after5) if o.blend_w >= 1.0) + 1, 5
        )
        self.assertEqual(eng5.summary()["blends_stretched"], 0)
        # a jump past even eight ticks' worth stops at the cap and the step limit absorbs the rest (I5 holds)
        _eng8, _n, _s, outs8 = self._swap_run(np.array([0.0, 0.2, 0.0]), ticks=24)
        after8 = outs8[self.K0 :]
        self.assertEqual(
            next(i for i, o in enumerate(after8) if o.blend_w >= 1.0) + 1, 8
        )
        prev = None
        for o in outs8:
            t = o.target.astype(np.float64)
            if prev is not None:
                self.assertLessEqual(
                    float(np.linalg.norm(t[:3] - prev[:3])), 0.05 + 1e-6
                )
            prev = t

    def test_blend_off_and_golden_switch_hard(self):
        offset = np.array([0.0, 0.03, 0.0])
        eng, _, _, outs = self._swap_run(offset, params=PhaseParams.golden_params())
        self.assertTrue(all(o.blend_w == 1.0 for o in outs))
        self.assertEqual(eng.summary()["blends"], 0)
        eng, _new, _s_obs, outs = self._swap_run(
            offset, params=PhaseParams(lambda_max=2.0, blend=False)
        )
        self.assertTrue(all(o.blend_w == 1.0 for o in outs))


class TestLead(unittest.TestCase):
    """Test 8 (I1, I6, section 3.5): e leads u, never goes back, stays out of windows, past change rows and the end."""

    def _run(self, params, flips, seed=5, arm=None):
        master = make_master(80, seed=seed, step=0.004, turn=0.05, flips=flips)
        chunk = master[:40].copy()
        eng = PhaseEngine(params)
        log = run_open(eng, arm or PerfectArm(chunk[0]), chunk, max_ticks=60)
        return eng, chunk, log

    def test_lead_invariants(self):
        for alpha in (0.005, 0.01):
            params = PhaseParams(lambda_max=2.5, alpha=alpha, lead=True)
            G = [20, 30]
            _eng, _chunk, log = self._run(params, [(20, 0.0), (30, 1.0)])
            es = [o.e for _, o, _ in log]
            for i, (u_before, out, _) in enumerate(log):
                self.assertGreaterEqual(out.e + 1e-9, out.u)  # e >= u
                self.assertLessEqual(out.e, 39 + 1e-9)  # not past the end
                if i:
                    self.assertGreaterEqual(out.e + 1e-9, es[i - 1], "e is monotone")
                for g in G:
                    if g - params.w_pre <= out.u < g + params.w_post:
                        self.assertAlmostEqual(
                            out.e, out.u, places=9, msg="e == u inside a window"
                        )
                    if out.u < g - params.w_pre:
                        self.assertLessEqual(
                            out.e, g - params.w_pre + 1e-9, "e stays out of the window"
                        )
                    if out.u < g:
                        self.assertLess(out.e, g + 1e-9, "e does not pass a change row")
            self.assertGreater(
                max(o.e - o.u for _, o, _ in log), 0.5, "the lead did lead"
            )
            self.assertLessEqual(
                max(o.e - o.u for _, o, _ in log), params.lead_max_rows + 1e-9
            )

    def test_lead_never_passes_the_window_entrance(self):
        """Leftover lead (more than the braking removed) is cut back at the entrance of a window: e == u inside it."""
        params = PhaseParams(lambda_max=2.5, lead=True)
        master = make_master(80, seed=5, step=0.004, turn=0.0, flips=[(14, 0.0)])
        eng = PhaseEngine(params)
        arm = PerfectArm(master[0])
        eng.set_chunk(master[:40].copy(), "sync")
        ws = 14 - params.w_pre
        for k in range(40):
            if abs(eng.u - 9.0) < 1.0 and k > 0:
                eng._e_off = 3.0  # as if a soft cause had cut lambda faster than the lead could unwind
            out = eng.tick(Measured(arm.pose(), None, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
            if out.u < ws:
                self.assertLessEqual(out.e, ws + 1e-9, f"tick {k}")
            elif out.u < 14 + params.w_post:
                self.assertAlmostEqual(out.e, out.u, places=9, msg=f"tick {k}")
            if out.u >= 20:
                break
        else:
            self.fail("did not get through the window")

    def test_lead_speed_is_inside_the_caps(self):
        params = PhaseParams(lambda_max=3.0, lead=True)
        d = params.derived()
        _eng, chunk, log = self._run(params, [], seed=7)
        prev = chunk[0].astype(np.float64)
        for _, out, _ in log:
            t = out.target.astype(np.float64)
            if out.blend_w == 1.0:
                self.assertLessEqual(
                    float(np.linalg.norm(t[:3] - prev[:3])) / DT,
                    min(d["v_cap"], d["v_lag"]) * (1 + 1e-6),
                )
            prev = t

    def test_raw_views_ignore_the_lead(self):
        """I1: prev_rows / future_row use u and the policy's rows, not the issued phase."""
        params = PhaseParams(lambda_max=2.5, lead=True)
        master = make_master(80, seed=5, step=0.004, turn=0.05)
        chunk = master[:20].copy()
        eng = PhaseEngine(params)
        arm = PerfectArm(chunk[0])
        eng.set_chunk(chunk, "sync")
        for k in range(8):
            out = eng.tick(Measured(arm.pose(), 0.0, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        self.assertGreater(out.e, out.u)
        prev = eng.prev_rows()
        want = np.array(
            [interp_rows(chunk, eng.u + j) for j in range(len(prev))]
        ).astype(np.float32)
        np.testing.assert_allclose(prev, want, atol=2e-6)
        self.assertEqual(len(prev), math.floor(19 - eng.u + 1e-9) + 1)
        self.assertTrue(np.array_equal(eng.future_row(), chunk[-1]))

    def test_d_dn_is_the_braking_rate(self):
        self.assertAlmostEqual(PhaseParams(lead=True).derived()["d_dn"], 0.9 / 2.7)


class TestPrevRowsAndFuture(unittest.TestCase):
    def test_prev_rows_integer_u_is_the_chunk_tail(self):
        master = make_master(60, seed=2)
        chunk = master[:10].copy()
        eng = PhaseEngine(PhaseParams.golden_params())
        eng.set_chunk(chunk, "sync")
        for k in range(4):
            eng.tick(Measured(chunk[0], None, k * DT))
        self.assertEqual(eng.u, 3.0)
        prev = eng.prev_rows()
        self.assertTrue(np.array_equal(prev, chunk[3:]))
        self.assertEqual(prev.dtype, np.float32)
        self.assertTrue(np.array_equal(eng.future_row(), chunk[-1]))

    def test_prev_rows_fractional_u_is_on_the_policy_grid(self):
        master = make_master(80, seed=3, step=0.004)
        chunk = master[:10].copy()
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = PerfectArm(chunk[0])
        eng.set_chunk(chunk, "sync")
        for k in range(4):
            out = eng.tick(Measured(arm.pose(), None, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        u = eng.u
        self.assertNotEqual(u, math.floor(u))
        prev = eng.prev_rows()
        for j, row in enumerate(prev):
            np.testing.assert_allclose(
                row, interp_rows(chunk, u + j).astype(np.float32), atol=2e-6
            )
        self.assertEqual(len(prev), math.floor(9 - u) + 1)


class TestPlainSplice(unittest.TestCase):
    """Test 9 (section 3.7): the plain join projects the last target onto the new rows."""

    def _start(self, params, master, h=8):
        """Plain loop on master rows: chunk 0 at master phase 0 (global phase = master phase + 1), 4 ticks run."""
        eng = PhaseEngine(params)
        arm = PerfectArm(master[0])
        eng.splice_plain(chunk_at(master, 0.0, 12), Measured(arm.pose(), 0.0, 0.0), h)
        for k in range(1, 5):
            out = eng.tick(Measured(arm.pose(), 0.0, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        return eng, arm

    def _second(self, eng, arm, master, lag_rows, h=8, flips=None):
        phi_now = eng.s - 1.0  # master phase of the last issued target
        second = chunk_at(
            flips if flips is not None else master, phi_now - lag_rows, 12
        )
        n = eng.splice_plain(second, Measured(arm.pose(), 0.0, 0.5), h)
        return n, second

    def test_golden_starts_at_minus_one(self):
        master = make_master(120, seed=2, step=0.006, turn=0.0)
        eng, arm = self._start(PhaseParams.golden_params(), master)
        n, _ = self._second(eng, arm, master, 1.5)
        self.assertEqual(eng.u, -1.0)
        self.assertEqual(n, 8)

    def test_projection_up_to_four_rows(self):
        master = make_master(120, seed=2, step=0.006, turn=0.0)
        for lag_rows, want in ((1.5, 1.5), (3.0, 3.0), (6.0, 4.0)):
            eng, arm = self._start(PhaseParams(lambda_max=2.5), master)
            n, _ = self._second(eng, arm, master, lag_rows)
            self.assertAlmostEqual(eng.u, want, delta=0.06, msg=f"lag {lag_rows}")
            self.assertLessEqual(eng.u, 4.0 + 1e-9)
            self.assertGreaterEqual(n, 1)
        eng, arm = self._start(
            PhaseParams(lambda_max=2.5, plain_reanchor=False), master
        )
        self._second(eng, arm, master, 3.0)
        self.assertEqual(eng.u, -1.0)  # the switch is off: the golden start

    def test_never_past_the_row_before_a_gripper_change(self):
        master = make_master(120, seed=2, step=0.006, turn=0.0)
        probe, _ = self._start(PhaseParams(lambda_max=2.5), master)
        s_now = probe.s
        # the new chunk is 3.4 rows behind the last target and closes the gripper at its row 3 (G - 1 = 2 < 3.4)
        flipped = make_master(
            120, seed=2, step=0.006, turn=0.0, flips=[(int(s_now - 1.0 - 3.4) + 3, 0.0)]
        )
        eng, arm = self._start(PhaseParams(lambda_max=2.5), master)
        _, second = self._second(eng, arm, master, 3.4, flips=flipped)
        change = [
            j for j in range(1, 12) if b_of(second[j, 6]) != b_of(second[j - 1, 6])
        ]
        self.assertTrue(change)
        self.assertLessEqual(eng.u, change[0] - 1 + 1e-9)
        self.assertGreater(eng.u, 0.0)

    def test_opposite_direction_falls_back_to_the_virtual_row(self):
        master = make_master(120, seed=2, step=0.006, turn=0.0)
        eng, arm = self._start(PhaseParams(lambda_max=2.5), master)
        reversed_chunk = chunk_at(master, eng.s - 1.0 - 2.0, 12)[
            ::-1
        ].copy()  # walks back along the path
        eng.splice_plain(reversed_chunk, Measured(arm.pose(), 0.0, 0.6), 8)
        self.assertEqual(eng.u, -1.0)

    def test_far_chunk_is_joined_through_the_virtual_row_and_step_limited(self):
        master = make_master(120, seed=2, step=0.006, turn=0.0)
        eng, arm = self._start(PhaseParams(lambda_max=2.5), master)
        n = eng.splice_plain(
            chunk_at(master, 60.0, 12), Measured(arm.pose(), 0.0, 0.6), 8
        )
        self.assertEqual(eng.u, -1.0)
        self.assertGreaterEqual(n, 1)
        prev = eng.tick(Measured(arm.pose(), 0.0, 0.6)).target.astype(np.float64)
        for k in range(8):
            out = eng.tick(Measured(arm.pose(), 0.0, 0.7 + k * DT))
            arm.step(out.target)
            eng.executed(out.target)
            t = out.target.astype(np.float64)
            self.assertLessEqual(float(np.linalg.norm(t[:3] - prev[:3])), 0.05 + 1e-6)
            self.assertLessEqual(out.dp, 0.08 + 1e-9)
            prev = t

    def test_first_query_of_an_episode_starts_from_the_measured_pose(self):
        master = make_master(120, seed=2, step=0.006, turn=0.0)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        pose = master[0].copy()
        pose[:3] += np.array(
            [-0.04, 0.03, 0.0]
        )  # the arm is 50 mm behind and beside row 0
        eng.splice_plain(chunk_at(master, 0.0, 12), Measured(pose, 0.0, 0.0), 8)
        out = eng.tick(Measured(pose, 0.0, 0.0))
        t = out.target.astype(np.float64)
        self.assertLessEqual(
            float(np.linalg.norm(t[:3] - pose[:3])), 0.05 + 1e-6
        )  # step-limited, not teleported
        # and it is on the way from the measured pose to row 0
        v, w = master[0, :3].astype(np.float64) - pose[:3], t[:3] - pose[:3]
        self.assertGreater(
            float(v @ w) / (np.linalg.norm(v) * np.linalg.norm(w)), 0.999
        )

    def test_tick_prediction_is_not_beaten_by_the_loop(self):
        for flips in ((), ((6, 0.0),)):
            master = make_master(200, seed=4, step=0.005, turn=0.1, flips=list(flips))
            for params in (
                PhaseParams(lambda_max=2.5),
                PhaseParams(lambda_max=2.0, alpha=0.01),
            ):
                eng = PhaseEngine(params)
                arm = LagArm(master[0])
                m_ticks = eng.splice_plain(
                    chunk_at(master, 0.0, 10), Measured(arm.pose(), 0.0, 0.0), 8
                )
                reached = None
                for k in range(60):
                    out = eng.tick(Measured(arm.pose(), 0.0, k * DT))
                    arm.step(out.target)
                    eng.executed(out.target)
                    if out.u >= 7 - 1e-9 and reached is None:
                        reached = k + 1
                self.assertIsNotNone(reached)
                self.assertGreaterEqual(
                    reached,
                    m_ticks,
                    "the closed loop is never faster than the prediction",
                )
                if not flips:
                    self.assertLess(
                        m_ticks, 8
                    )  # and without a gripper window it saves ticks over the golden 8


class TestSwapReanchor(unittest.TestCase):
    """Test 10 (section 3.7): forward only, <= 3 rows, never past G - 1; recorded even with the feature off."""

    def _swap(self, lag_rows, reanchor, new_flips=()):
        master = make_master(300, seed=31, step=0.006, turn=0.0)
        master_new = make_master(
            300, seed=31, step=0.006, turn=0.0, flips=list(new_flips)
        )
        eng = PhaseEngine(PhaseParams(lambda_max=2.5, swap_reanchor=reanchor))
        arm = PerfectArm(master[0])
        eng.set_chunk(chunk_at(master, 1.0, 40), "sync")
        s_obs = None
        for k in range(5):
            out = eng.tick(Measured(arm.pose(), 0.0, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
            if k == 2:
                s_obs = eng.s
        # an anchor on the lagging state: row 0 of the new chunk is lag_rows behind the master phase of the observation
        new = chunk_at(master_new, s_obs - lag_rows, 12)
        info = eng.swap(new, s_obs)
        return eng, info, s_obs, new

    def test_anchor_rows_recorded_and_applied_forward_only(self):
        eng, info, s_obs, _ = self._swap(2.0, True)
        self.assertAlmostEqual(info["anchor_rows"], 2.0, delta=0.05)
        self.assertEqual(eng.summary()["reanchors"], 1)
        # reanchored: the new chunk plays 2 rows earlier than its origin says
        self.assertAlmostEqual(eng.s - eng._o, (eng.s - s_obs) + 2.0, delta=0.06)

    def test_feature_off_still_records(self):
        eng, info, s_obs, _ = self._swap(2.0, False)
        self.assertAlmostEqual(info["anchor_rows"], 2.0, delta=0.05)
        self.assertEqual(eng.summary()["reanchors"], 0)
        self.assertEqual(len(eng.summary()["swap_anchor_rows"]), 1)
        self.assertAlmostEqual(eng.s - eng._o, eng.s - s_obs, places=9)

    def test_at_most_three_rows(self):
        eng, info, s_obs, _ = self._swap(4.0, True)
        self.assertGreater(
            info["anchor_rows"], 3.0
        )  # found beyond the 3 rows that may be applied
        self.assertAlmostEqual(eng.s - eng._o, (eng.s - s_obs) + 3.0, delta=0.06)

    def test_backwards_is_recorded_but_never_applied(self):
        eng, info, s_obs, _ = self._swap(-1.5, True)
        self.assertLessEqual(info["anchor_rows"], 1e-9)
        self.assertEqual(eng.summary()["reanchors"], 0)
        self.assertAlmostEqual(eng.s - eng._o, eng.s - s_obs, places=9)

    def test_not_past_the_row_before_a_change(self):
        # the new chunk (3 rows behind) closes the gripper two rows after the swap tick's phase: the reanchor may
        # skip only up to the row before it
        _, info0, s_obs, _ = self._swap(3.0, False)
        u_new = info0["d_rows"]
        change_row = math.floor(u_new) + 2
        flips = [(int(s_obs - 3.0) + change_row, 0.0)]
        eng, info, _, new = self._swap(3.0, True, new_flips=flips)
        self.assertEqual(
            info["d_rows"], u_new
        )  # the old plan was not touched by the new chunk's gripper
        change = [j for j in range(1, 12) if b_of(new[j, 6]) != b_of(new[j - 1, 6])]
        self.assertTrue(change)
        self.assertGreater(
            change[0], u_new
        )  # the change is still ahead of the swap tick
        self.assertGreater(
            info["anchor_rows"], 1.5
        )  # the unbounded skip would have gone 3 rows
        skip = s_obs - eng._o  # rows the swapped chunk was moved forward by
        self.assertLessEqual(u_new + skip, change[0] - 1 + 1e-9)
        self.assertLess(skip, 2.0)
        self.assertEqual(eng.summary()["swap_anchor_rows"][0], info["anchor_rows"])


class TestSwapJumpStatistic(unittest.TestCase):
    """The switch jump of rtcc:233-237 (WP2 golden RTC test): the distance between the new chunk's row at the swap
    tick and the old chunk's row at the same tick, None when the old plan is already over. The anchor is recorded
    either way."""

    H_OLD = 6

    def _run(self, params, ticks_before):
        master = make_master(300, seed=5, step=0.006, turn=0.02)
        eng = PhaseEngine(params)
        arm = PerfectArm(master[0])
        old = chunk_at(master, 1.0, self.H_OLD)
        eng.set_chunk(old, "sync")
        for k in range(ticks_before):
            out = eng.tick(Measured(arm.pose(), None, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        # observed at the last tick, swapped in at this one (actual = 1), 0.5 rows off the old chunk: a visible jump
        s_obs = eng.s
        new = chunk_at(master, ticks_before + 0.5, 10)
        return eng, old, new, eng.swap(new, s_obs)

    def test_golden_jump_is_the_rtcc_distance_while_the_old_chunk_runs(self):
        # the old row at the swap tick is row 2 and row 5 (its last)
        for ticks in (2, 5):
            eng, old, new, info = self._run(PhaseParams.golden_params(), ticks)
            self.assertEqual(info["d_rows"], 1.0)
            old_row = old[ticks]  # k - old_tick
            expect = float(
                np.linalg.norm(
                    np.asarray(new[1][:3], np.float64) - old_row[:3].astype(np.float64)
                )
            )
            self.assertIsNotNone(info["jump_m"], f"ticks={ticks}")
            self.assertAlmostEqual(info["jump_m"], expect, places=12)
            self.assertEqual(eng.summary()["switch_jump_m"], [info["jump_m"]])

    def test_golden_jump_is_none_when_the_old_chunk_is_already_over(self):
        # k - old_tick = 6 and 8 >= len(old) = 6
        for ticks in (6, 8):
            eng, _old, _new, info = self._run(PhaseParams.golden_params(), ticks)
            self.assertIsNone(info["jump_m"], f"ticks={ticks}")
            self.assertEqual(eng.summary()["switch_jump_m"], [])
            # the anchor against the clamped last row of the old plan is still recorded
            self.assertIsNotNone(info["anchor_rows"], f"ticks={ticks}")
            self.assertEqual(len(eng.summary()["swap_anchor_rows"]), 1)

    def test_accelerated_jump_follows_the_same_rule(self):
        params = PhaseParams(lambda_max=2.5)
        eng, _old, _new, info = self._run(params, 1)
        self.assertIsNotNone(info["jump_m"])
        eng, _old, _new, info = self._run(params, 4)  # the phase is past the old end
        self.assertIsNone(info["jump_m"])
        self.assertEqual(eng.summary()["switch_jump_m"], [])


class TestGateAndSettle(unittest.TestCase):
    """Test 11 (section 3.6): the close gate, the post-close settle, the throttle hold."""

    G = 5

    def _chunk(self, close=True, n=30):
        flips = [(self.G, 0.0 if close else 1.0)]
        master = make_master(n + 10, seed=14, step=0.003, turn=0.0, flips=flips)
        if not close:
            master[: self.G, 6] = 0.0
        return master[:n].copy()

    def _drive_to_gate(self, eng, chunk, arm_offset_fn):
        """Tick until the gate event; arm_offset_fn(k_since_park) -> (dz metres, speed injected by moving x)."""
        eng.set_chunk(chunk, "sync")
        pos = chunk[0].copy()
        outs = []
        for k in range(30):
            x = pos.copy()
            x[2] += arm_offset_fn(k)
            out = eng.tick(Measured(x, 0.0, k * DT))
            eng.executed(out.target)
            pos = out.target.copy()
            outs.append(out)
            if out.grip_event:
                return outs
        return outs

    def test_gate_waits_at_most_five_ticks_then_goes(self):
        eng = PhaseEngine(PhaseParams())
        outs = self._drive_to_gate(eng, self._chunk(), lambda k: 0.03)
        ev = outs[-1].grip_event
        self.assertIsNotNone(ev)
        self.assertEqual(ev["gate_wait_ticks"], 5)
        self.assertTrue(ev["gate_timeout"])
        parked = [o for o in outs if o.binding == "gate"]
        self.assertEqual(len(parked), 5)
        for o in parked:  # the phase stops just before the closing row, the pose is the row's, the gripper is open
            self.assertAlmostEqual(o.u, self.G - 1e-3, places=9)
            self.assertEqual(o.target[6], 1.0)
        self.assertTrue(np.array_equal(outs[-1].target, self._chunk()[self.G]))
        self.assertEqual(outs[-1].target[6], 0.0)

    def test_gate_releases_when_the_arm_arrives(self):
        eng = PhaseEngine(PhaseParams())
        # 25 mm off, closing in by 8 mm per tick once parked: within 10 mm after two parked ticks
        state = {"d": 0.025}

        def off(k):
            d = state["d"]
            state["d"] = max(0.0, d - 0.008)
            return d

        outs = self._drive_to_gate(eng, self._chunk(), off)
        ev = outs[-1].grip_event
        self.assertIsNotNone(ev)
        self.assertFalse(ev["gate_timeout"])
        self.assertLess(ev["gate_wait_ticks"], 5)
        self.assertLessEqual(ev["arm_err_m"], 0.010 + 1e-9)

    def test_gate_also_needs_the_arm_to_be_slow(self):
        eng = PhaseEngine(PhaseParams())
        chunk = self._chunk()
        eng.set_chunk(chunk, "sync")
        pos = chunk[0].copy()
        waits, ev = 0, None
        for k in range(24):
            x = pos.copy()
            x[0] += (
                0.005 if k % 2 == 0 else -0.005
            )  # always within 5 mm of the row, but moving at 0.1 m/s
            out = eng.tick(Measured(x, 0.0, k * DT))
            eng.executed(out.target)
            pos = out.target.copy()
            waits += out.binding == "gate"
            ev = out.grip_event or ev
        self.assertEqual(waits, 5)
        self.assertTrue(ev["gate_timeout"])
        self.assertLessEqual(
            ev["arm_err_m"], 0.010
        )  # the distance was fine, the speed was not
        self.assertGreater(ev["arm_speed_m_s"], 0.05)

    def test_open_has_no_gate(self):
        eng = PhaseEngine(PhaseParams())
        chunk = self._chunk(close=False)
        pose0 = chunk[0].copy()
        pose0[6] = 0.0
        eng.set_chunk(chunk, "sync")
        ev = None
        waits = 0
        for k in range(14):
            x = pose0.copy()
            x[2] += 0.03
            out = eng.tick(Measured(x, 0.0, k * DT))
            eng.executed(out.target)
            waits += out.binding == "gate"
            ev = out.grip_event or ev
        self.assertEqual(waits, 0)
        self.assertEqual(ev["kind"], "open")
        self.assertEqual(ev["gate_wait_ticks"], 0)

    def _settle_run(self, aperture_fn, issued_fn, params=None, ticks=26):
        """Window off so only the post-close limit can hold lambda at 1; returns [(k, out)] after the close."""
        params = params or PhaseParams(
            lambda_max=2.5, w_pre=0, w_post=0, grip_gate=False
        )
        master = make_master(120, seed=15, step=0.003, turn=0.0, flips=[(4, 0.0)])
        chunk = master[:80].copy()
        eng = PhaseEngine(params)
        eng.set_chunk(chunk, "sync")
        pos = chunk[0].copy()
        t_close = None
        outs = []
        for k in range(ticks):
            x = pos.copy()
            x[6] = issued_fn(k, t_close)
            out = eng.tick(Measured(x, aperture_fn(k, t_close), k * DT))
            eng.executed(out.target)
            pos = out.target.copy()
            if out.grip_event and t_close is None:
                t_close = k
            outs.append((k, out))
        return eng, t_close, outs

    def test_settle_waits_for_the_fingers_to_move_then_stop(self):
        def issued(k, tc):
            return (
                1.0 if tc is None else 0.0
            )  # the issued value reads closed from the tick after the command

        def aperture(k, tc):
            if tc is None:
                return 0.0
            j = (
                k - tc
            )  # fingers start 0.2 s after the command, open -> closed over 0.3 s
            return 0.0 if j < 2 else min(0.6, 0.2 * (j - 1)) if j < 6 else 0.6

        eng, tc, outs = self._settle_run(aperture, issued)
        after = [(k - tc, o) for k, o in outs if k > tc]
        # lambda stays <= 1 until >= 0.3 s after the issued value read closed, the fingers moved and held 2 ticks
        released = next(j for j, o in after if o.lam > 1.0 + 1e-9)
        self.assertGreaterEqual(released, 5)
        for j, o in after:
            if j < released:
                self.assertLessEqual(o.lam, 1.0 + 1e-9, f"j {j}")
        ev = eng.summary()["grip_events"][0]
        self.assertIsNotNone(ev["settle_s"])
        self.assertGreaterEqual(ev["settle_s"], 0.3)
        self.assertLess(ev["settle_s"], 1.0)

    def test_aperture_that_never_moves_is_held_until_the_timeout(self):
        eng, tc, outs = self._settle_run(
            lambda k, tc: 0.0, lambda k, tc: 1.0 if tc is None else 0.0
        )
        after = [(k - tc, o) for k, o in outs if k > tc]
        released = next(j for j, o in after if o.lam > 1.0 + 1e-9)
        self.assertGreaterEqual(released, 10)  # not before 1.0 s
        self.assertLessEqual(released, 12)
        self.assertGreaterEqual(eng.summary()["grip_events"][0]["settle_s"], 1.0)

    def test_early_stillness_is_not_stable(self):
        """The fingers start ~0.2 s after the command: two still ticks before they move must not release."""

        def aperture(k, tc):
            if tc is None:
                return 0.0
            j = k - tc
            return (
                0.0 if j < 4 else 0.3
            )  # still for 3 ticks after the issued value, then a jump to 0.3, still after

        _eng, tc, outs = self._settle_run(
            aperture, lambda k, tc: 1.0 if tc is None else 0.0
        )
        after = [(k - tc, o) for k, o in outs if k > tc]
        released = next(j for j, o in after if o.lam > 1.0 + 1e-9)
        self.assertGreaterEqual(released, 6)  # moved at j=4, stable at j=5 and 6

    def test_unknown_aperture_uses_the_time_rule(self):
        _eng, tc, outs = self._settle_run(
            lambda k, tc: None, lambda k, tc: 1.0 if tc is None else 0.0
        )
        after = [(k - tc, o) for k, o in outs if k > tc]
        released = next(j for j, o in after if o.lam > 1.0 + 1e-9)
        self.assertGreaterEqual(released, 8)
        self.assertLessEqual(released, 10)

    def test_lambda_stays_one_while_the_issued_value_lags_the_command(self):
        """The 0.75 s throttle: the command went out but the issued value (state[6]) has not caught up."""
        _eng, tc, outs = self._settle_run(
            lambda k, tc: 0.0,
            lambda k, tc: 1.0 if (tc is None or k - tc < 8) else 0.0,
            ticks=22,
        )
        for k, o in outs:
            if tc is not None and tc < k <= tc + 8:
                self.assertLessEqual(o.lam, 1.0 + 1e-9, f"tick {k}")
                self.assertEqual(
                    o.binding in {"post", "rate", "landing", "gate", "end"}, True
                )

    def test_settle_off_when_disabled(self):
        params = PhaseParams(
            lambda_max=2.5, w_pre=0, w_post=0, grip_gate=False, grip_settle=False
        )
        _eng, tc, outs = self._settle_run(
            lambda k, tc: 0.0, lambda k, tc: 1.0 if tc is None else 0.0, params=params
        )
        self.assertGreater(max(o.lam for k, o in outs if k > tc + 3), 1.2)


class TestNonFinite(unittest.TestCase):
    """Test 12 (I10, section 3.10): a bad chunk raises, a bad measurement holds, nothing non-finite is issued."""

    def _engine(self, params=None):
        master = make_master(60, seed=2)
        eng = PhaseEngine(params or PhaseParams(lambda_max=2.5))
        arm = PerfectArm(master[0])
        eng.set_chunk(master[:20].copy(), "sync")
        outs = []
        for k in range(4):
            out = eng.tick(Measured(arm.pose(), None, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
            outs.append(out)
        return eng, arm, master, outs

    def test_non_finite_chunks_raise_everywhere(self):
        for params in (PhaseParams(lambda_max=2.5), PhaseParams.golden_params()):
            eng, arm, master, _ = self._engine(params)
            calls = {
                "set_chunk": lambda r, e=eng: e.set_chunk(r, "sync"),
                "swap": lambda r, e=eng: e.swap(r, e.s - 2),
                "append": lambda r, e=eng: e.append(r),
                "splice_plain": lambda r, e=eng, a=arm: e.splice_plain(
                    r, Measured(a.pose(), None, 9.0), 8
                ),
            }
            for bad in (np.nan, np.inf, -np.inf):
                rows = master[:10].copy()
                rows[3, 2] = bad
                for name, call in calls.items():
                    with self.assertRaises(NonFiniteChunk, msg=name):
                        call(rows)
            self.assertTrue(issubclass(NonFiniteChunk, ValueError))
            # the engine was not touched by the refused chunks
            out = eng.tick(Measured(arm.pose(), None, 5.0))
            self.assertTrue(np.isfinite(out.target).all())

    def test_nan_measurement_holds_the_last_target(self):
        eng, arm, _master, outs = self._engine()
        last = outs[-1].target
        bad = arm.pose()
        bad[1] = np.nan
        out = eng.tick(Measured(bad, None, 0.9))
        self.assertTrue(out.hold)
        self.assertTrue(np.isfinite(out.target).all())
        self.assertTrue(np.array_equal(out.target, last))
        self.assertEqual(eng.summary()["nonfinite"], 1)
        eng.executed(out.target)
        # three in a row: the plan is dropped
        o2 = eng.tick(Measured(bad, None, 1.0))
        o3 = eng.tick(Measured(bad, None, 1.1))
        self.assertTrue(o3.reset)
        self.assertTrue(all(np.isfinite(o.target).all() for o in (out, o2, o3)))

    def test_nan_on_the_very_first_tick_raises_instead_of_inventing_a_target(self):
        master = make_master(60, seed=2)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        eng.set_chunk(master[:10].copy(), "sync")
        bad = master[0].copy()
        bad[0] = np.nan
        with self.assertRaises((RuntimeError, FloatingPointError)):
            eng.tick(Measured(bad, None, 0.0))

    def test_never_returns_a_non_finite_target_with_inf_poses(self):
        eng, arm, master, _ = self._engine()
        for v in (np.inf, -np.inf, np.nan):
            bad = arm.pose()
            bad[3] = v
            out = eng.tick(Measured(bad, None, 3.0))
            self.assertTrue(np.isfinite(out.target).all())
            self.assertTrue(out.hold)
            eng.executed(out.target)
            eng.set_chunk(master[:20].copy(), "sync")

    def test_comparisons_are_written_nan_safe(self):
        src = inspect.getsource(PhaseEngine._feasible)
        self.assertIn("not (dp <= p.hard_leash_m and dr <= p.hard_leash_rad)", src)
        self.assertIn("not (sp <= p.step_m and sr <= p.step_rad)", src)

    def test_non_finite_sent_is_ignored(self):
        eng, arm, _master, _outs = self._engine()
        eng.executed(np.full(7, np.nan, np.float32))
        out = eng.tick(Measured(arm.pose(), None, 4.0))
        self.assertTrue(np.isfinite(out.target).all())


class TestOutputsAndRange(unittest.TestCase):
    """Test 13 (I7): float32 everywhere; a blend never walks the pose into the client's workspace clamp."""

    # the client's defaults (rc Args): copied as numbers only
    MIN_Z, MAX_XY = 0.075, 0.85
    RX, RY, RZ = (1.50, 4.03), (-0.74, 0.96), (-2.44, 0.77)

    @staticmethod
    def _angle_out(v, lo, hi):
        centre, half = 0.5 * (lo + hi), 0.5 * (hi - lo)
        off = float(wrap_to_pi(np.array([v - centre]))[0])
        return abs(off) > half

    def hits(self, t):
        return bool(
            t[2] < self.MIN_Z
            or abs(t[0]) > self.MAX_XY
            or abs(t[1]) > self.MAX_XY
            or self._angle_out(float(t[3]), *self.RX)
            or self._angle_out(float(t[4]), *self.RY)
            or self._angle_out(float(t[5]), *self.RZ)
        )

    def test_all_outputs_are_float32(self):
        master = make_master(200, seed=2, step=0.004, flips=[(10, 0.0), (30, 1.0)])
        for params in (
            PhaseParams(lambda_max=2.5),
            PhaseParams.golden_params(),
            PhaseParams(lead=True),
        ):
            eng = PhaseEngine(params)
            arm = LagArm(master[0])
            eng.set_chunk(master[:40].copy(), "sync")
            for k in range(30):
                m = Measured(arm.pose(), None, k * DT)
                prop = eng.begin(m, None)
                out = eng.commit()
                arm.step(out.target)
                eng.executed(out.target)
                self.assertEqual(out.target.dtype, np.float32)
                self.assertEqual(out.target.shape, (7,))
                self.assertTrue(np.isfinite(out.target).all())
                if prop.past_end:
                    break
        self.assertEqual(eng.prev_rows().dtype, np.float32)
        self.assertEqual(eng.future_row().dtype, np.float32)
        self.assertEqual(eng.preview(4).dtype, np.float32)

    def test_euler_wrap_takes_the_short_way(self):
        """Rows at rx -3.13 and +3.13 are 0.023 rad apart: the interpolation never swings through rx = 0, and a
        step of the issued rx stays tiny (the client's raw_rot uses the same wrapped difference)."""
        master = make_master(60, seed=2, step=0.004, turn=0.0)
        chunk = master[:30].copy()
        chunk[:, 3] = np.where(np.arange(30) % 2 == 0, -3.13, 3.13).astype(np.float32)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = PerfectArm(chunk[0])
        prev = None
        for k, (_, out, _) in enumerate(run_open(eng, arm, chunk, max_ticks=40)):
            self.assertGreater(abs(float(out.target[3])), 3.0, f"tick {k}")
            if prev is not None:
                self.assertLess(float(abs(wrap_to_pi(out.target[3] - prev))), 0.03)
            prev = out.target[3]
        self.assertGreater(k, 5)

    def test_blend_offset_is_shrunk_before_it_triggers_a_clamp(self):
        z0 = 0.09
        n = 12
        rows_old = np.zeros((n, 7))
        rows_old[:, 0] = 0.45 + 0.004 * np.arange(n)
        rows_old[:, 2] = z0
        rows_old[:, 3] = 3.1
        rows_old[:, 6] = 1.0
        rows_new = rows_old.copy()
        rows_new[:, 2] = np.where(
            np.arange(n) < 2, 0.10, 0.079
        )  # drops 21 mm right after the swap
        rows_old, rows_new = rows_old.astype(np.float32), rows_new.astype(np.float32)
        self.assertGreater(
            float(rows_new[:, 2].min()), self.MIN_Z + 0.002
        )  # the policy's own path is above the floor
        outcomes = {}
        for use_ws in (False, True):
            eng = PhaseEngine(PhaseParams(lambda_max=1.0 if False else 2.0))
            if use_ws:
                eng.set_workspace(self.hits)
            pose = rows_old[0].copy()
            pose[2] = (
                0.08  # the arm (and so the last target) sits 10 mm under the new chunk's z
            )
            eng.set_chunk(rows_old, "sync")
            hit = 0
            for k in range(3):
                out = eng.tick(Measured(pose, None, k * DT))
                pose = out.target.copy()
                pose[2] = 0.08
                eng.executed(pose)
            eng.swap(rows_new, eng.s - 1.0)
            for k in range(3, 9):
                out = eng.tick(Measured(pose, None, k * DT))
                hit += self.hits(out.target)
                pose = out.target.copy()
                eng.executed(out.target)
            outcomes[use_ws] = hit
        self.assertGreater(
            outcomes[False],
            0,
            "the scenario must really push into the floor without the check",
        )
        self.assertEqual(outcomes[True], 0)

    def test_blended_targets_never_clamp_on_random_scenarios(self):
        rng = np.random.default_rng(5)
        total = 0
        for case in range(25):
            master = make_master(200, seed=100 + case, step=0.003, turn=0.1)
            master[:, 2] = 0.2 + 0.05 * np.sin(np.arange(200) / 9.0)
            eng = PhaseEngine(PhaseParams(lambda_max=2.5))
            eng.set_workspace(self.hits)
            arm = LagArm(master[0])
            eng.set_chunk(chunk_at(master, 1.0, 10), "sync")
            pending = None
            for k in range(40):
                m = Measured(arm.pose(), None, k * DT)
                if pending and pending[0] <= k:
                    eng.swap(pending[1], pending[2])
                    pending = None
                prop = eng.begin(m, None)
                if prop.past_end:
                    eng.set_chunk(chunk_at(master, eng.s + 1.0, 10), "sync")
                    eng.begin(m, None)
                out = eng.commit()
                arm.step(out.target)
                eng.executed(out.target)
                total += 1
                self.assertFalse(self.hits(out.target), f"case {case} tick {k}")
                if pending is None and eng.u >= 4.0:
                    off = np.append(rng.normal(0, 0.03, 2), rng.normal(0, 0.01))
                    pending = (k + 2, chunk_at(master, eng.s, 10, offset=off), eng.s)
        self.assertGreater(total, 800)


class TestSummaryAndRecords(unittest.TestCase):
    """Test 14, engine part: summary() is the section 8 record, JSON-safe; drain_records() the trace lines."""

    def _run(self):
        master = make_master(
            120, seed=3, step=0.005, turn=0.1, flips=[(12, 0.0), (26, 1.0)]
        )
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = LagArm(master[0])
        eng.set_chunk(chunk_at(master, 1.0, 12), "sync")
        pending = None
        for k in range(60):
            m = Measured(arm.pose(), 0.0, k * DT)
            if pending and pending[0] <= k:
                eng.swap(pending[1], pending[2])
                pending = None
            prop = eng.begin(m, None)
            if prop.past_end:
                eng.set_chunk(chunk_at(master, eng.s + 1.0, 12), "sync")
                eng.begin(m, None)
            out = eng.commit()
            arm.step(out.target)
            eng.executed(out.target)
            if pending is None and eng.u >= 5.0:
                eng.prev_rows()
                pending = (
                    k + 2,
                    chunk_at(master, eng.s, 12, offset=np.array([0.0, 0.03, 0.0])),
                    eng.s,
                )
        return eng

    def test_summary_keys_and_json(self):
        s = self._run().summary()
        json.dumps(s, allow_nan=False)
        for key in (
            "ticks", "rows_advanced", "lambda_eff", "ticks_saved_est", "lambda_p50", "lambda_p90",
            "frac_ticks_lambda_gt_1.1", "binding", "leash_holds", "hold_resets", "dp_max_m", "dr_max_rad",
            "step_max_m", "step_max_rad", "blends", "blend_max_step_m", "switch_jump_m", "swap_anchor_rows",
            "splice_rows", "grip_events", "starved_ticks", "governor_ticks", "early_triggers", "delays_rows",
            "plain_end_holds", "nonfinite",
        ):  # fmt: skip
            self.assertIn(key, s)
        self.assertEqual(
            set(s["binding"]),
            {
                "max",
                "v",
                "omega",
                "lag",
                "kappa",
                "window",
                "post",
                "soft_leash",
                "gov",
                "rate",
                "landing",
                "gate",
                "hard_leash",
                "step",
                "end",
            },
        )
        self.assertEqual(s["ticks"], 60)
        self.assertAlmostEqual(s["lambda_eff"], s["rows_advanced"] / s["ticks"])
        self.assertAlmostEqual(s["ticks_saved_est"], s["rows_advanced"] - s["ticks"])
        self.assertEqual(sum(s["binding"].values()), s["ticks"])
        self.assertGreater(s["lambda_eff"], 1.0)
        self.assertGreaterEqual(len(s["grip_events"]), 2)
        for ev in s["grip_events"]:
            for key in (
                "s",
                "kind",
                "lam_at_cmd",
                "arm_err_m",
                "arm_speed_m_s",
                "gate_wait_ticks",
                "gate_timeout",
                "settle_s",
            ):
                self.assertIn(key, ev)

    def test_trace_records(self):
        eng = self._run()
        recs = eng.drain_records()
        json.dumps(recs, allow_nan=False)
        kinds = collections.Counter(r["kind"] for r in recs)
        self.assertEqual(kinds["tick"], 60)
        self.assertGreaterEqual(kinds["chunk"], 2)
        self.assertGreaterEqual(kinds["request"], 1)
        self.assertGreaterEqual(kinds["grip"], 2)
        self.assertTrue(
            all(
                r["event"] in {"close", "open"}
                for r in recs
                if r["kind"] == "grip" and "event" in r
            )
        )
        tick = next(r for r in recs if r["kind"] == "tick")
        for key in ("k", "t", "s", "u", "e", "lam", "cap", "chunk_id", "tgt", "meas", "aperture", "g_cmd", "g_issued",
                    "dp", "dr", "eps", "rho", "blend_w", "inflight_age", "hold"):  # fmt: skip
            self.assertIn(key, tick)
        self.assertEqual(len(tick["tgt"]), 7)
        chunk = next(r for r in recs if r["kind"] == "chunk")
        for key in (
            "chunk_id",
            "origin",
            "rows",
            "d_rows",
            "anchor_rows",
            "jump_m",
            "n_blend",
            "nonfinite",
        ):
            self.assertIn(key, chunk)
        self.assertEqual(eng.drain_records(), [])  # drained

    def test_scheduler_counters_and_notes(self):
        eng = PhaseEngine(PhaseParams())
        eng.bump("starved_ticks")
        eng.bump("starved_ticks", 2)
        eng.note_value("delays_rows", 2.5)
        eng.note({"kind": "request", "delay_rows": 3})
        s = eng.summary()
        self.assertEqual(s["starved_ticks"], 3)
        self.assertEqual(s["delays_rows"], [2.5])
        self.assertEqual(eng.drain_records()[0]["delay_rows"], 3)

    def test_new_episode_clears_everything(self):
        eng = self._run()
        eng.new_episode()
        s = eng.summary()
        self.assertEqual((s["ticks"], s["grip_events"], s["leash_holds"]), (0, [], 0))
        self.assertFalse(eng.has_rows())
        self.assertEqual(eng.s, 0.0)


class TestEngineMisc(unittest.TestCase):
    def test_reset_keeps_the_phase_and_the_last_target(self):
        master = make_master(60, seed=2)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = PerfectArm(master[0])
        eng.set_chunk(master[:20].copy(), "sync")
        for k in range(5):
            out = eng.tick(Measured(arm.pose(), None, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        s = eng.s
        eng.reset()
        self.assertFalse(eng.has_rows())
        self.assertEqual((eng.s, eng.lam), (s, 1.0))
        eng.set_chunk(master[30:50].copy(), "sync")
        out = eng.tick(Measured(arm.pose(), None, 1.0))
        self.assertGreater(eng.s, s)
        self.assertLessEqual(out.dp, 0.08)

    def test_rejected_rolls_the_tick_back(self):
        master = make_master(60, seed=2)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = PerfectArm(master[0])
        eng.set_chunk(master[:20].copy(), "sync")
        for k in range(4):
            out = eng.tick(Measured(arm.pose(), None, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        s, lam = eng.s, eng.lam
        eng.begin(Measured(arm.pose(), None, 0.5), None)
        eng.commit()
        self.assertGreater(eng.s, s)
        eng.rejected()
        self.assertEqual((eng.s, eng.lam), (s, lam))
        self.assertEqual(eng.summary()["rejected"], 1)

    def test_commit_after_a_swap_recomputes_from_the_same_measurement(self):
        master = make_master(120, seed=2, step=0.005)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = PerfectArm(master[0])
        eng.set_chunk(chunk_at(master, 1.0, 6), "sync")
        for k in range(3):
            out = eng.tick(Measured(arm.pose(), None, k * DT))
            arm.step(out.target)
            eng.executed(out.target)
        m = Measured(arm.pose(), None, 0.4)
        s_obs = eng.s - 1.0
        prop = eng.begin(m, None)
        info = eng.swap(
            chunk_at(master, s_obs, 12), s_obs
        )  # arrives between begin and commit
        out = eng.commit()
        self.assertFalse(out.hold)
        self.assertGreater(eng.s, prop.s_cand - 1e-9)
        self.assertGreaterEqual(info["d_rows"], 1.0)

    def test_commit_without_begin_and_begin_without_rows(self):
        eng = PhaseEngine(PhaseParams())
        with self.assertRaises(RuntimeError):
            eng.commit()
        with self.assertRaises(RuntimeError):
            eng.prev_rows()
        with self.assertRaises(RuntimeError):
            eng.future_row()
        with self.assertRaises(RuntimeError):
            eng.append(np.zeros((4, 7), np.float32))
        prop = eng.begin(Measured(np.zeros(7), None, 0.0), None)
        self.assertEqual(prop.lam, 0.0)
        out = eng.commit()  # no rows: the measured pose itself is held
        self.assertTrue(out.hold)
        eng2 = PhaseEngine(PhaseParams())
        eng2.begin(Measured(np.full(7, np.nan), None, 0.0), None)
        with self.assertRaises(
            RuntimeError
        ):  # no rows, no last target and no finite pose: nothing to hold
            eng2.commit()

    def test_bad_chunk_shapes(self):
        eng = PhaseEngine(PhaseParams())
        for bad in (np.zeros((0, 7)), np.zeros((4, 6)), np.zeros(7)):
            with self.assertRaises(ValueError):
                eng.set_chunk(bad, "sync")

    def test_governor_slows_the_phase_towards_the_chunk_end(self):
        master = make_master(120, seed=2, step=0.003, turn=0.0)
        chunk = master[:12].copy()
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = PerfectArm(chunk[0])
        eng.set_chunk(chunk, "sync")
        lams = []
        for k in range(14):
            m = Measured(arm.pose(), None, k * DT)
            prop = eng.begin(m, Inflight(age_ticks=k, d_hat_ticks=6, done=False))
            if prop.past_end:
                break
            out = eng.commit()
            arm.step(out.target)
            eng.executed(out.target)
            lams.append((out.u, out.lam, out.binding))
        self.assertTrue(any(b == "gov" for _, _, b in lams))
        self.assertLess(lams[-1][0], 11.0 + 1e-9)  # it did not run into the end
        self.assertGreater(eng.summary()["governor_ticks"], 0)

    def test_append_extends_without_clamping_to_row_h_minus_1(self):
        master = make_master(120, seed=2, step=0.004, turn=0.0)
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = PerfectArm(master[0])
        eng.set_chunk(master[:10].copy(), "sync")
        seen = []
        for k in range(14):
            m = Measured(arm.pose(), None, k * DT)
            prop = eng.begin(m, None)
            if prop.past_end and eng.rows_left < 3.0 and len(seen) < 1:
                eng.append(master[10:20].copy())
                seen.append(k)
                prop = eng.begin(m, None)
            if prop.past_end:
                break
            out = eng.commit()
            arm.step(out.target)
            eng.executed(out.target)
        self.assertTrue(seen)
        self.assertGreater(
            eng.u, 10.0
        )  # ran on past the first chunk's last row, no 2.5 Hz stutter
        self.assertTrue(np.array_equal(eng.future_row(), master[19]))


class TestComputeBudget(unittest.TestCase):
    """G-H: begin + commit costs well under 2 ms p99 / 10 ms max of CPU time (thread CPU time, so that the OS
    preempting a niced process on a busy shared machine is not counted as engine work)."""

    def test_tick_cost(self):
        master = make_master(
            400, seed=9, step=0.005, turn=0.1, flips=[(15, 0.0), (40, 1.0), (70, 0.0)]
        )
        eng = PhaseEngine(PhaseParams(lambda_max=2.5))
        arm = LagArm(master[0])
        eng.set_chunk(chunk_at(master, 1.0, 10), "sync")
        pending = None
        cpu = []
        for k in range(300):
            m = Measured(arm.pose(), 0.0, k * DT)
            if pending and pending[0] <= k:
                eng.swap(pending[1], pending[2])
                pending = None
            t0 = time.thread_time_ns()
            prop = eng.begin(m, None)
            if prop.past_end:
                eng.set_chunk(chunk_at(master, eng.s + 1.0, 10), "sync")
                prop = eng.begin(m, None)
            out = eng.commit()
            cpu.append(time.thread_time_ns() - t0)
            arm.step(out.target)
            eng.executed(out.target)
            if pending is None and eng.u >= 4.0:
                off = np.array([0.0, 0.03, 0.0])
                pending = (k + 2, chunk_at(master, eng.s, 10, offset=off), eng.s)
            eng.drain_records()
        ms = np.array(cpu[5:]) / 1e6
        self.assertLess(
            np.percentile(ms, 99), 2.0, f"p99 {np.percentile(ms, 99):.3f} ms"
        )
        self.assertLess(ms.max(), 10.0, f"max {ms.max():.3f} ms")


class TestFuzzInvariants(unittest.TestCase):
    """G-B in one place: seeded random episodes through a minimal RTC / plain scheduler and a lagging arm, every tick
    checked against I3-I10 with the client's own formulas (rc:706-707)."""

    @staticmethod
    def _client_dist(target, pose):
        dp = float(np.linalg.norm(np.asarray(target[:3], np.float64) - pose[:3]))
        dr = float(
            np.abs(wrap_to_pi(np.asarray(target[3:6], np.float64) - pose[3:6])).max()
        )
        return dp, dr

    def _check_windows(self, case, k, eng, s0, out, params):
        """I3: every tick whose move meets [G - w_pre, G + w_post) of a change row of the plan in force has lambda <= 1."""
        s1 = eng.s
        if out.hold or s1 <= s0 or eng.has_rows() is False:
            return
        for g in eng._G:
            if (
                s0 - eng._o < g + params.w_post - 1e-9
                and s1 - eng._o > g - params.w_pre + 1e-9
            ):
                self.assertLessEqual(
                    s1 - s0, 1.0 + 1e-9, f"{case} t{k}: window of row {g}"
                )

    def _check_tick(self, case, k, out, m, prev_target, params):
        t = out.target
        self.assertEqual(t.dtype, np.float32, f"{case} t{k}")
        self.assertTrue(np.isfinite(t).all(), f"{case} t{k}")
        self.assertLessEqual(
            out.lam, params.lambda_max + 1e-9, f"{case} t{k}: I9 lambda <= lambda_max"
        )
        self.assertGreaterEqual(out.lam, -1e-12)
        if out.hold:
            if np.array_equal(t, prev_target):
                return
            # the only other hold: the last target was outside the hard leash and is walked back toward the arm, one step
            # limit at most, and closer to it than it was (review finding: a pushed arm must be yielded to)
            self.assertIsNotNone(prev_target, f"{case} t{k}")
            pdp, pdr = self._client_dist(prev_target, m.pose)
            self.assertTrue(
                not (pdp <= 0.08 and pdr <= 0.28),
                f"{case} t{k}: a hold re-sends the last target (or recovers from outside the leash)",
            )
            p64, t64 = prev_target.astype(np.float64), t.astype(np.float64)
            self.assertLessEqual(float(np.linalg.norm(t64[:3] - p64[:3])), 0.05 + 1e-6)
            self.assertLessEqual(
                float(np.abs(wrap_to_pi(t64[3:6] - p64[3:6])).max()), 0.12 + 1e-6
            )
            ndp, ndr = self._client_dist(t, m.pose)
            self.assertTrue(
                ndp < pdp - 1e-9 or ndr < pdr - 1e-9 or ndp == 0.0,
                f"{case} t{k}: recovery must come closer to the arm",
            )
            return
        dp, dr = self._client_dist(t, m.pose)
        self.assertLessEqual(
            dp, 0.08, f"{case} t{k}: I4 {dp}"
        )  # and so never the client's 0.10 refusal
        self.assertLessEqual(dr, 0.28, f"{case} t{k}: I4 {dr}")
        if prev_target is not None:
            p64 = prev_target.astype(np.float64)
            t64 = t.astype(np.float64)
            self.assertLessEqual(
                float(np.linalg.norm(t64[:3] - p64[:3])),
                0.05 + 1e-6,
                f"{case} t{k}: I5",
            )
            self.assertLessEqual(
                float(np.abs(wrap_to_pi(t64[3:6] - p64[3:6])).max()),
                0.12 + 1e-6,
                f"{case} t{k}: I5 rot",
            )
        self.assertGreaterEqual(out.e + 1e-9, out.u, f"{case} t{k}: I6")

    def _rtc_episode(self, case, rng, n_ticks=110):
        flips, g = [], 1.0
        for r in sorted(
            rng.choice(np.arange(8, 160), size=int(rng.integers(0, 4)), replace=False)
        ):
            g = 1.0 - g
            flips.append((int(r), g))
        master = make_master(
            400,
            seed=case,
            step=float(rng.uniform(0.003, 0.012)),
            turn=float(rng.uniform(0.0, 0.15)),
            flips=flips,
        )
        params = PhaseParams(
            lambda_max=float(rng.uniform(1.0, 3.0)),
            alpha=float(rng.choice([0.005, 0.01])),
            lead=bool(rng.integers(0, 2)),
            swap_reanchor=bool(rng.integers(0, 2)),
        )
        eng = PhaseEngine(params)
        arm = LagArm(
            master[0],
            tau=float(rng.uniform(0.15, 0.45)),
            gain=float(rng.uniform(0.0, 1.0)),
            sticky_gripper=int(rng.integers(0, 9)) or None,
        )
        stick_at = int(rng.integers(30, 90)) if rng.random() < 0.3 else None
        H, pending, prev_target = 12, None, None
        stats = collections.Counter()
        for k in range(n_ticks):
            if stick_at is not None:
                arm.stuck = stick_at <= k < stick_at + 9
            m = Measured(arm.pose(), float(rng.random()), k * DT)
            if pending and pending[0] <= k:
                eng.swap(pending[1], pending[2])
                pending = None
            if not eng.has_rows():
                eng.set_chunk(chunk_at(master, eng.s + 1.0, H), "sync")
                pending = None
            infl = Inflight(k - pending[3], pending[4], False) if pending else None
            prop = eng.begin(m, infl)
            if prop.past_end:
                if pending:
                    eng.swap(pending[1], pending[2])
                    pending = None
                else:
                    eng.set_chunk(chunk_at(master, eng.s + 1.0, H), "sync")
                prop = eng.begin(m, None)
            s0 = eng.s
            out = eng.commit()
            self._check_tick(case, k, out, m, prev_target, params)
            self._check_windows(case, k, eng, s0, out, params)
            stats["hold"] += out.hold
            stats["event"] += out.grip_event is not None
            stats["blend"] += out.blend_w < 1.0
            stats["reset"] += out.reset
            if rng.random() < 0.01 and not out.hold:
                eng.rejected()
                eng.reset()
                pending = None
                stats["rejected"] += 1
                continue
            arm.step(out.target)
            eng.executed(out.target)
            prev_target = out.target.copy()
            if out.reset:
                pending = None
            if pending is None and eng.has_rows() and 5.0 <= eng.u < H - 1:
                jump = (
                    rng.normal(0.0, 0.03, 3)
                    if rng.random() < 0.5
                    else rng.normal(0.0, 0.003, 3)
                )
                delay = int(rng.integers(1, 6))
                pending = (
                    k + delay,
                    chunk_at(master, eng.s, H, offset=jump),
                    eng.s,
                    k,
                    delay + 1,
                )
        json.dumps(eng.summary(), allow_nan=False)
        return stats

    def test_rtc_episodes(self):
        rng = np.random.default_rng(1101)
        total = collections.Counter()
        for case in range(36):
            total.update(self._rtc_episode(case, rng))
        for key in ("hold", "event", "blend"):
            self.assertGreater(total[key], 0, f"the fuzz never exercised {key}")

    def test_plain_episodes(self):
        rng = np.random.default_rng(1102)
        events = 0
        for case in range(20):
            flips, g = [], 1.0
            for r in sorted(
                rng.choice(
                    np.arange(8, 120), size=int(rng.integers(0, 3)), replace=False
                )
            ):
                g = 1.0 - g
                flips.append((int(r), g))
            master = make_master(
                400,
                seed=500 + case,
                step=float(rng.uniform(0.003, 0.01)),
                turn=0.1,
                flips=flips,
            )
            params = PhaseParams(
                lambda_max=float(rng.uniform(1.0, 3.0)),
                alpha=float(rng.choice([0.005, 0.01])),
                lead=bool(rng.integers(0, 2)),
            )
            eng = PhaseEngine(params)
            arm = LagArm(master[0], tau=float(rng.uniform(0.15, 0.4)))
            remaining, prev_target = 0, None
            for k in range(130):
                m = Measured(arm.pose(), None, k * DT)
                if remaining <= 0 or not eng.has_rows():
                    lag = float(rng.uniform(-1.0, 5.0))
                    phi = max(eng.s - 1.0 - lag, 0.0)
                    h = int(rng.integers(5, 11))
                    remaining = eng.splice_plain(chunk_at(master, phi, 12), m, h)
                    self.assertGreaterEqual(remaining, 1)
                s0 = eng.s
                out = eng.tick(m)
                self._check_tick(f"plain{case}", k, out, m, prev_target, params)
                self._check_windows(f"plain{case}", k, eng, s0, out, params)
                events += out.grip_event is not None
                arm.step(out.target)
                eng.executed(out.target)
                prev_target = out.target.copy()
                remaining -= 1
            json.dumps(eng.summary(), allow_nan=False)
        self.assertGreater(events, 5)


if __name__ == "__main__":
    unittest.main()
