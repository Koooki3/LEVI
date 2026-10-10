"""How well candidate boundaries and segments match a reference.

Points (boundaries, candidate times) and segments, per episode, in seconds:

- ``boundary_scores``: for each tolerance (default 0.1 s and 0.2 s), the
  share of reference boundaries with a candidate within the tolerance
  (Boundary Recall@tolerance; one candidate counts for one boundary), the
  share of candidates that match one (precision), and the candidates that
  match none per minute of episode (false candidates per minute). With them,
  how far each reference boundary is from its nearest candidate: mean (MAE)
  and 90th percentile (P90), on every boundary, matched or not, so a missed
  boundary counts as the distance to whatever candidate is closest.
- ``segment_f1``: Segment F1 at several IoU thresholds, with the matching
  rule of ``levi.harness.grading.episode`` (same subtask, the reference's
  best-IoU candidate at or above the threshold, each candidate used once);
  at IoU 0.3 it is that function's ``segment_f1``.

Pure functions, no I/O. ``levi.agent.evaluation.temporal`` (one tolerance,
identity-matched annotation rows) and ``levi.harness.grading`` (one IoU)
stay as they are; this module adds the multi-threshold views an event
reader is judged by.
"""

import numpy as np

TOLERANCES = (0.1, 0.2)
IOUS = (0.3, 0.5, 0.7)
# Float slack on a tolerance, in seconds.
EPS = 1e-9


def boundaries(segments):
    """The inner boundaries of an episode's segments (``start``/``end`` in
    seconds): every start and end except the episode's first start and last
    end, merged where two coincide within a millisecond."""
    points = sorted(
        {round(float(s[k]), 3) for s in segments for k in ("start", "end")}
        if segments
        else set()
    )
    if len(points) <= 2:
        return []
    return points[1:-1]


def _finite_sorted(values):
    values = np.asarray(list(values), dtype=float)
    return np.sort(values[np.isfinite(values)])


def match(reference, candidates, tolerance):
    """``[(reference_time, candidate_time)]``: the largest one-to-one matching
    with ``|r - c| <= tolerance`` (plus ``EPS``, so a candidate exactly one
    tolerance away counts whatever the float rounding). On a line the
    earliest-first sweep is maximal."""
    ref, cand = _finite_sorted(reference), _finite_sorted(candidates)
    limit = tolerance + EPS
    pairs, j = [], 0
    for r in ref:
        while j < len(cand) and r - cand[j] > limit:
            j += 1
        if j < len(cand) and abs(cand[j] - r) <= limit:
            pairs.append((float(r), float(cand[j])))
            j += 1
    return pairs


def _p90(values):
    return float(np.percentile(values, 90)) if len(values) else None


def boundary_scores(reference, candidates, durations, tolerances=TOLERANCES):
    """Scores over episodes. ``reference`` and ``candidates`` map an episode
    to its boundary times; ``durations`` to its length in seconds (the
    denominator of false candidates per minute). Episodes missing from
    ``candidates`` have none; candidates of an episode without a reference
    entry are ignored (they cannot be judged)."""
    episodes = sorted(reference)
    minutes = sum(max(0.0, float(durations[e])) for e in episodes) / 60
    ref_count = sum(len(_finite_sorted(reference[e])) for e in episodes)
    cand_count = sum(len(_finite_sorted(candidates.get(e, ()))) for e in episodes)
    nearest, uncovered = [], 0
    for e in episodes:
        cand = _finite_sorted(candidates.get(e, ()))
        for r in _finite_sorted(reference[e]):
            if len(cand):
                nearest.append(float(np.min(np.abs(cand - r))))
            else:
                uncovered += 1
    out = {
        "episodes": len(episodes),
        "minutes": round(minutes, 4),
        "reference_boundaries": ref_count,
        "candidates": cand_count,
        "candidates_per_minute": round(cand_count / minutes, 3) if minutes else None,
        # Distance from each reference boundary to its nearest candidate, in
        # episodes that have any candidate; ``no_candidate`` counts the rest.
        "nearest_mae": round(float(np.mean(nearest)), 4) if nearest else None,
        "nearest_p90": round(_p90(nearest), 4) if nearest else None,
        "no_candidate": uncovered,
        "at": {},
    }
    for tol in tolerances:
        matched, errors = 0, []
        for e in episodes:
            pairs = match(reference[e], candidates.get(e, ()), tol)
            matched += len(pairs)
            errors += [abs(r - c) for r, c in pairs]
        recall = matched / ref_count if ref_count else None
        precision = matched / cand_count if cand_count else None
        if precision is None or recall is None:
            f1 = None
        else:
            f1 = 2 * precision * recall / (precision + recall) if matched else 0.0
        out["at"][f"{tol:g}"] = {
            "tolerance_seconds": tol,
            "recall": None if recall is None else round(recall, 4),
            "precision": None if precision is None else round(precision, 4),
            "f1": None if f1 is None else round(f1, 4),
            "matched": matched,
            "false_candidates": cand_count - matched,
            "false_per_minute": round((cand_count - matched) / minutes, 3)
            if minutes
            else None,
            "matched_mae": round(float(np.mean(errors)), 4) if errors else None,
            "matched_p90": round(_p90(errors), 4) if errors else None,
        }
    return out


def _iou(a, b):
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return inter / union if union > 0 else 0.0


def _episode_pairs(ref, cand, threshold):
    ref = [s for s in ref if s.get("end") is not None]
    cand = [s for s in cand if s.get("end") is not None]
    pairs, used = 0, set()
    for r in ref:
        best, score = None, threshold
        for j, c in enumerate(cand):
            if j in used or c.get("subtask") != r.get("subtask"):
                continue
            value = _iou(r, c)
            if value >= score:
                best, score = j, value
        if best is not None:
            used.add(best)
            pairs += 1
    return pairs, len(ref), len(cand)


def _f1(pairs, n_ref, n_cand):
    precision = pairs / n_cand if n_cand else (1.0 if not n_ref else 0.0)
    recall = pairs / n_ref if n_ref else 1.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def segment_f1(reference, candidates, ious=IOUS):
    """Segment F1 per IoU threshold. ``reference``/``candidates`` map an
    episode to segments (``start``, ``end``, ``subtask``). ``mean`` is the
    mean of per-episode F1 (as ``grading.grade`` averages), ``pooled`` the F1
    of all matches over all segments."""
    out = {}
    for threshold in ious:
        per, pairs_all, ref_all, cand_all = [], 0, 0, 0
        for e in sorted(reference):
            pairs, n_ref, n_cand = _episode_pairs(
                reference[e], candidates.get(e, []), threshold
            )
            per.append(_f1(pairs, n_ref, n_cand))
            pairs_all, ref_all, cand_all = (
                pairs_all + pairs,
                ref_all + n_ref,
                cand_all + n_cand,
            )
        out[f"{threshold:g}"] = {
            "iou": threshold,
            "mean": round(sum(per) / len(per), 4) if per else None,
            "pooled": round(_f1(pairs_all, ref_all, cand_all), 4)
            if ref_all or cand_all
            else None,
            "matched": pairs_all,
            "reference_segments": ref_all,
            "candidate_segments": cand_all,
        }
    return out
