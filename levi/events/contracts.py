"""What the event readers produce: signal facts and event candidates.

Two records, both rebuildable from the episode's table and never part of an
annotation bundle:

- ``SignalObservation``: one fact a recorded signal states -- a gripper
  crossing, a height turn, a still span, a change point -- with the channel it
  was read from (feature, dimension, actor, units) and both its time on the
  recorded timestamps and its source row. Bound to the table it came from by
  ``source_sha256`` when the caller knows it.
- ``EventCandidate``: a place on the timeline where something may have
  changed, with the window it lies in. ``salience`` orders candidates for
  evidence gathering (higher = look first); it is **not** a probability and no
  reader may present it as one. ``boundary_probability`` stays None until a
  calibrated estimate exists.

Both keep ``source_frame_index`` (the table row) next to the time in seconds,
so nothing downstream has to convert between them with ``frame / fps`` -- a
conversion that is wrong on a table with dropped frames.

The JSON field ``schema_version`` carries the record's schema name, as other
LEVI contracts do (``levi.agent.schema.ChangeSet``).
"""

import math
from typing import Literal

from pydantic import Field, model_validator

from ..agent.schema import Contract

OBSERVATION_SCHEMA = "levi.signal_observation.v1"
CANDIDATE_SCHEMA = "levi.event_candidate.v1"

# Names of actors, features, dimensions and event types: short and printable.
_NAME = r"^[A-Za-z0-9_.:/\- ]{1,200}$"
_ACTOR = r"^[A-Za-z0-9_.-]{1,40}$"


class SignalSource(Contract):
    """The channel a fact was read from."""

    feature: str = Field(pattern=_NAME)
    # The dimension's declared name, and its position in the feature's vector
    # (both when known; a dataset may leave dimensions unnamed).
    dimension: str | None = Field(default=None, pattern=_NAME)
    index: int | None = Field(default=None, ge=0)
    # "measured": a recorded state; "commanded": what the policy or operator
    # asked for (an action); "derived": computed from other channels (a speed).
    kind: Literal["measured", "commanded", "derived"] = "measured"
    units: str | None = Field(default=None, max_length=40)
    frame: str | None = Field(default=None, max_length=80)


def _check_window(window, center):
    start, end = window
    if start > end:
        raise ValueError("time_window_s must be [start, end] with start <= end")
    if center is not None and not start <= center <= end:
        raise ValueError("The centre time must lie in time_window_s")


class SignalObservation(Contract):
    schema_version: Literal["levi.signal_observation.v1"] = OBSERVATION_SCHEMA
    episode_index: int = Field(ge=0)
    actor_id: str = Field(default="arm_0", pattern=_ACTOR)
    # What was observed: "gripper_open", "gripper_close", "height_low",
    # "height_high", "still", "change_point", ...
    kind: str = Field(pattern=_NAME)
    # A point fact has time_s inside its window; a span (still) covers it.
    time_s: float
    time_window_s: tuple[float, float]
    source_frame_index: int = Field(ge=0)
    sources: list[SignalSource] = Field(min_length=1, max_length=16)
    # A measured quantity that goes with the fact (a height, how far a
    # gripper closed on [0, 1]); None when there is none.
    value: float | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def window(self):
        _check_window(self.time_window_s, self.time_s)
        return self


class EventCandidate(Contract):
    schema_version: Literal["levi.event_candidate.v1"] = CANDIDATE_SCHEMA
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    episode_index: int = Field(ge=0)
    event_type: str = Field(pattern=_NAME)
    actor_id: str = Field(default="arm_0", pattern=_ACTOR)
    center_time_s: float
    time_window_s: tuple[float, float]
    source_frame_index: int = Field(ge=0)
    sources: list[SignalSource] = Field(min_length=1, max_length=16)
    # Evidence priority on [0, 1]: an ordering, not a probability.
    salience: float = Field(ge=0, le=1)
    boundary_probability: float | None = Field(default=None, ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list, max_length=256)
    status: Literal["proposed", "merged", "rejected"] = "proposed"

    @model_validator(mode="after")
    def window(self):
        _check_window(self.time_window_s, self.center_time_s)
        return self


def window_at(times, row):
    """``(start, end)`` in seconds that a fact first seen at ``row`` happened
    in: from the previous finite timestamp to this one (a change between two
    samples happened after the earlier one). The row's own time at the start."""
    end = float(times[row])
    for j in range(row - 1, -1, -1):
        value = float(times[j])
        if math.isfinite(value) and value <= end:
            return value, end
    return end, end
