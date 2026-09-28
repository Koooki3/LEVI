"""Score a student against the teacher on held-out episodes.

The reference is the teacher's pseudo-label of the held-out frames (the
``test`` split), so these numbers say how close the student comes to SAM3,
not how right SAM3 is. Reported: COCO segm AP50 and AP50:95, per-concept
recall and precision at mask IoU >= 0.5 (student score >= its confidence
threshold), matched mean IoU, and offline throughput.
"""

from __future__ import annotations

import contextlib
import io
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from .common import mask_iou


def _decode_gt(ann: dict[str, Any], h: int, w: int) -> np.ndarray:
    from pycocotools import mask as mask_utils

    rles = mask_utils.frPyObjects(ann["segmentation"], h, w)
    return mask_utils.decode(mask_utils.merge(rles)).astype(bool)


def evaluate(student: Any, test_dir: Path, *, confidence: float, batch: int = 8) -> dict[str, Any]:
    import cv2
    from pycocotools import mask as mask_utils
    from pycocotools.coco import COCO
    from pycocotools.cocoeval import COCOeval

    gt_path = test_dir / "_annotations.coco.json"
    data = json.loads(gt_path.read_text())
    names = {c["id"]: c["name"] for c in data["categories"]}
    by_name = {v: k for k, v in names.items()}
    images = data["images"]
    gts: dict[int, list[dict[str, Any]]] = {}
    for ann in data["annotations"]:
        gts.setdefault(ann["image_id"], []).append(ann)
    results = []
    per = {name: {"gt": 0, "tp": 0, "pred": 0, "ious": []} for name in names.values()}
    agnostic = {"gt": 0, "tp": 0}
    seconds = 0.0
    for start in range(0, len(images), batch):
        chunk = images[start : start + batch]
        rgbs = [cv2.cvtColor(cv2.imread(str(test_dir / im["file_name"])), cv2.COLOR_BGR2RGB) for im in chunk]
        t0 = time.perf_counter()
        detections = student.predict(rgbs)
        seconds += time.perf_counter() - t0
        for im, det in zip(chunk, detections, strict=True):
            h, w = im["height"], im["width"]
            preds = []
            for j in range(len(det)):
                name = student.label(int(det.class_id[j]))
                cat = by_name.get(name)
                mask = np.asarray(det.mask[j], dtype=bool) if det.mask is not None else None
                if mask is None or cat is None or not mask.any():
                    continue
                score = float(det.confidence[j])
                rle = mask_utils.encode(np.asfortranarray(mask.astype(np.uint8)))
                rle["counts"] = rle["counts"].decode()
                results.append({"image_id": im["id"], "category_id": cat, "segmentation": rle, "score": score})
                if score >= confidence:
                    preds.append((score, name, mask))
            refs = [(names[a["category_id"]], _decode_gt(a, h, w)) for a in gts.get(im["id"], [])]
            for name, _ in refs:
                per[name]["gt"] += 1
            agnostic["gt"] += len(refs)
            for _, name, _ in preds:
                per[name]["pred"] += 1
            # Greedy by score: same-concept matches, then class-agnostic.
            used = set()
            for score, name, mask in sorted(preds, key=lambda p: -p[0]):
                best, best_iou = None, 0.5
                for k, (ref_name, ref) in enumerate(refs):
                    if k in used or ref_name != name:
                        continue
                    iou = mask_iou(mask, ref)
                    if iou >= best_iou:
                        best, best_iou = k, iou
                if best is not None:
                    used.add(best)
                    per[name]["tp"] += 1
                    per[name]["ious"].append(best_iou)
            used_any = set()
            for score, name, mask in sorted(preds, key=lambda p: -p[0]):
                for k, (_, ref) in enumerate(refs):
                    if k not in used_any and mask_iou(mask, ref) >= 0.5:
                        used_any.add(k)
                        agnostic["tp"] += 1
                        break
    ap50 = ap = None
    if results and data["annotations"]:
        with contextlib.redirect_stdout(io.StringIO()):
            coco_gt = COCO(str(gt_path))
            coco_dt = coco_gt.loadRes(results)
            ev = COCOeval(coco_gt, coco_dt, "segm")
            ev.evaluate()
            ev.accumulate()
            ev.summarize()
        ap, ap50 = float(ev.stats[0]), float(ev.stats[1])
    concepts = {}
    for name, v in per.items():
        concepts[name] = {
            "reference_instances": v["gt"],
            "recall": round(v["tp"] / v["gt"], 4) if v["gt"] else None,
            "precision": round(v["tp"] / v["pred"], 4) if v["pred"] else None,
            "matched_miou": round(float(np.mean(v["ious"])), 4) if v["ious"] else None,
        }
    return {
        "reference": "teacher pseudo-labels on held-out episodes",
        "images": len(images),
        "reference_instances": agnostic["gt"],
        "ap50": round(ap50, 4) if ap50 is not None else None,
        "ap": round(ap, 4) if ap is not None else None,
        "recall_class_agnostic": round(agnostic["tp"] / agnostic["gt"], 4) if agnostic["gt"] else None,
        "confidence": confidence,
        "concepts": concepts,
        "offline_fps": round(len(images) / seconds, 1) if seconds else None,
    }
