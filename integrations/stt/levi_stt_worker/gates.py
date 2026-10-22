"""The gate pipeline: VAD -> only speech to the model -> output gates.

Detector and recogniser are injected (``silero.py`` and ``qwen.py`` provide
the real ones), so the decision logic is tested without Torch. The gates are
the 2026-10-20 benchmark's pipeline P6: on 12 non-speech clips only a 6-talker
babble got through, and no genuine command was dropped (thresholds chosen on
the same clips, so in-sample; every transcript still needs a person's OK).
A clip with less than 0.25 s of speech never reaches the model.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .common import (
    CONTEXT_S,
    LANGUAGES,
    MIN_AVG_LOGPROB,
    MIN_SPEECH_S,
    SAMPLE_RATE,
    clean_text,
)


@dataclass(frozen=True)
class Recognition:
    """What the model said. ``language`` is the model's own tag (None for the
    "language None" non-speech answer); ``avg_logprob`` is the mean token
    log-probability of the transcription text; ``capped`` means generation
    stopped at the token limit instead of at an end token."""

    language: str | None
    text: str
    avg_logprob: float | None
    capped: bool = False


class SpeechDetector(Protocol):
    def segments(self, samples: np.ndarray) -> list[tuple[int, int]]:
        """Speech spans as (start, end) sample indices, ascending."""


class Recogniser(Protocol):
    def recognise(self, samples: np.ndarray) -> Recognition: ...


def speech_audio(samples: np.ndarray, spans: list[tuple[int, int]]) -> np.ndarray:
    """The detected speech with ``CONTEXT_S`` of audio around it. Feeding only
    the VAD spans cut real words (on 54 benchmark clips: 44 exact with spans
    alone, 50 with one second of context, 50 with the whole clip), because
    the VAD starts late on quiet or reverberant onsets; the context keeps
    those words while a long silent or noisy lead-in or tail is still not
    sent to the model."""
    pad = round(CONTEXT_S * SAMPLE_RATE)
    return samples[max(0, spans[0][0] - pad) : spans[-1][1] + pad]


def run_gates(
    samples: np.ndarray,
    detector: SpeechDetector,
    recogniser: Recogniser,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """The ``result`` body (without ``event`` and ``id``) for one clip."""
    started = clock()
    spans = detector.segments(samples)
    speech_s = sum(end - start for start, end in spans) / SAMPLE_RATE
    vad_ms = round((clock() - started) * 1000)
    if speech_s < MIN_SPEECH_S:
        return {"heard": False, "reason": "no_speech"}
    started = clock()
    heard = recogniser.recognise(speech_audio(samples, spans))
    infer_ms = round((clock() - started) * 1000)
    text = clean_text(heard.text)
    if heard.language is None or not text:
        return {"heard": False, "reason": "empty"}
    language = LANGUAGES.get(heard.language.strip().casefold())
    if language is None:
        return {"heard": False, "reason": "language"}
    score = heard.avg_logprob
    if (
        heard.capped
        or score is None
        or not math.isfinite(score)
        or score < MIN_AVG_LOGPROB
    ):
        return {"heard": False, "reason": "low_confidence"}
    return {
        "heard": True,
        "language": language,
        "text": text,
        "avg_logprob": round(score, 4) + 0.0,
        "speech_s": round(speech_s, 2),
        "timing_ms": {"vad": vad_ms, "infer": infer_ms},
    }
