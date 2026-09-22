"""The task fact ledger: what one run did, measured by LEVI where it can be.

Written to ``outputs/LEVI/datasets/<name>/tasks/<run>/ledger.json``. It is
rebuilt from the journal each time, so writing it again (after a late usage
report, say) replaces it with the same facts plus the new ones -- never a
second ledger for the same run.

Two token figures are kept apart on purpose. ``delivered`` is what LEVI itself
sent back to the agent (tool-result bytes and images), measured on this side.
``reported`` is the agent's own account, which also covers its reasoning and
anything LEVI never saw. Neither is a price.
"""

import time
from collections import defaultdict

from .layout import task_dir, write_json

# Bytes of JSON per token, the rough rule used for text tool results.
BYTES_PER_TOKEN = 4
# Pixels per image token for vision models that bill by area.
PIXELS_PER_TOKEN = 750


def all_events(store, run_id):
    rows, after = [], 0
    while True:
        page = store.events(run_id, after)
        if not page:
            return rows
        rows += page
        after = page[-1]["seq"]


# The web UI's principal before events carried their channel.
LEGACY_HUMAN = {"local-human"}


def channel(event):
    """Who a call answered: the agent pays for its responses, a person's
    browser does not. Older events are classified by their principal."""
    if event.get("channel"):
        return event["channel"]
    return "human" if event.get("principal") in LEGACY_HUMAN else "agent"


def _tools(events):
    return {
        who: _tally([e for e in events if channel(e) == who])
        for who in ("agent", "human")
    }


def _tally(events):
    tools = defaultdict(
        lambda: {
            "calls": 0,
            "failed": 0,
            "seconds": 0.0,
            "response_bytes": 0,
            "images": 0,
            "image_pixels": 0,
        }
    )
    for event in events:
        if event.get("type") not in {"action.completed", "action.failed"}:
            continue
        row = tools[event["tool"]]
        row["calls"] += 1
        row["failed"] += event["type"] == "action.failed"
        row["seconds"] += event.get("elapsed_seconds") or 0.0
        row["response_bytes"] += event.get("response_bytes") or 0
        row["images"] += event.get("images") or 0
        row["image_pixels"] += event.get("image_pixels") or 0
    for row in tools.values():
        row["seconds"] = round(row["seconds"], 3)
    return dict(sorted(tools.items()))


def _episodes(store, run):
    """Per-episode outcome of the run: what was proposed and what it warned."""
    rows = []
    for episode in run["context"]["episodes"]:
        key = f"{run['id']}:{episode}"
        try:
            shard = store.get("shards", key)
        except KeyError:
            rows.append({"episode": f"episode_{episode:06d}", "processed": False})
            continue
        proposals = shard["output"]["proposals"]
        try:
            warnings = store.get("quality", key)["warnings"]
        except KeyError:
            warnings = []
        try:
            evidence = store.get("evidence", key)
            frames = len(evidence["items"])
            duration = evidence["summary"]["end"] - evidence["summary"]["start"]
            tasks = evidence["summary"].get("tasks", [])
        except KeyError:
            frames, duration, tasks = 0, None, []
        rows.append(
            {
                "episode": f"episode_{episode:06d}",
                "processed": True,
                "duration_seconds": round(duration, 2) if duration else None,
                "evidence_frames": frames,
                "tasks": tasks,
                "segments": [
                    {
                        "subtask": p.get("subtask_id"),
                        "start": p.get("start"),
                        "end": p.get("end"),
                        "outcome": p.get("outcome"),
                        "attempt": p.get("attempt"),
                    }
                    for p in proposals
                ],
                "warnings": [w["reason"] for w in warnings],
            }
        )
    return rows


def build(store, run_id):
    run = store.get("runs", run_id)
    events = all_events(store, run_id)
    by_channel = _tools(events)
    # Tokens are the agent's: what the web UI fetched for a person is listed
    # separately and never counted as model input.
    tools = by_channel["agent"]
    # Bookkeeping after the task ended (closing it, reading it back) is not
    # part of the task's wall time.
    times = [
        e["time"]
        for e in events
        if "time" in e and e.get("type") not in {"closed", "closure_failed"}
    ]
    started = run.get("created_at") or (times[0] if times else None)
    finished = times[-1] if times else None
    delivered_bytes = sum(t["response_bytes"] for t in tools.values())
    delivered_pixels = sum(t["image_pixels"] for t in tools.values())
    samples = [s for s in store.list("usage_samples") if s.get("run_id") == run_id]
    samples.sort(key=lambda s: s.get("at", 0))
    change = None
    if run.get("changes"):
        try:
            change = store.get("changes", run["changes"])
        except KeyError:
            change = None
    return {
        "schema": "levi.harness.ledger.v1",
        "run_id": run_id,
        "dataset": run["context"]["repo_id"],
        "dataset_key": run["dataset_key"],
        "workflow": run["context"]["workflow"]["kind"],
        "instruction": run["context"].get("instruction"),
        "provider": run["context"].get("provider"),
        "status": run["status"],
        "harness": run.get("harness", {}),
        "episodes_in_scope": len(run["context"]["episodes"]),
        "episodes_completed": len(run.get("completed", [])),
        "wall_seconds": round(finished - started, 1) if started and finished else None,
        "started_at": started,
        "finished_at": finished,
        "tools": tools,
        "human_calls": by_channel["human"],
        "tokens": {
            "delivered": {
                "response_bytes": delivered_bytes,
                "images": sum(t["images"] for t in tools.values()),
                "image_pixels": delivered_pixels,
                "estimated_input_tokens": delivered_bytes // BYTES_PER_TOKEN
                + delivered_pixels // PIXELS_PER_TOKEN,
                "method": f"bytes/{BYTES_PER_TOKEN} + pixels/{PIXELS_PER_TOKEN}, "
                "measured by LEVI on the responses it sent to the agent",
            },
            "metered": run.get("tokens"),
            "reported": samples[-1] if samples else None,
        },
        "published_revision": change.get("published_revision") if change else None,
        "changeset": change["id"] if change else None,
        "decisions": change.get("decisions", {}) if change else {},
        "episodes": _episodes(store, run),
        "closure": run.get("closure"),
        "written_at": time.time(),
    }


def write(store, run_id):
    ledger = build(store, run_id)
    from .cost import of

    ledger["cost"] = of(ledger, store.get("runs", run_id), all_events(store, run_id))
    path = task_dir(store.state, ledger["dataset_key"], run_id) / "ledger.json"
    write_json(path, ledger)
    return ledger, path
