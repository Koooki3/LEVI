"""An existing LeRobot dataset as a conversion input.

v2.x can be re-exported (e.g. as a RECAP value dataset: same frames, new
reward/label columns). v3.x is recognized so the UI can explain why it is
not convertible yet, instead of calling it an unknown folder.
"""

import json
from collections import Counter
from pathlib import Path

from ...versions import is_dataset_v2
from ..dataset import read_jsonl
from ..options import Options
from ..report import EpisodeFinding, InputReport, Requirement
from .base import InputFormat


def read_info(root: Path) -> dict | None:
    try:
        return json.loads((root / "meta/info.json").read_text())
    except (OSError, ValueError):
        return None


def episode_outcomes(
    root: Path, overrides: dict[int, str] | None = None
) -> dict[int, str | None]:
    """Episode index → outcome: human label (``overrides``) over
    ``levi_outcome`` metadata; ``None`` when neither exists."""
    rows = read_jsonl(root / "meta/episodes.jsonl")
    overrides = overrides or {}
    return {
        row["episode_index"]: overrides.get(
            row["episode_index"], row.get("levi_outcome")
        )
        for row in rows
    }


class LeRobotDataset(InputFormat):
    id = "lerobot"
    label = "LeRobot dataset"
    description = "A converted LeRobot dataset (meta/info.json, parquet, videos)."
    evidence = "real-data"
    # Not a raw capture: the capture pipeline cannot read it; outputs that
    # accept it ("from_dataset") rewrite its tables and reuse its videos.
    convertible = False
    viewable = False

    def detect(self, root: Path) -> float:
        return 1.0 if read_info(root) else 0.0

    def inspect(self, root: Path, options: Options, progress=None) -> InputReport:
        root = Path(root)
        info = read_info(root) or {}
        version = info.get("codebase_version", "?")
        report = InputReport(
            source=str(root), format=self.id, label=self.label, variant=version
        )
        reqs = []

        def put(rid, label, status, detail="", fix=""):
            reqs.append(
                Requirement(
                    id=rid,
                    label=label,
                    status=status,
                    detail=detail,
                    fix=fix if status in ("fail", "warn") else "",
                )
            )

        v2 = is_dataset_v2(version)
        put(
            "version",
            "LeRobot v2.x layout (per-episode files)",
            "pass" if v2 else "fail",
            f"codebase_version {version}",
            "LeRobot v3 datasets can be browsed and annotated, but not re-exported yet: "
            "convert with lerobot's v3→v2.1 script, or export from the raw capture.",
        )
        if not v2:
            report.requirements = reqs
            return report
        try:
            rows = read_jsonl(root / "meta/episodes.jsonl")
            tasks = read_jsonl(root / "meta/tasks.jsonl")
        except (OSError, ValueError) as exc:
            put("metadata", "episodes.jsonl and tasks.jsonl readable", "fail", str(exc))
            report.requirements = reqs
            return report
        put(
            "metadata",
            "episodes.jsonl and tasks.jsonl readable",
            "pass",
            f"{len(rows)} episodes, {len(tasks)} task(s)",
        )
        missing = [
            r["episode_index"]
            for r in rows
            if not (
                root
                / info["data_path"].format(
                    episode_chunk=r["episode_index"] // info["chunks_size"],
                    episode_index=r["episode_index"],
                )
            ).is_file()
        ]
        put(
            "data_files",
            "Parquet file for every episode",
            "fail" if missing else "pass",
            f"missing for episodes {missing[:5]}" if missing else "",
        )
        conversion = {}
        path = root / "meta/levi_conversion.json"
        if path.exists():
            conversion = json.loads(path.read_text())
        opts = conversion.get("options", {})
        dropped = (
            opts.get("filter_static", False)
            or conversion.get("timing", "resample") == "resample"
        )
        if conversion:
            put(
                "frame_selection",
                "Every captured step kept (no filtering or resampling)",
                "warn" if dropped else "pass",
                "Converted with static-frame filtering and/or FPS resampling: rows are not "
                "one per executed action"
                if dropped
                else "",
                "For RECAP, re-export from the raw capture with timing=retime and filtering off.",
            )
        else:
            put(
                "frame_selection",
                "Every captured step kept (no filtering or resampling)",
                "info",
                "Not converted by LEVI; frame selection unknown",
            )
        labels = episode_outcomes(
            root, {int(k): v for k, v in options.outcome_labels.items()}
        )
        counts = Counter(v or "unlabeled" for v in labels.values())
        unlabeled = counts.get("unlabeled", 0)
        put(
            "outcomes",
            "Per-episode success/failure labels",
            "warn" if unlabeled else "pass",
            f"{counts.get('success', 0)} success / {counts.get('failure', 0)} failure"
            + (f" / {unlabeled} unlabeled" if unlabeled else ""),
            "Label outcomes in the LEVI viewer, or export as demonstrations (SFT).",
        )
        report.summary = {
            "demos": len(rows),
            "tasks": dict(Counter(t for r in rows for t in r.get("tasks", []))),
            "frames": int(sum(r.get("length", 0) for r in rows)),
            "outcomes": dict(counts),
            "fps_min": info.get("fps"),
            "fps_max": info.get("fps"),
            "output_fps": info.get("fps"),
            "cameras": [
                k
                for k, f in info.get("features", {}).items()
                if f.get("dtype") == "video"
            ],
            "timing": conversion.get("timing"),
        }
        report.episodes = [
            EpisodeFinding(
                source_id=str(r["episode_index"]),
                frames=r.get("length"),
                outcome=labels.get(r["episode_index"]),
            )
            for r in rows
        ]
        report.requirements = reqs
        return report

    def episodes(self, root: Path, options: Options):
        raise ValueError(
            "LeRobot datasets are exported with from_dataset, not the capture pipeline"
        )
