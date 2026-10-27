"""How close a learner's annotation is to a teacher's reference.

The teacher's committed work on a task is saved as a reference before the task
is cleared for the learner:
``outputs/LEVI/datasets/<name>/teaching/reference-<task>.json``. A learner run
is then graded episode by episode against it, deterministically:

- segments match when they name the same subtask and overlap by IoU >= 0.3;
- ``segment_f1`` is the F1 of that matching;
- ``outcome_accuracy`` is the share of matched segments with the same outcome;
- ``boundary_mae`` is the mean start/end error of matched segments (seconds);
- ``time_accuracy`` is the share of the reference's annotated time the
  candidate gives the same subtask; ``outcome_time_accuracy`` the share where
  the outcome is the same as well. Both count time, not segments, so one
  wrongly split attempt costs what it covers, and neither depends on the
  task, its vocabulary or the frame rate.

``agreement`` is high when every one of those clears its threshold; the learner
is then producing the teacher's annotation, not merely something plausible.
"""

from ..events.boundary_metrics import match_segments, segment_f1_counts

IOU = 0.3
THRESHOLDS = {
    "segment_f1": 0.8,
    "outcome_accuracy": 0.9,
    "time_accuracy": 0.8,
    "outcome_time_accuracy": 0.75,
    "boundary_mae": 1.0,  # seconds, lower is better
}


def episode(reference, candidate):
    # one matching rule and one F1 for grading and the event readers (levi.events.boundary_metrics)
    pairs, ref, cand = match_segments(reference, candidate, IOU)
    f1 = segment_f1_counts(len(pairs), len(ref), len(cand))
    outcomes = [r.get("outcome") == c.get("outcome") for r, c in pairs]
    errors = [
        (abs(r["start"] - c["start"]) + abs(r["end"] - c["end"])) / 2 for r, c in pairs
    ]
    total = sum(max(0.0, r["end"] - r["start"]) for r in ref)
    same_subtask = same_outcome = 0.0
    for r in ref:
        for c in cand:
            if c.get("subtask") != r.get("subtask"):
                continue
            overlap = max(0.0, min(r["end"], c["end"]) - max(r["start"], c["start"]))
            same_subtask += overlap
            if c.get("outcome") == r.get("outcome"):
                same_outcome += overlap
    return {
        "reference_segments": len(ref),
        "candidate_segments": len(cand),
        "matched": len(pairs),
        "segment_f1": round(f1, 3),
        "outcome_accuracy": round(sum(outcomes) / len(outcomes), 3)
        if outcomes
        else 0.0,
        "boundary_mae": round(sum(errors) / len(errors), 2) if errors else None,
        "time_accuracy": round(min(1.0, same_subtask / total), 3) if total else None,
        "outcome_time_accuracy": round(min(1.0, same_outcome / total), 3)
        if total
        else None,
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
