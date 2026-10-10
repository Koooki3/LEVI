"""What the robot's own recorded signals say about an episode, as a few lines
of text.

Pictures show what happened; the state and action columns say *when*, to the
frame, for what a camera sees only between two sampled frames: the gripper
closing or opening, the arm reaching its lowest point, the arm standing still.
They are facts about the robot, not subtask boundaries and not attempts:
measured against a reference annotation (2026-09-23), a release sat on the
recorded opening while contact and lift did not sit on the closing or the
lowest point, and agents told to "count attempts" with them split repeated
grasps the task counted as one. So they are given without interpretation:
what a close or a turn means is the task definition's call.

Only what the dataset declares is used: float vector columns (``action``,
``observation.*``) and their dimension names in ``meta/info.json``. A gripper
is a dimension whose name says so; a position is one named ``x``/``y``/``z``
(or ending in ``_x``...). Nothing is assumed about the task, the units or the
robot; datasets without such names get the arm's overall stillness only, and
datasets without vector columns get no signals at all.
"""

import json
import re
from pathlib import Path

import numpy as np

from ..events import gripper, motion

# Hysteresis on the gripper channel's range, shared with the other readers.
from ..events.gripper import HIGH, LOW  # noqa: F401
from ..events.motion import STILL, STILL_SECONDS  # noqa: F401

GRIPPER = re.compile(r"grip|finger|claw|jaw", re.IGNORECASE)
AXIS = re.compile(r"^(?:.*[_.\s-])?(?:pos(?:ition)?[_.\s-]?)?([xyz])$", re.IGNORECASE)
# A turn in height counts when the arm comes back by this share of the
# episode's height range.
TURN = 0.15
MAX_ITEMS = 24


def _names(feature):
    names = feature.get("names")
    if isinstance(names, dict):
        names = next(iter(names.values()), None)
    if isinstance(names, list) and all(isinstance(n, str) for n in names):
        return names
    return None


def vector_columns(info):
    """Float vector columns with their dimension names (None when unnamed)."""
    out = {}
    for key, feature in (info.get("features") or {}).items():
        if not (key == "action" or key.startswith("observation.")):
            continue
        if feature.get("dtype") not in {"float32", "float64"}:
            continue
        shape = feature.get("shape") or []
        if len(shape) != 1 or shape[0] < 1:
            continue
        out[key] = _names(feature)
    return out


def _matrix(table, key):
    values = table[key].to_numpy()
    try:
        return np.stack([np.asarray(v, dtype=float).reshape(-1) for v in values])
    except (TypeError, ValueError):
        return None


def _t(x):
    return f"{x:.1f}"


def _clip(items):
    if len(items) > MAX_ITEMS:
        return [*items[:MAX_ITEMS], f"… +{len(items) - MAX_ITEMS} more"]
    return items


def gripper_events(times, values, bounds=None, open_level="auto", start="first"):
    """Close/open crossings of one gripper dimension.

    Crossings are found on the episode's own range, so a grasp on a wide
    object that never nears the gripper's minimum still counts. Unless
    ``open_level`` says which end is open, the level of the episode's first
    sample is taken as "open" (a robot starts an episode with an empty
    hand): an episode that starts holding something reads upside down, and
    only a declared ``open_level`` reads it right. ``start="robust"`` reads
    the starting level from the first three samples instead, so one stale
    first sample does not turn the episode upside down -- but neither does
    a real close-and-open in the first frames read right then, which is why
    it is not the default (a policy that closes for the first second of a
    rollout and then opens is a recorded case). With the dataset's range
    (``bounds``) a continuous channel also says how far it closed, as a
    share of that range -- a gripper stopped well above fully closed is
    usually holding something. The crossings are ``events.gripper``'s, the
    kernel anchored reviews use too, on the episode's range rather than the
    dataset's."""
    channel = gripper.read(
        values,
        bounds,
        range_source="episode",
        open_level=open_level,
        # Jitter of a gripper that never moved is no grasp.
        min_travel=0.2,
        start=start,
    )
    if channel is None:
        return []
    u, bounds = channel.u, channel.bounds
    flip = channel.open_level == "low"
    finite = np.isfinite(values)
    # An open/closed flag (only the dataset's two extremes) has no "how far".
    binary = bounds is None or np.all(
        np.isin(np.round(values[finite], 6), np.round(bounds, 6))
    )
    events = []
    for i, kind in channel.crossings:
        event = {"t": float(times[i]), "kind": kind}
        if kind == "close" and bounds and not binary:
            # How far it closed: the bottom of the fall that crossed.
            j = i
            while j + 1 < len(u) and not np.isnan(u[j + 1]) and u[j + 1] < u[j] - 1e-6:
                j += 1
            share = (values[j] - bounds[0]) / (bounds[1] - bounds[0])
            event["level"] = float(np.clip(1 - share if flip else share, 0, 1))
        events.append(event)
    return events


def turns(times, values):
    """Height extremes: the arm's lowest and highest points between moves."""
    finite = np.isfinite(values)
    if finite.sum() < 3:
        return []
    span = np.nanmax(values) - np.nanmin(values)
    if span <= 1e-9:
        return []
    need = TURN * span
    idx = np.flatnonzero(finite)
    out = [(int(idx[0]), "start")]
    hi_i = lo_i = int(idx[0])
    direction = 0
    for i in idx[1:]:
        v = values[i]
        if direction >= 0:
            if v > values[hi_i]:
                hi_i = i
            if values[hi_i] - v >= need:
                if direction > 0 or hi_i != out[-1][0]:
                    out.append((hi_i, "high"))
                direction, lo_i = -1, i
                continue
        if direction <= 0:
            if v < values[lo_i]:
                lo_i = i
            if v - values[lo_i] >= need:
                if direction < 0 or lo_i != out[-1][0]:
                    out.append((lo_i, "low"))
                direction, hi_i = 1, i
    last = int(idx[-1])
    if last != out[-1][0]:
        out.append((last, "end"))
    # An extreme at the first or last sample is the start or end itself.
    edge = (times[idx[0]], times[last])
    out = [
        (i, kind)
        for i, kind in out
        if kind in {"start", "end"}
        or min(abs(times[i] - edge[0]), abs(times[i] - edge[1])) > 0.3
    ]
    return [
        {"t": float(times[i]), "kind": kind, "value": float(values[i])}
        for i, kind in out
    ]


def still_spans(times, positions, fps):
    """Where the arm stands still, on the recorded timestamps (see
    ``events.motion``)."""
    return motion.still_spans(times, positions, fps)


def summarize(table, info, stats=None, open_levels=None, start="first"):
    """Signal lines and machine-readable events for one episode's table.
    ``open_levels`` may declare which end of a gripper channel is open
    (``{"observation.state.gripper": "high" | "low"}``); undeclared channels
    take the level the episode starts at, read from its first sample or,
    with ``start="robust"``, its first three (see ``gripper_events``)."""
    columns = vector_columns(info)
    fps = float(info.get("fps") or 0) or None
    times = table["timestamp"].to_numpy(dtype=float)
    if not fps and len(times) > 1:
        fps = 1 / np.median(np.diff(times))
    lines, events = [], []
    grip_done = False
    # A recorded gripper that never moved still makes its command a command,
    # not the gripper's own record.
    recorded_gripper = False
    position = None
    # Recorded state before commands: it is what happened, not what was asked.
    ordered = sorted(columns, key=lambda k: (k == "action", k))
    commands = {}
    for key in ordered:
        matrix = _matrix(table, key)
        names = columns[key]
        if matrix is None or (names and len(names) != matrix.shape[1]):
            continue
        names = names or [None] * matrix.shape[1]
        for d, name in enumerate(names):
            if not name or not GRIPPER.search(name):
                continue
            bounds = None
            s = (stats or {}).get(key) or {}
            if isinstance(s.get("min"), list) and isinstance(s.get("max"), list):
                try:
                    bounds = (float(s["min"][d]), float(s["max"][d]))
                except (IndexError, TypeError, ValueError):
                    bounds = None
            found = gripper_events(
                times,
                matrix[:, d],
                bounds,
                (open_levels or {}).get(f"{key}.{name}", "auto"),
                start,
            )
            if key == "action":
                commands[name] = found
                if grip_done or recorded_gripper:
                    continue
            else:
                recorded_gripper = True
            if not found:
                continue
            grip_done = True
            parts = []
            for e in found:
                text = f"{e['kind']} {_t(e['t'])}"
                if "level" in e:
                    text += f" (to {e['level']:.0%})"
                parts.append(text)
                events.append({"t": e["t"], "kind": e["kind"]})
            lines.append(f"gripper ({key}.{name}): " + ", ".join(_clip(parts)))
        axes = {}
        for d, name in enumerate(names):
            m = AXIS.match(name or "")
            if m and not GRIPPER.search(name or ""):
                axes.setdefault(m.group(1).lower(), d)
        if position is None and {"x", "y", "z"} <= set(axes):
            position = (key, [axes["x"], axes["y"], axes["z"]], matrix)
    if commands and (grip_done or recorded_gripper):
        # The command is the intent; it earns a line only when the recorded
        # gripper did not follow it closely (a command it never carried out,
        # or one that took the gripper a while).
        recorded = [e for e in events if e["kind"] in {"close", "open"}]
        for name, found in commands.items():
            if not found:
                continue
            followed = len(found) == len(recorded) and all(
                a["kind"] == b["kind"] and 0 <= b["t"] - a["t"] <= 0.3
                for a, b in zip(found, recorded, strict=True)
            )
            if not followed:
                parts = [f"{e['kind']} {_t(e['t'])}" for e in found]
                lines.append(
                    f"gripper command (action.{name}): " + ", ".join(_clip(parts))
                )
            break
    if position is not None:
        key, dims, matrix = position
        z = matrix[:, dims[2]]
        tt = turns(times, z)
        if len(tt) > 2:
            arrow = {"low": "low", "high": "high", "start": "start", "end": "end"}
            parts = [f"{arrow[e['kind']]} {e['value']:.3g}@{_t(e['t'])}" for e in tt]
            lines.append(f"height ({key}.z): " + ", ".join(_clip(parts)))
            events += [
                {"t": e["t"], "kind": e["kind"]}
                for e in tt
                if e["kind"] in {"low", "high"}
            ]
        spans = still_spans(times, matrix[:, dims], fps or 1)
    else:
        # No named position: the arm's overall motion over every non-gripper
        # dimension of the first state column, each scaled to its range.
        spans = []
        for key in ordered:
            matrix = _matrix(table, key)
            names = columns[key]
            if matrix is None:
                continue
            keep = [
                d
                for d in range(matrix.shape[1])
                if not (names and names[d] and GRIPPER.search(names[d]))
            ]
            if not keep:
                continue
            part = matrix[:, keep]
            scale = np.nanmax(part, axis=0) - np.nanmin(part, axis=0)
            scale[~np.isfinite(scale) | (scale <= 1e-9)] = 1
            spans = still_spans(times, part / scale, fps or 1)
            break
    if spans:
        lines.append(
            "still: " + ", ".join(_clip([f"{_t(a)}–{_t(b)}" for a, b in spans]))
        )
    return {"lines": lines, "events": sorted(events, key=lambda e: e["t"])}


def episode_signals(context, root, episode):
    """Signals of one episode from a run's snapshot; None when the dataset
    has nothing to read (no declared vector columns)."""
    from . import media

    root = Path(root)
    info_path = root / "meta" / "info.json"
    if not info_path.is_file():
        return None
    info = json.loads(info_path.read_text())
    if not vector_columns(info):
        return None
    stats = None
    stats_path = root / "meta" / "stats.json"
    if stats_path.is_file():
        try:
            stats = json.loads(stats_path.read_text())
        except ValueError:
            stats = None
    table = media.episode_table(media.snapshot_state(context, root), episode)
    result = summarize(table, info, stats)
    return result if result["lines"] else None


def for_run(wb, run, episode):
    """Signals of one episode of a run, or None. Never raises: signals are
    an addition to the evidence, and a dataset they cannot be read from must
    still be annotated from its pictures."""
    from .schema import TaskContext

    try:
        context = TaskContext.model_validate(run["context"])
        return episode_signals(context, wb.store.run_dir(run["id"]) / "input", episode)
    except Exception:  # noqa: BLE001 - see docstring
        return None
