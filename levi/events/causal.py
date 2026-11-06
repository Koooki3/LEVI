"""Typed look/no-look decisions, step by step, over the causal signal stream.

The first, model-free level of the guided harness's quick check
(``levi-hub/programs/levi2/GUIDED-HARNESS-DESIGN-20261020.md`` §3.2.1): at
each policy step, is this a moment worth pausing for a local VLM to look?

Three layers, one detector each:

- ``candidates`` reads a whole recorded episode (change points and the edges
  of still spans use later samples). It is the offline upper bound.
- ``stream.CausalSignals`` reads one observed prefix: gripper crossings,
  motion stop/start and (opt-in) height turns, from the past only.
- ``Trigger`` (this module) is a thin decision layer on top of it. It does
  not detect gripper, motion or height events itself; it maps the stream's
  candidates to a reason and adds only what needs the decision context the
  stream does not have: how long nothing has happened, progress towards a
  goal, the policy's consecutive action chunks, the current subtask.

Each ``feed`` returns one ``Decision``: ``look``, a ``reason`` from
``REASONS`` (``grasp``, ``release``, ``stall``, ``anomaly``, ``maybe_done``;
None when there is nothing to look at) and ``score``.

- ``score`` is an evidence priority on [0, 1] (``SCORES``, set by hand like
  ``candidates.PRIORITY``), **not a probability**; ``probability`` stays None
  until a calibrated estimate exists (design §3.2.3).
- Every decision records its delay: ``time_s`` is when it was decided (the
  step it was fed), ``event_time_s`` the rule's own estimate of when the
  thing happened, ``delay_s`` the difference. A gripper crossing is decided
  at the sample that leaves the hysteresis band (delay 0); a stop only
  once the arm has been still for ``still_ns`` (so about that late); a height
  turn once the height has moved ``height_turn_m`` back from the extreme; a
  stall ``stall_seconds`` after the last activity. These delays are part of
  the rule, not noise, and the evaluation reports them
  (``causal_eval``).

Mapping (``cue`` names the stream kind or the decision rule):

- ``gripper_close`` -> ``grasp``, ``gripper_open`` -> ``release`` (a commanded
  gripper channel and a measured one both count; ``refractory_seconds``
  folds the second into the first);
- ``height_low`` -> ``grasp`` when the gripper was last seen closed (the lift
  after a grasp), else ``release`` (the lift after a placement);
- ``motion_stop`` -> ``stall`` (a short pause, low score);
- ``no_progress`` -> ``stall``: still, with no gripper change, for
  ``stall_seconds``; or, when the caller gives a distance to the goal, no
  improvement by ``goal_progress_m`` for ``stall_seconds``;
- ``maybe_done``: released, then still for ``done_seconds``;
- ``anomaly``: the stream reports a gap, a missing or out-of-range signal
  (once per run of such steps), or two consecutive action chunks disagree by
  more than ``chunk_disagreement`` on the steps they share.

``height_high`` and ``motion_start`` are not looks: no reason fits them, and
a resumed motion means the policy is acting.

A decision can only ask to pause and look, never cause an action. Pure: no
I/O, no thread, no model, no video. CPU budget: on the robot host the
decision runs once per policy step next to the 1 kHz control loop, so it
must stay below ``CPU_BUDGET_PERCENT`` (1 %) of one core at the policy step
rate, measured by ``causal_eval --timing`` at the recorded pace; above it,
it is not enabled on the robot.
"""

import math
from dataclasses import dataclass, field
from numbers import Real
from typing import Literal

import numpy as np

from .stream import CausalSignals

REASONS = ("grasp", "release", "stall", "anomaly", "maybe_done")
Reason = Literal["grasp", "release", "stall", "anomaly", "maybe_done"]
# Evidence priority of each cue (see the module docstring); not probabilities.
SCORES = {
    "gripper": 0.9,
    "anomaly": 0.8,
    "maybe_done": 0.7,
    "no_progress": 0.6,
    "height_low": 0.5,
    "motion_stop": 0.3,
}
# The decision layer's share of one core at the policy step rate.
CPU_BUDGET_PERCENT = 1.0
# Action chunks are bounded so one call stays cheap.
MAX_CHUNK = (256, 64)


@dataclass(frozen=True)
class TriggerConfig:
    stall_seconds: float = 3.0
    done_seconds: float = 2.0
    goal_progress_m: float = 0.01
    # None: action chunks are not compared.
    chunk_disagreement: float | None = None
    refractory_seconds: float = 0.5
    min_score: float = 0.0

    def __post_init__(self):
        for name in ("stall_seconds", "done_seconds", "goal_progress_m"):
            value = getattr(self, name)
            if not _number(value) or not 0 < value <= 600:
                raise ValueError(f"{name}: positive finite number required")
        if not _number(self.refractory_seconds) or not (
            0 <= self.refractory_seconds <= 60
        ):
            raise ValueError("refractory_seconds: 0 to 60 seconds")
        if not _number(self.min_score) or not 0 <= self.min_score <= 1:
            raise ValueError("min_score: a number on [0, 1]")
        if self.chunk_disagreement is not None and (
            not _number(self.chunk_disagreement) or self.chunk_disagreement <= 0
        ):
            raise ValueError("chunk_disagreement: positive finite number or None")


def _number(value):
    return (isinstance(value, Real) and not isinstance(value, bool)) and math.isfinite(
        value
    )


@dataclass(frozen=True)
class Cue:
    reason: Reason
    cue: str
    score: float
    event_time_s: float


@dataclass(frozen=True)
class Decision:
    look: bool
    reason: Reason | None
    score: float
    step: int
    time_s: float
    event_time_s: float | None = None
    delay_s: float | None = None
    cue: str | None = None
    # Calibrated probability; None until one exists.
    probability: float | None = None
    subtask: str | None = None
    # Every cue of this step, looked at or not (suppressed ones too).
    cues: tuple = field(default_factory=tuple)

    def as_dict(self):
        return {
            "look": self.look,
            "reason": self.reason,
            "score": self.score,
            "probability": self.probability,
            "step": self.step,
            "time_s": self.time_s,
            "event_time_s": self.event_time_s,
            "delay_s": self.delay_s,
            "cue": self.cue,
            "subtask": self.subtask,
        }


class Trigger:
    """One episode's decisions. ``signals`` is the episode's
    ``CausalSignals`` (this layer feeds it; nothing else should). State is
    built only from what was fed, so the decision at step k cannot depend
    on any later step."""

    def __init__(self, signals, config=None):
        if not isinstance(signals, CausalSignals):
            raise TypeError("signals must be a CausalSignals")
        self.signals = signals
        self.config = config or TriggerConfig()
        self._origin_ns = None
        self._closed = None  # gripper last seen closed (None: not yet seen)
        self._moving = None
        self._still_since = None  # time a confirmed still span started
        self._activity = None  # last gripper change or motion start
        self._released = None  # time of the last release
        self._stalled = False
        self._done = False
        self._unknown = False
        self._goal = None  # (best distance, time it was reached)
        self._goal_stalled = False
        self._chunk = None  # (step, array)
        self._subtask = None
        self._last_look = {}

    def feed(
        self, sample, *, now_ns, action_chunk=None, subtask=None, goal_distance_m=None
    ):
        """Feed one step (a ``stream.Sample``) and decide. ``action_chunk``:
        the policy's planned actions from this step on (rows = steps);
        ``subtask``: the current subtask's name; ``goal_distance_m``: the
        end effector's distance to the subtask's target, when known. A
        sample the stream rejects raises its ``ValueError`` and changes
        nothing here."""
        chunk = _chunk(action_chunk)
        if goal_distance_m is not None and (
            not _number(goal_distance_m) or goal_distance_m < 0
        ):
            raise ValueError("goal_distance_m: nonnegative finite metres or None")
        if subtask is not None and (not isinstance(subtask, str) or len(subtask) > 200):
            raise ValueError("subtask: a name of at most 200 characters or None")
        update = self.signals.feed(sample, now_ns=now_ns)
        if self._origin_ns is None:
            self._origin_ns = sample.observed_ns
        now = (sample.observed_ns - self._origin_ns) / 1e9
        c = self.config
        cues = []
        if subtask != self._subtask:
            # A new subtask is a new context: its stall and done clocks restart.
            self._subtask = subtask
            self._activity = now
            self._stalled = self._done = False
            self._goal, self._goal_stalled = None, False

        for cand in update.candidates:
            kind = cand.event_type
            start = cand.time_window_s[0]
            if kind in ("gripper_close", "gripper_open"):
                closed = kind == "gripper_close"
                cues.append(
                    Cue("grasp" if closed else "release", kind, SCORES["gripper"], now)
                )
                self._closed = closed
                self._activity = now
                self._stalled = self._done = False
                if not closed:
                    self._released = now
            elif kind == "height_low":
                reason = "grasp" if self._closed else "release"
                cues.append(Cue(reason, kind, SCORES["height_low"], start))
            elif kind == "motion_stop":
                self._moving = False
                self._still_since = start
                cues.append(Cue("stall", kind, SCORES["motion_stop"], start))
            elif kind == "motion_start":
                self._moving = True
                self._still_since = None
                self._activity = now
                self._stalled = self._done = False

        if update.unknown and not self._unknown:
            cues.append(Cue("anomaly", update.unknown[0], SCORES["anomaly"], now))
        self._unknown = bool(update.unknown)
        if update.unknown and "observation_gap" in update.unknown:
            # The stream restarted its readers; so do the clocks here.
            self._moving = self._still_since = None
            self._activity = now

        if self._activity is None:
            self._activity = now
        if self._moving is False and self._still_since is not None:
            quiet = max(self._still_since, self._activity)
            if not self._stalled and now - quiet >= c.stall_seconds:
                self._stalled = True
                cues.append(Cue("stall", "no_progress", SCORES["no_progress"], quiet))
            released = self._released
            if (
                not self._done
                and released is not None
                and not self._closed
                and now - max(released, self._still_since) >= c.done_seconds
            ):
                self._done = True
                cues.append(
                    Cue(
                        "maybe_done",
                        "released_and_still",
                        SCORES["maybe_done"],
                        max(released, self._still_since),
                    )
                )

        if goal_distance_m is not None:
            best = self._goal
            if best is None or goal_distance_m <= best[0] - c.goal_progress_m:
                self._goal, self._goal_stalled = (goal_distance_m, now), False
            elif not self._goal_stalled and now - best[1] >= c.stall_seconds:
                self._goal_stalled = True
                cues.append(
                    Cue("stall", "no_goal_progress", SCORES["no_progress"], best[1])
                )

        if chunk is not None:
            if c.chunk_disagreement is not None and self._chunk is not None:
                old_step, old = self._chunk
                shift = sample.step - old_step
                overlap = min(len(old) - shift, len(chunk))
                if 0 < overlap and old.shape[1] == chunk.shape[1]:
                    gap = float(
                        np.max(np.abs(old[shift : shift + overlap] - chunk[:overlap]))
                    )
                    if gap > c.chunk_disagreement:
                        cues.append(
                            Cue("anomaly", "chunk_disagreement", SCORES["anomaly"], now)
                        )
            self._chunk = (sample.step, chunk)

        return self._decide(sample.step, now, cues)

    def _decide(self, step, now, cues):
        c = self.config
        best = None
        for cue in sorted(cues, key=lambda x: (-x.score, x.event_time_s, x.cue)):
            last = self._last_look.get(cue.reason)
            if last is not None and now - last < c.refractory_seconds:
                continue
            if cue.score >= c.min_score:
                best = cue
                break
        if best is None:
            return Decision(
                False, None, 0.0, step, now, subtask=self._subtask, cues=tuple(cues)
            )
        self._last_look[best.reason] = now
        return Decision(
            True,
            best.reason,
            best.score,
            step,
            now,
            event_time_s=best.event_time_s,
            delay_s=round(now - best.event_time_s, 9),
            cue=best.cue,
            subtask=self._subtask,
            cues=tuple(cues),
        )


def _chunk(value):
    if value is None:
        return None
    # Always the trigger's own copy: the previous chunk is kept between steps,
    # and a policy may rewrite one buffer in place for every plan.
    array = np.array(value, dtype=float)
    if array.ndim == 1:
        array = array[:, None]
    if (
        array.ndim != 2
        or not 1 <= array.shape[0] <= MAX_CHUNK[0]
        or not 1 <= array.shape[1] <= MAX_CHUNK[1]
        or not np.isfinite(array).all()
    ):
        raise ValueError("action_chunk: a finite bounded (steps x dims) array")
    return array
