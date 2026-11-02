"""Chunk acceleration v3, the phase clock: parameters, data types and the engine interface (pure numpy, no I/O).

Design: fr3_accel_test/decisions/2026-11-01-phase-clock-design.md (the contract; section numbers below refer to it).

One engine for the three modes (plain, RTC, VLASH). A phase s runs in policy rows (1 row = 0.1 s of demonstration
time) and advances lambda_k rows per 10 Hz tick; every tick the engine issues the chunk's interpolation at the
issued phase e >= u (u = the reference phase the policy believes it is at). The client's own checks (0.10 m /
0.35 rad from the measured pose, binarized gripper, 10 Hz) are untouched; the engine adds a soft and a hard leash
against the measured pose, a step limit, min-jerk offset blending, exact gripper landings, a close gate and a
post-close settle test.

This file holds the FROZEN interface of section 6 and the engine behind it. ``PhaseParams`` is complete (section 5:
every parameter, its default, its validated range, the relaxation tags); ``PhaseEngine`` implements sections 3.1-3.11
and the logging records of section 8. Nothing here talks to the robot, a server or a camera.

Relaxed limits carry the tag ``RELAXED[2026-11-01]`` followed by the ledger number (LIMITS_LEDGER.md section E,
checked both ways by check_relaxed.py): E1 and E2 are rules v3 deletes (documented below, no field), E3-E7, E9, E10,
E13 and E14 sit on the field they relax, E11 and E12 live in run_collect_phase.py and levi/marvel0/command.py. The
section keeps one tag date; the rows changed on 2026-11-02 (the "strong" profile: E3, E6, E9, E13, E14) say so in the
ledger and on their tag line.
"""

from __future__ import annotations

import collections
import dataclasses
import math
import numbers

import numpy as np

RELAXED_TAG = "RELAXED[2026-11-01]"
PHASE_RULE = "v3"

# fr3_env.py:301,392-398: 1.0 = open, 0.0 = closed. The close gate and the post-close settle only apply to 1 -> 0.
GRIP_OPEN, GRIP_CLOSED = 1.0, 0.0
GRIP_THRESHOLD = 0.5  # b(g) = (g >= 0.5), the client's own binarization

# The client rejects a target further than this from the measured pose (rc:372,375). Only a human changes these.
CLIENT_MAX_M = 0.10
CLIENT_MAX_RAD = 0.35

# Leash recovery (section 3.8 addendum): a last target outside the hard leash is walked back toward the measured pose by
# at most one step limit per tick; the step is only issued when it lands inside this fraction of the client's own limit
# (otherwise the client would reject it and the old target would stay in force: the target is re-anchored instead).
_RECOVER_MARGIN = 0.9
_STEP_SAFETY = 1.0 - 1e-6
# ... and it is moved back only as far as needed: until it sits this fraction of the hard leash from the measured pose,
# per bound (translation; each rotation axis), a bound that holds is left alone. 0.95 puts it 4 mm / 14 mrad inside
# 0.08 m / 0.28 rad: more than ten standard deviations of the 50-300 um pose noise of an arm pressing on a contact, so
# the noise alone does not trip the next recovery, and a backward move of a few millimetres instead of the full 50 mm
# step (review 11-02 M1: a 50 mm sawtooth at ~1.5 Hz in contact at lambda 3.0).
_RECOVER_INSIDE = 0.95

ALPHAS = (
    0.005,
    0.01,
)  # controller setpoint filter alpha; must match `set_clip.py --show`
MINJERK_PEAK = 1.875  # max of d/dsigma of w(s) = 10 s^3 - 15 s^4 + 6 s^5: peak per-tick increment = 1.875 * jump / n_b

# alpha -> (D_alpha s, v_cap m/s, omega_cap rad/s, a_lat m/s^2). Used where PhaseParams leaves the field None.
_BY_ALPHA = {
    0.005: (0.27, 0.30, 0.8, 2.4),
    0.01: (0.17, 0.35, 1.0, 2.4),
}

# A VLASH grasp re-plan (PhaseEngine.replan) leaves at least this many rows of the new chunk ahead of the phase.
REPLAN_RUNWAY_ROWS = 3

# Switches golden mode turns off (section 3.11); `golden` forces these on construction.
_GOLDEN_FORCED = {
    "lambda_max": 1.0,
    "lead": False,
    "swap_reanchor": False,
    "w_pre": 0,
    "w_post": 0,
    "soft_leash": False,
    "hard_leash": False,
    "step_limit": False,
    "blend": False,
    "plain_reanchor": False,
    "grip_gate": False,
    "grip_settle": False,
    "open_settle": False,
    "governor": False,
    "early_trigger": False,
    "grasp_replan_m": 0.0,
    "close_hold": False,
}

# Validated ranges (section 5, "validation range" column): name -> (lo, hi, lo_open). Optional (None) fields are
# only checked when set. Anything the spec gives no range for is held to "no looser than the spec value".
_RANGES = {
    "lambda_max": (
        1.0,
        3.0,
        False,
    ),  # the page offers 2.0 / 2.5 / 3.0 (2026-11-02)
    "w_pre": (0, 6, False),
    "w_post": (0, 10, False),
    "v_cap": (0.0, 0.40, True),
    "omega_cap": (0.0, 1.2, True),
    "a_lat": (0.0, 2.4, True),
    "d_alpha": (0.10, 0.40, False),
    "lag_safety": (0.0, 0.94, True),
    "lag_offset_m": (0.0, 0.01, False),
    "lag_offset_rad": (0.0, 0.02, False),
    "d_meas_min": (0.10, 0.40, False),
    "d_meas_max": (0.10, 0.40, False),
    "d_meas_window": (1, 120, False),
    "d_meas_speed": (0.0, 0.2, True),
    "curv_len": (0.005, 0.05, False),
    "curv_min_arc": (0.0, 0.05, False),
    "rate_up": (0.0, 1.0, True),
    "jerk": (0.0, 0.6, True),
    "a_tan_up": (0.0, 2.0, True),
    "d_dn_max": (0.0, 0.5, True),
    "d_dn_gain": (0.0, 0.9, True),
    "rate_dn_soft": (0.0, 0.5, True),
    "soft_m0": (0.0, 0.08, True),
    "soft_m1": (0.0, 0.08, True),
    "soft_rad0": (0.0, 0.28, True),
    "soft_rad1": (0.0, 0.28, True),
    "hard_leash_m": (
        0.0,
        CLIENT_MAX_M,
        True,
    ),  # strictly below the client's 0.10 m (checked again below)
    "hard_leash_rad": (0.0, CLIENT_MAX_RAD, True),
    "step_m": (0.0, 0.05, True),
    "step_rad": (0.0, 0.12, True),
    "hold_reset_after": (1, 3, False),
    "bisect_iters": (1, 32, False),
    "pause_dp": (0.0, 0.002, False),
    "pause_dr": (0.0, 0.01, False),
    "blend_min_ticks": (1, 5, False),
    "blend_max_ticks": (1, 5, False),
    "blend_stretch_max_ticks": (1, 8, False),
    "blend_step_m": (0.0, 0.03, True),
    "blend_step_rad": (0.0, 0.06, True),
    "blend_skip_m": (0.0, 0.005, False),
    "blend_skip_rad": (0.0, 0.02, False),
    "plain_reanchor_rows": (0, 4, False),
    "plain_reanchor_span": (1, 9, False),
    "swap_reanchor_rows": (0, 3, False),
    "swap_reanchor_span": (1, 9, False),
    "reanchor_cos_min": (0.5, 1.0, False),
    "lead_max_rows": (0.0, 3.0, True),
    "gate_dist_m": (0.0, 0.010, True),
    "gate_speed": (0.0, 0.05, True),
    "gate_max_ticks": (1, 5, False),
    "settle_min_s": (0.3, 1.0, False),
    "settle_aperture_delta": (0.02, 1.0, False),
    "settle_stable_ticks": (2, 10, False),
    "settle_stable_eps": (0.0, 0.01, True),
    "settle_timeout_s": (0.3, 1.0, False),
    "settle_unknown_s": (0.3, 1.0, False),
    "open_settle_min_s": (0.2, 1.0, False),
    "open_settle_timeout_s": (0.3, 1.5, False),
    "open_settle_unknown_s": (0.3, 1.5, False),
    "governor_margin_rows": (0.0, 5.0, False),
    "delay_history": (1, 32, False),
    "grasp_replan_m": (0.0, 0.10, False),
}
_INT_FIELDS = frozenset(
    {
        "w_pre",
        "w_post",
        "d_meas_window",
        "hold_reset_after",
        "bisect_iters",
        "blend_min_ticks",
        "blend_max_ticks",
        "blend_stretch_max_ticks",
        "plain_reanchor_rows",
        "plain_reanchor_span",
        "swap_reanchor_rows",
        "swap_reanchor_span",
        "gate_max_ticks",
        "settle_stable_ticks",
        "delay_history",
    }
)
_BOOL_FIELDS = frozenset(
    {
        "golden",
        "lead",
        "swap_reanchor",
        "soft_leash",
        "hard_leash",
        "step_limit",
        "blend",
        "plain_reanchor",
        "grip_gate",
        "grip_settle",
        "open_settle",
        "governor",
        "early_trigger",
        "close_hold",
    }
)
_OPTIONAL_FIELDS = frozenset(
    {"d_alpha", "v_cap", "omega_cap", "a_lat"}
)  # None = the per-alpha table value


def wrap_to_pi(values) -> np.ndarray:
    """Wrap angles into [-pi, pi): the same expression as rc.wrap_to_pi (rc:178-180)."""
    return (np.asarray(values) + np.pi) % (2 * np.pi) - np.pi


def _is_real(v) -> bool:
    return (
        isinstance(v, numbers.Real)
        and not isinstance(v, bool)
        and math.isfinite(float(v))
    )


@dataclasses.dataclass(frozen=True)
class PhaseParams:
    """Every tunable of the phase clock (section 5) with its default; the defaults are pinned by test_phase.py.

    Page controls are ``lambda_max`` and ``alpha`` only (``for_page``). Everything else is an internal constant that
    offline sweeps may override with ``dataclasses.replace``. Fields left ``None`` (``d_alpha``, ``v_cap``,
    ``omega_cap``, ``a_lat``) follow ``alpha``: read the effective values from ``derived()``, never from the field.

    Construction validates every range of section 5 and raises ValueError naming the field. ``golden=True`` is a
    master switch (section 3.11): construction forces lambda_max 1 and every feature switch off, so
    ``PhaseParams(golden=True) == PhaseParams.golden_params()``. A non-golden set must keep ``hard_leash`` and
    ``step_limit`` on: they are what keeps every target inside the client's 0.10 m / 0.35 rad (I4, I5).
    """

    # ---- page controls and mode switches ----
    # RELAXED[2026-11-01] E9 was: effective ~1.05 rows per tick in v1/v2 (new knob; 11-02: page offers 2.0/2.5/3.0, the page default is 3.0)
    lambda_max: float = (
        2.0  # the field's own default; the page always passes its value (for_page)
    )
    alpha: float = 0.005
    golden: bool = False  # today's behaviour, bit for bit (I2); not a page option
    # RELAXED[2026-11-01] E10 was: v1 TAU 0.2 s lead along the chord (only when lead is on; default off, frozen at 04:30)
    lead: bool = False
    swap_reanchor: bool = (
        False  # RTC swap reanchor; default frozen at 04:30 (section 9.4)
    )

    # ---- gripper window (section 3.2) ----
    # RELAXED[2026-11-01] E3 was: whole 10-row gripper check (accel.py:205-208) + GRIP_HOLD_S 1.0 s that restarts; 11-02 w_post 5 -> 3
    w_pre: int = 3
    w_post: int = 3

    # ---- speed caps (section 3.2); None = by alpha (0.005 / 0.01) ----
    # RELAXED[2026-11-01] E4 was: SPEED 0.25 m/s, hard cap 0.30 (accel.py:42, run_collect_accel.py:136); lag cap 0.265 m/s
    v_cap: float | None = None  # 0.30 / 0.35 m/s
    # RELAXED[2026-11-01] E5 was: ANG_SPEED 0.8 rad/s (accel.py:43); 1.0 rad/s only at alpha 0.01
    omega_cap: float | None = None  # 0.8 / 1.0 rad/s
    # RELAXED[2026-11-01] E6 was: CUT_MAX 2 mm, i.e. a_lat 0.1 m/s^2 (accel.py:65); 11-01 spec 0.6 / 1.5, 11-02 2.4 / 2.4
    a_lat: float | None = None  # 2.4 m/s^2 at both alphas (the validated maximum)
    # Two limits v3 deletes outright (no field, no code path), each tag on one line for the ledger check:
    # RELAXED[2026-11-01] E1 was: settle term 0.2 s / 5 mm (accel.py:46,51)
    # RELAXED[2026-11-01] E2 was: DEV 5 mm / ROT_MAX 0.15 rad shape rejection (accel.py:44-45)

    # ---- lag model (section 3.2): the arm trails the target by about D * v + 4 mm ----
    d_alpha: float | None = (
        None  # D_alpha s: 0.27 (alpha 0.005, measured) / 0.17 (alpha 0.01, modelled)
    )
    lag_safety: float = 0.94
    lag_offset_m: float = 0.004  # static lag offset, tracking_real/README.md:98
    lag_offset_rad: float = 0.01
    d_meas_min: float = 0.10  # D_meas clamp [s]
    d_meas_max: float = 0.40
    d_meas_window: int = 30  # moving ticks collected before D_meas may raise D_hat
    d_meas_speed: float = 0.03  # m/s: a tick counts as moving above this

    # ---- curvature (section 3.2); the estimator of accel.py:349-368 ----
    # A 15 mm arc read the policy's 2-3 mm row jitter as tight corners: the curvature cap then bound 35-58 % of all ticks
    # and held lambda near 1 (replay 11-02). 50 mm of arc each side measures the path's real turns.
    # RELAXED[2026-11-01] E13 was: curv_len 0.015 m (accel.py:349-368); 11-02 0.05 m
    curv_len: float = 0.05  # m of arc each side of a vertex
    curv_min_arc: float = 0.010  # m: shorter arcs give no curvature

    # ---- rates (section 3.3) ----
    # RELAXED[2026-11-01] E14 was: rate_up 0.6, jerk 0.3, a_tan_up 1.0 (spec 11-01); 11-02 0.8 / 0.45 / 1.5
    rate_up: float = 0.8  # lambda rise per tick
    jerk: float = 0.45  # lambda rise per tick^2
    a_tan_up: float = 1.5  # m/s^2 tangential acceleration bound on the rise
    d_dn_max: float = 0.5  # cap of the braking fall per tick, delta_dn = min(d_dn_max, d_dn_gain / g_lead)
    d_dn_gain: float = 0.9
    rate_dn_soft: float = 0.5  # fall per tick for soft reasons (soft leash, governor)

    # ---- leash and step limit (sections 3.4, 3.8) ----
    soft_leash: bool = True
    soft_m0: float = 0.05  # m: soft leash starts cutting (eps = |x_k - T_{k-1}|) ...
    soft_m1: float = 0.08  # ... and has cut everything above 1 here
    soft_rad0: float = 0.20
    soft_rad1: float = 0.28
    hard_leash: bool = True
    hard_leash_m: float = 0.08  # T_k against x_k; must stay below the client's 0.10 m
    hard_leash_rad: float = 0.28  # must stay below the client's 0.35 rad
    step_limit: bool = True
    step_m: float = 0.05  # between two issued targets (MAX_JUMP 0.05, unchanged)
    step_rad: float = 0.12  # per axis; tightened from MAX_ROT_JUMP 0.15 (E8, a tightening, not a relaxation)
    hold_reset_after: int = (
        3  # consecutive leash holds before reset() and a synchronous query
    )
    bisect_iters: int = 8
    pause_dp: float = 0.0005  # m and rad: a segment shorter than both is a policy pause, taken at lambda_max
    pause_dr: float = 0.005

    # ---- blending (section 3.7) ----
    blend: bool = True
    blend_min_ticks: int = 2
    blend_max_ticks: int = 5
    # A jump that 5 ticks cannot spread under the per-tick increment below (offsets above 0.08 m / 0.16 rad) is blended
    # over up to this many ticks instead, so that the scheduled increment stays at blend_step_m / blend_step_rad (G-F).
    # Set it equal to blend_max_ticks for the section 5 behaviour (clip at 5).
    blend_stretch_max_ticks: int = 8
    # Peak per-tick increment of the min-jerk offset. Section 5 allows up to 0.03 m / 0.06 rad (the validated maximum, kept as the
    # range); the default is tighter, 0.025 m / 0.05 rad, because G-F asks for a speed spike of at most half of what a hard switch
    # gives (the 50 mm step limit): half of 50 mm is 25 mm, and 30 mm per tick gave 0.55-0.56 (replay, 46 and 75 mm jumps). A tightening,
    # not a relaxation (no ledger row); it costs about 0.01 of duration ratio at jump 0 and 0.03 at 75 mm.
    blend_step_m: float = 0.025
    blend_step_rad: float = 0.05
    blend_skip_m: float = 0.002  # smaller jumps are not blended
    blend_skip_rad: float = 0.01

    # ---- reanchor (section 3.7) ----
    plain_reanchor: bool = True
    # RELAXED[2026-11-01] E7 was: MAX_SKIP 3 (accel.py:54); plain reanchors up to 4 rows, never past G-1
    plain_reanchor_rows: int = 4
    plain_reanchor_span: int = (
        5  # new rows 0..min(h-1, span) searched for the projection
    )
    swap_reanchor_rows: int = (
        3  # RTC swap reanchor: forward only, at most this many rows
    )
    swap_reanchor_span: int = 4
    reanchor_cos_min: float = 0.5  # local tangent cosine for either reanchor

    # ---- lead (section 3.5) ----
    lead_max_rows: float = 3.0

    # ---- gripper gate and settle (section 3.6) ----
    grip_gate: bool = True  # close (1 -> 0) only
    gate_dist_m: float = 0.008  # 11-02: tightened from 0.010 (closer arm before a close; a tightening, no tag)
    gate_speed: float = 0.05  # m/s
    gate_max_ticks: int = 5
    grip_settle: bool = (
        True  # lambda <= 1 after a close until the fingers have moved and stopped
    )
    # Close hold (real runs 2026-11-02): with the settle above lambda is only kept <= 1, and the policy's plan lifts 3-5
    # rows after a close; three closes of 13 accelerated ones caught nothing because the arm rose while the fingers had
    # not yet closed (finger opening 0.39-0.81 when the lift started; on a re-grasp the fingers start up to 0.5 s after
    # the command). With close_hold the phase does not advance after a close until that settle is done (the fingers
    # have moved and stopped, at least settle_min_s; settle_timeout_s at most, settle_unknown_s without a reading).
    close_hold: bool = True
    settle_min_s: float = 0.3
    settle_aperture_delta: float = 0.02
    settle_stable_ticks: int = 2
    settle_stable_eps: float = 0.01
    settle_timeout_s: float = 1.0
    settle_unknown_s: float = (
        0.8  # aperture unknown (plain without a stage capture): time only
    )
    # Open settle (review 11-02 m1): after an open (issued value 0 -> 1) lambda <= 1 until the fingers have visibly opened
    # (aperture up by settle_aperture_delta) and open_settle_min_s has passed, or open_settle_timeout_s; with no aperture
    # reading, open_settle_unknown_s. Gripper model (fr3_env, arms.GripperModel, lag_robot_server): the fingers start
    # 0.2 s after the command and travel for 0.5 s; the clock starts at the tick the issued value first reads open, about
    # one tick after the command. 0.3 s is then ~0.4 s after the command, 0.2 s into the travel (the model has opened by
    # ~0.13 of aperture, the 0.02 test passes one tick earlier): the arm does not pull away while the fingers still touch
    # the object. With w_post 3 the window already holds lambda at 1 to ~0.2 s after that tick, so the settle adds about one
    # tick per open. The 0.75 s command throttle needs no time here: while it holds the open back, the issued value lags the
    # command and lambda is held at 1 by that rule. 0.8 s = start delay + travel + one tick (fingers that have not moved by
    # then are held by something waiting does not fix); 0.5 s without a reading = start delay + 60 % of the travel.
    open_settle: bool = True
    open_settle_min_s: float = 0.3
    open_settle_timeout_s: float = 0.8
    open_settle_unknown_s: float = 0.5

    # ---- inference governor, early trigger, delay bookkeeping (section 3.9) ----
    governor: bool = True
    governor_margin_rows: float = 0.5
    early_trigger: bool = True
    delay_history: int = 8

    # ---- VLASH grasp re-plan (2026-11-02 real run) ----
    # The FR3 policy predicts actions as deltas from the state it is given, and VLASH gives it the planned state at the end
    # of the queue (chunk[-1]) together with a camera image of where the arm is now. Accelerated, the two drift apart: on
    # the real arm 45-90 mm at the median, 79-95 mm before the first grasp of three green-plate runs that all missed, and
    # in replay 68 mm (p50) / 96 mm (p90) at the prefetches whose reply brings a gripper close, against 16 / 27 mm (max 31)
    # without acceleration. A reply that brings a close (open -> close against the last queued row) and was requested
    # with a state farther than this from the measured arm is dropped and asked again synchronously from the measured
    # state, so the grasp is planned from a state that matches the image. 0 turns it off; golden forces 0.
    grasp_replan_m: float = 0.035

    def __post_init__(self) -> None:
        if self.golden:
            for name, value in _GOLDEN_FORCED.items():
                object.__setattr__(self, name, value)
        # alpha: canonical table key
        if not _is_real(self.alpha) or not any(
            math.isclose(float(self.alpha), a, rel_tol=0, abs_tol=1e-9) for a in ALPHAS
        ):
            raise ValueError(
                f"PhaseParams.alpha={self.alpha!r} must be one of {ALPHAS} and match `set_clip.py --show`"
            )
        object.__setattr__(
            self,
            "alpha",
            next(
                a
                for a in ALPHAS
                if math.isclose(float(self.alpha), a, rel_tol=0, abs_tol=1e-9)
            ),
        )
        for f in dataclasses.fields(self):
            name, value = f.name, getattr(self, f.name)
            if name == "alpha":
                continue
            if name in _BOOL_FIELDS:
                if not isinstance(value, (bool, np.bool_)):
                    raise ValueError(f"PhaseParams.{name}={value!r} must be a bool")
                object.__setattr__(self, name, bool(value))
                continue
            if value is None and name in _OPTIONAL_FIELDS:
                continue
            if not _is_real(value):
                raise ValueError(
                    f"PhaseParams.{name}={value!r} must be a finite number"
                )
            if name in _INT_FIELDS:
                if float(value) != int(value):
                    raise ValueError(f"PhaseParams.{name}={value!r} must be an integer")
                value = int(value)
            else:
                value = float(value)
            lo, hi, lo_open = _RANGES[name]
            if not ((value > lo) if lo_open else (value >= lo)) or not value <= hi:
                shape = ("(" if lo_open else "[") + f"{lo}, {hi}]"
                raise ValueError(
                    f"PhaseParams.{name}={value!r} is outside {shape} (design section 5)"
                )
            object.__setattr__(self, name, value)
        if not self.soft_m0 < self.soft_m1 or not self.soft_rad0 < self.soft_rad1:
            raise ValueError(
                "PhaseParams soft leash needs soft_m0 < soft_m1 and soft_rad0 < soft_rad1"
            )
        if (
            not self.hard_leash_m < CLIENT_MAX_M
            or not self.hard_leash_rad < CLIENT_MAX_RAD
        ):
            raise ValueError(
                f"PhaseParams hard leash {self.hard_leash_m} m / {self.hard_leash_rad} rad must stay below the client's "
                f"{CLIENT_MAX_M} m / {CLIENT_MAX_RAD} rad"
            )
        if self.d_meas_min > self.d_meas_max:
            raise ValueError("PhaseParams.d_meas_min must not exceed d_meas_max")
        if self.blend_min_ticks > self.blend_max_ticks:
            raise ValueError(
                "PhaseParams.blend_min_ticks must not exceed blend_max_ticks"
            )
        if self.blend_stretch_max_ticks < self.blend_max_ticks:
            raise ValueError(
                "PhaseParams.blend_stretch_max_ticks must not be below blend_max_ticks"
            )
        if self.settle_min_s > self.settle_timeout_s:
            raise ValueError(
                "PhaseParams.settle_min_s must not exceed settle_timeout_s"
            )
        if self.open_settle_min_s > self.open_settle_timeout_s:
            raise ValueError(
                "PhaseParams.open_settle_min_s must not exceed open_settle_timeout_s"
            )
        if not self.golden and not (self.hard_leash and self.step_limit):
            raise ValueError(
                "PhaseParams: hard_leash and step_limit stay on outside golden mode (invariants I4, I5); "
                "use PhaseParams.golden_params() for today's behaviour"
            )

    # ---- constructors ----
    @staticmethod
    def for_page(lambda_max: float, alpha: float) -> PhaseParams:
        """The two page controls with every other value at its default; ValueError outside section 5's ranges.

        lambda_max must be in [1.0, 3.0] and alpha one of {0.005, 0.01} (the entry point rejects the same way).
        """
        return PhaseParams(lambda_max=lambda_max, alpha=alpha)

    @staticmethod
    def golden_params() -> PhaseParams:
        """Golden mode (section 3.11): lambda_max 1, every feature off. The mode adapters then reproduce today's
        RtcChunker / VlashChunker / plain ``chunk[:h]`` bit for bit (I2). Not a page option."""
        return PhaseParams(golden=True)

    @staticmethod
    def from_dict(d: dict) -> PhaseParams:
        """Inverse of ``as_dict`` (a logged header back to parameters); unknown keys raise TypeError."""
        return PhaseParams(**d)

    # ---- derived values ----
    def lag_caps(self, d_hat: float) -> tuple[float, float]:
        """(v_lag m/s, omega_lag rad/s) for a lag estimate ``d_hat`` seconds (section 3.2)."""
        d_hat = float(d_hat)
        v_lag = self.lag_safety * (self.hard_leash_m - self.lag_offset_m) / d_hat
        w_lag = self.lag_safety * (self.hard_leash_rad - self.lag_offset_rad) / d_hat
        return v_lag, w_lag

    def derived(self, hz: float = 10.0) -> dict:
        """Effective values after the per-alpha defaults: D_alpha, v_cap, omega_cap, v_lag, omega_lag, a_lat, g_lead,
        d_dn. v_lag / omega_lag are the raw lag caps at D_alpha (the engine also applies v_cap / omega_cap and may
        raise D to D_meas); g_lead = D_alpha / dt; d_dn = the braking fall per tick (0.5 with lead off, else
        min(0.5, 0.9 / g_lead)). JSON-safe."""
        d_tab, v_tab, w_tab, a_tab = _BY_ALPHA[self.alpha]
        d_alpha = d_tab if self.d_alpha is None else self.d_alpha
        v_lag, omega_lag = self.lag_caps(d_alpha)
        g_lead = d_alpha * float(hz)  # D_alpha / dt
        d_dn = (
            min(self.d_dn_max, self.d_dn_gain / g_lead) if self.lead else self.d_dn_max
        )
        return {
            "D_alpha": d_alpha,
            "v_cap": v_tab if self.v_cap is None else self.v_cap,
            "omega_cap": w_tab if self.omega_cap is None else self.omega_cap,
            "v_lag": v_lag,
            "omega_lag": omega_lag,
            "a_lat": a_tab if self.a_lat is None else self.a_lat,
            "g_lead": g_lead,
            "d_dn": d_dn,
        }

    def as_dict(self) -> dict:
        """Every field by name, JSON-safe (numbers, bools, None): the session header's ``params``."""
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


@dataclasses.dataclass(frozen=True, eq=False)
class Measured:
    """What the client measured this tick. ``pose`` is coerced to float32 (7,) like rc's ``state``."""

    pose: (
        np.ndarray
    )  # float32 (7,), == rc `state` (cartesian_position; [6] = issued gripper)
    aperture: (
        float | None
    )  # obs["robot_state"]["gripper_position"] (fr3_env.py:498); None if unknown
    t: float  # time.monotonic() of this tick

    def __post_init__(self) -> None:
        pose = np.asarray(self.pose, dtype=np.float32)
        if pose.shape != (7,):
            raise ValueError(f"Measured.pose must have shape (7,), got {pose.shape}")
        object.__setattr__(self, "pose", pose)


@dataclasses.dataclass(frozen=True)
class Inflight:
    """The scheduler's view of the request in flight (None to ``begin`` = no request in flight)."""

    age_ticks: int  # ticks since the request was sent
    d_hat_ticks: int  # expected total delay in ticks (section 3.9)
    done: bool  # the reply has arrived but has not been swapped in yet


class NonFiniteChunk(ValueError):
    """A chunk with NaN / inf. Raised straight out of set_chunk / swap / append / splice_plain (I10): the episode
    aborts through the collector's exception path and the arm stays on the last accepted target."""


@dataclasses.dataclass(frozen=True)
class Proposal:
    """Result of ``PhaseEngine.begin``: the tick's candidate, nothing committed yet."""

    lam: float  # rows to advance this tick (after rates)
    s_cand: float  # candidate reference phase s + lam, landed on the next gripper change / chunk end
    past_end: bool  # s + lam would pass the current chunk's last executable row: collect, block or query first
    binding: str = ""  # which term bound lam (a key of summary()["binding"]); "" = none (lambda_max)


@dataclasses.dataclass(frozen=True, eq=False)
class TickOut:
    """Result of ``commit`` / ``tick``: the target for this tick and why."""

    target: np.ndarray  # float32 (7,), finite, inside the workspace clamps (I7)
    s: float  # global reference phase after this tick
    u: float  # chunk-local reference phase (what the policy believes)
    e: float  # chunk-local issued phase, e >= u (I1, I6)
    lam: float  # rows advanced this tick (can be < 1 only for leash / governor / gate / settle / end)
    cap: float  # the per-segment lambda cap before rates and leash
    hold: bool = False  # the previous target was re-sent unchanged (leash hold)
    blend_w: float = (
        1.0  # min-jerk weight of the new path while blending, 1.0 = no blend active
    )
    dp: float = 0.0  # |target - measured| in m, rc:706-707 formula
    dr: float = 0.0  # max wrapped angle difference in rad
    grip_event: dict | None = (
        None  # {s, kind, lam_at_cmd, arm_err_m, arm_speed_m_s, gate_wait_ticks, gate_timeout, settle_s}
    )
    binding: str = ""
    reset: bool = False  # this hold was the hold_reset_after-th in a row: the engine dropped its plan (has_rows() is False)


BINDINGS = (
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
    "grasp",
)  # the keys of summary()["binding"] (section 8): which term bound lambda on a tick
_SNAP = 1e-9  # a phase this close to an integer row IS that row (float sums like (o + G) - o must not drop a row)
_GATE_STOP = (
    1e-3  # the close gate parks the phase this many rows before the closing row
)
# The close gate's wait belongs to one close: when the closing row it waits at lies more than this many gate_dist_m from
# the row where the wait began (a reload moved the close: plain re-query, VLASH sync, an RTC swap, a jump between
# chunks), the new close gets its own wait, so a wait spent at one place never releases a close somewhere else with the
# arm still on its way (review of the 11-02 fixes: 13-45 mm at the release). Under a total cap of this many
# gate_max_ticks gate-held ticks across such moves, so a close that moves on every reload still goes out. The cap
# releases a close only after _GATE_AT_PLACE waits at its current place (a fresh place gets a look at the arm first;
# plain with 75 mm chunk jumps: close error p90 28.8 -> 16.8 mm at the same speed), or unconditionally at twice the cap.
_GATE_MOVED = 2.0
_GATE_TOTAL = 2
_GATE_AT_PLACE = 2
_TICK_GAP_S = (
    1.0  # s without a tick after which a far-away last target is dropped (new episode)
)
_MIN_ADV = 0.02  # rows: a leash-limited advance below this counts as a hold (a creep is no progress)
_INF = math.inf
_MAX_RECORDS = (
    200_000  # undrained trace lines kept before the oldest are dropped (and counted)
)


def _b(g) -> int:
    """The client's gripper binarization b(g) = (g >= 0.5)."""
    return 1 if g >= GRIP_THRESHOLD else 0


def _snap(x: float) -> float:
    """Round a phase onto the integer row it is within 1e-9 of."""
    r = round(x)
    return float(r) if abs(x - r) < _SNAP else x


def _floor(x: float) -> int:
    """floor of a snapped phase."""
    return math.floor(_snap(x))


def _minjerk(sigma: float) -> float:
    """w(sigma) = 10 s^3 - 15 s^4 + 6 s^5 on [0, 1] (stream:40-42); peak slope MINJERK_PEAK = 1.875."""
    s = 0.0 if sigma < 0.0 else (min(sigma, 1.0))
    return s * s * s * (10.0 - 15.0 * s + 6.0 * s * s)


def vertex_curvature(
    points: np.ndarray, curv_len: float = 0.015, min_arc: float = 0.010
) -> np.ndarray:
    """Curvature at each vertex of a polyline (1/m), the estimator of accel.py:349-368.

    The turning angle between the chords to the points ``curv_len`` of arc before and after the vertex, over the mean
    chord length; the first and last vertex are 0. A vertex whose two chords together are shorter than ``min_arc`` (a
    policy pause, where a turn means nothing) gets 0, as does one with a zero chord.
    """
    P = np.asarray(points, dtype=np.float64)
    n = len(P)
    kap = np.zeros(n)
    if n < 3:
        return kap
    dP = np.diff(P, axis=0)
    ln = np.linalg.norm(dP, axis=1)
    total = float(ln.sum())
    if total < 1e-12:
        return kap
    cum = np.concatenate([[0.0], np.cumsum(ln)])
    idx = np.arange(1, n - 1)
    su = np.stack(
        [np.maximum(0.0, cum[idx] - curv_len), np.minimum(total, cum[idx] + curv_len)]
    )  # (2, n-2) arc positions
    j = np.clip(np.searchsorted(cum, su, side="right") - 1, 0, n - 2)
    lj = ln[j]
    fr = np.where(lj > 1e-12, (su - cum[j]) / np.maximum(lj, 1e-12), 0.0)
    pt = P[j] + fr[..., None] * dP[j]
    va, vb = P[idx] - pt[0], pt[1] - P[idx]
    la, lb = np.linalg.norm(va, axis=1), np.linalg.norm(vb, axis=1)
    ok = (la > 1e-9) & (lb > 1e-9) & (la + lb >= min_arc)
    cos = np.einsum("ij,ij->i", va, vb) / np.where(ok, la * lb, 1.0)
    theta = np.arccos(np.clip(cos, -1.0, 1.0))
    kap[idx] = np.where(ok, theta / (0.5 * (la + lb)), 0.0)
    return kap


def _rot_diff(a: np.ndarray, b: np.ndarray) -> float:
    """max_i |wrap(a - b)_i| of two euler triples: the client's raw_rot.max() norm (nan for a non-finite input)."""
    with np.errstate(invalid="ignore"):
        return float(np.abs(wrap_to_pi(np.asarray(a, np.float64) - b)).max())


def _interp(R: np.ndarray, D: np.ndarray, lo: int, phi: float) -> np.ndarray:
    """X(phi) of section 3.1 on stored rows R (first row = chunk-local index lo, D = per-segment [dp, wrapped de]):
    a row copy at an integer phase (bit-exact), the linear blend in between, the first / last row outside."""
    a = _snap(phi) - lo
    n = len(R)
    if a <= 0.0 or n == 1:
        return R[0].copy()
    if a >= n - 1:
        return R[n - 1].copy()
    j = int(a)
    f = a - j
    out = R[j].copy()
    if f != 0.0:
        out[:6] += f * D[j]
    return out


def _segment_deltas(R: np.ndarray) -> np.ndarray:
    """Per-segment [dp (3), wrap(de) (3)] of rows R: what _interp adds."""
    return np.concatenate(
        [np.diff(R[:, :3], axis=0), wrap_to_pi(np.diff(R[:, 3:6], axis=0))], axis=1
    )


class PhaseEngine:
    """The phase-clock core. One instance per client run; call order each tick: ``begin`` then ``commit`` (or
    ``tick``), then ``executed`` / ``rejected`` once the client's own clamp has answered. Not thread-safe.

    Section 3.4 gives the per-tick order, 3.7 the chunk joins, 3.8 the leash. The phase ``s`` (rows, global, kept
    across chunks) advances by lambda per tick; the chunk-local reference phase is ``u = s - origin``. Row ``j`` of
    the loaded plan is the policy's own row; ``lo`` is the chunk-local index of the first stored row (-1 while plain
    keeps the last issued target as virtual row -1). Hook order for the schedulers:

    * RTC tick:  ``swap`` (reply arrived) / ``set_chunk`` (none loaded), ``begin``, ``commit``, ``prev_rows`` (request).
    * VLASH tick: ``append`` (prefetch arrived) / ``set_chunk``, ``begin``, ``commit``, ``future_row`` (request).
    * plain: ``splice_plain`` at the query, then ``tick`` every loop iteration.

    Anything that changes the plan after ``begin`` makes ``commit`` recompute its proposal from the same measurement.

    Leash holds: when no advance fits the hard leash and the step limit (or only a creep under 0.02 rows does), the
    last target is re-sent (``TickOut.hold``). The ``hold_reset_after``-th hold in a row drops the plan inside the
    engine (``TickOut.reset`` is True, ``has_rows()`` False) so that a scheduler which checks ``has_rows()``
    queries again; a tick with no plan re-sends the last target as well. The last target is only re-anchored to the
    measured pose on the very first tick, or after more than a second without a tick if the arm is then outside the
    hard leash (the environment was reset). A last target that the arm itself has left behind (pushed away, bounced
    off a contact) is moved back toward the measured pose just inside the leash, by at most one step limit per tick
    (``_recovery_target``, invariant I5 kept), never jumped; a stuck arm whose target is still inside the leash just
    holds. The close gate's wait survives plan reloads and ``reset`` (it ends when the close goes out, the command
    changes or the plan has no close ahead).

    Additions to the section 6 interface (all optional, none changes a frozen signature): ``new_episode``,
    ``set_workspace`` (the client's clamp as a predicate, invariant I7), ``bump`` / ``note_value`` / ``note`` (the
    schedulers' counters and trace lines), ``TickOut.reset``, ``vertex_curvature`` and ``BINDINGS``.
    """

    def __init__(self, params: PhaseParams, hz: float = 10.0):
        if not _is_real(hz) or hz <= 0:
            raise ValueError(f"PhaseEngine hz={hz!r} must be a positive number")
        self.params = params
        self.hz = float(hz)
        self.dt = 1.0 / self.hz
        self._derived = params.derived(self.hz)
        self._golden = bool(params.golden)
        self._lam_max = float(params.lambda_max)
        self._workspace_hit = None
        self.new_episode()

    # ------------------------------------------------------------------ lifecycle
    def new_episode(self) -> None:
        """Forget everything about the previous episode (plan, last target, measurements, statistics). ``reset``
        keeps the last issued target and the phase; this does not. Call it when the arm has been moved by hand or
        by the environment reset."""
        self._R = None  # (N, 7) float64: the plan rows from chunk-local index lo
        self._D = (
            None  # (N-1, 6) float64: per-segment [dp, wrapped de] for interpolation
        )
        self._lo = 0
        self._o = 0.0  # origin: global phase of chunk-local row 0
        self._u_end = 0.0  # last executable chunk-local row
        self._raw_last = None  # float32 (7,): newest chunk's last row exactly
        self._chunk_id = -1
        self._chunk_kind = ""
        self._geom_ver = 0
        self._built_g = None
        self._ws_shrunk = False
        self._event_k = -1
        self._row0_change = None  # None: not decided yet (the commanded gripper value was not known at load)
        self._row0_ahead = False
        self._nseg = 0
        self._dpl = self._drl = self._lv = self._lw = self._lk = []
        self._pause = self._win = []
        self._G: list[int] = []  # chunk-local gripper change rows
        self._prof = None
        self._prof_key = None
        # phase state
        self._s = 0.0
        self._lam = 1.0
        self._dlam = 0.0
        self._e_off = 0.0
        self._fresh = False
        self._T_prev = None
        self._dir_prev = None
        self._g_cmd = None
        self._blend = None
        self._blend_pending = False
        # close gate: wait ticks and the time of the first one; both survive plan reloads and reset() (review 11-02 M2)
        # while the close stays where the wait began (_gate_pos); _gate_total counts gate-held ticks across moves
        self._gate_wait = 0
        self._gate_since = None
        self._gate_pos = None
        self._gate_total = 0
        self._holds = 0
        self._recov_run = (
            0  # leash recoveries since the last advance the hard leash did not cut
        )
        # measurement
        self._m = None
        self._inflight = None
        self._x64 = None
        self._x_prev = None
        self._t_prev = None
        self._speed = None
        self._eps = 0.0
        self._rho = 0.0
        self._gamma = 1.0
        self._observed = None
        self._last_gi = None
        self._dmeas: collections.deque = collections.deque(
            maxlen=self.params.d_meas_window
        )
        self._dhat = self._derived["D_alpha"]
        self._settle_active = False
        self._settle_t0 = 0.0
        self._settle_ap0 = None
        self._settle_ap_prev = None
        self._settle_moved = False
        self._settle_stable = 0
        self._settle_event = None
        self._osettle_active = False
        self._osettle_t0 = 0.0
        self._osettle_ap0 = None
        self._osettle_event = None
        # proposal / commit bookkeeping
        self._prop = None
        self._prop_valid = False
        self._snap_state = None
        self._k = 0
        # trace and statistics
        self._records: list[dict] = []
        self._st: dict = collections.defaultdict(int)
        self._lists: dict = collections.defaultdict(list)
        self._bind_count = {k: 0 for k in BINDINGS}
        self._lams: list[float] = []
        self._rows_adv = 0.0
        self._dp_max = self._dr_max = self._step_max = self._rstep_max = 0.0
        self._blend_step_max = 0.0
        self._events: list[dict] = []

    def reset(self) -> None:
        """Drop rows, blend and hold state; lambda -> 1; the phase s is kept (so a reset never rewinds). The close gate's
        wait is kept: a plan reloaded after a reset that lands on the same close goes on counting toward the timeout
        (otherwise a scheduler that resets every tick, or a plain placeholder shorter than the wait, never closes)."""
        self._R = None
        self._D = None
        self._G = []
        self._prof = None
        self._prof_key = None
        self._blend = None
        self._blend_pending = False
        self._holds = 0
        self._recov_run = 0
        self._lam = 1.0
        self._dlam = 0.0
        self._e_off = 0.0
        self._fresh = False
        self._prop_valid = False
        self._prop = None
        self._snap_state = None
        self._geom_ver += 1

    def has_rows(self) -> bool:
        """True when a chunk is loaded (so a scheduler knows whether a synchronous query is due)."""
        return self._R is not None

    def set_workspace(self, hits_clamp) -> None:
        """Optional (invariant I7): ``hits_clamp(target_float32_7) -> bool`` says whether the client's own workspace
        clamp (min_z / max_xy / rpy ranges) would change the pose of ``target``. The engine then shrinks a blend
        offset until the offset itself causes no clamp; a path the policy itself drives into a clamp is left alone.
        The caller builds the predicate from the client's ``Args`` (this module never imports the client)."""
        self._workspace_hit = hits_clamp

    def bump(self, name: str, n: int = 1) -> None:
        """Count a scheduler event into summary() (starved_ticks, early_triggers, plain_end_holds, ...)."""
        self._st[name] += int(n)

    def note(self, record: dict) -> None:
        """Append a scheduler-made line (a request's delay, a chunk's latency, ...) to the trace."""
        self._emit(dict(record))

    def note_value(self, name: str, value) -> None:
        """Append a number to a summary() list (delays_rows, ...)."""
        self._lists[name].append(float(value))

    # ------------------------------------------------------------------ plan loading
    @staticmethod
    def _check_rows(rows) -> tuple[np.ndarray, np.ndarray]:
        r32 = np.asarray(rows, dtype=np.float32)
        if r32.ndim != 2 or r32.shape[1] != 7 or len(r32) < 1:
            raise ValueError(f"expected an (H, 7) action chunk, got {r32.shape}")
        if not np.isfinite(r32).all():
            raise NonFiniteChunk(
                "action chunk has non-finite values; refusing to execute it"
            )
        return r32.astype(np.float64), r32

    def _g_known(self) -> bool:
        return self._g_cmd is not None or self._last_gi is not None

    def _g_ref(self) -> int:
        if self._g_cmd is not None:
            return self._g_cmd
        return self._last_gi if self._last_gi is not None else 1

    def _install(self, R64, lo, origin, u_end, raw_last, kind, keep_row0=False) -> None:
        keep = (self._row0_ahead, self._row0_change)
        self._R = R64
        self._lo = int(lo)
        self._o = float(origin)
        self._u_end = float(u_end)
        self._raw_last = raw_last
        self._chunk_kind = kind
        self._chunk_id += 1
        self._geom_ver += 1
        self._prof = None
        self._prop_valid = False
        if keep_row0:  # a continuation of the same plan: row 0 keeps whatever it was
            self._row0_ahead, self._row0_change = keep
        else:
            self._row0_ahead = self._o > self._s - _SNAP  # row 0 not yet issued
            self._row0_change = None
        self._rebuild(self._g_ref())

    def _rebuild(self, g_ref: int) -> None:
        """Segment geometry, static caps, change rows and gripper windows of the stored plan (O(N), on any change)."""
        p, dt = self.params, self.dt
        R, lo = self._R, self._lo
        n = len(R)
        self._built_g = g_ref
        self._prof = None
        self._prof_key = None
        if n >= 2:
            dP = np.diff(R[:, :3], axis=0)
            dE = wrap_to_pi(np.diff(R[:, 3:6], axis=0))
            dpl = np.linalg.norm(dP, axis=1)
            drl = np.abs(dE).max(axis=1)
            self._D = np.concatenate([dP, dE], axis=1)
        else:
            dpl = drl = np.zeros(0)
            self._D = np.zeros((0, 6))
        kap = vertex_curvature(R[:, :3], p.curv_len, p.curv_min_arc)
        kseg = np.maximum(kap[:-1], kap[1:]) if n >= 2 else np.zeros(0)
        d = self._derived
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            lv = np.where(dpl > 0, d["v_cap"] * dt / np.maximum(dpl, 1e-300), _INF)
            lw = np.where(drl > 0, d["omega_cap"] * dt / np.maximum(drl, 1e-300), _INF)
            curved = (kseg > 1e-9) & (dpl > 1e-12)
            lk = np.where(
                curved,
                np.sqrt(d["a_lat"] / np.where(curved, kseg, 1.0))
                * dt
                / np.where(curved, dpl, 1.0),
                _INF,
            )
        self._nseg = max(n - 1, 0)
        self._dpl, self._drl = dpl.tolist(), drl.tolist()
        self._lv, self._lw, self._lk = lv.tolist(), lw.tolist(), lk.tolist()
        self._pause = ((dpl < p.pause_dp) & (drl < p.pause_dr)).tolist()
        # gripper change rows (section 3.2): b(g_j) != b(g_{j-1}) for j >= 1, and row 0 against the commanded value
        bits = (R[:, 6] >= GRIP_THRESHOLD).astype(np.int8)
        G = [lo + a for a in range(1, n) if lo + a >= 1 and bits[a] != bits[a - 1]]
        if self._row0_change is None and self._g_known():
            self._row0_change = bool(
                self._row0_ahead and lo <= 0 < lo + n and bits[-lo] != g_ref
            )
        if self._row0_change:
            G.insert(0, 0)
        self._G = sorted(set(G))
        win = [False] * self._nseg
        for g in self._G:
            for j in range(g - p.w_pre, g + p.w_post):
                a = j - lo
                if 0 <= a < self._nseg:
                    win[a] = True
        self._win = win

    def _profile(self, dhat: float):
        """Per-segment braked lambda caps and their labels for the lag estimate ``dhat`` (cached)."""
        key = (self._geom_ver, dhat)
        if self._prof_key == key:
            return self._prof
        p, dt, lm = self.params, self.dt, self._lam_max
        v_lag, w_lag = p.lag_caps(dhat)
        soft, kind = [], []
        dpl, drl, lv, lw, lk = self._dpl, self._drl, self._lv, self._lw, self._lk
        for a in range(self._nseg):
            best, k = lm, "max"
            x = lv[a]
            if x < best:
                best, k = x, "v"
            x = lw[a]
            if x < best:
                best, k = x, "omega"
            if dpl[a] > 0.0:
                x = v_lag * dt / dpl[a]
                if x < best:
                    best, k = x, "lag"
            if drl[a] > 0.0:
                x = w_lag * dt / drl[a]
                if x < best:
                    best, k = x, "lag"
            x = lk[a]
            if x < best:
                best, k = x, "kappa"
            if self._pause[a]:
                best, k = lm, "max"  # a policy pause is taken at lambda_max
            best = max(best, 1.0)  # I8: every cap has a floor of 1
            best = min(best, lm)
            if self._win[a] and best > 1.0:
                best, k = 1.0, "window"
            soft.append(best)
            kind.append(k)
        c, ck = soft[:], kind[:]
        d2 = 2.0 * self._derived["d_dn"]
        for a in range(
            self._nseg - 2, -1, -1
        ):  # the braking pass (section 3.3); the chunk end is free
            lim = math.sqrt(c[a + 1] * c[a + 1] + d2)
            if lim < c[a]:
                c[a], ck[a] = lim, ck[a + 1]
        self._prof = (c, ck, soft)
        self._prof_key = key
        return self._prof

    def _seg_range(self, j0: int, j1: int):
        """Chunk-local executable segment indices clipped to what exists, as plan indices (a0, a1) or None."""
        if self._nseg == 0:
            return None
        jmax = min(self._lo + self._nseg - 1, int(self._u_end) - 1)
        if jmax < self._lo:
            return None
        j0 = min(max(j0, self._lo), jmax)
        j1 = min(max(j1, j0), jmax)
        return j0 - self._lo, j1 - self._lo

    def _cmin(self, prof, j0: int, j1: int):
        rng = self._seg_range(j0, j1)
        if rng is None:
            return self._lam_max, "max"
        c, ck, _ = prof
        a0, a1 = rng
        best, k = c[a0], ck[a0]
        for a in range(a0 + 1, a1 + 1):
            if c[a] < best:
                best, k = c[a], ck[a]
        return best, k

    def _lam_hat(self, u: float, lam_prev: float, prof):
        """Section 3.4 step 1: the lowest braked cap over the segments the move could cover. Two passes: the first
        over the move at the previous lambda, the second over the larger of that and the first result, so the
        segments the final move touches are always inside the range that was minimised."""
        j0 = _floor(u)
        l1, _ = self._cmin(prof, j0, _floor(u + lam_prev))
        return self._cmin(prof, j0, _floor(u + max(lam_prev, l1)))

    def _X(self, phi: float) -> np.ndarray:
        """X(phi): the plan interpolated at chunk-local phase phi (float64 (7,)); the gripper is the floor row's."""
        return _interp(self._R, self._D, self._lo, phi)

    def _grip_raw(self, u: float) -> float:
        """The raw gripper value of the floor row at phase u (no interpolation, no blending: I3)."""
        a = _floor(u) - self._lo
        a = 0 if a < 0 else (len(self._R) - 1 if a >= len(self._R) else a)
        return float(self._R[a, 6])

    @staticmethod
    def _project(R, D, lo, phis, ref_p):
        """Closest point of the polyline X(phis)[:3] to ref_p: (phase, distance, unit tangent or None)."""
        pts = np.array([_interp(R, D, lo, f)[:3] for f in phis])
        best = (_INF, phis[0], None)
        for i in range(len(phis) - 1):
            a, b = pts[i], pts[i + 1]
            ab = b - a
            L2 = float(ab @ ab)
            t = 0.0 if L2 < 1e-18 else min(max(float((ref_p - a) @ ab) / L2, 0.0), 1.0)
            dist = float(np.linalg.norm(a + t * ab - ref_p))
            if dist < best[0] - 1e-15:
                ln = math.sqrt(L2)
                best = (
                    dist,
                    phis[i] + t * (phis[i + 1] - phis[i]),
                    ab / ln if ln > 1e-9 else None,
                )
        return best[1], best[0], best[2]

    @staticmethod
    def _phi_grid(a0: float, a1: float) -> list[float]:
        """Phases a0, the integer rows strictly between, a1: the vertices of the plan polyline on [a0, a1]."""
        out = [a0]
        out += [
            float(r)
            for r in range(math.floor(a0 + _SNAP) + 1, math.floor(a1 + _SNAP) + 1)
            if a0 < r < a1
        ]
        if a1 > a0:
            out.append(a1)
        return out

    def _start_blend(self, e_prev_new: float) -> int:
        """Offset blend from the last issued target onto the plan just loaded (section 3.7). Returns n_b (0: none).
        delta = T_{k-1} - X(e_prev), so the blend starts at exactly the last target (T(0) = T_{k-1}, section 3.8)."""
        self._blend = None
        self._blend_pending = False
        p = self.params
        if self._golden or not p.blend:
            return 0
        if self._T_prev is None:
            self._blend_pending = (
                True  # no target yet: the first begin() blends from the measured pose
            )
            return 0
        ref = self._X(max(e_prev_new, 0.0))
        delta = np.empty(6)
        delta[:3] = np.asarray(self._T_prev[:3], np.float64) - ref[:3]
        delta[3:] = wrap_to_pi(np.asarray(self._T_prev[3:6], np.float64) - ref[3:6])
        dp = float(np.linalg.norm(delta[:3]))
        dr = float(np.abs(delta[3:]).max())
        if not (dp >= p.blend_skip_m or dr >= p.blend_skip_rad):
            return 0
        n_need = math.ceil(
            max(
                MINJERK_PEAK * dp / p.blend_step_m,
                MINJERK_PEAK * dr / p.blend_step_rad,
            )
            - 1e-9
        )
        n_b = int(min(max(n_need, p.blend_min_ticks), p.blend_max_ticks))
        if n_need > p.blend_max_ticks:
            # five ticks would carry more than blend_step_m / blend_step_rad per tick: spread it over more, up to the stretch cap
            n_b = int(min(n_need, p.blend_stretch_max_ticks))
            if n_b > p.blend_max_ticks:
                self._st["blends_stretched"] += 1
        self._blend = {"delta": delta, "n": n_b, "k": 0.0}
        self._st["blends"] += 1
        return n_b

    def set_chunk(self, rows: np.ndarray, kind: str) -> None:
        """Synchronous (re)load of the chunk (first tick, after reset, plain): ``rows`` (H, 7), origin = this tick's
        candidate. ``kind`` is a short label kept in the trace ("sync", "plain", ...). Gripper changes are
        re-detected against the commanded value. Raises NonFiniteChunk on NaN / inf."""
        r64, r32 = self._check_rows(rows)
        self._install(r64, 0, self._s + 1.0, len(r64) - 1, r32[-1].copy(), kind)
        self._fresh = True  # the first tick lands exactly on row 0 (lambda 1), blended from the last target
        self._e_off = 0.0
        self._start_blend(0.0)
        self._record_chunk(r32, kind, s_obs=self._s + 1.0)

    def swap(self, rows: np.ndarray, s_obs: float) -> dict:
        """RTC: a reply for the observation taken at reference phase ``s_obs`` arrives; its origin is s_obs, so
        u_new = s_cand - s_obs. Blends the offset away, optionally reanchors (swap_reanchor) and always records the
        signed, untruncated ``anchor_rows``. Returns {"d_rows", "jump_m", "anchor_rows", "n_blend"}. Raises
        NonFiniteChunk."""
        r64, r32 = self._check_rows(rows)
        s_obs = float(s_obs)
        if not math.isfinite(s_obs):
            raise ValueError(f"swap: s_obs={s_obs!r} must be finite")
        p = self.params
        H = len(r64)
        lam_est = 1.0 if self._golden else max(self._lam, 1.0)
        s_est = self._s + lam_est
        u_new = s_est - s_obs
        old = self._R is not None
        old_last = (self._lo + len(self._R) - 1) if old else 0
        jump = anchor = None
        u_p = u_new
        tangent_old = tan_new = None
        # the old reference point and the new polyline, before the plan is replaced
        ref_p = None
        old_over = True
        if old:
            u_old = s_est - self._o
            old_over = u_old > old_last + 1e-9
            if 0.0 <= u_old:
                ref_p = self._X(min(u_old, float(old_last)))[:3]
                a = _floor(u_old)
                if a - self._lo < self._nseg:
                    d = self._D[min(max(a - self._lo, 0), self._nseg - 1), :3]
                    ln = float(np.linalg.norm(d))
                    tangent_old = d / ln if ln > 1e-9 else None
        # swap in the new plan (origin s_obs); the old reference point was taken above
        self._install(r64, 0, s_obs, H - 1, r32[-1].copy(), "swap")
        self._fresh = False
        if ref_p is not None and 0.0 <= u_new <= H - 1:
            if not old_over:
                # the switch jump statistic of rtcc:233-237: None when the old plan is already over (the anchor
                # below still uses the clamped end point)
                jump = float(
                    np.linalg.norm(
                        np.asarray(self._X(u_new)[:3], np.float64)
                        - np.asarray(ref_p, np.float64)
                    )
                )
                self._lists["switch_jump_m"].append(jump)
            a1 = min(u_new + p.swap_reanchor_span, float(H - 1))
            u_p, _, tan_new = self._project(
                self._R, self._D, self._lo, self._phi_grid(u_new, a1), ref_p
            )
            anchor = u_p - u_new
            self._lists["swap_anchor_rows"].append(anchor)
        skip = 0.0
        if (
            p.swap_reanchor
            and not self._golden
            and anchor is not None
            and anchor > 0.0
            and (
                tangent_old is None
                or tan_new is None
                or float(tangent_old @ tan_new) >= p.reanchor_cos_min
            )
        ):
            skip = min(anchor, float(p.swap_reanchor_rows))
            ahead = [g for g in self._G if g > u_new]
            if ahead:
                skip = min(skip, max(0.0, ahead[0] - 1 - u_new))
        if skip > 0.0:
            self._o = s_obs - skip
            self._st["reanchors"] += 1
        # lead offset carries over (section 3.5); the blend starts from the last target
        self._e_off = min(self._e_off, p.lead_max_rows) if p.lead else 0.0
        e_prev_new = (self._s - self._o) + self._e_off
        n_b = self._start_blend(e_prev_new)
        self._record_chunk(
            r32,
            "swap",
            s_obs=s_obs,
            d_rows=u_new,
            anchor=anchor,
            jump=jump,
            n_blend=n_b,
        )
        return {
            "d_rows": float(u_new),
            "jump_m": jump,
            "anchor_rows": anchor,
            "n_blend": int(n_b),
        }

    def replan(self, rows: np.ndarray) -> dict:
        """VLASH grasp re-plan (PhaseParams.grasp_replan_m): ``rows`` were planned from this tick's measured state and
        replace the plan while the arm moves. ``set_chunk`` would land on row 0, that is on the lagging arm, at lambda 1,
        and the target would step back toward the arm (review 2026-11-02: 6 mm per tick in a line-world sim). Here the
        phase is placed where the last target projects onto the new path, never later than one row before the first
        gripper change ahead and never closer than REPLAN_RUNWAY_ROWS to the chunk's end, lambda is kept, and what is
        left of the offset is blended as at any swap. Returns {"anchor_rows", "n_blend"}. Raises NonFiniteChunk."""
        if self._golden:
            raise RuntimeError("PhaseEngine.replan: not in golden mode")
        r64, r32 = self._check_rows(rows)
        H = len(r64)
        s_est = self._s + max(self._lam, 1.0)  # the next tick's candidate phase
        ref_p = None
        if self._T_prev is not None and np.isfinite(self._T_prev).all():
            ref_p = np.asarray(self._T_prev[:3], np.float64)
        self._install(r64, 0, s_est, H - 1, r32[-1].copy(), "replan")
        self._fresh = False
        u_p = 0.0
        hi = float(max(H - 1 - REPLAN_RUNWAY_ROWS, 0))
        ahead = [g for g in self._G if g > 0.0]
        if ahead:
            hi = min(hi, max(0.0, ahead[0] - 1.0))
        if ref_p is not None and hi > 0.0:
            u_p, _, _ = self._project(
                self._R, self._D, self._lo, self._phi_grid(0.0, hi), ref_p
            )
        self._o = s_est - u_p
        self._e_off = 0.0
        n_b = self._start_blend(self._s - self._o)
        self._st["grasp_replans"] += 1
        self._record_chunk(r32, "replan", s_obs=s_est, anchor=u_p, n_blend=n_b)
        return {"anchor_rows": float(u_p), "n_blend": int(n_b)}

    def append(self, rows: np.ndarray) -> None:
        """VLASH: the prefetched chunk joins at origin + H (it was predicted from chunk[-1], so it continues the
        path); no clamping of the join to row H-1. Raises NonFiniteChunk."""
        if self._R is None:
            raise RuntimeError("PhaseEngine.append: no chunk to append to")
        r64, r32 = self._check_rows(rows)
        u_now = self._s - self._o
        R = np.vstack([self._R, r64])
        lo = self._lo
        drop = (
            _floor(u_now) - 2 - lo
        )  # rows far behind the phase are never needed again
        if drop > 0:
            R = R[drop:]
            lo += drop
        self._install(
            R, lo, self._o, lo + len(R) - 1, r32[-1].copy(), "append", keep_row0=True
        )
        self._record_chunk(r32, "append", s_obs=None)

    def splice_plain(self, rows: np.ndarray, m: Measured, h: int) -> int:
        """Plain hook A: join a freshly queried chunk (virtual row -1 = the last issued target, section 3.7), plan
        the first ``h`` rows and return m_ticks, the number of ticks the loop should run before it queries again
        (golden: h). Raises NonFiniteChunk."""
        r64, r32 = self._check_rows(rows)
        p = self.params
        H = len(r64)
        h = max(1, min(int(h), H))
        self._observe(m)
        T = self._T_prev
        u0 = -1.0
        e_p = None
        if not self._golden and p.plain_reanchor and T is not None:
            ahead_rows = r64[: min(h - 1, p.plain_reanchor_span) + 1]
            if len(ahead_rows) >= 2:
                phis = self._phi_grid(0.0, float(len(ahead_rows) - 1))
                e_p, _, tan_new = self._project(
                    r64, _segment_deltas(r64), 0, phis, np.asarray(T[:3], np.float64)
                )
                cos_ok = (
                    self._dir_prev is None
                    or tan_new is None
                    or float(self._dir_prev @ tan_new) >= p.reanchor_cos_min
                )
                if e_p > 0.0 and cos_ok:
                    bits = (r64[:, 6] >= GRIP_THRESHOLD).astype(np.int8)
                    g_ref = self._g_ref()
                    G = [j for j in range(1, H) if bits[j] != bits[j - 1]]
                    if bits[0] != g_ref:
                        G.insert(0, 0)
                    lim = min(
                        [g - 1 for g in G if g <= math.ceil(e_p)]
                        + [float(p.plain_reanchor_rows)]
                    )
                    u0 = max(
                        -1.0, min(e_p - self._e_off, float(p.plain_reanchor_rows), lim)
                    )
        if T is None:
            T = np.asarray(m.pose, np.float32).copy()
            self._T_prev = T
        if u0 <= -1.0 + 1e-12:
            # virtual row -1 = the last issued target; no offset to blend away
            R = np.vstack([np.asarray(T, np.float64)[None, :], r64])
            self._install(R, -1, self._s + 1.0, h - 1, r32[-1].copy(), "plain")
            self._blend = None
            self._blend_pending = False
            self._e_off = 0.0
            u0 = -1.0
        else:
            self._install(r64, 0, self._s - u0, h - 1, r32[-1].copy(), "plain")
            self._e_off = min(self._e_off, p.lead_max_rows) if p.lead else 0.0
            self._start_blend(u0 + self._e_off)
        self._fresh = False
        self._lists["splice_rows"].append(u0 + 1.0)
        self._record_chunk(r32, "plain", s_obs=self._s + 1.0, anchor=e_p)
        if self._golden:
            return h
        return self._open_loop_ticks(u0, h)

    def _gate_clear(self) -> None:
        """The close gate's wait is over (close sent, command changed, or no close ahead any more)."""
        self._gate_wait, self._gate_since, self._gate_pos, self._gate_total = (
            0,
            None,
            None,
            0,
        )

    def _close_ahead(self) -> bool:
        """The loaded plan still has a close (1 -> 0 against the commanded value) ahead of the phase."""
        if self._R is None or self._g_ref() != 1:
            return False
        s, o, lo = self._s, self._o, self._lo
        return any(o + g > s - _SNAP and _b(self._R[g - lo, 6]) == 0 for g in self._G)

    def _open_loop_ticks(self, u0: float, h: int) -> int:
        """Ticks the lambda law needs from phase u0 to row h-1 without any measurement-driven slowdown: the
        closed loop can only be slower, so the plain loop never reaches the plan's end early. No allowance for a
        close gate wait: a re-query inside the wait keeps it (count and time survive the reload while the close
        stays put), and a longer placeholder only parked the arm at the plan's end (replay of the 11-02 fixes)."""
        prof = self._profile(self._dhat)
        p, dt = self.params, self.dt
        u, lam, dlam = u0, self._lam, self._dlam
        end = float(h - 1)
        for k in range(1, 400):
            cap, _ = self._lam_hat(u, lam, prof)
            a = _floor(u) - self._lo
            dp = self._dpl[a] if 0 <= a < self._nseg else 0.0
            rise = min(
                p.rate_up,
                max(dlam, 0.0) + p.jerk,
                p.a_tan_up * dt * dt / dp if dp > 1e-9 else _INF,
            )
            new = min(cap, lam + rise)
            u_n = u + new
            land = next((g for g in self._G if g > u + 1e-9), None)
            if land is not None and u_n >= land:
                u_n = float(land)
            u_n = min(u_n, end)
            dlam, lam, u = (u_n - u) - lam, u_n - u, u_n
            if u >= end - 1e-9:
                return max(1, k)
        return 400

    def preview(self, n: int) -> np.ndarray:
        """(n, 7) float32 placeholder plan for the plain client, whose loop wants a chunk-shaped array; the real
        targets come from ``tick`` each tick. Golden: the first n rows of the chunk."""
        if self._R is None:
            raise RuntimeError("PhaseEngine.preview: no chunk loaded")
        n = int(n)
        if n < 1:
            raise ValueError("preview needs n >= 1")
        a0 = -self._lo if self._lo <= 0 else 0
        idx = np.minimum(np.arange(a0, a0 + n), len(self._R) - 1)
        return self._R[idx].astype(np.float32)

    # ------------------------------------------------------------------ measurement
    def _observe(self, m: Measured) -> None:
        """Per-tick bookkeeping of one measurement (idempotent for the same object)."""
        if self._observed is m:
            return
        self._observed = m
        p = self.params
        x32 = m.pose
        x64 = x32.astype(np.float64)
        finite = bool(np.isfinite(x64).all())
        self._x64 = x64
        # a second look at the same tick (plain: hook A then hook B build their own Measured): refresh the leash
        # inputs but keep the velocity and the gripper tracking of the first look
        same_tick = self._t_prev is not None and abs(m.t - self._t_prev) < 0.5 * self.dt
        # more than a second without a tick: the environment was reset (or the client blocked); the arm may be
        # nowhere near the last target
        gap = self._t_prev is not None and (m.t - self._t_prev) > _TICK_GAP_S
        if not same_tick:
            # speed of the arm from successive measured poses
            self._speed = None
            if (
                finite
                and self._x_prev is not None
                and self._t_prev is not None
                and m.t > self._t_prev
            ):
                self._speed = float(np.linalg.norm(x64[:3] - self._x_prev[:3])) / (
                    m.t - self._t_prev
                )
            if finite and not gap:
                self._x_prev, self._t_prev = x64[:7].copy(), float(m.t)
            elif finite:  # first look after a gap: no velocity from before it
                self._x_prev, self._t_prev, self._speed = (
                    x64[:7].copy(),
                    float(m.t),
                    None,
                )
            else:
                self._x_prev = None
        # the last target: anchor to the measured pose when there is none, or when a gap left the arm outside its leash
        if finite:
            if self._T_prev is None:
                self._T_prev = x32.copy()
            elif gap and not self._within_hard_leash(self._T_prev, x32):
                self._T_prev = x32.copy()
                self._st["reanchored_to_measured"] += 1
                self._blend = None  # the offset was made against the stale target
                self._blend_pending = self._R is not None
        # soft-leash inputs
        T = self._T_prev
        if finite and T is not None and np.isfinite(T).all():
            self._eps = float(np.linalg.norm(x64[:3] - T[:3]))
            self._rho = _rot_diff(x64[3:6], T[3:6])
            g_m = min(
                _clip01((p.soft_m1 - self._eps) / (p.soft_m1 - p.soft_m0)),
                _clip01((p.soft_rad1 - self._rho) / (p.soft_rad1 - p.soft_rad0)),
            )
            self._gamma = g_m
        else:
            self._eps = self._rho = _INF
            self._gamma = 0.0
        if not same_tick:
            self._track_gripper(m, finite)
        if self._blend_pending and self._R is not None and self._T_prev is not None:
            e_prev = (self._s - self._o) + self._e_off
            self._start_blend(e_prev)

    def _within_hard_leash(self, T, x32) -> bool:
        p = self.params
        dp = float(np.linalg.norm(np.asarray(T[:3], np.float64) - x32[:3]))
        dr = _rot_diff(T[3:6], x32[3:6])
        return bool(dp <= p.hard_leash_m) and bool(dr <= p.hard_leash_rad)

    def _track_gripper(self, m: Measured, finite: bool) -> None:
        """Post-close settle (section 3.6 c): from the tick the issued value first reads closed until the fingers
        have moved and stopped, or a timeout."""
        p = self.params
        gi = _b(m.pose[6]) if finite else None
        if gi == 0 and self._last_gi == 1 and p.grip_settle:
            self._settle_active = True
            self._settle_t0 = float(m.t)
            self._settle_ap0 = m.aperture
            self._settle_ap_prev = m.aperture
            self._settle_moved = False
            self._settle_stable = 0
            last = self._events[-1] if self._events else None
            self._settle_event = last if last and last["kind"] == "close" else None
        if gi == 1 and self._last_gi == 0 and p.open_settle:
            self._osettle_active = True
            self._osettle_t0 = float(m.t)
            self._osettle_ap0 = m.aperture
            last = self._events[-1] if self._events else None
            self._osettle_event = last if last and last["kind"] == "open" else None
        if gi is not None:
            self._last_gi = gi
        if self._osettle_active:
            self._open_settle_step(m)
        if not self._settle_active:
            return
        dt_s = float(m.t) - self._settle_t0
        ap = m.aperture
        done = False
        if dt_s >= p.settle_timeout_s:
            done = True
        elif ap is None or self._settle_ap0 is None:
            done = dt_s >= p.settle_unknown_s
        else:
            if abs(ap - self._settle_ap0) >= p.settle_aperture_delta:
                self._settle_moved = True
            if self._settle_moved and self._settle_ap_prev is not None:
                if abs(ap - self._settle_ap_prev) < p.settle_stable_eps:
                    self._settle_stable += 1
                else:
                    self._settle_stable = 0
            done = (
                dt_s >= p.settle_min_s
                and self._settle_moved
                and self._settle_stable >= p.settle_stable_ticks
            )
        self._settle_ap_prev = ap
        if done:
            self._settle_active = False
            if self._settle_event is not None:
                self._settle_event["settle_s"] = dt_s
                self._emit(
                    {"kind": "grip", "phase": "settled", "k": self._k, "settle_s": dt_s}
                )
            self._settle_event = None

    def _open_settle_step(self, m: Measured) -> None:
        """Open settle (``open_settle``): done when the fingers have opened by settle_aperture_delta and
        open_settle_min_s has passed, at open_settle_timeout_s, or (no aperture reading) at open_settle_unknown_s."""
        p = self.params
        dt_s = float(m.t) - self._osettle_t0
        ap, ap0 = m.aperture, self._osettle_ap0
        if dt_s >= p.open_settle_timeout_s:
            done = True
        elif ap is None or ap0 is None:
            done = dt_s >= p.open_settle_unknown_s
        else:
            done = dt_s >= p.open_settle_min_s and ap - ap0 >= p.settle_aperture_delta
        if not done:
            return
        self._osettle_active = False
        if self._osettle_event is not None:
            self._osettle_event["settle_s"] = dt_s
            self._emit(
                {"kind": "grip", "phase": "settled", "k": self._k, "settle_s": dt_s}
            )
        self._osettle_event = None

    def _post_active(self) -> bool:
        """Section 3.4 step 2: lambda <= 1 while the issued gripper value lags the command or the fingers settle
        (after a close: ``grip_settle``; after an open: ``open_settle``)."""
        if self.params.open_settle and self._osettle_active:
            return True
        if not self.params.grip_settle:
            return False
        if self._settle_active:
            return True
        return (
            self._g_cmd is not None
            and self._last_gi is not None
            and self._last_gi != self._g_cmd
        )

    # ------------------------------------------------------------------ the tick
    def begin(self, m: Measured, inflight: Inflight | None) -> Proposal:
        """Steps 1-6 of section 3.4: the segment caps, close/open limits, soft leash, governor and rates give
        ``lam`` and the candidate phase ``s_cand``; ``past_end`` is set when s + lam would pass the chunk's last
        executable row. Commits nothing: the scheduler may now collect a reply (``swap``), block or query, then
        call ``commit``. Raises NonFiniteChunk never (chunks are checked on entry); a non-finite measured pose
        only makes the leash infeasible (hold)."""
        self._m = m
        self._inflight = inflight
        self._observe(m)
        if self._R is not None and self._row0_change is None and self._g_known():
            self._rebuild(self._g_ref())
        self._propose()
        prop = self._prop
        return Proposal(
            lam=prop["lam"],
            s_cand=prop["s_cand"],
            past_end=prop["past_end"],
            binding=prop["bind"],
        )

    def _propose(self) -> None:
        p = self.params
        s, o = self._s, self._o
        self._gov_on = False
        if self._R is None:
            self._prop = {
                "lam": 0.0,
                "s_cand": s,
                "past_end": False,
                "bind": "end",
                "cap": 0.0,
                "landing": False,
            }
            self._prop_valid = True
            return
        u = s - o
        end_s = o + self._u_end
        bind = "max"
        gov_on = False
        landing = False
        if self._golden:
            cap = lam = 1.0
            s_c = s + 1.0
        elif self._fresh:
            s_c = o
            lam = cap = s_c - s
            bind = "landing"
        else:
            prof = self._profile(self._dhat)
            cap, bind = self._lam_hat(u, self._lam, prof)
            lam0 = cap
            if lam0 > 1.0 and self._post_active():
                lam0, bind = 1.0, "post"
            lam_s = lam0
            if p.soft_leash and lam0 > 1.0 and self._gamma < 1.0:
                lam_s = 1.0 + (lam0 - 1.0) * self._gamma
                bind = "soft_leash"
            inflight = self._inflight
            if p.governor and inflight is not None and not inflight.done:
                gv = max(0.0, self._u_end - u - p.governor_margin_rows) / max(
                    1.0, float(inflight.d_hat_ticks - inflight.age_ticks)
                )
                if gv < lam_s:
                    lam_s, bind, gov_on = gv, "gov", True
            lam1 = max(lam_s, min(lam0, self._lam - p.rate_dn_soft))
            if lam1 > lam_s:
                bind = "rate"
            a = _floor(u) - self._lo
            dp_cur = self._dpl[a] if 0 <= a < self._nseg else 0.0
            rise = min(
                p.rate_up,
                max(self._dlam, 0.0) + p.jerk,
                p.a_tan_up * self.dt * self.dt / dp_cur if dp_cur > 1e-9 else _INF,
            )
            lam = max(min(lam1, self._lam + rise), 0.0)
            if lam < lam1:
                bind = "rate"
            s_c = s + lam
            land = next((g for g in self._G if o + g > s + _SNAP), None)
            if land is not None and s_c >= o + land - 1e-12:
                if s_c > o + land + 1e-12:
                    bind = "landing"
                s_c, landing = o + land, True
        past = s_c > end_s + 1e-9
        if past:
            s_c = max(s, end_s)
            bind = "end"
        self._gov_on = gov_on
        self._prop = {
            "lam": s_c - s,
            "s_cand": s_c,
            "past_end": past,
            "bind": bind,
            "cap": cap,
            "landing": landing,
        }
        self._prop_valid = True

    # ------------------------------------------------------------------ commit
    def _grip_value(self, u: float, hold_open: bool) -> float:
        g = self._grip_raw(u)
        if hold_open and _b(g) == 0:
            tp = self._T_prev
            return float(tp[6]) if tp is not None and _b(tp[6]) == 1 else GRIP_OPEN
        return g

    def _target_at(self, alpha, s_c, e_c, force_done, hold_open):
        """T(alpha): the plan at the partial advance (section 3.8) with the blend offset, float32 (7,), and the
        weight of the new path. With a workspace predicate set, the offset is scaled down (I7, section 3.7) until it
        no longer makes the client's own clamp move the pose."""
        s = self._s
        o = self._o
        sa = s + alpha * (s_c - s) if alpha < 1.0 else s_c
        ua = sa - o
        e_prev = (s - o) + self._e_off
        ea = e_prev + alpha * (e_c - e_prev) if alpha < 1.0 else e_c
        T = self._X(ea)
        T[6] = self._grip_value(ua, hold_open)
        bl = self._blend
        w = 1.0
        if bl is not None:
            sig = 1.0 if (force_done and alpha >= 1.0) else (bl["k"] + alpha) / bl["n"]
            w = _minjerk(sig)
            if w < 1.0:
                off = (1.0 - w) * bl["delta"]
                T32 = (T + np.append(off, 0.0)).astype(np.float32)
                hit = self._workspace_hit
                if hit is not None and hit(T32):
                    clean = T.astype(np.float32)
                    if not hit(
                        clean
                    ):  # the offset did it; a path the policy itself clamps is left alone
                        lo_k, hi_k = 0.0, 1.0
                        for _ in range(8):
                            mid = 0.5 * (lo_k + hi_k)
                            if hit((T + np.append(mid * off, 0.0)).astype(np.float32)):
                                hi_k = mid
                            else:
                                lo_k = mid
                        T32 = (T + np.append(lo_k * off, 0.0)).astype(np.float32)
                        self._ws_shrunk = True
                return T32, w
        return T.astype(np.float32), w

    def _feasible(self, T32, x32, Tp):
        """(ok, dp, dr, step_m, step_rad, which) for a candidate target; `not (d <= L)` so NaN is infeasible."""
        p = self.params
        t64 = T32.astype(np.float64)
        dp = float(np.linalg.norm(t64[:3] - x32[:3]))
        dr = _rot_diff(t64[3:6], x32[3:6])
        sp = sr = 0.0
        if Tp is not None:
            sp = float(np.linalg.norm(t64[:3] - Tp[:3]))
            sr = _rot_diff(t64[3:6], Tp[3:6])
        which = ""
        if p.hard_leash and not (dp <= p.hard_leash_m and dr <= p.hard_leash_rad):
            which = "hard_leash"
        elif (
            p.step_limit
            and Tp is not None
            and not (sp <= p.step_m and sr <= p.step_rad)
        ):
            which = "step"
        return which == "", dp, dr, sp, sr, which

    def commit(self) -> TickOut:
        """Steps 7-12 of section 3.4: close gate, issued phase e, blend, joint bisection of s and e against the
        hard leash and the step limit, range check (I7), float32 and finite check. Raises if the target would be
        non-finite. Advances s to what was accepted and writes the trace record."""
        m = self._m
        if m is None:
            raise RuntimeError("PhaseEngine.commit before begin")
        if not self._prop_valid:
            self._propose()
        prop = self._prop
        p = self.params
        x32 = m.pose
        self._snap_state = (
            self._s, self._lam, self._dlam, self._e_off, self._fresh, self._T_prev,
            self._g_cmd, self._gate_wait, self._holds, self._dir_prev,
            None if self._blend is None else self._blend["k"],
            self._blend_pending, self._gate_since, self._recov_run,
            self._gate_pos, self._gate_total,
        )  # fmt: skip
        k = self._k
        self._k += 1
        self._st["ticks"] += 1
        if self._R is None:
            return self._hold_tick(m, "end", noplan=True)
        s, o = self._s, self._o
        s_c = prop["s_cand"]
        bind = prop["bind"]
        cap = prop["cap"]
        # ---- 7. close gate (section 3.6) ----
        # The wait is measured from its first tick across plan reloads (set_chunk / swap / splice_plain / reset): it ends
        # when the close is sent, the commanded gripper value changes, or the loaded plan no longer has a close ahead.
        # Release at gate_max_ticks wait ticks or gate_max_ticks ticks of time (half a tick of slack for loop jitter).
        # A close that a reload moved more than _GATE_MOVED * gate_dist_m starts its own wait (count and time), under a
        # cap of _GATE_TOTAL * gate_max_ticks gate-held ticks in all (after _GATE_AT_PLACE waits at the current place;
        # twice the cap is the hard backstop).
        hold_open = False
        gate_info = None
        u_c = _snap(s_c - o)
        if p.grip_gate and not self._golden:
            g_c = self._grip_raw(u_c)
            g_ref = self._g_ref()
            if _b(g_c) == 0 and g_ref == 1:
                gf = _floor(u_c)
                row = self._R[min(max(gf - self._lo, 0), len(self._R) - 1)]
                err = float(np.linalg.norm(self._x64[:3] - row[:3]))
                spd = 0.0 if self._speed is None else self._speed
                ready = bool(err <= p.gate_dist_m) and bool(spd <= p.gate_speed)
                gpos = np.asarray(row[:3], np.float64)
                if (
                    self._gate_since is not None
                    and self._gate_pos is not None
                    and float(np.linalg.norm(gpos - self._gate_pos))
                    > _GATE_MOVED * p.gate_dist_m
                ):
                    self._gate_wait, self._gate_since = 0, None  # a different close
                if self._gate_since is None:
                    self._gate_since, self._gate_pos = float(m.t), gpos
                waited_s = float(m.t) - self._gate_since
                gate_cap = (
                    _GATE_TOTAL * p.gate_max_ticks
                )  # not `cap`: that is this tick's lambda cap (trace, TickOut)
                timeout = (
                    self._gate_wait >= p.gate_max_ticks
                    or waited_s >= (p.gate_max_ticks - 0.5) * self.dt
                    or (
                        self._gate_total >= gate_cap
                        and (
                            self._gate_wait >= _GATE_AT_PLACE
                            or self._gate_total >= 2 * gate_cap
                        )
                    )
                )
                if ready or timeout:
                    gate_info = {
                        "gate_wait_ticks": self._gate_wait,
                        "gate_wait_s": waited_s,
                        "gate_timeout": not ready,
                        "arm_err_m": err,
                        "arm_speed_m_s": spd,
                    }
                else:
                    self._gate_wait += 1
                    self._gate_total += 1
                    hold_open = True
                    s_c = max(s, min(s_c, o + gf - _GATE_STOP))
                    u_c = s_c - o
                    bind = "gate"
                    self._st["gate_waits"] += 1
            elif self._gate_since is not None and not self._close_ahead():
                self._gate_clear()
        # ---- close hold: after a close the plan waits for the fingers (PhaseParams.close_hold) ----
        if p.close_hold and not self._golden and self._settle_active and s_c > s:
            s_c = s
            u_c = s_c - o
            bind = "grasp"
            self._st["grasp_hold_ticks"] += 1
        # ---- 8. issued phase e (section 3.5) ----
        e_prev = (s - o) + self._e_off
        e_c = u_c
        if p.lead and not self._golden:
            lam_c = s_c - s
            e_tgt = u_c + min(
                p.lead_max_rows,
                self._derived["g_lead"] * max(lam_c - 1.0, 0.0),
            )
            prof = self._profile(self._dhat)
            a_cap, _ = self._cmin(prof, _floor(e_prev), _floor(e_prev + self._lam_max))
            e_c = max(e_prev, u_c, min(e_tgt, e_prev + a_cap))
            in_win, barrier = self._barrier(u_c)
            e_c = u_c if in_win else min(e_c, barrier)
            e_c = max(min(e_c, self._u_end), u_c)
        # ---- 9/10. target and the joint bisection against the leash and the step limit ----
        Tp = self._T_prev
        force_done = bool(prop["landing"]) and not hold_open
        self._ws_shrunk = False
        T32, w = self._target_at(1.0, s_c, e_c, force_done, hold_open)
        ok, dp, dr, sp, sr, which = self._feasible(T32, x32, Tp)
        alpha = 1.0
        hold = False
        if not ok:
            ok0 = True
            if Tp is not None:
                ok0, _, _, _, _, _ = self._feasible(Tp, x32, None)
            if not ok0 or Tp is None:
                hold = True
            else:
                lo_a, hi_a = 0.0, 1.0
                for _ in range(p.bisect_iters):
                    mid = 0.5 * (lo_a + hi_a)
                    Tm, _w = self._target_at(mid, s_c, e_c, force_done, hold_open)
                    if self._feasible(Tm, x32, Tp)[0]:
                        lo_a = mid
                    else:
                        hi_a = mid
                alpha = lo_a
                if alpha * (s_c - s) < _MIN_ADV:
                    hold = True  # the leash lets it creep by less than a pause: that is no progress
                else:
                    self._ws_shrunk = False
                    T32, w = self._target_at(alpha, s_c, e_c, force_done, hold_open)
            bind = which if not hold else "hard_leash"
        if hold:
            return self._hold_tick(m, "hard_leash", cap=cap)
        if not bool(np.isfinite(T32).all()):
            raise FloatingPointError(
                "PhaseEngine: refusing to issue a non-finite target"
            )
        _ok, dp, dr, sp, sr, _ = self._feasible(T32, x32, Tp)
        s_new = s_c if alpha >= 1.0 else s + alpha * (s_c - s)
        e_new = e_c if alpha >= 1.0 else e_prev + alpha * (e_c - e_prev)
        u_new = _snap(s_new - o)
        lam_eff = s_new - s
        # ---- state update ----
        self._s = s_new
        self._e_off = max(e_new - u_new, 0.0) if (p.lead and not self._golden) else 0.0
        self._dlam = lam_eff - self._lam
        self._lam = lam_eff
        self._holds = 0
        if alpha >= 1.0 or which != "hard_leash":
            # an advance the hard leash did not cut: the arm keeps up, so earlier recoveries are history. One the leash
            # cut leaves the count alone (a blocked arm alternates cut advances and recoveries: see _hold_tick).
            self._recov_run = 0
        self._fresh = False
        bl = self._blend
        if bl is not None:
            bl["k"] += 1.0 if (force_done and alpha >= 1.0) else alpha
            if bl["k"] >= bl["n"] - 1e-9 or (force_done and alpha >= 1.0):
                self._blend = None
        self._update_dmeas(dp, hold=False)
        if self._ws_shrunk:
            self._st["ws_shrinks"] += 1
        event = self._grip_event(
            T32, u_new, lam_eff, gate_info, exact=prop["landing"] and alpha >= 1.0
        )
        if prop["past_end"] and alpha >= 1.0 and s_new >= o + self._u_end - 1e-9:
            self._st["end_holds"] += 1
            bind = "end"
        if self._gov_on:
            self._st["governor_ticks"] += 1
        self._finish_tick(
            m, T32, dp, dr, sp, sr, lam_eff, cap, bind, hold=False, w=w, u=u_new,
            e=e_new if p.lead else u_new, event=event, k=k,
        )  # fmt: skip
        self._advance_dir(Tp, T32)
        self._T_prev = T32.copy()
        return TickOut(
            target=T32,
            s=self._s,
            u=u_new,
            e=e_new,
            lam=lam_eff,
            cap=float(cap),
            hold=False,
            blend_w=float(w),
            dp=dp,
            dr=dr,
            grip_event=event,
            binding=bind,
        )

    def _recovery_target(self, x32):
        """The last target is outside the hard leash of the measured pose (the arm was pushed away, or bounced off a
        contact): move it back toward the arm only as far as needed, so that invariant I5 holds and the normal
        bisection fits again. Per bound: the translation moves by min(step_m, |x - T| - 0.95 hard_leash_m) along the
        line to the arm, each rotation axis by min(step_rad, its excess over 0.95 hard_leash_rad); a bound that holds
        (translation inside 0.08 m, or every axis inside 0.28 rad) is left untouched (``_RECOVER_INSIDE``). Returns
        ``(target, anchored)`` or None when the last target is inside the leash (or nothing can be said about a
        non-finite pose). ``anchored`` is True when even one full step would leave a target the client rejects (the
        arm is more than about 0.14 m off): the target is then re-anchored to the measured pose, because a rejected
        target leaves the old one in force and the arm would be pulled back for ever (the client re-queries and
        yields to the arm in that case today)."""
        p = self.params
        Tp = self._T_prev
        if not (p.hard_leash and p.step_limit) or Tp is None:
            return None
        x64 = x32.astype(np.float64)
        t64 = Tp.astype(np.float64)
        if not (np.isfinite(x64[:6]).all() and np.isfinite(t64).all()):
            return None
        if self._within_hard_leash(Tp, x32):
            return None
        T = Tp.copy()
        v = x64[:3] - t64[:3]
        n = float(np.linalg.norm(v))
        if n > p.hard_leash_m:
            move = min(p.step_m * _STEP_SAFETY, n - _RECOVER_INSIDE * p.hard_leash_m)
            T[:3] = (t64[:3] + v * (move / n)).astype(np.float32)
        d = wrap_to_pi(x64[3:6] - t64[3:6])
        if float(np.abs(d).max()) > p.hard_leash_rad:
            need = np.maximum(np.abs(d) - _RECOVER_INSIDE * p.hard_leash_rad, 0.0)
            d = np.sign(d) * np.minimum(need, p.step_rad * _STEP_SAFETY)
            T[3:6] = (t64[3:6] + d).astype(np.float32)
        dp = float(np.linalg.norm(T[:3].astype(np.float64) - x64[:3]))
        dr = _rot_diff(T[3:6].astype(np.float64), x64[3:6])
        if (
            dp <= _RECOVER_MARGIN * CLIENT_MAX_M
            and dr <= _RECOVER_MARGIN * CLIENT_MAX_RAD
        ):
            return T, False
        T[:6] = x32[:6]
        return T, True

    def _hold_tick(self, m, bind, *, cap=0.0, noplan=False) -> TickOut:
        """Re-send the last target unchanged (leash hold, gate park without progress, or no plan loaded). If that
        target is itself outside the hard leash, move it back just inside instead (see ``_recovery_target``); the
        tick still counts as a hold, so the third one in a row drops the plan. Recoveries also count on their own:
        an arm blocked by a contact alternates advances the leash cuts with recoveries (the advances end each run of
        holds), and the hold_reset_after-th recovery without an uncut advance in between drops the plan as well."""
        p = self.params
        Tp = self._T_prev
        if Tp is None:
            raise RuntimeError(
                "PhaseEngine: no previous target to hold (the measured pose was not finite on the first tick)"
            )
        if not np.isfinite(Tp).all():
            raise FloatingPointError("PhaseEngine: the previous target is not finite")
        T32 = Tp.copy()
        x32 = m.pose
        rec = self._recovery_target(x32)
        if rec is not None:
            T32, anchored = rec
            self._T_prev = T32.copy()
            self._st["leash_recoveries"] += 1
            if anchored:
                self._st["recovery_reanchors"] += 1
            # the blend offset was made against the old target: start it again from this one
            self._blend = None
            self._blend_pending = self._R is not None
            if not noplan:
                self._recov_run += 1
        _, dp, dr, _, _, _ = self._feasible(T32, x32, None)
        if not bool(np.isfinite(x32).all()):
            self._st["nonfinite"] += 1
        reset = False
        if noplan:
            self._st["noplan_holds"] += 1
        else:
            self._holds += 1
            self._st["leash_holds"] += 1
            if (
                self._holds >= p.hold_reset_after
                or self._recov_run >= p.hold_reset_after
            ):
                reset = True
                self._st["hold_resets"] += 1
        if reset:
            self.reset()  # drops the plan; lambda back to 1, the phase and the last target stay
        else:
            self._dlam = -self._lam
            self._lam = 0.0
        u = (self._s - self._o) if self._R is not None else 0.0
        self._finish_tick(
            m, T32, dp, dr, 0.0, 0.0, 0.0, cap, bind, hold=True, w=1.0, u=u, e=u, event=None, k=self._k - 1
        )  # fmt: skip
        return TickOut(
            target=T32, s=self._s, u=u, e=u, lam=0.0, cap=float(cap), hold=True,
            blend_w=1.0, dp=dp, dr=dr, grip_event=None, binding=bind, reset=reset,
        )  # fmt: skip

    def _barrier(self, u: float):
        """(inside a gripper window?, the phase e may not pass) for reference phase u (I3, I6)."""
        p = self.params
        barrier = _INF
        for g in self._G:
            ws, we = g - p.w_pre, g + p.w_post
            if u < ws - _SNAP:
                barrier = min(barrier, float(ws))
            elif u < we - _SNAP and (p.w_pre > 0 or p.w_post > 0):
                return True, barrier
        return False, barrier

    def _advance_dir(self, Tp, T32) -> None:
        if Tp is None:
            return
        d = np.asarray(T32[:3], np.float64) - Tp[:3]
        n = float(np.linalg.norm(d))
        if n > 5e-4:
            self._dir_prev = d / n

    def _update_dmeas(self, dp: float, hold: bool) -> None:
        """D_meas (section 3.2): the lag of the arm in seconds, from ticks where it moved."""
        p = self.params
        if hold or self._speed is None or not (self._speed > p.d_meas_speed):
            return
        if not math.isfinite(dp):
            return
        self._dmeas.append((dp - p.lag_offset_m) / self._speed)
        if len(self._dmeas) >= p.d_meas_window:
            med = sorted(self._dmeas)[len(self._dmeas) // 2]
            dm = min(max(med, p.d_meas_min), p.d_meas_max)
            self._dhat = max(self._derived["D_alpha"], dm)

    def _grip_event(self, T32, u, lam_eff, gate_info, exact):
        """A gripper command change: record it (section 3.6) and latch the new command tentatively."""
        new = _b(T32[6])
        prev = self._g_ref()
        if new == prev:
            return None
        gf = _floor(u)
        row = self._R[min(max(gf - self._lo, 0), len(self._R) - 1)]
        err = float(np.linalg.norm(self._x64[:3] - row[:3]))
        spd = 0.0 if self._speed is None else self._speed
        ev = {
            "s": float(self._s),
            "kind": "close" if new == 0 else "open",
            "lam_at_cmd": float(lam_eff),
            "arm_err_m": err,
            "arm_speed_m_s": spd,
            "gate_wait_ticks": int(gate_info["gate_wait_ticks"]) if gate_info else 0,
            "gate_wait_s": float(gate_info["gate_wait_s"]) if gate_info else 0.0,
            "gate_timeout": bool(gate_info["gate_timeout"]) if gate_info else False,
            "settle_s": None,
            "exact": bool(exact),
        }
        if gate_info:
            ev["arm_err_m"], ev["arm_speed_m_s"] = (
                gate_info["arm_err_m"],
                gate_info["arm_speed_m_s"],
            )
        self._gate_clear()
        self._g_cmd = new
        self._events.append(ev)
        self._event_k = self._k - 1
        self._emit({**ev, "kind": "grip", "event": ev["kind"], "k": self._k - 1})
        return dict(ev)

    def _finish_tick(
        self, m, T32, dp, dr, sp, sr, lam_eff, cap, bind, *, hold, w, u, e, event, k
    ) -> None:
        """Statistics and the tick trace line."""
        if bind in self._bind_count:
            self._bind_count[bind] += 1
        self._lams.append(float(lam_eff))
        self._rows_adv += lam_eff
        if math.isfinite(dp):
            self._dp_max = max(self._dp_max, dp)
        if math.isfinite(dr):
            self._dr_max = max(self._dr_max, dr)
        if not hold:
            self._step_max = max(self._step_max, sp)
            self._rstep_max = max(self._rstep_max, sr)
            if self._blend is not None or w < 1.0:
                self._blend_step_max = max(self._blend_step_max, sp)
        inf = self._inflight
        self._emit(
            {
                "kind": "tick",
                "k": k,
                "t": float(m.t),
                "s": float(self._s),
                "u": float(u),
                "e": float(e),
                "lam": float(lam_eff),
                "cap": float(cap),
                "chunk_id": self._chunk_id,
                "tgt": T32.tolist(),
                "meas": m.pose.tolist(),
                "aperture": None if m.aperture is None else float(m.aperture),
                "g_cmd": self._g_cmd,
                "g_issued": self._last_gi,
                "dp": dp if math.isfinite(dp) else None,
                "dr": dr if math.isfinite(dr) else None,
                "eps": self._eps if math.isfinite(self._eps) else None,
                "rho": self._rho if math.isfinite(self._rho) else None,
                "blend_w": float(w),
                "inflight_age": None if inf is None else int(inf.age_ticks),
                "hold": bool(hold),
                "binding": bind,
            }
        )

    def _emit(self, rec: dict) -> None:
        if len(self._records) >= _MAX_RECORDS:
            del self._records[: _MAX_RECORDS // 10]
            self._st["records_dropped"] += _MAX_RECORDS // 10
        self._records.append(rec)

    def _record_chunk(
        self, r32, kind, *, s_obs, d_rows=None, anchor=None, jump=None, n_blend=0
    ) -> None:
        self._emit(
            {
                "kind": "chunk",
                "chunk_id": self._chunk_id,
                "chunk_kind": kind,
                "k": self._k,
                "s_obs": None if s_obs is None else float(s_obs),
                "origin": float(self._o),
                "rows": r32.tolist(),
                "d_rows": None if d_rows is None else float(d_rows),
                "anchor_rows": None if anchor is None else float(anchor),
                "jump_m": None if jump is None else float(jump),
                "n_blend": int(n_blend),
                "nonfinite": False,
            }
        )

    def tick(self, m: Measured, inflight: Inflight | None = None) -> TickOut:
        """``begin`` then ``commit`` in one call (plain: no scheduler step in between)."""
        self.begin(m, inflight)
        return self.commit()

    def executed(self, sent: np.ndarray) -> None:
        """The client accepted ``sent`` (after its own clamp): latch the gripper command and the issued target."""
        sent = np.asarray(sent, dtype=np.float32).reshape(-1)
        if sent.shape != (7,):
            raise ValueError(f"executed: expected a (7,) target, got {sent.shape}")
        if not bool(np.isfinite(sent).all()):
            return
        self._T_prev = sent.copy()
        g = _b(sent[6])
        if self._g_cmd is not None and g != self._g_cmd:
            self._gate_clear()  # the commanded gripper changed: a gate wait is over
        self._g_cmd = g
        self._snap_state = None

    def rejected(self) -> None:
        """The client refused the target (STEP / ROT notes): roll the tick back as a hold."""
        st = self._snap_state
        if st is None:
            return
        (self._s, self._lam, self._dlam, self._e_off, self._fresh, self._T_prev,
         self._g_cmd, self._gate_wait, self._holds, self._dir_prev, bk, self._blend_pending,
         self._gate_since, self._recov_run, self._gate_pos, self._gate_total) = st  # fmt: skip
        if self._blend is not None and bk is not None:
            self._blend["k"] = bk
        elif bk is not None and self._blend is None:
            pass  # the blend finished on the rejected tick; the client drops the chunk next (reset)
        self._prop_valid = False
        self._snap_state = None
        self._st["rejected"] += 1
        if self._events and self._event_k == self._k - 1:
            self._events.pop()  # the gripper command never went out
            self._event_k = -1

    # ------------------------------------------------------------------ views
    def prev_rows(self) -> np.ndarray:
        """RTC prev_actions: X_raw(u + j), j = 0..floor(H-1-u), the policy's own 10 Hz grid in float32 (I1). An
        integer u gives exactly ``chunk[idx:]``."""
        if self._R is None:
            raise RuntimeError("PhaseEngine.prev_rows: no chunk loaded")
        u = max(_snap(self._s - self._o), 0.0)
        last = self._lo + len(self._R) - 1
        n = math.floor(last - u + _SNAP) + 1
        n = max(n, 1)
        phis = u + np.arange(n)
        a = np.clip(phis - self._lo, 0.0, len(self._R) - 1)
        a = np.where(np.abs(a - np.round(a)) < _SNAP, np.round(a), a)
        j = np.minimum(np.floor(a).astype(int), max(len(self._R) - 2, 0))
        f = a - j
        out = self._R[j].copy()
        if len(self._R) >= 2:
            out[:, :6] += f[:, None] * self._D[j]
        out[a >= len(self._R) - 1] = self._R[-1]
        out32 = out.astype(np.float32)
        self._emit({"kind": "request", "k": self._k - 1, "prev": out32.tolist()})
        return out32

    @property
    def last_gripper(self) -> float | None:
        """Gripper value of the last queued row (chunk[-1][6]), None when no chunk is loaded."""
        if self._raw_last is None or self._R is None:
            return None
        return float(self._raw_last[6])

    def future_row(self) -> np.ndarray:
        """VLASH future state: ``chunk[-1]`` exactly, untouched by lead, blend or leash (I1)."""
        if self._raw_last is None:
            raise RuntimeError("PhaseEngine.future_row: no chunk loaded")
        out = self._raw_last.copy()
        self._emit({"kind": "request", "k": self._k - 1, "future": out.tolist()})
        return out

    @property
    def u(self) -> float:
        """Chunk-local reference phase (rows); what RTC / VLASH / the policy believe."""
        return float(_snap(self._s - self._o)) if self._R is not None else 0.0

    @property
    def s(self) -> float:
        """Global reference phase (rows); kept across chunks and resets."""
        return float(self._s)

    @property
    def lam(self) -> float:
        """Last committed lambda (rows per tick); 1.0 after reset."""
        return float(self._lam)

    @property
    def rows_left(self) -> float:
        """Rows from u to the last executable row of the loaded chunks."""
        if self._R is None:
            return 0.0
        return float(self._u_end - (self._s - self._o))

    # ------------------------------------------------------------------ records
    def summary(self) -> dict:
        """JSON-safe per-episode ``eval.chunk_accel`` record (section 8): counts, binding terms, leash / step
        maxima, blends, grip events, pipeline counters. Accumulates until ``new_episode``."""
        lam = np.asarray(self._lams, dtype=np.float64)
        n = int(self._st["ticks"])
        rows = float(self._rows_adv)
        st = self._st
        out = {
            "rule": PHASE_RULE,
            "ticks": n,
            "rows_advanced": rows,
            "lambda_eff": rows / n if n else 0.0,
            "ticks_saved_est": rows - n,
            "lambda_p50": float(np.percentile(lam, 50)) if len(lam) else 0.0,
            "lambda_p90": float(np.percentile(lam, 90)) if len(lam) else 0.0,
            "frac_ticks_lambda_gt_1.1": float((lam > 1.1).mean()) if len(lam) else 0.0,
            "binding": dict(self._bind_count),
            "leash_holds": int(st["leash_holds"]),
            "hold_resets": int(st["hold_resets"]),
            "dp_max_m": float(self._dp_max),
            "dr_max_rad": float(self._dr_max),
            "step_max_m": float(self._step_max),
            "step_max_rad": float(self._rstep_max),
            "blends": int(st["blends"]),
            "blend_max_step_m": float(self._blend_step_max),
            "switch_jump_m": [float(v) for v in self._lists["switch_jump_m"]],
            "swap_anchor_rows": [float(v) for v in self._lists["swap_anchor_rows"]],
            "splice_rows": [float(v) for v in self._lists["splice_rows"]],
            "grip_events": [dict(e) for e in self._events],
            "starved_ticks": int(st["starved_ticks"]),
            "governor_ticks": int(st["governor_ticks"]),
            "early_triggers": int(st["early_triggers"]),
            "delays_rows": [float(v) for v in self._lists["delays_rows"]],
            "plain_end_holds": int(st["plain_end_holds"]),
            "nonfinite": int(st["nonfinite"]),
            "end_holds": int(st["end_holds"]),
            "noplan_holds": int(st["noplan_holds"]),
            "gate_waits": int(st["gate_waits"]),
            "rejected": int(st["rejected"]),
            "reanchors": int(st["reanchors"]),
            "grasp_replans": int(st["grasp_replans"]),
            "grasp_hold_ticks": int(st["grasp_hold_ticks"]),
            "ws_shrinks": int(st["ws_shrinks"]),
            "reanchored_to_measured": int(st["reanchored_to_measured"]),
            "leash_recoveries": int(st["leash_recoveries"]),
            "recovery_reanchors": int(st["recovery_reanchors"]),
            "blends_stretched": int(st["blends_stretched"]),
            "d_hat_s": float(self._dhat),
            "records_dropped": int(st["records_dropped"]),
        }
        for name, vals in self._lists.items():
            out.setdefault(name, [float(v) for v in vals])
        for name, val in st.items():
            out.setdefault(name, int(val))
        return out

    def drain_records(self) -> list[dict]:
        """Trace lines (section 8: tick / chunk / request / grip records) since the last drain, in order."""
        out, self._records = self._records, []
        return out


def _clip01(x: float) -> float:
    return 0.0 if x < 0.0 else (min(x, 1.0))


__all__ = [
    "ALPHAS",
    "BINDINGS",
    "CLIENT_MAX_M",
    "CLIENT_MAX_RAD",
    "GRIP_CLOSED",
    "GRIP_OPEN",
    "GRIP_THRESHOLD",
    "MINJERK_PEAK",
    "PHASE_RULE",
    "RELAXED_TAG",
    "Inflight",
    "Measured",
    "NonFiniteChunk",
    "PhaseEngine",
    "PhaseParams",
    "Proposal",
    "TickOut",
    "vertex_curvature",
    "wrap_to_pi",
]
