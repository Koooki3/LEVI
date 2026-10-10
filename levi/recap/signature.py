"""Which settings decide a RECAP result's numbers.

Two computations with the same signature give the same values, advantages
and labels for an episode, so a subset can be merged into a stored result
only when the signatures match. The signature is a normalised digest of the
*effective* configuration: the checkpoint manifest, the run's parameters,
the worker's software versions, the base models, the frame selection
(static filter) and the dataset's fingerprint.

Every field is classified here, explicitly, as affecting the numbers or not
(``tests/test_recap_signature.py`` fails while a manifest, result, static
filter or worker field is missing from these sets). At run time a field
nobody classified counts as affecting: a merge is refused rather than risked.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# checkpoints.Manifest (by alias). Thresholds, return range and lookahead are
# also compared through the run's effective values below; keeping the
# manifest's own copies is conservative (`levi recap set` changes them).
MANIFEST_AFFECTING = frozenset(
    {
        "provider",
        "weights",
        "sha256",
        "critic_expert_variant",
        "num_bins",
        "v_min",
        "v_max",
        "max_token_len",
        "precision",
        "views",
        "env_type",
        "model_type",
        "action_dim",
        "action_horizon",
        "static_filter",
        "return_min",
        "return_max",
        "gamma",
        "failure_reward",
        "lookahead",
        "positive_quantile",
        "unified_threshold",
        "base_models",
    }
)
MANIFEST_IGNORED = frozenset(
    {
        "schema",  # format tag
        "name",  # the result's key itself
        "size_bytes",  # implied by sha256
        "step",  # a label of the weights sha256 already identifies
        "source",
        "imported_at",
        "notes",
        "variant_source",  # how critic_expert_variant was found, not its value
        "provenance",  # free text about where numbers came from
        "inspection",  # what the import read from the weights
    }
)

# Keys of a published record (result.json / revision.json and the reader's
# annotations). "checkpoint", "static_filter" and "worker" are compared
# field by field (below).
RESULT_AFFECTING = frozenset(
    {
        "provider",
        "checkpoint",
        "dataset_type",
        "labels",
        "fingerprint",  # dataset revision / capture fingerprint
        "static_filter",
        "worker",
        "base_models",  # the base-model folders' import records
        "fps",
        "threshold",
        "threshold_source",
        "positive_quantile",
        "lookahead",
        "gamma",
        "failure_reward",
        "return_min",
        "return_max",
        "return_range_source",
        "compute_version",  # advantage.RECAP_COMPUTE_VERSION
    }
)
RESULT_IGNORED = frozenset(
    {
        "schema",
        "dataset",
        "job_id",
        "repo_id",
        "request",
        "last_request",
        "dataset_type_source",  # how the type was chosen, not the type
        "dataset_type_reason",
        "skipped_episodes",
        "dev_only_base_models",  # derived from base_models
        # LEVI's own code: what of it decides the numbers is versioned by
        # compute_version; a plain deploy must not block every merge.
        "levi_commit",
        "outcomes",  # per episode, carried with each episode
        "threshold_provenance",  # text
        "model",
        "version",
        "previous_version",
        "merged",
        "merged_from",
        "episodes",
        "episode_indices",
        "frames",
        "positive_fraction",
        "created_at",
        "revision_id",
        "layout",
    }
)
CHECKPOINT_AFFECTING = frozenset({"sha256", "manifest"})
CHECKPOINT_IGNORED = frozenset({"name"})

# A run's static_filter record: which frames are valued.
STATIC_FILTER_AFFECTING = frozenset({"mode", "applied", "rule", "source", "params"})
STATIC_FILTER_IGNORED = frozenset(
    {"reason", "frames", "kept_frames", "dropped_fraction", "skipped"}
)
# checkpoints.StaticFilter: all of it decides which frames are kept.
STATIC_FILTER_PARAMS = frozenset(
    {
        "rule",
        "xyz_threshold_m",
        "euler_threshold_rad",
        "gripper_epsilon",
        "gripper_protect_margin",
        "min_frames",
    }
)

# Worker provenance (integrations/recap_value/levi_recap_worker). Keys that
# repeat manifest fields are compared there and count as affecting here too.
WORKER_AFFECTING = frozenset(
    {
        "provider",
        "model",  # the fake provider's curve
        "rlinf_commit",
        "torch",
        "transformers",
        "transformers_patch",
        "decoder",
        "precision",
        "views",
        "env_type",
        "model_type",
        "action_dim",
        "action_horizon",
        "max_token_len",
        "critic_expert_variant",
        "weights_sha256",
    }
)
WORKER_IGNORED = frozenset(
    {
        # The device and batch size only change bfloat16 rounding (docs:
        # "bf16 across GPUs is not bit-identical"), timings and memory nothing.
        "device",
        "batch_size",
        "load",
        "load_seconds",
        "peak_vram_mib",
        "elapsed_seconds",  # added by runner.py
    }
)


def _canon(value: Any) -> Any:
    """JSON-comparable: numbers as floats (10 == 10.0), tuples as lists."""
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return str(value)


def _pick(value: Any, ignored: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"": value} if value is not None else {}
    return {k: v for k, v in value.items() if k not in ignored}


def payload(record: dict[str, Any], *, worker: bool = True) -> dict[str, Any]:
    """The normalised effective configuration of a record (or of the meta a
    run is about to publish). Unclassified keys are kept (they affect)."""
    checkpoint = record.get("checkpoint") or {}
    manifest = checkpoint.get("manifest") or {}
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key in RESULT_IGNORED or key in ("checkpoint", "static_filter", "worker"):
            continue
        out[key] = value
    out["checkpoint"] = _pick(checkpoint, CHECKPOINT_IGNORED | {"manifest"})
    out["manifest"] = _pick(manifest, MANIFEST_IGNORED)
    out["static_filter"] = _pick(
        record.get("static_filter") or {}, STATIC_FILTER_IGNORED
    )
    if worker:
        out["worker"] = _pick(record.get("worker") or {}, WORKER_IGNORED)
    return _canon(out)


def digest(record: dict[str, Any], *, worker: bool = True) -> str:
    text = json.dumps(payload(record, worker=worker), sort_keys=True)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def differences(
    old: dict[str, Any], new: dict[str, Any], *, worker: bool = True
) -> list[str]:
    """Dotted names of the effective settings that differ (empty: mergeable)."""
    a, b = payload(old, worker=worker), payload(new, worker=worker)
    out = []
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        if (
            isinstance(va, dict)
            and isinstance(vb, dict)
            and key
            in (
                "checkpoint",
                "manifest",
                "static_filter",
                "worker",
                "fingerprint",
            )
        ):
            out += [
                f"{key}.{sub}"
                for sub in sorted(set(va) | set(vb))
                if _norm(va.get(sub)) != _norm(vb.get(sub))
            ]
        elif _norm(va) != _norm(vb):
            out.append(key)
    return out


def _norm(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
