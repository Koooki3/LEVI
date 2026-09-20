"""Atomic, revisioned Parquet storage for object annotations."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ..naming import timestamp_id
from .rle import validate_rle
from .schema import ObjectAnnotation, ObjectEdit, ObjectTrack, ReviewStatus

SCHEMA_VERSION = "levi.sam3.sidecar.v1"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{time.time_ns()!s}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    os.replace(temp, path)


def _write_table(path: Path, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    temp = path.with_suffix(path.suffix + f".{time.time_ns()!s}.tmp")
    pq.write_table(table, temp)
    os.replace(temp, path)


OBJECT_SCHEMA = pa.schema(
    [
        ("episode_index", pa.int64()),
        ("object_id", pa.string()),
        ("concept", pa.string()),
        ("category", pa.string()),
        ("attributes_json", pa.string()),
        ("status", pa.string()),
        ("source", pa.string()),
    ]
)
TRACK_SCHEMA = pa.schema(
    [
        ("episode_index", pa.int64()),
        ("camera_key", pa.string()),
        ("track_id", pa.int64()),
        ("object_id", pa.string()),
        ("concept", pa.string()),
        ("category", pa.string()),
        ("start_frame", pa.int64()),
        ("end_frame", pa.int64()),
        ("mean_score", pa.float32()),
        ("min_score", pa.float32()),
        ("status", pa.string()),
        ("lineage_json", pa.string()),
    ]
)
MASK_SCHEMA = pa.schema(
    [
        ("episode_index", pa.int64()),
        ("frame_index", pa.int64()),
        ("timestamp", pa.float64()),
        ("camera_key", pa.string()),
        ("object_id", pa.string()),
        ("track_id", pa.int64()),
        ("concept", pa.string()),
        ("category", pa.string()),
        ("bbox_xyxy", pa.list_(pa.float32())),
        ("image_size", pa.list_(pa.int32())),
        ("rle_size", pa.list_(pa.int32())),
        ("rle_counts", pa.list_(pa.int64())),
        ("score", pa.float32()),
        ("visible", pa.bool_()),
        ("occluded", pa.bool_()),
        ("status", pa.string()),
        ("source", pa.string()),
        ("prompt", pa.string()),
    ]
)
QA_SCHEMA = pa.schema(
    [
        ("episode_index", pa.int64()),
        ("frame_index", pa.int64()),
        ("camera_key", pa.string()),
        ("track_id", pa.int64()),
        ("kind", pa.string()),
        ("severity", pa.string()),
        ("message", pa.string()),
        ("status", pa.string()),
    ]
)
EVENT_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("episode_index", pa.int64()),
        ("kind", pa.string()),
        ("object_id", pa.string()),
        ("target_object_id", pa.string()),
        ("start_frame", pa.int64()),
        ("end_frame", pa.int64()),
        ("start_timestamp", pa.float64()),
        ("end_timestamp", pa.float64()),
        ("confidence", pa.float32()),
        ("evidence_json", pa.string()),
        ("status", pa.string()),
    ]
)


class SidecarStore:
    """Persist one dataset's object annotations beneath a workspace directory."""

    def __init__(self, root: Path, identity: dict[str, Any] | None = None):
        self.root = root
        self.identity = identity or {}
        self.staging_root = root / "staging"
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def meta_path(self) -> Path:
        return self.root / "meta.json"

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    def initialize(self) -> None:
        if not self.meta_path.exists():
            _write_json(
                self.meta_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "identity": self.identity,
                    "coordinate_system": "pixel_xyxy_half_open",
                    "mask_encoding": "coco_uncompressed_rle_fortran",
                    "review_policy": {
                        "suggested": ">=0.90",
                        "needs_review": "0.60-0.90",
                        "mandatory_review": "<0.60",
                    },
                },
            )

    def current_revision(self) -> str | None:
        if not self.current_path.exists():
            return None
        return json.loads(self.current_path.read_text()).get("revision_id")

    def revision_path(self, revision_id: str) -> Path:
        if not revision_id or "/" in revision_id or "\\" in revision_id:
            raise ValueError("invalid annotation revision")
        return self.root / "revisions" / revision_id

    def list_revisions(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for path in sorted((self.root / "revisions").glob("*/revision.json")):
            values.append(json.loads(path.read_text()))
        return values

    def publish(
        self,
        annotations: list[ObjectAnnotation],
        tracks: list[ObjectTrack] | None = None,
        *,
        qa: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        parent_revision: str | None = None,
        model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        # Pydantic models normally validate at construction time, but review
        # edits can update model attributes in place. Re-validate at the write
        # boundary so a malformed refinement can never reach Parquet.
        annotations = [
            ObjectAnnotation.model_validate(row.model_dump()) for row in annotations
        ]
        for row in annotations:
            validate_rle(row.mask_rle)
            if row.mask_rle["size"] != row.image_size:
                raise ValueError("RLE size must match image_size")
        # Claimed by creating the revision directory itself, so two writers
        # sharing this workspace can never pick the same id.
        revision_id = timestamp_id(self.root / "revisions", create_dir=True)
        revision = self.revision_path(revision_id)
        masks_root = revision / "masks"
        object_rows = self._objects(annotations)
        track_rows = tracks or self._derive_tracks(annotations)
        _write_table(revision / "objects.parquet", object_rows, OBJECT_SCHEMA)
        _write_table(
            revision / "tracks.parquet",
            [self._track_row(row) for row in track_rows],
            TRACK_SCHEMA,
        )
        _write_table(revision / "qa.parquet", qa or [], QA_SCHEMA)
        _write_table(revision / "events.parquet", events or [], EVENT_SCHEMA)
        by_camera: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for row in annotations:
            by_camera.setdefault((row.episode_index, row.camera_key), []).append(
                self._mask_row(row)
            )
        for (episode_index, camera_key), rows in by_camera.items():
            camera_name = camera_key.replace("/", "_").replace("\\", "_")
            _write_table(
                masks_root / f"episode-{episode_index:06d}" / f"{camera_name}.parquet",
                sorted(rows, key=lambda item: (item["frame_index"], item["track_id"])),
                MASK_SCHEMA,
            )
        revision_info = {
            "schema_version": SCHEMA_VERSION,
            "revision_id": revision_id,
            "parent_revision": parent_revision,
            "created_at": datetime.now(UTC).isoformat(),
            "annotation_count": len(annotations),
            "track_count": len(track_rows),
            "model": model or {"provider": "human"},
        }
        _write_json(revision / "revision.json", revision_info)
        _write_json(self.current_path, {"revision_id": revision_id})
        return revision_info

    def read_annotations(self, revision_id: str | None = None) -> list[dict[str, Any]]:
        revision_id = revision_id or self.current_revision()
        if not revision_id:
            return []
        root = self.revision_path(revision_id) / "masks"
        rows: list[dict[str, Any]] = []
        for path in sorted(root.glob("episode-*/**/*.parquet")):
            rows.extend(pq.read_table(path).to_pylist())
        return rows

    def read_episode(
        self, episode_index: int, revision_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self.read_annotations(revision_id)
            if row["episode_index"] == episode_index
        ]

    def apply_edit(self, edit: ObjectEdit) -> dict[str, Any]:
        current = self.current_revision()
        if edit.base_revision and edit.base_revision != current:
            raise ValueError("annotation revision is stale; reload before editing")
        rows = [
            ObjectAnnotation.model_validate(self._from_mask_row(row))
            for row in self.read_annotations(current)
        ]
        selected = [
            row
            for row in rows
            if row.episode_index == edit.episode_index
            and row.camera_key == edit.camera_key
            and (edit.object_id is None or row.object_id == edit.object_id)
            and (edit.track_id is None or row.track_id == edit.track_id)
            and (edit.frame_index is None or row.frame_index == edit.frame_index)
        ]
        if not selected:
            raise ValueError("edit did not match any annotation")
        if edit.operation == "accept":
            for row in selected:
                row.status = ReviewStatus.ACCEPTED
        elif edit.operation == "reject":
            for row in selected:
                row.status = ReviewStatus.REJECTED
        elif edit.operation == "relabel":
            if not edit.concept:
                raise ValueError("relabel requires concept")
            for row in selected:
                row.concept = edit.concept
        elif edit.operation == "occlusion":
            if edit.occluded is None:
                raise ValueError("occlusion edit requires occluded")
            for row in selected:
                row.occluded = edit.occluded
                row.visible = (
                    not edit.occluded if edit.visible is None else edit.visible
                )
        elif edit.operation == "delete":
            rows = [row for row in rows if row not in selected]
        elif edit.operation == "refine":
            self._apply_refinement(selected, edit.payload)
        elif edit.operation in {"split", "merge"}:
            raise ValueError("split/merge require an explicit track operation payload")
        return self.publish(rows, parent_revision=current)

    @staticmethod
    def _apply_refinement(
        rows: list[ObjectAnnotation], payload: dict[str, Any]
    ) -> None:
        bbox = payload.get("bbox_xyxy")
        if bbox is not None and (not isinstance(bbox, list) or len(bbox) != 4):
            raise ValueError("refine bbox_xyxy must contain four numbers")
        rle = payload.get("mask_rle")
        if rle is not None and not isinstance(rle, dict):
            raise ValueError("refine mask_rle must be an object")
        if isinstance(rle, dict):
            validate_rle(rle)
        for row in rows:
            values = row.model_dump()
            if bbox is not None:
                values["bbox_xyxy"] = [float(number) for number in bbox]
            if rle is not None:
                values["mask_rle"] = rle
            values["source"] = "human"
            values["status"] = ReviewStatus.ACCEPTED
            validated = ObjectAnnotation.model_validate(values)
            if validated.mask_rle["size"] != validated.image_size:
                raise ValueError("refine RLE size must match image_size")
            row.bbox_xyxy = validated.bbox_xyxy
            row.mask_rle = validated.mask_rle
            row.source = validated.source
            row.status = validated.status

    @staticmethod
    def _objects(rows: list[ObjectAnnotation]) -> list[dict[str, Any]]:
        grouped: dict[tuple[int, str], ObjectAnnotation] = {}
        for row in rows:
            grouped.setdefault((row.episode_index, row.object_id), row)
        return [
            {
                "episode_index": row.episode_index,
                "object_id": row.object_id,
                "concept": row.concept,
                "category": row.category,
                "attributes_json": "{}",
                "status": row.status.value,
                "source": row.source,
            }
            for row in grouped.values()
        ]

    @staticmethod
    def _derive_tracks(rows: list[ObjectAnnotation]) -> list[ObjectTrack]:
        grouped: dict[tuple[int, str, int], list[ObjectAnnotation]] = {}
        for row in rows:
            grouped.setdefault(
                (row.episode_index, row.camera_key, row.track_id), []
            ).append(row)
        result = []
        for (episode_index, camera_key, track_id), values in grouped.items():
            result.append(
                ObjectTrack(
                    episode_index=episode_index,
                    camera_key=camera_key,
                    track_id=track_id,
                    object_id=values[0].object_id,
                    concept=values[0].concept,
                    category=values[0].category,
                    start_frame=min(row.frame_index for row in values),
                    end_frame=max(row.frame_index for row in values),
                    mean_score=sum(row.score for row in values) / len(values),
                    min_score=min(row.score for row in values),
                    status=(
                        ReviewStatus.ACCEPTED
                        if all(row.status == ReviewStatus.ACCEPTED for row in values)
                        else ReviewStatus.REJECTED
                        if all(row.status == ReviewStatus.REJECTED for row in values)
                        else ReviewStatus.NEEDS_REVIEW
                        if any(
                            row.status == ReviewStatus.NEEDS_REVIEW for row in values
                        )
                        else ReviewStatus.SUGGESTED
                    ),
                )
            )
        return result

    @staticmethod
    def _track_row(row: ObjectTrack) -> dict[str, Any]:
        value = row.model_dump()
        value["lineage_json"] = json.dumps(value.pop("lineage"), ensure_ascii=False)
        value["status"] = row.status.value
        return value

    @staticmethod
    def _mask_row(row: ObjectAnnotation) -> dict[str, Any]:
        return {
            "episode_index": row.episode_index,
            "frame_index": row.frame_index,
            "timestamp": row.timestamp,
            "camera_key": row.camera_key,
            "object_id": row.object_id,
            "track_id": row.track_id,
            "concept": row.concept,
            "category": row.category,
            "bbox_xyxy": row.bbox_xyxy,
            "image_size": row.image_size,
            "rle_size": row.mask_rle["size"],
            "rle_counts": row.mask_rle["counts"],
            "score": row.score,
            "visible": row.visible,
            "occluded": row.occluded,
            "status": row.status.value,
            "source": row.source,
            "prompt": row.prompt,
        }

    @staticmethod
    def _from_mask_row(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["mask_rle"] = {
            "size": value.pop("rle_size"),
            "counts": value.pop("rle_counts"),
        }
        value["status"] = value.get("status", ReviewStatus.SUGGESTED)
        return value
