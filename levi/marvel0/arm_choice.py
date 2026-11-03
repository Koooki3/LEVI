"""The arm controller a person chose for terminal A on the marvel0 page.

The control file (``control_site.py``) is the rig owner's: the page reads it and
never writes it, and its ``[stack] arm_controller`` / ``arm_preset`` are the
**default** controller of terminal A. On A's card a person may choose another
one from a closed list (``CHOICES``): the lab's controller, or the anyspeedup one
at ``today`` or ``S1``. The page keeps that choice in its own workspace, next to
the stack's records: ``<workspace state>/marvel0/stack/arm_choice/record.json``
(folder 0700, file 0600, written whole and read back the way ``record.py``
writes and reads every record of the page, schema ``SCHEMA``), with one audit
line per change in ``history.jsonl`` beside it. "Use the control file's default"
writes the record with no choice in it.

``effective`` is the one place that decides which controller terminal A is
started as: the saved choice when there is one and it can be used, else the
file's. Everything downstream reads its answer: A's specs (the argv, the record
A's start writes, the checks of that start), the stack document's ``act.arm``
(``configured``, ``running`` against it, the alpha rule v3 is forced to while no
A runs, ``ramp_ok``, the default rollout group's suffix) and
``arm_facts.from_file``.

What the choice can and cannot do:

* it is changed only while A is stopped and no guided start or stop is in
  progress (``stack_act.Act.choice_blockers``), so the controller an A runs is
  always the one its record names;
* an anyspeedup choice needs the file's ``anyspeedup_root`` (the same check the
  file's own value went through); whether that checkout is there and built is
  still asked at A's start (``arm_controller_missing`` / ``_not_built``);
* a saved choice that can no longer be used (the file lost its
  ``anyspeedup_root``) or cannot be read keeps A from starting
  (``arm_choice_no_anyspeedup``, ``arm_choice_unreadable``) instead of starting
  the file's controller unannounced; the document still shows the file's;
* ``S2`` is refused with its own code, as in the file
  (``control_site.ARM_PRESETS_REFUSED``).

What it is worth: the same as the control file's switches. It stops a mistake
on the page; another program of the same account can write the folder.
"""

import dataclasses
from pathlib import Path

from . import control_site, record

SCHEMA = "levi.marvel0.arm_choice.v1"
FOLDER = "arm_choice"
# What a person may choose: (controller, preset). The lab's controller has no
# preset. A test keeps the anyspeedup presets inside ``control_site.ARM_PRESETS``.
CHOICES = (("lab", None), ("anyspeedup", "today"), ("anyspeedup", "S1"))
# The request's controller that clears the choice: A goes back to the file's.
DEFAULT = "default"
# ``state`` of the choice as the page shows it.
STATES = ("default", "chosen", "unusable", "unreadable")

NONE = {"state": "default", "choice": None, "at": None}
UNREADABLE = {"state": "unreadable", "choice": None, "at": None}


class Invalid(Exception):
    """A request for something that is not one of ``CHOICES``: ``code`` and
    the facts as ``params`` (a code of ``stack_act.TEXT``)."""

    def __init__(self, code, **params):
        self.code = code
        self.params = params
        super().__init__(code)


def check(controller, preset):
    """The choice a request asks for: one of ``CHOICES``, or None for "the
    control file's default". Raises ``Invalid``: ``arm_choice_preset_refused``
    for a preset the file refuses too (``S2``), ``arm_choice_invalid`` for
    anything else that is not on the list."""
    if controller == DEFAULT and preset is None:
        return None
    if (
        controller == "anyspeedup"
        and isinstance(preset, str)
        and preset in control_site.ARM_PRESETS_REFUSED
    ):
        raise Invalid(
            "arm_choice_preset_refused",
            preset=preset,
            reason=control_site.ARM_PRESETS_REFUSED[preset],
        )
    pair = (controller, preset)
    if pair not in CHOICES:
        raise Invalid("arm_choice_invalid")
    return pair


def anyspeedup_root_ok(stack_control) -> bool:
    """Does the control file name an anyspeedup checkout the page may start
    (the file's own check of ``anyspeedup_root``)?"""
    root = getattr(stack_control, "anyspeedup_root", None)
    try:
        control_site._strict_path(root)
    except control_site._Bad:
        return False
    return True


def unusable(stack_control, choice):
    """Why ``choice`` cannot be used with the file's ``stack_control`` (a code),
    or None. None (the file's default) is always usable."""
    if choice is None:
        return None
    if choice not in CHOICES:
        return "arm_choice_unreadable"
    if choice[0] == "anyspeedup" and not anyspeedup_root_ok(stack_control):
        return "arm_choice_no_anyspeedup"
    return None


# --- the record ----------------------------------------------------------------------


def folder(state_root) -> Path:
    return Path(state_root) / FOLDER


def read(state_root) -> dict:
    """The saved choice: ``{"state": "default" | "chosen" | "unreadable",
    "choice": (controller, preset) | None, "at"}``. Never raises and creates
    nothing. A record that is there and is not exactly what ``write`` writes is
    unreadable, never a guess."""
    try:
        value = record.read_record(folder(state_root))
    except (record.RecordCorrupt, OSError, ValueError):
        return dict(UNREADABLE)
    if value is None:
        return dict(NONE)
    if value.get("schema") != SCHEMA:
        return dict(UNREADABLE)
    at = value.get("at")
    at = at if isinstance(at, (int, float)) and not isinstance(at, bool) else None
    raw = value.get("choice", "missing")
    if raw is None:
        return {**NONE, "at": at}
    if not isinstance(raw, dict) or set(raw) != {"controller", "preset"}:
        return dict(UNREADABLE)
    pair = (raw["controller"], raw["preset"])
    if pair not in CHOICES:
        return dict(UNREADABLE)
    return {"state": "chosen", "choice": pair, "at": at}


def write(directory, choice, request_id, now) -> dict:
    """Save ``choice`` (one of ``CHOICES``, or None for the file's default) in
    ``directory`` (made by the caller, 0700), whole, under the record's lock."""
    if choice is not None and choice not in CHOICES:
        raise ValueError("not a choice of the page")
    value = {
        "schema": SCHEMA,
        "choice": None
        if choice is None
        else {"controller": choice[0], "preset": choice[1]},
        "request_id": request_id,
        "at": round(now, 3),
    }
    with record.locked(directory):
        record.write_record(directory, value)
    return value


# --- what A is started as --------------------------------------------------------------


def identity(stack_control):
    """``{"controller", "preset"}`` of a ``StackControl`` (the preset is None for
    the lab's controller), or None."""
    if stack_control is None:
        return None
    return {
        "controller": stack_control.arm_controller,
        "preset": stack_control.arm_preset_in_use,
    }


def effective(stack_control, saved) -> tuple:
    """``(StackControl, view)``: the control file's ``stack_control`` with
    terminal A's controller as the page will start it, and what the page shows
    of the choice. The saved choice (``read``) wins when it can be used;
    otherwise the file's own controller stands (and ``view["code"]`` says why
    the choice was not used: A's start is then refused with that code).
    ``stack_control`` None (no valid file): None."""
    saved = saved if isinstance(saved, dict) else UNREADABLE
    state, choice = saved.get("state"), saved.get("choice")
    view = {
        "state": "default",
        "controller": None,
        "preset": None,
        "code": None,
        "at": saved.get("at"),
    }
    if state == "unreadable" or state not in ("default", "chosen"):
        view.update(state="unreadable", code="arm_choice_unreadable")
        return stack_control, view
    if state == "default":
        return stack_control, view
    view.update(controller=choice[0], preset=choice[1])
    code = unusable(stack_control, choice)
    if stack_control is None or code is not None:
        view.update(state="unusable", code=code or "arm_choice_no_anyspeedup")
        return stack_control, view
    view["state"] = "chosen"
    changes = {"arm_controller": choice[0]}
    if choice[1] is not None:
        changes["arm_preset"] = choice[1]
    return dataclasses.replace(stack_control, **changes), view


def options(stack_control) -> list:
    """The choices as A's card offers them: each with whether it is the file's
    default and whether it can be chosen now (``code``: why not)."""
    default = identity(stack_control)
    out = []
    for controller, preset in CHOICES:
        code = unusable(stack_control, (controller, preset))
        out.append(
            {
                "controller": controller,
                "preset": preset,
                "default": default == {"controller": controller, "preset": preset},
                "allowed": stack_control is not None and code is None,
                "code": code,
            }
        )
    return out
