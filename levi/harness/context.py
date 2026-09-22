"""The learner's brief: what LEVI knows locally, in the prompt of a model it runs.

An external agent reads memory through MCP. A model LEVI runs itself (a local
Ollama VLM, an API model) only sees its prompt, so without this the dataset's
memory, the teacher's corrections and the committed examples never reached it.
The brief is built from the harness snapshot frozen into the run at plan time,
so a task keeps the knowledge it was planned with.

Held-out rule: examples never come from an episode the run itself annotates --
the learner must not be handed the answer. Teacher notes are general rules and
may mention any episode.
"""

import json

from .layout import memory_path, read_json

MAX_CHARS = 6000
MAX_EXAMPLES = 2


def _examples(memory, scope, limit=MAX_EXAMPLES):
    rows = []
    for name, episode in sorted(memory.get("episodes", {}).items()):
        index = int(name.rsplit("_", 1)[-1])
        if index in scope or not episode.get("segments"):
            continue
        rows.append(
            {
                "episode": name,
                "duration_seconds": episode.get("duration_seconds"),
                "segments": [
                    {
                        k: s.get(k)
                        for k in (
                            "subtask",
                            "start",
                            "end",
                            "outcome",
                            "content",
                            "uncertainty",
                        )
                        if s.get(k) not in (None, "")
                    }
                    for s in episode["segments"]
                ],
            }
        )
        if len(rows) >= limit:
            break
    return rows


def brief(state, run, phase=None):
    """A bounded dict for the learner, or None when nothing is known locally."""
    from . import knowledge
    from .teaching import workspace_notes

    key = run["dataset_key"]
    scope = set(run["context"]["episodes"])
    workflow = run["context"]["workflow"]["kind"]
    snapshot = (run.get("harness") or {}).get("memory") or {}
    memory = read_json(memory_path(state, key)) or {}
    frozen_at = (run.get("harness") or {}).get("frozen_at")
    notes = [
        row
        for row in memory.get("teaching", [])
        if row.get("workflow") in (workflow, None)
        and (frozen_at is None or row.get("at", 0) <= frozen_at)
    ]
    value = {
        "about": "LEVI's local memory of this dataset: facts from human-reviewed "
        "work and a teacher's corrections. Use them to judge; never copy them "
        "as labels for the episodes you are given.",
        "dataset_profile": snapshot.get("profile") or memory.get("profile"),
        "subtask_vocabulary": {
            name: {
                "outcomes": row.get("outcomes"),
                "median_seconds": row.get("median_seconds"),
            }
            for name, row in (
                snapshot.get("subtasks") or memory.get("subtasks") or {}
            ).items()
        },
        "recurring_uncertainty": snapshot.get("uncertainty_patterns") or [],
        "teacher_notes": [f"({row['count']}x) {row['note']}" for row in notes[:12]],
        "levi_notes": [row["note"] for row in workspace_notes(state, workflow)],
        # LEVI's built-in, dataset-agnostic rules (levi/knowledge/annotation.md).
        "levi_rules": knowledge.texts("annotation"),
        "lessons": [
            row["text"]
            for row in (snapshot.get("lessons") or memory.get("lessons", []))
        ],
        "examples_from_other_episodes": _examples(memory, scope),
        "phase": phase,
    }
    value = {k: v for k, v in value.items() if v}
    if set(value) <= {"about", "phase", "levi_rules"}:
        # Rules alone are not memory of this dataset; say only them.
        return {"levi_rules": value["levi_rules"]} if "levi_rules" in value else None
    # Bounded: drop examples first, then trim notes, until it fits.
    while len(json.dumps(value, ensure_ascii=False)) > MAX_CHARS:
        if value.get("examples_from_other_episodes"):
            value["examples_from_other_episodes"].pop()
            if not value["examples_from_other_episodes"]:
                value.pop("examples_from_other_episodes")
        elif len(value.get("teacher_notes", [])) > 3:
            value["teacher_notes"].pop()
        else:
            break
    return value


def as_text(value):
    if not value:
        return ""
    return "\n\n## LEVI local memory (reviewed; context, not labels)\n" + json.dumps(
        value, ensure_ascii=False, indent=1
    )
