"""Per-dataset RECAP value / advantage results.

``outputs/LEVI/workbench/recap_values/<catalog name>/`` holds one of two
layouts (both are read; ``LEVI_RECAP_STORE_LAYOUT`` picks the one written):

``models`` (the default) -- one result per (dataset, value model),
recomputing replaces it::

    models/<checkpoint>/head.json            {"version": ...}: the live version
    models/<checkpoint>/v/<version>/         episode-NNNNNN.parquet,
                                             advantages.parquet, summary.json,
                                             result.json (written last)
    current.json                             {"model": ...}: the result shown

``revisions`` (the original layout, ``LEVI_RECAP_STORE_LAYOUT=revisions``)
-- one revision per run::

    revisions/<id>/episode-NNNNNN.parquet   per-frame values and labels
    revisions/<id>/advantages.parquet       RLinf's advantages_{tag}.parquet columns
    revisions/<id>/revision.json            checkpoint, parameters, threshold, provenance
    revisions/<id>/summary.json             per-episode means and positive fractions
    current.json                            {"revision_id": ...}

Both keep ``jobs/``, ``plans/``, ``results/`` (worker runs) and
``dataset.json`` (the dataset-level type) beside them.

A result is named by a *ref*: the checkpoint name of a model result, or the
revision id of an original-layout revision. A model result's *version*
changes on every recomputation. A new version is written completely
(files fsynced, ``result.json`` last), then ``head.json`` is replaced
atomically; a reader resolves the head once and reads only inside that
version, which stays on disk for ``RETIRE_GRACE_SECONDS`` after it is
replaced. A crash before the head switch leaves the old version live; the
unfinished directory is removed by the next publication under the lock.

Recomputing a subset of episodes merges into the model's result when the
parameters that decide the numbers are identical (``signature``): untouched
episodes are hard-linked from the previous version. Different parameters
are refused (``MergeRefused``) -- recompute the whole dataset instead.

Keyed by catalog name, so a namespace keeps its own results. Kept outside
agent bundles: labels come from a model, not from an annotator.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .. import catalog, naming
from . import signature as _signature

SCHEMA = "levi.recap_value.revision.v1"
RESULT_SCHEMA = "levi.recap_value.result.v1"
LEGACY, MODELS = "revisions", "models"
LAYOUTS = (LEGACY, MODELS)
# How long a replaced version stays readable for a reader that resolved it
# just before the switch.
RETIRE_GRACE_SECONDS = 900.0
RETIRED = "RETIRED"
MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"  # = checkpoints.NAME_PATTERN

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


class MergeRefused(ValueError):
    """A subset recomputation whose parameters differ from the stored result."""


def root(name: str) -> Path:
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("Invalid dataset name")
    return catalog.STATE / "recap_values" / name


def layout() -> str:
    """The layout new results are written in (``LEVI_RECAP_STORE_LAYOUT``)."""
    value = os.getenv("LEVI_RECAP_STORE_LAYOUT", MODELS) or MODELS
    if value not in LAYOUTS:
        raise ValueError("LEVI_RECAP_STORE_LAYOUT is revisions or models")
    return value


_configured_layout = layout  # ``publish`` takes a ``layout`` argument


def is_model_name(value: str | None) -> bool:
    return bool(value) and bool(re.fullmatch(MODEL_PATTERN, value))


def write_json(path: Path, value: Any) -> None:
    catalog.atomic(path, value)


def write_table(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    pq.write_table(table, temp)
    os.replace(temp, path)


def _fsync_dir(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_table(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    with temp.open("wb") as handle:
        pq.write_table(table, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _durable_json(path: Path, value: Any) -> None:
    """Survives power loss: the bytes reach the disk before the rename, and
    the rename before the call returns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    with temp.open("w") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _fsync_dir(path.parent)


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


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- locating


@dataclass(frozen=True)
class Located:
    """One published result at one version."""

    ref: str  # checkpoint name (models) or revision id (revisions)
    layout: str
    version: str  # the model result's version, or the revision id
    folder: Path

    @property
    def record_path(self) -> Path:
        name = "result.json" if self.layout == MODELS else "revision.json"
        return self.folder / name


def _slot(name: str, model: str) -> Path:
    if not is_model_name(model):
        raise ValueError(f"Invalid value model name {model!r}")
    return root(name) / MODELS / model


def head(name: str, model: str) -> str | None:
    """The live version of a model's result (only a completely written one)."""
    if not is_model_name(model):
        return None
    slot = _slot(name, model)
    version = (load_json(slot / "head.json") or {}).get("version")
    if (
        isinstance(version, str)
        and naming.is_timestamp_id(version)
        and (slot / "v" / version / "result.json").is_file()
    ):
        return version
    return None


def locate(
    name: str, ref: str | None = None, version: str | None = None
) -> Located | None:
    """A result by ref (default: the current one). A model result resolves
    to its head, or to ``version`` when given and still on disk (a reader
    pinned to it); a ref naming both a model and a revision means the model."""
    ref = ref or current_ref(name)
    if not ref:
        return None
    if is_model_name(ref):
        slot = root(name) / MODELS / ref
        chosen = version or head(name, ref)
        if (
            chosen
            and naming.is_timestamp_id(chosen)
            and (slot / "v" / chosen / "result.json").is_file()
        ):
            return Located(ref, MODELS, chosen, slot / "v" / chosen)
    if naming.is_timestamp_id(ref):
        folder = root(name) / LEGACY / ref
        if (folder / "revision.json").is_file() and version in (None, ref):
            return Located(ref, LEGACY, ref, folder)
    return None


def current_ref(name: str) -> str | None:
    """The result shown by default: ``current.json`` names a model or an
    original-layout revision."""
    value = catalog.read(root(name) / "current.json", {})
    model = value.get("model")
    if model and head(name, model):
        return model
    rid = value.get("revision_id")
    legacy = rid and naming.is_timestamp_id(rid)
    if legacy and (root(name) / LEGACY / rid / "revision.json").is_file():
        return rid
    return None


current_id = current_ref  # the original name, kept for callers


def _annotate(record: dict[str, Any], found: Located) -> dict[str, Any]:
    checkpoint = record.get("checkpoint") or {}
    return {
        **record,
        "revision_id": found.ref,
        "model": checkpoint.get("name"),
        "version": found.version,
        "layout": found.layout,
    }


def revision(
    name: str, rid: str | None = None, version: str | None = None
) -> dict[str, Any] | None:
    """A result's record, annotated with ``revision_id`` (its ref),
    ``model``, ``version`` and ``layout``."""
    if rid is not None and not (naming.is_timestamp_id(rid) or is_model_name(rid)):
        return None
    found = locate(name, rid, version)
    if not found:
        return None
    record = catalog.read(found.record_path, None)
    return _annotate(record, found) if isinstance(record, dict) else None


def summary(
    name: str, rid: str | None = None, version: str | None = None
) -> dict[str, Any] | None:
    found = locate(name, rid, version)
    if not found:
        return None
    value = catalog.read(found.folder / "summary.json", None)
    if not isinstance(value, dict):
        return None
    return {**value, "revision_id": found.ref, "version": found.version}


def read_episode(
    name: str, episode: int, rid: str | None = None, version: str | None = None
):
    found = locate(name, rid, version)
    if not found:
        return None
    path = found.folder / f"episode-{episode:06d}.parquet"
    try:
        return pq.read_table(path)
    except (FileNotFoundError, OSError):
        return None


def advantages_path(
    name: str, rid: str | None = None, version: str | None = None
) -> Path | None:
    found = locate(name, rid, version)
    return found.folder / "advantages.parquet" if found else None


def revisions(name: str) -> list[str]:
    """Original-layout revision ids, oldest first."""
    folder = root(name) / LEGACY
    if not folder.is_dir():
        return []
    return sorted(
        p.name
        for p in folder.iterdir()
        if naming.is_timestamp_id(p.name) and (p / "revision.json").is_file()
    )


def models(name: str) -> list[str]:
    """Value models with a live result on this dataset, by name."""
    folder = root(name) / MODELS
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if head(name, p.name))


def results(name: str) -> list[str]:
    """Every result ref: model results, then original-layout revisions."""
    return [*models(name), *revisions(name)]


# ---------------------------------------------------------------- writing


def _tables(ep: int, cols: dict[str, Any], dataset_name: str):
    """One episode's table, its rows of RLinf's advantages table and its
    summary row."""
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
    rlinf = pa.table(
        {
            "episode_index": pa.array(np.full(n, ep, dtype=np.int64)),
            "frame_index": pa.array(np.asarray(cols["frame_index"], np.int64)),
            "advantage_continuous": pa.array(np.asarray(cols["advantage"], np.float64)),
            "return": pa.array(np.asarray(cols["return"], np.float64)),
            "value_current": pa.array(np.asarray(cols["value"], np.float64)),
            "value_next": pa.array(np.asarray(cols["value_next"], np.float64)),
            "reward_sum": pa.array(np.asarray(cols["reward_sum"], np.float64)),
            "reward_sum_raw": pa.array(np.asarray(cols["reward_sum_raw"], np.float64)),
            "num_valid_rewards": pa.array(
                np.asarray(cols["num_valid_rewards"], np.int64)
            ),
            "dataset_name": pa.array([dataset_name] * n, pa.string()),
            "advantage": positive_column,
        },
        schema=RLINF_SCHEMA,
    )
    # A value-only result (no outcomes, so no returns) has no labels: its
    # fractions are null, never 0 (that would read as "all negative").
    positive = int(np.count_nonzero(cols["positive"])) if labelled else None
    row = {
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
        # The episode's length before the static filter (= frames without it).
        "episode_frames": int(cols.get("episode_frames", n)),
    }
    return table, rlinf, row


def _rlinf_table(parts: list[pa.Table], labelled: bool) -> pa.Table:
    """RLinf's advantages table; a values-only result leaves out the label
    column (a trainer reading it must not take a missing label for False)."""
    table = pa.concat_tables(parts) if parts else RLINF_SCHEMA.empty_table()
    if not labelled and "advantage" in table.column_names:
        table = table.drop(["advantage"])
    return table


def _positive_fraction(per_episode: dict[str, dict[str, Any]]) -> float | None:
    labelled = [r for r in per_episode.values() if r["positive_fraction"] is not None]
    if not labelled:
        return None
    frames = sum(r["frames"] for r in labelled)
    # Whole frame counts, as before (a fraction times a count is exact once rounded).
    positives = sum(round(r["positive_fraction"] * r["frames"]) for r in labelled)
    return positives / frames if frames else 0.0


def publish(
    name: str,
    episodes: dict[int, dict[str, np.ndarray]],
    meta: dict[str, Any],
    *,
    dataset_name: str,
    subset: bool = False,
    layout: str | None = None,
) -> dict[str, Any]:
    """Publish one computation and make it current, in ``layout`` (the one
    the job's plan recorded) or else the configured one.

    ``episodes`` maps episode index to per-frame arrays (frame_index,
    timestamp, value, value_next, reward_sum, reward_sum_raw, return,
    advantage, num_valid_rewards, positive -- ``None`` for values only).
    ``subset``: the run named its episodes, so a model result merges them
    into the stored one instead of replacing it."""
    chosen = layout or _configured_layout()
    if chosen not in LAYOUTS:
        raise ValueError(f"Unknown RECAP store layout {chosen!r}")
    if chosen == MODELS:
        return publish_model(
            name, episodes, meta, dataset_name=dataset_name, subset=subset
        )
    return _publish_revision(name, episodes, meta, dataset_name=dataset_name)


def _publish_revision(
    name: str,
    episodes: dict[int, dict[str, np.ndarray]],
    meta: dict[str, Any],
    *,
    dataset_name: str,
) -> dict[str, Any]:
    """The original layout: one complete revision, then ``current.json``.
    The revision directory is reserved first; ``revision.json`` is written
    last, so a directory without it is an interrupted publication and is
    never served."""
    folder = root(name) / LEGACY
    rid = naming.timestamp_id(folder, create_dir=True)
    target = folder / rid
    per_episode: dict[str, Any] = {}
    rlinf_parts = []
    for ep in sorted(episodes):
        table, rlinf, row = _tables(ep, episodes[ep], dataset_name)
        write_table(target / f"episode-{ep:06d}.parquet", table)
        rlinf_parts.append(rlinf)
        per_episode[str(ep)] = row
    write_table(
        target / "advantages.parquet",
        _rlinf_table(rlinf_parts, _positive_fraction(per_episode) is not None),
    )
    record = {
        "schema": SCHEMA,
        "revision_id": rid,
        "dataset": name,
        **meta,
        "episodes": len(episodes),
        "episode_indices": sorted(int(e) for e in episodes),
        "frames": sum(r["frames"] for r in per_episode.values()),
        "positive_fraction": _positive_fraction(per_episode),
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


def signature(record: dict[str, Any]) -> str:
    """The digest of the settings that decide a result's numbers
    (``levi/recap/signature.py``)."""
    return _signature.digest(record)


def signature_differences(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    return _signature.differences(old, new)


def _link(source: Path, target: Path) -> None:
    """Hard-link an unchanged episode file (copy across file systems)."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())


def publish_model(
    name: str,
    episodes: dict[int, dict[str, np.ndarray]],
    meta: dict[str, Any],
    *,
    dataset_name: str,
    subset: bool = False,
) -> dict[str, Any]:
    """Write a new version of the (dataset, value model) result and switch
    the head to it. A full computation replaces the result; a subset merges
    into it when ``signature`` matches, else ``MergeRefused``."""
    model = (meta.get("checkpoint") or {}).get("name")
    if not is_model_name(model):
        raise ValueError(f"Invalid value model name {model!r}")
    with locked(name):
        sweep(name)
        slot = _slot(name, model)
        old = locate(name, model)
        old_record = catalog.read(old.record_path, None) if old else None
        carried: list[int] = []
        if subset and old_record:
            differs = signature_differences(old_record, meta)
            if differs:
                raise MergeRefused(
                    f"The stored {model} result was computed with different "
                    f"parameters ({', '.join(differs)}); recompute the whole "
                    "dataset instead of a subset"
                )
            # An episode this run was asked for but skipped (e.g. by the
            # static filter) leaves the result with its reason; it is not
            # kept with values the request meant to replace.
            dropped = {int(k) for k in meta.get("skipped_episodes") or {}}
            carried = [
                int(e)
                for e in old_record.get("episode_indices") or []
                if int(e) not in episodes and int(e) not in dropped
            ]
        version = naming.timestamp_id(slot / "v", create_dir=True)
        target = slot / "v" / version
        now = time.time()
        old_summary = (
            (catalog.read(old.folder / "summary.json", {}) or {}) if old else {}
        )
        old_rows = old_summary.get("episodes") or {}
        per_episode: dict[str, Any] = {}
        parts: list[pa.Table] = []
        for ep in sorted(episodes):
            table, rlinf, row = _tables(ep, episodes[ep], dataset_name)
            _durable_table(target / f"episode-{ep:06d}.parquet", table)
            parts.append(rlinf)
            per_episode[str(ep)] = {
                **row,
                "computed_at": now,
                "job_id": meta.get("job_id"),
                "version": version,
            }
        if carried:
            for ep in carried:
                _link(
                    old.folder / f"episode-{ep:06d}.parquet",
                    target / f"episode-{ep:06d}.parquet",
                )
                per_episode[str(ep)] = {
                    "computed_at": old_record.get("created_at"),
                    "job_id": old_record.get("job_id"),
                    "version": old.version,
                    **old_rows.get(str(ep), {}),
                }
            previous = pq.read_table(old.folder / "advantages.parquet")
            parts.append(
                previous.filter(
                    pc.is_in(previous["episode_index"], pa.array(carried, pa.int64()))
                )
            )
        labelled = meta.get("dataset_type") != "value_only"
        if not labelled:
            parts = [
                p.drop(["advantage"]) if "advantage" in p.column_names else p
                for p in parts
            ]
        rlinf_table = _rlinf_table(parts, labelled)
        if parts:
            rlinf_table = rlinf_table.sort_by(
                [("episode_index", "ascending"), ("frame_index", "ascending")]
            )
        _durable_table(target / "advantages.parquet", rlinf_table)
        indices = sorted({*episodes, *carried})
        outcomes = {
            **{
                k: v
                for k, v in (old_record or {}).get("outcomes", {}).items()
                if int(k) in carried
            },
            **(meta.get("outcomes") or {}),
        }
        skipped = {
            k: v
            for k, v in {
                **((old_record or {}).get("skipped_episodes") or {}),
                **(meta.get("skipped_episodes") or {}),
            }.items()
            if int(k) not in indices
        }
        ordered = {str(ep): per_episode[str(ep)] for ep in indices}
        sources: dict[tuple, list[int]] = {}
        for ep in indices:
            row = ordered[str(ep)]
            sources.setdefault((row.get("version"), row.get("job_id")), []).append(ep)
        record = {
            "schema": RESULT_SCHEMA,
            "dataset": name,
            **meta,
            "static_filter": _merged_filter(meta.get("static_filter"), ordered),
            # Which computation each episode comes from.
            "merged_from": [
                {"version": v, "job_id": j, "episodes": eps}
                for (v, j), eps in sorted(sources.items(), key=lambda i: str(i[0][0]))
            ],
            "outcomes": outcomes,
            "skipped_episodes": skipped,
            "model": model,
            "version": version,
            "previous_version": old.version if old else None,
            "merged": bool(carried),
            "episodes": len(indices),
            "episode_indices": indices,
            "frames": sum(r["frames"] for r in ordered.values()),
            "positive_fraction": _positive_fraction(ordered),
            "created_at": now,
        }
        if carried:
            # The request describes only this run's episodes.
            record["last_request"] = record.pop("request", None)
        _durable_json(
            target / "summary.json",
            {
                "threshold": meta.get("threshold"),
                "threshold_source": meta.get("threshold_source"),
                "skipped_episodes": skipped,
                "episodes": ordered,
            },
        )
        _durable_json(target / "result.json", record)
        # The new directory entries must reach the disk before the head can
        # name them (power loss after the switch must not lose the version).
        for folder in (target, slot / "v", slot, slot.parent):
            _fsync_dir(folder)
        # The switch: one atomic rename. Before it the old version is live,
        # after it the new one; nothing in between is ever served.
        _durable_json(
            slot / "head.json",
            {"version": version, "previous": record["previous_version"], "at": now},
        )
        _durable_json(
            root(name) / "current.json", {"model": model, "updated_at": time.time()}
        )
        if old:
            _retire(old.folder)
        return _annotate(record, Located(model, MODELS, version, target))


def _merged_filter(
    filtering: dict[str, Any] | None, rows: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """The static filter record with its frame counts over every episode of
    the result (a merge would otherwise show the last subset's counts)."""
    if not filtering or not filtering.get("applied"):
        return filtering
    out = dict(filtering)
    lengths = [r.get("episode_frames") for r in rows.values()]
    kept = sum(int(r["frames"]) for r in rows.values())
    total = sum(lengths) if all(isinstance(n, int) for n in lengths) else None
    out.update(
        frames=total,
        kept_frames=kept,
        dropped_fraction=(1 - kept / total) if total else (0.0 if total == 0 else None),
    )
    return out


def _retire(folder: Path) -> None:
    try:
        _durable_json(folder / RETIRED, {"retired_at": time.time()})
    except OSError:
        pass


def sweep(name: str, *, now: float | None = None) -> dict[str, list[str]]:
    """Reclaim model-result versions nobody can be reading: replaced ones
    past the grace period, and unfinished ones a crash left (safe under the
    lock, which every publication holds). Returns what it removed and what
    it newly marked as replaced."""
    now = time.time() if now is None else now
    done: dict[str, list[str]] = {"removed": [], "retired": []}
    base = root(name) / MODELS
    if not base.is_dir():
        return done
    with locked(name):
        for slot in sorted(p for p in base.iterdir() if p.is_dir()):
            for stray in slot.glob(".head.json.*.tmp"):
                stray.unlink(missing_ok=True)
            live = head(name, slot.name) if is_model_name(slot.name) else None
            versions = slot / "v"
            if not versions.is_dir():
                continue
            for folder in sorted(p for p in versions.iterdir() if p.is_dir()):
                if folder.name == live:
                    continue
                label = f"{slot.name}/{folder.name}"
                if not (folder / "result.json").is_file():
                    shutil.rmtree(folder, ignore_errors=True)
                    done["removed"].append(label)
                    continue
                marker = load_json(folder / RETIRED)
                if not marker:
                    # Complete but not live: replaced by a publication that
                    # stopped before marking it. Start its grace now.
                    _retire(folder)
                    done["retired"].append(label)
                elif now - float(marker.get("retired_at") or 0) >= RETIRE_GRACE_SECONDS:
                    shutil.rmtree(folder, ignore_errors=True)
                    done["removed"].append(label)
    return done
