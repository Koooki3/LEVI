"""Every supported input and output format, and what connects them.

Adding a format = one module in ``inputs/`` or ``outputs/`` plus one line
here; the API, the web UI, the CLI and the contract tests pick it up.
"""

from pathlib import Path

from .inputs.base import InputFormat
from .inputs.image_sequence import ImageSequence
from .inputs.lerobot import LeRobotDataset
from .inputs.robot_capture import RobotCapture
from .options import Options
from .outputs.base import OutputFormat
from .outputs.lerobot_v21 import LeRobotV21
from .outputs.recap_value import RecapValue
from .report import InputReport

INPUT_FORMATS: dict[str, InputFormat] = {
    f.id: f for f in (RobotCapture(), ImageSequence(), LeRobotDataset())
}
OUTPUT_FORMATS: dict[str, OutputFormat] = {
    f.id: f for f in (LeRobotV21(), RecapValue())
}

# Formats users ask about that LEVI does not convert, and why — listed so the
# UI answers "can I…" instead of silently not offering it.
UNSUPPORTED = [
    {
        "id": "lerobot_v3_output",
        "label": "LeRobot v3.x dataset (output)",
        "reason": "Conversion writes v2.1 (the layout openpi and RLinf RECAP load). "
        "v3 datasets can be browsed and annotated.",
        "workaround": "Convert the v2.1 output with lerobot's "
        "convert_dataset_v21_to_v30 script.",
    },
    {
        "id": "lerobot_v3_input",
        "label": "LeRobot v3.x dataset (input)",
        "reason": "Re-export reads per-episode parquet/MP4 files; v3 packs "
        "episodes into shared files.",
        "workaround": "Export from the raw capture, or convert v3 back to v2.1 first.",
    },
    {
        "id": "rlds",
        "label": "RLDS / Open X-Embodiment",
        "reason": "No reader or writer yet.",
        "workaround": "Convert to LeRobot with lerobot's port scripts, then use LEVI.",
    },
    {
        "id": "hdf5",
        "label": "HDF5 (robomimic / ALOHA)",
        "reason": "No reader yet.",
        "workaround": "Add an InputFormat in levi/conversion/inputs/ (see docs/CONVERSION.md).",
    },
]


def detect(root: Path) -> InputFormat | None:
    scores = [(f.detect(root), f) for f in INPUT_FORMATS.values()]
    score, best = max(scores, key=lambda item: item[0])
    return best if score > 0 else None


def output_format(target: str) -> OutputFormat:
    if target not in OUTPUT_FORMATS:
        raise ValueError(f"Unknown output format: {target}")
    return OUTPUT_FORMATS[target]


def with_defaults(
    options: Options, target: str, explicit: set[str] = frozenset()
) -> Options:
    """Apply a target's default options except those the caller set."""
    defaults = {
        k: v for k, v in output_format(target).defaults.items() if k not in explicit
    }
    return options.model_copy(update={**defaults, "target": target})


def inspect(root: Path, options: Options, progress=None) -> InputReport:
    """Detect, inspect and rate every output target for ``root``."""
    fmt = detect(root)
    if fmt is None:
        report = InputReport(source=str(root))
        report.summary = {
            "hint": "Expected task/demo_NNNN capture folders or a LeRobot dataset "
            "(meta/info.json)."
        }
    else:
        report = fmt.inspect(root, options, progress)
    # Each target is rated at its own defaults (e.g. RECAP keeps every
    # step); the UI shows those defaults on the card and lets them be changed.
    report.targets = [
        out.check(report, with_defaults(options, out.id))
        if report.format
        else _unsupported(out, "Input format not recognized")
        for out in OUTPUT_FORMATS.values()
    ]
    return report


def _unsupported(out: OutputFormat, reason: str):
    from .report import TargetCompatibility

    return TargetCompatibility(
        target=out.id, label=out.label, status="unsupported", reasons=[reason]
    )


def export(
    source: Path, target: Path, options: Options, progress_path=None, intermediate=None
):
    """Run one conversion: the capture pipeline or a dataset re-export."""
    from . import pipeline
    from .progress import Progress

    fmt = detect(source)
    if fmt is None:
        raise ValueError("Input format not recognized")
    out = output_format(options.target)
    if fmt.id not in out.inputs:
        raise ValueError(f"{out.label} cannot be produced from {fmt.label}")
    if not fmt.convertible:
        report = fmt.inspect(source, options)
        if report.failed:
            raise ValueError(
                "Preflight failed: "
                + "; ".join(f"{r.label}: {r.detail}" for r in report.failed)
            )
        return out.from_dataset(
            source, target, options, Progress(progress_path, pipeline.STAGES)
        )
    return pipeline.run(source, target, options, fmt, out, progress_path, intermediate)


def capabilities() -> dict:
    return {
        "inputs": [f.describe() for f in INPUT_FORMATS.values()],
        "outputs": [f.describe() for f in OUTPUT_FORMATS.values()],
        "unsupported": UNSUPPORTED,
        "matrix": {
            i: [o.id for o in OUTPUT_FORMATS.values() if i in o.inputs]
            for i in INPUT_FORMATS
        },
    }
