"""Signal facts of one episode, per channel and actor, as ``SignalObservation``.

The same readers as the signal lines (``agent.signals``): gripper crossings
from ``events.gripper`` on the episode's range, height turns, still spans on
the recorded timestamps (``events.motion``). The differences are what a
profile adds: every gripper of every actor is read (not just the first that
moved), a declared ``open_level`` decides which end is open, and every fact
says which feature, dimension and actor it came from. The signal lines
themselves are not changed by this module.
"""

import numpy as np

from . import gripper, motion
from .contracts import SignalObservation, SignalSource, window_at
from .signal_profiles import grippers, infer, positions


def _source(channel, feature=None):
    return SignalSource(
        feature=feature or channel.feature,
        dimension=channel.dimension,
        index=channel.index,
        kind=channel.kind,
        units=channel.units,
        frame=channel.frame,
    )


def _bounds(channel, stats):
    if channel.valid_range is not None:
        return tuple(channel.valid_range)
    s = (stats or {}).get(channel.feature) or {}
    try:
        return (float(s["min"][channel.index]), float(s["max"][channel.index]))
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _row(times, t):
    """The first row recorded at time ``t`` (a time the readers took from
    ``times``; not a search, so unsorted timestamps do not mislead it)."""
    return int(np.flatnonzero(times == t)[0])


def read(table, info, profile=None, stats=None, episode_index=0, source_sha256=None):
    """``[SignalObservation]`` of one episode's table, sorted by time."""
    from ..agent.signals import _matrix, turns

    profile = profile or infer(info)
    times = table["timestamp"].to_numpy(dtype=float)
    fps = float(info.get("fps") or 0) or None
    if not fps and len(times) > 1:
        fps = 1 / np.median(np.diff(times))
    matrices = {}

    def column(feature):
        if feature not in matrices:
            matrices[feature] = (
                _matrix(table, feature) if feature in table.columns else None
            )
        return matrices[feature]

    out = []

    def add(kind, actor, row, sources, *, value=None, window=None, time=None):
        out.append(
            SignalObservation(
                episode_index=episode_index,
                actor_id=actor,
                kind=kind,
                time_s=float(times[row]) if time is None else time,
                time_window_s=window or window_at(times, row),
                source_frame_index=row,
                sources=sources,
                value=value,
                source_sha256=source_sha256,
            )
        )

    for channel in grippers(profile):
        matrix = column(channel.feature)
        if matrix is None or channel.index >= matrix.shape[1]:
            continue
        found = gripper.read(
            matrix[:, channel.index],
            _bounds(channel, stats),
            range_source="episode",
            open_level=channel.open_level or "auto",
            min_travel=0.2,
        )
        if found is None:
            continue
        for row, kind in found.crossings:
            add(f"gripper_{kind}", channel.actor_id, row, [_source(channel)])

    for actor, (feature, dims) in positions(profile).items():
        matrix = column(feature)
        if matrix is None or max(dims) >= matrix.shape[1]:
            continue
        by_index = {c.index: c for c in profile.channels if c.feature == feature}
        z = by_index[dims[2]]
        for e in turns(times, matrix[:, dims[2]]):
            if e["kind"] in {"low", "high"}:
                row = _row(times, e["t"])
                add(f"height_{e['kind']}", actor, row, [_source(z)], value=e["value"])
        sources = [
            SignalSource(
                feature=feature,
                dimension=by_index[d].dimension,
                index=d,
                kind="derived",
                units=by_index[d].units,
                frame=by_index[d].frame,
            )
            for d in dims
        ]
        for start, end in motion.still_spans(times, matrix[:, dims], fps or 1):
            row = _row(times, start)
            add("still", actor, row, sources, window=(start, end), time=start)
    return sorted(out, key=lambda o: (o.time_s, o.actor_id, o.kind))
