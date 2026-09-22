"""Where harness artifacts live (plan §10.2), named after the dataset they describe.

Every path is derived from the workbench directory the caller already uses, so
a test workspace and the real one never mix. Names follow the global rule:
per-dataset files carry the dataset name, per-run files the run id, and a
report may add an hour stamp -- nothing else, and never a hash.
"""

import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _checked(value: str) -> str:
    if not NAME.fullmatch(value or "") or ".." in value:
        raise ValueError(f"Not a readable artifact name: {value!r}")
    return value


def outputs(state: Path) -> Path:
    """``outputs/LEVI`` for a workbench directory ``outputs/LEVI/workbench``."""
    return Path(state).parent


def dataset_dir(state: Path, key: str) -> Path:
    return outputs(state) / "datasets" / _checked(key)


def task_dir(state: Path, key: str, run_id: str) -> Path:
    return dataset_dir(state, key) / "tasks" / _checked(run_id)


def reports_dir(state: Path, key: str) -> Path:
    return dataset_dir(state, key) / "reports"


def memory_path(state: Path, key: str) -> Path:
    return Path(state) / "memory" / f"{_checked(key)}.json"


def improvements_dir(state: Path, key: str) -> Path:
    return Path(state) / "improvements" / _checked(key)


def hour_stamp(at: float | None = None) -> str:
    """``YYYYmmddTHH`` in UTC, the same clock and spelling as run ids."""
    moment = datetime.fromtimestamp(at, UTC) if at else datetime.now(UTC)
    return moment.strftime("%Y%m%dT%H")


def report_path(state: Path, key: str, kind: str, at: float | None = None) -> Path:
    """``<dataset>/reports/<kind>-<YYYYmmddTHH>.json``; a re-run within the
    same hour replaces that hour's report instead of adding a second one."""
    return reports_dir(state, key) / f"{_checked(kind)}-{hour_stamp(at)}.json"


def harness_lock(state: Path, key: str):
    """Serialise read-modify-write of one dataset's memory and candidates.

    Not reentrant (flock on a fresh handle): take it once, at the outermost
    caller -- closing a run, refreshing a ledger, one capability call.
    """
    from levi.agent.store import dataset_lock

    return dataset_lock(Path(state), f"harness-{_checked(key)}")


def write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per writer, so two processes replacing the same report cannot share
    # (and truncate) one temporary file. Transient; never left behind.
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}-{threading.get_ident()}.partial"
    )
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)
    return path


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
