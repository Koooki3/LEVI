"""The ``fake`` provider: no Torch, no NumPy, no GPU.

Answers come from the JSON file named by ``LEVI_STT_FAKE_SCRIPT`` and are
consumed in order, one per ``transcribe`` request. The file is a list of
entries, or an object ``{"answers": [...], "load_delay_s": 0, "ready":
{"load_s": 0.1, "vram_mib": 4700}, "startup_error": {"code": "...",
"message": "..."}}``. An entry is one of

* ``{"heard": true, "language": "English", "text": "...", "avg_logprob": -0.1,
  "speech_s": 1.8}`` (the last two default to -0.1 and the clip length),
* ``{"heard": false, "reason": "no_speech|language|low_confidence|empty"}``,
* ``{"error": {"code": "internal", "message": "..."}}``,
* ``{"crash": true}``: the process dies before answering,

and may carry ``"delay_s"`` to answer late (timeout tests). The WAV check and
all protocol errors are the real ones: only the recognition is scripted.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .common import EXIT_CRASH, EXIT_STARTUP, REASONS
from .errors import RequestError, StartupError
from .wav import WavClip

SCRIPT_ENV = "LEVI_STT_FAKE_SCRIPT"


def _load_script() -> dict[str, Any]:
    path = os.environ.get(SCRIPT_ENV, "").strip()
    if not path:
        return {"answers": []}
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise StartupError(
            "internal", f"{SCRIPT_ENV} cannot be read as JSON.", EXIT_STARTUP
        ) from None
    script = {"answers": value} if isinstance(value, list) else value
    if not isinstance(script, dict) or not isinstance(script.get("answers", []), list):
        raise StartupError(
            "internal",
            f"{SCRIPT_ENV} must be a list or an object with answers.",
            EXIT_STARTUP,
        )
    return script


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self._script = _load_script()
        self._answers: list[dict[str, Any]] = list(self._script.get("answers", []))

    def load(self) -> dict[str, Any]:
        failure = self._script.get("startup_error")
        if isinstance(failure, dict):
            raise StartupError(
                str(failure.get("code", "internal")),
                str(failure.get("message", "scripted failure")),
                EXIT_STARTUP,
            )
        time.sleep(float(self._script.get("load_delay_s", 0)))
        return dict(self._script.get("ready", {}))

    def transcribe(self, request_id: str, clip: WavClip) -> dict[str, Any]:
        if not self._answers:
            raise RequestError("internal", "The fake script has no answer left.")
        entry = self._answers.pop(0)
        if entry.get("crash"):
            os._exit(EXIT_CRASH)
        time.sleep(float(entry.get("delay_s", 0)))
        failure = entry.get("error")
        if isinstance(failure, dict):
            raise RequestError(
                str(failure.get("code", "internal")),
                str(failure.get("message", "scripted failure")),
            )
        if not entry.get("heard"):
            reason = entry.get("reason", "no_speech")
            return {
                "heard": False,
                "reason": reason if reason in REASONS else "no_speech",
            }
        return {
            "heard": True,
            "language": entry.get("language", "English"),
            "text": str(entry.get("text", "")),
            "avg_logprob": float(entry.get("avg_logprob", -0.1)),
            "speech_s": float(entry.get("speech_s", round(clip.seconds, 2))),
            "timing_ms": entry.get("timing_ms", {"vad": 1, "infer": 1}),
        }

    def close(self) -> None:
        pass
