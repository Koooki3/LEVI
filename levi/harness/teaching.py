"""Teaching memory: what a teacher corrected, kept where the learner sees it.

A supervised phase leaves a record in the store (learner output, the teacher's
decision, note and, for a revision, the corrected output). That record used to
stop there: the next task gave the learner the same prompt and it made the same
mistakes. Now every piece of feedback is also

- written to ``outputs/LEVI/datasets/<name>/teaching/<run>/episode_NNNNNN-<phase>.json``
  (plan §10.2), readable, one file per phase, the raw material for training a
  local model later; and
- folded into the dataset memory as a *note* (the teacher's words, with where
  and how often it applied). Notes are what ``context.brief`` hands the learner
  at plan time.

Notes are deduplicated by text; an accepted phase with no note teaches nothing
new and adds nothing. Workspace-wide notes (about using LEVI rather than about
one dataset) go to ``workbench/memory/workspace.json``.
"""

import time

from .layout import dataset_dir, memory_path, read_json, write_json

KEEP_NOTES = 40
WORKSPACE = "workspace"


def record_path(state, key, run_id, episode, phase):
    return (
        dataset_dir(state, key)
        / "teaching"
        / run_id
        / f"episode_{episode:06d}-{phase}.json"
    )


def _fold_note(memory, note):
    notes = memory.setdefault("teaching", [])
    for row in notes:
        if row["note"] == note["note"]:
            row["count"] += 1
            row["last_run"] = note["run_id"]
            row["at"] = note["at"]
            row["decisions"][note["decision"]] = (
                row["decisions"].get(note["decision"], 0) + 1
            )
            break
    else:
        notes.append(
            {
                "note": note["note"],
                "scope": note["scope"],
                "workflow": note.get("workflow"),
                "phase": note.get("phase"),
                "count": 1,
                "decisions": {note["decision"]: 1},
                "first_run": note["run_id"],
                "last_run": note["run_id"],
                "at": note["at"],
            }
        )
    notes.sort(key=lambda row: (-row["count"], -row["at"]))
    del notes[KEEP_NOTES:]


def record(store, teaching, key, workflow=None):
    """Keep one piece of teacher feedback as a file and as a memory note."""
    feedback = teaching.get("feedback") or {}
    if not feedback:
        return None
    path = record_path(
        store.state, key, teaching["run_id"], teaching["episode"], teaching["phase"]
    )
    write_json(
        path,
        {
            "schema": "levi.harness.teaching.v1",
            "run_id": teaching["run_id"],
            "dataset_key": key,
            "episode": f"episode_{teaching['episode']:06d}",
            "phase": teaching["phase"],
            "mode": teaching.get("mode"),
            "decision": feedback["decision"],
            "note": feedback.get("note", ""),
            "learner_output": teaching.get("learner_output"),
            "accepted_output": teaching.get("accepted_output"),
            "evidence_ids": [row["id"] for row in teaching.get("evidence", [])],
            "teacher": teaching.get("teacher"),
            "reviewed_at": teaching.get("reviewed_at"),
        },
    )
    text = (feedback.get("note") or "").strip()
    if text and not (feedback["decision"] == "accept" and len(text) < 40):
        # "Looks right" on an accepted phase carries no lesson.
        memory = read_json(memory_path(store.state, key)) or {
            "schema": "levi.harness.memory.v1",
            "dataset_key": key,
            "sources": [],
            "episodes": {},
            "profile": {},
            "subtasks": {},
            "uncertainty": {},
            "cost": {},
            "lessons": [],
        }
        _fold_note(
            memory,
            {
                "note": text,
                "scope": "dataset",
                "workflow": workflow,
                "phase": teaching["phase"],
                "decision": feedback["decision"],
                "run_id": teaching["run_id"],
                "at": time.time(),
            },
        )
        memory["updated_at"] = time.time()
        write_json(memory_path(store.state, key), memory)
    return path


def note_workspace(state, text, *, topic, run_id=None):
    """A lesson about using LEVI itself, for every dataset."""
    memory = read_json(memory_path(state, WORKSPACE)) or {
        "schema": "levi.harness.workspace-memory.v1",
        "teaching": [],
    }
    _fold_note(
        memory,
        {
            "note": text.strip(),
            "scope": "workspace",
            "workflow": topic,
            "phase": topic,
            "decision": "teach",
            "run_id": run_id or "teacher",
            "at": time.time(),
        },
    )
    memory["updated_at"] = time.time()
    write_json(memory_path(state, WORKSPACE), memory)
    return memory


def workspace_notes(state, topic=None, limit=12):
    memory = read_json(memory_path(state, WORKSPACE)) or {}
    rows = [
        row
        for row in memory.get("teaching", [])
        if topic is None or row.get("workflow") in (topic, None, "general")
    ]
    return rows[:limit]
