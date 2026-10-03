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


def place_state(proposals, episode):
    """What the episode's last ``place`` time segment says: ``success``,
    ``failure``, ``unknown``, ``none`` (the episode has no ``place`` segment),
    or ``missing`` (``proposals`` is None: there is no committed time-segment
    set to read).

    "Last" is the segment that starts latest; of two that start together, the
    one listed last. A segment with no recorded outcome counts as unknown."""
    if proposals is None:
        return "missing"
    places = sorted(
        (
            p
            for p in proposals
            if p.get("episode_index") == episode
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

    The condition holds when the last place segment is a success. A failure,
    or no place segment, makes a success a failure. An unknown outcome makes it
    a failure that is also undecided (a person looks at it). With no time
    segments to read (``missing``) the outcome stands on the rest of the rule
    and ``basis.missing_inputs`` says so; ``anchored.undecided`` then marks a
    success as undecided. A failure stays a failure whatever the place says."""
    if not basis.get("require_place"):
        return outcome, basis, False
    basis = {**basis, "place_outcome": place}
    if place == "missing":
        basis["missing_inputs"] = sorted({*basis.get("missing_inputs", []), PLACE})
        return outcome, basis, False
    if outcome != "success" or place == "success":
        return outcome, basis, False
    return "failure", basis, place == "unknown"
