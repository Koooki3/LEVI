"""Event-anchored review: one narrow question at every recorded robot event.

A review run with ``workflow.anchored`` does not sample an episode evenly and
ask for its outcome. It finds the moments the robot's own signals mark -- a
gripper opening, say -- and at each one shows the model a few frames per
camera at fixed offsets and asks one question whose answer is a handful of
enums. A rule over the answers says whether the event counts (a *valid*
event); a rule over the valid events gives the episode's outcome. Model
timestamps play no part, so an annotation's timing error can neither hide nor
invent an event.

Everything task-specific is in a spec (``AnchoredSpec``): the anchor, the
frames per camera, the question, the answer fields, the rules. LEVI ships
specs in ``anchored_specs/`` (``plates-release``, the plates release-review
rules, reproduces the accepted external release-anchored review exactly); a
plan may name one (``{"spec": "<id>"}``, or an older id the spec lists under
``aliases``) or give its own in full. A spec's ``title`` is what people see;
its id is for plans and records. The plan freezes the resolved spec, so approval covers
the question and the rules.

Anchors are read from what the dataset declares, as ``signals.py`` does: a
float vector column (``observation.state`` first, else ``action``, or the
spec's ``column``) and a dimension whose name says gripper (or the spec's
``dimension``). A binary command channel (a raw capture's view records the
gripper command as open=1/close=0) gives the command's transitions to the
frame; a continuous one (measured aperture) gives crossings of its range
with hysteresis, which lag the command by the gripper's travel time.
Datasets without such a column cannot be reviewed this way.

Each event's answers, the frames shown (per camera and offset), its validity
and a supported/contradicted/unknown reading of every condition are kept per
run (``anchored`` records and ``episode_NNNNNN-anchored.json`` beside the
run), and the episode's outcome goes to the ordinary review queue as one
``outcome`` proposal citing the frames it rests on. Only a person commits it.
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import Field, model_validator

from ..events import gripper

# Hysteresis on the gripper channel's range, shared with signals.py.
from ..events.gripper import HIGH, LOW  # noqa: F401
from .schema import Contract

SPECS_DIR = Path(__file__).parent / "anchored_specs"
# Frames an outcome proposal may cite (Proposal.evidence_ids).
MAX_CITED = 32


class AnchorSpec(Contract):
    signal: Literal["gripper"] = "gripper"
    # "end" is the episode's last frame: one event per episode, no gripper
    # channel read (the rule ``final_state`` judges how the episode ends).
    event: Literal["open", "close", "end"] = "open"
    # Which vector column and dimension: by default the first recorded state
    # column (observation.*) with a gripper-named dimension, else action.
    column: str | None = Field(default=None, max_length=200)
    dimension: str | None = Field(default=None, max_length=200)
    # Which end of the channel's range is open (a width: high; some grippers
    # record closure: low).
    open_level: Literal["high", "low"] = "high"


class ViewSpec(Contract):
    role: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,40}$")
    camera: str = Field(min_length=1, max_length=200)
    # Offsets from the anchor frame, in frames or in seconds (converted with
    # the dataset's fps); clamped to the episode.
    offsets: list[int] | None = Field(default=None, min_length=1, max_length=32)
    offsets_seconds: list[float] | None = Field(
        default=None, min_length=1, max_length=32
    )
    # What the offsets count from: the anchor event (default), the episode's
    # first frame or its last (a view of the starting or final state).
    at: Literal["anchor", "start", "end"] = "anchor"

    @model_validator(mode="after")
    def one_kind(self):
        if (self.offsets is None) == (self.offsets_seconds is None):
            raise ValueError("A view gives offsets or offsets_seconds, not both")
        return self


class AnswerField(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    enum: list[str] = Field(min_length=2, max_length=24)


class Condition(Contract):
    field: str
    is_in: list[str] | None = Field(default=None, alias="in")
    not_in: list[str] | None = None

    model_config = Contract.model_config | {"populate_by_name": True}

    @model_validator(mode="after")
    def one_test(self):
        if (self.is_in is None) == (self.not_in is None):
            raise ValueError("A condition gives in or not_in")
        return self


class Probe(Contract):
    """One more narrow question beside the spec's own: its frames, its text,
    its answer fields."""

    views: list[ViewSpec] | None = Field(default=None, min_length=1, max_length=8)
    question: str = Field(min_length=1, max_length=8000)
    fields: list[AnswerField] = Field(min_length=1, max_length=16)
    max_output_tokens: int = Field(default=200, ge=16, le=4096)

    def answer_schema(self):
        return _answer_schema(self.fields)


class Waiver(Contract):
    # A required label the episode does not need when every condition holds
    # on the start check's answer (e.g. a colour already stacked at the start).
    label: str
    when: list[Condition] = Field(min_length=1, max_length=16)


class StartCheck(Probe):
    """Asked once per episode on frames at its start (views ``at`` start or
    end); its answer can waive required labels."""

    waive: list[Waiver] = Field(min_length=1, max_length=16)


class Veto(Contract):
    """A rule that any event can break. At each event where ``ask_when``
    (over the event's own answer) is not contradicted, ``veto_when`` is read
    over the veto's own question -- when it has one; its views default to the
    spec's -- or over the event's answer. All supported: confirmed;
    one contradicted: cleared; else undecided. A confirmed veto fails the
    episode (``effect: episode``) or only its event (``effect: event``)."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    title: dict[str, str] | None = None
    effect: Literal["episode", "event"] = "episode"
    ask_when: list[Condition] = Field(default_factory=list, max_length=16)
    views: list[ViewSpec] | None = Field(default=None, min_length=1, max_length=8)
    question: str | None = Field(default=None, min_length=1, max_length=8000)
    fields: list[AnswerField] | None = Field(default=None, min_length=1, max_length=16)
    veto_when: list[Condition] = Field(min_length=1, max_length=16)
    max_output_tokens: int = Field(default=200, ge=16, le=4096)

    @model_validator(mode="after")
    def asked_or_read(self):
        _title(self.title, f"Veto {self.id!r} title")
        if (self.question is None) != (self.fields is None):
            raise ValueError(f"Veto {self.id!r} gives a question with its fields")
        if self.views is not None and self.question is None:
            raise ValueError(f"Veto {self.id!r} has views but no question")
        if "max_output_tokens" in self.model_fields_set and self.question is None:
            raise ValueError(
                f"Veto {self.id!r} has max_output_tokens but no question to ask"
            )
        return self

    def answer_schema(self):
        return _answer_schema(self.fields)


class EpisodeRule(Contract):
    # Success when every label here has a valid event (label_field names the
    # answer that carries it); without labels, when at least min_valid events
    # are valid.
    label_field: str | None = None
    require_labels: list[str] = Field(default_factory=list, max_length=16)
    min_valid: int = Field(default=1, ge=1, le=100)
    # Without labels, what else the valid events must satisfy. ``any_valid``:
    # nothing (min_valid valid events are enough). ``last_valid_not_regrasped``:
    # the gripper does not close again after the last valid event, so the
    # object is not picked up again. The rule only looks after the last valid
    # event, so a task that grasps again between placements is not failed by
    # it; what it cannot stop is a half-done or twice-placed episode (use
    # ``min_valid`` as a task-level lower bound).
    # ``final_state``: for a spec whose only event is the episode's end
    # (``anchor.event`` ``end``): success when that one event is valid, failure
    # when it is contradicted, and a failure that is undecided when it is
    # unknown. It reads no gripper channel and no time segment.
    rule: Literal["any_valid", "last_valid_not_regrasped", "final_state"] = "any_valid"
    # Also require that the episode's last time segment of the subtask
    # ``place`` (the one that starts last) is not a failure or unknown. The
    # review cannot see time segments: the live service applies this part
    # (levi/live/judge.py) and the review's own outcome leaves it out.
    require_place: bool = False


class AnchoredSpec(Contract):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    version: int = Field(default=1, ge=1)
    # Display name per language ({"en": ..., "zh": ...}); the id is shown
    # only where there is no title.
    title: dict[str, str] | None = None
    description: str = Field(default="", max_length=2000)
    # "candidate": still being validated -- listed and plannable when named,
    # never a default. Left out of the frozen form when stable.
    status: Literal["stable", "candidate"] = "stable"
    anchor: AnchorSpec = Field(default_factory=AnchorSpec)
    views: list[ViewSpec] = Field(min_length=1, max_length=8)
    question: str = Field(min_length=1, max_length=8000)
    # Ordered: the model writes the fields in this order.
    fields: list[AnswerField] = Field(min_length=1, max_length=16)
    valid_when: list[Condition] = Field(min_length=1, max_length=16)
    unknown_values: list[str] = Field(default_factory=lambda: ["unclear"])
    episode: EpisodeRule = Field(default_factory=EpisodeRule)
    max_output_tokens: int = Field(default=200, ge=16, le=4096)
    # Optional, both absent from a spec that does not use them (and from its
    # frozen form): one start check per episode, and vetoes at every event.
    start: StartCheck | None = None
    vetoes: list[Veto] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def consistent(self):
        _title(self.title, "title")
        values = _values(self.fields, "Answer field")
        _check(self.valid_when, values, "valid_when")
        rule = self.episode
        if rule.require_labels:
            if rule.label_field not in values:
                raise ValueError("episode.label_field must name an answer field")
            if set(rule.require_labels) - values[rule.label_field]:
                raise ValueError("episode.require_labels must be values of label_field")
        if rule.rule != "any_valid" or rule.require_place:
            if rule.require_labels:
                raise ValueError(
                    "episode.rule and episode.require_place apply to a spec "
                    "without require_labels"
                )
            if rule.rule != "final_state" and self.anchor.event != "open":
                raise ValueError(
                    "episode.rule reads the closes after an opening: the anchor "
                    "event must be open"
                )
        if (rule.rule == "final_state") != (self.anchor.event == "end"):
            raise ValueError(
                "episode.rule final_state goes with anchor.event end (the "
                "episode's last frame), and only with it"
            )
        if rule.rule == "final_state" and (rule.require_place or rule.min_valid != 1):
            raise ValueError(
                "episode.rule final_state has one event per episode: it takes "
                "neither require_place nor a min_valid above 1"
            )
        if rule.require_place and rule.rule == "any_valid":
            raise ValueError(
                "episode.require_place goes with episode.rule last_valid_not_regrasped"
            )
        _roles(self.views, "View")
        if any(v.at != "anchor" for v in self.views):
            # Every event would be shown the same frames; the start check
            # (or a veto) is where the episode's first or last frame belongs.
            raise ValueError(
                "views are at the anchor; show the episode's start or end in "
                "the start check or a veto"
            )
        if self.start is not None:
            own = _values(self.start.fields, "start field")
            labels = [w.label for w in self.start.waive]
            if len(set(labels)) != len(labels):
                raise ValueError("start.waive names a label twice")
            for w in self.start.waive:
                if w.label not in rule.require_labels:
                    raise ValueError(
                        f"start.waive names {w.label!r}, not one of "
                        "episode.require_labels"
                    )
                _check(w.when, own, f"start.waive {w.label!r}")
            if not self.start.views or any(v.at == "anchor" for v in self.start.views):
                raise ValueError("start views are at the episode's start or end")
            _roles(self.start.views, "start view")
        ids = [v.id for v in self.vetoes]
        if len(set(ids)) != len(ids):
            raise ValueError("Veto ids must be unique")
        for veto in self.vetoes:
            where = f"veto {veto.id!r}"
            _check(veto.ask_when, values, f"{where} ask_when")
            own = (
                values
                if veto.fields is None
                else _values(veto.fields, f"{where} field")
            )
            _check(veto.veto_when, own, f"{where} veto_when")
            if veto.views:
                _roles(veto.views, f"{where} view")
        return self

    def answer_schema(self):
        """The JSON schema the server decodes against: every field required,
        in the spec's order, each one of its values."""
        return _answer_schema(self.fields)


def _title(title, what):
    if title is not None and (
        not title
        or any(
            not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2,4})?", k)
            or not v.strip()
            or len(v) > 200
            for k, v in title.items()
        )
    ):
        raise ValueError(
            f"{what} maps language codes to non-empty names of up to 200 characters"
        )


def _answer_schema(fields):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [f.name for f in fields],
        "properties": {f.name: {"enum": list(f.enum)} for f in fields},
    }


def _values(fields, what):
    names = [f.name for f in fields]
    if len(set(names)) != len(names):
        raise ValueError(f"{what} names must be unique")
    return {f.name: set(f.enum) for f in fields}


def _check(conditions, values, where):
    for c in conditions:
        if c.field not in values:
            raise ValueError(f"{where} names an unknown field {c.field!r}")
        unknown = set(c.is_in or c.not_in or []) - values[c.field]
        if unknown:
            raise ValueError(
                f"{where} on {c.field!r} uses values it cannot take: "
                + ", ".join(sorted(unknown))
            )


def _roles(views, what):
    if len({v.role for v in views}) != len(views):
        raise ValueError(f"{what} roles must be unique")


# Keys a spec that does not use them leaves out of its frozen form, so a plan
# for an older spec freezes exactly what it froze before they existed.
_OPTIONAL = {"status": "stable", "start": None, "vetoes": []}
_OPTIONAL_EPISODE = {"rule": "any_valid", "require_place": False}


def dump(spec):
    """A spec as the plain dict a plan freezes and ``anchored.specs`` lists."""
    out = spec.model_dump(by_alias=True)
    for key, empty in _OPTIONAL.items():
        if out.get(key) == empty:
            out.pop(key)

    def views(listed):
        for v in listed or []:
            if v.get("at") == "anchor":
                v.pop("at")

    for key, empty in _OPTIONAL_EPISODE.items():
        if out["episode"].get(key) == empty:
            out["episode"].pop(key)

    views(out["views"])
    if "start" in out:
        views(out["start"]["views"])
    for veto in out.get("vetoes", []):
        views(veto.get("views"))
        if veto.get("question") is None:
            # Nothing is asked, so there is no answer to bound.
            veto.pop("max_output_tokens")
    return out


def cameras(spec):
    """Every camera a spec (a dict or AnchoredSpec) shows, in first-use order."""
    spec = spec if isinstance(spec, dict) else dump(spec)
    listed = list(spec["views"])
    for veto in spec.get("vetoes") or []:
        listed += veto.get("views") or []
    if spec.get("start"):
        listed += spec["start"]["views"]
    return list(dict.fromkeys(v["camera"] for v in listed))


def _shipped():
    """(specs by id, {alias: id}) from ``anchored_specs/``. A spec file may
    list ``aliases``: former ids that plans, stored runs and scripts still
    name (``plates-release-ar2`` for ``plates-release``)."""
    specs, aliases = {}, {}
    for path in sorted(SPECS_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        former = raw.pop("aliases", [])
        spec = AnchoredSpec.model_validate(raw)
        specs[spec.id] = spec
        for name in former:
            aliases[name] = spec.id
    clash = set(aliases) & set(specs)
    if clash:
        raise ValueError(f"Anchored spec aliases shadow ids: {sorted(clash)}")
    return specs, aliases


def builtin():
    """The specs LEVI ships, by id."""
    return _shipped()[0]


def aliases():
    """Former built-in ids that still resolve: ``{alias: id}``."""
    return _shipped()[1]


def lookup(spec_id):
    """The built-in spec with this id or former id, else None."""
    specs, former = _shipped()
    return specs.get(former.get(spec_id, spec_id))


def title_of(spec, lang="en"):
    """A spec's display name in ``lang`` (else English, else any), from the
    spec itself or -- for a spec frozen before titles, by its id or former
    id -- from the built-in one; None when there is none."""
    spec = spec if isinstance(spec, dict) else spec.model_dump(by_alias=True)
    title = spec.get("title")
    if not title:
        shipped = lookup(spec.get("id"))
        title = shipped.title if shipped else None
    if not title:
        return None
    return title.get(lang) or title.get("en") or next(iter(title.values()))


def titles(spec):
    """Every language's display name of a spec (see ``title_of``)."""
    spec = spec if isinstance(spec, dict) else spec.model_dump(by_alias=True)
    if spec.get("title"):
        return dict(spec["title"])
    shipped = lookup(spec.get("id"))
    return dict(shipped.title) if shipped and shipped.title else None


def resolve(value):
    """A plan's ``workflow.anchored``: ``{"spec": "<built-in id or former
    id>"}`` or a whole spec. Returns the full spec as a plain dict (frozen in
    the plan)."""
    if not isinstance(value, dict):
        raise ValueError("workflow.anchored must be an object")  # noqa: TRY004 - pydantic reports ValueError
    if set(value) == {"spec"}:
        spec = lookup(value["spec"])
        if spec is None:
            raise ValueError(
                f"Unknown anchored review spec {value['spec']!r}; built in: "
                + ", ".join(sorted(builtin()))
            )
        return dump(spec)
    return dump(AnchoredSpec.model_validate(value))


def spec_of(workflow):
    value = (workflow or {}).get("anchored")
    return AnchoredSpec.model_validate(value) if value else None


# --- anchors ---------------------------------------------------------------


def _gripper_channel(table, info, stats, anchor):
    from .signals import GRIPPER, _matrix, vector_columns

    columns = vector_columns(info)
    if anchor.column:
        if anchor.column not in columns:
            raise ValueError(
                f"Anchor column {anchor.column!r} is not a float vector column "
                "of this dataset"
            )
        order = [anchor.column]
    else:
        # Recorded state first, then the command.
        order = sorted(columns, key=lambda k: (k == "action", k))
    for key in order:
        names = columns[key]
        if not names:
            continue
        for d, name in enumerate(names):
            if anchor.dimension:
                if name != anchor.dimension:
                    continue
            elif not name or not GRIPPER.search(name):
                continue
            matrix = _matrix(table, key)
            if matrix is None or matrix.shape[1] != len(names):
                continue
            bounds = None
            s = (stats or {}).get(key) or {}
            try:
                bounds = (float(s["min"][d]), float(s["max"][d]))
            except (KeyError, IndexError, TypeError, ValueError):
                bounds = None
            return key, name, matrix[:, d], bounds
    raise ValueError(
        "No gripper channel to anchor on: the dataset declares no float vector "
        "column with a gripper-named dimension (name one with anchor.column / "
        "anchor.dimension)"
    )


def crossings(values, bounds, open_level="high"):
    """Row positions where the channel opens and closes, with hysteresis on
    its range (the dataset's, else the episode's). The level the episode
    starts at is its initial state. The kernel is ``events.gripper``'s, which
    the signal lines use too (on the episode's own range)."""
    channel = gripper.read(
        values, bounds, range_source="dataset", open_level=open_level
    )
    return channel.crossings if channel is not None else []


def anchor_rows(table, info, stats, anchor):
    """(row positions of the anchor events, row positions where the gripper
    closes, channel description). The closes are every closing the channel
    crosses, whatever the anchor event is."""
    if anchor.event == "end":
        # The episode's last frame, the one event; nothing is read from the
        # gripper.
        return ([len(table) - 1] if len(table) else []), [], "episode end"
    key, name, values, bounds = _gripper_channel(table, info, stats, anchor)
    found = crossings(values, bounds, anchor.open_level)
    return (
        [i for i, kind in found if kind == anchor.event],
        [i for i, kind in found if kind == "close"],
        f"{key}.{name}",
    )


def anchors(table, info, stats, anchor):
    """(row positions of the anchor events, channel description)."""
    rows, _, channel = anchor_rows(table, info, stats, anchor)
    return rows, channel


def view_offsets(view, fps):
    """A view's offsets in frames (seconds converted with the dataset's
    fps): the rows it shows on an evenly sampled table, and the ``offset``
    a record names a shown frame by."""
    if view.offsets is not None:
        return list(view.offsets)
    return [round(s * fps) for s in view.offsets_seconds]


# A table is evenly sampled when every row is within this share of a frame
# of ``timestamp[0] + row / fps``.
EVEN = 0.01


def uneven_times(times, fps):
    """The table's timestamps when they are not evenly sampled at ``fps``
    (dropped frames, a jittery clock), else None. Only then do views given
    in seconds look up rows by timestamp; LEVI's own conversions are always
    even (``timestamp = frame_index / fps``), and so read as they always
    did."""
    times = np.asarray(times, dtype=float)
    if len(times) < 2 or not fps or fps <= 0 or not np.isfinite(times).all():
        return None
    if np.any(np.diff(times) < 0):
        return None
    drift = times - times[0] - np.arange(len(times)) / fps
    return None if np.all(np.abs(drift) <= EVEN / fps) else times


# --- judging ---------------------------------------------------------------


def read(conditions, answer, unknown_values):
    """Each condition over an answer as supported / contradicted / unknown,
    and their reading together: supported when all hold, contradicted when
    one fails on a definite answer, unknown otherwise."""
    checks = []
    for c in conditions:
        value = answer.get(c.field)
        holds = value in c.is_in if c.is_in is not None else value not in c.not_in
        checks.append(
            {
                "field": c.field,
                "value": value,
                "result": "supported"
                if holds
                else "unknown"
                if value in unknown_values
                else "contradicted",
            }
        )
    results = {c["result"] for c in checks}
    verdict = (
        "supported"
        if results <= {"supported"}
        else "contradicted"
        if "contradicted" in results
        else "unknown"
    )
    return checks, verdict


def judge(spec, answer):
    """Each condition as supported / contradicted / unknown, and the event's
    reading: supported (valid) when all hold, contradicted when one fails on
    a definite answer, unknown otherwise."""
    return read(spec.valid_when, answer, spec.unknown_values)


VETO = {"supported": "confirmed", "contradicted": "cleared", "unknown": "undecided"}


def veto_verdict(spec, veto, event_answer, own_answer=None):
    """A veto's checks and verdict at one event (``confirmed`` / ``cleared``
    / ``undecided``), from its own answer or, without a question, the
    event's."""
    checks, reading = read(
        veto.veto_when,
        event_answer if veto.question is None else own_answer,
        spec.unknown_values,
    )
    return checks, VETO[reading]


def settle(verdict, vetoes):
    """An event's verdict after its ``effect: event`` vetoes: a confirmed one
    contradicts it, an undecided one leaves a supported event unknown."""
    mine = {v["verdict"] for v in vetoes if v["effect"] == "event"}
    if "confirmed" in mine:
        return "contradicted"
    if "undecided" in mine and verdict == "supported":
        return "unknown"
    return verdict


def waivers(spec, start_answer):
    """(labels waived, labels whose waiver is undecided) from the start
    check's answer; nothing without one."""
    if spec.start is None or start_answer is None:
        return [], []
    waived, unsure = [], []
    for w in spec.start.waive:
        reading = read(w.when, start_answer, spec.unknown_values)[1]
        if reading == "supported":
            waived.append(w.label)
        elif reading == "unknown":
            unsure.append(w.label)
    return waived, [x for x in unsure if x not in waived]


def outcome(spec, events, start_answer=None, closes=None):
    """The episode's outcome from its judged events (and the start check's
    answer, and the frames where the gripper closes, ``closes``), and what it
    rests on. ``closes`` is None when the record has none (a record made
    before they were kept): a rule that needs them then falls back to the
    valid events alone and says so in ``basis.missing_inputs``."""
    valid = [e for e in events if e["valid"]]
    rule = spec.episode
    if rule.require_labels:
        waived, unsure = waivers(spec, start_answer)
        labels = {e["answer"].get(rule.label_field) for e in valid}
        missing = [
            x for x in rule.require_labels if x not in labels and x not in waived
        ]
        # A missing label with an undecided event (or an undecided waiver) is
        # the reviewer's to look at.
        undecided = [
            x
            for x in missing
            if x in unsure
            or any(
                e["verdict"] == "unknown"
                and e["answer"].get(rule.label_field) in (x, *spec.unknown_values)
                for e in events
            )
        ]
        verdict, basis = (
            "failure" if missing else "success",
            {
                "valid_labels": sorted(labels, key=str),
                "missing_labels": missing,
                "undecided_labels": undecided,
            },
        )
        if spec.start is not None:
            # A waiver matters only for a label with no valid event; one
            # whose label also has events that are not valid (contradicted
            # or unknown) is contested: the episode did handle that label,
            # so the start check's answer may be wrong.
            decided = [x for x in waived if x not in labels]
            basis["waived_labels"] = decided
            basis["redundant_waivers"] = [x for x in waived if x in labels]
            basis["contested_waivers"] = [
                x
                for x in decided
                if any(
                    not e["valid"] and e["answer"].get(rule.label_field) == x
                    for e in events
                )
            ]
    elif rule.rule == "final_state":
        # One event, the episode's end. Success when it is valid; a definite
        # "not there" is a failure; an unknown reading is a failure too, but
        # an undecided one (``final_reading`` names which).
        reading = events[-1]["verdict"] if events else None
        basis = {
            "valid_events": len(valid),
            "min_valid": rule.min_valid,
            "rule": rule.rule,
            "final_reading": reading,
        }
        verdict = "success" if reading == "supported" else "failure"
    else:
        ok = len(valid) >= rule.min_valid
        basis = {"valid_events": len(valid), "min_valid": rule.min_valid}
        if rule.rule != "any_valid":
            last = max((e["frame_index"] for e in valid), default=None)
            after = (
                None
                if closes is None or last is None
                else sum(1 for c in closes if c > last)
            )
            basis |= {
                "rule": rule.rule,
                "last_valid_frame": last,
                "closes_after_last_valid": after,
                "require_place": rule.require_place,
            }
            # The place condition needs the time segments, which the review
            # cannot see: until the live service applies it (judge.merge), a
            # success rests on an input it does not have.
            missing = [
                *(["closes"] if closes is None else []),
                *(["place"] if rule.require_place else []),
            ]
            if missing:
                basis["missing_inputs"] = missing
            if closes is not None and after:
                ok = False
        verdict = "success" if ok else "failure"
    if any(v.effect == "episode" for v in spec.vetoes):

        def found(state):
            return [
                {"veto": v["id"], "frame_index": e.get("frame_index")}
                for e in events
                for v in e.get("vetoes") or []
                if v["effect"] == "episode" and v["verdict"] == state
            ]

        basis["vetoes"] = found("confirmed")
        basis["undecided_vetoes"] = found("undecided")
        if basis["vetoes"]:
            verdict = "failure"
    return verdict, basis


def undecided(verdict, basis):
    """Whether an outcome rests on something undecided: a required label (or
    its waiver), the final state the rule ``final_state`` could not read, or
    -- for a success -- a veto, a contested waiver or an input its rule needed
    and did not have (``missing_inputs``)."""
    return bool(
        basis.get("undecided_labels")
        or (
            basis.get("rule") == "final_state"
            and basis.get("final_reading") in (None, "unknown")
        )
        or (
            verdict == "success"
            and (
                basis.get("undecided_vetoes")
                or basis.get("contested_waivers")
                or basis.get("missing_inputs")
            )
        )
    )


def validate_answer(spec, raw):
    """The model's answer as a dict of the spec's fields, or ValueError."""
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError("Anchored answer is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Anchored answer is not an object")  # noqa: TRY004 - one error type for a bad answer
    out = {}
    for field in spec.fields:
        if value.get(field.name) not in field.enum:
            raise ValueError(
                f"Anchored answer field {field.name!r} is missing or not one of "
                + ", ".join(field.enum)
            )
        out[field.name] = value[field.name]
    extra = set(value) - set(out)
    if extra:
        raise ValueError("Anchored answer has fields the spec does not ask for")
    return out


# --- one episode -----------------------------------------------------------


def view_rows(view, fps, n, last, times=None):
    """Row positions a view shows: its offsets from the anchor row ``n``, the
    first row or the last, clamped to the episode. With ``times`` (the
    table's timestamps, from ``uneven_times``) an offset in seconds is the
    row whose timestamp is nearest that many seconds from the base row's
    (the earlier row on a tie), not that many frames at the declared fps."""
    base = n if view.at == "anchor" else 0 if view.at == "start" else last
    if times is None or view.offsets_seconds is None:
        return [min(last, max(0, base + d)) for d in view_offsets(view, fps)]
    times = np.asarray(times, dtype=float)[: last + 1]
    rows = []
    for s in view.offsets_seconds:
        target = times[base] + s
        i = int(np.searchsorted(times, target))
        if i > last:
            i = last
        elif i > 0 and target - times[i - 1] <= times[i] - target:
            i -= 1
        rows.append(i)
    return rows


def review_episode(wb, id, config, context, episode, started):
    """Judge every anchor event of one episode; returns (summary, evidence,
    ModelOutput with one outcome proposal, usage)."""
    from . import media, observations
    from .formats import DATASETS
    from .schema import ModelOutput, Proposal

    spec = spec_of(context.workflow)
    directory = wb.store.run_dir(id)
    root = directory / "input"
    folder = directory / "evidence"
    state = media.snapshot_state(context, root)
    table = media.episode_table(state, episode)
    info = json.loads((root / "meta/info.json").read_text())
    stats = None
    if (root / "meta/stats.json").is_file():
        try:
            stats = json.loads((root / "meta/stats.json").read_text())
        except ValueError:
            stats = None
    fps = float(info.get("fps") or 0) or 1.0
    positions, close_rows, channel = anchor_rows(table, info, stats, spec.anchor)
    last = len(table) - 1
    frames = table.frame_index.to_numpy(dtype=int)
    times = table.timestamp.to_numpy(dtype=float)
    # Seconds become rows by timestamp only on an unevenly sampled table.
    uneven = uneven_times(times, fps)
    adapter = DATASETS[context.dataset_adapter]
    asked_vetoes = [v for v in spec.vetoes if v.question is not None]

    def shows(views, n):
        return {
            v.role: [int(frames[r]) for r in view_rows(v, fps, n, last, uneven)]
            for v in views
        }

    # Frames per camera: every offset of every view the questions show, at
    # every anchor (and the start check's once), clamped to the episode.
    wanted = {n: shows(spec.views, n) for n in positions}
    per_camera = {}
    for view in spec.views:
        per_camera.setdefault(view.camera, set()).update(
            f for n in positions for f in wanted[n][view.role]
        )
    for veto in asked_vetoes:
        for view in veto.views or []:
            per_camera.setdefault(view.camera, set()).update(
                f for n in positions for f in shows([view], n)[view.role]
            )
    start_frames = shows(spec.start.views, 0) if spec.start else {}
    for view in spec.start.views if spec.start else []:
        per_camera.setdefault(view.camera, set()).update(start_frames[view.role])
    if not positions:
        # No event: the outcome still cites what the episode ends on.
        per_camera.setdefault(spec.views[0].camera, set()).add(int(frames[last]))
    evidence, summary = [], None
    for camera, found in per_camera.items():
        chosen = sorted(found)
        if not chosen:
            continue
        scoped = context.model_copy(update={"cameras": [camera]})
        summary, rows = adapter.sample_frames(scoped, root, episode, folder, chosen)
        evidence += rows
    summary["workflow"] = context.workflow
    summary["anchored"] = {"spec": spec.id, "channel": channel}
    evidence = observations.persist(wb, id, episode, summary, evidence)
    by_frame = {(row["camera_key"], row["frame_index"]): row for row in evidence}
    total = {"requests": 0, "tokens": 0, "elapsed_seconds": 0.0}

    def shown_for(views, rows):
        out = []
        for view in views:
            for d, frame in zip(view_offsets(view, fps), rows[view.role], strict=True):
                row = by_frame[(view.camera, frame)]
                out.append(
                    {
                        "role": view.role,
                        "camera": view.camera,
                        "offset": d,
                        "frame_index": frame,
                        "evidence_id": row["id"],
                        "artifact": row["artifact"],
                        "sha256": row["sha256"],
                    }
                )
        return out

    def brief(shown):
        return [
            {
                k: s[k]
                for k in ("role", "camera", "offset", "frame_index", "evidence_id")
            }
            for s in shown
        ]

    def spent(usage):
        total["requests"] += 0 if usage.get("cached") else 1
        total["tokens"] += usage.get("tokens") or 0
        total["elapsed_seconds"] += usage.get("elapsed_seconds") or 0.0
        return {
            k: usage.get(k)
            for k in ("tokens", "prompt_tokens", "elapsed_seconds", "cached")
            if usage.get(k) is not None
        }

    def put(phase, probe, shown):
        return ask(
            wb,
            id,
            config,
            context,
            episode,
            phase,
            probe,
            probe.answer_schema(),
            shown,
            started,
        )

    start = None
    if spec.start is not None:
        shown = shown_for(spec.start.views, start_frames)
        answer, usage = put("start", spec.start, shown)
        start = {
            "answer": answer,
            "waive": [
                {
                    "label": w.label,
                    "checks": read(w.when, answer, spec.unknown_values)[0],
                    "reading": read(w.when, answer, spec.unknown_values)[1],
                }
                for w in spec.start.waive
            ],
            "frames": brief(shown),
            "usage": spent(usage),
        }
    events = []
    for n in positions:
        shown = shown_for(spec.views, wanted[n])
        anchor = f"anchor-{int(frames[n]):06d}"
        answer, usage = put(anchor, spec, shown)
        checks, verdict = judge(spec, answer)
        event = {
            "frame_index": int(frames[n]),
            "timestamp": float(times[n]),
            "answer": answer,
            "checks": checks,
            "verdict": verdict,
            "valid": verdict == "supported",
            "frames": brief(shown),
            "usage": spent(usage),
        }
        if spec.vetoes:
            results = []
            for veto in spec.vetoes:
                result = {"id": veto.id, "effect": veto.effect}
                if read(veto.ask_when, answer, spec.unknown_values)[1] == (
                    "contradicted"
                ):
                    results.append(result | {"verdict": "not_asked"})
                    continue
                own = None
                if veto.question is not None:
                    views = veto.views or spec.views
                    mine = shown_for(views, shows(views, n))
                    own, usage = put(f"{anchor}-veto-{veto.id}", veto, mine)
                    result |= {
                        "answer": own,
                        "frames": brief(mine),
                        "usage": spent(usage),
                    }
                checks, reading = veto_verdict(spec, veto, answer, own)
                results.append(result | {"checks": checks, "verdict": reading})
            event["vetoes"] = results
            event["verdict"] = settle(verdict, results)
            event["valid"] = event["verdict"] == "supported"
        events.append(event)
    closes = [int(frames[i]) for i in close_rows]
    verdict, basis = outcome(spec, events, start and start["answer"], closes)
    record = {
        "schema": "levi.anchored.v1",
        "run_id": id,
        "episode_index": episode,
        "spec": {"id": spec.id, "version": spec.version, "title": titles(spec)},
        "channel": channel,
        "event": spec.anchor.event,
        "outcome": verdict,
        "basis": basis,
        "events": events,
        "usage": total,
        "at": time.time(),
    }
    if start is not None:
        record["start"] = start
    if uneven is not None and any(
        v.offsets_seconds is not None
        for v in [
            *spec.views,
            *(spec.start.views if spec.start else []),
            *(view for veto in spec.vetoes for view in veto.views or []),
        ]
    ):
        # Offsets in seconds were looked up by timestamp (an even table's
        # record leaves this out, as before).
        record["offset_timing"] = "timestamps"
    if spec.episode.rule == "last_valid_not_regrasped":
        # Frames where the gripper closes, for the rule that reads them. A
        # spec with the default rule leaves them out, so its records stay as
        # they were (a record without them reads as unknown to ``outcome``).
        record["closes"] = closes
    wb.store.put("anchored", f"{id}:{episode}", record)
    (directory / f"episode_{episode:06d}-anchored.json").write_text(
        json.dumps(record, ensure_ascii=False)
    )
    # Cite the frames the verdict rests on: an event that vetoed the episode
    # first (the frames its confirmed vetoes asked about, then its own), the
    # episode's last frame when there is no event, the start check's frames
    # when a waiver decided the outcome, then the valid events, each by its
    # frames nearest the anchor (one before, one after, per camera).
    vetoing = {v["frame_index"] for v in basis.get("vetoes") or []}
    cited = []

    def cite(ids):
        cited.extend(x for x in dict.fromkeys(ids) if x not in cited)

    ordered = sorted(
        events, key=lambda e: (e["frame_index"] not in vetoing, not e["valid"])
    )

    def near(e):
        for v in e.get("vetoes") or []:
            if v["verdict"] == "confirmed":
                cite(f["evidence_id"] for f in v.get("frames") or [])
        for view in spec.views:
            mine = [f for f in e["frames"] if f["role"] == view.role]
            before = [f for f in mine if f["offset"] < 0]
            after = [f for f in mine if f["offset"] >= 0]
            cite(
                f["evidence_id"]
                for f in ([before[-1]] if before else [])
                + ([after[0]] if after else [])
            )

    for e in ordered:
        if e["frame_index"] in vetoing:
            near(e)
    if not events:
        cite([by_frame[(spec.views[0].camera, int(frames[last]))]["id"]])
    if basis.get("waived_labels"):
        cite(f["evidence_id"] for f in start["frames"])
    for e in ordered:
        if e["frame_index"] not in vetoing:
            near(e)
    if not cited:
        cited = [evidence[-1]["id"]]
    name = spec.anchor.event

    def vetoed(e):
        hits = [
            f"veto {v['id']} {v['verdict']}"
            for v in e.get("vetoes") or []
            if v["verdict"] in ("confirmed", "undecided")
        ]
        return "; " + ", ".join(hits) if hits else ""

    parts = [
        f"{'valid' if e['valid'] else e['verdict']} {'final state' if name == 'end' else name} at {e['timestamp']:.1f} s ("
        + ", ".join(f"{k}={v}" for k, v in e["answer"].items())
        + vetoed(e)
        + ")"
        for e in events
    ]
    content = (
        f"Anchored review ({title_of(spec) or spec.id}): "
        + (
            "start ("
            + ", ".join(f"{k}={v}" for k, v in start["answer"].items())
            + "); "
            if start is not None
            else ""
        )
        + (
            f"{len(events)} gripper {name} event(s); "
            if name != "end"
            else "final-state check on the episode's last frames; "
        )
        + ("; ".join(parts) if parts else "none recorded")
        + f". Outcome {verdict}."
    )
    note = (
        "valid: " + ", ".join(basis["valid_labels"])
        if "valid_labels" in basis
        else f"{basis['valid_events']} valid event(s)"
    )
    if basis.get("closes_after_last_valid"):
        note += (
            f"; the gripper closed {basis['closes_after_last_valid']} time(s) "
            "after the last valid event"
        )
    if basis.get("waived_labels"):
        note += "; not required at the start: " + ", ".join(basis["waived_labels"])
    if basis.get("vetoes"):
        note += "; vetoed: " + ", ".join(sorted({v["veto"] for v in basis["vetoes"]}))
    doubts = []
    if basis.get("undecided_labels"):
        doubts.append("undecided for " + ", ".join(basis["undecided_labels"]))
    if verdict == "success" and basis.get("contested_waivers"):
        doubts.append(
            "waived at the start but with events that are not valid: "
            + ", ".join(basis["contested_waivers"])
        )
    if basis.get("require_place"):
        doubts.append(
            "the last-placement condition is not applied here: the live verdict decides"
        )
    if basis.get("rule") == "final_state" and undecided(verdict, basis):
        doubts.append("the final state could not be read from the last frames")
    if verdict == "success" and basis.get("undecided_vetoes"):
        doubts.append(
            "veto undecided: "
            + ", ".join(sorted({v["veto"] for v in basis["undecided_vetoes"]}))
        )
    proposal = Proposal(
        episode_index=episode,
        kind="outcome",
        content=content[:8000],
        start=float(summary["start"]),
        outcome=verdict,
        evidence_ids=cited[:MAX_CITED],
        evidence_note=note[:1000],
        uncertainty="; ".join(doubts),
    )
    wb.validate_proposals(context, [proposal], evidence, summary)
    output = ModelOutput(
        summary=f"{title_of(spec) or spec.id}: {sum(e['valid'] for e in events)}/{len(events)} valid",
        proposals=[proposal],
    )
    return summary, evidence, output, total


def ask(wb, id, config, context, episode, phase, spec, schema, shown, started):
    """One anchored question: the budget, cache and accounting of a model
    phase (see ``Workbench.model_step``), with the question and schema of
    ``spec`` (the spec itself, its start check or one of its vetoes) instead
    of the annotation contract."""
    from .planning import require
    from .runtime import EpisodeRejected
    from .schema import ProviderConfig
    from .store import digest, file_hash

    run = wb.store.get("runs", id)
    require(wb, run)
    if run.get("control"):
        raise ValueError("Execution paused/cancelled before model call")
    current = ProviderConfig.model_validate(wb.store.get("providers", config.name))
    if not current.enabled or current.model_dump(
        exclude={"enabled"}
    ) != config.model_dump(exclude={"enabled"}):
        raise ValueError(
            "Provider configuration changed or disconnected before model phase"
        )
    directory = wb.store.run_dir(id)
    fingerprint = digest(
        {
            "input": run["manifest"],
            "config": config.model_dump(),
            "question": spec.question,
            "schema": schema,
            "max_output_tokens": spec.max_output_tokens,
            "images": [(s["evidence_id"], s["sha256"]) for s in shown],
        }
    )
    cache_id = f"{id}:{episode}:{phase}"
    try:
        cached = wb.store.get("model_cache", cache_id)
    except KeyError:
        cached = None
    if cached and cached["fingerprint"] == fingerprint:
        for s in shown:
            if file_hash(directory / "evidence" / s["artifact"]) != s["sha256"]:
                raise ValueError("Evidence changed; cached result is invalid")
        wb.store.mutate(
            "runs", id, lambda r: r.update(cache_hits=r.get("cache_hits", 0) + 1)
        )
        return cached["answer"], {**cached["usage"], "cached": True}
    available = (
        float("inf")
        if context.budget.max_tokens is None
        else context.budget.max_tokens - run["tokens"] - run["reserved_tokens"]
    )
    seconds = (
        context.budget.max_seconds
        - run["elapsed_seconds"]
        - (time.monotonic() - started)
    )
    if run["requests"] >= context.budget.max_calls or available < 256 or seconds < 1:
        raise ValueError("Approved budget exhausted")
    reservation = min(max(8192, config.context_tokens), available)
    wb.store.mutate(
        "runs",
        id,
        lambda r: r.update(
            requests=r["requests"] + 1,
            reserved_tokens=r["reserved_tokens"] + reservation,
        ),
    )
    budget = context.budget.model_copy(
        update={
            "max_tokens": reservation,
            "max_calls": 1,
            "max_seconds": max(1, int(seconds)),
        }
    )
    began = time.monotonic()
    problem = None
    try:
        raw, usage = wb.provider.ask(
            config,
            spec.question,
            [s["artifact"] for s in shown],
            directory / "evidence",
            schema,
            spec.max_output_tokens,
            budget,
        )
    except BaseException:
        wb.store.mutate(
            "runs",
            id,
            lambda r: r.update(reserved_tokens=r["reserved_tokens"] - reservation),
        )
        wb.store.event(id, "usage_unknown", phase=phase, episode=episode)
        raise
    answer = None
    try:
        answer = validate_answer(spec, raw)
    except ValueError as exc:
        # Spent is spent: settle the tokens, then set the episode aside.
        problem = exc
    overspent = usage.get("tokens", reservation) > reservation
    usage = {
        **usage,
        "elapsed_seconds": time.monotonic() - began,
        "images": len(shown),
        "usage_kind": usage.get(
            "usage_kind",
            "reported" if "tokens" in usage else "conservative_reservation",
        ),
    }
    with wb.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        latest = wb.store.get("runs", id)
        latest.update(
            requests=latest["requests"] - 1 + usage.get("requests", 1),
            reserved_tokens=latest["reserved_tokens"] - reservation,
            tokens=latest["tokens"] + usage.get("tokens", reservation),
        )
        wb.store.save(db, "runs", id, latest)
        if not overspent and problem is None:
            wb.store.save(
                db,
                "model_cache",
                cache_id,
                {"fingerprint": fingerprint, "answer": answer, "usage": usage},
            )
    if overspent:
        raise ValueError(
            "Provider exceeded reserved budget; stopped before further calls"
        )
    wb.store.event(id, "model_step", phase=phase, episode=episode, usage=usage)
    if problem is not None:
        (directory / f"episode_{episode:06d}-{phase}-rejected.json").write_text(
            json.dumps(
                {
                    "learner_error": str(problem),
                    "learner_raw": (raw or "")[:20000],
                    "usage": usage,
                },
                ensure_ascii=False,
            )
        )
        raise EpisodeRejected(str(problem), phase) from problem
    return answer, usage


# --- reading results -------------------------------------------------------


def records(store, dataset_key, run_id=None):
    """Anchored runs on a dataset, newest first: (run, {episode: record})."""
    runs = [
        r
        for r in store.list("runs")
        if r.get("dataset_key") == dataset_key
        and ((r.get("context") or {}).get("workflow") or {}).get("anchored")
        and (run_id is None or r["id"] == run_id)
    ]
    runs.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    out = []
    for run in runs:
        found = {}
        prefix = f"{run['id']}:"
        for key in store.ids("anchored"):
            if key.startswith(prefix):
                found[int(key[len(prefix) :])] = store.get("anchored", key)
        out.append((run, found))
    return out


def run_summary(run, found):
    spec = run["context"]["workflow"]["anchored"]
    return {
        "run_id": run["id"],
        "status": run["status"],
        "created_at": run.get("created_at"),
        "spec": {
            "id": spec["id"],
            "version": spec.get("version"),
            "title": titles(spec),
        },
        "episodes": {
            str(ep): {
                "outcome": rec["outcome"],
                "events": len(rec["events"]),
                "valid": sum(e["valid"] for e in rec["events"]),
            }
            for ep, rec in sorted(found.items())
        },
        "requests": run.get("requests"),
        "tokens": run.get("tokens"),
        "elapsed_seconds": run.get("elapsed_seconds"),
    }


def payload(store, dataset_key, episode=None, run_id=None) -> dict[str, Any] | None:
    """The newest anchored review of a dataset (or ``run_id``): per-episode
    outcomes, or for one episode its record with every event's evidence.
    None when there is none (for that episode)."""
    for run, found in records(store, dataset_key, run_id):
        if episode is None:
            if found:
                return run_summary(run, found)
            continue
        if episode in found:
            spec = run["context"]["workflow"]["anchored"]
            record = found[episode]

            # The store keeps keys sorted; answers read in the spec's order.
            def ordered(answer, fields):
                order = [f["name"] for f in fields or []]
                return {k: answer[k] for k in order if k in answer}

            vetoes = {v["id"]: v for v in spec.get("vetoes") or []}
            for event in record["events"]:
                event["answer"] = ordered(event["answer"], spec["fields"])
                for v in event.get("vetoes") or []:
                    if v.get("answer") is not None and v["id"] in vetoes:
                        v["answer"] = ordered(v["answer"], vetoes[v["id"]]["fields"])
            if record.get("start") and spec.get("start"):
                record["start"]["answer"] = ordered(
                    record["start"]["answer"], spec["start"]["fields"]
                )
            shown = {
                "id": spec["id"],
                "version": spec.get("version"),
                "title": titles(spec),
                "fields": spec["fields"],
                "valid_when": spec["valid_when"],
            }
            for key in ("start", "vetoes"):
                if spec.get(key):
                    shown[key] = spec[key]
            return {**record, "status": run["status"], "spec": shown}
    return None
