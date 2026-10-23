"""Pure mode and FR3 wire-space contracts; no training or serving claims."""

MODES = ("plain", "rtc", "vlash")
VLASH_SCRIPT = "lab/vlash/serve_policy_vlash.py"
VLASH_CLIENT = "lab/vlash/run_collect_vlash.py"
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
