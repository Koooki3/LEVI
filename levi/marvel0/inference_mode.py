"""Pure mode and FR3 wire-space contracts; no training or serving claims."""

MODES = ("plain", "rtc", "vlash")
VLASH_SCRIPT = "lab/vlash/serve_policy_vlash.py"
VLASH_CLIENT = "lab/vlash/run_collect_vlash.py"
# In-chunk acceleration (integrations/chunk_accel): a client-side switch. Rules v1 and v2 run over plain inference only
# (the ramp client below); rule v3, the phase clock, runs over plain, RTC and VLASH (its own entry point, further down).
CHUNK_ACCEL_CLIENT = "lab/chunk_accel/run_collect_accel.py"
CHUNK_ACCEL_SPEED_M_S = 0.25  # the client's default ramp speed (accel.SPEED)
CHUNK_ACCEL_TAU_S = 0.2  # the controller's setpoint low-pass it leads by (accel.TAU)
# The rules of the page. The ramp client's --accel-rule (accel.RULES) takes v1 and v2: v1, its default, hurries straight
# gripper-free chunks only; v2 also re-times curved chunks along their path and hurries the actions before a gripper
# change (docs/CHUNK_ACCEL.md, "Rule v2"). v3 is not an --accel-rule: it is another client, below. Hiding the whole of v3
# from the page is this tuple without "v3" (command.py lets v3, and only v3, past "plain inference only": ledger E12).
CHUNK_ACCEL_RULES = ("v1", "v2", "v3")
CHUNK_ACCEL_RULE_DEFAULT = "v1"
# Rule v3, the phase clock (integrations/chunk_accel/phase.py, design fr3_accel_test/decisions/2026-11-01-phase-clock-design.md):
# one entry point for the three modes, chosen by --phase-mode. The values the page offers are a subset of what
# phase.PhaseParams validates (tests/marvel0/test_chunk_accel_runtime.py holds the two together).
CHUNK_ACCEL_PHASE_CLIENT = "lab/chunk_accel/run_collect_phase.py"
# The inference modes v3 runs with. Dropping a mode here hides it from the page and makes the backend refuse it
# (chunk_accel_v3_mode_disabled); command-logic.ts mirrors this tuple and the runtime test checks they are equal.
CHUNK_ACCEL_V3_MODES = ("plain", "rtc", "vlash")
CHUNK_ACCEL_V3_LAMBDAS = (
    2.0,
    2.5,
    3.0,
)  # --phase-lambda-max: chunk rows advanced per 10 Hz tick, at most
# 3.0 with the "strong" profile of 2026-11-02 (phase.PhaseParams defaults: a_lat 2.4, 50 mm curvature arc, gripper window 3/3,
# rise 0.8 / jerk 0.45, close gate 8 mm), chosen on the user's direction of 11-01 ("明显的加速提升和同时稳定性的balance", no
# over-strict limits): the 11-01 spec profile gave duration ratios 0.79 (RTC) / 0.82 (VLASH) / 0.93 (plain), which the user
# could not see. Offline replay of the strong profile at 3.0 (jump 0, real latency, H0 / H1): RTC 0.63 / 0.64, VLASH 0.59,
# plain 0.76 / 0.75, with 0 rejected jumps, 0 targets over the 0.08 m leash, 0 steps over 0.05 m, contour p95 7 mm (golden
# 6.7-7.0): fr3_accel_test/variable_speed/phase_eval/results/DECISIONS_20261102_strong.md, LIMITS_LEDGER.md E3, E6, E9, E13, E14.
CHUNK_ACCEL_V3_LAMBDA_DEFAULT = 3.0
# --phase-alpha: the controller's setpoint filter, which must be what `set_clip.py --show` reads back
CHUNK_ACCEL_V3_ALPHAS = (0.005, 0.01)
CHUNK_ACCEL_V3_ALPHA_DEFAULT = 0.005
# Audited state-conditioned FR3 configs: absolute pose+gripper at the wire, H=10.
# This is a space contract, not evidence of VLASH delay-aware fine-tuning.
VLASH_CONFIGS = frozenset(("pi05_fr3_all_state", "pi05_fr3_all_state_cfg"))


class ModeError(ValueError):
    def __init__(self, code, field, message):
        super().__init__(message)
        self.code, self.field = code, field


def resolve(body, *, default_rtc=True):
    rtc = body.get("rtc")
    if rtc is not None and not isinstance(rtc, bool):
        raise ModeError("invalid_type", "rtc", "rtc must be true or false")
    mode = body.get("inference_mode")
    if mode is None:
        return "rtc" if (default_rtc if rtc is None else rtc) else "plain"
    if mode not in MODES:
        raise ModeError(
            "inference_mode_invalid", "inference_mode", "choose plain, rtc or vlash"
        )
    if rtc is not None and rtc != (mode == "rtc"):
        raise ModeError(
            "inference_mode_conflict", "rtc", "rtc conflicts with inference_mode"
        )
    return mode


def recorded(body):
    try:
        return resolve(body, default_rtc=False)
    except ModeError:
        return "invalid"


def overlap(value=None, horizon=10):
    value = 2 if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < horizon:
        raise ModeError(
            "vlash_overlap_out_of_range",
            "vlash_overlap_steps",
            "overlap must be an integer greater than zero and below action_horizon",
        )
    return value


def capable(config):
    return config in VLASH_CONFIGS


def matches(served, form):
    return (
        served.get("model_id") == form.model_id
        and recorded(served) == form.inference_mode
        and (
            form.inference_mode != "vlash"
            or served.get("vlash_overlap_steps") == form.vlash_overlap_steps
        )
    )
