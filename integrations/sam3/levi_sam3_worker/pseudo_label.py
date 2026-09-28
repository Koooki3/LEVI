"""SAM3 as a teacher: text-prompted pseudo-labels on sampled frames -> COCO.

Used by LEVI's "distil a fast student" pipeline. Every ``stride``-th frame
of each planned (episode, camera) item is segmented once per concept with
the SAM3 *image* model (one shared backbone pass per frame). Candidates of
all concepts are merged by mask NMS (the higher score wins an overlap above
``nms_iou``), so one physical object keeps one label. Items carry a split
(``train`` / ``valid`` / ``test``); each split becomes a COCO folder with
``_annotations.coco.json`` next to its JPEG frames, the layout RF-DETR
trains from. ``test`` holds the held-out episodes the student is scored on.

Nothing heavy is imported at module import time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .worker import _atomic_json, _ensure_checkpoint

SPLITS = ("train", "valid", "test")


def _polygons(mask: Any, cv2: Any, np: Any) -> list[list[float]]:
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(contour) < 4:
            continue
        points = cv2.approxPolyDP(contour, 1.0, True).reshape(-1, 2)
        if len(points) >= 3:
            out.append(points.reshape(-1).astype(float).tolist())
    return out


def _iou(a: Any, b: Any, np: Any) -> float:
    inter = np.logical_and(a, b).sum()
    if inter == 0:
        return 0.0
    return float(inter / np.logical_or(a, b).sum())


def _progress(path: Path | None, **value: Any) -> None:
    if path is None:
        return
    try:
        _atomic_json(path, {**value, "updated_at": time.time()})
    except OSError:
        pass


def _frames(item: dict[str, Any], stride: int, cv2: Any):
    """(local index, RGB) for every ``stride``-th frame of one episode."""
    cap = cv2.VideoCapture(str(item["video"]))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video {item['video']}")
    start = int(item.get("start_frame", 0))
    length = int(item.get("length") or 0)
    if start:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    index = 0
    try:
        while not length or index < length:
            if index % stride:
                if not cap.grab():
                    return
            else:
                ok, frame = cap.read()
                if not ok:
                    return
                yield index, cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            index += 1
    finally:
        cap.release()


def run(plan_path: Path, output_path: Path, progress_path: Path | None = None) -> None:
    plan = json.loads(plan_path.read_text())
    concepts: list[str] = list(plan["concepts"])
    out = Path(plan["output_dir"])
    threshold = float(plan.get("threshold", 0.5))
    nms_iou = float(plan.get("nms_iou", 0.6))
    min_area = int(plan.get("min_area", 30))
    default_stride = max(1, int(plan.get("stride", 3)))
    items: list[dict[str, Any]] = plan["items"]
    total = sum(
        (int(i.get("length") or 0) + max(1, int(i.get("stride") or default_stride)) - 1)
        // max(1, int(i.get("stride") or default_stride))
        for i in items
    )
    _progress(progress_path, stage="teacher_loading", done=0, total=total)

    import cv2
    import numpy as np
    import torch
    from sam3.model.sam3_image_processor import Sam3Processor
    from sam3.model_builder import build_sam3_image_model

    if not torch.cuda.is_available():
        raise RuntimeError("The SAM3 teacher needs a CUDA device")
    checkpoint, _ = _ensure_checkpoint()
    model = build_sam3_image_model(checkpoint_path=str(checkpoint), load_from_HF=False)
    processor = Sam3Processor(model, confidence_threshold=threshold)
    coco = {
        split: {
            "images": [],
            "annotations": [],
            "categories": [{"id": i, "name": c, "supercategory": "object"} for i, c in enumerate(concepts)],
        }
        for split in SPLITS
    }
    counts = {split: {"images": 0, "annotations": 0} for split in SPLITS}
    per_concept = {c: 0 for c in concepts}
    item_errors = []
    image_id = 0
    ann_id = 0
    done = 0
    started = time.time()
    for item in items:
        split = item.get("split", "train")
        stride = max(1, int(item.get("stride") or default_stride))
        folder = out / split
        folder.mkdir(parents=True, exist_ok=True)
        tag = f"{item.get('dataset', 'dataset')}-{int(item['episode_index']):06d}-{str(item['camera_key']).split('.')[-1]}"
        try:
            for index, rgb in _frames(item, stride, cv2):
                with torch.autocast("cuda", dtype=torch.bfloat16):
                    state = processor.set_image(torch.from_numpy(rgb).permute(2, 0, 1))
                    candidates = []
                    for ci, concept in enumerate(concepts):
                        processor.reset_all_prompts(state)
                        state = processor.set_text_prompt(concept, state)
                        masks = state["masks"][:, 0].cpu().numpy()
                        scores = state["scores"].float().cpu().numpy()
                        for mask, score in zip(masks, scores, strict=False):
                            if mask.sum() > min_area:
                                candidates.append((float(score), ci, mask.astype(bool)))
                candidates.sort(key=lambda c: -c[0])
                kept: list[tuple[float, int, Any]] = []
                for cand in candidates:
                    if all(_iou(cand[2], k[2], np) < nms_iou for k in kept):
                        kept.append(cand)
                image_id += 1
                name = f"{tag}-{index:05d}.jpg"
                cv2.imwrite(str(folder / name), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
                h, w = rgb.shape[:2]
                coco[split]["images"].append(
                    {
                        "id": image_id,
                        "file_name": name,
                        "height": h,
                        "width": w,
                        "episode_index": int(item["episode_index"]),
                        "camera_key": item["camera_key"],
                        "frame_index": index,
                        "dataset": item.get("dataset"),
                    }
                )
                counts[split]["images"] += 1
                for score, ci, mask in kept:
                    polygons = _polygons(mask, cv2, np)
                    if not polygons:
                        continue
                    ys, xs = np.where(mask)
                    x0, y0, x1, y1 = float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)
                    ann_id += 1
                    coco[split]["annotations"].append(
                        {
                            "id": ann_id,
                            "image_id": image_id,
                            "category_id": ci,
                            "iscrowd": 0,
                            "segmentation": polygons,
                            "bbox": [x0, y0, x1 - x0, y1 - y0],
                            "area": float(mask.sum()),
                            "score": round(score, 4),
                        }
                    )
                    counts[split]["annotations"] += 1
                    per_concept[concepts[ci]] += 1
                done += 1
                if done % 10 == 0:
                    _progress(progress_path, stage="teacher", done=done, total=total,
                              current_episode=item["episode_index"], current_camera=item["camera_key"])
        except Exception as exc:  # noqa: BLE001 - one bad item must not sink the set
            item_errors.append({"episode_index": item.get("episode_index"), "camera_key": item.get("camera_key"), "error": str(exc)})
    for split in SPLITS:
        if coco[split]["images"]:
            (out / split).mkdir(parents=True, exist_ok=True)
            (out / split / "_annotations.coco.json").write_text(json.dumps(coco[split]))
    seconds = time.time() - started
    _progress(progress_path, stage="teacher", done=done, total=total)
    status = "succeeded" if counts["train"]["images"] and counts["valid"]["images"] else "failed"
    _atomic_json(
        output_path,
        {
            "status": status,
            "error": None if status == "succeeded" else "the teacher produced no train or valid images",
            "splits": counts,
            "per_concept": per_concept,
            "item_errors": item_errors,
            "seconds": round(seconds, 2),
            "images_per_second": round(done / seconds, 3) if seconds else None,
            "teacher": {
                "provider": "sam3-image",
                "threshold": threshold,
                "nms_iou": nms_iou,
                "checkpoint": str(checkpoint),
            },
        },
    )
