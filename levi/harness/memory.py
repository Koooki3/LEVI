"""Verified local memory, one canonical record per dataset.

Only human-committed work becomes memory: a proposal a reviewer rejected, or a
run that was never committed, teaches nothing about the data. Aggregates are
recomputed from the per-episode entries every time, so the same run closing
twice, or a later run re-annotating an episode, cannot count anything twice.

The record is what a later task on the same dataset is handed at plan time
(``context``), and what a local model's teaching data is drawn from: each
episode entry names the run and published revision it came from.
"""

import re
import statistics
import time

from .layout import memory_path, read_json, write_json

# How an uncertainty note is filed. Keyword rules, deterministic and cheap; a
# note that matches none is kept under "other" rather than guessed at.
UNCERTAINTY = {
    "sampling_gap": re.compile(
        r"between (?:\S+ )?samples|sample spacing|\d s spacing|coarse|"
        r"cannot be excluded|cannot be resolved|采样间隙|采样间隔|粗采样|帧间|无法排除|无法确定",
        re.IGNORECASE,
    ),
    "human_intervention": re.compile(
        r"\bhuman\b|\bhand\b|人手|人为|有人|操作员|人工干预", re.IGNORECASE
    ),
    "recording_ends": re.compile(
        r"recording ends|last recorded frame|ends at|录制结束|录制中断|最后一帧|末帧|视频结束",
        re.IGNORECASE,
    ),
    "visual_ambiguity": re.compile(
        r"ambiguous|resolution|occlu|unclear|看不清|遮挡|模糊|分辨率|不清楚",
        re.IGNORECASE,
    ),
    "unrecorded_start": re.compile(
        r"starts with|already held|not recorded|开局已|开始前|未录到|已夹着",
        re.IGNORECASE,
    ),
}


def classify(note: str, outcome: str | None = None) -> list[str]:
    """File a note. A non-success segment must say why, in the same field
    as an uncertainty; when the note is that reason and names none of the
    uncertainty patterns, it is filed as a failure reason, not as doubt."""
    found = [name for name, rule in UNCERTAINTY.items() if rule.search(note or "")]
    if found or not note:
        return found
    return ["failure_reason"] if outcome == "failure" else ["other"]


def empty(dataset, key):
    return {
        "schema": "levi.harness.memory.v1",
        "dataset": dataset,
        "dataset_key": key,
        "sources": [],
        "episodes": {},
        "profile": {},
        "subtasks": {},
        "uncertainty": {},
        "cost": {},
        "lessons": [],
        "updated_at": None,
    }


def load(state, key, dataset=None):
    return read_json(memory_path(state, key)) or empty(dataset, key)


def _aggregate(memory):
    subtasks, uncertainty, durations = {}, {}, []
    for name, episode in sorted(memory["episodes"].items()):
        if episode.get("duration_seconds"):
            durations.append(episode["duration_seconds"])
        for segment in episode["segments"]:
            if not segment.get("subtask"):
                # Episode-level verdicts (review outcome/issue) are not
                # subtasks; they are counted below instead.
                continue
            row = subtasks.setdefault(
                segment["subtask"],
                {"count": 0, "outcomes": {}, "seconds": []},
            )
            row["count"] += 1
            outcome = segment.get("outcome") or "unlabelled"
            row["outcomes"][outcome] = row["outcomes"].get(outcome, 0) + 1
            if segment.get("start") is not None and segment.get("end") is not None:
                row["seconds"].append(segment["end"] - segment["start"])
            for category in classify(
                segment.get("uncertainty", ""), segment.get("outcome")
            ):
                entry = uncertainty.setdefault(category, {"count": 0, "episodes": []})
                entry["count"] += 1
                if name not in entry["episodes"]:
                    entry["episodes"].append(name)
    for row in subtasks.values():
        seconds = row.pop("seconds")
        row["median_seconds"] = (
            round(statistics.median(seconds), 2) if seconds else None
        )
    verdicts = {"episodes": 0, "other_task": 0, "outcomes": {}}
    for episode in memory["episodes"].values():
        points = [s for s in episode["segments"] if not s.get("subtask")]
        if not points:
            continue
        verdicts["episodes"] += 1
        if any(s.get("kind") == "issue" for s in points):
            verdicts["other_task"] += 1
        for s in points:
            if s.get("outcome"):
                verdicts["outcomes"][s["outcome"]] = (
                    verdicts["outcomes"].get(s["outcome"], 0) + 1
                )
    memory["episode_verdicts"] = verdicts if verdicts["episodes"] else {}
    memory["subtasks"] = dict(sorted(subtasks.items()))
    memory["uncertainty"] = dict(
        sorted(uncertainty.items(), key=lambda item: -item[1]["count"])
    )
    memory["profile"]["episodes_annotated"] = len(memory["episodes"])
    if durations:
        memory["profile"]["duration_seconds"] = {
            "median": round(statistics.median(durations), 2),
            "min": round(min(durations), 2),
            "max": round(max(durations), 2),
        }


def update(store, ledger):
    """Fold one closed run into its dataset's memory; no-op without new evidence.

    Returns (memory, changed).
    """
    key = ledger["dataset_key"]
    memory = load(store.state, key, ledger["dataset"])
    if ledger["run_id"] in memory["sources"]:
        return memory, False
    if not ledger.get("published_revision"):
        # Nothing a human accepted: the run's facts stay in its ledger only.
        return memory, False
    change = store.get("changes", ledger["changeset"])
    run = store.get("runs", ledger["run_id"])
    rejected = {k for k, v in change.get("decisions", {}).items() if v == "rejected"}
    accepted = [p for i, p in enumerate(change["proposals"]) if str(i) not in rejected]
    durations = {e["episode"]: e.get("duration_seconds") for e in ledger["episodes"]}
    for episode in sorted({p["episode_index"] for p in accepted}):
        name = f"episode_{episode:06d}"
        memory["episodes"][name] = {
            "run_id": ledger["run_id"],
            "revision": ledger["published_revision"],
            "duration_seconds": durations.get(name),
            "segments": [
                {
                    "subtask": p.get("subtask_id"),
                    "start": p.get("start"),
                    "end": p.get("end"),
                    "outcome": p.get("outcome"),
                    "attempt": p.get("attempt"),
                    "kind": p.get("kind"),
                    "content": p.get("content"),
                    "uncertainty": p.get("uncertainty", ""),
                }
                for p in accepted
                if p["episode_index"] == episode
            ],
        }
    definitions = run["context"]["workflow"].get("definitions") or []
    if definitions:
        # What the next task on this dataset can reuse, as a human approved it.
        memory["definitions"] = definitions
    memory["profile"].update(
        cameras=run["context"].get("cameras", []),
        tasks=sorted({task for e in ledger["episodes"] for task in e.get("tasks", [])})
        or memory["profile"].get("tasks", []),
        instruction=run["context"].get("instruction"),
    )
    _aggregate(memory)
    memory["cost"] = _cost(ledger)
    memory["sources"].append(ledger["run_id"])
    memory["updated_at"] = time.time()
    write_json(memory_path(store.state, key), memory)
    return memory, True


def _cost(ledger):
    completed = max(1, ledger["episodes_completed"])
    reported = (ledger["tokens"].get("reported") or {}).get("tokens")
    return {
        "last_run": ledger["run_id"],
        "delivered_tokens_per_episode": ledger["tokens"]["delivered"][
            "estimated_input_tokens"
        ]
        // completed,
        "reported_tokens_per_episode": reported // completed if reported else None,
        "wall_seconds_per_episode": round(ledger["wall_seconds"] / completed, 1)
        if ledger.get("wall_seconds")
        else None,
    }


def record_cost(store, ledger):
    """Fold a closed run's cost into the dataset's per-agent cost profile.

    Every closed run counts, committed or not: cost is measured, not reviewed.
    Folding the same run again (a late usage report) replaces its entry.
    Returns what the profile's medians were *before* this run.
    """
    from . import cost

    record = ledger.get("cost")
    if not record or not ledger.get("episodes_completed"):
        return {}
    memory = load(store.state, ledger["dataset_key"], ledger["dataset"])
    memory.setdefault("cost_profiles", {})
    _, before = cost.fold(memory["cost_profiles"], record)
    memory["cost_latest"] = record
    memory["cost_hints"] = cost.hints(record, before)
    if memory["cost"].get("last_run") in (None, ledger["run_id"]) or (
        ledger["run_id"] in memory["sources"]
    ):
        memory["cost"] = _cost(ledger)
    memory["updated_at"] = time.time()
    write_json(memory_path(store.state, ledger["dataset_key"]), memory)
    return before


def refresh_cost(store, ledger):
    """A usage report that arrives after closing updates the cost it describes."""
    return record_cost(store, ledger)


def rebuild(store, key):
    """Recompute the dataset's memory from its closed runs, oldest first.

    For when the folding rules change: the result is what closing those runs
    today would have produced. Lessons are kept -- they come from published
    improvements, not from runs.
    """
    from .ledger import write

    previous = load(store.state, key)
    lessons = previous["lessons"]
    # Teacher notes come from feedback, not from runs: a rebuild keeps them.
    notes = previous.get("teaching", [])
    path = memory_path(store.state, key)
    if path.exists():
        path.unlink()
    runs = sorted(
        (
            r
            for r in store.list("runs")
            if r.get("dataset_key") == key
            and (r.get("closure") or {}).get("state") == "closed"
        ),
        key=lambda r: r.get("created_at", 0),
    )
    # Ledgers on disk whose run record is gone (a cleaned-up duplicate run)
    # still hold measured cost; they count for the cost profile only.
    from .layout import dataset_dir

    known = {run["id"] for run in runs}
    orphans = []
    for found in sorted(
        (dataset_dir(store.state, key) / "tasks").glob("*/ledger.json")
    ):
        ledger = read_json(found) or {}
        if (
            ledger.get("run_id")
            and ledger["run_id"] not in known
            and ledger.get("cost")
        ):
            orphans.append(ledger)
    entries = [(run.get("created_at", 0), run, None) for run in runs] + [
        (ledger.get("started_at") or 0, None, ledger) for ledger in orphans
    ]
    folded = []
    for _, run, ledger in sorted(entries, key=lambda item: item[0]):
        if run is None:
            record_cost(store, ledger)
            continue
        # The ledger is rewritten too, so both follow the current rules.
        facts, _ = write(store, run["id"])
        _, changed = update(store, facts)
        record_cost(store, facts)
        if changed:
            folded.append(run["id"])
    memory = load(store.state, key)
    if lessons or notes:
        memory["lessons"] = lessons
        memory["teaching"] = notes
        write_json(path, memory)
    return {"dataset_key": key, "folded": folded, "path": str(path)}


def record_lesson(state, key, lesson):
    """A published improvement becomes a lesson; the same slug is updated."""
    memory = load(state, key)
    memory["lessons"] = [
        item for item in memory["lessons"] if item["slug"] != lesson["slug"]
    ] + [lesson]
    memory["updated_at"] = time.time()
    write_json(memory_path(state, key), memory)
    return memory


def context(state, key, limit=6):
    """The compact slice a new task is given at plan time."""
    memory = read_json(memory_path(state, key))
    if not memory or not (memory["episodes"] or memory.get("cost_profiles")):
        return None
    annotated = memory["profile"].get("episodes_annotated", 0)
    patterns = [
        f"{name}: {row['count']} note(s) across {len(row['episodes'])} of "
        f"{annotated} annotated episode(s)"
        for name, row in list(memory["uncertainty"].items())[:limit]
        if name != "failure_reason"
    ]
    return {
        "source": str(memory_path(state, key)),
        "episodes_annotated": annotated,
        "annotated": sorted(memory["episodes"]),
        "profile": memory["profile"],
        "subtasks": memory["subtasks"],
        "uncertainty_patterns": patterns,
        "lessons": [
            {"slug": item["slug"], "text": item["text"], "status": item["status"]}
            for item in memory["lessons"]
        ],
        "cost": memory["cost"],
        # Per agent key: what runs of each agent (API model, local VLM,
        # external MCP) have cost on this dataset, and advice for the next.
        "cost_profiles": {
            key: {"median": row.get("median"), "runs": len(row.get("runs", []))}
            for key, row in memory.get("cost_profiles", {}).items()
        },
        "cost_hints": memory.get("cost_hints", []),
        "note": "Committed, human-reviewed facts about this dataset only. Use them "
        "to plan and to know where evidence was thin before; they are not labels "
        "for episodes that have not been annotated.",
    }


def search(state, query, key=None, limit=20):
    """Plain case-insensitive search over segments and lessons."""
    needle = (query or "").lower()
    folder = memory_path(state, "x").parent
    paths = [memory_path(state, key)] if key else sorted(folder.glob("*.json"))
    hits = []
    for path in paths:
        memory = read_json(path)
        if not memory:
            continue
        for name, episode in sorted(memory["episodes"].items()):
            for segment in episode["segments"]:
                text = " ".join(
                    str(segment.get(k) or "")
                    for k in ("subtask", "outcome", "content", "uncertainty")
                )
                if needle in text.lower():
                    hits.append(
                        {
                            "dataset": memory["dataset_key"],
                            "episode": name,
                            "run_id": episode["run_id"],
                            "revision": episode["revision"],
                            **segment,
                        }
                    )
        for lesson in memory["lessons"]:
            if needle in lesson["text"].lower():
                hits.append({"dataset": memory["dataset_key"], "lesson": lesson})
    return {"query": query, "hits": hits[:limit], "total": len(hits)}
