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
from .providers import CompatibleProvider
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


class Workbench:
    def __init__(self, state, provider=None):
        self.store = Store(state)
        self.provider = provider or CompatibleProvider()

    def plan(self, context: TaskContext, principal=None):
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
            if not config.tools:
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
        from .planning import attach

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
        thread = threading.Thread(
            target=lambda: ctx.run(self.execute, id, owner, pilot=pilot), daemon=True
        )
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
                available_tokens = (
                    context.budget.max_tokens - run["tokens"] - run["reserved_tokens"]
                )
                if (
                    run["requests"] >= context.budget.max_calls
                    or available_tokens < 256
                ):
                    raise ValueError("Model budget exhausted")
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
                    refined_summary, dense = observations.observe(
                        context,
                        directory / "input",
                        episode,
                        directory / "evidence",
                        output.proposals,
                    )
                    refined_summary.update(
                        workflow=context.workflow,
                        candidate_draft=output.model_dump(),
                        phase="boundary_refinement",
                    )
                    evidence = observations.persist(self, id, episode, summary, dense)
                    # Only the requested window images go to refinement; old evidence remains retrievable.
                    output, usage = self.model_step(
                        id, config, context, refined_summary, dense, "refine", started
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
            reason = (
                str(exc)
                if type(exc) is ValueError
                else f"Execution blocked ({type(exc).__name__}); inspect provider configuration"
            )
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

    def model_step(self, id, config, context, summary, evidence, phase, started):
        context = TaskContext.model_validate(context.model_dump())
        from .observations import skills
        from .planning import require

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
        fingerprint = digest(
            {
                "input": run["manifest"],
                "config": config.model_dump(),
                "context": context.model_dump(exclude={"budget"}),
                "skills": skills(context),
                "summary": summary,
                "evidence": evidence,
            }
        )
        cache_id = f"{id}:{summary['episode_index']}:{phase}"
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
            return ModelOutput.model_validate(cached["output"]), cached["usage"]
        available = context.budget.max_tokens - run["tokens"] - run["reserved_tokens"]
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
        reservation = min(8192, available)
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
        began = time.monotonic()
        output, usage = self.provider.generate(
            config,
            context.instruction,
            summary,
            evidence,
            directory / "evidence",
            budget,
        )
        output = ModelOutput.model_validate(output)
        self.validate_proposals(context, output.proposals, evidence, summary)
        if (
            usage.get("requests", calls) > calls
            or usage.get("tokens", reservation) > reservation
        ):
            raise ValueError(
                "Provider exceeded reserved budget; stopped before further calls"
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
            self.store.save(
                db,
                "model_cache",
                cache_id,
                {
                    "fingerprint": fingerprint,
                    "output": output.model_dump(),
                    "usage": usage,
                },
            )
        (directory / f"episode_{summary['episode_index']:06d}-{phase}.json").write_text(
            json.dumps(
                {"output": output.model_dump(), "usage": usage}, ensure_ascii=False
            )
        )
        self.store.event(
            id, "model_step", phase=phase, episode=summary["episode_index"], usage=usage
        )
        return output, usage

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
                raise ValueError("Proposal begins outside episode")
            if proposal.end is not None and proposal.end > summary["end"]:
                raise ValueError("Proposal ends outside episode")

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
                "skills_version": "1",
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
            grouped = {}
            for index, proposal in enumerate(change["proposals"]):
                if change.get("decisions", {}).get(str(index)) == "rejected":
                    continue
                grouped.setdefault(proposal["episode_index"], []).append(proposal)
            for ep, proposals in grouped.items():
                response = app.get_episode_atoms(
                    ep,
                    repo_id=run["context"]["repo_id"],
                    revision=run["context"].get("revision"),
                )
                atoms = json.loads(response.body)["atoms"]
                for proposal in proposals:
                    ANNOTATIONS[proposal["kind"]].apply(
                        proposal, app=app, state=state, atoms=atoms, folder=folder
                    )
                app._write_episode_annotations(state, ep, atoms)
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
            provenance_path = folder / "agent-provenance.json"
            history = (
                json.loads(provenance_path.read_text())
                if provenance_path.exists()
                else []
            )
            change["status"] = "committed"
            change["published_revision"] = revision
            history.append(change)
            app.atomic(provenance_path, history)
        change["status"] = "committed"
        change["published_revision"] = revision
        receipt = {"ok": True, "changeset": id, "revision": revision, "parent": base}
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


def stop():
    # Only owned threads. External calls have bounded timeouts and keep their
    # durable lease/reservation until reconciliation after a process restart.
    with _LOCK:
        threads = list(_ACTIVE.values())
    for thread in threads:
        with suppress(RuntimeError):
            thread.join(timeout=0.1)


def prepare_evidence(wb, id):
    """Headless/MCP evidence preparation without model calls."""
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
        selected = (
            ctx.episodes
            if (run["plan"].get("pilot_review") or {}).get("accepted")
            else [run["plan"]["pilot_episode"]]
        )
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
        wb.store.mutate("runs", id, lambda r: r.update(prepared=selected))
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
        return {"run_id": id, "coverage": "sampled", "episodes": selected}
