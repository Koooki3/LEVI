"""How close a learner's annotation is to a teacher's reference.

The teacher's committed work on a task is saved as a reference before the task
is cleared for the learner:
``outputs/LEVI/datasets/<name>/teaching/reference-<task>.json``. A learner run
is then graded episode by episode against it, deterministically:

- segments match when they name the same subtask and overlap by IoU >= 0.3;
- ``segment_f1`` is the F1 of that matching;
- ``outcome_accuracy`` is the share of matched segments with the same outcome;
- ``boundary_mae`` is the mean start/end error of matched segments (seconds);
- ``place_agreement`` compares what matters most for stacking-type tasks: the
  ordered success/failure outcomes of the ``place`` segments.

``agreement`` is high when every one of those clears its threshold; the learner
is then producing the teacher's annotation, not merely something plausible.
"""

import time

from .layout import dataset_dir, read_json, write_json

IOU = 0.3
THRESHOLDS = {
    "segment_f1": 0.8,
    "outcome_accuracy": 0.9,
    "place_agreement": 0.9,
    "boundary_mae": 1.0,  # seconds, lower is better
}


def reference_path(state, key, task):
    return dataset_dir(state, key) / "teaching" / f"reference-{task}.json"


def save_reference(store, key, task, change_id, *, description=""):
    """Freeze a committed teacher changeset as the reference for ``task``."""
    change = store.get("changes", change_id)
    if change["status"] != "committed":
        raise ValueError("Only committed work can be a reference")
    rejected = {k for k, v in change.get("decisions", {}).items() if v == "rejected"}
    episodes = {}
    for index, p in enumerate(change["proposals"]):
        if str(index) in rejected or p["kind"] != "segment":
            continue
        episodes.setdefault(f"episode_{p['episode_index']:06d}", []).append(
            {
                "subtask": p.get("subtask_id"),
                "start": p["start"],
                "end": p.get("end"),
                "outcome": p.get("outcome"),
                "content": p["content"],
            }
        )
    value = {
        "schema": "levi.harness.reference.v1",
        "task": task,
        "description": description,
        "dataset_key": key,
        "run_id": change["run_id"],
        "changeset": change_id,
        "episodes": episodes,
        "saved_at": time.time(),
    }
    write_json(reference_path(store.state, key, task), value)
    return value


def _iou(a, b):
    start, end = max(a["start"], b["start"]), min(a["end"], b["end"])
    inter = max(0.0, end - start)
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return inter / union if union > 0 else 0.0


def episode(reference, candidate):
    ref = [s for s in reference if s.get("end") is not None]
    cand = [s for s in candidate if s.get("end") is not None]
    pairs, used = [], set()
    for r in ref:
        best, score = None, IOU
        for j, c in enumerate(cand):
            if j in used or c.get("subtask") != r.get("subtask"):
                continue
            value = _iou(r, c)
            if value >= score:
                best, score = j, value
        if best is not None:
            used.add(best)
            pairs.append((r, cand[best]))
    precision = len(pairs) / len(cand) if cand else (1.0 if not ref else 0.0)
    recall = len(pairs) / len(ref) if ref else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    outcomes = [r.get("outcome") == c.get("outcome") for r, c in pairs]
    errors = [
        (abs(r["start"] - c["start"]) + abs(r["end"] - c["end"])) / 2 for r, c in pairs
    ]
    ref_place = [
        s.get("outcome")
        for s in sorted(ref, key=lambda s: s["start"])
        if s.get("subtask") == "place"
    ]
    cand_place = [
        s.get("outcome")
        for s in sorted(cand, key=lambda s: s["start"])
        if s.get("subtask") == "place"
    ]
    length = max(len(ref_place), len(cand_place))
    same = sum(a == b for a, b in zip(ref_place, cand_place))
    return {
        "reference_segments": len(ref),
        "candidate_segments": len(cand),
        "matched": len(pairs),
        "segment_f1": round(f1, 3),
        "outcome_accuracy": round(sum(outcomes) / len(outcomes), 3)
        if outcomes
        else 0.0,
        "boundary_mae": round(sum(errors) / len(errors), 2) if errors else None,
        "place_agreement": round(same / length, 3) if length else 1.0,
        "reference_places": ref_place,
        "candidate_places": cand_place,
    }


def grade(reference, candidate_episodes):
    """Grade every reference episode; ``candidate_episodes`` maps name → segments."""
    rows = {
        name: episode(segments, candidate_episodes.get(name, []))
        for name, segments in sorted(reference["episodes"].items())
    }

    def mean(field):
        values = [r[field] for r in rows.values() if r[field] is not None]
        return round(sum(values) / len(values), 3) if values else None

    overall = {field: mean(field) for field in THRESHOLDS}
    passed = {
        field: (
            overall[field] is not None
            and (
                overall[field] <= limit
                if field == "boundary_mae"
                else overall[field] >= limit
            )
        )
        for field, limit in THRESHOLDS.items()
    }
    return {
        "task": reference["task"],
        "overall": overall,
        "passed": passed,
        "agreement": all(passed.values()),
        "episodes": rows,
        "thresholds": THRESHOLDS,
    }


def segments_of_change(change, *, learner=False):
    """Segments per episode from a changeset (or a learner's raw outputs)."""
    episodes = {}
    for p in change["proposals"]:
        if p["kind"] != "segment":
            continue
        episodes.setdefault(f"episode_{p['episode_index']:06d}", []).append(
            {
                "subtask": p.get("subtask_id"),
                "start": p["start"],
                "end": p.get("end"),
                "outcome": p.get("outcome"),
                "content": p["content"],
            }
        )
    return episodes


def learner_segments(store, run_id):
    """The learner's own final outputs, before any teacher revision.

    Per episode the last phase is the answer: refine over coarse, and the
    batches of a long episode (refine-1, refine-2, ...) over both, each
    contributing the proposals that start in its window.
    """
    phases = {}
    batches = {}
    for record in store.list("teaching"):
        if record["run_id"] != run_id or record.get("status") == "superseded":
            continue
        phase, ep = record["phase"], record["episode"]
        if phase.startswith("refine-"):
            window = record["summary"].get("refine_only") or {}
            batches.setdefault(ep, []).extend(
                p
                for p in record["learner_output"]["proposals"]
                if window.get("start", 0) <= p["start"] < window.get("end", 1e18)
            )
            continue
        rank = {"coarse": 0, "refine": 1}.get(phase, 0)
        current = phases.get(ep)
        if current is None or rank >= current[0]:
            phases[ep] = (rank, record["learner_output"]["proposals"])
    for ep, proposals in batches.items():
        phases[ep] = (2, sorted(proposals, key=lambda p: p["start"]))
    return {
        f"episode_{ep:06d}": [
            {
                "subtask": p.get("subtask_id"),
                "start": p["start"],
                "end": p.get("end"),
                "outcome": p.get("outcome"),
                "content": p["content"],
            }
            for p in proposals
            if p["kind"] == "segment"
        ]
        for ep, (_, proposals) in phases.items()
    }


def load_reference(state, key, task):
    value = read_json(reference_path(state, key, task))
    if value is None:
        raise KeyError(f"No reference {task!r} for {key}")
    return value


# Episode-level review tasks: is this episode the dataset's task, and did it
# succeed? The reference holds one verdict per episode.
REVIEW_THRESHOLDS = {"task_match_accuracy": 0.95, "outcome_accuracy": 0.85}


def review_verdicts(proposals):
    """{episode_NNNNNN: {task_match, outcome}} from outcome/issue proposals."""
    verdicts = {}
    for p in proposals:
        name = f"episode_{p['episode_index']:06d}"
        row = verdicts.setdefault(
            name, {"task_match": True, "outcome": None, "notes": []}
        )
        if p["kind"] == "issue":
            row["task_match"] = False
        elif p["kind"] == "outcome":
            row["outcome"] = p.get("outcome")
        row["notes"].append(p.get("content", "")[:200])
    return verdicts


def save_review_reference(store, key, task, change_id, *, description=""):
    change = store.get("changes", change_id)
    if change["status"] != "committed":
        raise ValueError("Only committed work can be a reference")
    rejected = {k for k, v in change.get("decisions", {}).items() if v == "rejected"}
    kept = [p for i, p in enumerate(change["proposals"]) if str(i) not in rejected]
    run = store.get("runs", change["run_id"])
    value = {
        "schema": "levi.harness.reference.v1",
        "kind": "review",
        "task": task,
        "description": description,
        "dataset_key": key,
        "run_id": change["run_id"],
        "revision": change.get("published_revision"),
        "instruction": run["context"]["instruction"],
        "cameras": run["context"]["cameras"],
        "episodes": dict(sorted(review_verdicts(kept).items())),
        "saved_at": time.time(),
    }
    write_json(reference_path(store.state, key, task), value)
    return value


def grade_review(reference, candidate):
    rows, match, outcome = {}, [], []
    for name, ref in sorted(reference["episodes"].items()):
        got = candidate.get(name)
        if got is None:
            rows[name] = {"missing": True}
            match.append(False)
            continue
        same_task = got["task_match"] == ref["task_match"]
        match.append(same_task)
        row = {"task_match": same_task}
        if ref["task_match"] and got["task_match"]:
            row["outcome"] = got["outcome"] == ref["outcome"]
            outcome.append(row["outcome"])
            if not row["outcome"]:
                row["expected"], row["got"] = ref["outcome"], got["outcome"]
        if not same_task:
            row["expected_task_match"] = ref["task_match"]
        rows[name] = row
    overall = {
        "task_match_accuracy": round(sum(match) / len(match), 3) if match else None,
        "outcome_accuracy": round(sum(outcome) / len(outcome), 3) if outcome else None,
    }
    passed = {
        k: overall[k] is not None and overall[k] >= limit
        for k, limit in REVIEW_THRESHOLDS.items()
    }
    return {
        "task": reference["task"],
        "overall": overall,
        "passed": passed,
        "agreement": all(passed.values()),
        "episodes": rows,
        "thresholds": REVIEW_THRESHOLDS,
    }


def learner_review(store, run_id):
    """The learner's own review verdicts, before any teacher revision."""
    proposals = []
    for record in store.list("teaching"):
        if record["run_id"] == run_id:
            proposals += record["learner_output"]["proposals"]
    return review_verdicts(proposals)


def grades_path(state, key):
    return dataset_dir(state, key) / "teaching" / "grades.json"


def record_grade(state, key, row):
    """Append one graded round to the dataset's learning curve."""
    rows = read_json(grades_path(state, key)) or []
    rows.append(row)
    write_json(grades_path(state, key), rows)
    return rows
