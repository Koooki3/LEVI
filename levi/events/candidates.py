"""Event candidates of one episode from every signal source, merged.

``read`` turns what the readers found -- gripper crossings, height turns and
the edges of still spans (``facts``), penalised change points
(``change_points``) -- into ``EventCandidate``s, merges the ones that point
at the same moment and orders them for evidence gathering.

The order is a fixed rule, not a fitted model:

- each source has a priority (``PRIORITY``): a gripper crossing first (a
  grasp or release is the event subtask boundaries in manipulation most
  often sit on), then a change point (scaled by its own salience, the cost
  it saves against the penalty), a height turn, the edge of a still span;
- candidates of any source within ``merge_seconds`` of a stronger one are
  merged into it: the stronger keeps its time, takes their sources and its
  window grows to cover theirs; the merged ones stay in the list with status
  ``merged``, so nothing found is lost from the record;
- ties: more distinct sources first, then earlier, then by id.

``salience`` is this priority, on [0, 1]: an evidence ordering, **not a
probability**, and the constants were set by hand (not tuned on any gold
set). A candidate is a place to look, never a subtask boundary by itself.

Pure: a table, ``meta/info.json`` (and optionally a signal profile and
``meta/stats.json``) in, records out. No model, no files.
"""

import numpy as np

from .contracts import EventCandidate, window_at

SOURCES = ("gripper", "height", "still", "change_point")
# Evidence priority of each source (see the module docstring); a change
# point's own salience scales its share.
PRIORITY = {"gripper": 0.9, "change_point": 0.7, "height": 0.5, "still": 0.3}
MERGE_SECONDS = 0.5


def _source_of(event_type):
    if event_type.startswith("gripper_"):
        return "gripper"
    if event_type.startswith("height_"):
        return "height"
    if event_type.startswith("still_"):
        return "still"
    return "change_point"


def _nearest(times, t):
    return int(np.abs(times - t).argmin())


def _from_facts(observations, times, episode_index, wanted):
    out, counts = [], {}

    def add(event_type, actor, row, sources, window=None):
        k = counts.get((event_type, actor), 0)
        counts[(event_type, actor)] = k + 1
        center = float(times[row])
        out.append(
            EventCandidate(
                id=f"{event_type}_{actor}_{k:03d}",
                episode_index=episode_index,
                event_type=event_type,
                actor_id=actor,
                center_time_s=center,
                time_window_s=window or window_at(times, row),
                source_frame_index=row,
                sources=sources,
                salience=PRIORITY[_source_of(event_type)],
            )
        )

    for fact in observations:
        source = _source_of(fact.kind)
        if source in {"gripper", "height"} and source in wanted:
            add(fact.kind, fact.actor_id, fact.source_frame_index, fact.sources)
        elif fact.kind == "still" and "still" in wanted:
            # A still span starts where a motion ended and ends where the
            # next began: both edges are places where something changed.
            start, end = fact.time_window_s
            add("still_start", fact.actor_id, _nearest(times, start), fact.sources)
            if end > start:
                add("still_end", fact.actor_id, _nearest(times, end), fact.sources)
    return out


def merge(found, merge_seconds=MERGE_SECONDS):
    """``found`` in priority order, each within ``merge_seconds`` of a
    stronger kept one marked ``merged`` and folded into it (see the module
    docstring). Returns new records; the input is not changed."""

    def order(c):
        return (-c.salience, c.center_time_s, c.id)

    kept, absorbed = [], []
    groups = {}
    for c in sorted(found, key=order):
        home = next(
            (
                k
                for k in kept
                if abs(k.center_time_s - c.center_time_s) <= merge_seconds + 1e-9
            ),
            None,
        )
        if home is None:
            kept.append(c)
            groups[c.id] = [c]
        else:
            groups[home.id].append(c)
            absorbed.append(c.model_copy(update={"status": "merged"}))
    out = []
    for k in kept:
        members = groups[k.id]
        sources, seen = [], set()
        for m in members:
            for s in m.sources:
                key = s.model_dump_json()
                if key not in seen and len(sources) < 16:
                    seen.add(key)
                    sources.append(s)
        out.append(
            k.model_copy(
                update={
                    "sources": sources,
                    "time_window_s": (
                        min(m.time_window_s[0] for m in members),
                        max(m.time_window_s[1] for m in members),
                    ),
                }
            )
        )
    distinct = {
        k.id: len({_source_of(m.event_type) for m in groups[k.id]}) for k in kept
    }
    out.sort(key=lambda c: (-c.salience, -distinct[c.id], c.center_time_s, c.id))
    return out + absorbed


def read(
    table,
    info,
    profile=None,
    stats=None,
    episode_index=0,
    sources=SOURCES,
    merge_seconds=MERGE_SECONDS,
    change_point_penalty=None,
):
    """``[EventCandidate]`` of one episode: the kept candidates first, in
    evidence priority, then the merged ones. ``sources`` picks the readers
    (``SOURCES``); ``change_point_penalty`` overrides the change-point
    detector's default penalty."""
    from . import change_points, facts

    unknown = set(sources) - set(SOURCES)
    if unknown:
        raise ValueError(f"Unknown event candidate sources: {sorted(unknown)}")
    times = table["timestamp"].to_numpy(dtype=float)
    found = []
    if set(sources) & {"gripper", "height", "still"}:
        found += _from_facts(
            facts.read(table, info, profile, stats, episode_index),
            times,
            episode_index,
            set(sources),
        )
    if "change_point" in sources:
        params = (
            {} if change_point_penalty is None else {"penalty": change_point_penalty}
        )
        for c in change_points.candidates(
            table, info, profile, stats, episode_index, **params
        ):
            found.append(
                c.model_copy(update={"salience": PRIORITY["change_point"] * c.salience})
            )
    return merge(found, merge_seconds)


def proposed(candidates):
    """The kept candidates (status ``proposed``), in the order given."""
    return [c for c in candidates if c.status == "proposed"]
