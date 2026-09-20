"""Conflict-checked inverse file patches, not workspace directory rollback."""

import shutil

from .runtime import new_id
from .schema import ChangeSet
from .store import Conflict, annotation_digest


def capture(folder, change):
    paths = set()
    from .formats import ANNOTATIONS

    for index, p in enumerate(change["proposals"]):
        if change.get("decisions", {}).get(str(index)) != "rejected":
            paths.update(ANNOTATIONS[p["kind"]].paths(p))
    if change.get("object_jobs"):
        paths.add("object_annotations/current.json")
    inverse = []
    for relative in sorted(paths):
        source = folder / relative
        backup = folder / "inverse" / change["id"] / relative
        if source.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, backup)
        inverse.append({"path": relative, "existed": source.exists()})
    return inverse


def propose(wb, id):
    original = wb.store.get("changes", id)
    if original["status"] != "committed" or not original.get("published_revision"):
        raise ValueError("Only a published ChangeSet can be undone")
    run = wb.store.get("runs", original["run_id"])
    if wb.store.head(run["dataset_key"]) != original["published_revision"]:
        raise Conflict(
            "Later edits exist; automatic inverse patch would overwrite them"
        )
    undo_run = {
        **run,
        # Second-precision ids collide when an undo follows its commit inside
        # the same second; an unchecked id silently overwrote the record it
        # was undoing.
        "id": new_id({item.split("-", 1)[-1] for item in wb.store.ids("runs")}),
        "status": "waiting_for_review",
        "changes": None,
        "input_run": run.get("input_run", run["id"]),
        "base_revision": original["published_revision"],
        "base_content": annotation_digest(wb.store.state, run["dataset_key"]),
    }
    wb.store.put("runs", undo_run["id"], undo_run)
    evidence_root = wb.store.run_dir(run["id"]) / "evidence"
    if evidence_root.exists():
        shutil.copytree(evidence_root, wb.store.run_dir(undo_run["id"]) / "evidence")
    for ep in run["completed"]:
        wb.store.put(
            "evidence",
            f"{undo_run['id']}:{ep}",
            wb.store.get("evidence", f"{run['id']}:{ep}"),
        )
    change = ChangeSet(
        id=new_id(set(wb.store.ids("changes"))),
        run_id=undo_run["id"],
        base_revision=original["published_revision"],
        proposals=[],
        provenance={
            "inverse_of": id,
            "coverage": "inverse patch",
            "reviewer_type": None,
        },
    ).model_dump()
    change["undo_of"] = id
    wb.store.put("changes", change["id"], change)
    wb.store.mutate("runs", undo_run["id"], lambda r: r.update(changes=change["id"]))
    return change


def apply(wb, folder, change, dataset):
    original = wb.store.get("changes", change["undo_of"])
    source = (
        wb.store.bundle(dataset, original["published_revision"])
        / "inverse"
        / original["id"]
    )
    for patch in original["inverse"]:
        path = folder / patch["path"]
        if patch["existed"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / patch["path"], path)
        else:
            path.unlink(missing_ok=True)
