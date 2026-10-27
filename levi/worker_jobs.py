"""Helpers shared by the background worker job modules (recap, segmentation)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

LOG_TAIL_BYTES = 4000


def log_tail(job: dict[str, Any]) -> str | None:
    """The last ``LOG_TAIL_BYTES`` of a job's log with credentials redacted."""
    try:
        data = Path(str(job.get("log_path"))).read_bytes()
    except OSError:
        return None
    if not data:
        return None
    tail = data[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
    if len(data) > LOG_TAIL_BYTES:
        tail = "...(truncated)...\n" + tail
    return re.sub(r"(?i)(token|authorization)=[^&\s]+", r"\1=<redacted>", tail).strip()
