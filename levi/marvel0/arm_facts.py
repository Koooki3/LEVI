"""What the arm controller the stack runs asks of the client command.

The facts are the ``arm`` part of the stack document's ``act`` (``stack_act.Act
._arm_doc``): which controller the page starts A as (the choice made on A's card,
else the control file's: ``arm_choice.effective``), which one the page's own A
was *started as* (its record, not the file: the file can be changed while A
runs), and what follows: the alpha rule v3 is forced to, why rule v3 is refused
(``changed``: the page would start another controller than the running A;
``foreign``: an A the page did not start runs while the anyspeedup one is chosen;
``unknown``: A could not be judged), whether the ramp rules v1 and v2 are
offered, and the suffix of the default rollout group.

``read`` never raises. When the stack document cannot be read at all, the control
file and the saved choice answer (``from_file``), and an anyspeedup controller
chosen for a stack that is on, or a saved choice that cannot be read or used,
then reads ``unknown`` (nothing is forced and rule v3 is refused): what runs on
the arm is not known. With the lab's controller, no control file or a stack that
is off, the answer asks nothing, as it always did.
"""

import math

from . import arm_choice, command, control_site

PROBLEMS = ("changed", "foreign", "unknown")


def from_document(fresh=False):
    """``act.arm`` of the stack document (the cached one the page polls, or a
    fresh read), or None when it cannot be had."""
    try:
        from . import stack_api

        document = stack_api.supervisor().view() if fresh else stack_api.stack_view()
        arm = document["act"]["arm"]
    except Exception:  # noqa: BLE001 - what cannot be read is answered from the file
        return None
    return arm if isinstance(arm, dict) else None


def _choice_now() -> dict:
    """The arm controller chosen on A's card (``arm_choice.read`` of the
    product's own stack folder); unreadable when it cannot be had."""
    try:
        from . import stack_act

        return arm_choice.read(stack_act._state_root())
    except Exception:  # noqa: BLE001 - what cannot be read is not a choice
        return dict(arm_choice.UNREADABLE)


def from_file() -> dict:
    """The facts from the control file and the choice made on A's card alone
    (``arm_choice.effective``, the same decision the stack document makes): the
    best that can be said when the stack document is not there (see the module
    doc).

    A saved choice that cannot be read or can no longer be used (``effective``
    then answers with the file's controller) also reads ``unknown`` for a stack
    that is on, whatever the file names: A may have been started from that
    choice before it broke, and without A's record (the stack document) which
    controller runs is not known, so nothing is forced and neither rule v3 nor
    the ramp rules are offered."""
    try:
        loaded = control_site.read()
        file_stack = loaded.site.stack if loaded.state == "ok" else None
        stack_control, view = arm_choice.effective(file_stack, _choice_now())
    except Exception:  # noqa: BLE001
        stack_control, view = None, {}
    on = bool(stack_control is not None and stack_control.enabled)
    anyspeedup = on and bool(stack_control.anyspeedup)
    blind = on and view.get("state") in ("unreadable", "unusable")
    unknown = anyspeedup or blind
    return {
        "configured": None,
        "enabled": anyspeedup,
        "running": None,
        "status": "unknown" if unknown else "idle",
        "alpha": None,
        "problem": "unknown" if unknown else None,
        "ramp_ok": not blind
        and (not anyspeedup or stack_control.arm_preset == "today"),
        "suffix": "",
    }


def read(fresh=False) -> dict:
    """The arm facts now (``fresh``: the stack is looked at again, as a start
    does; else the page's two-second cache). Never raises."""
    return from_document(fresh) or from_file()


def _controller(entry):
    return entry.get("controller") if isinstance(entry, dict) else None


def anyspeedup_involved(facts) -> bool:
    """Does the running A, or the control file of a stack that is on, name the
    anyspeedup controller?"""
    if not isinstance(facts, dict):
        return False
    if _controller(facts.get("running")) == "anyspeedup":
        return True
    return facts.get("enabled") is True and _controller(facts.get("configured")) == (
        "anyspeedup"
    )


def alpha_of(facts):
    value = facts.get("alpha") if isinstance(facts, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def arm_of(facts) -> command.Arm:
    """The ``command.Arm`` of the facts."""
    facts = facts if isinstance(facts, dict) else {}
    suffix = facts.get("suffix")
    return command.Arm(
        alpha=alpha_of(facts),
        ramp_ok=facts.get("ramp_ok") is not False,
        suffix=suffix if isinstance(suffix, str) else "",
        anyspeedup=anyspeedup_involved(facts),
    )


def problem_of(facts):
    """Why rule v3 is refused (one of ``PROBLEMS``), or None."""
    problem = facts.get("problem") if isinstance(facts, dict) else None
    return problem if problem in PROBLEMS else None


def running_of(facts) -> tuple:
    """``(controller, preset)`` of the A the page started and that runs, or
    ``(None, None)``: nothing is claimed for an A it did not start."""
    running = facts.get("running") if isinstance(facts, dict) else None
    if not isinstance(running, dict):
        return (None, None)
    controller, preset = running.get("controller"), running.get("preset")
    return (
        controller if controller in control_site.ARM_CONTROLLERS else None,
        preset if isinstance(preset, str) else None,
    )
