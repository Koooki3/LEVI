"""Fine-tune RF-DETR-Seg on the teacher's COCO folders (train/valid)."""

from __future__ import annotations

import csv
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .common import Progress
from .student import ARCHITECTURES


def _epochs_done(metrics: Path) -> int:
    try:
        with metrics.open() as handle:
            epochs = [int(float(row["epoch"])) for row in csv.DictReader(handle) if row.get("epoch") not in (None, "")]
        return max(epochs) + 1 if epochs else 0
    except (OSError, ValueError, KeyError):
        return 0


def train(
    dataset_dir: Path,
    work_dir: Path,
    *,
    architecture: str,
    epochs: int,
    batch_size: int,
    grad_accum: int,
    progress: Progress,
    num_workers: int = 8,
) -> dict[str, Any]:
    import rfdetr

    model = getattr(rfdetr, ARCHITECTURES[architecture])()
    work_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()

    def watch() -> None:
        metrics = work_dir / "metrics.csv"
        while not stop.wait(10):
            progress.set("training", _epochs_done(metrics), epochs)

    thread = threading.Thread(target=watch, daemon=True, name="train-progress")
    thread.start()
    started = time.time()
    try:
        model.train(
            dataset_dir=str(dataset_dir),
            epochs=epochs,
            batch_size=batch_size,
            grad_accum_steps=grad_accum,
            output_dir=str(work_dir),
            num_workers=num_workers,
            tensorboard=False,
            progress_bar=None,
        )
    finally:
        stop.set()
        thread.join(timeout=2)
    seconds = time.time() - started
    for name in ("checkpoint_best_total.pth", "checkpoint_best_ema.pth", "checkpoint_best_regular.pth", "last_ema.pth"):
        best = work_dir / name
        if best.is_file():
            break
    else:
        candidates = sorted(work_dir.glob("*.pth"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise RuntimeError("training finished without a checkpoint")
        best = candidates[-1]
    return {
        "seconds": round(seconds, 1),
        "epochs": epochs,
        "epochs_completed": _epochs_done(work_dir / "metrics.csv"),
        "batch_size": batch_size,
        "grad_accum_steps": grad_accum,
        "checkpoint": str(best),
        "picked": best.name,
    }


def install(checkpoint: Path, target: Path) -> None:
    """Copy the chosen checkpoint to the model folder (atomic rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    shutil.copyfile(checkpoint, temp)
    temp.replace(target)
