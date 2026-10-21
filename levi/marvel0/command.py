"""The demo client command (terminal E of the marvel0 procedures), rendered from a
closed schema as **text to copy**. Nothing here runs, reads or writes anything:
the page shows the command and a person pastes it into a terminal of their own.

What a person may choose is the form: model, task instruction, step and episode
limits, the control rate (``--control-hz``: 5, 10, 20, 25 or 50, nothing else),
``--min-z`` (never below the site floor, always below the home height), RTC on
or off with its horizon, rollout group and note. Everything else is fixed by
the site file and no form can reach it: the camera serials, the robot server
and policy server addresses, the home pose and its duration, every safety limit
(``--max-xy``, ``--max-step-m``, ``--max-step-rad``, the three angle ranges,
``--binarize-gripper``, ``--require-matching-server``,
``--allow-unknown-prompt``), the rollout root, and three constants: the
unattended mode is always off (``--levi-mode=off``; ``on`` starts without a
person at the start gate and is refused here), a new run, and no step-by-step.

The control rate moves three things with it (``docs/MARVEL0_RUN.md``, "Control
rate", has the evidence for each). At 10 Hz, the rate the policy was trained
at, no ``--control-hz`` is written and the command is the one this page always
made. Above 10 Hz the two per-step caps of the client (``--max-step-m``,
``--max-step-rad``: the client compares each commanded pose with the measured
one, per tick) are divided by ``rate / 10``, so the speed they allow in m/s and
rad/s stays what the site file allows at 10 Hz; below 10 Hz they stay as the
site file has them (a lower rate only lowers the speed they allow). The RTC
execute horizon, left empty, drops to the largest one that still leaves room
for the inference latency in beats of the chosen rate. Every other flag is a
length, an angle or a time in seconds, and is not touched.

Every flag is one ``--flag=value`` token, with two forms the client's parser
needs: the numbers of a tuple flag (``--home-pose`` and the three ranges)
follow the flag as separate tokens, as in the documented command, and a switch
is ``--flag`` or ``--no-flag``. The task instruction and the note are single
argv elements; each token is also ``shlex.quote``d in the text, and the
characters that would change meaning in a shell (quotes, backslash, ``$``,
backtick, ``!``) are refused.

``render`` ends by checking its own output (``verify_argv``): it reads the
form-chosen values back out of the argv, validates them again and renders
again, and refuses to hand out anything that differs from what the site and the
form allow.
"""

import math
import re
import shlex
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from . import site as marvel0_site
from .models import GROUP_MAX_CHARS, RESTRICTED_PROMPTS, default_group

SCRIPT = "examples/fr3_local/run_robotiq_client_collect_data.py"
NO_PROXY = "no_proxy=localhost,127.0.0.1,::1"
# The AERI form: the openpi .venv has no pyrealsense2, uv adds it for this run.
PREFIX = [
    "env",
    "-u",
    "VIRTUAL_ENV",
    NO_PROXY,
    "uv",
    "run",
    "--no-sync",
    "--with",
    "pyrealsense2==2.55.1.6486",
    "python",
    SCRIPT,
]
HANDS = (
    "Keep a hand on the physical e-stop. "
    "This preview starts nothing: the arm stack and the client have their own "
    "start buttons, each with its own confirmation."
)
# The client's measured floor of each task (TASK_MIN_Z of run_robotiq_client.py):
# an explicit --min-z beats it there, so a lower one is allowed (down to the site
# floor) but the fingertips can then reach the table. Any other instruction is
# compared with the plates floor, the lower of the two.
TASK_TABLE_MIN_Z = {RESTRICTED_PROMPTS[0]: 0.075, RESTRICTED_PROMPTS[1]: 0.05325}
OTHER_TABLE_MIN_Z = 0.05325
ACTION_HORIZON = 10  # actions per chunk
RTC_LATENCY_BEATS = 3  # measured inference latency in control beats, at REFERENCE_HZ

# The control rate. The options are exactly these; the rate the policy was trained
# at (the LeRobot dataset's ``meta/info.json`` says ``"fps": 10``; the client's
# ``Args.open_loop_horizon`` note says "trained with action_horizon=10 at 10 fps")
# is REFERENCE_HZ, the default, and the one for which the site file's per-step
# caps are written.
CONTROL_HZ_OPTIONS = (5, 10, 20, 25, 50)
REFERENCE_HZ = 10
CONTROL_HZ_DEFAULT = REFERENCE_HZ
# Below this many seconds an episode at a higher rate is shorter than the slower
# task's demos took at 10 Hz (screws: 90th percentile 287 steps = 28.7 s, the client's
# note at ``Args.max_timesteps``). The step limit is in steps and is never raised.
SHORT_EPISODE_S = 30
# The 99th percentile of the distance between a training action and the state it
# was taken from, the quantity the client's per-step check compares with its cap
# (translation in m; rotation: the largest of the three wrapped angle differences,
# in rad). Measured 2026-10-21 on every 6th episode (250 of 1497, 30336 rows) of
# ``lerobot_fr3_filtered_robotiq_v3``, read-only. A scaled cap below it rejects
# one training step in a hundred or more.
TRAIN_STEP_P99_M = 0.0384
TRAIN_STEP_P99_RAD = 0.0883
_GROUP = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{GROUP_MAX_CHARS - 1}}}")

# The flags in render order, one line each in the text: (flag, kind, source).
# kind: value (--flag=value), switch (--flag or --no-flag), tuple (flag, then
# one token per number). source: site (from the site file), form (what a
# person chooses), const (fixed here).
GROUPS = (
    (
        ("--remote-host", "value", "site"),
        ("--remote-port", "value", "site"),
        ("--view-camera-id", "value", "site"),
        ("--wrist-camera-id", "value", "site"),
        ("--franka-server-url", "value", "site"),
    ),
    (("--prompt", "value", "form"),),
    (
        ("--max-timesteps", "value", "form"),
        ("--control-hz", "value", "form"),
        ("--eval-num", "value", "form"),
        ("--min-z", "value", "form"),
    ),
    (
        ("--home-pose", "tuple", "site"),
        ("--home-duration-s", "value", "site"),
    ),
    (
        ("--max-xy", "value", "site"),
        ("--max-step-m", "value", "site"),
        ("--max-step-rad", "value", "site"),
        ("--rx-range", "tuple", "site"),
        ("--ry-range", "tuple", "site"),
        ("--rz-range", "tuple", "site"),
    ),
    (
        ("--binarize-gripper", "switch", "site"),
        ("--require-matching-server", "switch", "site"),
        ("--allow-unknown-prompt", "switch", "site"),
    ),
    (
        ("--levi-mode", "value", "const"),
        ("--new", "switch", "const"),
        ("--step-by-step", "switch", "const"),
        ("--rtc", "switch", "form"),
        ("--rtc-execute-horizon", "value", "form"),
        ("--open-loop-horizon", "value", "form"),
    ),
    (
        ("--rollout-root", "value", "site"),
        ("--rollout-group", "value", "form"),
        ("--eval-note", "value", "form"),
    ),
)
FLAGS = tuple(spec for group in GROUPS for spec in group)
TUPLE_ARITY = {"--home-pose": 6, "--rx-range": 2, "--ry-range": 2, "--rz-range": 2}
FIELDS = (
    "model_id",
    "prompt",
    "max_steps",
    "eval_num",
    "min_z",
    "rtc",
    "rtc_horizon",
    "rollout_group",
    "note",
    "control_hz",
)


class FormError(Exception):
    """The form is refused: a stable ``code`` (what a page translates), the
    ``field`` at fault, an English ``message`` and any ``extra`` facts."""

    def __init__(self, code, field, message, **extra):
        super().__init__(message)
        self.code, self.field, self.message, self.extra = code, field, message, extra


class SelfCheckError(RuntimeError):
    """The rendered argv is not what the site and the form allow (a bug)."""


@dataclass(frozen=True)
class Form:
    model_id: str
    prompt: str
    max_steps: int
    eval_num: int
    min_z: float
    rtc: bool
    rtc_horizon: int  # the RTC execute horizon, or the open-loop horizon with RTC off
    rollout_group: str
    note: str
    control_hz: int = CONTROL_HZ_DEFAULT  # one of CONTROL_HZ_OPTIONS


@dataclass(frozen=True)
class Preview:
    argv: list
    env: list
    cwd: str
    command: str
    warnings: list
    control: dict = dataclass_field(
        default_factory=dict
    )  # control_summary(): what the rate changes


def quote(token: str) -> str:
    return shlex.quote(token)


# --- the control rate -----------------------------------------------------------------------


def scaled_cap(site_value: float, hz: int) -> str:
    """A per-step cap of the site file as written for ``hz``: the site's value up
    to ``REFERENCE_HZ``, and divided by ``hz / REFERENCE_HZ`` above it, so the speed
    it allows (cap times rate) is never more than the site allows at 10 Hz."""
    if hz <= REFERENCE_HZ:
        return marvel0_site.number(site_value)
    return marvel0_site.number(site_value * REFERENCE_HZ / hz)


def latency_beats(hz: int) -> int:
    """The inference latency of an RTC request in control beats of ``hz``: the
    ``RTC_LATENCY_BEATS`` measured at 10 Hz are a time (0.3 s), and a beat is
    ``1 / hz`` seconds. Whole beats, rounded up (``rtc_chunker`` counts delay in ticks)."""
    return -(-RTC_LATENCY_BEATS * hz // REFERENCE_HZ)


def rtc_horizon_default(site_default: int, hz: int) -> int:
    """The RTC execute horizon of an empty field: the site's default up to 10 Hz;
    above it the largest horizon that still leaves ``latency_beats(hz)`` of the
    chunk's ``ACTION_HORIZON`` actions free, never above the site's default and
    never below 1 (at 50 Hz not even 1 leaves room, and the form warns)."""
    if hz <= REFERENCE_HZ:
        return site_default
    return min(site_default, max(1, ACTION_HORIZON - latency_beats(hz)))


def _seconds(value: float) -> float:
    return round(value, 6)


def control_summary(site, form) -> dict:
    """What the control rate of this form changes, in numbers (the page shows them
    next to the field, and the preview answers with them): the length of the
    episode in seconds, the per-step caps as written and the speeds they allow,
    the time of a chunk and of the executed horizon, and the RTC margin."""
    hz = form.control_hz
    cap_m = float(scaled_cap(site.safety.max_step_m, hz))
    cap_rad = float(scaled_cap(site.safety.max_step_rad, hz))
    beats = latency_beats(hz)
    out = {
        "hz": hz,
        "reference_hz": REFERENCE_HZ,
        "period_ms": _seconds(1000 / hz),
        "episode_s": _seconds(form.max_steps / hz),
        "step_cap_m": cap_m,
        "step_cap_rad": cap_rad,
        "speed_cap_m_s": _seconds(cap_m * hz),
        "speed_cap_rad_s": _seconds(cap_rad * hz),
        "chunk_s": _seconds(ACTION_HORIZON / hz),
        "horizon_s": _seconds(form.rtc_horizon / hz),
        "latency_beats": beats,
        "rtc": None,
    }
    if form.rtc:
        reached = form.rtc_horizon + beats
        out["rtc"] = {
            "horizon": form.rtc_horizon,
            "reached": reached,
            "margin": ACTION_HORIZON - reached,
        }
    return out


def control_public(site) -> dict:
    """What the page needs to show the same numbers before a command is made
    (``GET /api/levi/marvel0/site``, key ``control``)."""
    return {
        "options": list(CONTROL_HZ_OPTIONS),
        "default": CONTROL_HZ_DEFAULT,
        "reference_hz": REFERENCE_HZ,
        "action_horizon": ACTION_HORIZON,
        "latency_beats": RTC_LATENCY_BEATS,
        "short_episode_s": SHORT_EPISODE_S,
        "step_caps": {
            "m": site.safety.max_step_m,
            "rad": site.safety.max_step_rad,
        },
        "train_step_p99": {"m": TRAIN_STEP_P99_M, "rad": TRAIN_STEP_P99_RAD},
    }


# --- the form ----------------------------------------------------------------------------


def _normalise(prompt: str) -> str:
    """What the client (and the model's tokenizer) make of a task instruction
    before comparing it with the known ones."""
    return prompt.strip().replace("_", " ").replace("\n", " ")


def _prompt_problem(text, site, restricted):
    reason = marvel0_site.prompt_reason(text, site.caps.prompt_max_chars)
    if reason:
        return reason
    if restricted:
        known = {_normalise(k) for k in RESTRICTED_PROMPTS}
        if _normalise(text) not in known:
            return "unknown_for_model"
    return None


def prompt_problem(text, site, model):
    """Why ``text`` cannot be this model's task instruction, or ``None``: one
    of ``marvel0_site.prompt_reason``'s reasons, or ``unknown_for_model`` (a model
    of the restricted family takes only the client's two instructions)."""
    return _prompt_problem(text, site, model.restricted)


def _whole(body, field, default, lo, hi, code):
    value = body.get(field)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise FormError("invalid_type", field, f"{field} must be a whole number")
    if not lo <= value <= hi:
        raise FormError(
            code, field, f"{field} must be from {lo} to {hi}", min=lo, max=hi
        )
    return value


def _text(body, field, default):
    value = body.get(field)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise FormError("invalid_type", field, f"{field} must be text")
    return value.strip()


def _control_hz(body):
    """The control rate: one of ``CONTROL_HZ_OPTIONS`` as a whole number (a
    string, a float such as 20.0, a boolean or any other number is refused)."""
    value = body.get("control_hz")
    if value is None:
        return CONTROL_HZ_DEFAULT
    if isinstance(value, bool) or not isinstance(value, int):
        raise FormError(
            "invalid_type", "control_hz", "control_hz must be a whole number"
        )
    if value not in CONTROL_HZ_OPTIONS:
        raise FormError(
            "control_hz_unsupported",
            "control_hz",
            "this control rate is not offered",
            options=list(CONTROL_HZ_OPTIONS),
        )
    return value


def _min_z(site, body):
    value = body.get("min_z")
    if value is None:
        value = site.safety.min_z_default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FormError("invalid_type", "min_z", "min_z must be a number")
    try:
        number = float(value)
    except OverflowError:
        number = math.inf
    if not math.isfinite(number):
        raise FormError("invalid_type", "min_z", "min_z must be a finite number")
    # The value checked is the value written: ten significant digits.
    number = float(marvel0_site.number(number))
    floor, home = site.safety.min_z_floor, site.home.pose[2]
    if number <= 0:
        raise FormError("min_z_not_positive", "min_z", "min_z must be above zero")
    if number < floor:
        raise FormError(
            "min_z_below_floor",
            "min_z",
            "min_z cannot go below the site floor",
            floor=floor,
        )
    if number >= home:
        raise FormError(
            "min_z_above_home",
            "min_z",
            "min_z must be below the height of the home pose",
            home=home,
        )
    return number


def _validate(site, restricted, model_id, body):
    for key in body:
        if key not in FIELDS:
            raise FormError(
                "unknown_field", str(key), "this field is not part of the form"
            )
    caps = site.caps
    prompt = body.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise FormError("invalid_type", "prompt", "prompt must be text")
    prompt = (prompt or "").strip()
    problem = _prompt_problem(prompt, site, restricted)
    if problem == "unknown_for_model":
        raise FormError(
            "prompt_unknown_for_model",
            "prompt",
            "this model only takes the known task instructions",
        )
    if problem:
        raise FormError(
            "prompt_invalid",
            "prompt",
            "this task instruction cannot be used",
            reason=problem,
        )
    max_steps = _whole(
        body,
        "max_steps",
        caps.max_steps_default,
        1,
        caps.max_steps_cap,
        "max_steps_out_of_range",
    )
    eval_num = _whole(
        body,
        "eval_num",
        caps.eval_num_default,
        1,
        caps.eval_num_cap,
        "eval_num_out_of_range",
    )
    min_z = _min_z(site, body)
    control_hz = _control_hz(body)
    rtc = body.get("rtc")
    if rtc is None:
        rtc = True
    if not isinstance(rtc, bool):
        raise FormError("invalid_type", "rtc", "rtc must be true or false")
    horizon = _whole(
        body,
        "rtc_horizon",
        rtc_horizon_default(caps.rtc_horizon_default, control_hz)
        if rtc
        else caps.open_loop_default,
        1,
        caps.rtc_horizon_max if rtc else caps.open_loop_max,
        "rtc_horizon_out_of_range",
    )
    group = body.get("rollout_group")
    if group is None:
        group = default_group(model_id, rtc)
    if not isinstance(group, str):
        raise FormError("invalid_type", "rollout_group", "rollout_group must be text")
    if not _GROUP.fullmatch(group) or ".." in group:
        raise FormError(
            "rollout_group_invalid",
            "rollout_group",
            "use letters, digits, dot, dash and underscore, up to 80, starting with a letter or digit",
        )
    rate = "" if control_hz == CONTROL_HZ_DEFAULT else f" hz={control_hz}"
    note = _text(
        body,
        "note",
        f"marvel0 page: {model_id} rtc={'on' if rtc else 'off'} horizon={horizon}{rate}",
    )
    problem = marvel0_site.prompt_reason(note, caps.note_max_chars)
    if problem:
        raise FormError(
            "note_invalid", "note", "this note cannot be used", reason=problem
        )
    return Form(
        model_id,
        prompt,
        max_steps,
        eval_num,
        min_z,
        rtc,
        horizon,
        group,
        note,
        control_hz,
    )


def validate(site, model, body) -> Form:
    """The ``Form`` of a request body for this model, with defaults filled in;
    raises ``FormError``. A key outside the form is an error."""
    named = body.get("model_id")
    if named is not None and named != model.id:
        raise FormError("model_unknown", "model_id", "the body names another model")
    return _validate(site, model.restricted, model.id, body)


# --- the command ----------------------------------------------------------------------------


def _values(site, form) -> dict:
    """``{flag: (kind, value)}`` for every flag this form sets, from the site
    (everything that guards the arm), the form, and the constants."""
    values = {
        "--remote-host": ("value", site.server.remote_host),
        "--remote-port": ("value", str(site.port)),
        "--view-camera-id": ("value", site.cameras.view),
        "--wrist-camera-id": ("value", site.cameras.wrist),
        "--franka-server-url": ("value", site.server.robot_server_url),
        "--prompt": ("value", form.prompt),
        "--max-timesteps": ("value", str(form.max_steps)),
        "--eval-num": ("value", str(form.eval_num)),
        "--min-z": ("value", marvel0_site.number(form.min_z)),
        "--home-pose": ("tuple", tuple(marvel0_site.number(v) for v in site.home.pose)),
        **marvel0_site.fixed_items(site),
        # The site's per-step caps are for 10 Hz; above it they are divided by the
        # rate ratio, so the speeds they allow stay the site's.
        "--max-step-m": ("value", scaled_cap(site.safety.max_step_m, form.control_hz)),
        "--max-step-rad": (
            "value",
            scaled_cap(site.safety.max_step_rad, form.control_hz),
        ),
        "--levi-mode": ("value", "off"),
        "--new": ("switch", True),
        "--step-by-step": ("switch", False),
        "--rollout-root": ("value", site.rollout_root),
        "--rollout-group": ("value", form.rollout_group),
        "--eval-note": ("value", form.note),
    }
    if form.control_hz != CONTROL_HZ_DEFAULT:
        values["--control-hz"] = ("value", str(form.control_hz))
    if form.rtc:
        values["--rtc"] = ("switch", True)
        values["--rtc-execute-horizon"] = ("value", str(form.rtc_horizon))
    else:
        values["--open-loop-horizon"] = ("value", str(form.rtc_horizon))
    return values


def _lines(site, form) -> list:
    """The argv in the lines of the text: the prefix in two, then one list of
    tokens per group of flags."""
    values = _values(site, form)
    lines = [PREFIX[:4], PREFIX[4:]]
    for group in GROUPS:
        tokens = [
            token
            for flag, _, _ in group
            if flag in values
            for token in marvel0_site.flag_tokens(flag, *values[flag])
        ]
        if tokens:
            lines.append(tokens)
    return lines


def _argv(site, form) -> list:
    return [token for line in _lines(site, form) for token in line]


def _read_back(argv) -> dict:
    """The tokens after the prefix as ``{flag: value}`` (a string for a value
    flag, a bool for a switch, a tuple of strings for a tuple flag); raises
    ``SelfCheckError`` for an unknown flag, a repeated one, a missing value or
    a wrong count of numbers."""
    kinds = {flag: kind for flag, kind, _ in FLAGS}
    tail, found, i = argv[len(PREFIX) :], {}, 0
    while i < len(tail):
        name, equals, value = tail[i].partition("=")
        if name not in kinds and name.startswith("--no-") and not equals:
            name = "--" + name[5:]
            if kinds.get(name) != "switch":
                raise SelfCheckError(f"unknown flag {name[:40]}")
            off = True
        elif name not in kinds:
            raise SelfCheckError(f"unknown flag {name[:40]}")
        else:
            off = False
        if name in found:
            raise SelfCheckError(f"{name} appears twice")
        kind = kinds[name]
        if kind == "value":
            if not equals or not value:
                raise SelfCheckError(f"{name} has no value")
            found[name] = value
        elif equals:
            raise SelfCheckError(f"{name} takes no =value")
        elif kind == "switch":
            found[name] = not off
        else:
            count = TUPLE_ARITY[name]
            numbers = tail[i + 1 : i + 1 + count]
            try:
                finite = len(numbers) == count and all(
                    math.isfinite(float(n)) for n in numbers
                )
            except ValueError:
                finite = False
            if not finite:
                raise SelfCheckError(f"{name} needs {count} numbers")
            found[name] = tuple(numbers)
            i += count
        i += 1
    return found


def verify_argv(argv, site, restricted) -> None:
    """Refuse (``SelfCheckError``) an argv that is not exactly what the site
    and the form allow: the prefix, then known flags only, none twice, the right
    count of numbers after a tuple flag, the form's values valid again (for a
    model of the restricted family, its instructions only), and every other
    token what the site file dictates."""
    if argv[: len(PREFIX)] != PREFIX:
        raise SelfCheckError("the command does not start with the fixed prefix")
    found = _read_back(argv)
    rtc = found.get("--rtc") is True
    try:
        body = {
            "prompt": found["--prompt"],
            "max_steps": int(found["--max-timesteps"]),
            "eval_num": int(found["--eval-num"]),
            "min_z": float(found["--min-z"]),
            "rtc": rtc,
            "rtc_horizon": int(
                found["--rtc-execute-horizon" if rtc else "--open-loop-horizon"]
            ),
            "rollout_group": found["--rollout-group"],
            "note": found["--eval-note"],
        }
        if "--control-hz" in found:
            body["control_hz"] = int(found["--control-hz"])
        form = _validate(site, restricted, None, body)
    except (KeyError, ValueError) as exc:
        raise SelfCheckError(
            f"a flag is missing or malformed ({type(exc).__name__})"
        ) from exc
    except FormError as exc:
        raise SelfCheckError(f"{exc.field}: {exc.code}") from exc
    if _argv(site, form) != list(argv):
        raise SelfCheckError(
            "the command differs from what the site and the form allow"
        )


def _task_floor(prompt) -> float:
    """The client's measured floor for this instruction (it normalises the
    instruction before it looks the task up)."""
    for task, floor in TASK_TABLE_MIN_Z.items():
        if _normalise(task) == _normalise(prompt):
            return floor
    return OTHER_TABLE_MIN_Z


def _warning(code, text, **params) -> dict:
    """A warning: a stable ``code``, the English ``text``, and the ``params``
    the page words it with where the sentence holds numbers that are not fixed."""
    return {"code": code, "text": text, **({"params": params} if params else {})}


def _rtc_warning(form):
    """The RTC margin: the execute horizon plus the inference latency in beats
    of the chosen rate against the actions of a chunk. At 10 Hz this is the
    sentence this page always had (three beats)."""
    beats = latency_beats(form.control_hz)
    reached = form.rtc_horizon + beats
    if not form.rtc or reached <= ACTION_HORIZON:
        return None
    hz = form.control_hz
    if 1 + beats > ACTION_HORIZON:
        # Not even a horizon of 1 fits: the chunk runs out before the next one can arrive.
        return _warning(
            "rtc_never_fits",
            f"At {hz} Hz a chunk of {ACTION_HORIZON} actions lasts "
            f"{_seconds(ACTION_HORIZON * 1000 / hz):g} ms, shorter than the inference "
            f"latency of about {beats} beats; even an execute horizon of 1 reaches "
            f"{1 + beats} of {ACTION_HORIZON} actions, so the client will wait for "
            "every chunk and RTC cannot keep the arm moving.",
            hz=hz,
            beats=beats,
            chunk=ACTION_HORIZON,
            chunk_ms=_seconds(ACTION_HORIZON * 1000 / hz),
        )
    text = (
        f"Execute horizon {form.rtc_horizon} plus about {beats} beats "
        f"of inference latency reaches {reached} of {ACTION_HORIZON} actions per chunk, "
        "so the client may wait for chunks; use a smaller horizon if it does."
    )
    if hz == REFERENCE_HZ:
        return _warning("rtc_margin_low", text)
    seconds = RTC_LATENCY_BEATS / REFERENCE_HZ
    return _warning(
        "rtc_margin_low",
        f"{text} At {hz} Hz a beat is {_seconds(1000 / hz):g} ms, and the latency is a "
        f"time (about {seconds:g} s), so its beats grow with the rate.",
        horizon=form.rtc_horizon,
        beats=beats,
        reached=reached,
        chunk=ACTION_HORIZON,
        hz=hz,
        beat_ms=_seconds(1000 / hz),
    )


def _rate_warnings(site, form) -> list:
    """The warnings of a control rate other than 10 Hz (none at 10 Hz): the
    policy was trained at 10 Hz; the speed profile is untested on this rig; the
    scaled per-step caps may be tighter than the training steps; and an episode of
    the same step limit may be too short."""
    hz = form.control_hz
    if hz == CONTROL_HZ_DEFAULT:
        return []
    out = [
        _warning(
            "control_hz_not_trained",
            f"The policy was trained on data recorded at {REFERENCE_HZ} Hz (a chunk of "
            f"{ACTION_HORIZON} actions is one second). At {hz} Hz the client plays "
            f"the actions at {hz / REFERENCE_HZ:g} times that speed, a motion the "
            "policy never saw.",
            hz=hz,
            speed=f"{hz / REFERENCE_HZ:g}",
        ),
        _warning(
            "control_hz_untested",
            f"A control rate of {hz} Hz changes the arm's speed profile and has not "
            "been tried on this rig. Run the first episodes with the owner present and "
            "a hand on the physical e-stop.",
            hz=hz,
        ),
    ]
    cap_m = float(scaled_cap(site.safety.max_step_m, hz))
    cap_rad = float(scaled_cap(site.safety.max_step_rad, hz))
    if cap_m < TRAIN_STEP_P99_M or cap_rad < TRAIN_STEP_P99_RAD:
        out.append(
            _warning(
                "step_cap_tight",
                f"At {hz} Hz the per-step caps are {cap_m:g} m and {cap_rad:g} rad "
                f"(the speeds the site allows at {REFERENCE_HZ} Hz, kept). One training "
                f"step in a hundred is larger than {TRAIN_STEP_P99_M:g} m or "
                f"{TRAIN_STEP_P99_RAD:g} rad, so the client will reject chunks, ask "
                "again, and spend a step of the budget on each.",
                hz=hz,
                cap_m=cap_m,
                cap_rad=cap_rad,
                p99_m=TRAIN_STEP_P99_M,
                p99_rad=TRAIN_STEP_P99_RAD,
            )
        )
    seconds = form.max_steps / hz
    if hz > REFERENCE_HZ and seconds < SHORT_EPISODE_S:
        out.append(
            _warning(
                "episode_short",
                f"{form.max_steps} steps at {hz} Hz last at least {_seconds(seconds):g} s, "
                f"shorter than the {SHORT_EPISODE_S} s the demos of the slower task took "
                f"at {REFERENCE_HZ} Hz. The step limit is in steps and is not raised for "
                "you; raise it if the task is cut off.",
                steps=form.max_steps,
                hz=hz,
                seconds=_seconds(seconds),
                minimum=SHORT_EPISODE_S,
            )
        )
    return out


def _warnings(site, model, form) -> list:
    out = []
    if not model.verified:
        out.append(
            _warning(
                "unverified_model",
                "No real-GPU serving check is recorded for this model; "
                "watch the first steps with a hand on the physical e-stop.",
            )
        )
    rtc = _rtc_warning(form)
    if rtc:
        out.append(rtc)
    table = _task_floor(form.prompt)
    if form.min_z < table:
        out.append(
            _warning(
                "min_z_below_task_table",
                f"min-z {marvel0_site.number(form.min_z)} is below {table}, "
                "the floor the client measured for this task; "
                "the fingertips can reach the table.",
            )
        )
    return out + _rate_warnings(site, form)


def render(site, model, form) -> Preview:
    """The command for this form as an argv and as text to copy. Checks its own
    output (``verify_argv``) before returning it."""
    if form.model_id != model.id:
        raise ValueError("the form is for another model")
    lines = _lines(site, form)
    argv = [token for line in lines for token in line]
    verify_argv(argv, site, model.restricted)
    text = " \\\n  ".join(
        [f"cd {quote(site.openpi_checkout)} &&"]
        + [" ".join(quote(t) for t in line) for line in lines]
    )
    return Preview(
        argv=argv,
        env=[NO_PROXY],
        cwd=site.openpi_checkout,
        command=text,
        warnings=_warnings(site, model, form),
        control=control_summary(site, form),
    )
