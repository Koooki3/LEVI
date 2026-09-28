"""Run one LEVI plan: per-frame V(o_t) for every planned episode.

Writes ``<output stem>.values.parquet`` (episode_index, frame_index, value)
and then ``<output>`` (status, provenance) — the result JSON last, so its
presence means the values are complete. A failure is reported in the result
JSON as well as in the log.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False))
    os.replace(temp, path)


class Progress:
    def __init__(self, path: Path | None, total: int):
        self.path = path
        self.total = total
        self.done = 0
        self.stage = "loading"
        self.write()

    def write(self) -> None:
        if self.path is not None:
            write_json(
                self.path, {"stage": self.stage, "done": self.done, "total": self.total}
            )

    def values(self, done: int) -> None:
        self.stage = "values"
        self.done = done
        self.write()


def run_plan(plan_path: Path, output: Path, progress_path: Path | None) -> int:
    started = time.time()
    try:
        plan = json.loads(Path(plan_path).read_text())
        total = int(
            sum(len(e.get("keep", ())) or e["length"] for e in plan["episodes"])
        )
        progress = Progress(progress_path, total)
        provider = plan["provider"]
        if provider == "fake":
            from .fake import values as compute
        elif provider == "rlinf":
            from .rlinf_provider import values as compute
        else:
            raise ValueError(f"unknown provider {provider!r}")
        episodes, provenance = compute(plan, progress)
        tables = [
            pa.table(
                {
                    "episode_index": pa.array(np.full(len(v), ep, dtype=np.int64)),
                    "frame_index": pa.array(np.asarray(f, dtype=np.int64)),
                    "value": pa.array(np.asarray(v, dtype=np.float32)),
                }
            )
            for ep, (f, v) in sorted(episodes.items())
        ]
        values_file = output.with_name(f"{output.stem}.values.parquet")
        temp = values_file.with_name(f".{values_file.name}.{time.time_ns()}.tmp")
        values_file.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.concat_tables(tables), temp)
        os.replace(temp, values_file)
        progress.values(total)
        write_json(
            output,
            {
                "status": "succeeded",
                "provider": provider,
                "values_file": values_file.name,
                "episodes": {str(ep): len(v) for ep, (_f, v) in episodes.items()},
                "provenance": {
                    **provenance,
                    "elapsed_seconds": round(time.time() - started, 3),
                },
            },
        )
        return 0
    except Exception as exc:  # noqa: BLE001 -- any failure is reported to LEVI
        traceback.print_exc()
        write_json(
            output,
            {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000]},
        )
        return 1
