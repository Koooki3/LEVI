"""Synthetic live workspaces for the analysis-record tests: dataset state
files, a run store with change sets, stats records. No rollout, no service,
no network; every number is built here so the tests can compare to a hand
count."""

import json
import sqlite3
from pathlib import Path

from test_live_service import cfg

from levi.live import backfill, jsonio, mirror, stats


def workspace(tmp_path):
    c = cfg(tmp_path)
    c.live_dir.mkdir(parents=True, exist_ok=True)
    return c


def put_state(c, name, group, task, demos, **extra):
    state = {"group": group, "task_folder": task, "demos": demos, **extra}
    jsonio.write(mirror.state_path(c, name), state)
    mirror._STATE_CACHE.clear()
    return state


def put_changes(c, changeset, proposals, decisions=None):
    path = Path(c.workspace) / backfill.STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, body TEXT NOT NULL,"
        " PRIMARY KEY(kind,id))"
    )
    body = {"proposals": proposals, "decisions": decisions or {}}
    db.execute(
        "INSERT OR REPLACE INTO records VALUES('changes',?,?)",
        (changeset, json.dumps(body)),
    )
    db.commit()
    db.close()


def seg(episode, sid, start, end, outcome=None, **over):
    item = {
        "kind": "segment",
        "episode_index": episode,
        "subtask_id": sid,
        "start": start,
        "end": end,
        "style": "subtask",
    }
    if outcome:
        item["outcome"] = outcome
    item.update(over)
    return item


def row(episode, *, changeset=None, operator=None, verdict=None, state="done", **over):
    value = {
        "state": state,
        "episode_index": episode,
        "run_id": "s1",
        "completed_at": 100.0 + episode,
        "temporal": {"changeset": changeset, "committed_at": 200.0 + episode}
        if changeset
        else {},
    }
    if operator:
        value["operator_label"] = {"outcome": operator, "by": "operator"}
    if verdict:
        value["verdict"] = {
            "outcome": verdict,
            "undecided": False,
            "review": "auto",
            "at": 210.0 + episode,
        }
    value.update(over)
    return value


def record(dataset="g__t", demo="demo_0000", group="g", task="t", **over):
    value = {
        "schema": "levi.live.analysis.v1",
        "at": 1.0,
        "labelled_at": 1.0,
        "dataset": dataset,
        "group": group,
        "task": task,
        "demo": demo,
        "episode_index": 0,
        "session": "s1",
        "operator": None,
        "online": None,
        "agent": None,
        "segments": None,
        "spec": None,
        "capability": None,
    }
    value.update(over)
    return value


def segments_block(*triples):
    """(id, start, end[, outcome]) -> a record's ``segments`` block."""
    from levi.live import analysis

    items = [
        {
            "id": t[0],
            "start": t[1],
            "end": t[2],
            "duration": round(t[2] - t[1], 3),
            "outcome": t[3] if len(t) > 3 else None,
        }
        for t in triples
    ]
    return analysis.segment_block(items)


def operator(outcome):
    return {"outcome": outcome, "by": "operator"}


def agent(outcome, undecided=False):
    return {
        "outcome": outcome,
        "undecided": undecided,
        "review": "auto",
        "reviewed": False,
    }


def stats_row(c, dataset, demo, **parts):
    row_ = stats.normalize({"dataset": dataset, "demo": demo, "at": 300.0, **parts})
    stats.record(c.live_dir, row_)
