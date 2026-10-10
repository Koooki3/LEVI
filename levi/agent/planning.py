"""Executable approval contracts. Planning is deterministic and never calls a model."""

import time
from typing import Any, Literal

from pydantic import Field, model_serializer, model_validator

from .schema import Contract
from .store import Conflict, annotation_digest, digest

# Where an event candidate may come from: the episode's recorded signals
# (``levi.events``). Gripper crossings, height turns, the edges of still
# spans, penalised change points.
EVENT_SOURCES = ("gripper", "height", "still", "change_point")


class EventIntelligence(Contract):
    """What a temporal plan lets LEVI do with event candidates (``workflow.
    event_intelligence``; docs/EVENTS.md).

    ``candidates``: before the boundary refinement, read event candidates
    from the episode's recorded signals and add the windows around the most
    salient ones to the refinement's frames, within the plan's frame cap and
    the model's image limit (``levi.events.sampling``). The model's prompt,
    its number of requests and the coarse pass are unchanged; a candidate is
    a place to look, never a boundary. Every value below is frozen in the
    plan, so changing one needs a new approval.

    Omitted, ``None`` and ``{"mode": "off"}`` all mean off and are stored
    the same way: as no key at all, so a plan without event intelligence
    freezes exactly what it froze before this block existed.
    """

    mode: Literal["off", "candidates"] = "off"
    sources: list[Literal["gripper", "height", "still", "change_point"]] = Field(
        default_factory=lambda: list(EVENT_SOURCES), min_length=1, max_length=4
    )
    # Candidate windows added to one episode's refinement, at most.
    max_windows: int = Field(default=4, ge=1, le=16)
    # Candidates of different sources closer than this are one candidate.
    merge_seconds: float = Field(default=0.5, ge=0, le=5)
    # The change-point penalty (levi.events.change_points.PENALTY, chosen on
    # development gold labels).
    change_point_penalty: float = Field(default=0.75, gt=0, le=100)
    # How the frames are shared: whole windows in priority order, a window
    # that does not fit is skipped (never thinned).
    planner: Literal["greedy"] = "greedy"
    # A model asking for more evidence on its own: not available; the frames
    # a run reads are decided by LEVI and this plan.
    active_evidence: Literal[False] = False

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("event_intelligence.sources must be distinct")
        return self


class SubtaskDefinition(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    label: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=1, max_length=2000)
    starts_when: str = Field(min_length=1, max_length=1000)
    ends_when: str = Field(min_length=1, max_length=1000)
    success_when: str = Field(min_length=1, max_length=1000)
    confusions: str = Field(default="", max_length=2000)


class Workflow(Contract):
    kind: str = Field(default="review", pattern=r"^(review|temporal|objects)$")
    definitions: list[SubtaskDefinition] = Field(default_factory=list, max_length=64)
    overlapping_layers: bool = False
    coarse_step_seconds: float = Field(default=2.0, ge=0.05, le=60)
    boundary_window_seconds: float = Field(default=1.0, ge=0.05, le=10)
    boundary_tolerance_seconds: float = Field(default=0.2, ge=0.01, le=5)
    max_evidence_frames: int = Field(default=96, ge=3, le=1000)
    object_concepts: list[str] = Field(default_factory=list, max_length=30)
    mask_definition: str = Field(default="visible", pattern="^visible$")
    # Review: how many of the episode's samples sit at its end, 0.5 s apart
    # and ending on the last frame; the rest stay spread over the episode.
    # An outcome is judged on the final state, which uniform samples show once.
    final_samples: int = Field(default=0, ge=0, le=16)
    # Temporal: refine boundaries in a second pass "always" (default,
    # recommended) or "auto" -- only when the coarse step is wider than half
    # the boundary window. "auto" is faster but measured less accurate than
    # "always" in paired runs (see ``runtime.refines``).
    refine: Literal["always", "auto"] = "always"
    pilot_episode: int | None = Field(default=None, ge=0)
    # False: the person approving the plan waives the separate pilot step
    # (plan approval and the final commit still take a person). For a capable
    # external agent the pilot cost an extra agent and a wait before the rest.
    require_human_pilot: bool = True
    # Review: judge the episode at the events its robot signals mark instead
    # of on evenly spread samples -- a built-in spec's id ({"spec": "<id>"})
    # or a whole spec, resolved and frozen here (see ``anchored.py``).
    anchored: dict[str, Any] | None = None
    # Temporal: event candidates from the recorded signals choose extra
    # refinement windows (see ``EventIntelligence``). Off leaves no key.
    event_intelligence: EventIntelligence | None = None

    @model_serializer(mode="wrap")
    def _frozen(self, handler):
        out = handler(self)
        if out.get("event_intelligence") is None:
            out.pop("event_intelligence", None)
        return out

    @model_validator(mode="after")
    def unique(self):
        if (
            self.event_intelligence is not None
            and self.event_intelligence.mode == "off"
        ):
            self.event_intelligence = None
        if self.event_intelligence is not None and self.kind != "temporal":
            raise ValueError(
                "Event intelligence chooses refinement frames; it is for "
                "temporal workflows"
            )
        if self.anchored is not None:
            if self.kind != "review":
                raise ValueError("An anchored review is a review workflow")
            from .anchored import resolve

            self.anchored = resolve(self.anchored)
        if not self.require_human_pilot and self.kind == "objects":
            raise ValueError(
                "Object masks are reviewed on the pilot; the pilot cannot be waived"
            )
        if len({d.id for d in self.definitions}) != len(self.definitions):
            raise ValueError("Subtask IDs must be unique")
        if any(not item.strip() for item in self.object_concepts):
            raise ValueError("Object concepts cannot be blank")
        return self


def material(run):
    return {
        k: run[k]
        for k in (
            "context",
            "provider_config",
            "base_revision",
            "base_content",
            "planned_hashes",
            "files",
            "skill_fingerprints",
            *(["runtime_binding"] if run.get("runtime_binding") else []),
            # Only a plan with event intelligence on has it (see attach).
            *(["event_algorithm"] if run.get("event_algorithm") else []),
        )
    } | (
        # The approved plan covers the harness parameters it runs with; the
        # memory slice is context, not a parameter, and stays out.
        {"harness": {k: run["harness"][k] for k in ("parameters", "sources")}}
        if run.get("harness")
        else {}
    )


def clarify(context):
    from .schema import TaskContext

    ctx = TaskContext.model_validate(context)
    flow = Workflow.model_validate(ctx.workflow)
    missing = []
    if flow.kind in {"temporal", "objects"} and not ctx.cameras:
        missing.append(
            {
                "field": "cameras",
                "message": "Select at least one camera for video annotation",
            }
        )
    # A temporal plan without definitions gets the dataset's vocabulary in
    # force (its own, else LEVI's built-in manipulation vocabulary) at
    # planning, so they are no longer something to ask for.
    if flow.kind == "objects" and not flow.object_concepts:
        missing.append(
            {"field": "object_concepts", "message": "Specify target object concepts"}
        )
    if (
        ctx.cameras
        and not ctx.allow_media_egress
        and ctx.provider != "external"
        and flow.kind != "objects"
    ):
        missing.append(
            {
                "field": "allow_media_egress",
                "message": "Authorize the selected media scope or use a local object-only workflow",
            }
        )
    if flow.anchored:
        from .anchored import cameras

        absent = [c for c in cameras(flow.anchored) if c not in ctx.cameras]
        if absent:
            missing.append(
                {
                    "field": "cameras",
                    "message": "Select the cameras the anchored review spec shows: "
                    + ", ".join(absent),
                }
            )
    if flow.pilot_episode is not None and flow.pilot_episode not in ctx.episodes:
        missing.append(
            {
                "field": "pilot_episode",
                "message": "Pilot must belong to the approved scope",
            }
        )
    return {
        "questions": missing,
        "ready": not missing,
        "workflow": flow.model_dump(),
        "model_calls": 0,
    }


def _extra_requests(spec):
    """What an anchored spec's start check and vetoes add to the estimate."""
    asked = [v["id"] for v in spec.get("vetoes") or [] if v.get("question")]
    return (
        f", plus one per veto question asked at it ({', '.join(asked)})"
        if asked
        else ""
    ) + (", plus one start check per episode" if spec.get("start") else "")


def attach(run):
    flow = Workflow.model_validate(run["context"]["workflow"])
    from .runtime_binding import binding

    run["runtime_binding"] = binding(run["context"].get("pilot_runtime"))
    from .observations import skills
    from .schema import TaskContext

    run["skill_fingerprints"] = {
        k: digest(v)
        for k, v in skills(TaskContext.model_validate(run["context"])).items()
    }
    if flow.event_intelligence is not None:
        # The candidate readers' constants and the planner's version are
        # approved with the plan (in its digest; require() compares them).
        from levi.events.candidates import algorithm

        run["event_algorithm"] = algorithm()
    else:
        run.pop("event_algorithm", None)
    run["plan"] = {
        "schema": "levi.harness.plan.v1",
        "revision": 1,
        "digest": digest(material(run)),
        "approval": None,
        # Waived in the plan itself, so approving the plan approves the waiver.
        "pilot_review": None
        if flow.require_human_pilot
        else {"accepted": True, "waived": True, "actor": "plan"},
        "pilot_episode": flow.pilot_episode
        if flow.pilot_episode is not None
        else run["context"]["episodes"][0],
        "questions": clarify(run["context"])["questions"],
        "estimate": {
            "basis": (
                (
                    "one request per episode on the frames at its end"
                    if flow.anchored["anchor"]["event"] == "end"
                    else "one request per anchor event (the robot's recorded "
                    f"gripper {flow.anchored['anchor']['event']}) per episode"
                )
                + _extra_requests(flow.anchored)
            )
            if flow.anchored
            else (
                "one coarse request per episode; a temporal plan adds a bounded "
                'boundary refinement (workflow.refine "always"; "auto" may skip '
                "it, a long episode is refined in several batches); provider tool "
                "turns may add requests"
            ),
            # An anchored review asks at least its start check per episode,
            # when its spec has one.
            "minimum_requests": (
                len(run["context"]["episodes"])
                # A final-state spec asks exactly once per episode (its one
                # question on the last frames), plus its start check when it
                # has one; a release review may ask none.
                * (
                    1
                    + bool(
                        flow.anchored.get("start")
                        and (flow.anchored.get("anchor") or {}).get("event") == "end"
                    )
                )
                if flow.anchored.get("start")
                or (flow.anchored.get("anchor") or {}).get("event") == "end"
                else 0
            )
            if flow.anchored
            else 0
            if flow.kind == "objects"
            else len(run["context"]["episodes"])
            * (2 if flow.kind == "temporal" else 1),
            "tokens": "unknown until pilot; budget is a stop limit, not a price quote",
        },
        # Which spec the plan freezes and whether it is still a candidate,
        # for the person approving it.
        "anchored_spec": {
            "id": flow.anchored["id"],
            "version": flow.anchored.get("version"),
            "status": flow.anchored.get("status", "stable"),
        }
        if flow.anchored
        else None,
        "submission": "draft_then_human_commit",
        "excluded": [
            "unselected episodes/cameras",
            "source writes",
            "automatic commit",
            "cross-camera identity inference",
        ],
    }
    if flow.event_intelligence is not None:
        # Only a plan that turns it on carries these keys: a plan without it
        # looks exactly as it did before they existed.
        events = _event_estimate(flow, len(run["context"].get("cameras") or []))
        run["plan"]["estimate"]["basis"] += events.pop("basis")
        run["plan"]["estimate"]["event_intelligence"] = events
        run["plan"]["event_intelligence"] = flow.event_intelligence.model_dump()
    return run


def _event_estimate(flow, cameras):
    """What event candidates add to a temporal plan's cost, for the person
    approving it: frames, never requests."""
    import math

    events = flow.event_intelligence
    # The refinement samples a window at half the boundary tolerance at its
    # finest (``observations.refine_spacings``), from window before to after.
    per_window = (
        math.floor(
            2 * flow.boundary_window_seconds / (flow.boundary_tolerance_seconds / 2)
            + 1e-9
        )
        + 1
    )
    most = min(
        events.max_windows * per_window * max(1, cameras), flow.max_evidence_frames
    )
    return {
        "basis": (
            f"; event intelligence adds up to {events.max_windows} signal "
            "candidate windows per episode to that refinement (more frames in "
            "the same request, within max_evidence_frames; no extra request; "
            "more images cost more tokens, within max_tokens)"
        ),
        "extra_requests": 0,
        "max_windows_per_episode": events.max_windows,
        "frames_per_window": per_window * max(1, cameras),
        "max_extra_frames_per_episode": most,
        "frames_cap": flow.max_evidence_frames,
    }


def require(wb, run, *, bulk=False):
    from .observations import skills
    from .runtime_binding import binding
    from .schema import TaskContext

    if run.get("runtime_binding") != binding(run["context"].get("pilot_runtime")):
        raise Conflict(
            "Runtime configuration or account changed; create and approve a new plan"
        )
    current_skills = {
        k: digest(v)
        for k, v in skills(TaskContext.model_validate(run["context"])).items()
    }
    if run.get("skill_fingerprints") != current_skills:
        raise Conflict("Workflow skills changed; create and approve a new plan")
    plan = run.get("plan")
    if not plan or not plan.get("approval"):
        raise Conflict(
            "Approve this execution plan before starting; legacy plans must be recreated"
        )
    if (
        plan["digest"] != digest(material(run))
        or plan["approval"]["digest"] != plan["digest"]
    ):
        raise Conflict("Execution contract changed; plan must be approved again")
    if (run["context"].get("workflow") or {}).get("event_intelligence") or run.get(
        "event_algorithm"
    ):
        from levi.events.candidates import algorithm

        if run.get("event_algorithm") != algorithm():
            raise Conflict(
                "The event candidate algorithm changed; create and approve a new plan"
            )
    if (
        wb.store.head(run["dataset_key"]) != run["base_revision"]
        or annotation_digest(wb.store.state, run["dataset_key"]) != run["base_content"]
    ):
        raise Conflict("Annotation baseline changed; create and approve a new plan")
    review = plan.get("pilot_review") or {}
    if bulk and review.get("accepted") and not review.get("waived"):
        current = wb.store.get("changes", run["changes"])
        if plan["pilot_review"].get("draft_digest") != pilot_digest(
            current, plan["pilot_episode"]
        ):
            raise Conflict(
                "Pilot annotations changed after acceptance; review the pilot again"
            )
    if bulk and not (plan.get("pilot_review") or {}).get("accepted"):
        raise Conflict("Review and accept the pilot before expanding execution")


def approve(wb, id, revision, who):
    def update(run):
        plan = run.get("plan")
        if not plan or plan["revision"] != revision or run["status"] != "planned":
            raise Conflict("Plan revision or state changed")
        if plan["questions"]:
            raise ValueError("Resolve the plan's missing requirements before approval")
        if plan["digest"] != digest(material(run)):
            raise Conflict("Plan changed")
        plan["approval"] = {"digest": plan["digest"], "actor": who, "at": time.time()}

    result = wb.store.mutate("runs", id, update)
    require(wb, result)
    wb.store.event(id, "plan_approved", revision=revision, actor=who)
    return result


def pilot_review(wb, id, revision, accepted, note, who):
    run = wb.store.get("runs", id)
    if (run["plan"].get("pilot_review") or {}).get("waived"):
        raise Conflict("This plan waived the pilot when it was approved")
    require(wb, run)
    if (
        run["status"] != "waiting_for_review"
        or run["plan"]["pilot_episode"] not in run["completed"]
    ):
        raise Conflict("A completed pilot is required")
    change = wb.store.get("changes", run["changes"])
    if change["revision"] != revision:
        raise Conflict("Pilot draft changed; review the current revision")
    if run["context"]["workflow"]["kind"] == "objects" and not change.get(
        "object_jobs"
    ):
        raise ValueError("Complete and review the object pilot before acceptance")
    report = wb.validate(change)
    if accepted and report.get("pending_objects"):
        raise ValueError("Review pilot object masks first")

    def update(value):
        value["plan"]["pilot_review"] = {
            "accepted": accepted,
            "note": note,
            "actor": who,
            "changes_revision": revision,
            "draft_digest": pilot_digest(change, run["plan"]["pilot_episode"]),
            "at": time.time(),
        }

    wb.store.mutate("runs", id, update)
    wb.store.event(id, "pilot_reviewed", accepted=accepted, actor=who)
    return wb.store.get("runs", id)


def pilot_digest(change, episode):
    return digest(
        [
            {"proposal": p, "decision": change.get("decisions", {}).get(str(i))}
            for i, p in enumerate(change["proposals"])
            if p["episode_index"] == episode
        ]
    )


def rebudget(wb, id, revision, budget):
    """Budget-only revision retains completed work; approval is always revoked."""

    def update(run):
        if run["status"] in {
            "running",
            "queued",
            "succeeded",
            "partially_succeeded",
            "cancelled",
        }:
            raise Conflict("Pause execution before revising its budget")
        if run["plan"]["revision"] != revision:
            raise Conflict("Plan revision changed")
        if budget.max_seconds is None and run["context"]["provider"] != "external":
            raise ValueError("Unlimited task duration requires an external Agent")
        if budget.max_calls < run["requests"] or (
            budget.max_tokens is not None
            and budget.max_tokens < run["tokens"] + run["reserved_tokens"]
        ):
            raise ValueError(
                "New budget cannot be below already consumed or reserved usage"
            )
        old = run["plan"]
        wb.store.put("plan_history", f"{id}:{revision}", old)
        run["context"]["budget"] = budget.model_dump()
        attach(run)
        run["plan"]["revision"] = revision + 1
        run["plan"]["pilot_review"] = old.get("pilot_review")
        run["status"] = "planned"
        run["control"] = None

    result = wb.store.mutate("runs", id, update)
    wb.store.event(
        id, "plan_revised", revision=revision + 1, reused_episodes=result["completed"]
    )
    return result
