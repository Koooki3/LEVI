"""Native annotated export through the existing exporter, followed by re-read checks."""

import json

from .formats import DATASETS
from .schema import TaskContext
from .store import Conflict, pin


def export(wb, run):
    from pathlib import Path

    from .store import dataset_lock

    with dataset_lock(wb.store.state, "export-" + run["dataset_key"]):
        try:
            result = wb.store.get("exports", run["id"])
        except KeyError:
            result = None
        if result:
            if not (
                Path(result["output_dir"]) / "levi-roundtrip-report.json"
            ).is_file():
                raise ValueError(
                    "Previously verified export was removed; create a new export from the dataset panel"
                )
            return result
        result = _export(wb, run)
        wb.store.put("exports", run["id"], result)
        return result


def _export(wb, run):
    from backend import app
    from levi.annotations.schema import ObjectAnnotation
    from levi.annotations.sidecar import SidecarStore
    from levi.naming import timestamp_id

    from .evaluation import mask_iou

    if run["status"] not in {"succeeded", "partially_succeeded"}:
        raise Conflict("Commit reviewed changes before export")
    change = wb.store.get("changes", run["changes"])
    if wb.store.head(run["dataset_key"]) != change["published_revision"]:
        raise Conflict("Newer annotations exist; export the current reviewed revision")
    ctx = TaskContext.model_validate(run["context"])
    adapter = DATASETS[ctx.dataset_adapter]
    adapter.verify(ctx, run["manifest"])
    state = adapter.publication_state(ctx)
    parent = app.EXPORT_ROOT / run["dataset_key"]
    parent.mkdir(parents=True, exist_ok=True)
    directory = parent / timestamp_id(parent, create_dir=True) / run["dataset_key"]
    with pin(wb.store.state, run["dataset_key"], wb.store.bundle(run["dataset_key"])):
        result = app._do_export(state, str(directory), True)
        source = app._sidecar(state)
        # Native export names are defined by the existing exporter.
        original = [
            ObjectAnnotation.model_validate(r) for r in source.read_annotations()
        ]
    candidates = list(directory.rglob("objects.parquet"))
    rows = []
    if original:
        if not candidates:
            raise ValueError("Export lost object sidecar")
        # The root with meta.json/current.json resolves exactly the active revision.
        roots = [
            p.parent
            for p in directory.rglob("current.json")
            if p.parent.name in {"sam3", "object_annotations"}
        ]
        if not roots:
            raise ValueError("Export has no active object revision")
        exported = SidecarStore(roots[0])
        rows = [ObjectAnnotation.model_validate(r) for r in exported.read_annotations()]
        key = lambda r: (
            r.episode_index,
            r.camera_key,
            r.frame_index,
            r.object_id,
            r.track_id,
        )
        expected = {key(r): r for r in original}
        actual = {key(r): r for r in rows}
        if expected.keys() != actual.keys():
            raise ValueError("Export changed object identities or frame correspondence")
        for k, row in expected.items():
            other = actual[k]
            if (
                row.image_size != other.image_size
                or row.timestamp != other.timestamp
                or mask_iou(row.mask_rle, other.mask_rle) != 1
            ):
                raise ValueError("Export mask/time roundtrip failed")
    provenance = directory / "meta/levi_agent_provenance.json"
    if not provenance.is_file() or not any(
        c["id"] == change["id"] for c in json.loads(provenance.read_text())
    ):
        raise ValueError("Export lost Agent provenance")
    adapter.verify(ctx, run["manifest"])
    report = {
        "ok": True,
        "object_rows": len(rows),
        "checks": [
            "active revision",
            "source unchanged",
            "provenance re-read",
            "object identity/time/RLE roundtrip",
        ],
        "format": "LeRobot native language columns + LEVI lossless object sidecar + meta/levi_agent_provenance.json",
        "temporal_extension": "subtask identity, attempt, outcome and uncertainty are preserved in Agent provenance, not flattened into standard language fields",
    }
    (directory / "levi-roundtrip-report.json").write_text(json.dumps(report, indent=2))
    wb.store.event(run["id"], "export_validated", report=report)
    return {**result, "validation": report}
