"""Read-only association to a legacy client's C2 session.

The client does not put campaign notes in C2. A matching rollout must first
prove the campaign, segment and arm code. Never select a newest session or
infer identity from a checkpoint basename. No label or robot command is
returned or executed here.
"""

import os
import time
from pathlib import Path

from levi.live import sessions

from . import adapters as A
from . import guided as G


def _path(value):
    return (
        os.path.realpath(value)
        if isinstance(value, str) and os.path.isabs(value)
        else None
    )


def associate(plan, folder, root, segment, *, prompt, not_before, now=None):
    out = {"status": "waiting", "segment": segment}
    if not root or not plan or not segment or not_before is None:
        return out
    child = A.child_of(plan, segment)
    if child is None:
        return out
    arm = child["arm"]
    expected = plan["campaign"]["arms"][arm]["policy_forward"]
    group = A.groups_of(plan)[arm]
    if not prompt:
        return {**out, "status": "unavailable"}
    try:
        collected = A.collect(plan, folder, root, segment)
    except (G.GuidedError, OSError, ValueError, KeyError):
        return {**out, "status": "unavailable"}
    facts = [f for rows in collected["facts"].values() for f in rows]
    proven = {(f.run_id, f.episode_id.split("/", 2)[1]) for f in facts}
    if not proven:
        return out
    now = time.time() if now is None else now
    folder_c2 = Path(root) / sessions.SESSION_DIR
    try:
        if folder_c2.is_symlink():
            return {**out, "status": "unavailable"}
        entries = sorted(folder_c2.iterdir())
    except FileNotFoundError:
        return out
    except OSError:
        return {**out, "status": "unavailable"}
    matched, rejected = [], False
    for path in entries[:1000]:
        if not path.name.endswith(".json") or path.name.startswith("."):
            continue
        raw = A.read_json(path, limit=64 * 1024)
        if not isinstance(raw, dict):
            continue
        s = sessions._session(str(path), path.name, raw, now)
        if s is None or (s.run_id, s.task_folder) not in proven:
            continue
        policy = raw.get("policy") if isinstance(raw.get("policy"), dict) else {}
        if (
            raw.get("schema") != "levi.eval.session.v1"
            or s.group != group
            or not s.session_id
            or not s.run_id
            or s.started_at is None
            or s.started_at < not_before
            or s.updated_at is None
            or s.updated_at > now + 5
            or s.label_mode != "dual_label"
            or s.levi_enabled is not True
            or policy.get("config") != expected["config"]
            or _path(policy.get("checkpoint_dir")) != _path(expected["checkpoint_dir"])
            or _path(expected["checkpoint_dir"]) is None
            or s.prompt != prompt
        ):
            rejected = True
            continue
        matched.append(s)
    # A dead or stale session can be inspected, but must never cause a
    # redirect to it while another matching writer is active.
    active = [
        s
        for s in matched
        if not s.crashed
        and s.state not in ("finished", "stopped")
        and s.age_s is not None
        and s.age_s <= sessions.STALE_S
    ]
    choices = active if active else matched
    if len(choices) > 1:
        return {**out, "status": "ambiguous", "candidates": len(choices)}
    if not choices:
        return {**out, "status": "mismatch" if rejected else "waiting"}
    s = choices[0]
    s.root = str(Path(root).expanduser())
    return {
        **out,
        "status": "linked" if active else "ended",
        "session": {**s.public(), "dataset": None, "fault": s.fault},
    }
