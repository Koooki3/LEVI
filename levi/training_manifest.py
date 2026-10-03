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

from . import catalog, naming, paths

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
    ]
)


# Per-frame prompt columns, written only when the export asks for them
# (``prompt_subtask``): existing exports keep exactly the columns above.
PROMPT_TEMPLATE = "task-subtask-v1"
# A template id names one exact text format; changing the text is a new id.
PROMPT_TEMPLATES = {PROMPT_TEMPLATE: "{task}；当前子任务：{subtask}"}
PROMPT_SCHEMA = pa.schema(
    [
        *FRAMES_SCHEMA,
        # The training task text, exactly as ``task`` (the first of an
        # episode's tasks): the version of the prompt without a subtask.
        ("prompt_task", pa.string()),
        # The template filled with the task and the subtask covering the frame
        # (null: no usable, reviewed segment covers it).
        ("prompt_subtask", pa.string()),
        ("prompt_template", pa.string()),
        # Which segment the subtask came from and who stands behind it.
        ("prompt_subtask_source", pa.string()),
    ]
)


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
        out.append(
            {
                "start": start,
                "end": float(end),
                "id": levi.get("subtask_id") or atom.get("content"),
                "outcome": levi.get("outcome"),
                "attempt": levi.get("attempt"),
                "review": levi.get("review"),
            }
        )
    return out


def _prompt_vocabulary(folder: Path) -> tuple[str, dict[str, str]]:
    """(source, subtask id -> prompt text) from the vocabulary in force: the
    dataset's own, else LEVI's built-in. The text is the entry's ``label``
    (a person's wording; for the built-in vocabulary the English name, equal
    to the id) with whitespace collapsed. The special labels (``other``,
    ``unknown``, ``background``) name no phase of the task and get none."""
    from .annotations import vocabulary

    value = vocabulary.read(folder)
    text = {}
    for entry in value["subtasks"]:
        sid = entry.get("id")
        label = " ".join(str(entry.get("label") or sid or "").split())
        if sid and label and sid not in vocabulary.SPECIAL:
            text[str(sid)] = label
    return value["source"], text


def _span_end(span: dict[str, Any]) -> str:
    return "end" if np.isinf(span["end"]) else f"{span['end']:.3f}"


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
    prompt_subtask: bool = False,
    allow_auto_subtask: bool = False,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Write ``manifest.json`` and ``frames.parquet`` for one dataset and
    operation; returns the manifest (with ``output_dir``).

    ``prompt_subtask`` adds the per-frame prompt columns (PROMPT_SCHEMA): the
    task-only prompt and, for frames inside a reviewed time segment, the
    ``task-subtask-v1`` prompt. A segment the live service wrote and nobody
    reviewed (``review`` auto) is not used unless ``allow_auto_subtask``."""
    if allow_auto_subtask and not prompt_subtask:
        raise ManifestError("allow_auto_subtask only applies with prompt_subtask")
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

    schema = PROMPT_SCHEMA if prompt_subtask else FRAMES_SCHEMA
    columns: dict[str, list] = {f.name: [] for f in schema}
    template = PROMPT_TEMPLATES[PROMPT_TEMPLATE]
    vocabulary_source, prompt_text = (
        _prompt_vocabulary(folder) if prompt_subtask else (None, {})
    )
    prompt_stats = {
        "with_subtask": 0,
        "included_with_subtask": 0,
        "auto_rejected": 0,
        "auto_used": 0,
        "not_in_vocabulary": 0,
        "no_task": 0,
    }
    prompt_used: dict[str, str] = {}
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
        sub_span = [-1] * n  # which span (a later one wins an overlap) covers a frame
        for k, s in enumerate(spans):
            hit = np.nonzero((timestamp >= s["start"] - 1e-6) & (timestamp < s["end"]))[
                0
            ]
            for i in hit:
                sub_id[i], sub_out[i], sub_try[i] = s["id"], s["outcome"], s["attempt"]
                sub_rev[i] = s["review"]
                sub_span[i] = k

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
        prompt_frames = 0
        if prompt_subtask:
            task = task_of[ep]
            p_sub: list[str | None] = [None] * n
            p_src: list[str | None] = [None] * n
            for i, k in enumerate(sub_span):
                if k < 0:
                    continue
                span = spans[k]
                text = prompt_text.get(str(span["id"]))
                if text is None:
                    prompt_stats["not_in_vocabulary"] += 1
                    continue
                reviewed = span["review"] in (None, "", "edited")
                if not reviewed and not allow_auto_subtask:
                    prompt_stats["auto_rejected"] += 1
                    continue
                if not task:
                    prompt_stats["no_task"] += 1
                    continue
                prompt_stats["auto_used"] += not reviewed
                p_sub[i] = template.format(task=task, subtask=text)
                p_src[i] = (
                    f"segment {span['start']:.3f}-{_span_end(span)} "
                    f"review={span['review'] or 'human'}"
                )
                prompt_used[str(span["id"])] = text
                prompt_frames += 1
                prompt_stats["included_with_subtask"] += int(include[i])
            prompt_stats["with_subtask"] += prompt_frames
            columns["prompt_task"] += [task] * n
            columns["prompt_subtask"] += p_sub
            columns["prompt_template"] += [PROMPT_TEMPLATE] * n
            columns["prompt_subtask_source"] += p_src
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
                **({"prompt_subtask_frames": prompt_frames} if prompt_subtask else {}),
            }
        )

    frames = pa.table(columns, schema=schema)
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
            **(
                {
                    "prompt": _prompt_meta(
                        annotation["revision"],
                        vocabulary_source,
                        prompt_used,
                        prompt_stats,
                        allow_auto_subtask,
                        frames.num_rows,
                        int(np.count_nonzero(frames["include"].to_numpy())),
                        sum(
                            1 for ep in known if len(ds.rows[ep].get("tasks") or []) > 1
                        ),
                    )
                }
                if prompt_subtask
                else {}
            ),
        },
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


def _dropper(include: np.ndarray, reason: list):
    """Exclude the frames of ``mask`` for ``why`` (the first reason stays)."""

    def drop(mask, why):
        for i in np.nonzero(mask & include)[0]:
            reason[i] = why
        include[mask] = False

    return drop


def _prompt_meta(
    revision, vocabulary_source, used, stats, allow_auto, frames, included, multi_task
):
    return {
        "template": PROMPT_TEMPLATE,
        "format": PROMPT_TEMPLATES[PROMPT_TEMPLATE],
        # What the task-only prompt is when an episode has several tasks.
        "task_rule": "first entry of the episode's tasks list (else the task_index "
        "lookup); the other tasks are not used",
        "multi_task_episodes": multi_task,
        "annotation_revision": revision,
        "vocabulary": {"source": vocabulary_source, "text": dict(sorted(used.items()))},
        "allow_auto_subtask": allow_auto,
        "frames_with_subtask": stats["with_subtask"],
        "frames_with_subtask_share": round(stats["with_subtask"] / max(1, frames), 4),
        "frames_included_with_subtask": stats["included_with_subtask"],
        "frames_included_with_subtask_share": round(
            stats["included_with_subtask"] / max(1, included), 4
        ),
        # Frames a segment covers whose subtask got no prompt, by reason.
        "frames_auto_rejected": stats["auto_rejected"],
        "frames_auto_used": stats["auto_used"],
        "frames_not_in_vocabulary": stats["not_in_vocabulary"],
        "frames_without_task": stats["no_task"],
    }


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
        "--prompt-subtask",
        action="store_true",
        help="add per-frame prompt columns: the task-only prompt and the "
        f"{PROMPT_TEMPLATE} prompt from reviewed time segments",
    )
    make.add_argument(
        "--allow-auto-subtask",
        action="store_true",
        help="with --prompt-subtask, also use time segments the live service "
        "wrote and nobody reviewed (default: they are not used)",
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
                prompt_subtask=args.prompt_subtask,
                allow_auto_subtask=args.allow_auto_subtask,
                output=args.output,
            )
            if not args.json:
                result = {
                    k: result[k]
                    for k in ("output_dir", "operation", "counts", "anchored", "recap")
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
