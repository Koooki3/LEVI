"""Which questions are physical safety: a static table, not a model judgement.

Keys are (question kind, context). A physical question never gets a local answer and
never goes to the panel: it goes to a person. A pair that is not listed is physical
(fail-closed), and so is an unknown context.
"""

from __future__ import annotations

from types import MappingProxyType

from .contracts import Context, Safety

P, N = Safety.PHYSICAL, Safety.NON_PHYSICAL
OFF, LIVE = Context.OFFLINE, Context.LIVE

# The live completion judgement ("may the arm be released") is physical; the same
# question on recorded data (offline) is not. Source: the 2026-10-22 decision that
# anything that moves the arm or says "done, release it" goes to a person, and the
# default adopted in the 2026-11-08 graded-answer design (open question 1, which the
# user has not answered yet): live is physical, offline is not. Fail-closed either way.
_TABLE = {
    ("completion_check", LIVE): P,
    ("completion_check", OFF): N,
    ("reset_ready", LIVE): P,
    ("release_round", LIVE): P,
    ("plan", LIVE): P,  # becomes the policy prompt
    ("workspace_clear", LIVE): P,
    ("person_or_obstacle", LIVE): P,
    ("collision", LIVE): P,
    ("grip_force", LIVE): P,
    ("wait_human_reason", LIVE): P,
    ("boundary", OFF): N,
    ("description", OFF): N,
    ("outcome_label", OFF): N,
    ("qc_flag", OFF): N,
    ("inventory", OFF): N,
    ("plan", OFF): N,
    ("tempo", OFF): N,
}
SAFETY_TABLE = MappingProxyType(_TABLE)


def classify(kind: str, context) -> Safety:
    try:
        ctx = Context(context)
    except ValueError:
        return Safety.PHYSICAL
    return _TABLE.get((kind, ctx), Safety.PHYSICAL)


def is_physical(kind: str, context) -> bool:
    return classify(kind, context) is Safety.PHYSICAL
