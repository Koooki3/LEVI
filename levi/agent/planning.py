"""Executable approval contracts. Planning is deterministic and never calls a model."""

import time

from pydantic import Field, model_validator

from .schema import Contract
from .store import Conflict, annotation_digest, digest


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
    definitions: list[SubtaskDefinition] = Field(default_factory=list, max_length=40)
    overlapping_layers: bool = False
    coarse_step_seconds: float = Field(default=2.0, ge=0.05, le=60)
    boundary_window_seconds: float = Field(default=1.0, ge=0.05, le=10)
    boundary_tolerance_seconds: float = Field(default=0.2, ge=0.01, le=5)
    max_evidence_frames: int = Field(default=96, ge=3, le=1000)
    object_concepts: list[str] = Field(default_factory=list, max_length=30)
    mask_definition: str = Field(default="visible", pattern="^visible$")
    pilot_episode: int | None = Field(default=None, ge=0)
    require_human_pilot: bool = True

    @model_validator(mode="after")
    def unique(self):
        if len({d.id for d in self.definitions}) != len(self.definitions):
            raise ValueError("Subtask IDs must be unique")
        if any(not item.strip() for item in self.object_concepts):
            raise ValueError("Object concepts cannot be blank")
        if not self.require_human_pilot:
            raise ValueError("This release requires human pilot acceptance")
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
        )
    }


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
    if flow.kind == "temporal" and not flow.definitions:
        missing.append(
            {
                "field": "definitions",
                "message": "Define observable subtask start, end and success conditions",
            }
        )
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
    run["plan"] = {
        "schema": "levi.harness.plan.v1",
        "revision": 1,
        "digest": digest(material(run)),
        "approval": None,
        "pilot_review": None,
        "pilot_episode": flow.pilot_episode
        if flow.pilot_episode is not None
        else run["context"]["episodes"][0],
        "questions": clarify(run["context"])["questions"],
        "estimate": {
            "basis": "one coarse request per episode plus one bounded temporal refinement; provider tool turns may add requests",
            "minimum_requests": 0
            if flow.kind == "objects"
            else len(run["context"]["episodes"])
            * (2 if flow.kind == "temporal" else 1),
            "tokens": "unknown until pilot; budget is a stop limit, not a price quote",
        },
        "submission": "draft_then_human_commit",
        "excluded": [
            "unselected episodes/cameras",
            "source writes",
            "automatic commit",
            "cross-camera identity inference",
        ],
    }
    return run


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
    if (
        wb.store.head(run["dataset_key"]) != run["base_revision"]
        or annotation_digest(wb.store.state, run["dataset_key"]) != run["base_content"]
    ):
        raise Conflict("Annotation baseline changed; create and approve a new plan")
    if bulk and (plan.get("pilot_review") or {}).get("accepted"):
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
        if (
            budget.max_calls < run["requests"]
            or budget.max_tokens < run["tokens"] + run["reserved_tokens"]
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
