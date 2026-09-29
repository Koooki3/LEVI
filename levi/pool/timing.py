"""How an export's frame rate meets the sources' measured rates.

``retime`` keeps every row and declares the rows at the export fps, so a
source recorded at ``source_fps`` gets a time axis ``source_fps / fps`` as
long as the recording (time-scale factor; 0.95 = 5% shorter). ``resample``
only drops rows and never adds any: a raw capture measured below the export
fps cannot be converted at it, and the export is refused. See
docs/TRAINING_POOL.md ("Frame rates").
"""

import math
from typing import Literal

Timing = Literal["resample", "retime"]
# The pipeline lowers resample's target when fps > slowest camera + this.
TOLERANCE = 0.01
# Retime differences smaller than this (fraction) are not worth a note.
NOTE_FRACTION = 0.02
FORMATS_WITH_TIMING = ("lerobot_v21", "recap_value")


def has_timing(format: str | None) -> bool:
    return format in FORMATS_WITH_TIMING


def default_timing(format: str | None) -> Timing | None:
    """The format's own default; ``None`` where timing means nothing (a raw
    capture copy has no time axis to declare)."""
    if not has_timing(format):
        return None
    return "retime" if format == "recap_value" else "resample"


def time_scale(source_fps: float | None, fps: float, timing: str | None):
    """Exported duration divided by recorded duration, or None if unknown.
    1.0 for ``resample`` (rows are picked by time, so the axis is true)."""
    if timing != "retime":
        return 1.0
    if not source_fps or source_fps <= 0:
        return None
    return source_fps / fps


def rounded(value: float | None) -> float | None:
    """Six decimals: a probed rate is 9.437066…, no need to keep all of it."""
    return None if value is None else round(float(value), 6)


def _measured(row: dict) -> float | None:
    value = row.get("measured_fps")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def warnings(chosen: list[dict], fps: float, timing: str | None) -> list[dict]:
    """Notes about the chosen raw captures at this export fps and timing.
    Episodes without a measured rate (an older index, or metadata without
    one) are not judged."""
    if timing not in ("resample", "retime"):
        return []
    rates = [
        (row, rate)
        for row in chosen
        if row.get("format") == "robot_capture" and (rate := _measured(row))
    ]
    out: list[dict] = []
    if timing == "resample":
        below = [(r, v) for r, v in rates if v < fps - TOLERANCE]
        if below:
            slowest = min(v for _, v in below)
            suggested = max(1, math.floor(slowest + 1e-9))
            out.append(
                {
                    "code": "source_fps_below_export",
                    "blocking": False,
                    "refused": True,
                    "message": f"{len(below)} raw episode(s) were recorded below "
                    f"the export fps ({slowest:g} < {fps:g}); resample cannot add "
                    f"frames, so the export is refused. Lower the fps to "
                    f"{suggested}, or use retime",
                    "episodes": len(below),
                    "sources": sorted({r["source"] for r, _ in below})[:10],
                    "export_fps": fps,
                    "min_source_fps": slowest,
                    "suggested_fps": suggested,
                    "suggested_timing": "retime",
                }
            )
    else:
        scales = [(r, v / fps) for r, v in rates]
        off = [(r, s) for r, s in scales if abs(s - 1) > NOTE_FRACTION]
        if off:
            worst = max((s for _, s in off), key=lambda s: abs(s - 1))
            percent = round(abs(1 - worst) * 100, 1)
            out.append(
                {
                    "code": "retime_time_scale",
                    "blocking": False,
                    "level": "info",
                    "message": f"{len(off)} raw episode(s) get a time axis up to "
                    f"{percent:g}% {'shorter' if worst < 1 else 'longer'} than "
                    "recorded (retime declares every frame at the export fps)",
                    "episodes": len(off),
                    "export_fps": fps,
                    "max_deviation_percent": percent,
                    "direction": "shorter" if worst < 1 else "longer",
                    "min_source_fps": min(v for _, v in rates),
                    "max_source_fps": max(v for _, v in rates),
                }
            )
    return out
