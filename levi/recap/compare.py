"""Read-only browsing and comparisons of published RECAP revisions.

A comparison joins frame identities within an episode, checks the source and
viewing clock, and keeps the parameters of both runs. Values in [-1, 0] are
normalised using each checkpoint's return range; a difference alone is not a
measure of model quality. Outcome separation uses the saved, matching labels
of the runs, never a label edited after inference.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import jobs, store

# Stable warning keys are translated by the viewer.
COMPARABLE = (
    ("lookahead", "lookahead_differs"),
    ("gamma", "gamma_differs"),
    ("failure_reward", "failure_reward_differs"),
    ("return_min", "return_range_differs"),
    ("return_max", "return_range_differs"),
    ("threshold", "threshold_differs"),
    ("dataset_type", "dataset_type_differs"),
)


def _brief(ds: jobs.Dataset, record: dict[str, Any], current: str | None):
    checkpoint = record.get("checkpoint") or {}
    manifest = checkpoint.get("manifest") or {}
    return {
        "revision_id": record["revision_id"],
        "model": record.get("model"),
        "version": record.get("version"),
        "layout": record.get("layout"),
        "checkpoint": checkpoint.get("name"),
        "step": manifest.get("step"),
        "provider": record.get("provider"),
        "created_at": record.get("created_at"),
        "episodes": record.get("episodes"),
        "frames": record.get("frames"),
        "threshold": record.get("threshold"),
        "threshold_source": record.get("threshold_source"),
        "positive_quantile": record.get("positive_quantile"),
        "lookahead": record.get("lookahead"),
        "gamma": record.get("gamma"),
        "failure_reward": record.get("failure_reward"),
        "dataset_type": record.get("dataset_type"),
        "positive_fraction": record.get("positive_fraction"),
        "return_min": record.get("return_min"),
        "return_max": record.get("return_max"),
        "value_support": {
            "num_bins": manifest.get("num_bins"),
            "v_min": manifest.get("v_min"),
            "v_max": manifest.get("v_max"),
        },
        "precision": manifest.get("precision"),
        "stale": bool(jobs.stale_reasons(ds, record)),
        "current": record["revision_id"] == current,
        "static_filter": jobs._filter_brief(record),
        "dev_only_base_models": record.get("dev_only_base_models") or [],
    }


def revisions_payload(repo_id: str) -> dict[str, Any]:
    """Published results, newest computation first: one per value model
    (``layout: models``), then any original-layout revisions. Browsing
    never changes current. ``results`` is the same list under its new name."""
    ds = jobs.dataset(repo_id)
    current = store.current_id(ds.name)
    records = [store.revision(ds.name, ref) for ref in store.results(ds.name)]
    rows = [_brief(ds, record, current) for record in records if record]
    rows.sort(key=lambda r: (r.get("created_at") or 0, r["revision_id"]), reverse=True)
    return {"current": current, "revisions": rows, "results": rows}


def _pearson(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 2 or np.ptp(a) == 0 or np.ptp(b) == 0:
        return None
    value = float(np.corrcoef(a, b)[0, 1])
    return value if np.isfinite(value) else None


def _auc(success: np.ndarray, failure: np.ndarray) -> float | None:
    if len(success) == 0 or len(failure) == 0:
        return None
    ordered = np.sort(failure)
    wins = np.searchsorted(ordered, success, side="left")
    upper = np.searchsorted(ordered, success, side="right")
    ties = upper - wins
    return float((wins.sum() + 0.5 * ties.sum()) / (len(success) * len(failure)))


def _metrics(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    return {
        "mean_a": float(a.mean()),
        "mean_b": float(b.mean()),
        "mean_abs_diff": float(np.abs(a - b).mean()),
        "corr": _pearson(a, b),
    }


def _join(table_a, table_b, labels: bool = True):
    """Align common original frame indices, then verify their timestamps.
    ``labels=False`` (a value-only side) joins the values alone."""
    fa = table_a["frame_index"].to_numpy(zero_copy_only=False)
    fb = table_b["frame_index"].to_numpy(zero_copy_only=False)
    if len(np.unique(fa)) != len(fa) or len(np.unique(fb)) != len(fb):
        raise jobs.RecapError(
            409, "A published revision contains duplicate frame indices"
        )
    common, ia, ib = np.intersect1d(fa, fb, return_indices=True)

    def col(table, name, idx):
        return table[name].to_numpy(zero_copy_only=False)[idx]

    if not np.allclose(
        col(table_a, "timestamp", ia), col(table_b, "timestamp", ib), rtol=0, atol=1e-6
    ):
        raise jobs.RecapError(
            409, "Shared frame timestamps differ between these revisions"
        )
    numeric = ("value", "advantage") if labels else ("value",)
    result = {
        key: (col(table_a, key, ia), col(table_b, key, ib))
        for key in (*numeric, "positive")
        if labels or key != "positive"
    }
    for key in numeric:
        result[key] = tuple(np.asarray(v, dtype=np.float64) for v in result[key])
        if any(not np.all(np.isfinite(v)) for v in result[key]):
            raise jobs.RecapError(
                409, "A published revision contains non-finite values"
            )
    if labels:
        result["positive"] = tuple(v.astype(bool) for v in result["positive"])
    return common, result


def _fingerprint(record):
    fingerprint = record.get("fingerprint") or {}
    if fingerprint.get("source_fingerprint"):
        return "source_fingerprint", fingerprint["source_fingerprint"]
    if fingerprint.get("dataset_revision"):
        # Native LeRobot revisions stat metadata only, not parquet/video
        # payloads. Still refuse metadata changes, but never claim full
        # source verification from this weaker identity.
        return "dataset_metadata", fingerprint["dataset_revision"]
    return None


def _return_units(values, record):
    lo, hi = record.get("return_min"), record.get("return_max")
    if lo is None or hi is None or hi <= lo:
        return None
    return (values + 1.0) * (hi - lo) + lo


def compare_payload(
    repo_id: str,
    rid_a: str,
    rid_b: str,
    version_a: str | None = None,
    version_b: str | None = None,
) -> dict[str, Any]:
    """Compare two results on common frames; reject different source data.
    Both versions are fixed once, at the start, and every episode is read
    from them (``version_a``/``version_b`` pin what the reader saw: 409 if
    recomputed since)."""
    ds = jobs.dataset(repo_id)
    rec_a = jobs._published(ds.name, rid_a, version_a)
    rec_b = jobs._published(ds.name, rid_b, version_b)
    if rid_a == rid_b:
        raise jobs.RecapError(400, "Choose two different revisions to compare")
    fp_a, fp_b = _fingerprint(rec_a), _fingerprint(rec_b)
    if fp_a and fp_b and fp_a[0] == fp_b[0] and fp_a[1] != fp_b[1]:
        raise jobs.RecapError(
            409,
            "The dataset changed between these revisions; recompute both on the same source",
        )
    current = store.current_id(ds.name)
    set_a = {int(e) for e in rec_a.get("episode_indices") or []}
    set_b = {int(e) for e in rec_b.get("episode_indices") or []}
    shared = sorted(set_a & set_b)
    rows = []
    labelled = jobs.has_labels(rec_a) and jobs.has_labels(rec_b)
    pooled = {"value": ([], [])}
    if labelled:
        pooled.update(advantage=([], []), positive=([], []))
    mean_a, mean_b, outcomes = [], [], []
    saved_a, saved_b = rec_a.get("outcomes") or {}, rec_b.get("outcomes") or {}
    outcome_changes = False
    for ep in shared:
        table_a = store.read_episode(
            ds.name, ep, rec_a["revision_id"], rec_a["version"]
        )
        table_b = store.read_episode(
            ds.name, ep, rec_b["revision_id"], rec_b["version"]
        )
        if table_a is None or table_b is None:
            raise jobs.RecapError(409, f"A published revision is missing episode {ep}")
        common, joined = _join(table_a, table_b, labelled)
        n = len(common)
        if not n:
            continue
        va, vb = joined["value"]
        for key in pooled:
            pooled[key][0].append(joined[key][0])
            pooled[key][1].append(joined[key][1])
        oa, ob = saved_a.get(str(ep)), saved_b.get(str(ep))
        outcome_changes |= oa != ob
        outcome = oa if oa == ob and oa in ("success", "failure") else None
        mean_a.append(float(va.mean()))
        mean_b.append(float(vb.mean()))
        outcomes.append(outcome)
        pa, pb = joined["positive"] if labelled else (None, None)
        rows.append(
            {
                "episode": ep,
                "outcome": outcome,
                "frames": n,
                "label_agreement": float((pa == pb).mean()) if labelled else None,
                "positive_fraction_a": float(pa.mean()) if labelled else None,
                "positive_fraction_b": float(pb.mean()) if labelled else None,
                "mean_value_a": float(va.mean()),
                "mean_value_b": float(vb.mean()),
                "value_mean_abs_diff": float(np.abs(va - vb).mean()),
                "value_corr": _pearson(va, vb),
            }
        )
    frames = sum(r["frames"] for r in rows)
    result: dict[str, Any] = {
        "dataset": ds.name,
        "a": _brief(ds, rec_a, current),
        "b": _brief(ds, rec_b, current),
        "episodes": {
            "shared": len(shared),
            "only_a": len(set_a - set_b),
            "only_b": len(set_b - set_a),
        },
        "frames": {
            "shared": frames,
            "only_a": max(0, int(rec_a.get("frames") or 0) - frames),
            "only_b": max(0, int(rec_b.get("frames") or 0) - frames),
        },
        "notes": [],
        "per_episode": rows,
    }
    notes = result["notes"]
    for key, warning in COMPARABLE:
        if rec_a.get(key) != rec_b.get(key) and warning not in notes:
            notes.append(warning)
    filters = [
        {
            k: v
            for k, v in (r.get("static_filter") or {}).items()
            if k
            not in ("frames", "kept_frames", "skipped", "episodes", "skipped_episodes")
        }
        for r in (rec_a, rec_b)
    ]
    if filters[0] != filters[1]:
        notes.append("static_filter_differs")
    if result["a"]["value_support"] != result["b"]["value_support"]:
        notes.append("value_support_differs")
    if result["a"]["precision"] != result["b"]["precision"]:
        notes.append("precision_differs")
    if outcome_changes:
        notes.append("outcomes_differ")
    if result["a"]["stale"] or result["b"]["stale"]:
        notes.append("stale_results")
    if any(not fp or fp[0] != "source_fingerprint" for fp in (fp_a, fp_b)):
        notes.append("fingerprint_unavailable")
    if result["frames"]["only_a"] or result["frames"]["only_b"]:
        notes.append("coverage_differs")
    if not rows:
        result.update(
            {
                "labels": None,
                "value": None,
                "advantage": None,
                "value_return_units": None,
                "outcome_separation": None,
            }
        )
        return result
    value_a, value_b = (np.concatenate(p) for p in pooled["value"])
    result["value"] = _metrics(value_a, value_b)
    if labelled:
        _label_metrics(result, pooled)
    else:
        # A value-only side has no advantages, labels or return range.
        notes.append("labels_unavailable")
        result.update(labels=None, advantage=None)
    raw_a, raw_b = _return_units(value_a, rec_a), _return_units(value_b, rec_b)
    result["value_return_units"] = (
        _metrics(raw_a, raw_b) if raw_a is not None and raw_b is not None else None
    )
    marks = np.array([o or "" for o in outcomes])
    ma, mb = np.array(mean_a), np.array(mean_b)
    ok, bad = marks == "success", marks == "failure"
    auc_a, auc_b = _auc(ma[ok], ma[bad]), _auc(mb[ok], mb[bad])
    result["outcome_separation"] = (
        None
        if auc_a is None or auc_b is None
        else {
            "success_episodes": int(ok.sum()),
            "failure_episodes": int(bad.sum()),
            "auc_a": auc_a,
            "auc_b": auc_b,
        }
    )
    return result


def _label_metrics(result: dict[str, Any], pooled) -> None:
    adv_a, adv_b = (np.concatenate(p) for p in pooled["advantage"])
    pos_a, pos_b = (np.concatenate(p) for p in pooled["positive"])
    result["labels"] = {
        "agreement": float((pos_a == pos_b).mean()),
        "positive_a_only": int((pos_a & ~pos_b).sum()),
        "positive_b_only": int((pos_b & ~pos_a).sum()),
        "both_positive": int((pos_a & pos_b).sum()),
        "both_negative": int((~pos_a & ~pos_b).sum()),
        "positive_fraction_a": float(pos_a.mean()),
        "positive_fraction_b": float(pos_b.mean()),
    }
    result["advantage"] = _metrics(adv_a, adv_b)
