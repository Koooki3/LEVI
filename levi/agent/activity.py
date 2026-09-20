"""What an agent is doing to this workspace, as it happens.

Run events answer "what happened in run X". This answers the question a person
watching the workbench actually has: what is the agent doing right now, to
which dataset, and where did the result land. Every channel -- the web UI, the
REST API, the MCP bridge and a managed Pilot -- passes through one dispatcher,
so recording there covers all of them without a second audit path.

The journal reuses the run-event table under a fixed stream id, so it inherits
its sequencing, persistence and cursor semantics rather than inventing a
parallel mechanism.
"""

import time

STREAM = "workspace-activity"
KEEP = 500

# Calls a person does not need narrated: polling, discovery and paging.
QUIET = {
    "capabilities.list",
    "objects.frame",
    "objects.get",
    "runs.events",
    "runs.get",
    "runs.list",
    "workspace.get_context",
}

# What each capability produces, in words a person can act on. The dispatcher
# adds the dataset, the episode and the artifact path.
PHRASES = {
    "annotations.propose_events": "proposed events",
    "annotations.propose_segments": "proposed subtask segments",
    "changes.approve": "approved the changes",
    "changes.commit": "committed the annotations",
    "changes.diff": "read the staged changes",
    "changes.edit": "edited the draft",
    "changes.rebase": "rebased the draft onto the published revision",
    "changes.undo": "drafted an undo",
    "changes.validate": "validated the changes",
    "datasets.inspect": "inspected a dataset",
    "evidence.read": "read evidence",
    "evidence.refine": "refined evidence around a boundary",
    "export.plan": "planned an export",
    "export.run": "exported the dataset",
    "media.sample": "sampled evidence",
    "objects.detect": "measured object candidates",
    "objects.edit": "edited object masks",
    "objects.inspect": "reviewed staged masks",
    "objects.plan": "planned a SAM3 scope",
    "objects.propose": "proposed object masks",
    "objects.run": "ran the SAM3 worker",
    "objects.strategy": "chose an object-annotation path",
    "plans.approve": "approved the plan",
    "plans.clarify": "checked the plan's requirements",
    "plans.estimate": "estimated the cost",
    "plans.review_pilot": "reviewed the pilot",
    "runs.abandon": "abandoned a run",
    "runs.execute": "ran the model",
    "runs.finish": "froze the completed episodes",
    "runs.plan": "created a run plan",
    "runs.prepare": "prepared evidence",
    "runs.report_usage": "reported its token use",
    "runs.result": "read the artifact manifest",
    "workspace.clean": "cleaned up regenerable files",
}


def channel(principal):
    """Which door this call came through, in the words the UI shows."""
    if getattr(principal, "human", False):
        return "human"
    return "agent"


def artifacts(name, value, run=None):
    """Paths a person may want to copy, produced by this call."""
    found = []
    if not isinstance(value, dict):
        return found
    if name == "changes.commit" and value.get("revision"):
        dataset = (run or {}).get("dataset_key", "")
        found.append(
            {
                "label": "committed revision",
                "path": f"outputs/LEVI/workbench/agent/datasets/{dataset}"
                f"/revisions/{value['revision']}",
            }
        )
    if name == "export.run" and value.get("path"):
        found.append({"label": "export", "path": value["path"]})
    if name == "objects.propose" and value.get("job_id"):
        found.append({"label": "staged masks", "job": value["job_id"]})
    if name == "workspace.clean" and value.get("applied"):
        found.append(
            {"label": "freed", "detail": f"{round(value.get('bytes', 0) / 1e6, 1)} MB"}
        )
    return found


def record(
    store,
    *,
    name,
    principal,
    status,
    dataset=None,
    run=None,
    detail=None,
    elapsed=None,
    value=None,
    error=None,
):
    """One line of the story, appended where the UI can stream it."""
    if name in QUIET and status != "failed":
        return None
    row = {
        "at": time.time(),
        "tool": name,
        "action": PHRASES.get(name, name),
        "channel": channel(principal),
        "principal": principal.id,
        "status": status,
        "dataset": dataset,
        "run": (run or {}).get("id"),
        "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
        "detail": detail or {},
        "artifacts": artifacts(name, value, run) if value is not None else [],
        "error": error,
    }
    store.event(STREAM, "activity", **row)
    return row


def recent(store, after=0, limit=KEEP):
    rows = store.events(STREAM, after)
    return rows[-limit:] if limit else rows
