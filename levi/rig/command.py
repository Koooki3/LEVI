"""The demo client command (terminal E of the rig procedures), rendered from a
closed schema as **text to copy**. Nothing here runs, reads or writes anything:
the page shows the command and a person pastes it into a terminal of their own.

What a person may choose is the form: model, task instruction, step and episode
limits, ``--min-z`` (never below the site floor, always below the home height),
RTC on or off with its horizon, rollout group and note. Everything else is
fixed by the site file and no form can reach it: the camera serials, the robot
server and policy server addresses, the home pose and its duration, every
safety limit (``--max-xy``, ``--max-step-m``, ``--max-step-rad``, the three
angle ranges, ``--binarize-gripper``, ``--require-matching-server``,
``--allow-unknown-prompt``), the rollout root, and three constants: the
unattended mode is always off (``--levi-mode=off``; ``on`` starts without a
person at the start gate and is refused here), a new run, and no step-by-step.

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

from . import site as rig_site
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
    "This page never starts the arm stack or this client."
)
# The client's measured floor of each task (TASK_MIN_Z of run_robotiq_client.py):
# an explicit --min-z beats it there, so a lower one is allowed (down to the site
# floor) but the fingertips can then reach the table. Any other instruction is
# compared with the plates floor, the lower of the two.
TASK_TABLE_MIN_Z = {RESTRICTED_PROMPTS[0]: 0.075, RESTRICTED_PROMPTS[1]: 0.05325}
OTHER_TABLE_MIN_Z = 0.05325
ACTION_HORIZON = 10  # actions per chunk
RTC_LATENCY_BEATS = 3  # measured inference latency in control beats
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


@dataclass(frozen=True)
class Preview:
    argv: list
    env: list
    cwd: str
    command: str
    warnings: list


def quote(token: str) -> str:
    return shlex.quote(token)


# --- the form ----------------------------------------------------------------------------


def _normalise(prompt: str) -> str:
    """What the client (and the model's tokenizer) make of a task instruction
    before comparing it with the known ones."""
    return prompt.strip().replace("_", " ").replace("\n", " ")


def _prompt_problem(text, site, restricted):
    reason = rig_site.prompt_reason(text, site.caps.prompt_max_chars)
    if reason:
        return reason
    if restricted:
        known = {_normalise(k) for k in RESTRICTED_PROMPTS}
        if _normalise(text) not in known:
            return "unknown_for_model"
    return None


def prompt_problem(text, site, model):
    """Why ``text`` cannot be this model's task instruction, or ``None``: one
    of ``rig_site.prompt_reason``'s reasons, or ``unknown_for_model`` (a model
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
    number = float(rig_site.number(number))
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
    rtc = body.get("rtc")
    if rtc is None:
        rtc = True
    if not isinstance(rtc, bool):
        raise FormError("invalid_type", "rtc", "rtc must be true or false")
    horizon = _whole(
        body,
        "rtc_horizon",
        caps.rtc_horizon_default if rtc else caps.open_loop_default,
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
    note = _text(
        body,
        "note",
        f"rig page: {model_id} rtc={'on' if rtc else 'off'} horizon={horizon}",
    )
    problem = rig_site.prompt_reason(note, caps.note_max_chars)
    if problem:
        raise FormError(
            "note_invalid", "note", "this note cannot be used", reason=problem
        )
    return Form(model_id, prompt, max_steps, eval_num, min_z, rtc, horizon, group, note)


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
        "--min-z": ("value", rig_site.number(form.min_z)),
        "--home-pose": ("tuple", tuple(rig_site.number(v) for v in site.home.pose)),
        **rig_site.fixed_items(site),
        "--levi-mode": ("value", "off"),
        "--new": ("switch", True),
        "--step-by-step": ("switch", False),
        "--rollout-root": ("value", site.rollout_root),
        "--rollout-group": ("value", form.rollout_group),
        "--eval-note": ("value", form.note),
    }
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
            for token in rig_site.flag_tokens(flag, *values[flag])
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


def _warning(code, text) -> dict:
    return {"code": code, "text": text}


def _warnings(model, form) -> list:
    out = []
    if not model.verified:
        out.append(
            _warning(
                "unverified_model",
                "No real-GPU serving check is recorded for this model; "
                "watch the first steps with a hand on the physical e-stop.",
            )
        )
    reached = form.rtc_horizon + RTC_LATENCY_BEATS
    if form.rtc and reached > ACTION_HORIZON:
        out.append(
            _warning(
                "rtc_margin_low",
                f"Execute horizon {form.rtc_horizon} plus about {RTC_LATENCY_BEATS} beats "
                f"of inference latency reaches {reached} of {ACTION_HORIZON} actions per chunk, "
                "so the client may wait for chunks; use a smaller horizon if it does.",
            )
        )
    table = _task_floor(form.prompt)
    if form.min_z < table:
        out.append(
            _warning(
                "min_z_below_task_table",
                f"min-z {rig_site.number(form.min_z)} is below {table}, "
                "the floor the client measured for this task; "
                "the fingertips can reach the table.",
            )
        )
    return out


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
        warnings=_warnings(model, form),
    )
