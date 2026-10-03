"""The live service's share of the terminal-aware episode verdict.

A release-review spec with ``episode.require_place`` (``generic-release.v2``)
asks for one more thing than the review run can see: the episode's last
``place`` time segment must not be a failure or unknown. The review run works
from the gripper signal and the model's answers; the time segments are the
other labelling step of the same batch, committed before the review. The
worker therefore reads them (``place_state`` over the committed change set's
proposals) and merges the result into the review's outcome (``merge``).

Both functions are pure over plain dicts, so the rule can be replayed offline.
Standard library only.
"""

PLACE = "place"


def place_state(change, episode):
    """What the episode's last ``place`` time segment says: ``success``,
    ``failure``, ``unknown``, ``none`` (the episode has no ``place`` segment),
    or ``missing`` (``change`` is None: there is no committed time-segment set
    to read).

    ``change`` is the committed change set (a dict with ``proposals`` and, when
    a person reviewed it, ``decisions``). A segment a person rejected is not
    part of the episode's labels and is left out. "Last" is the segment that
    starts latest; of two that start together, the one listed last. A segment
    with no recorded outcome counts as unknown."""
    if change is None:
        return "missing"
    rejected = {
        k for k, v in (change.get("decisions") or {}).items() if v == "rejected"
    }
    places = sorted(
        (
            p
            for i, p in enumerate(change.get("proposals") or [])
            if str(i) not in rejected
            and p.get("kind") == "segment"
            and p.get("episode_index") == episode
            and p.get("style", "subtask") == "subtask"
            and p.get("subtask_id") == PLACE
        ),
        key=lambda p: p.get("start") or 0.0,
    )
    if not places:
        return "none"
    return places[-1].get("outcome") or "unknown"


def merge(outcome, basis, place):
    """``(outcome, basis, undecided)`` once the place condition is applied to
    a review outcome and its basis; a review without ``basis.require_place``
    passes through unchanged.

    The review's own outcome does not apply the condition, so its basis names
    ``place`` in ``missing_inputs`` (a success is undecided wherever the review
    record alone is read: the training manifest, a person accepting the
    proposal). Here the input is given, and the name is dropped.

    The condition holds when the last place segment is a success. A failure,
    or no place segment, makes a success a failure. An unknown outcome, or no
    time segments to read at all (``missing``, which ``basis.missing_inputs``
    keeps), makes it a failure that is also undecided: an input that could not
    be read is never taken for a success. A failure stays a failure whatever
    the place says."""
    if not basis.get("require_place"):
        return outcome, basis, False
    basis = {**basis, "place_outcome": place}
    missing = sorted(set(basis.get("missing_inputs", [])) - {PLACE})
    if place == "missing":
        missing = sorted({*missing, PLACE})
    if missing:
        basis["missing_inputs"] = missing
    else:
        basis.pop("missing_inputs", None)
    if outcome != "success" or place == "success":
        return outcome, basis, False
    return "failure", basis, place in ("unknown", "missing")
