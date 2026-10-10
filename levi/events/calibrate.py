"""Calibrate the change-point penalty on development reference annotations.

    python -m levi.events.calibrate --root <dir> --gold <dir> [--gold <dir> ...]
        [--report-gold <dir> ...] [--map OLD=NEW ...] [--exclude-source TEXT ...]
        [--penalties 0.5,1,2,4] [--out report.json]

Reads, never writes (except ``--out``):

- ``--gold``: folders of reference annotations, one JSON per episode, each
  with ``source`` (a raw robot capture folder relative to ``--root``),
  ``frames`` and ``segments`` (``start_n``/``end_n`` rows). A folder with a
  ``MANIFEST.json`` contributes only the episodes it lists (a locked set).
  The penalty is chosen on these: the largest one whose F1 at the 0.5 s
  tolerance is within ``SLACK`` of the best (of near-equal settings, the one
  that cuts least).
- ``--report-gold``: folders scored with the chosen penalty but never used to
  choose it -- the number to report.
- the capture's ``end_effector_pose.csv`` and ``gripper_state.csv`` (through
  ``levi.conversion.raw.load``, which checks their alignment); never video.

A folder whose name contains a ``--refuse`` pattern (by default ``frozen``,
``heldout`` and ``held-out``) is refused outright: test sets are not for
calibration. ``--exclude-source`` skips episodes whose source path contains
the text. ``--map OLD=NEW`` rewrites a source path prefix (a copy of the
capture somewhere else).

The reference boundaries are the segments' inner boundaries, at the
capture's recorded timestamps; candidates are ``change_points.candidates``
of the capture as an FR3-style table (x, y, z, roll, pitch, yaw, gripper
width with open = wide).
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

from . import boundary_metrics, change_points

REFUSE = ("frozen", "heldout", "held-out")
TOLERANCES = (0.2, 0.5, 1.0)
PENALTIES = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)
CHOOSE_AT = "0.5"
SLACK = 0.01
NAMES = ["x", "y", "z", "rx", "ry", "rz", "gripper"]
INFO = {
    "fps": 10,
    "features": {
        "observation.state": {"dtype": "float64", "shape": [7], "names": NAMES}
    },
}
PROFILE = {
    "channels": [
        {"role": "gripper", "feature": "observation.state", "index": 6,
         "open_level": "high", "units": "m"},
    ]
}  # fmt: skip


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def gold_files(folder, refuse=REFUSE):
    folder = Path(folder)
    lowered = folder.resolve().name.lower()
    if any(p in lowered for p in refuse) or any(
        p in part.lower() for part in folder.resolve().parts for p in refuse
    ):
        raise SystemExit(f"refused: {folder} looks like a test set ({refuse})")
    manifest = folder / "MANIFEST.json"
    if manifest.is_file():
        listed = json.loads(manifest.read_text()).get("episodes") or {}
        return [folder / f"{name}.json" for name in sorted(listed)]
    return sorted(p for p in folder.glob("*.json") if p.name != "MANIFEST.json")


def capture_table(demo):
    """``(table, times)`` of a raw capture: relative timestamps and an
    FR3-style state vector."""
    import pandas as pd

    from ..conversion import raw

    pose, grip, xyz, q, _ = raw.load(Path(demo))
    times = pose["timestamp_sec"].to_numpy(float)
    times = times - times[0]
    width = pd.to_numeric(grip["gripper_width"], errors="coerce").to_numpy(float)
    state = np.concatenate([xyz, raw.euler(q), width[:, None]], axis=1)
    return pd.DataFrame({"timestamp": times, "observation.state": list(state)}), times


def load_episode(path, root, mapping, exclude):
    """``(record, None)`` or ``(None, reason)``."""
    gold = json.loads(Path(path).read_text())
    source = gold.get("source") or ""
    for old, new in mapping:
        if source.startswith(old):
            source = new + source[len(old) :]
    if any(text in source for text in exclude):
        return None, "excluded source"
    demo = Path(root) / source
    if not demo.is_dir():
        return None, "source missing"
    try:
        table, times = capture_table(demo)
    except Exception as exc:  # noqa: BLE001 - one bad capture is a skipped row
        return None, f"capture unreadable: {exc}"
    if int(gold.get("frames") or -1) != len(times):
        return None, f"frames {gold.get('frames')} != rows {len(times)}"
    starts = sorted({int(s["start_n"]) for s in gold.get("segments") or []})
    rows = [r for r in starts if 0 < r < len(times)]
    return {
        "id": gold.get("episode") or Path(path).stem,
        "source": source,
        "sha256": {
            name: _sha256(demo / name)
            for name in ("end_effector_pose.csv", "gripper_state.csv")
        },
        "table": table,
        "times": times,
        "boundaries": [float(times[r]) for r in rows],
        "duration": float(times[-1] - times[0]),
    }, None


def score(episodes, penalty, **params):
    reference, candidates, durations = {}, {}, {}
    for i, ep in enumerate(episodes):
        found = change_points.candidates(
            ep["table"], INFO, _profile(), episode_index=i, penalty=penalty, **params
        )
        reference[i] = ep["boundaries"]
        candidates[i] = [c.center_time_s for c in found]
        durations[i] = ep["duration"]
    return boundary_metrics.boundary_scores(
        reference, candidates, durations, TOLERANCES
    )


def _profile():
    from .signal_profiles import resolve

    return resolve(INFO, PROFILE)


def collect(folders, root, mapping, exclude, refuse):
    episodes, skipped = [], []
    for folder in folders:
        for path in gold_files(folder, refuse):
            record, reason = load_episode(path, root, mapping, exclude)
            if record is None:
                skipped.append({"gold": str(path), "reason": reason})
            else:
                episodes.append(record)
    return episodes, skipped


def _summary(scores):
    at = scores["at"]
    return {
        "candidates_per_minute": scores["candidates_per_minute"],
        "nearest_mae": scores["nearest_mae"],
        "nearest_p90": scores["nearest_p90"],
        **{f"recall@{k}": at[k]["recall"] for k in at},
        **{f"f1@{k}": at[k]["f1"] for k in at},
        **{f"false_per_minute@{k}": at[k]["false_per_minute"] for k in at},
    }


def choose(sweep, penalties):
    """The largest penalty whose F1 at ``CHOOSE_AT`` is within ``SLACK`` of
    the best."""
    f1 = {p: sweep[f"{p:g}"][f"f1@{CHOOSE_AT}"] or 0.0 for p in penalties}
    best = max(f1.values())
    return max(p for p in penalties if f1[p] >= best - SLACK)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m levi.events.calibrate")
    parser.add_argument("--root", required=True, help="folder sources are relative to")
    parser.add_argument("--gold", action="append", required=True)
    parser.add_argument("--report-gold", action="append", default=[])
    parser.add_argument("--map", action="append", default=[])
    parser.add_argument("--exclude-source", action="append", default=[])
    parser.add_argument("--refuse", action="append", default=list(REFUSE))
    parser.add_argument("--penalties", default=",".join(f"{p:g}" for p in PENALTIES))
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    mapping = [tuple(m.split("=", 1)) for m in args.map]
    penalties = [float(p) for p in args.penalties.split(",") if p.strip()]
    calib, skipped = collect(
        args.gold, args.root, mapping, args.exclude_source, args.refuse
    )
    report, skipped_report = collect(
        args.report_gold, args.root, mapping, args.exclude_source, args.refuse
    )
    if not calib:
        raise SystemExit("no calibration episode could be read")
    sweep = {f"{p:g}": _summary(score(calib, p)) for p in penalties}
    chosen = choose(sweep, penalties)
    out = {
        "schema_version": "levi.change_point_calibration.v1",
        "chosen_penalty": chosen,
        "chosen_by": f"largest penalty with F1 at {CHOOSE_AT} s within {SLACK} "
        "of the best on the calibration set",
        "parameters": {
            "min_seconds": change_points.MIN_SECONDS,
            "max_per_minute": change_points.MAX_PER_MINUTE,
            "tolerances": list(TOLERANCES),
        },
        "calibration": {
            "folders": args.gold,
            "episodes": [
                {"id": e["id"], "source": e["source"], "sha256": e["sha256"]}
                for e in calib
            ],
            "skipped": skipped,
            "sweep": sweep,
        },
        "report": None,
    }
    if report:
        out["report"] = {
            "folders": args.report_gold,
            "episodes": [
                {"id": e["id"], "source": e["source"], "sha256": e["sha256"]}
                for e in report
            ],
            "skipped": skipped_report,
            "at_chosen": _summary(score(report, chosen)),
            "sweep": {f"{p:g}": _summary(score(report, p)) for p in penalties},
        }
    text = json.dumps(out, indent=1)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
