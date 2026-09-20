"""One capability dispatcher for REST, MCP and the Workbench UI."""

from typing import Any, Literal

from pydantic import Field

from .runtime import Workbench
from .schema import Budget, Contract, Proposal, TaskContext
from .security import Principal
from .store import Conflict


class Empty(Contract):
    pass


class RunRef(Contract):
    run_id: str


class PlanApproval(RunRef):
    revision: int = Field(ge=1)


class Rebudget(PlanApproval):
    budget: Budget


class PilotReview(RunRef):
    revision: int = Field(ge=0)
    accepted: bool
    note: str = Field(min_length=1, max_length=2000)


class Start(RunRef):
    pilot: bool = True


class ChangesRef(Contract):
    changeset_id: str


class Review(ChangesRef):
    revision: int = Field(ge=0)


class Decide(Review):
    indices: list[int] = Field(min_length=1, max_length=500)
    decision: Literal["accepted", "rejected"]


class Edit(Review):
    proposals: list[Proposal] = Field(max_length=500)


class Propose(RunRef):
    proposals: list[Proposal] = Field(max_length=500)
    inspected_episodes: list[int] = Field(min_length=1)


class Events(RunRef):
    after: int = Field(default=0, ge=0)


class Recall(RunRef):
    episode: int = Field(ge=0)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=32)


class ObjectJob(Contract):
    job_id: str


class ObjectInspect(ObjectJob):
    offset: int = Field(default=0, ge=0)


from levi.annotations.schema import ObjectEdit


class ObjectFrame(ObjectJob):
    episode: int = Field(ge=0)
    camera: str
    timestamp: float = Field(ge=0)
    window_seconds: float = Field(default=0, ge=0, le=2)


class ObjectCorrection(ObjectJob):
    edit: ObjectEdit


class ExportRequest(RunRef):
    target: str = "lerobot-native"


class Seek(RunRef):
    evidence_id: str


from .objects import ObjectRequest

SPECS = {
    "runs.result": (
        RunRef,
        "read",
        "Read the durable artifact manifest and review status",
    ),
    "plans.rebudget": (
        Rebudget,
        "approve",
        "Revise budget, retain completed shards, revoke execution approval",
    ),
    "export.run": (
        RunRef,
        "commit",
        "Export reviewed native data to a new directory and re-read lossless sidecars",
    ),
    "objects.frame": (
        ObjectFrame,
        "read",
        "Read persistent staged masks for the existing player clock",
    ),
    "evidence.read": (
        Recall,
        "read",
        "Read a bounded page of exact evidence; images resolve via authenticated artifacts",
    ),
    "runs.quality": (
        RunRef,
        "read",
        "Read per-episode uncertainty and uncovered intervals",
    ),
    "plans.clarify": (
        TaskContext,
        "read",
        "Ask only missing requirements; never calls a model",
    ),
    "plans.approve": (
        PlanApproval,
        "approve",
        "Approve the exact execution contract; does not approve annotation commit",
    ),
    "plans.review_pilot": (
        PilotReview,
        "approve",
        "Accept or reject pilot quality before expanding scope",
    ),
    "runs.finish": (
        RunRef,
        "draft",
        "Freeze completed shards for partial human review without another model call",
    ),
    "objects.inspect": (
        ObjectInspect,
        "read",
        "Review staged masks against immutable source frames",
    ),
    "objects.edit": (
        ObjectCorrection,
        "approve",
        "Human correction of staged tracks; invalidates approval",
    ),
    "runs.prepare": (
        RunRef,
        "draft",
        "Create bounded immutable evidence artifacts without a model call",
    ),
    "annotations.propose_segments": (
        Propose,
        "draft",
        "Stage evidence-grounded suggestions; never approve",
    ),
    "annotations.propose_events": (
        Propose,
        "draft",
        "Stage event suggestions using the same validators",
    ),
    "episodes.query": (RunRef, "read", "Read frozen episode scope"),
    "objects.status": (
        Empty,
        "read",
        "Check configured worker/checkpoint without probing CUDA",
    ),
    "objects.plan": (ObjectRequest, "draft", "Plan SAM3 against the frozen snapshot"),
    "objects.run": (
        ObjectJob,
        "execute",
        "Run isolated SAM3 worker; stage results for human review",
    ),
    "objects.get": (ObjectJob, "read", "Get staged SAM3 progress"),
    "capabilities.list": (Empty, "read", "Discover schemas, scope and side effects"),
    "workspace.get_context": (
        Empty,
        "read",
        "List accessible catalog IDs, never local paths",
    ),
    "datasets.inspect": (
        TaskContext,
        "read",
        "Inspect fixed dataset scope and snapshot cost",
    ),
    "runs.plan": (TaskContext, "draft", "Persist an immutable run plan; no model call"),
    "runs.get": (RunRef, "read", "Read a durable run"),
    "runs.events": (Events, "read", "Replay sequenced run events"),
    "runs.execute": (
        Start,
        "execute",
        "Execute pilot or remaining shards; consumes model budget",
    ),
    "runs.pause": (RunRef, "execute", "Request pause at next safe boundary"),
    "runs.cancel": (
        RunRef,
        "execute",
        "Request cancellation; not an immediate termination claim",
    ),
    "runs.resume": (
        Start,
        "execute",
        "Resume uncompleted shards without repeating completed ones",
    ),
    "media.sample": (RunRef, "read", "Read frozen sampled evidence and coverage"),
    "changes.undo": (
        ChangesRef,
        "draft",
        "Create a conflict-checked inverse draft requiring human review",
    ),
    "changes.diff": (
        ChangesRef,
        "read",
        "Read typed suggestions, base version and provenance",
    ),
    "changes.review": (
        Decide,
        "approve",
        "Accept or reject selected suggestions in one human action",
    ),
    "changes.edit": (
        Edit,
        "draft",
        "Replace draft proposals with a revision precondition",
    ),
    "changes.validate": (
        ChangesRef,
        "read",
        "Validate source, evidence, range and annotation revision",
    ),
    "changes.approve": (Review, "approve", "Human approval of an exact draft revision"),
    "changes.commit": (
        Review,
        "commit",
        "Atomically publish all approved annotation changes",
    ),
    "view.seek": (
        Seek,
        "read",
        "Return evidence navigation; never force browser navigation",
    ),
    "export.plan": (
        ExportRequest,
        "read",
        "Describe native export constraints; never upload",
    ),
}


def public_run(run):
    return {
        k: v for k, v in run.items() if k not in {"files", "manifest", "planned_hashes"}
    }


def _invoke(
    workbench: Workbench,
    principal: Principal,
    name: str,
    arguments: dict[str, Any],
    key=None,
):
    if name not in SPECS:
        raise ValueError("Unknown capability")
    schema, permission, _ = SPECS[name]
    principal.require(permission)
    args = schema.model_validate(arguments)
    store = workbench.store
    run = None
    change = None
    if hasattr(args, "job_id"):
        job = store.get("object_jobs", args.job_id)
        run = store.get("runs", job["run_id"])
    elif hasattr(args, "changeset_id"):
        change = store.get("changes", args.changeset_id)
        run = store.get("runs", change["run_id"])
    elif hasattr(args, "run_id"):
        run = store.get("runs", args.run_id)
    repo = (
        args.repo_id
        if isinstance(args, TaskContext)
        else run["context"]["repo_id"]
        if run
        else None
    )
    principal.require(permission, repo)
    if (
        run
        and not principal.human
        and name
        in {"media.sample", "evidence.read", "objects.inspect", "objects.frame"}
        and run["context"]["cameras"]
        and not run["context"]["allow_media_egress"]
    ):
        raise PermissionError("External media access was not authorized by this plan")
    if name == "runs.result":
        from .tracking import manifest

        return manifest(workbench, args.run_id)
    if name == "export.run":
        from .exporting import export

        return export(workbench, run)
    if name == "objects.frame":
        from .objects import frame_result

        return frame_result(
            workbench,
            job,
            args.episode,
            args.camera,
            args.timestamp,
            args.window_seconds,
        )
    if name == "evidence.read":
        from .observations import recall

        return recall(workbench, run, args.episode, args.offset, args.limit)
    if name == "runs.quality":
        reports = []
        for ep in run["completed"]:
            try:
                reports.append(
                    {"episode": ep, **store.get("quality", f"{run['id']}:{ep}")}
                )
            except KeyError:
                pass
        return {"episodes": reports}
    if name == "plans.clarify":
        from .planning import clarify

        return clarify(args.model_dump())
    if name == "plans.rebudget":
        from .planning import rebudget

        return public_run(rebudget(workbench, args.run_id, args.revision, args.budget))
    if name == "plans.approve":
        from .planning import approve

        return public_run(approve(workbench, args.run_id, args.revision, principal.id))
    if name == "plans.review_pilot":
        from .planning import pilot_review

        return public_run(
            pilot_review(
                workbench,
                args.run_id,
                args.revision,
                args.accepted,
                args.note,
                principal.id,
            )
        )
    if name in {
        "runs.prepare",
        "annotations.propose_segments",
        "annotations.propose_events",
        "objects.plan",
        "objects.run",
    }:
        from .planning import require

        require(workbench, run)
    if name == "runs.finish":
        if run["status"] not in {
            "blocked",
            "paused",
            "interrupted",
            "waiting_for_review",
            "cancelled",
        }:
            raise Conflict("Stop the executor before reviewing partial work")
        if not run["completed"]:
            raise ValueError("No completed shards to review")
        workbench.prepare_changes(run["id"])
        if run["status"] == "cancelled":
            return public_run(store.get("runs", run["id"]))
        return public_run(
            store.mutate(
                "runs", run["id"], lambda r: r.update(status="waiting_for_review")
            )
        )
    if name == "runs.prepare":
        from .runtime import prepare_evidence

        return prepare_evidence(workbench, args.run_id)
    if name == "episodes.query":
        return {
            "episodes": run["context"]["episodes"],
            "cameras": run["context"]["cameras"],
        }
    if name.startswith("annotations.propose_"):
        if run["status"] in {"running", "queued", "succeeded", "cancelled"}:
            raise Conflict("Stop execution before staging suggestions")
        if set(args.inspected_episodes) & set(run["completed"]):
            raise Conflict("Use changes.edit for already completed episodes")
        if not set(args.inspected_episodes) <= set(run.get("prepared", [])):
            raise ValueError("Prepare evidence for every inspected episode first")
        if any(p.episode_index not in args.inspected_episodes for p in args.proposals):
            raise ValueError("Proposal is outside inspected scope")
        if set(args.inspected_episodes) != {run["plan"]["pilot_episode"]}:
            from .planning import require

            require(workbench, run, bulk=True)
        for ep in args.inspected_episodes:
            saved = store.get("evidence", f"{run['id']}:{ep}")
            proposals = [p for p in args.proposals if p.episode_index == ep]
            workbench.validate_proposals(
                TaskContext.model_validate(run["context"]),
                proposals,
                saved["items"],
                saved["summary"],
            )
            from .observations import quality

            store.put(
                "quality",
                f"{run['id']}:{ep}",
                quality(
                    TaskContext.model_validate(run["context"]),
                    proposals,
                    saved["items"],
                    saved["summary"],
                ),
            )
            store.put(
                "shards",
                f"{run['id']}:{ep}",
                {
                    "output": {
                        "summary": "External Agent suggestions",
                        "proposals": [p.model_dump() for p in proposals],
                    },
                    "usage": {"external": True},
                },
            )
        store.mutate(
            "runs",
            run["id"],
            lambda r: r.update(
                completed=sorted(set(r["completed"] + args.inspected_episodes)),
                status="waiting_for_review",
            ),
        )
        change = workbench.prepare_changes(run["id"])
        change["provenance"]["provider"] = {"external_principal": principal.id}
        store.put("changes", change["id"], change)
        return change
    if name == "objects.inspect":
        from .objects import inspect_result

        return inspect_result(workbench, job, args.offset)
    if name == "objects.edit":
        from .objects import edit_result

        return edit_result(workbench, job, args.edit)
    if name == "objects.status":
        from .objects import readiness

        return readiness()
    if name == "objects.plan":
        from .objects import plan

        return plan(workbench, args)
    if name == "objects.run":
        from .objects import launch

        return launch(workbench, args.job_id)
    if name == "objects.get":
        return {k: v for k, v in job.items() if k not in {"plan", "dataset_root"}}
    if name == "capabilities.list":
        return {
            "schema_version": "levi.agent.capabilities.v1",
            "tools": [
                {
                    "name": n,
                    "inputSchema": cls.model_json_schema(),
                    "permission": p,
                    "description": description,
                    "available": principal.human or p in principal.operations,
                    "side_effect": p not in {"read"},
                    "timeout_seconds": 120,
                    "retry": "read-only or idempotency-key",
                    "cancellation": "shard-boundary",
                }
                for n, (cls, p, description) in SPECS.items()
            ],
        }
    if name == "workspace.get_context":
        from levi.catalog import DEMOS, datasets

        ids = [r["id"] for r in datasets().values()] + DEMOS
        return {
            "datasets": [r for r in ids if principal.human or r in principal.datasets],
            "mode": "draft",
            "experimental": True,
        }
    if name == "datasets.inspect":
        from .formats import DATASETS

        adapter = DATASETS[args.dataset_adapter]
        ctx = adapter.pin(args)
        state, files = adapter.inspect(ctx)
        return {
            "context": ctx.model_dump(),
            "episodes": ctx.episodes,
            "version": state.info["codebase_version"],
            "snapshot_bytes": sum(files.values()),
            "within_budget": sum(files.values()) <= ctx.budget.max_snapshot_bytes,
        }
    if name == "runs.plan":
        return public_run(workbench.plan(args))
    if name == "runs.get":
        return public_run(run)
    if name == "runs.events":
        return {"events": store.events(args.run_id, args.after)}
    if name in {"runs.execute", "runs.resume"}:
        return public_run(workbench.launch(args.run_id, pilot=args.pilot))
    if name in {"runs.pause", "runs.cancel"}:
        return public_run(workbench.control(args.run_id, name.split(".")[1]))
    if name == "media.sample":
        rows = []
        for ep in sorted(set(run["completed"] + run.get("prepared", []))):
            rows += store.get("evidence", f"{run['id']}:{ep}")["items"]
        return {"coverage": "sampled", "items": rows}
    if name == "changes.undo":
        from .undo import propose

        return propose(workbench, args.changeset_id)
    if name == "changes.diff":
        return change
    if name == "changes.validate":
        return workbench.validate(change)
    if name == "changes.review":

        def decide(value):
            if value["revision"] != args.revision or value["status"] == "committed":
                raise Conflict("Draft revision changed")
            if any(i < 0 or i >= len(value["proposals"]) for i in args.indices):
                raise ValueError("Unknown suggestion index")
            value.setdefault("decisions", {}).update(
                {str(i): args.decision for i in args.indices}
            )
            value.update(status="draft", revision=value["revision"] + 1)

        return store.mutate("changes", args.changeset_id, decide)
    if name == "changes.edit":

        def edit(value):
            if value["revision"] != args.revision or value["status"] == "committed":
                raise Conflict("Draft revision changed")
            value.update(
                proposals=[p.model_dump() for p in args.proposals],
                decisions={},
                status="draft",
                revision=value["revision"] + 1,
            )
            report = workbench.validate(value)
            for row in report["quality"]:
                store.put("quality", f"{value['run_id']}:{row['episode']}", row)

        return store.mutate("changes", args.changeset_id, edit)
    if name == "changes.approve":
        report = workbench.validate(change)
        if report.get("pending_objects"):
            raise ValueError("Review all staged object masks before approval")

        def approve(value):
            if value["revision"] != args.revision or value["status"] == "committed":
                raise Conflict("Draft revision changed")
            value.setdefault("decisions", {}).update(
                {
                    str(i): value.get("decisions", {}).get(str(i), "accepted")
                    for i in range(len(value["proposals"]))
                }
            )
            value["status"] = "approved"
            value["provenance"]["reviewer_type"] = "human"
            value["provenance"]["reviewer"] = principal.id

        return store.mutate("changes", args.changeset_id, approve)
    if name == "changes.commit":
        if not key:
            raise ValueError("Commit requires an idempotency key")
        return workbench.commit(args.changeset_id, key, args.revision)
    if name == "view.seek":
        for ep in sorted(set(run["completed"] + run.get("prepared", []))):
            for row in store.get("evidence", f"{run['id']}:{ep}")["items"]:
                if row["id"] == args.evidence_id:
                    return {"repo_id": repo, **row}
        raise ValueError("Evidence not found in this run")
    if name == "export.plan":
        from .formats import EXPORTS

        if args.target not in EXPORTS:
            raise ValueError("Unknown export adapter")
        return EXPORTS[args.target].plan(run)
    raise ValueError("Capability not implemented")


def invoke(workbench, principal, name, arguments, key=None):
    """All channels share authorization, schema validation and core-owned audit."""
    import time

    from .grants import check_call
    from .runtime import new_id
    from .tracking import scope

    if name not in SPECS:
        raise ValueError("Unknown capability")
    schema, permission, _ = SPECS[name]
    principal.require(permission)
    args = schema.model_validate(arguments)
    if not principal.human and name == "runs.plan" and args.provider != "external":
        raise PermissionError(
            "External Pilot connections cannot dispatch API-model plans"
        )
    arguments = args.model_dump()
    run = scope(workbench.store, arguments)
    repo = arguments.get("repo_id") or (run["context"]["repo_id"] if run else None)
    principal.require(permission, repo)
    if not principal.human and name not in {
        "capabilities.list",
        "workspace.get_context",
    }:
        check_call(workbench.store, principal, run["id"] if run else None)
    noisy = name in {
        "runs.events",
        "runs.get",
        "capabilities.list",
        "objects.get",
        "objects.frame",
    }
    call_id = new_id()
    started = time.monotonic()

    def event(kind, **data):
        if run and not noisy:
            workbench.store.event(
                run["id"],
                kind,
                call_id=call_id,
                tool=name,
                principal=principal.id,
                source="core",
                **data,
            )

    parameters = {
        k: v
        for k, v in arguments.items()
        if k
        in {
            "run_id",
            "job_id",
            "changeset_id",
            "episode",
            "episode_index",
            "camera",
            "camera_key",
            "timestamp",
            "revision",
            "evidence_id",
            "pilot",
            "offset",
            "limit",
            "decision",
            "indices",
        }
    }
    if "proposals" in arguments:
        parameters["proposal_count"] = len(arguments["proposals"])
    event("action.started", parameters=parameters)
    try:
        value = _invoke(workbench, principal, name, arguments, key)
        if name == "runs.plan":
            run = workbench.store.get("runs", value["id"])
            event("action.started")
        event(
            "action.completed",
            elapsed_seconds=time.monotonic() - started,
            **(
                {
                    "evidence": {
                        k: value[k]
                        for k in ("episode_index", "timestamp", "camera_key")
                    }
                }
                if name == "view.seek"
                else {}
            ),
        )
        return value
    except Exception as exc:
        event(
            "action.failed",
            error_type=type(exc).__name__,
            elapsed_seconds=time.monotonic() - started,
        )
        raise
