"""What each recorded channel of a dataset means: a signal profile.

A profile lists the channels the event readers use -- grippers, positions,
rotations -- and for each one the vector column and dimension it lives in,
the actor (which arm) it belongs to, whether it is measured or commanded,
its units and frame and, for a gripper, which end of its range is open.

Profiles come from two places:

- ``infer(info)``: read from ``meta/info.json`` with the rules the signal
  lines have always used (``agent.signals``): a gripper is a float-vector
  dimension whose name matches ``GRIPPER``; a position is one named
  ``x``/``y``/``z`` (``AXIS``), the first of each axis in a column; ``action``
  is commanded, ``observation.*`` measured. Nothing about which end of a
  gripper is open can be inferred, so ``open_level`` stays None and readers
  fall back to the level the episode starts at -- the rule that reads an
  episode that starts with a closed gripper upside down.
- a declaration (a ``SignalProfile`` as JSON, for example a dataset's
  ``meta/levi_signal_profile.json``, or ``from_action_contract`` for an
  embodiment registered in ``levi.counterfactual``). ``resolve`` lays it over
  the inferred profile channel by channel: a declared channel replaces the
  inferred one at the same (feature, index), adds one the names did not
  reveal, or (``role="ignore"``) removes one they wrongly suggested.

A declared ``open_level`` is what reads an episode that starts closed the
right way up: ``open_levels(profile)`` gives it in the form
``agent.signals.summarize(open_levels=...)`` takes.
"""

import json
import re
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import Field, model_validator

from ..agent.schema import Contract

SCHEMA = "levi.signal_profile.v1"
# The optional declaration a dataset may carry.
DECLARATION = Path("meta") / "levi_signal_profile.json"

# Rotation dimensions: rx/ry/rz or roll/pitch/yaw (with an optional prefix).
ROTATION = re.compile(
    r"^(?:.*[_.\s-])?(?:rot(?:ation)?[_.\s-]?)?(r[xyz]|roll|pitch|yaw)$",
    re.IGNORECASE,
)
# An actor named in a dimension: left/right, or a numbered arm or robot.
ACTOR = re.compile(
    r"(?:^|[_.\s-])(left|right|(?:arm|robot)[_-]?\d+)(?=[_.\s-]|$)", re.IGNORECASE
)
DEFAULT_ACTOR = "arm_0"
_ROTATION_AXIS = {
    "rx": "x",
    "ry": "y",
    "rz": "z",
    "roll": "x",
    "pitch": "y",
    "yaw": "z",
}


class ChannelSpec(Contract):
    role: Literal["gripper", "position", "rotation", "ignore"]
    feature: str = Field(min_length=1, max_length=200)
    # Position in the feature's vector; the declared name, when it has one.
    index: int = Field(ge=0)
    dimension: str | None = Field(default=None, max_length=200)
    actor_id: str = Field(default=DEFAULT_ACTOR, pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    # None: "commanded" for ``action``, "measured" otherwise.
    kind: Literal["measured", "commanded"] | None = None
    units: str | None = Field(default=None, max_length=40)
    frame: str | None = Field(default=None, max_length=80)
    # Positions and rotations: which axis.
    axis: Literal["x", "y", "z"] | None = None
    # Grippers: which end of the range is open; None = not known.
    open_level: Literal["high", "low"] | None = None
    # The channel's valid range, when declared (used as the gripper's range).
    valid_range: tuple[float, float] | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.kind is None:
            self.kind = "commanded" if self.feature == "action" else "measured"
        if self.open_level is not None and self.role != "gripper":
            raise ValueError("open_level is only for a gripper channel")
        if self.role in {"position", "rotation"} and self.axis is None:
            raise ValueError(f"A {self.role} channel needs an axis")
        if self.valid_range is not None:
            lo, hi = self.valid_range
            if not (np.isfinite([lo, hi]).all() and hi > lo):
                raise ValueError("valid_range must be finite with max > min")
        return self

    @property
    def key(self):
        return (self.feature, self.index)


class SignalProfile(Contract):
    schema_version: Literal["levi.signal_profile.v1"] = SCHEMA
    origin: Literal["inferred", "declared", "resolved"] = "declared"
    channels: list[ChannelSpec] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def unique(self):
        keys = [c.key for c in self.channels]
        if len(keys) != len(set(keys)):
            raise ValueError("A (feature, index) appears twice in the profile")
        return self


def actor_of(name):
    """The actor a dimension name points at (``left``, ``right``, ``arm_1``),
    lower-cased with ``-`` as ``_``; the default actor when it names none."""
    m = ACTOR.search(name or "")
    return m.group(1).lower().replace("-", "_") if m else DEFAULT_ACTOR


def infer(info):
    """The profile ``meta/info.json`` implies, with the signal lines' rules.
    Channels are listed in the order those lines read them: recorded state
    columns before the command, by name."""
    from ..agent.signals import AXIS, GRIPPER, vector_columns

    columns = vector_columns(info)
    channels = []
    for key in sorted(columns, key=lambda k: (k == "action", k)):
        names = columns[key]
        if not names:
            continue
        seen = set()
        for d, name in enumerate(names):
            if not name:
                continue
            actor = actor_of(name)
            if GRIPPER.search(name):
                channels.append(
                    ChannelSpec(
                        role="gripper",
                        feature=key,
                        index=d,
                        dimension=name,
                        actor_id=actor,
                    )
                )
                continue
            m = AXIS.match(name)
            if m:
                axis = m.group(1).lower()
                # The first dimension per axis (and actor) in a column, as the
                # signal lines take it.
                if ("position", actor, axis) not in seen:
                    seen.add(("position", actor, axis))
                    channels.append(
                        ChannelSpec(
                            role="position",
                            feature=key,
                            index=d,
                            dimension=name,
                            actor_id=actor,
                            axis=axis,
                        )
                    )
                continue
            m = ROTATION.match(name)
            if m:
                axis = _ROTATION_AXIS[m.group(1).lower()]
                if ("rotation", actor, axis) not in seen:
                    seen.add(("rotation", actor, axis))
                    channels.append(
                        ChannelSpec(
                            role="rotation",
                            feature=key,
                            index=d,
                            dimension=name,
                            actor_id=actor,
                            axis=axis,
                        )
                    )
    return SignalProfile(origin="inferred", channels=channels)


def resolve(info, declared=None):
    """The inferred profile with ``declared`` (a ``SignalProfile`` or its JSON
    form) laid over it: a declared channel replaces the inferred one at the
    same (feature, index) or adds one; ``role="ignore"`` removes it. A
    declared channel must name a float vector column the dataset has and an
    index inside it."""
    from ..agent.signals import vector_columns

    inferred = infer(info)
    if declared is None:
        return inferred
    declared = SignalProfile.model_validate(
        declared.model_dump() if isinstance(declared, SignalProfile) else declared
    )
    columns = vector_columns(info)
    shapes = {
        key: int(((info.get("features") or {}).get(key) or {}).get("shape", [0])[0])
        for key in columns
    }
    by_key = {c.key: c for c in inferred.channels}
    order = [c.key for c in inferred.channels]
    for channel in declared.channels:
        if channel.feature not in columns:
            raise ValueError(
                f"Declared channel {channel.feature!r} is not a float vector "
                "column of this dataset"
            )
        if channel.index >= shapes[channel.feature]:
            raise ValueError(
                f"Declared index {channel.index} is outside {channel.feature!r}"
            )
        names = columns[channel.feature]
        if names and channel.dimension and names[channel.index] != channel.dimension:
            raise ValueError(
                f"Declared dimension {channel.dimension!r} is not dimension "
                f"{channel.index} of {channel.feature!r} ({names[channel.index]!r})"
            )
        if channel.dimension is None and names:
            channel = channel.model_copy(update={"dimension": names[channel.index]})
        if channel.key not in by_key:
            order.append(channel.key)
        by_key[channel.key] = channel
    return SignalProfile(
        origin="resolved",
        channels=[by_key[k] for k in order if by_key[k].role != "ignore"],
    )


def from_action_contract(contract, feature="action"):
    """A declaration from a registered action contract
    (``levi.counterfactual.schema``): its dimension order, units, frame and
    which gripper value is open. ``feature`` is the vector column laid out
    that way (the action, and on FR3 conversions the state as well)."""
    values = tuple(float(v) for v in contract.gripper_values)
    open_value = float(contract.gripper_open_value)
    if open_value == max(values):
        open_level = "high"
    elif open_value == min(values):
        open_level = "low"
    else:
        raise ValueError("The contract's open gripper value is not an end of its range")
    channels = []
    for d, name in enumerate(contract.dimension_names):
        if d == contract.gripper_index:
            channels.append(
                ChannelSpec(
                    role="gripper",
                    feature=feature,
                    index=d,
                    dimension=name,
                    open_level=open_level,
                    valid_range=(min(values), max(values)),
                )
            )
        elif name in {"x", "y", "z"}:
            channels.append(
                ChannelSpec(
                    role="position",
                    feature=feature,
                    index=d,
                    dimension=name,
                    axis=name,
                    units=contract.position_unit,
                    frame=contract.frame,
                )
            )
        elif name.lower() in _ROTATION_AXIS:
            channels.append(
                ChannelSpec(
                    role="rotation",
                    feature=feature,
                    index=d,
                    dimension=name,
                    axis=_ROTATION_AXIS[name.lower()],
                    units=contract.rotation_unit,
                    frame=contract.frame,
                )
            )
    return SignalProfile(origin="declared", channels=channels)


def read_declared(root):
    """A dataset's own declaration (``meta/levi_signal_profile.json``), or
    None when it has none. Never written by LEVI: a person or a converter
    puts it there."""
    path = Path(root) / DECLARATION
    if not path.is_file():
        return None
    return SignalProfile.model_validate(json.loads(path.read_text()))


def for_dataset(root, info=None):
    """The resolved profile of a LeRobot dataset folder."""
    root = Path(root)
    if info is None:
        info = json.loads((root / "meta" / "info.json").read_text())
    return resolve(info, read_declared(root))


def open_levels(profile):
    """``{"<feature>.<dimension>": "high" | "low"}`` for every gripper channel
    whose open end is known, as ``agent.signals.summarize`` takes it."""
    return {
        f"{c.feature}.{c.dimension}": c.open_level
        for c in profile.channels
        if c.role == "gripper" and c.open_level and c.dimension
    }


def grippers(profile):
    return [c for c in profile.channels if c.role == "gripper"]


def positions(profile, role="position"):
    """``{actor: (feature, [ix, iy, iz])}``: per actor, the first column (in
    profile order) with all three axes."""
    found = {}
    for actor in dict.fromkeys(c.actor_id for c in profile.channels):
        axes_by_feature = {}
        for c in profile.channels:
            if c.role == role and c.actor_id == actor:
                axes_by_feature.setdefault(c.feature, {}).setdefault(c.axis, c.index)
        for feature, axes in axes_by_feature.items():
            if {"x", "y", "z"} <= set(axes):
                found[actor] = (feature, [axes["x"], axes["y"], axes["z"]])
                break
    return found
