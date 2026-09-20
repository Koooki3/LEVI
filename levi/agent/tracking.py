"""Core-generated action journal and readable artifact manifests."""

import json
import time

from .store import file_hash


def scope(store, arguments):
    if arguments.get("run_id"):
        return store.get("runs", arguments["run_id"])
    if arguments.get("changeset_id"):
        return store.get(
            "runs", store.get("changes", arguments["changeset_id"])["run_id"]
        )
    if arguments.get("job_id"):
        return store.get(
            "runs", store.get("object_jobs", arguments["job_id"])["run_id"]
        )
    return None


def manifest(wb, run_id):
    run = wb.store.get("runs", run_id)
    root = wb.store.run_dir(run_id)
    evidence = {}
    for episode in sorted(set(run.get("prepared", []) + run.get("completed", []))):
        try:
            for item in wb.store.get("evidence", f"{run_id}:{episode}")["items"]:
                if item.get("artifact"):
                    evidence["evidence/" + item["artifact"]] = {
                        k: item[k]
                        for k in ("episode_index", "timestamp", "camera_key")
                        if k in item
                    }
        except KeyError:
            pass
    artifacts = []
    for path in sorted(root.rglob("*")):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.name in {"result.json", "result.md"}
        ):
            continue
        if any(
            p in {"session", "runtime", "input"} for p in path.relative_to(root).parts
        ):
            continue
        artifacts.append(
            {
                "path": str(path.relative_to(wb.store.state)),
                "name": str(path.relative_to(root)),
                "bytes": path.stat().st_size,
                "sha256": file_hash(path),
                "type": path.suffix.removeprefix("."),
                "evidence": evidence.get(str(path.relative_to(root))),
            }
        )
    from levi.paths import ROOT

    try:
        path_base = str(wb.store.state.relative_to(ROOT))
    except ValueError:
        path_base = "."  # Standalone fixture store
    result = {
        "path_base": path_base,
        "schema_version": 1,
        "run_id": run_id,
        "dataset": run["context"]["repo_id"],
        "status": run["status"],
        "changeset": run.get("changes"),
        "issues": [run["reason"]] if run.get("reason") else [],
        "artifacts": artifacts,
        "generated_at": time.time(),
        "open_command": f"uv run levi agent open {run_id}",
    }
    try:
        exported = wb.store.get("exports", run_id)
        from pathlib import Path

        directory = Path(exported["output_dir"])
        result["export"] = {
            "path": str(directory.relative_to(ROOT))
            if directory.is_relative_to(ROOT)
            else str(directory),
            "validation": exported.get("validation"),
        }
    except KeyError:
        pass
    if run.get("changes"):
        changes = wb.store.get("changes", run["changes"])
        result["review_status"] = changes["status"]
    root.mkdir(parents=True, exist_ok=True)
    (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (root / "result.md").write_text(
        f"# LEVI: {result['dataset']}\n\nRun: {run_id}\n\nStatus: {result['status']}\n\n"
        + "\n".join(f"- {a['path']} ({a['bytes']} bytes)" for a in artifacts)
    )
    return result


def waiting_for(store, run):
    """Whose turn it is on this run, and whether its evidence is still on disk.

    An agent asking "what should I pick up" needs one answer, not a status
    code to interpret. It also needs to know when a run's evidence has been
    cleaned away: the frames are rebuildable from the source, but only by
    calling runs.prepare again, and only while that run's approved plan still
    holds. Reporting a cleaned run as ready would send an agent to read files
    that are not there.
    """
    from .housekeeping import FINISHED

    change = None
    if run.get("changes"):
        try:
            change = store.get("changes", run["changes"])
        except KeyError:
            change = None
    committed = bool(change and change["status"] == "committed")
    finished = run["status"] in FINISHED or committed
    evidence_ready = bool(run.get("prepared")) and not run.get("evidence_cleaned")

    if committed:
        state = "nothing; the annotations are published"
    elif run["status"] == "cancelled":
        state = "nothing; this run was abandoned"
    elif finished:
        state = "human_review"
    elif not (run.get("plan") or {}).get("approval"):
        state = "human_approval"
    elif change and change["status"] == "approved":
        state = "human_commit"
    elif change and change["status"] == "draft":
        state = "human_review"
    elif not evidence_ready:
        state = "agent_prepare"
    else:
        state = "agent_propose"
    return {
        "waiting_for": state,
        "finished": finished,
        "committed": committed,
        "plan_approved": bool((run.get("plan") or {}).get("approval")),
        "evidence_ready": evidence_ready,
        "evidence_cleaned": bool(run.get("evidence_cleaned")),
        "changeset": run.get("changes"),
        "changeset_status": change["status"] if change else None,
    }
