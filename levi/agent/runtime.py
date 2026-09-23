"""Durable, bounded single-orchestrator execution over existing LEVI services."""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import suppress
from contextvars import copy_context
from datetime import UTC, datetime

from . import media
from .formats import ANNOTATIONS, DATASETS
from .providers import RoutedProvider
from .schema import ChangeSet, ModelOutput, ProviderConfig, RunStatus, TaskContext
from .store import Conflict, Store, annotation_digest, digest, file_hash, pin

_ACTIVE = {}
_LOCK = threading.Lock()


def new_id(taken=()):
    """A ``YYYYmmddTHHMM`` id, with ``-2``, ``-3``… only on a real clash.

    Precision stops at the minute on purpose: finer digits read as an opaque
    suffix, and several ids in one minute are readable when they are spelled
    out as -2, -3 rather than as a timestamp tail nobody can parse.
    """
    base = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    if base not in taken:
        return base
    for attempt in range(2, 1000):
        candidate = f"{base}-{attempt}"
        if candidate not in taken:
            return candidate
    raise RuntimeError("Could not allocate an unused identifier")


def snap_to_episode(output, summary):
    """Timestamps are float32 in the data and decimals in an answer: an
    interval written to end at 11.2 s when the last frame is 11.1999998 s
    ends at the last frame. Only a sub-millisecond overshoot is snapped."""
    end = summary.get("end")
    if end is None:
        return output
    proposals = [
        p.model_copy(update={"end": end})
        if p.end is not None and end < p.end <= end + 1e-3
        else p
        for p in output.proposals
    ]
    return output.model_copy(update={"proposals": proposals})


def anchor_to_draft(output, summary):
    """A refinement only sees frames within ``boundary_window_seconds`` of
    each draft boundary, so a boundary it moves further than that has no
    evidence behind it: the draft's boundary stands. Applies when the answer
    keeps the draft's intervals one for one (LEVI pins them)."""
    from levi.inference.provider import pinned_draft

    draft = pinned_draft(summary)
    window = (summary.get("workflow") or {}).get("boundary_window_seconds")
    if not draft or not window or len(draft) != len(output.proposals):
        return output

    def held(new, old):
        if new is None or old is None:
            return new
        return new if abs(new - old) <= window else old

    starts = [
        held(new.start, old["start"])
        for new, old in zip(output.proposals, draft, strict=True)
    ]
    ends = [
        held(new.end, old.get("end"))
        for new, old in zip(output.proposals, draft, strict=True)
    ]
    # Intervals keep their order: a start that jumps before the previous
    # one's falls back, with the previous one's, to the draft.
    for i in range(1, len(starts)):
        if starts[i] < starts[i - 1]:
            starts[i - 1], starts[i] = draft[i - 1]["start"], draft[i]["start"]
    # Intervals that met in the draft share one boundary: the later start.
    for i in range(len(draft) - 1):
        joined = draft[i].get("end")
        if joined is not None and abs(joined - draft[i + 1]["start"]) < 1e-6:
            ends[i] = starts[i + 1]
    proposals = []
    for new, old, start, end in zip(output.proposals, draft, starts, ends, strict=True):
        if end is not None and end <= start:
            start, end = old["start"], old.get("end")
        proposals.append(new.model_copy(update={"start": start, "end": end}))
    return output.model_copy(update={"proposals": proposals})


class Workbench:
    def __init__(self, state, provider=None):
        self.store = Store(state)
        self.provider = provider or RoutedProvider()

    def plan(self, context: TaskContext, principal=None):
        from .supervision import require_teacher

        if context.supervision != "none" and context.provider in {
            "external",
            "local-tools",
        }:
            raise ValueError("Teacher supervision requires a configured learner model")
        require_teacher(self.store, context)
        if context.provider in {"external", "local-tools"}:
            config = None
            if (
                context.provider == "local-tools"
                and context.workflow["kind"] != "objects"
            ):
                raise ValueError(
                    "Local vision tools require an object annotation workflow"
                )
            if (
                context.cameras
                and not context.allow_media_egress
                and context.workflow["kind"] != "objects"
            ):
                raise ValueError("External media access needs explicit consent")
        else:
            config = ProviderConfig.model_validate(
                self.store.get("providers", context.provider)
            )
            if not config.enabled:
                raise ValueError("Provider is disconnected")
            if config.kind == "ollama":
                if not config.structured_output or not config.model_digest:
                    raise ValueError(
                        "Inspect and bind an installed Ollama model digest and enable structured output before planning"
                    )
            elif not config.tools:
                raise ValueError("Provider must declare structured tool-call support")
            if context.cameras and (
                not config.vision or not context.allow_media_egress
            ):
                raise ValueError(
                    "Vision capability and explicit media egress consent are required"
                )
        context = DATASETS[context.dataset_adapter].pin(context)
        state, files = DATASETS[context.dataset_adapter].inspect(context)
        if sum(files.values()) > context.budget.max_snapshot_bytes:
            raise ValueError(
                "Snapshot exceeds configured budget; select fewer episodes/cameras"
            )
        # dataset (the parent directory) + task kind + timestamp, so a run
        # directory says what it is without opening it. Never a digest.
        # The kind prefixes the id, so compare against the timestamp part or
        # the check never matches and a second run overwrites the first.
        stamps = {existing.split("-", 1)[-1] for existing in self.store.ids("runs")}
        id = f"{context.workflow['kind']}-{new_id(stamps)}"
        run = {
            "id": id,
            "status": RunStatus.PLANNED,
            "created_at": time.time(),
            "context": context.model_dump(),
            "provider_config": config.model_dump()
            if config
            else {"kind": context.provider, "name": context.provider},
            # Who asked for this run, so its cost can later be attributed to the
            # right agent rather than averaged over every agent that ever ran.
            "principal": getattr(principal, "id", None),
            "dataset_key": state.display_slug,
            "base_revision": self.store.head(state.display_slug),
            "base_content": annotation_digest(self.store.state, state.display_slug),
            "files": files,
            "snapshot_bytes": sum(files.values()),
            "completed": [],
            "failed": [],
            "planned_hashes": {
                rel: __import__("levi.agent.store", fromlist=["file_hash"]).file_hash(
                    state.root / rel
                )
                for rel in files
                if (state.root / rel).is_file()
            },
            "requests": 0,
            "tokens": 0,
            "reserved_tokens": 0,
            "elapsed_seconds": 0,
            "control": None,
            "reason": None,
            "coverage": "sampled",
            "changes": None,
        }
        from levi.harness import improvements, memory

        from .planning import attach

        # Frozen here: a candidate published while this run works affects the
        # next run, never this one.
        run["harness"] = {
            **improvements.snapshot(self.store.state, run["dataset_key"]),
            "memory": memory.context(self.store.state, run["dataset_key"]),
            # Teacher notes written after this instant reach the next task.
            "frozen_at": time.time(),
        }
        attach(run)
        self.store.put("runs", id, run)
        self.store.event(
            id, "planned", episodes=context.episodes, snapshot_bytes=sum(files.values())
        )
        return run

    def control(self, id, action):
        def update(run):
            if run["status"] in {RunStatus.SUCCEEDED, RunStatus.CANCELLED}:
                raise ValueError("Terminal run cannot be changed")
            if action not in {"pause", "cancel"}:
                raise ValueError("Unknown run control")
            run["control"] = action
            if run["status"] not in {RunStatus.RUNNING, RunStatus.QUEUED}:
                run["status"] = (
                    RunStatus.PAUSED if action == "pause" else RunStatus.CANCELLED
                )
            run["reason"] = (
                "Requested; an active operation stops at its next safe boundary"
            )

        run = self.store.mutate("runs", id, update)
        # A model request can run for minutes; the GPU should not keep
        # computing an answer nobody will use.
        from levi.inference.transport import abort

        if abort(id):
            self.store.event(id, "model_request_stopped")
        if action == "cancel":
            from .objects import cancel

            cancel(self, id)
        self.store.event(id, action + "_requested")
        return run

    def launch(self, id, *, pilot=True):
        from .planning import require

        require(self, self.store.get("runs", id), bulk=not pilot)
        owner = uuid.uuid4().hex
        if not self.store.claim(id, owner):
            raise Conflict("This run already has an active executor")
        try:

            def queued(run):
                if run["context"]["provider"] == "external":
                    raise ValueError(
                        "External agents prepare evidence and submit suggestions through MCP"
                    )
                if run.get("changes"):
                    draft = self.store.get("changes", run["changes"])
                    if draft["status"] == "approved":
                        raise Conflict(
                            "Approved draft must be committed before starting a new run"
                        )
                if run["status"] in {
                    RunStatus.SUCCEEDED,
                    RunStatus.PARTIAL,
                    RunStatus.CANCELLED,
                    RunStatus.RUNNING,
                }:
                    raise Conflict("Run cannot be started in its current state")
                run.update(status=RunStatus.QUEUED, control=None, reason=None)

            self.store.mutate("runs", id, queued)
        except BaseException:
            self.store.release(id, owner)
            raise
        ctx = copy_context()

        def work():
            from levi.inference.transport import requests_of

            # Pausing or cancelling the run cuts its in-flight model request.
            with requests_of(id):
                self.execute(id, owner, pilot=pilot)

        thread = threading.Thread(target=lambda: ctx.run(work), daemon=True)
        with _LOCK:
            _ACTIVE[id] = thread
        thread.start()
        return self.store.get("runs", id)

    def execute(self, id, owner, *, pilot=True):
        stopped = threading.Event()
        lease_lost = threading.Event()

        def heartbeat():
            while not stopped.wait(15):
                try:
                    if not self.store.claim(id, owner):
                        lease_lost.set()
                        return
                except sqlite3.OperationalError:
                    # Transient disk/DB contention is reconciled before the next side effect.
                    continue

        ticker = threading.Thread(target=heartbeat, daemon=True)
        ticker.start()
        started = time.monotonic()
        try:
            from .planning import require

            require(self, self.store.get("runs", id), bulk=not pilot)
            self.store.mutate(
                "runs", id, lambda run: run.update(status=RunStatus.RUNNING)
            )
            run = self.store.get("runs", id)
            context = TaskContext.model_validate(run["context"])
            directory = self.store.run_dir(id)
            directory.mkdir(parents=True, exist_ok=True)
            if "manifest" not in run or not (directory / "input").is_dir():
                # Incomplete snapshot is never reused after interruption, and a
                # snapshot removed by workspace.clean is taken again from the
                # source -- which is what makes that cleanup non-destructive.
                import shutil

                shutil.rmtree(directory / "input", ignore_errors=True)
                manifest = DATASETS[context.dataset_adapter].snapshot(
                    context,
                    directory / "input",
                    run["files"],
                    run.get("planned_hashes"),
                )
                if (
                    run.get("manifest")
                    and manifest["sha256"] != run["manifest"]["sha256"]
                ):
                    raise ValueError(
                        "The source no longer matches this run's cleaned-up "
                        "snapshot; create a new run rather than annotating "
                        "different data under an approved plan"
                    )
                self.store.mutate("runs", id, lambda r: r.update(manifest=manifest))
            config = ProviderConfig.model_validate(run["provider_config"])
            remaining = [ep for ep in context.episodes if ep not in run["completed"]]
            if pilot:
                remaining = [
                    ep for ep in remaining if ep == run["plan"]["pilot_episode"]
                ]
            for episode in remaining:
                run = self.store.get("runs", id)
                if lease_lost.is_set() or not self.store.claim(id, owner):
                    raise ValueError(
                        "Execution lease lost; resume after reconciliation"
                    )
                current_config = ProviderConfig.model_validate(
                    self.store.get("providers", config.name)
                )
                if current_config.model_dump(exclude={"enabled"}) != config.model_dump(
                    exclude={"enabled"}
                ):
                    raise ValueError(
                        "Provider configuration changed; create a new run before sending evidence"
                    )
                if not current_config.enabled:
                    raise ValueError(
                        "Provider was disconnected; reconnect before resuming"
                    )
                if run["control"]:
                    self.store.mutate(
                        "runs",
                        id,
                        lambda r: r.update(
                            status=RunStatus.CANCELLED
                            if r["control"] == "cancel"
                            else RunStatus.PAUSED
                        ),
                    )
                    return
                if (
                    time.monotonic() - started + run["elapsed_seconds"]
                    >= context.budget.max_seconds
                ):
                    raise ValueError("Wall-time budget exhausted")
                # model_step enforces fresh-call budgets after checking its
                # persisted cache. A teacher-approved cached phase can resume
                # even when the preceding request consumed the final reservation.
                from . import observations

                summary, evidence = observations.observe(
                    context, directory / "input", episode, directory / "evidence"
                )
                summary["workflow"] = context.workflow
                observations.persist(self, id, episode, summary, evidence)
                output, usage = self.model_step(
                    id, config, context, summary, evidence, "coarse", started
                )
                if context.workflow["kind"] == "temporal":
                    # The model's own boundaries, plus the published harness
                    # windows where the picture changed most -- the same
                    # learned policy an external agent gets from refine_first.
                    # ...all within what the model's context window holds,
                    # measured from the coarse call it just made.
                    from levi.inference import request_cost

                    costs = request_cost.fitted(config)
                    if costs is None and config.kind == "ollama":
                        costs, spent = request_cost.calibrate(
                            config,
                            json.dumps(summary, ensure_ascii=False)[:4000],
                            [
                                directory / "evidence" / row["artifact"]
                                for row in evidence
                                if row.get("artifact")
                            ],
                        )
                        self.store.mutate(
                            "runs",
                            id,
                            lambda r, spent=spent: r.update(
                                requests=r["requests"] + 2,
                                tokens=r["tokens"] + spent,
                            ),
                        )
                        self.store.event(
                            id, "request_cost_calibrated", tokens=spent, fit=costs
                        )
                    limit = observations.image_limit(
                        config,
                        usage,
                        evidence,
                        output.model_dump(),
                        sum(map(len, observations.skills(context).values()))
                        + len(json.dumps(summary, ensure_ascii=False)),
                        costs,
                    )
                    batches = observations.plan_refinement(
                        self,
                        self.store.get("runs", id),
                        episode,
                        output.proposals,
                        limit,
                        len(evidence),
                    )
                    finest = observations.refine_spacings(context)[0]
                    for number, batch in enumerate(batches, 1):
                        if batch.windows:
                            self.store.event(
                                id,
                                "harness_refinement",
                                episode=episode,
                                around=batch.windows,
                            )
                        if batch.spacing != finest or len(batches) > 1:
                            self.store.event(
                                id,
                                "refinement_coarsened",
                                episode=episode,
                                spacing_seconds=batch.spacing,
                                image_limit=limit,
                                batch=number,
                                batches=len(batches),
                            )
                        refined_summary, dense = observations.observe(
                            context,
                            directory / "input",
                            episode,
                            directory / "evidence",
                            batch.boundaries,
                            batch.spacing,
                        )
                        refined_summary.update(
                            workflow=context.workflow,
                            candidate_draft=output.model_dump(),
                            phase="boundary_refinement",
                        )
                        if batch.start is not None:
                            count = len(batch.boundaries)
                            refined_summary["refine_only"] = {
                                "start": batch.start,
                                "end": min(batch.end, refined_summary["end"]),
                                "proposals": count,
                                "instruction": f"Return all {count} draft "
                                "proposals that start in [start, end), each with "
                                "refined boundaries and outcome, and nothing "
                                "else; the rest of the draft is refined "
                                "separately.",
                            }
                        evidence = observations.persist(
                            self, id, episode, summary, dense
                        )
                        # Only the requested window images go to refinement;
                        # old evidence remains retrievable.
                        part, usage = self.model_step(
                            id,
                            config,
                            context,
                            refined_summary,
                            dense,
                            "refine" if len(batches) == 1 else f"refine-{number}",
                            started,
                        )
                        output = (
                            part
                            if batch.start is None
                            else observations.merge(output, part, batch)
                        )
                quality = observations.quality(
                    context, output.proposals, evidence, summary
                )
                self.store.put("quality", f"{id}:{episode}", quality)
                if lease_lost.is_set() or not self.store.claim(id, owner):
                    raise ValueError("Execution lease lost; result remains unpublished")
                if self.store.get("runs", id)["control"] == "cancel":
                    self.store.mutate(
                        "runs", id, lambda r: r.update(status=RunStatus.CANCELLED)
                    )
                    return
                self.store.put(
                    "shards",
                    f"{id}:{episode}",
                    {"output": output.model_dump(), "usage": usage},
                )
                self.store.mutate(
                    "runs",
                    id,
                    lambda r, episode=episode: r.update(
                        completed=sorted(set(r["completed"] + [episode]))
                    ),
                )
                self.store.event(
                    id,
                    "shard_completed",
                    episode=episode,
                    evidence_ids=[e["id"] for e in evidence],
                )
            self.prepare_changes(id)
            self.store.mutate("runs", id, lambda r: r.update(status=RunStatus.WAITING))
            # A model run is billed through LEVI, so its cost is recorded here
            # rather than waited for from the agent.
            from .usage import measure_run

            if sample := measure_run(self.store, id):
                self.store.event(id, "usage_measured", tokens=sample["tokens"])
            self.store.event(id, "waiting_for_review", coverage="sampled")
        except Exception as exc:  # noqa: BLE001 - worker boundary; no raw provider errors persisted
            # Never persist provider exception messages: HTTP libraries may include
            # Authorization headers or untrusted response bodies.
            from pydantic import ValidationError

            from levi.inference.gpu import GpuBusy
            from levi.inference.ollama import OllamaError
            from levi.inference.provider import InvalidAnswer, describe

            from .observations import ContextOverflow
            from .supervision import AwaitingTeacher

            if isinstance(exc, ValidationError):
                # The model's answer did not fit the schema: say where, so a
                # person or a teacher can see what it got wrong. These messages
                # describe model output, never credentials or HTTP bodies.
                reason = describe(exc)
            else:
                reason = (
                    str(exc)
                    if type(exc) is ValueError
                    or isinstance(
                        exc,
                        (
                            Conflict,
                            AwaitingTeacher,
                            GpuBusy,
                            OllamaError,
                            ContextOverflow,
                            InvalidAnswer,
                        ),
                    )
                    else f"Execution blocked ({type(exc).__name__}); inspect provider configuration"
                )
            blocked_by = "gpu" if isinstance(exc, GpuBusy) else None
            self.store.mutate(
                "runs",
                id,
                lambda r: r.update(
                    status=(
                        RunStatus.CANCELLED
                        if r.get("control") == "cancel"
                        else RunStatus.PAUSED
                        if r.get("control") == "pause"
                        else RunStatus.BLOCKED
                    ),
                    reason=reason,
                    # The guardian resumes these by itself once the GPU clears.
                    blocked_by=blocked_by,
                ),
            )
            self.store.event(id, "blocked", reason=reason)
        finally:
            self.store.mutate(
                "runs",
                id,
                lambda r: r.update(
                    elapsed_seconds=r["elapsed_seconds"] + time.monotonic() - started
                ),
            )
            stopped.set()
            ticker.join(timeout=35)
            self.store.release(id, owner)
            with _LOCK:
                _ACTIVE.pop(id, None)
            from levi.harness.closure import close_if_finished

            close_if_finished(self.store, id)

    def model_step(self, id, config, context, summary, evidence, phase, started):
        context = TaskContext.model_validate(context.model_dump())
        from .observations import skills
        from .planning import require
        from .supervision import gate, require_teacher

        require_teacher(self.store, context, id)
        run = self.store.get("runs", id)
        require(self, run)
        if run.get("control"):
            raise ValueError("Execution paused/cancelled before model call")
        current = ProviderConfig.model_validate(
            self.store.get("providers", config.name)
        )
        if not current.enabled or current.model_dump(
            exclude={"enabled"}
        ) != config.model_dump(exclude={"enabled"}):
            raise ValueError(
                "Provider configuration changed or disconnected before model phase"
            )
        directory = self.store.run_dir(id)
        from levi.harness.context import as_text, brief

        # What LEVI knows locally about this dataset, as frozen at plan time:
        # a model LEVI runs sees it only if it is in the prompt.
        learner_brief = brief(self.store.state, run, phase)
        fingerprint = digest(
            {
                "input": run["manifest"],
                "config": config.model_dump(),
                "context": context.model_dump(exclude={"budget"}),
                "skills": skills(context),
                "summary": summary,
                "evidence": evidence,
                "brief": learner_brief,
            }
        )
        cache_id = f"{id}:{summary['episode_index']}:{phase}"
        # The model sees this phase's images; it may cite any frame already
        # observed for the episode -- a refinement keeps the draft's
        # citations of coarse frames.
        try:
            ledger = self.store.get("evidence", f"{id}:{summary['episode_index']}")[
                "items"
            ]
        except KeyError:
            ledger = []
        ledger = list({row["id"]: row for row in [*ledger, *evidence]}.values())
        try:
            cached = self.store.get("model_cache", cache_id)
        except KeyError:
            cached = None
        if cached and cached["fingerprint"] == fingerprint:
            for row in evidence:
                if (
                    row["artifact"]
                    and file_hash(directory / "evidence" / row["artifact"])
                    != row["sha256"]
                ):
                    raise ValueError("Evidence changed; cached result is invalid")
            self.store.mutate(
                "runs", id, lambda r: r.update(cache_hits=r.get("cache_hits", 0) + 1)
            )
            output = ModelOutput.model_validate(cached["output"])
            return gate(
                self,
                id,
                context,
                summary,
                ledger,
                phase,
                fingerprint,
                output,
                cached.get("learner_error"),
                cached.get("learner_raw"),
            ), cached["usage"]
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
        if (
            run["requests"] >= context.budget.max_calls
            or available < 256
            or seconds < 1
        ):
            raise ValueError("Approved budget exhausted")
        # One call can consume at most its context window (prompt and answer
        # share it), so that is what it reserves.
        reservation = min(max(8192, config.context_tokens), available)
        calls = min(3, context.budget.max_calls - run["requests"])
        self.store.mutate(
            "runs",
            id,
            lambda r: r.update(
                requests=r["requests"] + calls,
                reserved_tokens=r["reserved_tokens"] + reservation,
            ),
        )
        budget = context.budget.model_copy(
            update={
                "max_tokens": reservation,
                "max_calls": calls,
                "max_seconds": max(1, int(seconds)),
            }
        )
        from levi.inference.provider import InvalidAnswer

        began = time.monotonic()
        # The tokens are spent whether or not the answer holds up: settle
        # them first. Under a teacher an invalid answer is the lesson itself,
        # so it goes to the teacher with the reason (and, when it did not
        # even parse, the raw text); without one it is kept beside the run
        # and the phase fails.
        raw = problem = None
        try:
            output, usage = self.provider.generate(
                config,
                context.instruction + as_text(learner_brief),
                summary,
                evidence,
                directory / "evidence",
                budget,
            )
            output = anchor_to_draft(
                snap_to_episode(ModelOutput.model_validate(output), summary),
                summary,
            )
        except InvalidAnswer as exc:
            problem, raw, usage = exc, exc.raw, exc.usage
            output = exc.salvaged or ModelOutput(
                summary="(the answer did not parse; see learner_raw)"
            )
        except BaseException:
            # No answer and no reported usage: give the reservation back and
            # count the attempt, or every failed call would strand its
            # reservation and the run would look spent.
            self.store.mutate(
                "runs",
                id,
                lambda r: r.update(
                    requests=r["requests"] - calls + 1,
                    reserved_tokens=r["reserved_tokens"] - reservation,
                ),
            )
            self.store.event(
                id, "usage_unknown", phase=phase, episode=summary["episode_index"]
            )
            raise
        if problem is None:
            try:
                self.validate_proposals(context, output.proposals, ledger, summary)
            except ValueError as exc:
                problem = exc
        supervised = context.supervision != "none"
        if problem is not None and not supervised and output.proposals:
            # No teacher to hand it to: keep what holds up on its own, say
            # what was dropped, and let the human review see the rest.
            # In the model's order, each proposal stays if the answer so far
            # still validates with it (so of two overlapping intervals the
            # first one written is kept).
            kept = []
            for proposal in output.proposals:
                try:
                    self.validate_proposals(context, [*kept, proposal], ledger, summary)
                except ValueError:
                    continue
                kept.append(proposal)
            if kept:
                self.store.event(
                    id,
                    "answer_salvaged",
                    phase=phase,
                    episode=summary["episode_index"],
                    dropped=len(output.proposals) - len(kept),
                    reason=str(problem)[:300],
                )
                output = output.model_copy(update={"proposals": kept})
                problem = raw = None
        overspent = (
            usage.get("requests", calls) > calls
            or usage.get("tokens", reservation) > reservation
        )
        usage = {
            **usage,
            "elapsed_seconds": time.monotonic() - began,
            "usage_kind": usage.get(
                "usage_kind",
                "reported" if "tokens" in usage else "conservative_reservation",
            ),
        }
        # Settle accounting and cache together; crash cannot give a free cached result.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self.store.get("runs", id)
            latest.update(
                requests=latest["requests"] - calls + usage.get("requests", calls),
                reserved_tokens=latest["reserved_tokens"] - reservation,
                tokens=latest["tokens"] + usage.get("tokens", reservation),
            )
            self.store.save(db, "runs", id, latest)
            # Spent is spent, overspent or not; only a usable answer is cached.
            if not overspent and (problem is None or supervised):
                self.store.save(
                    db,
                    "model_cache",
                    cache_id,
                    {
                        "fingerprint": fingerprint,
                        "output": output.model_dump(),
                        "usage": usage,
                        **({"learner_error": str(problem)} if problem else {}),
                        **({"learner_raw": raw} if raw else {}),
                    },
                )
        if usage.get("prompt_chars") and usage.get("prompt_tokens"):
            from levi.inference import request_cost

            request_cost.record(
                config,
                usage["prompt_chars"],
                sum(1 for row in evidence if row.get("artifact")),
                usage["prompt_tokens"],
            )
        if overspent:
            raise ValueError(
                "Provider exceeded reserved budget; stopped before further calls"
            )
        name = f"episode_{summary['episode_index']:06d}-{phase}"
        (directory / f"{name}{'-rejected' if problem else ''}.json").write_text(
            json.dumps(
                {
                    "output": output.model_dump(),
                    "usage": usage,
                    **({"learner_error": str(problem)} if problem else {}),
                    **({"learner_raw": raw} if raw else {}),
                },
                ensure_ascii=False,
            )
        )
        self.store.event(
            id, "model_step", phase=phase, episode=summary["episode_index"], usage=usage
        )
        if problem is not None and not supervised:
            raise problem
        return gate(
            self,
            id,
            context,
            summary,
            ledger,
            phase,
            fingerprint,
            output,
            str(problem) if problem else None,
            raw,
        ), usage

    @staticmethod
    def validate_proposals(context, proposals, evidence, summary):
        from .observations import quality

        quality(context, proposals, evidence, summary)
        known = {row["id"]: row for row in evidence}
        for proposal in proposals:
            if (
                proposal.episode_index not in context.episodes
                or proposal.episode_index != summary["episode_index"]
            ):
                raise ValueError("Proposal escapes episode scope")
            if not proposal.evidence_ids:
                raise ValueError(
                    f"Proposal at {proposal.start:g} s cites no evidence; cite "
                    "the frames it rests on"
                )
            # Name the citation that failed and why: a model told only that
            # "evidence is unknown" has to resend everything to find out which.
            for id in proposal.evidence_ids:
                if id not in known:
                    raise ValueError(
                        f"Proposal cites unknown evidence {id!r}; cite only ids "
                        f"from the evidence supplied for episode "
                        f"{summary['episode_index']}"
                    )
                if known[id]["episode_index"] != proposal.episode_index:
                    raise ValueError(
                        f"Proposal for episode {proposal.episode_index} cites "
                        f"evidence {id!r} from episode "
                        f"{known[id]['episode_index']}"
                    )
            if proposal.start < summary["start"] or proposal.start > summary["end"]:
                raise ValueError(
                    f"Proposal begins at {proposal.start:g} s, outside the "
                    f"episode ({summary['start']:g}-{summary['end']:g} s)"
                )
            if proposal.end is not None and proposal.end > summary["end"]:
                raise ValueError(
                    f"Proposal ends at {proposal.end:g} s, after the episode's "
                    f"last frame at {summary['end']:g} s"
                )

    def prepare_changes(self, id):
        run = self.store.get("runs", id)
        proposals = []
        for ep in run["completed"]:
            proposals += self.store.get("shards", f"{id}:{ep}")["output"]["proposals"]
        existing = run.get("changes")
        previous = self.store.get("changes", existing) if existing else None
        if previous and previous["status"] == "committed":
            raise Conflict("Published changes cannot be regenerated")
        if previous:
            # Keep human edits/decisions on completed shards; append ONLY newly
            # processed episodes. No resumption may replace a reviewed draft.
            known = set(previous["provenance"].get("included_episodes", []))
            proposals = previous["proposals"] + [
                p for p in proposals if p["episode_index"] not in known
            ]
        change = ChangeSet(
            id=existing or new_id(set(self.store.ids("changes"))),
            run_id=id,
            base_revision=run["base_revision"],
            proposals=proposals,
            revision=previous["revision"] + 1 if previous else 0,
            decisions=previous.get("decisions", {}) if previous else {},
            object_jobs=previous.get("object_jobs", []) if previous else [],
            provenance={
                "schema": "levi.agent.provenance.v1",
                "provider": run["provider_config"],
                "input_sha256": run["manifest"]["sha256"],
                "config_sha256": digest(run["context"]),
                "included_episodes": run["completed"],
                # What the agent was instructed with, as the plan approved it:
                # the skill fingerprints and the harness parameters.
                "skills_version": ",".join(
                    f"{name}@{_skill_version(name)}"
                    for name in sorted(run.get("skill_fingerprints", {}))
                )
                or "unknown",
                "harness": {
                    k: (run.get("harness") or {}).get(k)
                    for k in ("parameters", "sources")
                },
                "supervision": run["context"].get("supervision", "none"),
                "teacher_grant": run["context"].get("teacher_grant"),
                "reviewer_type": None,
                "coverage": "sampled",
            },
        ).model_dump()
        self.store.put("changes", change["id"], change)
        self.store.mutate("runs", id, lambda r: r.update(changes=change["id"]))
        return change

    def rebase(self, id, revision):
        """Move a reviewed draft onto the current published revision.

        Two runs on one dataset (say language segments and object masks) are
        normal; whichever commits first would otherwise force the other to be
        produced again. Rebasing keeps the staged work, re-reads what is now
        published, and drops the approval so a human decides again.
        """
        from .store import Conflict, annotation_digest, dataset_lock

        change = self.store.get("changes", id)
        run = self.store.get("runs", change["run_id"])
        with dataset_lock(self.store.state, run["dataset_key"]):
            change = self.store.get("changes", id)
            if change["status"] == "committed":
                raise Conflict("Published changes cannot be rebased")
            if change["revision"] != revision:
                raise Conflict("Reload the changeset: its revision moved")
            head = self.store.head(run["dataset_key"])
            if head == change["base_revision"]:
                return {"changeset_id": id, "base_revision": head, "rebased": False}
            # The frozen source must still be the one the suggestions were read
            # from; only the annotation bundle is allowed to have moved.
            context = TaskContext.model_validate(run["context"])
            DATASETS[context.dataset_adapter].verify(context, run["manifest"])
            previous = change["base_revision"]
            self.store.mutate(
                "changes",
                id,
                lambda c: c.update(
                    base_revision=head,
                    status="draft",
                    revision=c["revision"] + 1,
                    provenance={
                        **c["provenance"],
                        "rebased_from": previous,
                        "rebased_at": time.time(),
                    },
                ),
            )
            self.store.mutate(
                "runs",
                run["id"],
                lambda r: r.update(
                    base_revision=head,
                    base_content=annotation_digest(
                        self.store.state, run["dataset_key"]
                    ),
                ),
            )
            self.store.event(run["id"], "changes_rebased", changeset=id, onto=head)
            return {
                "changeset_id": id,
                "base_revision": head,
                "rebased": True,
                "previous_base": previous,
                "note": "Approval was cleared; review the draft against the new revision",
            }

    def validate(self, change):
        run = self.store.get("runs", change["run_id"])
        context = TaskContext.model_validate(run["context"])
        if context.mode == "read_only" and (
            change["proposals"] or change.get("object_jobs")
        ):
            raise ValueError("Read-only run cannot commit annotations")
        if self.store.head(run["dataset_key"]) != change["base_revision"]:
            raise Conflict("Annotations changed after planning; create a new run")
        if (
            annotation_digest(self.store.state, run["dataset_key"])
            != run["base_content"]
        ):
            raise Conflict("Saved annotations changed after planning; create a new run")
        DATASETS[context.dataset_adapter].verify(context, run["manifest"])
        input_root = self.store.run_dir(run.get("input_run", run["id"])) / "input"
        for relative, expected_hash in run["manifest"]["files"].items():
            path = media.backend().inside(relative, input_root)
            if not path.is_file() or file_hash(path) != expected_hash:
                raise ValueError("Input snapshot changed; create a new run")
        quality_reports = []
        for ep in run["completed"]:
            saved = self.store.get("evidence", f"{run['id']}:{ep}")
            for evidence in saved["items"]:
                if evidence.get("artifact"):
                    path = (
                        self.store.run_dir(run["id"])
                        / "evidence"
                        / evidence["artifact"]
                    )
                    if not path.is_file() or file_hash(path) != evidence["sha256"]:
                        raise ValueError("Evidence artifact changed; create a new run")
            proposals = [
                p
                for p in ChangeSet.model_validate(change).proposals
                if p.episode_index == ep
            ]
            self.validate_proposals(
                context, proposals, saved["items"], saved["summary"]
            )
            from .observations import quality

            quality_reports.append(
                {
                    "episode": ep,
                    **quality(context, proposals, saved["items"], saved["summary"]),
                }
            )
        if any(p["episode_index"] not in run["completed"] for p in change["proposals"]):
            raise ValueError("Proposal references an uncompleted shard")
        pending_objects = 0
        for job_id in change.get("object_jobs", []):
            from .objects import result_rows

            job = self.store.get("object_jobs", job_id)
            if job["run_id"] != run["id"]:
                raise ValueError("Object job belongs to another run")
            pending_objects += sum(
                r.status not in {"accepted", "rejected"} for r in result_rows(self, job)
            )
        return {
            "ok": True,
            "quality": quality_reports,
            "pending_objects": pending_objects,
            "coverage": "sampled",
            "completed": run["completed"],
            "unprocessed": sorted(set(context.episodes) - set(run["completed"])),
        }

    def commit(self, id, key, expected):
        from .store import dataset_lock

        change = self.store.get("changes", id)
        run = self.store.get("runs", change["run_id"])
        with dataset_lock(self.store.state, run["dataset_key"]):
            return self._commit(id, key, expected)

    def _commit(self, id, key, expected):
        request = digest([id, expected])
        receipt = self.store.receipt(key, request)
        if receipt:
            return receipt
        change = self.store.get("changes", id)
        if change["status"] != "approved" or change["revision"] != expected:
            raise Conflict("Review and approve this exact ChangeSet revision first")
        if self.validate(change).get("pending_objects"):
            raise Conflict("Staged objects need human review")
        run = self.store.get("runs", change["run_id"])
        if run["status"] not in {RunStatus.WAITING, RunStatus.PARTIAL}:
            raise Conflict("Pause execution and finish reviewing before committing")
        app = media.backend()
        context = TaskContext.model_validate(run["context"])
        state = DATASETS[context.dataset_adapter].publication_state(context)
        base, revision, folder = self.store.prepare(
            run["dataset_key"], run["context"]["workflow"]["kind"]
        )
        if base != change["base_revision"]:
            raise Conflict("Annotations changed before commit")
        from .undo import apply, capture

        change["inverse"] = capture(folder, change)
        if change.get("undo_of"):
            apply(self, folder, change, run["dataset_key"])
        with pin(self.store.state, run["dataset_key"], folder):
            from .supersede import split

            provenance_path = folder / "agent-provenance.json"
            history = (
                json.loads(provenance_path.read_text())
                if provenance_path.exists()
                else []
            )
            grouped = {}
            for index, proposal in enumerate(change["proposals"]):
                if change.get("decisions", {}).get(str(index)) == "rejected":
                    continue
                grouped.setdefault(proposal["episode_index"], []).append(proposal)
            replaced = {}
            origin = {"run_id": run["id"], "changeset": id}
            for ep, proposals in grouped.items():
                response = app.get_episode_atoms(
                    ep,
                    repo_id=run["context"]["repo_id"],
                    revision=run["context"].get("revision"),
                )
                atoms, gone = split(
                    json.loads(response.body)["atoms"], proposals, history, ep
                )
                if gone:
                    replaced[str(ep)] = len(gone)
                for proposal in proposals:
                    ANNOTATIONS[proposal["kind"]].apply(
                        proposal,
                        app=app,
                        state=state,
                        atoms=atoms,
                        folder=folder,
                        origin=origin,
                    )
                app._write_episode_annotations(state, ep, atoms)
            change["replaced"] = replaced
            if change.get("object_jobs"):
                from levi.annotations.schema import ObjectAnnotation

                from .objects import result_rows

                store = app._sidecar(state)
                existing = [
                    ObjectAnnotation.model_validate(r) for r in store.read_annotations()
                ]
                additions = []
                for job_id in change["object_jobs"]:
                    additions += result_rows(
                        self, self.store.get("object_jobs", job_id)
                    )
                # Append reviewed tracks with fresh per-camera IDs. A bounded
                # SAM3 run must never erase previously reviewed frames/tracks.
                additions = [r for r in additions if r.status == "accepted"]
                maxima = {}
                for row in existing:
                    scope = (row.episode_index, row.camera_key)
                    maxima[scope] = max(maxima.get(scope, -1), row.track_id)
                mapping = {}
                for row in additions:
                    scope = (row.episode_index, row.camera_key)
                    track_key = (*scope, row.object_id, row.track_id)
                    if track_key not in mapping:
                        maxima[scope] = maxima.get(scope, -1) + 1
                        mapping[track_key] = maxima[scope]
                    row.track_id = mapping[track_key]
                    row.object_id = (
                        f"agent-{run['id']}-{row.camera_key}-{row.object_id}"
                    )
                store.publish(
                    existing + additions,
                    parent_revision=store.current_revision(),
                    model={
                        "provider": "sam3",
                        "run_id": run["id"],
                        "reviewer_type": "human",
                    },
                )
            change["status"] = "committed"
            change["published_revision"] = revision
            history.append(change)
            app.atomic(provenance_path, history)
        change["status"] = "committed"
        change["published_revision"] = revision
        receipt = {
            "ok": True,
            "changeset": id,
            "revision": revision,
            "parent": base,
            # Earlier agent atoms this commit replaced, per episode.
            "replaced": change.get("replaced", {}),
        }
        result = self.store.publish(
            run["dataset_key"], base, revision, key, request, receipt, change=change
        )
        self.store.mutate(
            "runs",
            run["id"],
            lambda r: r.update(
                status=RunStatus.SUCCEEDED
                if set(r["completed"]) == set(r["context"]["episodes"])
                else RunStatus.PARTIAL
            ),
        )
        self.store.event(run["id"], "committed", revision=revision)
        return result


def _skill_version(name):
    """The ``metadata.version`` a skill declares in its front matter."""
    import re
    from pathlib import Path

    path = Path(__file__).parent / "skills" / name / "SKILL.md"
    try:
        found = re.search(r'version:\s*"?([\w.]+)"?', path.read_text())
    except OSError:
        return "missing"
    return found.group(1) if found else "unversioned"


def stop():
    # Only owned threads. External calls have bounded timeouts and keep their
    # durable lease/reservation until reconciliation after a process restart.
    with _LOCK:
        threads = list(_ACTIVE.values())
    for thread in threads:
        with suppress(RuntimeError):
            thread.join(timeout=0.1)


def prepare_evidence(wb, id, *, episodes=None):
    """Prepare a bounded subset of approved evidence without model calls.

    ``episodes``: only these -- a batch from ``runs.prepare`` (so a large
    scope never needs one long call), or the episodes an agent's first read
    names (so nobody waits for a whole dataset before starting). Episodes
    already prepared are kept as they are: preparing again would drop the
    frames a refinement added."""
    from .store import dataset_lock

    with dataset_lock(wb.store.state, "run-" + id):
        run = wb.store.get("runs", id)
        from .planning import require

        require(wb, run)
        if run["status"] in {
            RunStatus.RUNNING,
            RunStatus.QUEUED,
            RunStatus.CANCELLED,
            RunStatus.SUCCEEDED,
        }:
            raise Conflict("Run is not available for evidence preparation")
        ctx = TaskContext.model_validate(run["context"])
        directory = wb.store.run_dir(id)
        directory.mkdir(parents=True, exist_ok=True)
        if not run.get("manifest") or not (directory / "input").is_dir():
            # A snapshot removed by workspace.clean is taken again here, which
            # is what makes that cleanup non-destructive; a source that moved
            # since the plan was approved stops the run instead.
            import shutil

            shutil.rmtree(directory / "input", ignore_errors=True)
            manifest = DATASETS[ctx.dataset_adapter].snapshot(
                ctx, directory / "input", run["files"], run.get("planned_hashes")
            )
            if run.get("manifest") and manifest["sha256"] != run["manifest"]["sha256"]:
                raise ValueError(
                    "The source no longer matches this run's cleaned-up "
                    "snapshot; create a new run rather than annotating "
                    "different data under an approved plan"
                )
            wb.store.mutate("runs", id, lambda r: r.update(manifest=manifest))
        allowed = (
            ctx.episodes
            if (run["plan"].get("pilot_review") or {}).get("accepted")
            else [run["plan"]["pilot_episode"]]
        )
        if episodes is None:
            selected = list(allowed)
        else:
            selected = list(episodes)
            if not selected or len(selected) != len(set(selected)):
                raise ValueError("Prepare a nonempty set of distinct episodes")
            outside = sorted(set(selected) - set(allowed))
            if outside:
                raise ValueError(
                    "Evidence request exceeds the approved pilot/scope: "
                    + (
                        f"episodes {outside} open after the pilot is accepted"
                        if set(outside) <= set(ctx.episodes)
                        else f"episodes {outside} are outside the approved scope"
                    )
                )
            if set(selected) & set(run["completed"]):
                raise ValueError(
                    "Completed episodes cannot be prepared again in a batch"
                )
            done = set(run.get("prepared") or [])
            selected = [ep for ep in selected if ep not in done]
        for ep in selected:
            # The declared observation policy, not a second uniform sampler:
            # a temporal plan gets its coarse step, everything else the plan's
            # sample count. Headless/MCP agents therefore see exactly what the
            # in-process runtime sees.
            from .observations import observe, persist

            summary, evidence = observe(
                ctx, directory / "input", ep, directory / "evidence"
            )
            persist(wb, id, ep, summary, evidence)
        wb.store.mutate(
            "runs",
            id,
            lambda r: r.update(
                prepared=sorted(set(r.get("prepared") or []) | set(selected))
            ),
        )
        if ctx.workflow["kind"] == "objects":
            for ep in selected:
                wb.store.put(
                    "shards",
                    f"{id}:{ep}",
                    {
                        "output": {
                            "summary": "Object-only workflow; no language model call",
                            "proposals": [],
                        },
                        "usage": {"requests": 0, "tokens": 0},
                    },
                )
            wb.prepare_changes(id)
            wb.store.mutate("runs", id, lambda r: r.update(status="waiting_for_review"))
        wb.store.event(id, "evidence_prepared", coverage="sampled")
        value = {"run_id": id, "coverage": "sampled", "episodes": selected}
        top_k = (
            (run.get("harness") or {})
            .get("parameters", {})
            .get("evidence.refine_top_k", 0)
        )
        if top_k and ctx.workflow["kind"] == "temporal":
            from .observations import changes

            run = wb.store.get("runs", id)
            value["refine_first"] = {
                str(ep): changes(wb, run, ep, top_k)["suggested_around_seconds"]
                for ep in selected
            }
            value["refine_policy"] = (
                f"Published harness parameter evidence.refine_top_k={top_k} "
                f"({run['harness']['sources'].get('evidence.refine_top_k')}): call "
                "evidence.refine with these around_seconds before proposing; they "
                "are the coarse intervals where the picture changed most."
            )
        width = (
            (run.get("harness") or {})
            .get("parameters", {})
            .get("evidence.mosaic_tile_width")
        )
        if width:
            value["read_with"] = {
                "layout": "mosaic",
                "tile_width": width,
                "why": "evidence.read uses this dataset's published tile width when "
                "you omit tile_width; image tokens grow with its square.",
            }
        return value
