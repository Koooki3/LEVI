"""One dataset's batch: mirror, view, annotate, commit, judge, clean up.

``python -m levi.live.worker --config <live.toml> --dataset <name>`` is what
the supervisor starts when a dataset has finished rollouts to label and the
GPU policy allows it; it exits when the batch is done. It does the heavy
imports (pandas, OpenCV, pydantic) the idle supervisor never loads, and its
every step is resumable: the dataset's state file records the batch in
progress (``current``), runs live in LEVI's own store, and a crash or a
preemption at any point is continued by the next worker.

The pipeline for a batch of finished demos of one task:

1. **mirror** the finished demos into the workspace capture (hard links);
2. **view**: register the capture and build its browsing view (LEVI's normal
   raw-capture path); map demos to episode indices *from the view*, never from
   memory, because a view rebuild can renumber episodes;
3. leave out demos that already carry a person's annotations;
4. **temporal run** (subtask segments and per-segment outcomes) with the
   generic guideline quoting the task instruction: plan, approve, execute,
   validate, approve, commit -- every human gate taken by the audited
   automatic approver (auto.py), or left for a person when it is off;
5. **anchored review run** (one release-review question at each gripper
   opening; the episode's automatic verdict). It is executed and its records
   kept, but never committed: the verdict is not a human outcome label;
6. record the results, drop the finished runs' frozen inputs and evidence.

Exit codes: 0 batch done, 10 waiting for the model server, 11 waiting for a
person, 12 error (retried later), 13 preempted (SIGTERM), 14 nothing to do.
"""

import argparse
import contextlib
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

from . import auto as approver_log
from . import config as live_config
from . import generic, gpumgr, jsonio, mirror, stats

OK, NEED_MODEL, AWAIT_HUMAN, ERROR, PREEMPTED, NOTHING = 0, 10, 11, 12, 13, 14
SIDE = "observation.images.view1"
WRIST = "observation.images.hand"
RUN_DONE = {"succeeded", "partially_succeeded", "failed", "cancelled"}


class Stop(Exception):
    """SIGTERM arrived: pause the run and leave."""


def model_use(events, episode) -> dict:
    """What the model cost one episode in one run, from the run's journal:
    requests sent (cache hits are not requests), tokens and seconds the
    ``model_step`` events report."""
    use = {"requests": 0, "tokens": 0, "model_s": 0.0}
    for event in events:
        if event.get("type") != "model_step" or event.get("episode") != episode:
            continue
        usage = event.get("usage") or {}
        use["requests"] += 0 if usage.get("cached") else 1
        use["tokens"] += int(usage.get("tokens") or 0)
        use["model_s"] += float(usage.get("elapsed_seconds") or 0.0)
    return use


def episode_line(demo, episode, size, stages, row, gated) -> str:
    """One readable, greppable line for a labelled demo (``episode demo=...``).

    ``stages`` is ``{name: (wall_s, use)}``: the stage's wall clock (shared by
    the batch) and this episode's model use in it; ``gated`` is ``(count,
    seconds)`` the batch spent standing down for the policy server."""
    parts = [f"episode demo={demo} ep={episode} batch={size}"]
    for name, (wall, use) in stages.items():
        parts.append(
            f"{name}_wall={wall:.1f}s {name}_model={use['model_s']:.1f}s "
            f"{name}_requests={use['requests']} {name}_tokens={use['tokens']}"
        )
    temporal = row.get("temporal") or {}
    verdict = row.get("verdict")
    parts.append(f"segments={temporal.get('segments', 0)}")
    if verdict:
        parts.append(
            f"verdict={verdict.get('outcome')} valid_events={verdict.get('valid_events')}"
            + (" undecided" if verdict.get("undecided") else "")
        )
    else:
        parts.append("verdict=none")
    parts.append(f"gated={gated[0]}x/{gated[1]:.1f}s" if gated[0] else "gated=no")
    parts.append(f"state={row.get('state')}")
    if row.get("reason") and not temporal:
        parts.append(f"reason={str(row['reason'])[:120]!r}")
    return " ".join(parts)


class NeedModel(Exception):
    pass


class AwaitHuman(Exception):
    """A person has to act: ``kind`` is ``plan`` (approve the plan) or
    ``changes`` (approve and commit the draft). The supervisor learns which
    gate through ``worker.json`` and does not start another worker until it
    has been passed."""

    def __init__(self, kind, run_id, changeset=None):
        super().__init__(f"{kind} of {run_id}")
        self.kind, self.run_id, self.changeset = kind, run_id, changeset


class Worker:
    def __init__(self, config, name, provider_spec, *, log=None):
        self.config = config
        self.name = name
        self.provider_spec = provider_spec
        self.log = log or (lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True))
        self.stopping = False
        self._beat = 0.0
        self.progress_path = config.live_dir / "worker.json"
        self.detail: dict = {}
        from levi import paths
        from levi.agent.runtime import Workbench

        self.wb = Workbench(paths.STATE)
        self.store = self.wb.store
        from . import auto

        self.auto = auto.principal() if config.pipeline.auto_approve else None
        # What a person would do by hand: plan as the approver when it is on,
        # else as the local operator (and stop at the approval gate).
        from levi.agent.security import Principal

        self.planner = self.auto or Principal("live-planner", human=True)
        self.repo_id = ""
        self.entry: dict = {}
        # This batch's clock, for the per-episode summary line (finish).
        self.walls: dict = {}
        self.lengths: dict = {}
        self.view: Path | None = None
        self.gated = [0, 0.0]  # times the gate stopped it, and for how long

    # --- plumbing -------------------------------------------------------------

    def progress(self, phase, **detail):
        self.detail = {**self.detail, **detail}
        now = time.time()
        jsonio.write(
            self.progress_path,
            {
                "pid": os.getpid(),
                "dataset": self.name,
                "phase": phase,
                "started_at": self.detail.get("started_at", now),
                "updated_at": now,
                **{k: v for k, v in self.detail.items() if k != "started_at"},
            },
        )
        self._beat = now

    def heartbeat(self, phase):
        if time.time() - self._beat >= 4:
            self.progress(phase)

    def check_stop(self):
        if self.stopping:
            raise Stop

    def state(self):
        value = mirror.load_state(self.config, self.name)
        if not value:
            raise RuntimeError(f"No state for dataset {self.name}")
        return value

    def update(self, change):
        return jsonio.update(
            mirror.state_path(self.config, self.name), change, default=dict
        )

    def call(self, name, arguments, key=None, principal=None):
        from levi.agent.capabilities import invoke

        return invoke(
            self.wb, principal or self.auto or self.planner, name, arguments, key
        )

    # --- provider ---------------------------------------------------------------

    def ensure_provider(self):
        """The live workspace's model profile, bound to the vLLM now serving."""
        from levi.agent.schema import ProviderConfig
        from levi.inference import models, transport

        spec = self.provider_spec
        wanted = ProviderConfig.model_validate(
            {
                "name": spec["name"],
                "kind": "openai-local",
                "base_url": spec["base_url"],
                "model": spec["model"],
                "context_tokens": spec["context_tokens"],
                "max_images": spec["max_images"],
                "prompt_style": self.config.provider.prompt_style,
                "requests_in_flight": self.config.provider.requests_in_flight,
                "allow_localhost": True,
                "vision": True,
                "structured_output": True,
            }
        )
        transport.validate_local_endpoint(
            wanted.base_url, True, transport.OpenAILocalTransport.label
        )
        try:
            current = ProviderConfig.model_validate(
                self.store.get("providers", wanted.name)
            )
        except KeyError:
            current = None
        same = current is not None and current.model_dump(
            exclude={"model_digest", "context_tokens"}
        ) == wanted.model_dump(exclude={"model_digest", "context_tokens"})
        if same and current.model_digest:
            with contextlib.suppress(Exception):  # unreachable: rebind below
                if models.inspect(self.store, wanted.name)["digest_matches"]:
                    return current
        self.store.put("providers", wanted.name, wanted.model_dump())
        info = models.inspect(self.store, wanted.name)
        if not info.get("model"):
            raise NeedModel("the vLLM server does not serve the configured model")
        models.bind(
            self.store,
            wanted.name,
            info["model"]["digest"],
            vision=True,
            structured_output=True,
        )
        return ProviderConfig.model_validate(self.store.get("providers", wanted.name))

    def model_ready(self):
        return gpumgr.healthy(self.provider_spec["port"])

    # --- the dataset --------------------------------------------------------------

    def register_and_build(self):
        """Register the capture and bring its browsing view up to date."""
        from levi import catalog, views
        from levi.conversion.engine import fingerprint

        state = self.state()
        capture = Path(state["capture"])
        options = {"workers": self.config.resources.view_workers}
        entry = views.request(capture, options)
        deadline = time.time() + 3600
        while True:
            entry = catalog.datasets().get(entry["name"]) or entry
            view = Path(entry["view"]) if entry.get("view") else None
            ready = (
                entry.get("view_status") == "ready"
                and view is not None
                and views.is_view(view)
                and catalog.read(view / "meta/levi_view.json", {}).get(
                    "source_fingerprint"
                )
                == fingerprint(capture)
            )
            if ready:
                break
            if entry.get("view_status") == "failed":
                raise RuntimeError(
                    "view build failed: " + str(entry.get("view_error"))[:300]
                )
            if time.time() > deadline:
                raise RuntimeError("view build did not finish in an hour")
            self.check_stop()
            self.heartbeat("view")
            time.sleep(1.0)
            if entry.get("view_status") == "building" and not self._job_alive(entry):
                entry = views.request(capture, options)
        self.entry = entry
        self.repo_id = entry["id"]
        self.ensure_vocabulary()
        return entry

    @staticmethod
    def _job_alive(entry):
        from levi import catalog, jobs

        job = catalog.read(jobs.STATE / "jobs" / f"{entry.get('view_job')}.json", {})
        return job.get("status") in ("planned", "queued", "running")

    def ensure_vocabulary(self):
        """The dataset's own subtask vocabulary is the generic one (written
        once; a person's later edits are never overwritten)."""
        from backend import app
        from levi.annotations import vocabulary

        state = app._ensure_state(app.DatasetRef(repo_id=self.repo_id))
        if not vocabulary.own(state.annotations_dir)["subtasks"]:
            vocabulary.write(
                state.annotations_dir,
                generic.vocabulary(self.config.pipeline.vocabulary),
            )

    def episode_map(self, entry):
        """``{demo: episode_index}`` and ``{demo: seconds}`` from the view."""
        view = Path(entry["view"])
        self.view = view
        fps = float(jsonio.read(view / "meta/info.json", {}).get("fps") or 10.0)
        rows = {}
        for line in (view / "meta/episodes.jsonl").read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                demo = str(row.get("source_demo") or "").split("/")[-1]
                rows[demo] = (
                    int(row["episode_index"]),
                    max(0.0, (row.get("length", 1) - 1) / fps),
                )
        excluded = jsonio.read(view / "meta/levi_view.json", {}).get("excluded") or {}
        return (
            {d: r[0] for d, r in rows.items()},
            {d: r[1] for d, r in rows.items()},
            {str(k).split("/")[-1]: v for k, v in excluded.items()},
        )

    def human_annotated(self, episode):
        """Whether a person (or an earlier commit this service lost track of)
        already annotated this episode: never annotated over."""
        from levi import paths
        from levi.agent.store import resolve

        folder = resolve(paths.STATE, self.entry["name"], "annotations")
        path = Path(folder) / f"episode_{episode:06d}.json"
        value = jsonio.read(path)
        return bool(isinstance(value, dict) and value.get("atoms"))

    # --- batch selection -------------------------------------------------------------

    def start_batch(self):
        """Mirror what is finished and choose this batch's demos."""
        w, p = self.config.watch, self.config.pipeline
        mirror.verify_sources(self.config, self.name)
        mirror.refresh_changed(self.config, self.name)
        scanner = mirror.Scanner(self.config)
        scan = next((t for t in scanner.scan() if t.name == self.name), None)
        state = self.state()
        pending = [
            d
            for d, row in state["demos"].items()
            if row.get("state") == "mirrored"
            and row.get("attempts", 0) < p.max_attempts
        ]
        room = max(0, w.batch_max_episodes - len(pending))
        ready = list(scan.ready[:room]) if scan else []
        if ready:
            self.progress("mirror", ready=len(ready))
            mirror.mirror_dataset(self.config, state, ready)
        state = self.state()
        demos = sorted(
            d
            for d, row in state["demos"].items()
            if row.get("state") == "mirrored"
            and row.get("attempts", 0) < p.max_attempts
        )[: w.batch_max_episodes]
        return demos

    # --- runs ---------------------------------------------------------------------------

    def context(self, episodes, cameras, instruction, workflow):
        return {
            "repo_id": self.repo_id,
            "episodes": episodes,
            "cameras": cameras,
            "instruction": instruction,
            "provider": self.provider_spec["name"],
            "allow_media_egress": True,
            "workflow": {**workflow, "require_human_pilot": False},
            "budget": {
                "max_calls": 1000,
                "max_tokens": None,
                "max_seconds": min(86400, self.config.pipeline.budget_seconds),
                "max_artifact_bytes": 8 * 1024**3,
                "max_snapshot_bytes": 2 * 1024**3,
            },
        }

    def plan(self, context):
        run = self.call("runs.plan", context, principal=self.planner)
        if run["plan"]["questions"]:
            raise RuntimeError(f"plan has open questions: {run['plan']['questions']}")
        return run["id"]

    def run_state(self, run_id):
        return self.store.get("runs", run_id)

    def gate_open(self) -> bool:
        """May model requests go out? The supervisor's ``live/gate.json``:
        closed while the policy infers (timeshare). A missing file means no
        supervisor gates this worker; a stale one (no heartbeat for 20 s) is
        read as closed whatever it said (``gating.closed(worker=True)``), so a
        dead supervisor cannot leave it open."""
        from . import gating

        return not gating.closed(
            jsonio.read(self.config.live_dir / "gate.json"), worker=True
        )

    def stand_down(self, run_id, what):
        """The gate closed: cancel the run's in-flight model request (pausing
        aborts it, the server stops generating), wait for the run's thread to
        let go of its lease, then wait for the gate to open. The run resumes
        from its last finished episode; nothing is repeated."""
        self.progress("standing_down", note="the policy is inferring")
        stood = time.monotonic()
        run = self.run_state(run_id)
        if run["status"] in ("queued", "running"):
            self.call("runs.pause", {"run_id": run_id})
            deadline = time.time() + 40
            while time.time() < deadline:
                self.check_stop()
                if self.run_state(run_id)["status"] not in ("queued", "running"):
                    break
                time.sleep(0.2)
        self.progress("gated", note="the policy is inferring: the model waits")
        while not self.gate_open():
            self.check_stop()
            self.heartbeat("gated")
            time.sleep(0.2)
        self.gated[0] += 1
        self.gated[1] += time.monotonic() - stood
        self.progress(what, note="the gate opened: resuming")

    def drive(self, run_id, *, what):
        """Take a planned run through approval and execution until it waits
        for review (or ends). Returns the run."""
        waits = 0
        resumes = 0
        recovered = 0.0
        while True:
            self.check_stop()
            run = self.run_state(run_id)
            status = run["status"]
            self.heartbeat(what)
            if (
                status not in RUN_DONE
                and status != "waiting_for_review"
                and not self.gate_open()
            ):
                self.stand_down(run_id, what)
                continue
            if status == "planned" and not (run["plan"].get("approval")):
                if self.auto is None:
                    raise AwaitHuman("plan", run_id)
                self.call(
                    "plans.approve",
                    {"run_id": run_id, "revision": run["plan"]["revision"]},
                )
                continue
            if status in ("planned", "paused", "interrupted", "blocked"):
                if status == "blocked" or status == "interrupted":
                    if not self.model_ready():
                        raise NeedModel(run.get("reason") or status)
                    reason = run.get("reason") or ""
                    if "configuration changed" in reason or "digest" in reason.lower():
                        self.call("runs.cancel", {"run_id": run_id})
                        return self.run_state(run_id)
                    resumes += 1
                    if resumes > 3:
                        # Give up on this run for good rather than leave a
                        # blocked/interrupted one behind for every batch.
                        with contextlib.suppress(Exception):
                            self.call("runs.cancel", {"run_id": run_id})
                        return self.run_state(run_id)
                    self.progress(what, note=f"resuming ({reason[:120]})")
                # The model is needed from here on, not before: a run waiting
                # for a person must not make the service start one.
                if not self.model_ready():
                    raise NeedModel("the vLLM server is not answering")
                try:
                    self.call("runs.execute", {"run_id": run_id, "pilot": False})
                except Exception as exc:
                    text = str(exc)
                    if "active executor" in text and waits < 100:
                        waits += 1
                        self.store.recover()
                        time.sleep(3)
                        continue
                    raise
                continue
            if status in ("queued", "running"):
                # Short sleeps: a closed gate must be seen within a fraction
                # of a second (the file read is tiny); the store's lease
                # recovery is slower and runs every five seconds.
                if time.monotonic() - recovered >= 5.0:
                    self.store.recover()
                    recovered = time.monotonic()
                time.sleep(0.2)
                continue
            return run

    def review_and_commit(self, run_id):
        """Validate, approve and commit a finished temporal run's draft."""
        run = self.run_state(run_id)
        change = self.call("changes.diff", {"changeset_id": run["changes"]})
        if not change["proposals"]:
            self.call("runs.cancel", {"run_id": run_id})
            return None
        revision = change["revision"]
        self.call("changes.validate", {"changeset_id": change["id"]})
        if self.auto is None:
            raise AwaitHuman("changes", run_id, change["id"])
        self.call(
            "changes.approve", {"changeset_id": change["id"], "revision": revision}
        )
        change = self.call("changes.diff", {"changeset_id": change["id"]})
        self.call(
            "changes.commit",
            {"changeset_id": change["id"], "revision": change["revision"]},
            key=f"live:{change['id']}:{change['revision']}",
        )
        return change["id"]

    # --- the two phases -------------------------------------------------------------------

    def coarse_step(self, seconds, cameras):
        """A coarse step that keeps the episode's frames within the model's
        image limit (LEVI refuses to thin silently)."""
        limit = max(3, int(0.75 * self.provider_spec["max_images"]))
        base = self.config.pipeline.coarse_step_seconds
        per_camera = max(2, limit // max(1, cameras))
        need = seconds / (per_camera - 1)
        return max(base, math.ceil(need * 4) / 4)

    def temporal(self, batch, index, lengths):
        p = self.config.pipeline
        state = self.state()
        task = state.get("task_text") or self.name
        cameras = [SIDE]
        todo = [
            d for d in batch["demos"] if d in index and d not in batch.get("done", [])
        ]
        buckets: dict = {}
        for demo in todo:
            step = self.coarse_step(lengths.get(demo, 0.0), len(cameras))
            buckets.setdefault(step, []).append(demo)
        cap = min(240, max(96, 3 * self.provider_spec["max_images"]))
        for step, demos in sorted(buckets.items()):
            self.check_stop()
            attempt = 0
            while demos and attempt < 3:
                attempt += 1
                known = batch.setdefault("temporal", {}).get(str(step))
                run_id = known["run_id"] if known else None
                if run_id:
                    seen = self.run_state(run_id)
                    if self.committed(seen):
                        # Approved and committed meanwhile (by a person when
                        # the approver is off): the work is done, whoever did it.
                        self.record_temporal(demos, index, seen, seen["changes"], batch)
                        break
                    if seen["status"] in RUN_DONE:
                        run_id = None
                if run_id is None:
                    context = self.context(
                        [index[d] for d in demos],
                        cameras,
                        generic.guideline(task, p.guideline),
                        {
                            "kind": "temporal",
                            "definitions": generic.definitions(),
                            "coarse_step_seconds": step,
                            "max_evidence_frames": cap,
                            "refine": p.refine,
                            "pilot_episode": index[demos[0]],
                        },
                    )
                    run_id = self.plan(context)
                    batch["temporal"][str(step)] = {"run_id": run_id, "demos": demos}
                    self.save_current(batch)
                self.progress("temporal", run=run_id, step=step, episodes=len(demos))
                run = self.drive(run_id, what="temporal")
                if self.committed(run):
                    self.record_temporal(demos, index, run, run["changes"], batch)
                    break
                if run["status"] == "cancelled":
                    batch["temporal"].pop(str(step), None)
                    self.save_current(batch)
                    continue
                if run["status"] == "waiting_for_review":
                    try:
                        changeset = self.review_and_commit(run_id)
                    except Exception as exc:
                        if "changed" in str(exc) or "baseline" in str(exc).lower():
                            # A person committed to this dataset meanwhile: the plan's
                            # baseline is stale. Plan again.
                            self.call("runs.cancel", {"run_id": run_id})
                            batch["temporal"].pop(str(step), None)
                            continue
                        raise
                    run = self.run_state(run_id)
                    self.record_temporal(demos, index, run, changeset, batch)
                    break
                self.record_temporal(demos, index, run, None, batch)
                break

    def committed(self, run) -> bool:
        """Did this run's draft get committed (by anyone)?"""
        if run["status"] not in ("succeeded", "partially_succeeded"):
            return False
        try:
            return self.store.get("changes", run["changes"])["status"] == "committed"
        except KeyError:
            return False

    def record_temporal(self, demos, index, run, changeset, batch):
        completed = set(run.get("completed", []))
        failed = {f["episode"]: f for f in run.get("failed", [])}
        now = time.time()
        segments = {}
        reviewer = "auto"
        if changeset:
            change = self.store.get("changes", changeset)
            reviewer = (
                "auto"
                if change["provenance"].get("reviewer_type") == "auto"
                else "human"
            )
            for proposal in change["proposals"]:
                segments[proposal["episode_index"]] = (
                    segments.get(proposal["episode_index"], 0) + 1
                )

        def change_state(value):
            for demo in demos:
                ep = index[demo]
                row = value["demos"].setdefault(demo, {})
                if changeset and ep in completed:
                    row.update(
                        state="annotating",
                        temporal={
                            "run_id": run["id"],
                            "changeset": changeset,
                            "committed_at": now,
                            "review": reviewer,
                            "segments": segments.get(ep, 0),
                        },
                        episode_index=ep,
                    )
                elif ep in failed:
                    row["attempts"] = row.get("attempts", 0) + 1
                    row["reason"] = f"{failed[ep]['phase']}: {failed[ep]['reason']}"[
                        :300
                    ]
                else:
                    row["attempts"] = row.get("attempts", 0) + 1
                    row["reason"] = (
                        "not annotated: "
                        + str(run.get("reason") or run["status"])[:200]
                    )
            return value

        self.update(change_state)
        batch.setdefault("done", []).extend(
            d
            for d in demos
            if changeset and index[d] in completed and d not in batch.get("done", [])
        )

    def anchored(self, batch, index):
        from levi.agent import anchored as anchored_mod

        p = self.config.pipeline
        state = self.state()
        task = state.get("task_text") or self.name
        demos = [d for d in batch["demos"] if d in index]
        if not demos:
            return
        known = batch.get("anchored")
        run_id = known["run_id"] if known else None
        if run_id and self.run_state(run_id)["status"] in {"failed", "cancelled"}:
            run_id = None
        if run_id is None:
            spec = generic.anchored_spec(task, p.anchored_spec, p.anchored_min_valid)
            context = self.context(
                [index[d] for d in demos],
                anchored_mod.cameras(spec),
                f"Release review ({spec['id']}) of: {generic.clean_task(task)}",
                {"kind": "review", "anchored": spec, "pilot_episode": index[demos[0]]},
            )
            run_id = self.plan(context)
            batch["anchored"] = {"run_id": run_id}
            self.save_current(batch)
        self.progress("anchored", run=run_id, episodes=len(demos))
        run = self.drive(run_id, what="anchored")
        results = {}
        for demo in demos:
            try:
                record = self.call(
                    "anchored.get",
                    {"run_id": run_id, "episode": index[demo], "repo_id": self.repo_id},
                )
            except (KeyError, ValueError, PermissionError):
                results[demo] = None
                continue
            events = record.get("events") or []
            basis = record.get("basis") or {}
            results[demo] = {
                "outcome": record.get("outcome"),
                "events": len(events),
                "valid_events": sum(1 for e in events if e.get("valid")),
                "undecided": anchored_mod.undecided(record.get("outcome"), basis),
                "basis": {
                    k: basis[k]
                    for k in (
                        "valid_events",
                        "min_valid",
                        "valid_labels",
                        "missing_labels",
                        "vetoes",
                    )
                    if k in basis
                },
                "run_id": run_id,
                "spec": (record.get("spec") or {}).get("id"),
                "at": time.time(),
                "review": "auto",
                "evaluated": False,
            }

        def change_state(value):
            for demo, verdict in results.items():
                row = value["demos"].setdefault(demo, {})
                row["verdict"] = verdict
                if verdict is None:
                    row["verdict_reason"] = (
                        "no anchored record (episode set aside or no gripper openings)"
                    )
            return value

        self.update(change_state)
        batch["anchored"]["status"] = run["status"]

    # --- finishing ---------------------------------------------------------------------------

    def save_current(self, batch):
        self.update(lambda v: v.update(current=batch))

    def log_episodes(self, batch, index):
        """After a batch: one line per demo in worker.log and one record per
        demo in ``live/stats.jsonl`` (``stats.py``). Both are records, never
        part of the labelling: neither can fail the batch."""
        from levi.harness.ledger import all_events

        state = self.state()
        # A temporal batch may span several runs (one per coarse step).
        journals = [
            ("temporal", all_events(self.store, k["run_id"]))
            for k in (batch.get("temporal") or {}).values()
        ]
        if batch.get("anchored"):
            journals.append(
                ("review", all_events(self.store, batch["anchored"]["run_id"]))
            )
        frames = self.frame_counts()
        waking = self.vllm_timings()
        for number, demo in enumerate(batch["demos"]):
            row = state["demos"].get(demo) or {}
            episode = (index or {}).get(demo, row.get("episode_index"))
            stages = {}
            for stage, wall in self.walls.items():
                use = {"requests": 0, "tokens": 0, "model_s": 0.0}
                for name, events in journals:
                    if name == stage:
                        for key, value in model_use(events, episode).items():
                            use[key] += value
                stages[stage] = (wall, use)
            self.log(
                episode_line(
                    demo, episode, len(batch["demos"]), stages, row, self.gated
                )
            )
            with contextlib.suppress(Exception):
                c = self.config
                stats.record(
                    c.live_dir,
                    self.stats_row(
                        demo,
                        episode,
                        row,
                        journals,
                        first=number == 0,
                        frames=frames.get(episode),
                        waking=waking if number == 0 else {},
                    ),
                    c.resources.log_max_mb * 1024 * 1024,
                    c.resources.log_backups,
                )

    def frame_counts(self) -> dict:
        """``{episode_index: frames}`` from the view's episode list."""
        counts = {}
        if self.view is not None:
            with contextlib.suppress(OSError, ValueError, KeyError):
                for line in (
                    (self.view / "meta/episodes.jsonl").read_text().splitlines()
                ):
                    if line.strip():
                        item = json.loads(line)
                        counts[int(item["episode_index"])] = item.get("length")
        return counts

    @staticmethod
    def vllm_timings() -> dict:
        """What the supervisor says the model's wake or cold start cost for
        this batch (``LEVI_LIVE_VLLM_TIMINGS``)."""
        with contextlib.suppress(ValueError, TypeError):
            value = json.loads(os.environ.get("LEVI_LIVE_VLLM_TIMINGS") or "{}")
            if isinstance(value, dict):
                return value
        return {}

    def stats_row(self, demo, episode, row, journals, *, first, frames, waking):
        """The ``live/stats.jsonl`` record of one demo (schema in stats.py)."""
        p = self.config.pipeline
        temporal = row.get("temporal") or {}
        verdict = row.get("verdict") or {}
        base = row.get("completed_at")
        use = stats.usage_of(journals, episode, probe=first)
        planned = [
            e["time"]
            for stage, events in journals
            if stage == "temporal"
            and any(
                x.get("type") == "model_step" and x.get("episode") == episode
                for x in events
            )
            for e in events
            if e.get("type") == "planned" and e.get("time") is not None
        ]
        labels: dict = {}
        if temporal.get("changeset"):
            with contextlib.suppress(KeyError):
                change = self.store.get("changes", temporal["changeset"])
                for proposal in change["proposals"]:
                    if proposal.get("episode_index") == episode:
                        label = proposal.get("subtask_id") or "unlabeled"
                        labels[label] = labels.get(label, 0) + 1
        fps_seconds = self.lengths.get(demo)
        return {
            "schema": stats.SCHEMA,
            "at": round(time.time(), 3),
            "dataset": self.name,
            "demo": demo,
            "episode_index": episode,
            "session": row.get("run_id"),
            "attempts": row.get("attempts"),
            "excluded": False,
            "episode": {
                "frames": frames,
                "episode_seconds": None
                if fps_seconds is None
                else round(fps_seconds, 2),
            },
            "timeline": {
                "to_mirror_s": stats.after(row.get("mirrored_at"), base),
                "to_plan_s": stats.after(min(planned) if planned else None, base),
                "to_first_request_s": stats.after(use.pop("first_request_at"), base),
                "to_commit_s": stats.after(temporal.get("committed_at"), base),
                "to_verdict_s": stats.after(verdict.get("at"), base),
            },
            "model": use,
            "gate": {
                "closed_wait_s": round(self.gated[1], 2),
                "interruptions": self.gated[0],
                "vllm_wake_s": waking.get("vllm_wake_s"),
                "vllm_cold_start_s": waking.get("vllm_cold_start_s"),
            },
            "result": {
                "state": row.get("state"),
                "reason": None
                if temporal
                else (str(row["reason"])[:200] if row.get("reason") else None),
                "segments": temporal.get("segments") if temporal else None,
                "segment_labels": labels if temporal else None,
                "verdict": {
                    "outcome": verdict.get("outcome"),
                    "events": verdict.get("events"),
                    "valid_events": verdict.get("valid_events"),
                    "undecided": verdict.get("undecided"),
                }
                if verdict
                else None,
                "review": temporal.get("review") or verdict.get("review"),
                "spec": {
                    "guideline": p.guideline,
                    "release_review": verdict.get("spec") or p.anchored_spec
                    if p.anchored
                    else None,
                    "sha256": generic.manifest(self.config),
                },
                "provider": self.provider_spec.get("name"),
                "model": self.provider_spec.get("model"),
            },
        }

    def finish(self, batch, index=None):
        now = time.time()

        def change_state(value):
            for demo in batch["demos"]:
                row = value["demos"].get(demo)
                if row and row.get("state") in ("mirrored", "annotating"):
                    if row.get("temporal"):
                        row["state"] = "done"
                    elif row.get("attempts", 0) >= self.config.pipeline.max_attempts:
                        row["state"] = "failed"
                    else:
                        row["state"] = "mirrored"
            value["last_batch"] = {
                "demos": batch["demos"],
                "finished_at": now,
                "started_at": batch.get("started_at"),
                "temporal_runs": [
                    r["run_id"] for r in (batch.get("temporal") or {}).values()
                ],
                "anchored_run": (batch.get("anchored") or {}).get("run_id"),
            }
            value["current"] = None
            value["last_error"] = ""
            value["last_processed_at"] = now
            return value

        self.update(change_state)
        with contextlib.suppress(Exception):
            self.log_episodes(batch, index)
        if self.config.pipeline.cleanup:
            self.cleanup()

    def cleanup(self):
        """Archive old open release-review runs, then delete the frozen inputs
        and evidence of every finished run.

        A release-review run is left open (``waiting_for_review``) so a person
        can still accept its outcome proposals; committing one needs its frozen
        input, so the newest ``pipeline.keep_review_runs`` of a dataset keep
        theirs. Older ones are cancelled -- their verdicts live on in the
        anchored records and the dataset state -- and cleaned like any
        finished run."""
        from levi.agent import housekeeping

        def run_dir(run_id):
            return self.store.run_dir(run_id)

        open_runs = sorted(
            (
                r
                for r in self.store.list("runs")
                if r["context"]["repo_id"] == self.repo_id
                and r.get("principal") == "live-auto"
                and r["status"] == "waiting_for_review"
                and (r["context"].get("workflow") or {}).get("anchored")
            ),
            key=lambda r: r.get("created_at", 0),
            reverse=True,
        )
        keep = self.config.pipeline.keep_review_runs
        for run in open_runs[keep:]:
            with contextlib.suppress(Exception):
                self.call("runs.cancel", {"run_id": run["id"]})
        kept = [r["id"] for r in open_runs[:keep]]
        self.update(lambda v: v.update(review_runs_open=len(kept), review_runs=kept))
        with contextlib.suppress(Exception):
            housekeeping.apply(self.store, run_dir, dataset=self.repo_id)

    # --- the whole batch ------------------------------------------------------------------------

    def run(self):
        state = self.state()
        batch = state.get("current")
        if batch is None:
            demos = self.start_batch()
            if not demos:
                return NOTHING
            batch = {
                "demos": demos,
                "started_at": time.time(),
                "temporal": {},
                "anchored": None,
                "done": [],
            }
            self.save_current(batch)
        self.progress(
            "view", started_at=batch.get("started_at"), batch=len(batch["demos"])
        )
        # What was waiting for a person is checked first, from the store alone:
        # no model server, provider binding or view is needed to find out that
        # the plan is still unapproved or the draft uncommitted.
        self.update(lambda v: (v.pop("awaiting", None), v)[1])
        self.guard_human(batch)
        if not self.model_ready():
            raise NeedModel("the vLLM server is not answering")
        self.ensure_provider()
        entry = self.register_and_build()
        index, lengths, excluded = self.episode_map(entry)
        self.lengths = lengths
        batch["demos"] = self.filter_demos(batch["demos"], index, excluded, batch)
        self.save_current(batch)
        self.walls, self.gated = {}, [0, 0.0]
        if batch["demos"]:
            began = time.monotonic()
            self.temporal(batch, index, lengths)
            self.walls["temporal"] = time.monotonic() - began
            if self.config.pipeline.anchored:
                began = time.monotonic()
                self.anchored(batch, index)
                self.walls["review"] = time.monotonic() - began
        self.finish(batch, index)
        return OK

    def guard_human(self, batch):
        """With the approver off: still waiting for a person? Raises
        ``AwaitHuman`` for a temporal run whose plan is not approved or whose
        draft is neither committed nor rejected (a release review is judged
        in ``drive`` once its plan has been approved)."""
        if self.auto is not None:
            return
        runs = [k["run_id"] for k in (batch.get("temporal") or {}).values()]
        if batch.get("anchored"):
            runs.append(batch["anchored"]["run_id"])
        for run_id in runs:
            try:
                run = self.run_state(run_id)
            except KeyError:
                continue
            if run["status"] == "planned" and not run["plan"].get("approval"):
                raise AwaitHuman("plan", run_id)
        for known in (batch.get("temporal") or {}).values():
            try:
                run = self.run_state(known["run_id"])
                if run["status"] != "waiting_for_review" or not run.get("changes"):
                    continue
                change = self.store.get("changes", run["changes"])
            except KeyError:
                continue
            if change["proposals"] and change["status"] not in (
                "committed",
                "rejected",
            ):
                raise AwaitHuman("changes", run["id"], change["id"])

    def committed_here(self, batch) -> set:
        """Episodes of this batch whose draft was committed (by the approver
        or, with it off, by a person): their annotations are this batch's,
        not "somebody else's"."""
        episodes = set()
        for known in ((batch or {}).get("temporal") or {}).values():
            try:
                run = self.run_state(known["run_id"])
            except KeyError:
                continue
            if self.committed(run):
                episodes |= set(run.get("completed", []))
        return episodes

    def filter_demos(self, demos, index, excluded, batch=None):
        keep = []
        ours = self.committed_here(batch)

        def change_state(value):
            for demo in demos:
                row = value["demos"].setdefault(demo, {})
                if demo not in index:
                    row.update(
                        state="rejected",
                        reason="left out of the view: "
                        + json.dumps(excluded.get(demo))[:200],
                    )
                elif (
                    index[demo] not in ours
                    and self.human_annotated(index[demo])
                    and not row.get("temporal")
                ):
                    row.update(
                        state="skipped_human",
                        reason="a person already annotated this episode",
                    )
                else:
                    keep.append(demo)
            return value

        self.update(change_state)
        return keep


def configure_process(config):
    """Niceness, thread limits and the workspace environment, before any
    heavy import."""
    from . import resources

    resources.apply(config)
    os.environ["LEVI_WORKSPACE"] = str(config.workspace)
    os.environ["LEVI_LIVE_WORKER"] = "1"  # obeys the gate by standing down
    os.environ.setdefault("LEVI_DROID_SAMPLE", "off")
    os.environ["LEVI_GPU_SHARING"] = "allow"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m levi.live.worker")
    parser.add_argument("--config", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--provider-json", required=True)
    args = parser.parse_args(argv)
    config = live_config.load(args.config, args.workspace)
    configure_process(config)
    import logging

    # One line per model request from the HTTP client is noise at 10 s of requests.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    spec = json.loads(args.provider_json)
    from levi.paths import configure

    configure()
    from . import resources

    resources.limit_cv2(config.resources.threads)
    worker = Worker(config, args.dataset, spec)

    def term(*_):
        worker.stopping = True

    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGINT, term)
    code = ERROR
    awaiting = None
    try:
        code = worker.run()
        worker.log("batch finished" if code == OK else "nothing to do")
    except Stop:
        worker.log("stopping: pausing the current run")
        batch = (mirror.load_state(config, args.dataset) or {}).get("current") or {}
        for run_id in _open_runs(batch):
            with contextlib.suppress(Exception):
                worker.call("runs.pause", {"run_id": run_id})
        # Leave only once each run's thread has let go of its lease: a lease
        # left behind keeps the next worker waiting for it to expire (minutes).
        deadline = time.time() + 40
        while time.time() < deadline:
            running = [
                r
                for r in _open_runs(batch)
                if worker.run_state(r)["status"] in ("running", "queued")
            ]
            if not running:
                break
            time.sleep(0.3)
        code = PREEMPTED
    except NeedModel as exc:
        worker.log("waiting for the model:", exc)
        code = NEED_MODEL
    except AwaitHuman as exc:
        worker.log("waiting for a person:", exc)
        awaiting = {"kind": exc.kind, "run_id": exc.run_id, "changeset": exc.changeset}
        # On disk too: a restarted supervisor reads it back instead of starting
        # a model and a worker to find out.
        with contextlib.suppress(Exception):
            worker.update(lambda v: v.update(awaiting={**awaiting, "at": time.time()}))
        code = AWAIT_HUMAN
    except Exception as exc:  # noqa: BLE001 - the supervisor retries later
        worker.log("error:", type(exc).__name__, str(exc)[:500])
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        with contextlib.suppress(Exception):
            worker.update(lambda v: v.update(last_error=message))
        code = ERROR
    finally:
        with contextlib.suppress(Exception):
            jsonio.write(
                config.live_dir / "worker.json",
                {
                    "pid": os.getpid(),
                    "dataset": args.dataset,
                    "phase": "exited",
                    "exit": code,
                    "awaiting": awaiting,
                    # audit records that could not be written (see auto.audit)
                    "audit_failures": approver_log.AUDIT_FAILURES,
                    "updated_at": time.time(),
                },
            )
    return code


def _open_runs(batch):
    ids = [r["run_id"] for r in (batch.get("temporal") or {}).values()]
    if batch.get("anchored"):
        ids.append(batch["anchored"]["run_id"])
    return ids


if __name__ == "__main__":
    sys.exit(main())
