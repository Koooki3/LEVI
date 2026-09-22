"""One capability dispatcher for REST, MCP and the Workbench UI."""

from typing import Any, Literal

from pydantic import Field

from .runtime import Workbench
from .schema import Budget, Contract, ModelOutput, Proposal, TaskContext
from .security import Principal
from .store import Conflict


class Empty(Contract):
    pass


class RunRef(Contract):
    run_id: str


class TeacherFeedback(RunRef):
    teaching_id: str
    revision: int = Field(ge=0)
    decision: Literal["accept", "revise", "reject"]
    note: str = Field(min_length=1, max_length=1000)
    output: ModelOutput | None = None


class RunList(Contract):
    repo_id: str | None = None
    include_finished: bool = False
    limit: int = Field(default=25, ge=1, le=200)


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
    # Up to 32 frames per page with images, 200 rows as text only.
    limit: int = Field(default=8, ge=1, le=200)
    # "mosaic" returns one labelled contact sheet of this page instead of one
    # image per frame: the same evidence, an order of magnitude fewer tokens.
    layout: str = Field(default="single", pattern="^(single|mosaic)$")
    # None: the dataset's published harness value (evidence.mosaic_tile_width).
    tile_width: int | None = Field(default=None, ge=96, le=640)
    # False: the ledger rows only (ids, times, frames), no picture at all --
    # for citing evidence already seen without paying for it again.
    images: bool = True


class AgentObject(Contract):
    evidence_id: str
    concept: str = Field(min_length=1, max_length=120)
    # Either name a candidate from objects.detect -- a dozen tokens -- or send
    # the outline directly.
    candidate_id: str | None = None
    bbox_xyxy: list[float] = Field(default_factory=list, max_length=4)
    polygon: list[list[float]] = Field(default_factory=list, max_length=200)
    category: str | None = None
    score: float = Field(default=0.6, ge=0, le=1)
    track_id: int | None = Field(default=None, ge=0)
    object_id: str | None = None
    visible: bool = True
    occluded: bool = False


class Detect(RunRef):
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    max_candidates: int = Field(default=12, ge=1, le=40)
    min_area_fraction: float = Field(default=0.002, gt=0, le=0.5)
    colours: int = Field(default=6, ge=2, le=12)
    overlay: bool = True


class ProposeObjects(RunRef):
    objects: list[AgentObject] = Field(min_length=1, max_length=400)
    inspected_episodes: list[int] = Field(min_length=1)
    # "agent" keeps the object_ids as sent; "overlap" lets LEVI link the same
    # object across annotated frames, which is what frame-by-frame outlining
    # cannot do for itself.
    track_by: Literal["agent", "overlap"] = "agent"


class UsageReport(RunRef):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    requests: int | None = Field(default=None, ge=0)
    seconds: float | None = Field(default=None, ge=0)
    model: str | None = None
    note: str = Field(default="", max_length=2000)


class Abandon(RunRef):
    reason: str = Field(default="", max_length=500)


class Reset(Contract):
    dataset: str
    apply: bool = False


class Cleanup(Contract):
    dataset: str | None = None
    older_than_days: int = Field(default=0, ge=0, le=3650)
    include_open_runs: bool = False
    apply: bool = False


class Estimate(Contract):
    run_id: str | None = None
    workflow: str | None = None
    episodes: int = Field(default=1, ge=1, le=10000)
    evidence_frames: int | None = Field(default=None, ge=1, le=100000)
    reads: Literal["mosaic", "single"] | None = None
    agent_key: str | None = None


class Refine(RunRef):
    episode: int = Field(ge=0)
    around_seconds: list[float] = Field(min_length=1, max_length=8)
    cameras: list[str] = Field(default_factory=list, max_length=8)


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


class DatasetRef(Contract):
    repo_id: str


class KnowledgeList(Contract):
    topic: Literal["annotation", "interpretation", "harness"] | None = None
    refresh: bool = False


class KnowledgeRef(Contract):
    candidate_id: str = Field(pattern=r"^candidate-\d{3}$")


class KnowledgePromote(KnowledgeRef):
    topic: Literal["annotation", "interpretation", "harness"] | None = None
    text: str | None = Field(default=None, max_length=600)


class QualityInspect(DatasetRef):
    checks: list[str] = Field(default_factory=list, max_length=20)
    max_episodes: int = Field(default=0, ge=0, le=100000)
    decode_video: bool = False


class MemoryQuery(DatasetRef):
    query: str = Field(default="", max_length=200)


class ImprovementRef(DatasetRef):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,80}$")


class Probe(Contract):
    run_id: str
    episode: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    note: str = Field(default="", max_length=300)


class ImprovementEvaluate(ImprovementRef):
    # Required by evaluators that measure against known events.
    probes: list[Probe] = Field(default_factory=list, max_length=50)


class ImprovementMove(ImprovementRef):
    to: Literal[
        "evaluating",
        "qualified",
        "awaiting_authorization",
        "published",
        "retained",
        "rolled_back",
        "rejected",
        "resolved",
    ]
    note: str = Field(default="", max_length=1000)


class ImprovementRevise(ImprovementRef):
    value: int
    note: str = Field(default="", max_length=1000)


class TaskRequest(Contract):
    text: str = Field(min_length=3, max_length=2000)
    provider: str = Field(min_length=1, max_length=64)
    supervision: Literal["none", "shadow", "supervised"] = "none"
    teacher_grant: str | None = Field(default=None, max_length=100)


class TaskRef(Contract):
    task_id: str = Field(pattern=r"^task-\d{8}T\d{4}(-\d+)?$")


class TaskFeedback(TaskRef):
    decision: Literal["accept", "revise", "reject"]
    note: str = Field(default="", max_length=2000)
    spec: dict[str, Any] | None = None


class EvidenceChanges(RunRef):
    episode: int = Field(ge=0)
    top_k: int | None = Field(default=None, ge=1, le=20)


from .objects import ObjectRequest

SPECS = {
    "quality.inspect": (
        QualityInspect,
        "draft",
        (
            "Check a dataset's structure, timing, media, actions and "
            "distribution; writes <dataset>/reports/quality-<hour>.json"
        ),
    ),
    "memory.get": (
        DatasetRef,
        "read",
        (
            "Read this dataset's verified local memory: committed annotations, "
            "recurring uncertainty, cost and published lessons"
        ),
    ),
    "tasks.interpret": (
        TaskRequest,
        "draft",
        (
            "Turn a natural-language request into a checked task spec with the "
            "local model; nothing runs until a person approves it"
        ),
    ),
    "tasks.get": (TaskRef, "read", "Read a natural-language task and its steps"),
    "tasks.feedback": (
        TaskFeedback,
        "draft",
        "Accept, correct or reject an interpretation; kept as a lesson",
    ),
    "tasks.approve": (TaskRef, "approve", "Human approval of a checked task spec"),
    "tasks.advance": (
        TaskRef,
        "execute",
        "Run the task's next automatic step; stops at every human gate",
    ),
    "gpu.status": (
        Empty,
        "read",
        (
            "What the GPU guardian decides now for local models, why, the "
            "expected wait, and the window it learned for each workload"
        ),
    ),
    "cost.profile": (
        DatasetRef,
        "read",
        (
            "Measured token and time cost on this dataset per agent (API, local "
            "VLM, external MCP), the latest breakdown and advice for the next run"
        ),
    ),
    "knowledge.list": (
        KnowledgeList,
        "read",
        "Built-in knowledge entries, and local notes that could join them",
    ),
    "knowledge.promote": (
        KnowledgePromote,
        "approve",
        "Add a local note to built-in knowledge (repository file); a person's call",
    ),
    "knowledge.reject": (
        KnowledgeRef,
        "approve",
        "Decline a knowledge candidate; a person's call",
    ),
    "memory.rebuild": (
        DatasetRef,
        "approve",
        "Recompute a dataset's memory from its closed runs; a person's call",
    ),
    "memory.search": (
        MemoryQuery,
        "read",
        "Search committed segments and lessons in the dataset's local memory",
    ),
    "improvements.list": (
        DatasetRef,
        "read",
        (
            "List harness improvement candidates for a dataset and the "
            "parameters a new task would run with"
        ),
    ),
    "improvements.get": (ImprovementRef, "read", "Read one improvement candidate"),
    "improvements.evaluate": (
        ImprovementEvaluate,
        "draft",
        "Let LEVI measure a candidate on stored evidence at named probe cases",
    ),
    "improvements.revise": (
        ImprovementRevise,
        "draft",
        (
            "Change a candidate's value once during evaluation; it must then "
            "pass a new evaluation"
        ),
    ),
    "improvements.transition": (
        ImprovementMove,
        "draft",
        (
            "Move a candidate through the state machine; publishing, "
            "retaining and rolling back need a person"
        ),
    ),
    "evidence.changes": (
        EvidenceChanges,
        "read",
        (
            "Rank an episode's coarse intervals by how much the picture "
            "changes; refine the top ones first"
        ),
    ),
    "supervision.pending": (
        RunRef,
        "read",
        "Read the assigned teacher's evidence and pending annotation phases",
    ),
    "supervision.feedback": (
        TeacherFeedback,
        "draft",
        "Accept, revise or reject a learner phase; never approve execution or commit",
    ),
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
        "Read a bounded page of exact evidence; layout='mosaic' returns one labelled sheet instead of one image per frame",
    ),
    "evidence.refine": (
        Refine,
        "draft",
        "Add bounded extra frames around candidate boundaries, within the approved window and frame cap",
    ),
    "runs.report_usage": (
        UsageReport,
        "draft",
        "Report this agent's own token/request use for the run; feeds cost estimates",
    ),
    "runs.abandon": (
        Abandon,
        "approve",
        "Close a run nobody will finish, so its evidence can be cleaned up",
    ),
    "workspace.reset": (
        Reset,
        "approve",
        "Remove one dataset's agent history: runs, records and published revisions",
    ),
    "workspace.clean": (
        Cleanup,
        "approve",
        "Remove regenerable run snapshots and evidence; keeps committed revisions",
    ),
    "plans.estimate": (
        Estimate,
        "read",
        "Estimate the agent tokens a scope will cost, from recorded runs of this agent",
    ),
    "runs.quality": (
        RunRef,
        "read",
        (
            "Read the run's annotation uncertainty and uncovered intervals per "
            "episode (not dataset quality: that is quality.inspect)"
        ),
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
    "objects.strategy": (
        RunRef,
        "read",
        "Recommend SAM3 or agent-authored object annotation for this machine and scope",
    ),
    "objects.detect": (
        Detect,
        "read",
        "Measure candidate object regions in evidence frames; no model, no GPU",
    ),
    "objects.propose": (
        ProposeObjects,
        "draft",
        "Stage agent-authored object outlines for the same human review as worker output",
    ),
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
    "runs.list": (
        RunList,
        "read",
        "List runs in scope and what each one is waiting for",
    ),
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
    "changes.rebase": (
        Review,
        "approve",
        "Move a staged draft onto the current published revision; clears approval",
    ),
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
    elif getattr(args, "run_id", None):
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
    if (
        run
        and run["context"].get("supervision", "none") != "none"
        and not principal.human
        and permission in {"draft", "execute"}
        and name != "supervision.feedback"
    ):
        raise PermissionError(
            "External agents may only submit teacher feedback for this supervised task"
        )
    if name == "quality.inspect":
        from levi.catalog import display_name, local_root
        from levi.harness.quality import digest, inspect

        root = local_root(args.repo_id)
        if root is None:
            raise ValueError("quality.inspect reads registered local datasets")
        return digest(
            inspect(
                store.state,
                display_name(args.repo_id, None),
                root,
                repo_id=args.repo_id,
                checks=args.checks,
                max_episodes=args.max_episodes,
                decode_video=args.decode_video,
            )
        )
    if name.startswith("tasks."):
        from levi.harness import tasking

        if name == "tasks.interpret":
            task = tasking.interpret(
                store, args.text, args.provider, principal=principal
            )
            task.update(supervision=args.supervision, teacher_grant=args.teacher_grant)
            return tasking.save(store, task)
        task = store.get("tasks", args.task_id)
        if (
            not principal.human
            and f"local/{task['dataset_key']}" not in principal.datasets
        ):
            raise PermissionError("Dataset is outside this principal's scope")
        if name == "tasks.get":
            return task
        if name == "tasks.feedback":
            return tasking.feedback(
                store,
                args.task_id,
                decision=args.decision,
                note=args.note,
                spec=args.spec,
                by=principal.id,
            )
        if name == "tasks.approve":
            return tasking.approve(store, args.task_id, by=principal.id)
        return tasking.advance(workbench, args.task_id, principal)
    if name == "gpu.status":
        from levi.inference.gpu import report

        return report()
    if name == "cost.profile":
        from levi.catalog import display_name
        from levi.harness.layout import memory_path, read_json

        remembered = read_json(
            memory_path(store.state, display_name(args.repo_id, None))
        )
        remembered = remembered or {}
        return {
            "profiles": remembered.get("cost_profiles", {}),
            "hints": remembered.get("cost_hints", []),
            "latest": remembered.get("cost_latest"),
            "note": "tokens.source says whether a figure was metered by LEVI, "
            "reported by the agent, or LEVI's measured lower bound.",
        }
    if name in {"memory.get", "memory.search", "memory.rebuild"}:
        from levi.catalog import display_name
        from levi.harness import memory

        key = display_name(args.repo_id, None)
        if name == "memory.rebuild":
            from levi.harness.layout import harness_lock

            with harness_lock(store.state, key):
                return memory.rebuild(store, key)
        if name == "memory.search":
            return memory.search(store.state, args.query, key)
        return memory.context(store.state, key) or {
            "episodes_annotated": 0,
            "note": "No committed work on this dataset yet; memory starts with "
            "the first committed task.",
        }
    if name.startswith("knowledge."):
        from levi.harness import knowledge

        if name == "knowledge.promote":
            return knowledge.promote(
                store.state, args.candidate_id, args.topic, args.text
            )
        if name == "knowledge.reject":
            return knowledge.reject(store.state, args.candidate_id)
        if args.refresh:
            knowledge.refresh(store.state)
        topics = [args.topic] if args.topic else list(knowledge.TOPICS)
        return {
            "built_in": {t: knowledge.load(t) for t in topics},
            "candidates": [
                c
                for c in knowledge.candidates(store.state)
                if not args.topic or c["topic"] == args.topic
            ],
        }
    if name.startswith("improvements."):
        from levi.catalog import display_name
        from levi.harness import improvements

        key = display_name(args.repo_id, None)
        if name == "improvements.list":
            return {
                "candidates": [
                    {
                        k: c[k]
                        for k in (
                            "slug",
                            "kind",
                            "title",
                            "state",
                            "target",
                            "updated_at",
                        )
                    }
                    | {"evidence_runs": [e["run_id"] for e in c["evidence"]]}
                    for c in improvements.listing(store.state, key)
                ],
                "next_task_runs_with": improvements.snapshot(store.state, key),
                "parameters": {
                    n: {k: v for k, v in spec.items() if k != "type"}
                    for n, spec in improvements.PARAMETERS.items()
                },
            }
        if name == "improvements.get":
            return improvements.load(store.state, key, args.slug)
        from levi.harness.layout import harness_lock

        if name == "improvements.revise":
            with harness_lock(store.state, key):
                return improvements.revise(
                    store.state,
                    key,
                    args.slug,
                    args.value,
                    by=principal.id,
                    note=args.note,
                )
        if name == "improvements.evaluate":
            from levi.harness.evaluation import evaluate

            with harness_lock(store.state, key):
                candidate = improvements.load(store.state, key, args.slug)
                if candidate["state"] != "evaluating":
                    raise Conflict("Move the candidate to evaluating first")
                result = evaluate(
                    store, candidate, [p.model_dump() for p in args.probes]
                )
                result["by"] = principal.id
                candidate["evaluation"] = result
                improvements.write_json(
                    improvements.path(store.state, key, args.slug), candidate
                )
                return result
        with harness_lock(store.state, key):
            return improvements.transition(
                store.state,
                key,
                args.slug,
                args.to,
                by=principal.id,
                human=principal.human,
                note=args.note,
            )
    if name == "evidence.changes":
        from .observations import changes

        return changes(workbench, run, args.episode, args.top_k)
    if name == "supervision.pending":
        from .supervision import pending

        return pending(workbench, args.run_id, principal)
    if name == "supervision.feedback":
        from .supervision import feedback

        return feedback(
            workbench,
            args.run_id,
            principal,
            args.teaching_id,
            args.revision,
            args.decision,
            args.note,
            args.output,
        )
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
        from .observations import recall, text_page

        if not args.images:
            return text_page(workbench, run, args.episode, args.offset, args.limit)
        if args.limit > 32:
            raise ValueError("A page with images holds at most 32 frames")

        return recall(
            workbench,
            run,
            args.episode,
            args.offset,
            args.limit,
            args.layout,
            args.tile_width
            or (run.get("harness") or {})
            .get("parameters", {})
            .get("evidence.mosaic_tile_width")
            or 320,
        )
    if name == "evidence.refine":
        from .observations import refine

        return refine(workbench, run, args.episode, args.around_seconds, args.cameras)
    if name == "runs.report_usage":
        from .usage import record

        sample = record(store, run, args.model_dump())
        store.event(run["id"], "usage_reported", tokens=sample.get("tokens"))
        return sample
    if name == "runs.list":
        from .tracking import waiting_for

        rows = []
        # Newest first by creation time; the store's order is not chronological.
        for record in sorted(
            store.list("runs"),
            key=lambda item: item.get("created_at", 0),
            reverse=True,
        ):
            dataset = record["context"]["repo_id"]
            if args.repo_id and dataset != args.repo_id:
                continue
            if not principal.human and dataset not in principal.datasets:
                continue
            state = waiting_for(store, record)
            if state["finished"] and not args.include_finished:
                continue
            rows.append(
                {
                    "run_id": record["id"],
                    "dataset": dataset,
                    "workflow": record["context"]["workflow"]["kind"],
                    "episodes": record["context"]["episodes"],
                    "cameras": record["context"]["cameras"],
                    "instruction": record["context"]["instruction"],
                    "provider": record["provider_config"]["name"],
                    "status": record["status"],
                    "created_at": record["created_at"],
                    "completed": record["completed"],
                    **state,
                }
            )
            if len(rows) >= args.limit:
                break
        return {
            "runs": rows,
            "reading": (
                "waiting_for says whose turn it is. An agent acts on "
                "agent_prepare and agent_propose; everything else needs a "
                "human in LEVI. evidence_ready false means runs.prepare has "
                "to rebuild the frames before they can be read."
            ),
        }
    if name == "runs.abandon":
        if run["changes"]:
            change = store.get("changes", run["changes"])
            if change["status"] == "committed":
                raise Conflict("This run is committed; it cannot be abandoned")
            if change["status"] != "draft" or change.get("approval"):
                raise Conflict(
                    "Reject or commit the staged changes before abandoning the run"
                )
        store.mutate(
            "runs",
            args.run_id,
            lambda record: record.update(
                status="cancelled",
                reason=args.reason or "Abandoned from the human terminal",
            ),
        )
        store.event(args.run_id, "run_abandoned", reason=args.reason)
        return {
            "run_id": args.run_id,
            "status": "cancelled",
            "note": "Staged drafts are kept; workspace.clean can now free its evidence",
        }
    if name == "workspace.reset":
        from .housekeeping import reset as reset_dataset

        return reset_dataset(
            store, workbench.store.run_dir, args.dataset, apply=args.apply
        )
    if name == "workspace.clean":
        from .housekeeping import apply as clean_apply
        from .housekeeping import plan as clean_plan

        action = clean_apply if args.apply else clean_plan
        return action(
            store,
            workbench.store.run_dir,
            dataset=args.dataset,
            older_than_days=args.older_than_days,
            include_drafts=args.include_open_runs,
        )
    if name == "plans.estimate":
        from .usage import estimate

        target = store.get("runs", args.run_id) if args.run_id else None
        value = estimate(
            store,
            target,
            key=args.agent_key,
            workflow=args.workflow,
            episodes=args.episodes,
            frames=args.evidence_frames,
            reads=args.reads,
            principal=principal,
        )
        # This dataset's own measured cost for the same agent, when there is
        # one: a local figure beats the cross-dataset calibration.
        if target:
            from levi.harness.layout import memory_path, read_json

            from .usage import agent_key

            profile = (
                (read_json(memory_path(store.state, target["dataset_key"])) or {})
                .get("cost_profiles", {})
                .get(agent_key(target))
            )
            if profile and profile.get("median", {}).get("tokens_per_episode"):
                episodes = len(target["context"]["episodes"])
                value["local"] = {
                    "basis": f"{len(profile['runs'])} earlier run(s) of this agent "
                    "on this dataset",
                    "tokens_per_episode": profile["median"]["tokens_per_episode"],
                    "seconds_per_episode": profile["median"]["seconds_per_episode"],
                    "tokens": int(profile["median"]["tokens_per_episode"] * episodes),
                    "seconds": int(
                        (profile["median"]["seconds_per_episode"] or 0) * episodes
                    )
                    or None,
                }
        return value
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
    if name in {"annotations.propose_segments", "annotations.propose_events"}:
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
        # A receipt, not the ChangeSet: echoing every staged proposal back made
        # each call cost more than the last (quadratic over a run). The full
        # draft is one changes.diff away.
        staged = {}
        for proposal in change["proposals"]:
            key = str(proposal["episode_index"])
            staged[key] = staged.get(key, 0) + 1
        return {
            "id": change["id"],
            "run_id": change["run_id"],
            "revision": change["revision"],
            "status": change["status"],
            "accepted": {
                str(ep): staged.get(str(ep), 0) for ep in args.inspected_episodes
            },
            "staged_proposals": len(change["proposals"]),
            "staged_episodes": sorted(int(k) for k in staged),
            "remaining_episodes": sorted(
                set(run["context"]["episodes"])
                - set(run["completed"])
                - set(args.inspected_episodes)
            ),
            "full_draft": "changes.diff",
        }
    if name == "objects.inspect":
        from .objects import inspect_result

        return inspect_result(workbench, job, args.offset)
    if name == "objects.edit":
        from .objects import edit_result

        return edit_result(workbench, job, args.edit)
    if name == "objects.status":
        from .objects import readiness

        return readiness()
    if name == "objects.strategy":
        from .objects import readiness

        state = readiness()
        frames = sum(
            len(store.get("evidence", f"{run['id']}:{ep}")["items"])
            for ep in sorted(set(run.get("prepared", []) + run.get("completed", [])))
        )
        return {
            **state["strategy"],
            "readiness": {k: v for k, v in state.items() if k != "strategy"},
            "prepared_evidence_frames": frames,
            "note": "Advisory: both paths stage suggestions for the same human review",
        }
    if name == "objects.detect":
        from .objects import detect

        return detect(workbench, run, args)
    if name == "objects.propose":
        from .objects import propose

        return propose(
            workbench,
            run,
            args.objects,
            args.inspected_episodes,
            args.track_by,
        )
    if name == "objects.plan":
        from .objects import plan

        return plan(workbench, args)
    if name == "objects.run":
        from .objects import launch

        return launch(workbench, args.job_id)
    if name == "objects.get":
        plan = job.get("plan") or {}
        return {
            **{k: v for k, v in job.items() if k not in {"plan", "dataset_root"}},
            # Enough scope for a client to review or correct without reading
            # the frozen plan document itself.
            "scope": {
                "episodes": plan.get("episode_indices", []),
                "cameras": plan.get("camera_keys", []),
                "prompts": plan.get("prompts", []),
                "provider": plan.get("provider"),
            },
        }
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
        from levi.harness.knowledge import texts as knowledge_texts

        ids = [r["id"] for r in datasets().values()] + DEMOS
        reachable = [r for r in ids if principal.human or r in principal.datasets]
        # An agent's first call should leave it able to work. Everything it
        # needs to know that is not discoverable from a tool schema lives here:
        # what it may do, what only a person may do, what is already waiting,
        # and the one habit that decides what the job costs.
        from .tracking import waiting_for

        pending = []
        for record in sorted(
            store.list("runs"),
            key=lambda item: item.get("created_at", 0),
            reverse=True,
        ):
            dataset = record["context"]["repo_id"]
            if not principal.human and dataset not in principal.datasets:
                continue
            state = waiting_for(store, record)
            if state["waiting_for"].startswith("agent_"):
                pending.append(
                    {
                        "run_id": record["id"],
                        "dataset": dataset,
                        "workflow": record["context"]["workflow"]["kind"],
                        "episodes": record["context"]["episodes"],
                        "waiting_for": state["waiting_for"],
                        "evidence_ready": state["evidence_ready"],
                    }
                )
        return {
            "datasets": reachable,
            "mode": "draft",
            "you_may": [
                "read datasets in scope, plan runs and prepare evidence",
                "propose annotations and object masks for human review",
                "run the local SAM3 worker when this machine can host it",
            ],
            "only_a_person_may": [
                "approve a plan, accept a pilot, commit or undo annotations",
                "clean the workspace or abandon a run",
            ],
            "start_here": [
                (
                    "runs.list — the runs in scope and whose turn each one is; "
                    "'agent_prepare' and 'agent_propose' are yours"
                ),
                (
                    "runs.plan, then wait for a person to approve it, when "
                    "nothing is waiting for you"
                ),
                "runs.prepare — build the evidence for the approved scope",
                (
                    "evidence.read with layout='mosaic' — one labelled contact "
                    "sheet per page instead of one image per frame"
                ),
                "evidence.refine — extra frames only where a boundary is unclear",
                (
                    "annotations.propose_segments / objects.propose — cite the "
                    "evidence ids you actually read"
                ),
                (
                    "runs.report_usage — report your own token use when you "
                    "stop; LEVI cannot see it, and the next estimate depends on it"
                ),
            ],
            "waiting_for_you": pending,
            # Dataset-agnostic rules every annotator in LEVI follows.
            "annotation_rules": knowledge_texts("annotation"),
            "skills": (
                "Read levi://skills/levi-overview first, then the one for your "
                "workflow. They are short and they are the contract."
            ),
            "cost": (
                "Evidence dominates what a task costs. Read pages as mosaics, "
                "refine only unclear boundaries, and call plans.estimate "
                "before committing to a large scope."
            ),
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
        return public_run(workbench.plan(args, principal))
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
    if name == "changes.rebase":
        return workbench.rebase(args.changeset_id, args.revision)
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


def _response_size(name, value):
    """What this answer costs the agent that reads it, measured here.

    Text is the JSON the bridge sends; a mosaic sheet is sent as one image,
    counted by its pixel area.
    """
    import json

    try:
        size = {"response_bytes": len(json.dumps(value, ensure_ascii=False))}
    except (TypeError, ValueError):
        return {}
    sheet = value.get("mosaic") if isinstance(value, dict) else None
    if isinstance(sheet, dict) and sheet.get("width") and sheet.get("height"):
        size.update(images=1, image_pixels=sheet["width"] * sheet["height"])
    elif (
        name in {"media.sample", "evidence.read"}
        and value.get("images") is not False
        and isinstance(value.get("items"), list)
    ):
        # Single-frame reads send every available frame as its own image.
        frames = [
            row
            for row in value["items"]
            if isinstance(row, dict)
            and row.get("artifact")
            and row.get("image_available", True)
        ]
        if frames:
            size.update(
                images=len(frames),
                image_pixels=sum(
                    int(r["source_size"][0]) * int(r["source_size"][1])
                    for r in frames
                    if r.get("source_size")
                ),
            )
    return size


def invoke(workbench, principal, name, arguments, key=None):
    """All channels share authorization, schema validation and core-owned audit."""
    import time

    from . import activity
    from .grants import check_call
    from .tracking import scope

    if name not in SPECS:
        raise ValueError("Unknown capability")
    schema, permission, _ = SPECS[name]
    # Authorization, validation and scope resolution can all refuse the call.
    # Those refusals are exactly what someone watching the panel needs to see,
    # so they are recorded here rather than only inside the dispatch below.
    try:
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
    except Exception as exc:
        activity.record(
            workbench.store,
            name=name,
            principal=principal,
            status="failed",
            detail={"stage": "authorization"},
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
        raise
    noisy = name in {
        "runs.events",
        "runs.get",
        "capabilities.list",
        "objects.get",
        "objects.frame",
    }
    call_id = None
    started = time.monotonic()

    def event(kind, **data):
        nonlocal call_id
        if run and not noisy:
            from .store import NEW_CALL

            row = workbench.store.event(
                run["id"],
                kind,
                call_id=call_id or NEW_CALL,
                tool=name,
                principal=principal.id,
                channel=activity.channel(principal),
                source="core",
                **data,
            )
            # The journal sequence of the call's first event names the call:
            # unique across processes, unlike a minute-precision timestamp,
            # which every call in the same minute used to share.
            call_id = row["call_id"]

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
            "dataset",
            "apply",
            "workflow",
            "layout",
            "tile_width",
            "images",
        }
    }
    if "proposals" in arguments:
        parameters["proposal_count"] = len(arguments["proposals"])
    event("action.started", parameters=parameters)
    activity.record(
        workbench.store,
        name=name,
        principal=principal,
        status="started",
        dataset=repo,
        run=run,
        detail=parameters,
    )
    try:
        value = _invoke(workbench, principal, name, arguments, key)
        if name == "runs.plan":
            run = workbench.store.get("runs", value["id"])
            event("action.started")
        event(
            "action.completed",
            elapsed_seconds=time.monotonic() - started,
            **_response_size(name, value),
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
        activity.record(
            workbench.store,
            name=name,
            principal=principal,
            status="completed",
            dataset=repo,
            run=run,
            detail=parameters,
            elapsed=time.monotonic() - started,
            value=value,
        )
        if run:
            from levi.harness.closure import close_if_finished, refresh

            if not close_if_finished(workbench.store, run["id"]) and name == (
                "runs.report_usage"
            ):
                refresh(workbench.store, run["id"])
        return value
    except Exception as exc:
        event(
            "action.failed",
            error_type=type(exc).__name__,
            elapsed_seconds=time.monotonic() - started,
        )
        activity.record(
            workbench.store,
            name=name,
            principal=principal,
            status="failed",
            dataset=repo,
            run=run,
            detail=parameters,
            elapsed=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
        raise
