"""LeRobot v2.1: per-episode parquet + per-camera MP4, the target every other
LeRobot-based output builds on."""

from ..options import Options
from ..report import InputReport, Solution, TargetCompatibility
from .base import OutputFormat


class LeRobotV21(OutputFormat):
    id = "lerobot_v21"
    label = "LeRobot v2.1 dataset"
    description = (
        "Per-episode parquet (state, action, timestamps) and H.264 videos, "
        "loadable by LeRobot, openpi and the LEVI viewer."
    )
    evidence = "real-data"
    inputs = ("robot_capture", "image_sequence")
    ignored_warnings = ("outcomes",)

    def check(self, report: InputReport, options: Options) -> TargetCompatibility:
        reasons, solutions = [], []
        if report.format not in self.inputs:
            return TargetCompatibility(
                target=self.id,
                label=self.label,
                status="unsupported",
                reasons=[
                    f"{report.label} is already a finished dataset; "
                    "LEVI converts raw captures into LeRobot v2.1"
                    if report.format == "lerobot"
                    else f"No converter from {report.label} to {self.label}"
                ],
            )
        failed = report.failed
        for item in failed:
            reasons.append(f"{item.label}: {item.detail}".rstrip(": "))
        bad = [e.source_id for e in report.episodes if e.errors]
        if bad and len(bad) < len(report.episodes):
            solutions.append(
                Solution(
                    id="exclude_failed",
                    label=f"Exclude the {len(bad)} failing episode(s)",
                    options={
                        "exclude_demos": sorted(set(options.exclude_demos) | set(bad))
                    },
                )
            )
        # Outcome labels only matter to targets that train on them.
        warned = [r for r in report.warned if r.id not in self.ignored_warnings]
        if failed:
            status = "unsupported"
        elif warned:
            status = "warnings"
            reasons = [f"{r.label}: {r.detail}".rstrip(": ") for r in warned]
        else:
            status = "supported"
        return TargetCompatibility(
            target=self.id,
            label=self.label,
            status=status,
            reasons=reasons,
            solutions=solutions,
            defaults=self.defaults,
        )
