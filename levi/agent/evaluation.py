"""Reproducible engineering metrics; these do not replace real-video acceptance."""

import math


def temporal(reference, predicted, tolerance=0.2):
    """Greedy one-to-one matching, same identity/attempt/outcome/episode/layer."""
    used = set()
    errors = []
    matched = 0
    for expected in reference:
        candidates = []
        for i, row in enumerate(predicted):
            if i in used or any(
                row.get(k) != expected.get(k)
                for k in (
                    "episode_index",
                    "kind",
                    "subtask_id",
                    "attempt",
                    "outcome",
                    "layer",
                )
            ):
                continue
            error = abs(row["start"] - expected["start"])
            if expected.get("end") is not None:
                if row.get("end") is None:
                    continue
                error = max(error, abs(row["end"] - expected["end"]))
            candidates.append((error, i))
        if candidates:
            error, i = min(candidates)
            if error <= tolerance:
                used.add(i)
                errors.append(error)
                matched += 1
    precision = matched / len(predicted) if predicted else float(not reference)
    recall = matched / len(reference) if reference else float(not predicted)
    return {
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall)
        if precision + recall
        else 0,
        "matched": matched,
        "reference_count": len(reference),
        "predicted_count": len(predicted),
        "max_boundary_error": max(errors) if errors else None,
        "tolerance_seconds": tolerance,
    }


def mask_iou(a, b):
    import numpy as np

    from levi.annotations.rle import decode_rle

    left, right = np.asarray(decode_rle(a)), np.asarray(decode_rle(b))
    if left.shape != right.shape:
        raise ValueError("Mask dimensions differ")
    union = np.logical_or(left, right).sum()
    return float(np.logical_and(left, right).sum() / union) if union else 1.0


def efficiency(baseline, optimized):
    """Reject comparisons with different coverage, validation or quality scope."""
    required = (
        "dataset_digest",
        "policy_digest",
        "evidence_digest",
        "validation_digest",
        "output_digest",
    )
    if any(baseline[k] != optimized[k] for k in required):
        raise ValueError(
            "Comparison must use identical data, observation coverage, validation and output"
        )
    result = {}
    for key in (
        "tokens",
        "model_calls",
        "tool_calls",
        "elapsed_seconds",
        "retries",
        "cache_hits",
        "human_review_seconds",
    ):
        a, b = baseline.get(key), optimized.get(key)
        if a is not None and b is not None and math.isfinite(a) and math.isfinite(b):
            result[key] = {"baseline": a, "harness": b, "saved": a - b}
        else:
            result[key] = {"status": "not measured"}
    return result
