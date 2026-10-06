"""The reset analysis without an export: what would be reversed, and why not.

For the pool page and ``levi pool reset-analyze``. It reads each selected
episode where it lies (a raw capture's CSVs and videos, a LeRobot episode's
parquet and videos), runs the same analysis the export runs and returns one
verdict per episode with the measurements behind it. Nothing is written. A raw
capture is read at its own frame rate, every row, which is what an export
without the static filter converts; with the filter or a lower fps the export's
rows are a subset of these.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from ...conversion import pipeline, raw
from ...conversion.options import Options
from . import analysis, run
from . import contract as contract_mod
from .schema import ResetOptions


def _raw_evidence(row: dict, options: Options, camera: str) -> analysis.Evidence:
    demo = Path(row["key"])
    _pose, _grip, xyz, q, command = raw.load(demo)
    state, action = pipeline._state(xyz, q, command, options)
    video = None
    for source, feature in options.cameras.items():
        if feature == camera:
            video = raw.camera_path(demo, source)
    return analysis.Evidence(
        key=row["key"],
        state=state,
        action=action,
        width=run.raw_width(demo, np.arange(len(state))),
        release_video=video,
    )


def _lerobot_evidence(row: dict, camera_map: dict, camera: str) -> analysis.Evidence:
    import json

    root = Path(row["source_path"])
    info = json.loads((root / "meta/info.json").read_text())
    old = int(row["episode_index"])
    chunk = int(info.get("chunks_size") or 1000)
    table = pq.read_table(
        root / info["data_path"].format(episode_chunk=old // chunk, episode_index=old)
    )
    video = None
    for key, feature in info["features"].items():
        if feature.get("dtype") == "video" and camera_map.get(key, key) == camera:
            video = root / info["video_path"].format(
                episode_chunk=old // chunk, episode_index=old, video_key=key
            )
    rows = root / "meta/episodes.jsonl"
    already = False
    if rows.is_file():
        for line in rows.read_text().splitlines():
            if line.strip() and json.loads(line).get("episode_index") == old:
                already = "levi_reset" in json.loads(line)
    return analysis.Evidence(
        key=row["key"],
        state=np.asarray(table["observation.state"].to_pylist(), dtype=np.float32),
        action=np.asarray(table["action"].to_pylist(), dtype=np.float32),
        release_video=video,
        already_reset=already,
    )


def analyze_row(
    row: dict,
    opts: ResetOptions,
    conversion: Options,
    camera_map: dict | None = None,
    reviewer=None,
) -> dict:
    contract = contract_mod.resolve(opts.action_contract)
    verdict = {"key": row["key"], "task": row.get("task"), "source": row.get("source")}
    ok, why = (True, None)
    if opts.require_forward_success:
        ok, why = run.forward_ok(row)
    if not ok:
        return {**verdict, "eligible": False, "reason": why, "releases": []}
    try:
        if row["format"] == "robot_capture":
            ev = _raw_evidence(row, conversion, opts.release_camera)
        else:
            ev = _lerobot_evidence(row, camera_map or {}, opts.release_camera)
        found = analysis.analyze(ev, contract, opts, reviewer=reviewer)
    except (OSError, ValueError, KeyError) as exc:
        return {
            **verdict,
            "eligible": False,
            "reason": "reset_unreadable",
            "detail": str(exc)[:200],
            "releases": [],
        }
    record = found.record()
    return {
        **verdict,
        **{
            k: record[k]
            for k in ("eligible", "reason", "detail", "scope", "generation")
        },
        "releases": record["releases"],
        "rows": len(ev.state),
        "rows_kept": record["rows_kept"],
    }


def summarize(verdicts: list[dict]) -> dict:
    classes = Counter(r["class"] for v in verdicts for r in v["releases"])
    return {
        "episodes": len(verdicts),
        "reversible": sum(1 for v in verdicts if v["eligible"]),
        "reasons": dict(Counter(v["reason"] for v in verdicts if v["reason"])),
        "release_classes": dict(classes),
    }


def analyze_rows(
    rows: list[dict],
    opts: ResetOptions,
    conversion: Options,
    camera_map=None,
    workers=4,
    reviewer=None,
) -> dict:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        verdicts = list(
            pool.map(
                lambda r: analyze_row(r, opts, conversion, camera_map, reviewer), rows
            )
        )
    return {"summary": summarize(verdicts), "episodes": verdicts}
