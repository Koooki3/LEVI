"""Which policy produced a rollout, and how it was run.

Raw rollouts record it in ``metadata.json``: ``policy.config`` (the model),
``policy.checkpoint_dir``, ``policy.method``/``method`` (``dsrl``, ``rlt``,
``sfe``, ``student``; absent for a direct deployment), ``policy.phase`` and
``control_mode``. The folder layout repeats it (``models/<policy>/...`` and
``online_rl/<method>/<policy>/...``). LeRobot rollouts carry
``rollout_source_method`` and ``rollout_source_demo`` (the raw capture).

Nothing here is task specific: it reads those fields, in that priority.
"""

import re
from pathlib import Path

METHODS = ("direct", "dsrl", "rlt", "sfe", "student", "other", "unknown")
KNOWN = {"dsrl", "rlt", "sfe", "student"}
DIRECT = {"direct", "direct_deployment", "policy_rollout", "models", "model"}
LABELS = {
    "direct": "direct deployment",
    "dsrl": "DSRL online RL",
    "rlt": "RLT online RL",
    "sfe": "SFE online RL",
    "student": "student policy",
    "other": "other method",
    "unknown": "unknown method",
}
# Folder names that carry the run method (``online_rl/<method>/<policy>``).
ONLINE_RL_FOLDER = "online_rl"
DIRECT_FOLDER = "models"
FIELDS = (
    "policy_model",
    "policy_checkpoint",
    "policy_method",
    "policy_phase",
)


def _text(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def normalize_method(value, *, strict: bool = True) -> str | None:
    """A recorded method / control mode / folder as one of ``METHODS``.

    ``strict`` (a field that names a method): anything unrecognised is
    ``other``. Not strict (``control_mode``, a folder): unrecognised is
    ``None`` -- a teleoperation mode is not a policy method."""
    text = _text(value)
    if not text:
        return None
    key = text.lower().replace("-", "_").replace(" ", "_")
    if key in KNOWN:
        return key
    if key in DIRECT:
        return "direct"
    for suffix in ("_online_rl", "_policy_rollout", "_rollout"):
        if key.endswith(suffix) and key[: -len(suffix)] in KNOWN:
            return key[: -len(suffix)]
    return "other" if strict else None


def folder_policy(path) -> dict:
    """``method`` (and ``model``) from ``.../models/<policy>/...`` or
    ``.../online_rl/<method>/<policy>/...`` (the deepest such folder)."""
    parts = Path(str(path)).parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == ONLINE_RL_FOLDER and i + 1 < len(parts):
            method = normalize_method(parts[i + 1], strict=False)
            model = parts[i + 2] if i + 2 < len(parts) else None
            return {"method": method or "other", "model": model}
        if parts[i] == DIRECT_FOLDER and i + 1 < len(parts):
            return {"method": "direct", "model": parts[i + 1]}
    return {}


def _checkpoint(value) -> str | None:
    text = _text(value)
    return Path(text.rstrip("/")).name if text else None


def from_metadata(meta: dict) -> dict:
    """The policy fields of a raw capture's ``metadata.json``.

    ``policy_method`` is ``None`` when the metadata does not say how it was
    run; ``finish`` then tries the folder and, for a rollout, ``unknown``."""
    block = meta.get("policy")
    text = _text(block)  # older captures: "policy": "<name>"
    block = block if isinstance(block, dict) else {}
    server = block.get("server_metadata")
    server = server if isinstance(server, dict) else {}
    model = _text(block.get("config")) or _text(server.get("config")) or text
    checkpoint = _checkpoint(block.get("checkpoint_dir")) or _checkpoint(
        server.get("checkpoint_dir")
    )
    candidates = [
        normalize_method(block.get("method")),
        normalize_method(meta.get("method")),
        normalize_method(meta.get("control_mode"), strict=False),
    ]
    method = next((m for m in candidates if m and m != "other"), None) or next(
        (m for m in candidates if m), None
    )
    return {
        "policy_model": model,
        "policy_checkpoint": checkpoint,
        "policy_method": method,
        "policy_phase": _text(block.get("phase")),
    }


def from_lerobot(row: dict, linked: dict | None) -> dict:
    """A LeRobot rollout: the linked raw capture's fields, then the
    episode's own ``rollout_source_method`` / ``rollout_source_demo`` /
    ``rollout_policy`` for what the capture leaves empty."""
    out = {k: (linked or {}).get(k) for k in FIELDS}
    if out["policy_method"] == "unknown":
        out["policy_method"] = None
    folder = folder_policy(row.get("rollout_source_demo") or "")
    own = normalize_method(row.get("rollout_source_method"))
    if not out["policy_method"]:
        out["policy_method"] = (
            own if own and own != "other" else folder.get("method") or own
        )
    out["policy_model"] = out["policy_model"] or folder.get("model")
    out["policy_checkpoint"] = out["policy_checkpoint"] or _text(
        row.get("rollout_policy")
    )
    out["policy_phase"] = out["policy_phase"] or _text(row.get("rollout_policy_phase"))
    return out


def legacy(model, checkpoint) -> str | None:
    """The old single ``policy`` column: the checkpoint, else the model."""
    return checkpoint or model


def short_checkpoint(checkpoint: str | None) -> str | None:
    """``pi05_fr3_all_step49999`` -> ``step49999``."""
    if not checkpoint:
        return None
    match = re.search(r"(step[_-]?\d+)$", checkpoint)
    return match.group(1) if match else checkpoint


def label(fields: dict) -> str | None:
    """``pi05_fr3_all_state · step49999 · DSRL online RL``."""
    method = fields.get("policy_method")
    if not method and not fields.get("policy_model"):
        return None
    parts = [
        fields.get("policy_model"),
        short_checkpoint(fields.get("policy_checkpoint")),
        LABELS.get(method or "unknown", method),
    ]
    return " · ".join(p for p in parts if p)


def finish(fields: dict, category: str | None, folder=None) -> dict:
    """Complete a row's policy columns once its category is known. A
    rollout the metadata leaves open is read from its ``folder`` layout
    (``models/...``, ``online_rl/<method>/...``), and one with no policy
    information at all gets method ``unknown``. Other categories keep what
    the capture recorded (usually nothing)."""
    out = {k: fields.get(k) for k in FIELDS}
    if out.get("policy_method") == "unknown":
        out["policy_method"] = None
    if category == "rollout":
        place = folder_policy(folder) if folder is not None else {}
        found = place.get("method")
        if not out.get("policy_method") or (
            out["policy_method"] == "other" and found not in (None, "other")
        ):
            out["policy_method"] = found
        out["policy_model"] = out.get("policy_model") or place.get("model")
        out["policy_method"] = out["policy_method"] or "unknown"
    out["policy"] = legacy(out.get("policy_model"), out.get("policy_checkpoint"))
    out["policy_label"] = label(out)
    return out
