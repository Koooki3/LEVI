"""The student model store: one folder per distilled model.

``$LEVI_WORKSPACE/checkpoints/segmentation/<name>/`` holds ``weights.pth``
and a ``manifest.json`` that records everything a user must be able to check
before trusting a student: its concepts (class order), the teacher that
labelled its training frames, the training and held-out episodes, the
held-out scores against that teacher, the tracker settings and the licences.
A model is listed only once its manifest is complete; a failed distillation
removes its half-written folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .. import paths

SCHEMA = "levi.segmentation.student.v1"
MANIFEST = "manifest.json"
WEIGHTS = "weights.pth"
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
ARCHITECTURES = ("rf-detr-seg-nano", "rf-detr-seg-small", "rf-detr-seg-medium")
DEFAULT_TRACKER = {"lost_buffer": 300, "activation": 0.5, "matching": 0.8, "min_frames": 1}
LICENCES = {
    "student_code_and_base_weights": "Apache-2.0 (RF-DETR, Roboflow)",
    "tracker": "MIT (supervision ByteTrack)",
    "teacher": "SAM License (Meta); applies to the SAM3 weights whichever mirror they come from",
}


def root() -> Path:
    configured = os.getenv("LEVI_SEG_MODEL_DIR")
    if configured:
        return paths.inside(configured)
    return paths.inside(paths.CHECKPOINTS / "segmentation")


def base_weights_dir() -> Path:
    """Where RF-DETR keeps its COCO starting weights (``RF_HOME``)."""
    return root() / "_base"


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(NAME_PATTERN, name or "")) and not name.startswith("_")


def folder(name: str) -> Path:
    if not valid_name(name):
        raise ValueError(f"Invalid model name {name!r}")
    return root() / name


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def problems(manifest: dict[str, Any], directory: Path) -> list[str]:
    issues = []
    if manifest.get("schema") != SCHEMA:
        issues.append(f"schema is not {SCHEMA}")
    concepts = manifest.get("concepts")
    if not isinstance(concepts, list) or not concepts or not all(isinstance(c, str) and c for c in concepts):
        issues.append("concepts must be a non-empty list of names")
    if manifest.get("architecture") not in ARCHITECTURES and manifest.get("provider") != "fake":
        issues.append(f"architecture must be one of {', '.join(ARCHITECTURES)}")
    weights = directory / str(manifest.get("weights") or WEIGHTS)
    if not weights.is_file() or weights.stat().st_size == 0:
        issues.append(f"weights file {weights.name} is missing")
    return issues


def load(name: str) -> tuple[Path, dict[str, Any]]:
    directory = folder(name)
    manifest = _read(directory / MANIFEST)
    if manifest is None:
        raise KeyError(f"No segmentation model named {name!r}")
    issues = problems(manifest, directory)
    if issues:
        raise ValueError(f"Model {name} is not usable: " + "; ".join(issues))
    return directory, manifest


def public(name: str, manifest: dict[str, Any], directory: Path) -> dict[str, Any]:
    metrics = (manifest.get("metrics") or {}).get("heldout_vs_teacher") or {}
    return {
        "name": name,
        "provider": manifest.get("provider", "student"),
        "architecture": manifest.get("architecture"),
        "concepts": manifest.get("concepts", []),
        "datasets": [d.get("name") for d in manifest.get("datasets", []) if isinstance(d, dict)],
        "created_at": manifest.get("created_at"),
        "teacher": (manifest.get("teacher") or {}).get("name"),
        "confidence": manifest.get("confidence"),
        "metrics": {
            "ap50": metrics.get("ap50"),
            "ap": metrics.get("ap"),
            "recall_class_agnostic": metrics.get("recall_class_agnostic"),
            "concepts": {
                concept: (value or {}).get("recall")
                for concept, value in (metrics.get("concepts") or {}).items()
            },
            "images": metrics.get("images"),
        },
        "training_frames": ((manifest.get("teacher") or {}).get("pseudo_labels") or {}).get("splits"),
        "licence": manifest.get("licence"),
        "ready": not problems(manifest, directory),
        "path": str(directory),
    }


def listing(dataset: str | None = None) -> list[dict[str, Any]]:
    """Every usable model; those trained on ``dataset`` first, newest first."""
    base = root()
    if not base.is_dir():
        return []
    rows = []
    for directory in sorted(base.iterdir()):
        if not directory.is_dir() or directory.name.startswith(("_", ".")):
            continue
        manifest = _read(directory / MANIFEST)
        if manifest is None or problems(manifest, directory):
            continue
        row = public(directory.name, manifest, directory)
        row["for_this_dataset"] = bool(dataset and dataset in row["datasets"])
        rows.append(row)
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    rows.sort(key=lambda r: not r["for_this_dataset"])
    return rows


def worker_spec(name: str) -> dict[str, Any]:
    """What a worker plan carries about a model (explicit paths only)."""
    directory, manifest = load(name)
    return {
        "name": name,
        "provider": manifest.get("provider", "student"),
        "weights": str(directory / (manifest.get("weights") or WEIGHTS)),
        "architecture": manifest.get("architecture", "rf-detr-seg-small"),
        "concepts": manifest["concepts"],
        "class_names": manifest.get("class_names") or manifest["concepts"],
        "confidence": float(manifest.get("confidence", 0.4)),
        "tracker": {**DEFAULT_TRACKER, **(manifest.get("tracker") or {})},
        "fp16": True,
    }


def import_weights(
    weights: Path,
    name: str,
    concepts: list[str],
    *,
    architecture: str = "rf-detr-seg-small",
    note: str | None = None,
    link: bool = False,
) -> Path:
    """Register a student trained elsewhere (concepts in the model's class
    order). Its provenance is whatever ``note`` says; LEVI did not score it."""
    if architecture not in ARCHITECTURES:
        raise ValueError(f"architecture must be one of {', '.join(ARCHITECTURES)}")
    if not concepts:
        raise ValueError("concepts are required, in the model's class order")
    source = Path(weights).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    directory = folder(name)
    if (directory / MANIFEST).exists():
        raise FileExistsError(f"A model named {name} already exists")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / WEIGHTS
    if link:
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
    else:
        shutil.copyfile(source, target)
    manifest = {
        "schema": SCHEMA,
        "name": name,
        "provider": "student",
        "architecture": architecture,
        "concepts": list(concepts),
        "class_names": list(concepts),
        "confidence": 0.4,
        "tracker": dict(DEFAULT_TRACKER),
        "teacher": {"name": "unknown (imported)"},
        "datasets": [],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "licence": dict(LICENCES),
        "imported_from": str(source),
        "note": note,
        "metrics": {"heldout_vs_teacher": None},
        "weights": WEIGHTS,
    }
    (directory / MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    return directory


def delete(name: str) -> None:
    directory = folder(name)
    if not (directory / MANIFEST).is_file():
        raise KeyError(f"No segmentation model named {name!r}")
    shutil.rmtree(directory)
