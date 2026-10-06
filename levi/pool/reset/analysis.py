"""What part of an episode can be reversed, and how.

A forward episode reversed is a valid reset only where each reversed step is
something a robot could do. Reaching and carrying are. A grasp reversed is a
release onto the surface the object came from: valid. A release reversed is a
grasp, valid only if the object is still where the gripper can take it: if it
fell flat or rolled off, the reversed episode would close the fingers on
nothing while the object "flies" into them.

So the analysis finds every release, measures what the object did next (see
``vision``), and sorts each release into:

- ``in_place``: the object stayed put; reverse the frames as they are;
- ``in_reach``: it settled between the open fingers; the frames of its fall
  (the hold frame to the rest frame, arm still) are cut out, so every
  remaining frame is real and consistent and the gripper closes on an object
  that is there;
- ``escaped``: it left the fingers' reach: a real approach and grasp is
  missing from the data;
- ``unknown``: nothing could be measured (the arm left at once, no camera, the
  object still moving). Never reversed on a guess.

An episode with only the first two kinds is reversed whole. Otherwise it is
left out, or (``on_ineligible="partial"``) reversed from its last safe hold on,
or completed with a recorded stretch (``bridge``).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ...conversion import media
from . import contract as contract_mod
from . import events as events_mod
from . import profile, vision
from .schema import ResetOptions


@dataclass
class Evidence:
    """What the analysis reads of one forward episode (rows are the rows of
    the episode as it would be exported)."""

    key: str
    state: np.ndarray
    action: np.ndarray
    width: np.ndarray | None = None  # measured finger width per row (m)
    release_video: Path | None = None
    already_reset: bool = False


@dataclass
class ReleaseResult:
    row: int
    hold_row: int | None = None
    rest_row: int | None = None
    klass: str = "unknown"
    reason: str | None = None
    metrics: dict = field(default_factory=dict)
    seam: dict = field(default_factory=dict)

    def record(self) -> dict:
        return {
            "row": self.row,
            "hold_row": self.hold_row,
            "rest_row": self.rest_row,
            "class": self.klass,
            "reason": self.reason,
            "metrics": self.metrics,
            "seam": self.seam,
        }


@dataclass
class Analysis:
    key: str
    eligible: bool = False
    reason: str | None = None  # why not (an exclusion reason)
    detail: str | None = None
    scope: str = "full"  # full | partial
    generation: str = "reversed_source"
    semantics: str | None = None
    events: list[dict] = field(default_factory=list)
    releases: list[ReleaseResult] = field(default_factory=list)
    keep: list[int] = field(default_factory=list)  # source rows kept, ascending
    edits: dict[int, float] = field(default_factory=dict)
    bridge_anchor: int | None = None  # hold row of the one release a record replaces
    bridgeable: bool = False
    rows_cut: int | None = None
    anchor_keep: list[int] = field(
        default_factory=list
    )  # rows up to the anchor, ascending

    def record(self) -> dict:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "detail": self.detail,
            "scope": self.scope,
            "generation": self.generation,
            "semantics": self.semantics,
            "events": self.events,
            "releases": [r.record() for r in self.releases],
            "rows_kept": len(self.keep),
            "rows_cut": self.rows_cut,
            "bridge_anchor": self.bridge_anchor,
            "profile": profile.VERSION,
        }


def _wrap(delta):
    """Angle differences on (-pi, pi]: roll near +-pi sits on either side of the
    cut in two different recordings."""
    return (np.asarray(delta) + np.pi) % (2 * np.pi) - np.pi


def pose_distance(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    return float(np.linalg.norm(a[:3] - b[:3])), float(
        np.abs(_wrap(a[3:6] - b[3:6])).max()
    )


def _release(
    ev: Evidence,
    event: events_mod.GripEvent,
    others: list[events_mod.GripEvent],
    gripper: int,
    opened: float,
    allowed: set[str],
    frames: Callable[[Path, list[int]], dict],
    reviewer,
    opts: ResetOptions,
) -> ReleaseResult:
    n = len(ev.state)
    res = ReleaseResult(row=event.row)
    holds = [
        i
        for i in range(
            event.row - 1, max(-1, event.row - 1 - profile.HOLD_ROWS_BEFORE), -1
        )
        if ev.state[i, gripper] != opened
    ]
    if not holds:
        res.reason = "no_hold_frame"
        return res
    res.hold_row = holds[0]
    first = min(event.motion_end, n - 1)
    # Never past the next gripper event: that is another scene.
    limit = min((o.row for o in others if o.row > event.row), default=n)
    rests = []
    for i in range(first, min(limit, first + profile.SETTLE_ROWS_AFTER + 1)):
        near = min(
            (pose_distance(ev.state[i], ev.state[h]) for h in holds), key=lambda d: d[0]
        )
        if near[0] > profile.SEAM_POSITION_TOL or near[1] > profile.SEAM_ROTATION_TOL:
            break  # the arm has left: later rows see another scene
        if rests:
            step = pose_distance(ev.state[i], ev.state[rests[-1]])
            if step[0] > profile.STILL_STEP or step[1] > profile.STILL_TURN:
                # The camera moves with the arm: two frames are comparable
                # (what differs is the object) only while it holds still.
                break
        rests.append(i)
    if not rests:
        res.reason = "arm_left_before_settle"
        return res
    res.rest_row = rests[-1]
    if ev.release_video is None:
        res.reason = "no_release_camera"
        return res
    try:
        got = frames(ev.release_video, [holds[0], *rests])
        hold = got[holds[0]]
        measured = {i: vision.measure(hold, got[i]) for i in rests}
        # A blurred rest frame is the object still moving: only sharp ones count.
        sharp = [
            i
            for i in rests
            if measured[i]["sharp"] is None
            or measured[i]["sharp"] >= profile.BLUR_RATIO
        ]
        if not sharp:
            res.metrics = measured[rests[-1]]
            res.klass, res.reason = "unknown", "object_still_moving"
            return res
        final = sharp[-1]
        res.rest_row, res.metrics = final, {**measured[final], "final_row": final}
        # At rest means: the last two rest frames agree. One sharp frame proves
        # nothing (at 10 Hz a falling object is often not blurred) and the arm
        # may leave while the object is still on its way.
        steady = {final: 1.0}
        k = final - 1
        while k in sharp:
            if not vision.same_scene(vision.measure(got[k], got[final])):
                break
            steady[k] = 1.0
            k -= 1
        if len(steady) < opts.min_settled_rows:
            res.klass, res.reason = "unknown", "not_settled"
            return res
    except (KeyError, ValueError, OSError, cv2.error) as exc:
        res.reason = f"release_camera_unreadable: {str(exc)[:80]}"
        return res
    res.klass, res.reason = vision.classify(measured[final])
    if res.klass == "in_reach":
        # The seam goes at the earliest frame of the settled stretch from which
        # every frame to the end already shows the object in reach: the arm has
        # moved least there, and no frame of the fall is left in.
        seam = final
        for i in sorted(steady, reverse=True):
            if vision.classify(measured[i])[0] not in allowed | {"in_place"}:
                break
            seam = i
        res.rest_row = seam
        res.metrics = {**measured[seam], "final_row": final}
    res.hold_row = min(
        holds, key=lambda h: pose_distance(ev.state[res.rest_row], ev.state[h])[0]
    )
    pos, rot = pose_distance(ev.state[res.rest_row], ev.state[res.hold_row])
    res.seam = {"position_jump": round(pos, 5), "rotation_jump": round(rot, 5)}
    if reviewer is not None and res.klass in ("in_place", "in_reach"):
        try:
            verdict = reviewer(ev, res.hold_row, res.rest_row, res.record())
        except Exception as exc:  # noqa: BLE001  a failed second opinion is no opinion
            res.klass, res.reason = "unknown", f"review_failed: {type(exc).__name__}"
            return res
        if verdict and verdict.get("object_in_reach") is False:
            res.klass, res.reason = "escaped", "vlm_veto"
            res.metrics["vlm"] = verdict
        elif verdict:
            res.metrics["vlm"] = verdict
    return res


def analyze(
    ev: Evidence,
    contract: "contract_mod.schema.ActionContract",
    opts: ResetOptions,
    *,
    frames: Callable[[Path, list[int]], dict] = media.frames_at,
    reviewer=None,
) -> Analysis:
    out = Analysis(key=ev.key)
    if ev.already_reset:
        out.reason, out.detail = "reset_already_reset", "the source is itself a reset"
        return out
    try:
        contract_mod.check(contract, ev.state, ev.action)
    except contract_mod.ContractProblem as exc:
        out.reason, out.detail = "reset_action_contract", str(exc)
        return out
    out.semantics = contract.action_semantics
    n = len(ev.state)
    gripper = contract.gripper_index
    low, high = contract.gripper_values
    opened = contract.gripper_open_value
    closed = low if opened == high else high
    found = events_mod.find_events(
        (ev.state[:, gripper] == opened).astype(float), ev.width, opts.gripper_lead_rows
    )
    out.events = [e.record() for e in found]
    if not opts.allow_no_grasp and not any(e.kind == "grasp" for e in found):
        # Pushing, pouring, wiping: reversed, the object is pulled back or the
        # water runs uphill. Nothing in the signals can tell those apart from
        # a harmless reach, so a task with no grasp is not reversed unasked.
        out.reason = "reset_no_grasp"
        out.detail = "no grasp in the episode (allow_no_grasp reverses it anyway)"
        return out
    allowed = {"in_place"} | ({"in_reach"} if opts.max_release == "in_reach" else set())
    # A release of an empty gripper (nothing held) reverses to closing on
    # nothing in the same place: harmless, and not looked at.
    releases = [e for e in found if e.kind == "release" and e.held]
    out.releases = [
        _release(ev, e, found, gripper, opened, allowed, frames, reviewer, opts)
        for e in releases
    ]
    bad = [r for r in out.releases if r.klass not in allowed]
    out.edits = events_mod.lead_edits(found, closed, opened)

    def cuts(upto: int | None) -> set[int]:
        gone: set[int] = set()
        for r in out.releases:
            if r.klass == "in_reach" and (upto is None or r.rest_row < upto):
                gone.update(range(r.hold_row + 1, r.rest_row))
        return gone

    if not bad:
        gone = cuts(None)
        out.keep = [i for i in range(n) if i not in gone]
        out.eligible = True
        out.generation = "reversed_source_seam" if gone else "reversed_source"
        out.rows_cut = n - len(out.keep)
        return out
    worst = next((r for r in bad if r.klass == "escaped"), bad[0])
    out.reason = f"reset_release_{worst.klass}"
    out.detail = f"release at row {worst.row}: {worst.klass}" + (
        f" ({worst.reason})" if worst.reason else ""
    )
    # A recording can replace the end of the episode only if it is that one
    # release that is wrong and nothing is picked up after it: otherwise the
    # recording would have to undo those later operations too.
    later_grasp = any(e.kind == "grasp" and e.row > bad[0].row for e in found)
    out.bridgeable = len(bad) == 1 and bad[0].hold_row is not None and not later_grasp
    if out.bridgeable:
        out.bridge_anchor = bad[0].hold_row
        gone = cuts(out.bridge_anchor)
        out.anchor_keep = [i for i in range(out.bridge_anchor + 1) if i not in gone]
    if opts.on_ineligible == "partial" and bad[0].hold_row is not None:
        start = bad[0].hold_row
        gone = cuts(start)
        out.keep = [i for i in range(start + 1) if i not in gone]
        if len(out.keep) >= 2:
            out.eligible = True
            out.scope = "partial"
            out.generation = "partial"
            out.rows_cut = n - len(out.keep)
            out.detail += "; reversed from the last safe hold on"
    return out
