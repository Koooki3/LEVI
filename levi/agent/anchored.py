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

from .schema import Contract

SPECS_DIR = Path(__file__).parent / "anchored_specs"
# Hysteresis on the gripper channel's range, as in signals.py.
LOW, HIGH = 0.35, 0.65
# Frames an outcome proposal may cite (Proposal.evidence_ids).
MAX_CITED = 32


class AnchorSpec(Contract):
    signal: Literal["gripper"] = "gripper"
    event: Literal["open", "close"] = "open"
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


class EpisodeRule(Contract):
    # Success when every label here has a valid event (label_field names the
    # answer that carries it); without labels, when at least min_valid events
    # are valid.
    label_field: str | None = None
    require_labels: list[str] = Field(default_factory=list, max_length=16)
    min_valid: int = Field(default=1, ge=1, le=100)


class AnchoredSpec(Contract):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    version: int = Field(default=1, ge=1)
    # Display name per language ({"en": ..., "zh": ...}); the id is shown
    # only where there is no title.
    title: dict[str, str] | None = None
    description: str = Field(default="", max_length=2000)
    anchor: AnchorSpec = Field(default_factory=AnchorSpec)
    views: list[ViewSpec] = Field(min_length=1, max_length=8)
    question: str = Field(min_length=1, max_length=8000)
    # Ordered: the model writes the fields in this order.
    fields: list[AnswerField] = Field(min_length=1, max_length=16)
    valid_when: list[Condition] = Field(min_length=1, max_length=16)
    unknown_values: list[str] = Field(default_factory=lambda: ["unclear"])
    episode: EpisodeRule = Field(default_factory=EpisodeRule)
    max_output_tokens: int = Field(default=200, ge=16, le=4096)

    @model_validator(mode="after")
    def consistent(self):
        if self.title is not None and (
            not self.title
            or any(
                not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2,4})?", k)
                or not v.strip()
                or len(v) > 200
                for k, v in self.title.items()
            )
        ):
            raise ValueError(
                "title maps language codes to non-empty names of up to 200 characters"
            )
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("Answer field names must be unique")
        values = {f.name: set(f.enum) for f in self.fields}
        for c in self.valid_when:
            if c.field not in values:
                raise ValueError(f"valid_when names an unknown field {c.field!r}")
            unknown = set(c.is_in or c.not_in or []) - values[c.field]
            if unknown:
                raise ValueError(
                    f"valid_when on {c.field!r} uses values it cannot take: "
                    + ", ".join(sorted(unknown))
                )
        rule = self.episode
        if rule.require_labels:
            if rule.label_field not in values:
                raise ValueError("episode.label_field must name an answer field")
            if set(rule.require_labels) - values[rule.label_field]:
                raise ValueError("episode.require_labels must be values of label_field")
        if len({v.role for v in self.views}) != len(self.views):
            raise ValueError("View roles must be unique")
        return self

    def answer_schema(self):
        """The JSON schema the server decodes against: every field required,
        in the spec's order, each one of its values."""
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [f.name for f in self.fields],
            "properties": {f.name: {"enum": list(f.enum)} for f in self.fields},
        }


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
        return spec.model_dump(by_alias=True)
    return AnchoredSpec.model_validate(value).model_dump(by_alias=True)


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
    starts at is its initial state."""
    values = np.asarray(values, dtype=float)
    finite = np.isfinite(values)
    if finite.sum() < 2:
        return []
    lo, hi = (
        bounds
        if bounds and np.isfinite(bounds).all() and bounds[1] - bounds[0] > 1e-9
        else (np.nanmin(values), np.nanmax(values))
    )
    if hi - lo <= 1e-9:
        return []
    u = (values - lo) / (hi - lo)
    if open_level == "low":
        u = 1 - u
    first = int(np.argmax(finite))
    state = "open" if u[first] >= 0.5 else "closed"
    out = []
    for i in range(first + 1, len(u)):
        if not finite[i]:
            continue
        if state == "closed" and u[i] > HIGH:
            state = "open"
            out.append((i, "open"))
        elif state == "open" and u[i] < LOW:
            state = "closed"
            out.append((i, "close"))
    return out


def anchors(table, info, stats, anchor):
    """(row positions of the anchor events, channel description)."""
    key, name, values, bounds = _gripper_channel(table, info, stats, anchor)
    found = crossings(values, bounds, anchor.open_level)
    return [i for i, kind in found if kind == anchor.event], f"{key}.{name}"


def view_offsets(view, fps):
    if view.offsets is not None:
        return list(view.offsets)
    return [round(s * fps) for s in view.offsets_seconds]


# --- judging ---------------------------------------------------------------


def judge(spec, answer):
    """Each condition as supported / contradicted / unknown, and the event's
    reading: supported (valid) when all hold, contradicted when one fails on
    a definite answer, unknown otherwise."""
    checks = []
    for c in spec.valid_when:
        value = answer.get(c.field)
        holds = value in c.is_in if c.is_in is not None else value not in c.not_in
        checks.append(
            {
                "field": c.field,
                "value": value,
                "result": "supported"
                if holds
                else "unknown"
                if value in spec.unknown_values
                else "contradicted",
            }
        )
    results = {c["result"] for c in checks}
    verdict = (
        "supported"
        if results == {"supported"}
        else "contradicted"
        if "contradicted" in results
        else "unknown"
    )
    return checks, verdict


def outcome(spec, events):
    """The episode's outcome from its judged events, and what it rests on."""
    valid = [e for e in events if e["valid"]]
    rule = spec.episode
    if rule.require_labels:
        labels = {e["answer"].get(rule.label_field) for e in valid}
        missing = [x for x in rule.require_labels if x not in labels]
        # A missing label with an undecided event is the reviewer's to look at.
        undecided = [
            x
            for x in missing
            if any(
                e["verdict"] == "unknown"
                and e["answer"].get(rule.label_field) in (x, *spec.unknown_values)
                for e in events
            )
        ]
        return (
            "failure" if missing else "success",
            {
                "valid_labels": sorted(labels, key=str),
                "missing_labels": missing,
                "undecided_labels": undecided,
            },
        )
    ok = len(valid) >= rule.min_valid
    return "success" if ok else "failure", {
        "valid_events": len(valid),
        "min_valid": rule.min_valid,
    }


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
    positions, channel = anchors(table, info, stats, spec.anchor)
    last = len(table) - 1
    frames = table.frame_index.to_numpy(dtype=int)
    times = table.timestamp.to_numpy(dtype=float)
    adapter = DATASETS[context.dataset_adapter]
    # Frames per camera: every offset of every anchor, clamped to the episode.
    wanted = {}
    for view in spec.views:
        offsets = view_offsets(view, fps)
        wanted[view.role] = {
            n: [int(frames[min(last, max(0, n + d))]) for d in offsets]
            for n in positions
        }
    evidence, summary = [], None
    for view in spec.views:
        chosen = sorted({f for rows in wanted[view.role].values() for f in rows})
        if not chosen and view is spec.views[0]:
            # No event: the outcome still cites what the episode ends on.
            chosen = [int(frames[last])]
        if not chosen:
            continue
        scoped = context.model_copy(update={"cameras": [view.camera]})
        summary, rows = adapter.sample_frames(scoped, root, episode, folder, chosen)
        evidence += rows
    summary["workflow"] = context.workflow
    summary["anchored"] = {"spec": spec.id, "channel": channel}
    evidence = observations.persist(wb, id, episode, summary, evidence)
    by_frame = {(row["camera_key"], row["frame_index"]): row for row in evidence}
    events, total = [], {"requests": 0, "tokens": 0, "elapsed_seconds": 0.0}
    schema = spec.answer_schema()
    for n in positions:
        shown = []
        for view in spec.views:
            for d, frame in zip(
                view_offsets(view, fps), wanted[view.role][n], strict=True
            ):
                row = by_frame[(view.camera, frame)]
                shown.append(
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
        answer, usage = ask(
            wb,
            id,
            config,
            context,
            episode,
            f"anchor-{int(frames[n]):06d}",
            spec,
            schema,
            shown,
            started,
        )
        checks, verdict = judge(spec, answer)
        events.append(
            {
                "frame_index": int(frames[n]),
                "timestamp": float(times[n]),
                "answer": answer,
                "checks": checks,
                "verdict": verdict,
                "valid": verdict == "supported",
                "frames": [
                    {
                        k: s[k]
                        for k in (
                            "role",
                            "camera",
                            "offset",
                            "frame_index",
                            "evidence_id",
                        )
                    }
                    for s in shown
                ],
                "usage": {
                    k: usage.get(k)
                    for k in ("tokens", "prompt_tokens", "elapsed_seconds", "cached")
                    if usage.get(k) is not None
                },
            }
        )
        total["requests"] += 0 if usage.get("cached") else 1
        total["tokens"] += usage.get("tokens") or 0
        total["elapsed_seconds"] += usage.get("elapsed_seconds") or 0.0
    verdict, basis = outcome(spec, events)
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
    wb.store.put("anchored", f"{id}:{episode}", record)
    (directory / f"episode_{episode:06d}-anchored.json").write_text(
        json.dumps(record, ensure_ascii=False)
    )
    # Cite the frames the verdict rests on: the valid events first, each by
    # its frames nearest the anchor (one before, one after, per camera).
    cited = []
    for e in sorted(events, key=lambda e: not e["valid"]):
        for view in spec.views:
            mine = [f for f in e["frames"] if f["role"] == view.role]
            before = [f for f in mine if f["offset"] < 0]
            after = [f for f in mine if f["offset"] >= 0]
            for f in ([before[-1]] if before else []) + ([after[0]] if after else []):
                if f["evidence_id"] not in cited:
                    cited.append(f["evidence_id"])
    if not cited:
        cited = [evidence[-1]["id"]]
    name = spec.anchor.event
    parts = [
        f"{'valid' if e['valid'] else e['verdict']} {name} at {e['timestamp']:.1f} s ("
        + ", ".join(f"{k}={v}" for k, v in e["answer"].items())
        + ")"
        for e in events
    ]
    content = (
        f"Anchored review ({title_of(spec) or spec.id}): {len(events)} gripper "
        f"{name} event(s); "
        + ("; ".join(parts) if parts else "none recorded")
        + f". Outcome {verdict}."
    )
    note = (
        "valid: " + ", ".join(basis["valid_labels"])
        if "valid_labels" in basis
        else f"{basis['valid_events']} valid event(s)"
    )
    uncertainty = (
        "undecided for " + ", ".join(basis["undecided_labels"])
        if basis.get("undecided_labels")
        else ""
    )
    proposal = Proposal(
        episode_index=episode,
        kind="outcome",
        content=content[:8000],
        start=float(summary["start"]),
        outcome=verdict,
        evidence_ids=cited[:MAX_CITED],
        evidence_note=note[:1000],
        uncertainty=uncertainty,
    )
    wb.validate_proposals(context, [proposal], evidence, summary)
    output = ModelOutput(
        summary=f"{title_of(spec) or spec.id}: {sum(e['valid'] for e in events)}/{len(events)} valid",
        proposals=[proposal],
    )
    return summary, evidence, output, total


def ask(wb, id, config, context, episode, phase, spec, schema, shown, started):
    """One anchored question: the budget, cache and accounting of a model
    phase (see ``Workbench.model_step``), with the spec's question and schema
    instead of the annotation contract."""
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
            order = [f["name"] for f in spec["fields"]]
            for event in record["events"]:
                event["answer"] = {
                    k: event["answer"][k] for k in order if k in event["answer"]
                }
            return {
                **record,
                "status": run["status"],
                "spec": {
                    "id": spec["id"],
                    "version": spec.get("version"),
                    "title": titles(spec),
                    "fields": spec["fields"],
                    "valid_when": spec["valid_when"],
                },
            }
    return None
