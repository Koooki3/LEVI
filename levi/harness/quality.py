"""Dataset quality inspection shared by the Workbench, REST and MCP.

One implementation (``levi.diagnostics.diagnose``) behind every entry point;
this module only decides where the report is kept. Reports are analysis
exports, so they carry an hour stamp and sit with the dataset they describe:
``outputs/LEVI/datasets/<name>/reports/quality-<YYYYmmddTHH>.json``.
"""

import time
from pathlib import Path

from levi.diagnostics import CHECKS, diagnose

from .layout import report_path, write_json


def inspect(
    state: Path,
    key: str,
    root: Path,
    *,
    repo_id: str,
    checks=None,
    max_episodes=0,
    decode_video=False,
):
    if set(checks or ()) - set(CHECKS):
        raise ValueError("Unknown diagnostic check")
    started = time.monotonic()
    report = diagnose(root, max_episodes, checks or None, decode_video)
    report["repo_id"] = repo_id
    report["dataset_key"] = key
    report["seconds"] = round(time.monotonic() - started, 2)
    report["created_at"] = time.time()
    path = write_json(report_path(state, key, "quality"), report)
    report["path"] = str(path)
    return report


def digest(report):
    """What an agent needs to act on, without the passing rows."""
    return {
        "status": report["status"],
        "counts": report["counts"],
        "episodes": report["episodes"],
        "frames": report["frames"],
        "fps": report["fps"],
        "scope": report["scope"],
        "findings": [r for r in report["results"] if r["status"] != "pass"],
        "flagged_episodes": report["flagged_episodes"],
        "seconds": report.get("seconds"),
        "path": report.get("path"),
        "method": report["method"],
    }
