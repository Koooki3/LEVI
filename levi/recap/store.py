"""Revisioned per-dataset RECAP value / advantage results.

``outputs/LEVI/workbench/recap_values/<catalog name>/``::

    revisions/<id>/episode-NNNNNN.parquet   per-frame values and labels
    revisions/<id>/advantages.parquet       RLinf's advantages_{tag}.parquet columns
    revisions/<id>/revision.json            checkpoint, parameters, threshold, provenance
    revisions/<id>/summary.json             per-episode means and positive fractions
    current.json                            the revision the UI shows
    jobs/, plans/, results/                 worker runs (mutable, never published)

Keyed by catalog name, so a namespace keeps its own results. Kept outside
agent bundles: labels come from a model, not from an annotator.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .. import catalog, naming

SCHEMA = "levi.recap_value.revision.v1"
EPISODE_SCHEMA = pa.schema(
    [
        ("episode_index", pa.int64()),
        ("frame_index", pa.int64()),
        ("timestamp", pa.float64()),
        ("value", pa.float32()),
        ("value_next", pa.float32()),
        ("reward_sum", pa.float32()),
        ("return", pa.float32()),
        ("advantage", pa.float32()),
        ("positive", pa.bool_()),
    ]
)
# RLinf save_advantages_to_dataset writes these (pandas from Python lists).
RLINF_SCHEMA = pa.schema(
    [
        ("episode_index", pa.int64()),
        ("frame_index", pa.int64()),
        ("advantage_continuous", pa.float64()),
        ("return", pa.float64()),
        ("value_current", pa.float64()),
        ("value_next", pa.float64()),
        ("reward_sum", pa.float64()),
        ("reward_sum_raw", pa.float64()),
        ("num_valid_rewards", pa.int64()),
        ("dataset_name", pa.string()),
        ("advantage", pa.bool_()),
    ]
)


def root(name: str) -> Path:
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("Invalid dataset name")
    return catalog.STATE / "recap_values" / name


def write_json(path: Path, value: Any) -> None:
    catalog.atomic(path, value)


def write_table(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    pq.write_table(table, temp)
    os.replace(temp, path)


_HELD = threading.local()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()


@contextlib.contextmanager
def locked(name: str):
    """Serialises publication for one dataset across threads and LEVI
    processes sharing the workspace. Re-entrant within a thread (an flock
    taken twice by one process through two handles would deadlock)."""
    with _GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(name, threading.RLock())
    with thread_lock:
        held = _HELD.__dict__.setdefault("depth", {})
        if held.get(name):
            held[name] += 1
            try:
                yield
            finally:
                held[name] -= 1
            return
        folder = root(name)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            held[name] = 1
            try:
                yield
            finally:
                held[name] = 0
                fcntl.flock(handle, fcntl.LOCK_UN)


def current_id(name: str) -> str | None:
    value = catalog.read(root(name) / "current.json", {})
    rid = value.get("revision_id")
    if rid and (root(name) / "revisions" / rid / "revision.json").is_file():
        return rid
    return None


def revision(name: str, rid: str | None = None) -> dict[str, Any] | None:
    rid = rid or current_id(name)
    if not rid or not naming.is_timestamp_id(rid):
        return None
    path = root(name) / "revisions" / rid / "revision.json"
    return catalog.read(path, None)


def summary(name: str, rid: str | None = None) -> dict[str, Any] | None:
    rid = rid or current_id(name)
    if not rid or not naming.is_timestamp_id(rid):
        return None
    return catalog.read(root(name) / "revisions" / rid / "summary.json", None)


def read_episode(name: str, episode: int, rid: str | None = None):
    rid = rid or current_id(name)
    if not rid:
        return None
    path = root(name) / "revisions" / rid / f"episode-{episode:06d}.parquet"
    if not path.is_file():
        return None
    return pq.read_table(path)


def publish(
    name: str,
    episodes: dict[int, dict[str, np.ndarray]],
    meta: dict[str, Any],
    *,
    dataset_name: str,
) -> dict[str, Any]:
    """Write one complete revision, then point ``current.json`` at it.

    ``episodes`` maps episode index to per-frame arrays (frame_index,
    timestamp, value, value_next, reward_sum, reward_sum_raw, return,
    advantage, num_valid_rewards, positive). The revision directory is
    reserved first; ``revision.json`` is written last, so a directory
    without it is an interrupted publication and is never served."""
    folder = root(name) / "revisions"
    rid = naming.timestamp_id(folder, create_dir=True)
    target = folder / rid
    per_episode: dict[str, Any] = {}
    rlinf_parts = []
    frames, positives = 0, None
    for ep in sorted(episodes):
        cols = episodes[ep]
        n = len(cols["value"])
        labelled = cols.get("positive") is not None
        positive_column = (
            pa.array(np.asarray(cols["positive"], bool))
            if labelled
            else pa.nulls(n, pa.bool_())
        )
        table = pa.table(
            {
                "episode_index": pa.array(np.full(n, ep, dtype=np.int64)),
                "frame_index": pa.array(np.asarray(cols["frame_index"], np.int64)),
                "timestamp": pa.array(np.asarray(cols["timestamp"], np.float64)),
                "value": pa.array(np.asarray(cols["value"], np.float32)),
                "value_next": pa.array(np.asarray(cols["value_next"], np.float32)),
                "reward_sum": pa.array(np.asarray(cols["reward_sum"], np.float32)),
                "return": pa.array(np.asarray(cols["return"], np.float32)),
                "advantage": pa.array(np.asarray(cols["advantage"], np.float32)),
                "positive": positive_column,
            },
            schema=EPISODE_SCHEMA,
        )
        write_table(target / f"episode-{ep:06d}.parquet", table)
        rlinf_parts.append(
            pa.table(
                {
                    "episode_index": pa.array(np.full(n, ep, dtype=np.int64)),
                    "frame_index": pa.array(np.asarray(cols["frame_index"], np.int64)),
                    "advantage_continuous": pa.array(
                        np.asarray(cols["advantage"], np.float64)
                    ),
                    "return": pa.array(np.asarray(cols["return"], np.float64)),
                    "value_current": pa.array(np.asarray(cols["value"], np.float64)),
                    "value_next": pa.array(np.asarray(cols["value_next"], np.float64)),
                    "reward_sum": pa.array(np.asarray(cols["reward_sum"], np.float64)),
                    "reward_sum_raw": pa.array(
                        np.asarray(cols["reward_sum_raw"], np.float64)
                    ),
                    "num_valid_rewards": pa.array(
                        np.asarray(cols["num_valid_rewards"], np.int64)
                    ),
                    "dataset_name": pa.array([dataset_name] * n, pa.string()),
                    "advantage": positive_column,
                },
                schema=RLINF_SCHEMA,
            )
        )
        frames += n
        # A value-only result (no outcomes, so no returns) has no labels:
        # its fractions are null, never 0 (that would read as "all negative").
        positive = int(np.count_nonzero(cols["positive"])) if labelled else None
        if labelled:
            positives = (positives or 0) + positive
        per_episode[str(ep)] = {
            "positive_fraction": (positive / n if n else 0.0) if labelled else None,
            "mean_advantage": (float(np.mean(cols["advantage"])) if n else 0.0)
            if labelled
            else None,
            "mean_value": float(np.mean(cols["value"])) if n else 0.0,
            # The curve's range (the viewer's dataset-wide value axis); older
            # results lack both keys and readers must treat them as optional.
            "min_value": float(np.min(cols["value"])) if n else None,
            "max_value": float(np.max(cols["value"])) if n else None,
            "frames": n,
        }
    write_table(
        target / "advantages.parquet",
        pa.concat_tables(rlinf_parts) if rlinf_parts else RLINF_SCHEMA.empty_table(),
    )
    record = {
        "schema": SCHEMA,
        "revision_id": rid,
        "dataset": name,
        **meta,
        "episodes": len(episodes),
        "episode_indices": sorted(int(e) for e in episodes),
        "frames": frames,
        "positive_fraction": None
        if positives is None
        else (positives / frames if frames else 0.0),
        "created_at": time.time(),
    }
    write_json(
        target / "summary.json",
        {
            "revision_id": rid,
            "threshold": meta.get("threshold"),
            "threshold_source": meta.get("threshold_source"),
            "skipped_episodes": meta.get("skipped_episodes", {}),
            "episodes": per_episode,
        },
    )
    write_json(target / "revision.json", record)
    write_json(
        root(name) / "current.json", {"revision_id": rid, "updated_at": time.time()}
    )
    return record


def revisions(name: str) -> list[str]:
    folder = root(name) / "revisions"
    if not folder.is_dir():
        return []
    return sorted(
        p.name
        for p in folder.iterdir()
        if naming.is_timestamp_id(p.name) and (p / "revision.json").is_file()
    )


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
