"""Natural-language tasks: a sentence in, a checked plan and a report out.

"Check the quality of <dataset>, then annotate subtasks on the first 10 demos,
and give me the token and time totals" becomes a task with ordered steps. The
model LEVI runs (a local Ollama VLM) does the language part only: it proposes
a structured spec. LEVI checks every field against the catalog -- the dataset
exists, the episodes are in range, the cameras are real -- and the person
approves the spec before anything runs. Plan approval, pilot review and commit
stay the same human gates as everywhere else.

Records: ``datasets/<name>/tasks/<task-id>/task.json`` next to each step's run
ledger; the id is ``task-YYYYmmddTHHMM``. What the interpreter got right or
wrong is teachable: ``feedback`` stores the teacher's corrected spec as an
example and the note as a workspace lesson for the next interpretation.
"""

import json
import time
from typing import Literal

from pydantic import Field

from levi.agent.schema import Contract

from .layout import dataset_dir, memory_path, read_json, write_json

STEP_KINDS = ("quality", "annotate")


class Definition(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")
    label: str = Field(min_length=1, max_length=80)
    definition: str = Field(min_length=1, max_length=400)
    starts_when: str = Field(min_length=1, max_length=300)
    ends_when: str = Field(min_length=1, max_length=300)
    success_when: str = Field(min_length=1, max_length=300)


class Step(Contract):
    kind: Literal["quality", "annotate"]
    workflow: Literal["temporal", "review"] | None = None
    episodes: list[int] = Field(default_factory=list, max_length=1000)
    cameras: list[str] = Field(default_factory=list, max_length=8)
    instruction: str = Field(default="", max_length=4000)
    definitions: list[Definition] = Field(default_factory=list, max_length=12)


class TaskSpec(Contract):
    dataset: str = Field(description="Catalog id, e.g. local/<name>")
    steps: list[Step] = Field(min_length=1, max_length=6)
    report: list[Literal["tokens", "time"]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list, max_length=10)
    questions: list[str] = Field(default_factory=list, max_length=5)


def catalog_digest():
    """What the interpreter may choose from: datasets, sizes, cameras, tasks."""
    from levi.catalog import datasets

    rows = []
    for name, entry in sorted(datasets().items()):
        info = entry.get("info") or {}
        cameras = [
            key.rsplit(".", 1)[-1]
            for key, feature in (info.get("features") or {}).items()
            if feature.get("dtype") == "video"
        ]
        rows.append(
            {
                "id": entry["id"],
                "episodes": info.get("total_episodes"),
                "fps": info.get("fps"),
                "cameras": cameras,
                "kind": entry.get("kind"),
            }
        )
    return rows


def _examples(state, limit=4):
    memory = read_json(memory_path(state, "workspace")) or {}
    return memory.get("interpretations", [])[-limit:]


def _definitions_from_memory(state, key):
    """Subtask definitions this dataset was annotated with before."""
    memory = read_json(memory_path(state, key)) or {}
    return memory.get("definitions", [])


def prompt(state, text):
    from . import knowledge
    from .teaching import workspace_notes

    datasets = catalog_digest()
    known = {}
    for row in datasets:
        key = row["id"].split("/", 1)[-1]
        memory = read_json(memory_path(state, key)) or {}
        if memory.get("episodes") or memory.get("definitions"):
            known[row["id"]] = {
                "annotated_episodes": len(memory.get("episodes", {})),
                "subtasks": sorted(memory.get("subtasks", {})),
                "definitions": memory.get("definitions", [])[:8],
            }
    system = (
        "You turn a LEVI user's request into a task spec. LEVI is a robot-data "
        "workbench. Step kinds: 'quality' (run the dataset quality checks) and "
        "'annotate' (workflow 'temporal' = subtask segments with outcomes over "
        "time; 'review' = episode-level review). Choose the dataset only from the "
        "catalog; name it by its id. Episode indices start at 0: 'the first 10 "
        "demos' is [0..9]. Cameras are named by their short key. For temporal "
        "annotation give subtask definitions with observable start, end and "
        "success; reuse this dataset's known definitions when they exist. Put "
        "'tokens' and/or 'time' in report when the user asks for cost or time. "
        "State assumptions you made; ask a question instead of guessing when the "
        "dataset or scope is ambiguous. Output only the JSON spec."
    )
    user = {
        "request": text,
        "catalog": datasets,
        "local_memory": known,
        "rules": knowledge.texts("interpretation"),
        "lessons": [row["note"] for row in workspace_notes(state, "interpret")],
        "approved_examples": _examples(state),
    }
    return system, json.dumps(user, ensure_ascii=False)


def check(spec: TaskSpec):
    """Deterministic validation against the catalog; returns (spec, problems)."""
    catalog = {row["id"]: row for row in catalog_digest()}
    problems = []
    entry = catalog.get(spec.dataset)
    if entry is None:
        return spec, [f"Unknown dataset {spec.dataset!r}"]
    total = entry["episodes"] or 0
    for index, step in enumerate(spec.steps):
        if step.kind == "annotate":
            if not step.workflow:
                problems.append(f"step {index}: annotate needs a workflow")
            if not step.episodes:
                problems.append(f"step {index}: no episodes chosen")
            bad = [e for e in step.episodes if e < 0 or e >= total]
            if bad:
                problems.append(
                    f"step {index}: episodes out of range {bad} (0..{total - 1})"
                )
            if len(set(step.episodes)) != len(step.episodes):
                problems.append(f"step {index}: duplicate episodes")
            unknown = [c for c in step.cameras if c not in entry["cameras"]]
            if unknown:
                problems.append(f"step {index}: unknown cameras {unknown}")
            if step.workflow == "temporal" and not step.cameras:
                problems.append(f"step {index}: temporal annotation needs a camera")
            if step.workflow == "temporal" and not step.definitions:
                problems.append(f"step {index}: temporal annotation needs definitions")
            if not step.instruction.strip():
                problems.append(f"step {index}: empty instruction")
    return spec, problems


def new_id(state, key):
    from levi.agent.runtime import new_id as minute_id

    folder = dataset_dir(state, key) / "tasks"
    taken = (
        {p.name.split("-", 1)[-1] for p in folder.glob("task-*")}
        if folder.exists()
        else set()
    )
    return f"task-{minute_id(taken)}"


def path(state, key, task_id):
    return dataset_dir(state, key) / "tasks" / task_id / "task.json"


def interpret(store, text, provider_name, *, principal=None):
    """Ask the learner for a spec, check it, and store it for approval."""
    from levi.agent.schema import ProviderConfig
    from levi.inference.provider import client_for

    config = ProviderConfig.model_validate(store.get("providers", provider_name))
    if config.kind != "ollama" or not config.model_digest:
        raise ValueError("Natural-language tasks need a bound local Ollama model")
    from levi.inference.gpu import require_free

    require_free(config)
    system, user = prompt(store.state, text)
    started = time.monotonic()
    response = client_for(config, timeout=300).chat(
        config.model,
        config.model_digest,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        output_schema=TaskSpec.model_json_schema(),
        max_output_tokens=min(4096, config.context_tokens // 2),
        context_tokens=config.context_tokens,
    )
    seconds = round(time.monotonic() - started, 2)
    try:
        spec = TaskSpec.model_validate_json(response["content"])
        spec, problems = check(spec)
        raw = None
    except ValueError as exc:
        spec, problems, raw = (
            None,
            [f"Unparseable spec: {exc}"[:500]],
            response["content"][:4000],
        )
    key = (
        spec.dataset.split("/", 1)[-1]
        if spec and spec.dataset.startswith("local/")
        else "workspace"
    )
    task_id = new_id(store.state, key)
    task = {
        "schema": "levi.harness.task.v1",
        "id": task_id,
        "dataset_key": key,
        "request": text,
        "provider": provider_name,
        "model": config.model,
        "spec": spec.model_dump() if spec else None,
        "raw": raw,
        "problems": problems,
        "status": "needs_revision" if problems else "awaiting_approval",
        "interpretation": {
            "tokens": response["usage"]["tokens"],
            "token_source": response["usage"]["source"],
            "seconds": seconds,
        },
        "steps": [],
        "created_at": time.time(),
        "created_by": getattr(principal, "id", None),
    }
    store.put("tasks", task_id, task)
    write_json(path(store.state, key, task_id), task)
    return task


def save(store, task):
    store.put("tasks", task["id"], task)
    write_json(path(store.state, task["dataset_key"], task["id"]), task)
    return task


def feedback(store, task_id, *, decision, note="", spec=None, by=None):
    """A teacher corrects or accepts an interpretation; both are remembered."""
    from .teaching import note_workspace

    task = store.get("tasks", task_id)
    if decision == "revise":
        if spec is None:
            raise ValueError("A revision needs the corrected spec")
        corrected, problems = check(TaskSpec.model_validate(spec))
        if problems:
            raise ValueError("; ".join(problems))
        task["learner_spec"] = task["spec"]
        task["spec"] = corrected.model_dump()
        task["problems"] = []
        task["status"] = "awaiting_approval"
    elif decision == "accept":
        if task["problems"]:
            raise ValueError("A spec with problems cannot be accepted; revise it")
    elif decision != "reject":
        raise ValueError("decision must be accept, revise or reject")
    else:
        task["status"] = "rejected"
    task.setdefault("feedback", []).append(
        {"decision": decision, "note": note, "by": by, "at": time.time()}
    )
    save(store, task)
    state = store.state
    if note.strip():
        note_workspace(state, note, topic="interpret", run_id=task_id)
    if decision in {"accept", "revise"}:
        memory = read_json(memory_path(state, "workspace")) or {
            "schema": "levi.harness.workspace-memory.v1",
            "teaching": [],
        }
        examples = memory.setdefault("interpretations", [])
        examples[:] = [e for e in examples if e["request"] != task["request"]]
        examples.append({"request": task["request"], "spec": task["spec"]})
        del examples[:-12]
        write_json(memory_path(state, "workspace"), memory)
    return task


def approve(store, task_id, *, by):
    task = store.get("tasks", task_id)
    if task["status"] != "awaiting_approval" or task["problems"]:
        raise ValueError("Only a checked spec awaiting approval can be approved")
    task["status"] = "approved"
    task["approved_by"] = by
    task["approved_at"] = time.time()
    return save(store, task)


def _camera_keys(step, dataset):
    return [
        c if c.startswith("observation.images.") else f"observation.images.{c}"
        for c in step["cameras"]
    ]


def _plan_step(wb, task, step, principal):
    from levi.agent.schema import Budget, TaskContext

    spec = task["spec"]
    workflow = {"kind": step["workflow"]}
    if step["workflow"] == "temporal":
        workflow["definitions"] = step["definitions"]
    context = TaskContext(
        repo_id=spec["dataset"],
        episodes=step["episodes"],
        cameras=_camera_keys(step, spec["dataset"]),
        instruction=step["instruction"],
        provider=task["provider"],
        workflow=workflow,
        allow_media_egress=True,
        supervision=task.get("supervision", "none"),
        teacher_grant=task.get("teacher_grant"),
        budget=Budget(
            max_calls=max(8, 3 * 2 * len(step["episodes"]) + 6),
            max_tokens=max(16000, 60000 * len(step["episodes"])),
            max_seconds=max(300, 240 * len(step["episodes"])),
        ),
    )
    run = wb.plan(context, principal)
    return run["id"]


def advance(wb, task_id, principal=None):
    """Do the next automatic thing; stop at a human gate. Returns the task."""
    from levi.agent.tracking import waiting_for

    store = wb.store
    task = store.get("tasks", task_id)
    if task["status"] not in {"approved", "running", "waiting"}:
        raise ValueError(f"Task is {task['status']}; approve its spec first")
    steps = task.setdefault("steps", [])
    for index, step in enumerate(task["spec"]["steps"]):
        if index >= len(steps):
            steps.append({"kind": step["kind"], "state": "pending"})
        state = steps[index]
        if state["state"] == "done":
            continue
        if step["kind"] == "quality":
            from levi.catalog import local_root

            from .quality import digest, inspect

            root = local_root(task["spec"]["dataset"])
            checked = digest(
                inspect(
                    store.state,
                    task["dataset_key"],
                    root,
                    repo_id=task["spec"]["dataset"],
                    decode_video=True,
                )
            )
            state.update(
                state="done",
                report=checked["path"],
                status=checked["status"],
                findings=checked["findings"],
                seconds=checked["seconds"],
            )
            continue
        if not state.get("run_id"):
            state["run_id"] = _plan_step(wb, task, step, principal)
            state["state"] = "waiting"
            state["waiting_for"] = "human: approve the plan"
            break
        run = store.get("runs", state["run_id"])
        turn = waiting_for(store, run)
        if turn["committed"]:
            state.update(state="done", waiting_for=None)
            continue
        plan = run.get("plan") or {}
        if not plan.get("approval"):
            state["waiting_for"] = "human: approve the plan"
            break
        if run["status"] in {"queued", "running"}:
            state["waiting_for"] = "LEVI: model running"
            break
        teaching = [
            t
            for t in store.list("teaching")
            if t["run_id"] == run["id"] and t["status"] == "pending"
        ]
        if teaching:
            state["waiting_for"] = f"teacher: {len(teaching)} phase(s) to review"
            break
        pilot = plan.get("pilot_episode")
        done = set(run.get("completed", []))
        if run["status"] in {"planned", "paused", "blocked"} and pilot not in done:
            wb.launch(run["id"], pilot=True)
            state["waiting_for"] = "LEVI: model running (pilot)"
            break
        review = plan.get("pilot_review")
        if not review:
            state["waiting_for"] = "human: review the pilot"
            break
        if not review.get("accepted"):
            state.update(state="failed", waiting_for="pilot rejected; revise the plan")
            break
        if done != set(run["context"]["episodes"]):
            wb.launch(run["id"], pilot=False)
            state["waiting_for"] = "LEVI: model running (all episodes)"
            break
        state["waiting_for"] = f"human: {turn['waiting_for'].replace('_', ' ')}"
        break
    task["status"] = (
        "done"
        if all(s["state"] == "done" for s in steps)
        and len(steps) == len(task["spec"]["steps"])
        else "running"
    )
    if task["status"] == "done":
        task["report"] = report(store, task)
    return save(store, task)


def report(store, task):
    """Tokens and time for the whole task: interpretation, checks, each run."""
    from .ledger import build

    rows = [
        {
            "part": "interpretation",
            "tokens": task["interpretation"]["tokens"],
            "token_source": task["interpretation"]["token_source"],
            "seconds": task["interpretation"]["seconds"],
        }
    ]
    for step in task.get("steps", []):
        if step["kind"] == "quality":
            rows.append(
                {
                    "part": "quality",
                    "tokens": 0,
                    "token_source": "no model",
                    "seconds": step.get("seconds"),
                }
            )
        elif step.get("run_id"):
            ledger = build(store, step["run_id"])
            from .cost import of
            from .ledger import all_events

            cost = of(
                ledger,
                store.get("runs", step["run_id"]),
                all_events(store, step["run_id"]),
            )
            rows.append(
                {
                    "part": f"annotate {step['run_id']}",
                    "tokens": cost["tokens"]["value"],
                    "token_source": cost["tokens"]["source"],
                    "seconds": cost["seconds"]["wall"],
                    "model_seconds": cost["seconds"]["model"],
                }
            )
    wall = time.time() - task["created_at"]
    return {
        "tokens": sum(r["tokens"] or 0 for r in rows),
        "seconds_worked": round(sum(r["seconds"] or 0 for r in rows), 1),
        "wall_seconds": round(wall, 1),
        "parts": rows,
        "note": "Wall time includes waiting for people at the approval gates.",
    }
