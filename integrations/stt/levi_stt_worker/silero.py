"""Silero VAD on the CPU (silero-vad 6.2.3, the TorchScript model that ships
inside the wheel; nothing is downloaded)."""

from __future__ import annotations

import numpy as np

from .common import SAMPLE_RATE, VAD_THRESHOLD


class SileroDetector:
    def __init__(self, threshold: float = VAD_THRESHOLD) -> None:
        from silero_vad import get_speech_timestamps, load_silero_vad

        self._timestamps = get_speech_timestamps
        self._model = load_silero_vad()
        self._threshold = threshold

    def segments(self, samples: np.ndarray) -> list[tuple[int, int]]:
        import torch

        with torch.inference_mode():
            spans = self._timestamps(
                torch.from_numpy(samples),
                self._model,
                threshold=self._threshold,
                sampling_rate=SAMPLE_RATE,
            )
        return [(int(span["start"]), int(span["end"])) for span in spans]
