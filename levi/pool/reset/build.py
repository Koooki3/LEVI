"""The reversed episode: rows, actions and every camera.

``write`` takes a forward episode (already in LeRobot form: a parquet and one
video per camera), the analysis of what may be reversed and, optionally, a
recorded stretch to put in front, and writes the reset episode into the export:
states and actions rebuilt (``contract``), each camera's frames decoded once,
put in the new order and encoded once, a temporal map saying which source frame
every output frame is, and the statistics the dataset format asks for.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ...conversion import dataset, media
from .. import journal as journal_mod
from . import contract as contract_mod


@dataclass
class Source:
    """One episode in LeRobot form: where its rows and videos are."""

    key: str
    root: Path
    info: dict
    old: int  # episode index in ``root``
    mapping: dict[str, str]  # video key in root -> output key
    state: np.ndarray = field(default=None)
    action: np.ndarray = field(default=None)

    @property
    def chunk(self) -> int:
        return int(self.info.get("chunks_size") or 1000)

    def data_path(self) -> Path:
        return self.root / self.info["data_path"].format(
            episode_chunk=self.old // self.chunk, episode_index=self.old
        )

    def video_path(self, src_key: str) -> Path:
        return self.root / self.info["video_path"].format(
            episode_chunk=self.old // self.chunk,
            episode_index=self.old,
            video_key=src_key,
        )

    def load(self) -> "Source":
        table = pq.read_table(self.data_path())
        self.state = np.asarray(
            table["observation.state"].to_pylist(), dtype=np.float32
        )
        self.action = np.asarray(table["action"].to_pylist(), dtype=np.float32)
        return self


def runs(sequence: list[tuple[str, int]]) -> list[dict]:
    """A temporal map as runs: ``{"source", "from", "to", "step"}`` with step
    +1 or -1 (a single frame has step 0), in output order."""
    out: list[dict] = []
    for source, row in sequence:
        if out and out[-1]["source"] == source:
            last = out[-1]
            step = row - last["to"]
            if last["step"] == 0 and step in (1, -1):
                last["step"] = step
                last["to"] = row
                continue
            if step == last["step"] and step != 0:
                last["to"] = row
                continue
        out.append({"source": source, "from": row, "to": row, "step": 0})
    return out


def write_video(journal, unit, staging, rel, parts, fps, expected):
    """Encode ``parts`` (``[(Path, rows)]``) to ``staging/rel`` unless the
    journal already holds a verified copy. Returns (probe, pixel stats)."""
    recorded = journal.done(unit)
    if (
        recorded
        and set(recorded["files"]) == {rel}
        and journal.verified(recorded["files"])
    ):
        return recorded["probe"], recorded["stats"]
    dst = staging / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    scratch = staging / ".reset-scratch"
    with ExitStack() as stack:
        stores = [
            (stack.enter_context(media.FrameStore(path, scratch)), rows)
            for path, rows in parts
        ]
        shapes = {s.shape for s, _ in stores}
        if len(shapes) != 1:
            raise ValueError(
                f"the parts of {rel} differ in resolution: {sorted(shapes)}"
            )
        media.encode(media.ordered_frames(stores), dst, fps, expected)
    info = media.inspect(dst, pixels=True)
    probe = media.probe(dst)
    journal.record(
        unit,
        files={rel: journal_mod.file_record(staging, rel)},
        probe=probe,
        stats=info["stats"],
    )
    return probe, info["stats"]


def sequence(
    analysis_keep: list[int], source_id: str, record_rows: int | None, record_id: str
):
    """Output order as (source id, row): the recording first (when there is
    one), then the kept source rows from last to first."""
    seq: list[tuple[str, int]] = []
    if record_rows:
        seq += [(record_id, i) for i in range(record_rows)]
    seq += [(source_id, r) for r in list(analysis_keep)[::-1]]
    return seq


def write(
    *,
    ctx,
    source: Source,
    keep: list[int],
    edits: dict[int, float],
    gripper: int,
    semantics: str,
    record: Source | None,
    record_rows: int | None,
    new: int,
    offset: int,
    task_index: int,
    fps: float,
    staging: Path,
    data_path: str,
    video_path: str,
) -> dict:
    """Write one reset episode as episode ``new``; the caller writes the
    metadata. Returns the pieces the metadata needs."""
    states = contract_mod.reversed_states(source.state, keep, edits, gripper)
    if record is not None:
        states = np.vstack([record.state[:record_rows].astype(np.float32), states])
    actions = contract_mod.reversed_actions(states, semantics)
    n = len(states)
    seq = sequence(keep, source.key, record_rows, record.key if record else "")
    assert len(seq) == n
    columns = {
        "timestamp": pa.array(np.arange(n, dtype=np.float32) / fps),
        "frame_index": pa.array(np.arange(n, dtype=np.int64)),
        "episode_index": pa.array(np.full(n, new, dtype=np.int64)),
        "index": pa.array(np.arange(offset, offset + n, dtype=np.int64)),
        "task_index": pa.array(np.full(n, task_index, dtype=np.int64)),
        "observation.state": pa.array(states.tolist(), type=pa.list_(pa.float32())),
        "action": pa.array(actions.tolist(), type=pa.list_(pa.float32())),
    }
    out = staging / data_path.format(episode_chunk=new // 1000, episode_index=new)
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(columns), out, compression="snappy")
    stats = {
        key: dataset.stats(
            np.asarray(columns[key].to_numpy(), dtype=np.float64).reshape(-1, 1)
        )
        for key in ("timestamp", "frame_index", "episode_index", "index", "task_index")
    }
    stats["observation.state"] = dataset.stats(states)
    stats["action"] = dataset.stats(actions)
    probes, features = {}, {}
    for src_key, out_key in source.mapping.items():
        parts = []
        if record is not None:
            parts.append(
                (record.video_path(_record_key(record, out_key)), range(record_rows))
            )
        parts.append((source.video_path(src_key), list(keep)[::-1]))
        rel = video_path.format(
            episode_chunk=new // 1000, episode_index=new, video_key=out_key
        )
        unit = f"reset|{source.key}|{out_key}"
        probe, pixel_stats = write_video(ctx.journal, unit, staging, rel, parts, fps, n)
        probes[rel] = probe
        stats[out_key] = pixel_stats
        features[out_key] = (src_key, probe)
    return {
        "rows": n,
        "stats": stats,
        "probes": probes,
        "features": features,
        "temporal_map": runs(seq),
        "states": states,
    }


def _record_key(record: Source, out_key: str) -> str:
    for src_key, mapped in record.mapping.items():
        if mapped == out_key:
            return src_key
    raise KeyError(f"the recorded episode has no camera {out_key}")
