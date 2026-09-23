"""Read-only recognition of DROID raw trajectory and MP4 episode folders.

The browsing view is built by :mod:`levi.droid_view`; the ordinary robot
capture converter assumes pose/gripper CSVs and must never be used here.
"""

import json
from pathlib import Path

import numpy as np

from ..episodes import SourceEpisode
from ..report import EpisodeFinding, InputReport, Requirement
from .base import InputFormat

CAMERAS = {
    "wrist": ("wrist_mp4_path", "observation.images.wrist"),
    "exterior_1": ("ext1_mp4_path", "observation.images.exterior_1"),
    "exterior_2": ("ext2_mp4_path", "observation.images.exterior_2"),
}


def demo_dirs(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("demo_*") if path.is_dir())


def metadata(demo: Path) -> dict:
    paths = list(demo.glob("metadata_*.json"))
    if len(paths) != 1:
        raise ValueError(f"{demo.name}: expected exactly one metadata JSON")
    value = json.loads(paths[0].read_text())
    if not isinstance(value, dict):
        raise TypeError(f"{demo.name}: metadata must be an object")
    return value


def _validate_metadata(meta: dict) -> tuple[int, str, str]:
    length = meta.get("trajectory_length")
    task = meta.get("current_task")
    success = meta.get("success")
    if type(length) is not int or length < 2:
        raise ValueError("trajectory_length must be an integer of at least 2")
    if task is not None and not isinstance(task, str):
        raise ValueError("current_task must be text or absent")
    if type(success) is not bool:
        raise ValueError("success must be an explicit boolean")
    # DROID episodes without a language instruction remain useful for
    # evidence-led annotation. Keep the absence explicit instead of inferring
    # a task from the episode-level success flag.
    return (
        length,
        (task or "").strip() or "Unspecified task",
        "success" if success else "failure",
    )


def _validate_trajectory(demo: Path, expected: int) -> None:
    import h5py

    with h5py.File(demo / "trajectory.h5", "r") as handle:
        keys = {
            "observation/timestamp/control/step_start": (),
            "observation/robot_state/joint_positions": (7,),
            "observation/robot_state/gripper_position": (),
            "action/joint_position": (7,),
            "action/gripper_position": (),
        }
        for key, tail in keys.items():
            if key not in handle or handle[key].shape[1:] != tail:
                raise ValueError(f"Missing or malformed HDF5 field: {key}")
            if abs(handle[key].shape[0] - expected) > 3:
                raise ValueError(f"HDF5 length differs from metadata: {key}")
        timestamps = np.asarray(handle["observation/timestamp/control/step_start"][:])
        if not np.isfinite(timestamps).all() or np.any(np.diff(timestamps) <= 0):
            raise ValueError(
                "Control timestamps must be finite and strictly increasing"
            )


def camera_paths(demo: Path, meta: dict) -> dict[str, Path]:
    out = {}
    for name, (field, _) in CAMERAS.items():
        value = meta.get(field)
        if not isinstance(value, str) or not value.endswith(".mp4"):
            raise ValueError(f"{demo.name}: missing {field}")
        # Metadata contains remote source paths. Only its filename is trusted.
        path = demo / "recordings" / "MP4" / Path(value).name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"{demo.name}: missing or linked {name} MP4")
        out[name] = path
    if len(set(out.values())) != len(CAMERAS):
        raise ValueError(f"{demo.name}: duplicate camera files")
    return out


class DroidRaw(InputFormat):
    id = "droid_raw"
    label = "DROID raw (trajectory.h5 + three MP4 cameras)"
    description = (
        "Read-only DROID episode folders. LEVI builds an aligned browsing view; "
        "training-format conversion requires an explicit mapping."
    )
    evidence = "real-data"
    convertible = False
    viewable = True

    def detect(self, root: Path) -> float:
        for demo in demo_dirs(root)[:20]:
            if (demo / "trajectory.h5").is_file() and any(demo.glob("metadata_*.json")):
                return 1.0
        return 0.0

    def inspect(self, root, options, progress=None) -> InputReport:
        root = Path(root)
        demos = demo_dirs(root)
        report = InputReport(
            source=str(root),
            format=self.id,
            label=self.label,
            variant="three-camera raw",
            summary={"episodes": len(demos)},
        )
        if not demos:
            report.requirements.append(
                Requirement(
                    id="demos",
                    label="DROID episodes",
                    status="fail",
                    detail="No demo_* episode folders",
                    fix="Select the DROID raw root",
                )
            )
            return report
        try:
            import h5py  # noqa: F401
        except ImportError:
            report.requirements.append(
                Requirement(
                    id="h5py",
                    label="HDF5 reader",
                    status="fail",
                    detail="Optional h5py dependency is absent",
                    fix="uv sync --locked --extra droid",
                )
            )
        bad = 0
        missing_tasks = 0
        reader_available = not any(r.id == "h5py" for r in report.requirements)
        for demo in demos:
            finding = EpisodeFinding(source_id=demo.name)
            if not (demo / "trajectory.h5").is_file():
                finding.errors.append("Missing trajectory.h5")
            try:
                meta = metadata(demo)
                camera_paths(demo, meta)
                finding.frames, _, finding.outcome = _validate_metadata(meta)
                if not (meta.get("current_task") or "").strip():
                    finding.warnings.append(
                        "No task text; annotate from video evidence"
                    )
                    missing_tasks += 1
                if reader_available and (demo / "trajectory.h5").is_file():
                    _validate_trajectory(demo, finding.frames)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                finding.errors.append(str(exc))
            bad += bool(finding.errors)
            report.episodes.append(finding)
        report.requirements.append(
            Requirement(
                id="episodes",
                label="HDF5, metadata and three MP4s",
                status="fail" if bad else "pass",
                detail=f"{bad}/{len(demos)} incomplete episodes" if bad else "",
                fix="Repair or exclude incomplete source episodes" if bad else "",
            )
        )
        report.requirements.append(
            Requirement(
                id="task_text",
                label="Task text",
                status="warn" if missing_tasks else "pass",
                detail=f"{missing_tasks}/{len(demos)} episodes have no task text"
                if missing_tasks
                else "",
                fix="Use video evidence; do not infer a subtask from episode outcome"
                if missing_tasks
                else "",
            )
        )
        report.summary["complete_episodes"] = len(demos) - bad
        return report

    def episodes(self, root, options) -> list[SourceEpisode]:
        out = []
        for demo in demo_dirs(Path(root)):
            if demo.name in options.exclude_demos:
                continue
            meta = metadata(demo)
            frames, task, outcome = _validate_metadata(meta)
            cameras = camera_paths(demo, meta)
            out.append(
                SourceEpisode(
                    source_id=demo.name,
                    path=str(demo),
                    task=task,
                    outcome=outcome,
                    frames=frames,
                    cameras={key: str(path) for key, path in cameras.items()},
                    metadata={
                        "lab": meta.get("building"),
                        "raw_format": "droid",
                        "task_unspecified": not (
                            meta.get("current_task") or ""
                        ).strip(),
                    },
                )
            )
        return out
