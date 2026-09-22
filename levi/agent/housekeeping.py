"""Remove what an agent run can regenerate; keep what proves the work.

An annotation run leaves three kinds of files behind: the frozen input
snapshot and evidence images (regenerable from the dataset), the staged
drafts of runs that were never committed (still someone's unfinished work),
and the committed revisions plus their provenance and inverse patches (the
audit trail). Only the first kind is safe to delete without asking, and only
for runs that already ended.
"""

import shutil
import time
from pathlib import Path

# A run that is still planned/waiting may still be resumed by its agent.
FINISHED = {"succeeded", "partially_succeeded", "cancelled", "failed"}
REGENERABLE = ("input", "evidence")


def _size(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def plan(store, run_dir_for, *, dataset=None, older_than_days=0, include_drafts=False):
    """What a cleanup would remove, per run, without touching anything."""
    cutoff = time.time() - older_than_days * 86400
    items, total = [], 0
    for run in store.list("runs"):
        if dataset and run["context"]["repo_id"] != dataset:
            continue
        committed = bool(
            run.get("changes")
            and store.get("changes", run["changes"])["status"] == "committed"
        )
        finished = run["status"] in FINISHED or committed
        if not finished and not include_drafts:
            reason = "run is still open; resume or cancel it first"
            items.append(
                {"run_id": run["id"], "skipped": reason, "bytes": 0, "paths": []}
            )
            continue
        if run.get("created_at", 0) > cutoff:
            items.append(
                {
                    "run_id": run["id"],
                    "skipped": f"newer than {older_than_days} day(s)",
                    "bytes": 0,
                    "paths": [],
                }
            )
            continue
        directory = run_dir_for(run["id"])
        paths, size = [], 0
        for name in REGENERABLE:
            target = directory / name
            if target.is_dir():
                paths.append(str(target))
                size += _size(target)
        for sheet in directory.glob("*--sheet-*.png"):
            paths.append(str(sheet))
            size += sheet.stat().st_size
        if paths:
            items.append(
                {
                    "run_id": run["id"],
                    "dataset": run["context"]["repo_id"],
                    "committed": committed,
                    "status": run["status"],
                    "bytes": size,
                    "paths": paths,
                }
            )
            total += size
    return {
        "schema": "levi.agent.cleanup.v1",
        "regenerable": (
            "Evidence is rebuilt from the untouched dataset by runs.prepare, "
            "as long as that run's approved plan is still valid; changing the "
            "workflow skills or the plan means a new run instead."
        ),
        "runs": items,
        "bytes": total,
        "removes": "frozen input snapshots and evidence images of finished runs",
        "keeps": [
            "committed revisions, provenance and inverse patches",
            "staged drafts of runs that are still open",
            "run records, events and usage samples",
        ],
    }


def apply(store, run_dir_for, **kwargs):
    """Delete exactly what plan() listed; evidence is regenerated on demand."""
    report = plan(store, run_dir_for, **kwargs)
    removed = 0
    for item in report["runs"]:
        for path in item["paths"]:
            target = Path(path)
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                target.unlink(missing_ok=True)
            removed += 1
        if item["paths"]:
            # The evidence ledger stays: it records what was read and its
            # hashes, so a later reader learns the images were cleaned, not
            # that they never existed.
            store.mutate(
                "runs",
                item["run_id"],
                lambda run: run.update(evidence_cleaned=time.time()),
            )
    report["applied"] = True
    report["removed_paths"] = removed
    return report


# Records a run owns. Removing a run without them leaves rows nothing can
# reach and a live activity panel narrating work that no longer exists.
OWNED = (
    "changes",
    "shards",
    "evidence",
    "quality",
    "object_jobs",
    "detections",
    "object_evidence",
    "usage_samples",
    # A supervised run's teaching phases and its cached model answers are
    # keyed "<run>:<episode>:<phase>"; left behind they outlive the run.
    "teaching",
    "model_cache",
)


def _owned_ids(store, kind, run_ids):
    """Records belonging to these runs.

    Ownership is either a field in the body or the key itself: evidence,
    shards and quality are keyed "<run>:<episode>", and their bodies do not
    repeat the id.
    """
    found = []
    for key in store.ids(kind):
        if str(key).split(":", 1)[0] in run_ids:
            found.append(key)
            continue
        try:
            body = store.get(kind, key)
        except KeyError:
            continue
        if (body.get("run_id") or body.get("run")) in run_ids:
            found.append(key)
    return found


def reset(store, run_dir_for, dataset, *, apply=False):
    """Remove one dataset's agent history: runs, records, revisions, evidence.

    This is the deliberate counterpart to `clean`, which only frees what can
    be regenerated. Reset removes the record of the work itself, including
    revisions the agent published, and returns the dataset to having no agent
    history at all. It never touches another dataset, the connection grants,
    the provider profiles, or annotations that were not produced by these runs.
    """
    runs = [
        record
        for record in store.list("runs")
        if record["context"]["repo_id"] == dataset
        or record.get("dataset_key") == dataset.split("/")[-1]
    ]
    run_ids = {record["id"] for record in runs}
    records = {kind: _owned_ids(store, kind, run_ids) for kind in OWNED}
    records = {kind: ids for kind, ids in records.items() if ids}

    key = runs[0]["dataset_key"] if runs else dataset.split("/")[-1]
    tree = store.root / "datasets" / key
    # Natural-language tasks and what the harness learned from these runs:
    # task ledgers, teaching files, the dataset's memory and its improvement
    # candidates. Kept, the next task's brief would cite work that is gone.
    from levi.harness import layout

    tasks = [
        record["id"]
        for record in store.list("tasks")
        if record.get("dataset_key") == key
    ]
    harness = [
        layout.dataset_dir(store.state, key) / "tasks",
        layout.dataset_dir(store.state, key) / "teaching",
        layout.improvements_dir(store.state, key),
    ]
    memory = layout.memory_path(store.state, key)
    revisions = (
        sorted(p.name for p in (tree / "revisions").iterdir())
        if (tree / "revisions").is_dir()
        else []
    )
    report = {
        "schema": "levi.agent.reset.v1",
        "dataset": dataset,
        "runs": sorted(run_ids),
        "records": {kind: len(ids) for kind, ids in records.items()},
        "published_revisions": revisions,
        "tasks": len(tasks),
        "harness": [str(p) for p in [*harness, memory] if p.exists()],
        "bytes": _size(tree) if tree.exists() else 0,
        "path": str(tree),
        "keeps": [
            "other datasets entirely",
            "connection grants, provider profiles and settings",
            "annotations that no agent run produced",
            "the dataset's quality reports and the workspace-wide memory",
        ],
        "applied": False,
    }
    if not apply:
        return report

    # Order matters. Locating a run's directory reads its record, so every
    # path is resolved while the records still exist. Files go before records:
    # if the process stops halfway, what remains is a record pointing at a
    # missing directory -- visible, and reported as cleaned by evidence.read --
    # rather than orphaned files that nothing refers to any more. The previous
    # order dropped the records first and then failed to find the directories.
    directories = []
    for run_id in sorted(run_ids):
        try:
            directories.append(run_dir_for(run_id))
        except (KeyError, ValueError):
            continue
    failures = []
    for directory in [*directories, tree, *harness]:
        if not directory.exists():
            continue
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            failures.append(f"{directory}: {exc}")
    if failures:
        # Leave the records in place so the work stays visible and the reset
        # can be retried; a half-removed history is worse than none removed.
        report["not_removed"] = failures
        return report

    memory.unlink(missing_ok=True)
    store.drop_receipts(records.get("changes", []))
    for kind, ids in records.items():
        store.drop(kind, ids)
    store.drop("tasks", tasks)
    store.drop("runs", sorted(run_ids))
    # The activity journal narrates these runs; leaving its entries would keep
    # the live panel describing work whose record is gone.
    from .activity import STREAM

    store.drop_events([*sorted(run_ids), STREAM])
    store.drop_head(key)
    # Per-run lock files would otherwise outlive the runs they guarded.
    for run_id in run_ids:
        (store.root / "locks" / f"run-{run_id}.lock").unlink(missing_ok=True)
    report["applied"] = True
    report["not_removed"] = []
    return report
