"""Distil a fast student for one dataset and concept set.

Stages (one GPU lock for the whole chain):

1. **teacher** -- the SAM3 worker (its own environment) pseudo-labels every
   ``stride``-th frame of the chosen episodes with the text concepts and
   writes COCO train / valid / test folders;
2. **training** -- RF-DETR-Seg is fine-tuned from its COCO weights on
   train/valid;
3. **evaluating** -- the student is scored against the teacher on the
   held-out (test) episodes;
4. **registering** -- weights and ``manifest.json`` are written to the model
   folder LEVI chose; LEVI lists the model once the job succeeded.

The ``fake`` provider skips SAM3 and Torch and writes a manifest with a
placeholder weights file, for CPU tests of the job plumbing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .common import Heartbeat, Progress, atomic_json, gpu_lock

STAGES = ("teacher", "training", "evaluating", "registering")


def _teacher(plan: dict[str, Any], work: Path, progress: Progress) -> dict[str, Any]:
    teacher = plan["teacher"]
    teacher_plan = work / "teacher-plan.json"
    teacher_out = work / "teacher-result.json"
    teacher_progress = work / "teacher-progress.json"
    atomic_json(
        teacher_plan,
        {
            "concepts": plan["concepts"],
            "items": plan["items"],
            "stride": teacher.get("stride", 3),
            "threshold": teacher.get("threshold", 0.5),
            "nms_iou": teacher.get("nms_iou", 0.6),
            "min_area": teacher.get("min_area", 30),
            "output_dir": str(work / "coco"),
        },
    )
    env = {
        **os.environ,
        **{k: str(v) for k, v in (teacher.get("env") or {}).items()},
        "LEVI_SAM3_ENABLED": "1",
        # This driver already holds the GPU lock for the whole chain.
        "LEVI_GPU_LOCK_FILE": "",
        "PYTHONUNBUFFERED": "1",
    }
    log = (work / "teacher.log").open("ab")
    try:
        process = subprocess.Popen(
            [
                teacher["python"],
                "-m",
                "levi_sam3_worker.cli",
                "--pseudo-label",
                "--plan",
                str(teacher_plan),
                "--output",
                str(teacher_out),
                "--progress",
                str(teacher_progress),
            ],
            cwd=teacher["project"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        while process.poll() is None:
            time.sleep(2)
            try:
                value = json.loads(teacher_progress.read_text())
                progress.set("teacher", int(value.get("done", 0)), int(value.get("total", 0)))
            except (OSError, ValueError):
                progress.touch()
    finally:
        log.close()
    if not teacher_out.is_file():
        tail = (work / "teacher.log").read_bytes()[-3000:].decode("utf-8", "replace")
        raise RuntimeError(f"the SAM3 teacher exited without a result (code {process.returncode}):\n{tail}")
    result = json.loads(teacher_out.read_text())
    if result.get("status") != "succeeded":
        raise RuntimeError(f"the SAM3 teacher failed: {result.get('error')}")
    return result


def _fake(plan: dict[str, Any], progress: Progress) -> dict[str, Any]:
    items = plan["items"]
    progress.set("teacher", len(items), len(items))
    per_concept = {c: 1 for c in plan["concepts"]}
    return {
        "teacher": {
            "splits": {s: {"images": sum(1 for i in items if i.get("split") == s), "annotations": 0} for s in ("train", "valid", "test")},
            "per_concept": per_concept,
            "seconds": 0.0,
        },
        "training": {"seconds": 0.0, "epochs": plan["model"].get("epochs", 1), "picked": "fake"},
        "metrics": {
            "reference": "teacher pseudo-labels on held-out episodes",
            "ap50": None,
            "ap": None,
            "concepts": {c: {"recall": None} for c in plan["concepts"]},
            "fake": True,
        },
    }


def run(plan_path: Path, output: Path, progress_path: Path | None) -> int:
    plan = json.loads(plan_path.read_text())
    progress = Progress(progress_path)
    work = Path(plan["work_dir"])
    work.mkdir(parents=True, exist_ok=True)
    model = plan["model"]
    model_dir = Path(model["dir"])
    started = time.time()
    stages: dict[str, Any] = {}
    try:
        progress.set("queued", 0, 0)
        with gpu_lock(progress):
            if plan.get("provider") == "fake":
                fake = _fake(plan, progress)
                teacher, training, metrics = fake["teacher"], fake["training"], fake["metrics"]
                model_dir.mkdir(parents=True, exist_ok=True)
                (model_dir / "weights.pth").write_bytes(b"fake student weights")
            else:
                t0 = time.time()
                teacher = _teacher(plan, work, progress)
                stages["teacher"] = round(time.time() - t0, 1)
                from . import evaluate as evaluation
                from . import train as training_mod
                from .student import Student

                progress.set("training", 0, int(model.get("epochs", 20)))
                training = training_mod.train(
                    work / "coco",
                    work / "train",
                    architecture=model.get("architecture", "rf-detr-seg-small"),
                    epochs=int(model.get("epochs", 20)),
                    batch_size=int(model.get("batch_size", 8)),
                    grad_accum=int(model.get("grad_accum", 2)),
                    progress=progress,
                    num_workers=int(model.get("num_workers", 8)),
                )
                stages["training"] = training["seconds"]
                progress.set("evaluating", 0, 1)
                weights = model_dir / "weights.pth"
                training_mod.install(Path(training["checkpoint"]), weights)
                metrics = None
                if (work / "coco" / "test" / "_annotations.coco.json").is_file():
                    t0 = time.time()
                    with Heartbeat(progress):
                        student = Student(
                            weights,
                            architecture=model.get("architecture", "rf-detr-seg-small"),
                            concepts=plan["concepts"],
                            confidence=float(model.get("confidence", 0.4)),
                            fp16=True,
                            batch=8,
                        )
                        metrics = evaluation.evaluate(
                            student, work / "coco" / "test", confidence=float(model.get("confidence", 0.4))
                        )
                        metrics["precision"] = student.precision
                    stages["evaluating"] = round(time.time() - t0, 1)
            progress.set("registering", 0, 1)
            manifest = {
                **plan["manifest"],
                "teacher": {
                    **plan["manifest"].get("teacher", {}),
                    "pseudo_labels": {
                        "splits": teacher.get("splits"),
                        "per_concept": teacher.get("per_concept"),
                        "seconds": teacher.get("seconds"),
                        "images_per_second": teacher.get("images_per_second"),
                    },
                },
                "training": {k: v for k, v in training.items() if k != "checkpoint"},
                "metrics": {"heldout_vs_teacher": metrics},
                "weights": "weights.pth",
                "seconds": round(time.time() - started, 1),
                "stage_seconds": stages,
            }
            atomic_json(model_dir / "manifest.json", manifest)
            progress.set("registering", 1, 1)
        if not plan.get("keep_intermediates"):
            shutil.rmtree(work, ignore_errors=True)
        atomic_json(output, {"status": "succeeded", "model_dir": str(model_dir), "manifest": manifest})
        return 0
    except Exception as exc:  # noqa: BLE001 - reported to LEVI as a failed job
        # A half-written model folder must never be listed as a model.
        shutil.rmtree(model_dir, ignore_errors=True)
        atomic_json(output, {"status": "failed", "error": str(exc), "stage_seconds": stages})
        return 1
