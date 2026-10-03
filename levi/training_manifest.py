"""Training manifests: which frames of a dataset enter a learner's loss,
with what weight, and on what evidence.

A manifest is LEVI's hand-off to a fixed training recipe (docs/TRAINING_MANIFEST.md).
It never rewrites the dataset: it lists every frame of a registered
LeRobot v2.x dataset (or a raw capture's browsing view) once, with

- ``include`` / ``weight``: what one named *operation* decided;
- the evidence the decision rests on, per frame: the robot's own episode
  flag, a human label, the newest anchored review's verdict, the active
  annotation's subtask and its outcome, the RECAP value / advantage label;

and ``manifest.json`` records where each of those came from (LEVI commit,
dataset fingerprint, namespace, annotation revision, anchored run and spec,
RECAP revision, checkpoint and threshold, the operation and its parameters).

Operations are task-generic: they read episode outcomes and frame labels,
never a task's own vocabulary. An episode's verdict is, in order, a human
label, the anchored review (only for the tasks it is declared valid for)
and the robot's flag.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import catalog, manifest_prompts, naming, paths

SCHEMA = "levi.training_manifest.v1"
FRAMES = "frames.parquet"
MANIFEST = "manifest.json"
META_FILES = (
    "info.json",
    "episodes.jsonl",
    "tasks.jsonl",
    "episodes_stats.jsonl",
    "levi_view.json",
)

FRAMES_SCHEMA = pa.schema(
    [
        ("episode_index", pa.int64()),
        ("frame_index", pa.int64()),
        ("timestamp", pa.float64()),
        ("source_demo", pa.string()),
        ("task", pa.string()),
        ("include", pa.bool_()),
        ("weight", pa.float32()),
        ("exclude_reason", pa.string()),
        ("episode_success", pa.string()),
        ("episode_success_source", pa.string()),
        ("robot_flag", pa.string()),
        ("human_label", pa.string()),
        ("anchored_outcome", pa.string()),
        ("anchored_undecided", pa.bool_()),
        ("subtask_id", pa.string()),
        ("subtask_outcome", pa.string()),
        ("subtask_attempt", pa.int64()),
        # Who stands behind the subtask segment: "auto" (written by the live
        # service, unreviewed), "edited" (an automatic one a person changed),
        # null (a person or an agent a person reviewed).
        ("subtask_review", pa.string()),
        ("recap_value", pa.float32()),
        ("recap_advantage", pa.float32()),
        ("recap_positive", pa.bool_()),
        # Per-frame prompts (docs/TRAINING_MANIFEST.md, "Prompt columns"):
        # additive, a reader that does not know them ignores them.
        ("prompt_task", pa.string()),
        ("prompt_subtask", pa.string()),
        ("prompt_has_subtask", pa.bool_()),
        ("prompt_subtask_skip", pa.string()),
        ("prompt_subtask_source", pa.string()),
        ("prompt_subtask_origin", pa.string()),
        ("episode_task_count", pa.int64()),
    ]
)
PROMPT_COLUMNS = {
    "prompt_task": "the episode's task text alone (the first of its tasks)",
    "prompt_subtask": (
        "the task text with the current subtask from the prompt template; "
        "the task alone when prompt_has_subtask is false"
    ),
    "prompt_has_subtask": "whether prompt_subtask carries a subtask",
    "prompt_subtask_skip": "why prompt_subtask carries no subtask (null when it does)",
    "prompt_subtask_source": (
        "which time segment the subtask came from and who stands behind it"
    ),
    "prompt_subtask_origin": (
        "human, agent_run (an agent run's segment with no review mark: a person "
        "or a script with a person's identity approved it) or edited"
    ),
    "episode_task_count": "how many task texts the episode lists (only the first is used)",
}


def _frames_schema(template: str, max_chars: int) -> pa.Schema:
    """FRAMES_SCHEMA with each prompt column's template version and rule."""
    fields = []
    for column in FRAMES_SCHEMA:
        if column.name in PROMPT_COLUMNS:
            column = column.with_metadata(
                {
                    "levi.description": PROMPT_COLUMNS[column.name],
                    "levi.prompt_template": template,
                    "levi.prompt_template_text": manifest_prompts.template_text(
                        template
                    ),
                    "levi.subtask_max_chars": str(max_chars),
                }
            )
        fields.append(column)
    return pa.schema(fields)


class ManifestError(ValueError):
    """A refusal: the request cannot produce a trustworthy manifest."""


# ------------------------------------------------------------- operations


@dataclass(frozen=True)
class Param:
    kind: type
    default: Any
    help: str
    choices: tuple = ()


@dataclass(frozen=True)
class Operation:
    name: str
    summary: str
    needs: frozenset = frozenset()
    params: dict[str, Param] = field(default_factory=dict)


_FALLBACK = Param(
    str,
    "robot_flag",
    "episodes without a human label or an applicable anchored verdict: "
    "robot_flag uses the robot's flag, exclude leaves them out",
    ("robot_flag", "exclude"),
)
_UNLABELLED = Param(
    str,
    "exclude",
    "frames the RECAP revision did not label (e.g. static-filtered)",
    ("exclude", "include"),
)

_UNDECIDED = Param(
    str,
    "exclude",
    "anchored successes the review left undecided (undecided labels or "
    "vetoes, contested start-check waivers): exclude leaves them out, "
    "include keeps them",
    ("exclude", "include"),
)

OPERATIONS: dict[str, Operation] = {
    op.name: op
    for op in (
        Operation(
            "all_rollouts",
            "Every frame of every episode in scope, weight 1 (behaviour "
            "cloning on everything; the no-curation baseline).",
        ),
        Operation(
            "robot_flag_success",
            "Episodes the robot itself flagged success (the capture's "
            "levi_outcome or is_success), weight 1.",
        ),
        Operation(
            "verified_success",
            "Episodes whose verdict is success: a human label first, then "
            "the anchored review (for the tasks it is valid for), then the "
            "fallback.",
            frozenset({"anchored"}),
            {"fallback": _FALLBACK, "undecided": _UNDECIDED},
        ),
        Operation(
            "advantage_positive_mask",
            "Every episode in scope, only the frames the RECAP value model "
            "labels positive (A_t >= threshold) enter the loss.",
            frozenset({"recap"}),
            {"unlabelled": _UNLABELLED},
        ),
        Operation(
            "advantage_weighted",
            "Every frame in scope; the sampling / loss weight is "
            "positive_weight for positive-advantage frames and "
            "negative_weight otherwise.",
            frozenset({"recap"}),
            {
                "positive_weight": Param(float, 1.0, "weight of positive frames"),
                "negative_weight": Param(float, 0.2, "weight of negative frames"),
                "unlabelled_weight": Param(
                    float, 0.0, "weight of frames without a RECAP label"
                ),
            },
        ),
    )
}


def operations() -> list[dict[str, Any]]:
    return [
        {
            "name": op.name,
            "summary": op.summary,
            "needs": sorted(op.needs),
            "params": {
                k: {
                    "type": p.kind.__name__,
                    "default": p.default,
                    "help": p.help,
                    **({"choices": list(p.choices)} if p.choices else {}),
                }
                for k, p in op.params.items()
            },
        }
        for op in OPERATIONS.values()
    ]


def _params(op: Operation, given: dict[str, Any] | None) -> dict[str, Any]:
    given = dict(given or {})
    unknown = set(given) - set(op.params)
    if unknown:
        raise ManifestError(
            f"Operation {op.name} takes no parameter "
            + ", ".join(sorted(unknown))
            + (f" (it takes {', '.join(op.params)})" if op.params else "")
        )
    out = {}
    for key, spec in op.params.items():
        value = given.get(key, spec.default)
        try:
            value = spec.kind(value)
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"{key} must be a {spec.kind.__name__}") from exc
        if spec.choices and value not in spec.choices:
            raise ManifestError(f"{key} must be one of {', '.join(spec.choices)}")
        if spec.kind is float and (not np.isfinite(value) or value < 0):
            raise ManifestError(f"{key} must be a finite weight >= 0")
        out[key] = value
    return out


# ------------------------------------------------------------- evidence


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(ds) -> dict[str, Any]:
    """Content hashes of the metadata and every episode's data parquet (not
    the videos), and one digest over them: the dataset a trainer must load."""
    meta = {
        name: _sha256(ds.root / "meta" / name)
        for name in META_FILES
        if (ds.root / "meta" / name).is_file()
    }
    data = {}
    for ep in sorted(ds.rows):
        path = ds.data_path(ep)
        if path.is_file():
            data[str(path.relative_to(ds.root))] = _sha256(path)
    combined = hashlib.sha256(
        json.dumps({"meta": meta, "data": data}, sort_keys=True).encode()
    ).hexdigest()
    view = catalog.read(ds.root / "meta/levi_view.json", {})
    return {
        "sha256": combined,
        "meta_sha256": meta,
        "data_files": len(data),
        "source_fingerprint": view.get("source_fingerprint"),
        **ds.fingerprint(),
    }


def _flag(row: dict[str, Any]) -> str | None:
    value = row.get("levi_outcome")
    if value in ("success", "failure"):
        return value
    flag = row.get("is_success")
    if isinstance(flag, bool):
        return "success" if flag else "failure"
    return None


def _task(ds, row: dict[str, Any]) -> str | None:
    tasks = row.get("tasks")
    if isinstance(tasks, list) and tasks:
        return str(tasks[0])
    if "task_index" in row:
        return ds.tasks.get(int(row["task_index"]))
    return None


def _task_count(row: dict[str, Any]) -> int:
    tasks = row.get("tasks")
    if isinstance(tasks, list):
        return len([t for t in tasks if t])
    return 1 if "task_index" in row else 0


def _annotation(name: str) -> tuple[Path, dict[str, Any]]:
    from .agent.store import Store, annotation_digest, resolve

    state = catalog.STATE
    folder = resolve(state, name, "annotations")
    head = "legacy"
    if (state / "agent/workbench.sqlite3").exists():
        head = Store(state).head(name)
    return folder, {
        "revision": head,
        "digest": annotation_digest(state, name),
        "path": str(folder),
    }


def _subtasks(folder: Path, episode: int) -> list[dict[str, Any]]:
    path = folder / f"episode_{episode:06d}.json"
    try:
        atoms = json.loads(path.read_text()).get("atoms") or []
    except (OSError, ValueError):
        return []
    spans = sorted(
        (a for a in atoms if a.get("style") == "subtask" and "timestamp" in a),
        key=lambda a: float(a["timestamp"]),
    )
    out = []
    for i, atom in enumerate(spans):
        start = float(atom["timestamp"])
        end = atom.get("to")
        if end is None:
            end = float(spans[i + 1]["timestamp"]) if i + 1 < len(spans) else np.inf
        levi = atom.get("levi") or {}
        origin = levi.get("origin") or {}
        out.append(
            {
                "start": start,
                "end": float(end),
                "id": levi.get("subtask_id") or atom.get("content"),
                "text": atom.get("content"),
                "outcome": levi.get("outcome"),
                "attempt": levi.get("attempt"),
                "review": levi.get("review"),
                "origin": origin.get("kind"),
                "run_id": origin.get("run_id"),
            }
        )
    return out


def _anchored(
    name: str, run_id: str | None, allow_candidate: bool = False
) -> tuple[dict | None, dict[int, dict], list[str]]:
    """(run, verdicts per episode, candidate runs passed over).

    With no run named, the newest anchored review is used -- but one whose
    spec is still a *candidate* (not validated: the live service's generic
    release review, for one) is passed over, unless ``allow_candidate``. Naming
    the run (``anchored_run``) is the explicit choice and is always honoured."""
    from .agent.anchored import records
    from .agent.store import Store

    if not (catalog.STATE / "agent/workbench.sqlite3").exists():
        if run_id:
            raise ManifestError(f"No anchored review run {run_id!r} on {name}")
        return None, {}, []
    passed_over = []
    for run, found in records(Store(catalog.STATE), name, run_id):
        if not found:
            continue
        spec = (run.get("context") or {}).get("workflow", {}).get("anchored") or {}
        if (
            not run_id
            and not allow_candidate
            and spec.get("status", "stable") == "candidate"
        ):
            passed_over.append(run["id"])
            continue
        return run, found, passed_over
    if run_id:
        raise ManifestError(f"Anchored review run {run_id!r} has no results on {name}")
    return None, {}, passed_over


def _recap(ds, revision_id: str | None):
    from .recap import jobs as recap_jobs
    from .recap import store as recap_store

    record = recap_store.revision(ds.name, revision_id)
    if record is None:
        if revision_id:
            raise ManifestError(f"No RECAP revision {revision_id!r} on {ds.name}")
        return None, []
    return record, recap_jobs.stale_reasons(ds, record)


# ------------------------------------------------------------- build


def _dataset(repo_id: str):
    from .recap.jobs import RecapError, dataset

    if repo_id and not repo_id.startswith("local/"):
        repo_id = "local/" + repo_id
    try:
        return dataset(repo_id)
    except RecapError as exc:
        raise ManifestError(exc.detail) from exc


def _levi_commit() -> str | None:
    from .recap.jobs import _levi_commit as commit

    return commit()


def build(
    repo_id: str,
    operation: str,
    params: dict[str, Any] | None = None,
    *,
    tasks: list[str] | None = None,
    episodes: list[int] | None = None,
    anchored_run: str | None = None,
    anchored_tasks: list[str] | None = None,
    allow_candidate_anchored: bool = False,
    recap_revision: str | None = None,
    allow_stale: bool = False,
    output: str | Path | None = None,
    prompt_template: str = manifest_prompts.DEFAULT_TEMPLATE,
    subtask_max_chars: int = manifest_prompts.DEFAULT_SUBTASK_MAX_CHARS,
) -> dict[str, Any]:
    """Write ``manifest.json`` and ``frames.parquet`` for one dataset and
    operation; returns the manifest (with ``output_dir``)."""
    try:
        template_text = manifest_prompts.template_text(prompt_template)
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc
    if isinstance(subtask_max_chars, bool) or not (
        isinstance(subtask_max_chars, int) and 1 <= subtask_max_chars <= 2000
    ):
        raise ManifestError("subtask_max_chars is a whole number from 1 to 2000")
    if operation not in OPERATIONS:
        raise ManifestError(
            f"Unknown operation {operation!r}; known: " + ", ".join(OPERATIONS)
        )
    op = OPERATIONS[operation]
    settings = _params(op, params)
    ds = _dataset(repo_id)
    entry = catalog.datasets().get(ds.name, {})
    known = sorted(ds.rows)
    if not known:
        raise ManifestError(f"{ds.name} has no episodes in meta/episodes.jsonl")
    if episodes is not None:
        missing = sorted(set(episodes) - set(known))
        if missing:
            raise ManifestError(f"Episodes not in the dataset: {missing[:10]}")
    task_of = {ep: _task(ds, ds.rows[ep]) for ep in known}
    if tasks:
        unknown = set(tasks) - set(task_of.values())
        if unknown:
            raise ManifestError(
                "No episode has task " + ", ".join(repr(t) for t in sorted(unknown))
            )
    scope = {
        ep
        for ep in known
        if (episodes is None or ep in episodes) and (not tasks or task_of[ep] in tasks)
    }
    if not scope:
        raise ManifestError("No episode is in scope")

    from .agent.anchored import undecided as anchored_undecided

    run, verdicts, skipped = _anchored(ds.name, anchored_run, allow_candidate_anchored)
    # Human labels alone are a verification too (a task without anchored-review
    # rules is verified by people); refuse only when nothing verifies the scope.
    if (
        "anchored" in op.needs
        and run is None
        and not any(ep in scope for ep in ds.human)
    ):
        raise ManifestError(
            f"{operation} needs an anchored review or human outcome labels on "
            f"{ds.name}; run one (levi agent, workflow.anchored), label episodes, "
            "or use robot_flag_success"
            + (
                f". The anchored review(s) {', '.join(skipped)} use a candidate "
                "(not validated) spec and are not used by default: name one with "
                "--anchored-run or pass --allow-candidate-anchored"
                if skipped
                else ""
            )
        )
    applies = set(anchored_tasks) if anchored_tasks else None
    recap, stale = _recap(ds, recap_revision)
    if "recap" in op.needs:
        if recap is None:
            raise ManifestError(
                f"{operation} needs RECAP advantage labels on {ds.name}; "
                "run `levi recap run` first"
            )
        if stale and not allow_stale:
            raise ManifestError(
                "The RECAP labels are stale ("
                + "; ".join(stale)
                + "); recompute them or pass allow_stale"
            )
    folder, annotation = _annotation(ds.name)

    columns: dict[str, list] = {f.name: [] for f in FRAMES_SCHEMA}
    prompt_stats = _PromptStats()
    per_episode = []
    for ep in known:
        row = ds.rows[ep]
        path = ds.data_path(ep)
        if not path.is_file():
            raise ManifestError(f"Episode {ep} has no data file {path.name}")
        table = pq.read_table(path, columns=["frame_index", "timestamp"])
        frame_index = np.asarray(table["frame_index"].to_numpy(), dtype=np.int64)
        timestamp = np.asarray(table["timestamp"].to_numpy(), dtype=np.float64)
        n = len(frame_index)
        flag = _flag(row)
        human = ds.human.get(ep)
        record = verdicts.get(ep)
        use_anchored = record is not None and (
            applies is None or task_of[ep] in applies
        )
        anchored_outcome = record["outcome"] if record else None
        undecided = (
            anchored_undecided(record["outcome"], record.get("basis") or {})
            if record
            else None
        )
        if human:
            success, source = human, "human"
        elif use_anchored:
            success, source = anchored_outcome, "anchored"
        elif flag:
            success, source = flag, "robot_flag"
        else:
            success, source = None, None

        value = np.full(n, np.nan, dtype=np.float32)
        adv = np.full(n, np.nan, dtype=np.float32)
        positive: list[bool | None] = [None] * n
        if recap is not None:
            from .recap import store as recap_store

            labels = recap_store.read_episode(ds.name, ep, recap["revision_id"])
            if labels is not None:
                where = {int(f): i for i, f in enumerate(frame_index)}
                cols = labels.to_pydict()
                for f, v, a, p in zip(
                    cols["frame_index"],
                    cols["value"],
                    cols["advantage"],
                    cols["positive"],
                    strict=True,
                ):
                    i = where.get(int(f))
                    if i is not None:
                        value[i], adv[i], positive[i] = v, a, bool(p)

        spans = _subtasks(folder, ep)
        sub_id: list[str | None] = [None] * n
        sub_out: list[str | None] = [None] * n
        sub_try: list[int | None] = [None] * n
        sub_rev: list[str | None] = [None] * n
        segment_of: list[dict[str, Any] | None] = [None] * n
        for s in spans:
            hit = np.nonzero((timestamp >= s["start"] - 1e-6) & (timestamp < s["end"]))[
                0
            ]
            for i in hit:
                sub_id[i], sub_out[i], sub_try[i] = s["id"], s["outcome"], s["attempt"]
                sub_rev[i] = s["review"]
                segment_of[i] = s
        task_text = task_of[ep]
        prompts = [
            manifest_prompts.frame_prompts(
                task_text,
                segment_of[i],
                template=template_text,
                max_chars=subtask_max_chars,
            )
            for i in range(n)
        ]

        include = np.ones(n, dtype=bool)
        weight = np.ones(n, dtype=np.float32)
        reason: list[str | None] = [None] * n
        drop = _dropper(include, reason)

        if ep not in scope:
            drop(np.ones(n, bool), "out_of_scope")
        elif operation == "robot_flag_success":
            if flag != "success":
                drop(np.ones(n, bool), f"robot_flag_{flag or 'missing'}")
        elif operation == "verified_success":
            if source == "robot_flag" and settings["fallback"] == "exclude":
                drop(np.ones(n, bool), "unverified")
            elif success != "success":
                drop(np.ones(n, bool), f"{source or 'no'}_{success or 'verdict'}")
            elif (
                source == "anchored"
                and undecided
                and settings["undecided"] == "exclude"
            ):
                drop(np.ones(n, bool), "anchored_undecided")
        elif operation == "advantage_positive_mask":
            known_label = np.array([p is not None for p in positive])
            neg = np.array([p is False for p in positive])
            drop(neg, "advantage_negative")
            if settings["unlabelled"] == "exclude":
                drop(~known_label, "advantage_unlabelled")
        elif operation == "advantage_weighted":
            for i, p in enumerate(positive):
                weight[i] = (
                    settings["unlabelled_weight"]
                    if p is None
                    else settings["positive_weight"]
                    if p
                    else settings["negative_weight"]
                )
            drop(weight <= 0, "zero_weight")
        weight[~include] = 0.0

        columns["episode_index"] += [ep] * n
        columns["frame_index"] += frame_index.tolist()
        columns["timestamp"] += timestamp.tolist()
        columns["source_demo"] += [row.get("source_demo")] * n
        columns["task"] += [task_of[ep]] * n
        columns["include"] += include.tolist()
        columns["weight"] += weight.tolist()
        columns["exclude_reason"] += reason
        columns["episode_success"] += [success] * n
        columns["episode_success_source"] += [source] * n
        columns["robot_flag"] += [flag] * n
        columns["human_label"] += [human] * n
        columns["anchored_outcome"] += [anchored_outcome] * n
        columns["anchored_undecided"] += [undecided] * n
        columns["subtask_id"] += sub_id
        columns["subtask_outcome"] += sub_out
        columns["subtask_attempt"] += sub_try
        columns["subtask_review"] += sub_rev
        columns["recap_value"] += [None if np.isnan(x) else float(x) for x in value]
        columns["recap_advantage"] += [None if np.isnan(x) else float(x) for x in adv]
        columns["recap_positive"] += positive
        columns["prompt_task"] += [p[0] for p in prompts]
        columns["prompt_subtask"] += [p[1] for p in prompts]
        columns["prompt_has_subtask"] += [p[2] for p in prompts]
        columns["prompt_subtask_skip"] += [p[3] for p in prompts]
        columns["prompt_subtask_source"] += [
            manifest_prompts.segment_source(segment_of[i]) if p[2] else None
            for i, p in enumerate(prompts)
        ]
        columns["prompt_subtask_origin"] += [
            manifest_prompts.origin_class(segment_of[i]) if p[2] else None
            for i, p in enumerate(prompts)
        ]
        columns["episode_task_count"] += [_task_count(row)] * n
        for i, (p, keep) in enumerate(zip(prompts, include.tolist(), strict=True)):
            prompt_stats.add(p, keep, segment_of[i])
        per_episode.append(
            {
                "episode_index": ep,
                "source_demo": row.get("source_demo"),
                "rollout_source_demo": row.get("rollout_source_demo"),
                "task": task_of[ep],
                "frames": n,
                "included_frames": int(include.sum()),
                "weight": round(float(weight.sum()), 6),
                "robot_flag": flag,
                "episode_success": success,
                "episode_success_source": source,
                "anchored_outcome": anchored_outcome,
                "anchored_applied": bool(use_anchored),
                "subtask_frames": sum(1 for x in sub_id if x is not None),
                "subtask_frames_auto": sum(1 for x in sub_rev if x == "auto"),
                "task_count": _task_count(row),
                "prompt_subtask_frames": sum(1 for p in prompts if p[2]),
            }
        )

    frames = pa.table(
        columns, schema=_frames_schema(prompt_template, subtask_max_chars)
    )
    target = _target(ds.name, operation, output)
    temp = target / f".{FRAMES}.tmp"
    pq.write_table(frames, temp)
    temp.replace(target / FRAMES)

    included = [e for e in per_episode if e["included_frames"]]
    sources: dict[str, int] = {}
    for e in included:
        key = e["episode_success_source"] or "none"
        sources[key] = sources.get(key, 0) + 1
    manifest = {
        "schema": SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "levi_commit": _levi_commit(),
        "operation": {"name": operation, "summary": op.summary, "params": settings},
        "scope": {"tasks": tasks, "episodes": episodes},
        "dataset": {
            "name": ds.name,
            "repo_id": ds.repo_id,
            "base": entry.get("base"),
            "namespace": ds.name.split(catalog.SEPARATOR, 1)[1]
            if entry.get("base")
            else None,
            "root": str(ds.root),
            "kind": entry.get("kind"),
            "codebase_version": ds.info.get("codebase_version"),
            "fps": ds.fps,
            "episodes": len(known),
            "frames": frames.num_rows,
            "fingerprint": fingerprint(ds),
        },
        "annotation": {
            **annotation,
            # How much of the subtask labelling nobody has reviewed (written
            # by the live service): frames, and the share of labelled frames.
            "subtask_frames": sum(e["subtask_frames"] for e in per_episode),
            "subtask_frames_auto": sum(e["subtask_frames_auto"] for e in per_episode),
            "subtask_auto_share": round(
                sum(e["subtask_frames_auto"] for e in per_episode)
                / max(1, sum(e["subtask_frames"] for e in per_episode)),
                4,
            ),
        },
        "prompt": prompt_stats.record(
            prompt_template,
            template_text,
            subtask_max_chars,
            sum(1 for e in per_episode if e["task_count"] > 1),
        ),
        "outcomes": {
            "order": ["human", "anchored", "robot_flag"],
            "human_labels": len(ds.human),
            "robot_flags": sum(1 for e in per_episode if e["robot_flag"]),
        },
        "anchored": _anchored_meta(run, verdicts, anchored_tasks, skipped),
        "recap": _recap_meta(recap, stale),
        "counts": {
            "episodes_in_scope": len(scope),
            "episodes_included": len(included),
            "frames_included": int(np.count_nonzero(frames["include"].to_numpy())),
            "weight_sum": round(float(np.sum(frames["weight"].to_numpy())), 6),
            "included_by_verdict_source": sources,
        },
        "files": {
            FRAMES: {"sha256": _sha256(target / FRAMES), "rows": frames.num_rows}
        },
        "episodes": per_episode,
    }
    catalog.atomic(target / MANIFEST, manifest)
    return {**manifest, "output_dir": str(target)}


class _PromptStats:
    """What became of the second prompt's subtask, over every frame and over
    the frames that are included in the loss."""

    def __init__(self):
        self.frames = 0
        self.with_subtask = 0
        self.truncated = 0
        self.skipped: dict[str, int] = {}
        self.origins = dict.fromkeys(manifest_prompts.ORIGINS, 0)
        self.agent_runs: dict[str, int] = {}
        self.longest_chars = 0
        self.longest_bytes = 0
        self.included = {"frames": 0, "with_subtask": 0, "rejected_unreviewed": 0}

    def add(self, prompt, included, segment):
        _, text, has_subtask, skip, cut = prompt
        if text:
            self.longest_chars = max(self.longest_chars, len(text))
            self.longest_bytes = max(self.longest_bytes, len(text.encode()))
        if has_subtask:
            origin = manifest_prompts.origin_class(segment)
            self.origins[origin] += 1
            if origin == "agent_run":
                run = segment.get("run_id") or "unknown"
                self.agent_runs[run] = self.agent_runs.get(run, 0) + 1
        self.frames += 1
        self.with_subtask += bool(has_subtask)
        self.truncated += bool(cut)
        if skip:
            self.skipped[skip] = self.skipped.get(skip, 0) + 1
        if included:
            self.included["frames"] += 1
            self.included["with_subtask"] += bool(has_subtask)
            self.included["rejected_unreviewed"] += skip in UNREVIEWED

    def record(self, version, template, max_chars, several_tasks):
        return {
            "template_version": version,
            "template": template,
            "subtask_max_chars": max_chars,
            "source": "time segments with subtask_review empty or edited; "
            "automatic and unrecognised ones are never used. An empty review "
            "also covers an agent run's segment approved without the live "
            "service's mark (see frames_with_subtask_by_origin)",
            "frames": self.frames,
            "frames_with_subtask": self.with_subtask,
            "subtask_skipped": dict(sorted(self.skipped.items())),
            "frames_rejected_unreviewed": sum(
                self.skipped.get(k, 0) for k in UNREVIEWED
            ),
            "frames_with_subtask_by_origin": dict(self.origins),
            "frames_with_subtask_from_agent_runs": self.origins["agent_run"],
            "frames_with_subtask_by_agent_run": dict(sorted(self.agent_runs.items())),
            "truncated_frames": self.truncated,
            # The longest second prompt, before the RECAP suffix: openpi cuts a
            # prompt at max_token_len tokens (200 pi05, 48 pi0) from the end.
            # Bytes bound the token count from above for any language.
            "max_prompt_chars": self.longest_chars,
            "max_prompt_utf8_bytes": self.longest_bytes,
            "included_frames": dict(self.included),
            "episodes_with_several_tasks": several_tasks,
        }


UNREVIEWED = ("unreviewed", "review_unrecognised")


def _dropper(include: np.ndarray, reason: list):
    """Exclude the frames of ``mask`` for ``why`` (the first reason stays)."""

    def drop(mask, why):
        for i in np.nonzero(mask & include)[0]:
            reason[i] = why
        include[mask] = False

    return drop


def _anchored_meta(run, verdicts, anchored_tasks, skipped=()):
    if run is None:
        return {"skipped_candidate_runs": list(skipped)} if skipped else None
    spec = run["context"]["workflow"]["anchored"]
    return {
        "run_id": run["id"],
        "status": run.get("status"),
        "spec": {
            "id": spec.get("id"),
            "version": spec.get("version"),
            "sha256": hashlib.sha256(
                json.dumps(spec, sort_keys=True).encode()
            ).hexdigest(),
        },
        "provider": {
            k: (run.get("provider_config") or {}).get(k)
            for k in ("kind", "name", "model", "model_digest")
        },
        "episodes": len(verdicts),
        "applied_to_tasks": anchored_tasks,
        # A candidate spec is not validated; using it takes an explicit choice.
        "spec_status": spec.get("status", "stable"),
        "skipped_candidate_runs": list(skipped),
    }


def _recap_meta(record, stale):
    if record is None:
        return None
    checkpoint = record.get("checkpoint") or {}
    return {
        "revision_id": record["revision_id"],
        "checkpoint": checkpoint.get("name"),
        "checkpoint_sha256": checkpoint.get("sha256"),
        "provider": record.get("provider"),
        "threshold": record.get("threshold"),
        "threshold_source": record.get("threshold_source"),
        "threshold_provenance": record.get("threshold_provenance"),
        "lookahead": record.get("lookahead"),
        "positive_quantile": record.get("positive_quantile"),
        "dataset_type": record.get("dataset_type"),
        "static_filter": bool((record.get("static_filter") or {}).get("applied")),
        "labelled_frames": record.get("frames"),
        "levi_commit": record.get("levi_commit"),
        "stale_reasons": stale,
    }


def _target(name: str, operation: str, output) -> Path:
    if output:
        target = paths.inside(Path(output).expanduser())
        if target.exists() and any(target.iterdir()):
            raise ManifestError(f"{target} exists and is not empty")
        target.mkdir(parents=True, exist_ok=True)
        return target
    parent = root(name)
    rid = naming.timestamp_id(parent, suffix=f"-{operation}", create_dir=True)
    return parent / f"{rid}-{operation}"


def root(name: str) -> Path:
    if not name or "/" in name or name in {".", ".."}:
        raise ManifestError("Invalid dataset name")
    return paths.EXPORTS / name / "training_manifests"


def listing(repo_id: str) -> list[dict[str, Any]]:
    ds = _dataset(repo_id)
    folder = root(ds.name)
    out = []
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            value = catalog.read(path / MANIFEST, None)
            if value:
                out.append(
                    {
                        "output_dir": str(path),
                        "created_at": value.get("created_at"),
                        "operation": value.get("operation"),
                        "counts": value.get("counts"),
                    }
                )
    return out


# ------------------------------------------------------------- compare


def load(directory: str | Path) -> tuple[dict[str, Any], pa.Table]:
    """A manifest and its frames, refusing one whose frames file changed."""
    directory = Path(directory)
    manifest = catalog.read(directory / MANIFEST, None)
    if not manifest or manifest.get("schema") != SCHEMA:
        raise ManifestError(f"{directory} holds no {SCHEMA} manifest")
    expected = manifest["files"][FRAMES]["sha256"]
    if _sha256(directory / FRAMES) != expected:
        raise ManifestError(f"{directory / FRAMES} does not match its manifest")
    return manifest, pq.read_table(directory / FRAMES)


def compare(a: str | Path, b: str | Path) -> dict[str, Any]:
    """How much two manifests of one dataset differ as training input.

    ``input_difference`` is the total-variation distance between the two
    sampling distributions over frames (weight / total weight): 0 when a
    learner would draw the same frames equally often, 1 when disjoint."""
    ma, fa = load(a)
    mb, fb = load(b)
    if ma["dataset"]["fingerprint"]["sha256"] != mb["dataset"]["fingerprint"]["sha256"]:
        raise ManifestError("The manifests describe different dataset contents")
    key = ("episode_index", "frame_index")
    da = fa.select([*key, "weight"]).to_pandas().set_index(list(key))["weight"]
    db = fb.select([*key, "weight"]).to_pandas().set_index(list(key))["weight"]
    da, db = da.align(db, fill_value=0.0)
    pa_ = da / da.sum() if da.sum() > 0 else da
    pb_ = db / db.sum() if db.sum() > 0 else db
    ea = {e["episode_index"] for e in ma["episodes"] if e["included_frames"]}
    eb = {e["episode_index"] for e in mb["episodes"] if e["included_frames"]}
    return {
        "a": {"operation": ma["operation"]["name"], **ma["counts"]},
        "b": {"operation": mb["operation"]["name"], **mb["counts"]},
        "episodes_only_in_a": sorted(ea - eb),
        "episodes_only_in_b": sorted(eb - ea),
        "episodes_in_both": len(ea & eb),
        "frames_included_only_in_a": int(((da > 0) & (db <= 0)).sum()),
        "frames_included_only_in_b": int(((db > 0) & (da <= 0)).sum()),
        "input_difference": round(float(0.5 * (pa_ - pb_).abs().sum()), 6),
    }


# ------------------------------------------------------------- CLI


def _param(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        raise ValueError("a parameter is key=value")
    return key.strip(), value.strip()


def main(argv=None) -> int:
    """``levi export``: training manifests (docs/TRAINING_MANIFEST.md)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="levi export",
        description="Training manifests: which frames enter a learner's loss, "
        "with what weight and on what evidence",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    make = sub.add_parser("manifest", help="write a manifest for one operation")
    make.add_argument("dataset", help="local/<name> or the catalog name")
    make.add_argument("--operation", required=True, choices=list(OPERATIONS))
    make.add_argument(
        "--param", action="append", default=[], help="key=value (repeatable)"
    )
    make.add_argument("--task", action="append", help="limit the scope to a task")
    make.add_argument("--episodes", help="limit the scope: 0,3,5-9")
    make.add_argument("--anchored-run", help="anchored review run (default newest)")
    make.add_argument(
        "--allow-candidate-anchored",
        action="store_true",
        help="also use a newest anchored review whose spec is only a candidate "
        "(default: skipped; --anchored-run names one explicitly)",
    )
    make.add_argument(
        "--anchored-task",
        action="append",
        help="tasks the anchored verdict is valid for (default: every task it covers)",
    )
    make.add_argument("--recap-revision", help="RECAP revision (default current)")
    make.add_argument("--allow-stale", action="store_true")
    make.add_argument(
        "--prompt-template",
        default=manifest_prompts.DEFAULT_TEMPLATE,
        help="version of the per-frame prompt template (default: %(default)s)",
    )
    make.add_argument(
        "--subtask-max-chars",
        type=int,
        default=manifest_prompts.DEFAULT_SUBTASK_MAX_CHARS,
        help="a longer subtask text is cut to this many characters "
        "(default: %(default)s)",
    )
    make.add_argument("--output", help="a new directory inside the workspace")
    make.add_argument("--json", action="store_true", help="print the whole manifest")
    sub.add_parser("operations", help="the built-in operations and their parameters")
    show = sub.add_parser("list", help="manifests written for a dataset")
    show.add_argument("dataset")
    diff = sub.add_parser("diff", help="compare two manifests of one dataset")
    diff.add_argument("a")
    diff.add_argument("b")
    args = parser.parse_args(argv)
    try:
        if args.action == "operations":
            print(json.dumps(operations(), indent=1))
        elif args.action == "list":
            print(json.dumps(listing(args.dataset), indent=1))
        elif args.action == "diff":
            print(json.dumps(compare(args.a, args.b), indent=1))
        else:
            params = dict(_param(p) for p in args.param)
            result = build(
                args.dataset,
                args.operation,
                params,
                tasks=args.task,
                episodes=_episodes(args.episodes) if args.episodes else None,
                anchored_run=args.anchored_run,
                anchored_tasks=args.anchored_task,
                allow_candidate_anchored=args.allow_candidate_anchored,
                recap_revision=args.recap_revision,
                allow_stale=args.allow_stale,
                output=args.output,
                prompt_template=args.prompt_template,
                subtask_max_chars=args.subtask_max_chars,
            )
            if not args.json:
                result = {
                    k: result[k]
                    for k in (
                        "output_dir",
                        "operation",
                        "counts",
                        "anchored",
                        "recap",
                        "prompt",
                    )
                }
            print(json.dumps(result, indent=1, ensure_ascii=False))
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


def _episodes(text: str) -> list[int]:
    out: list[int] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        lo, sep, hi = part.partition("-")
        out += list(range(int(lo), int(hi) + 1)) if sep else [int(lo)]
    return sorted(set(out))
