"""What the arm controller the stack runs asks of the client command.

The facts are the ``arm`` part of the stack document's ``act`` (``stack_act.Act
._arm_doc``): which controller the control file names, which one the page's own A
was *started as* (its record, not the file: the file can be changed while A
runs), and what follows: the alpha rule v3 is forced to, why rule v3 is refused
(``changed``: the file names another controller than the running A; ``foreign``:
an A the page did not start runs while the file names the anyspeedup one;
``unknown``: A could not be judged), whether the ramp rules v1 and v2 are
offered, and the suffix of the default rollout group.

``read`` never raises. When the stack document cannot be read at all, the control
file alone answers, and a file that names the anyspeedup controller for a stack
that is on then reads ``unknown`` (nothing is forced and rule v3 is refused):
what runs on the arm is not known. With the lab's controller, no control file or
a stack that is off, the answer asks nothing, as it always did.
"""

import math

from . import command, control_site

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


def from_file() -> dict:
    """The facts from the control file alone: the best that can be said when the
    stack document is not there (see the module doc)."""
    try:
        loaded = control_site.read()
        stack_control = loaded.site.stack if loaded.state == "ok" else None
    except Exception:  # noqa: BLE001
        stack_control = None
    anyspeedup = bool(
        stack_control is not None and stack_control.enabled and stack_control.anyspeedup
    )
    return {
        "configured": None,
        "enabled": anyspeedup,
        "running": None,
        "status": "unknown" if anyspeedup else "idle",
        "alpha": None,
        "problem": "unknown" if anyspeedup else None,
        "ramp_ok": not anyspeedup or stack_control.arm_preset == "today",
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
