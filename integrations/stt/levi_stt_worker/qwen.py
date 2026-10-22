"""Qwen3-ASR (transformers-native ``-hf`` checkpoints) behind the Silero gate.

Recipe from the 2026-10-20 benchmark: bf16, sdpa attention, language
auto-detected by the model ("language English<asr_text>..."), greedy decoding
capped at 64 new tokens, no prompt and no hotwords (a prompt is parroted back
under a forced language and, under auto, made 9 of 38 genuine commands come
back as "language None"). Weights load from the verified local folder only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from .common import EXIT_STARTUP, MAX_NEW_TOKENS, SAMPLE_RATE
from .errors import StartupError
from .gates import Recognition, run_gates
from .silero import SileroDetector
from .wav import WavClip

THREADS = 2


def own_vram_mib() -> int | None:
    """GPU memory of this process as nvidia-smi reports it (CUDA context
    included), or None when nvidia-smi cannot say. Only our own row is read."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 2 and parts[0] == str(os.getpid()) and parts[1].isdigit():
            return int(parts[1])
    return None


class QwenAsrProvider:
    name = "qwen3-asr"

    def __init__(self, model_path: Path, device: str) -> None:
        self._path = model_path
        self._device = device
        self._model: Any = None
        self._processor: Any = None
        self._detector: SileroDetector | None = None
        self._stop_ids: set[int] = set()
        self._asr_text_id = -1
        self._real_device = ""

    # ------------------------------------------------------------------ load
    def load(self) -> dict[str, Any]:
        # The warm-up and the memory probe are part of loading: an out-of-memory
        # error or a kernel failure there must end as a startup error (event
        # and exit code), not as a traceback the service reports as a crash.
        try:
            self._load()
            self.recognise(
                _warmup_audio()
            )  # first-call kernel setup happens here, not on the first press
            vram = own_vram_mib()
        except StartupError:
            raise
        except Exception as error:  # noqa: BLE001 - reported as a startup error, never a traceback
            raise StartupError(
                "internal",
                f"The speech model could not be loaded ({type(error).__name__}).",
                EXIT_STARTUP,
            ) from None
        # The device is read from the weights, not echoed from the option, so the
        # manager's device check sees where the model really sits.
        return {
            "device": self._real_device,
            "vram_mib": vram if vram is not None else 0,
        }

    def _load(self) -> None:
        import torch

        if self._device != "cuda" or not torch.cuda.is_available():
            raise StartupError(
                "internal", "CUDA is not available to the speech worker.", EXIT_STARTUP
            )
        torch.set_num_threads(THREADS)
        from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration
        from transformers.utils import logging as hf_logging

        hf_logging.disable_progress_bar()  # stderr is captured by the service; no tqdm bars
        hf_logging.set_verbosity_error()
        self._processor = AutoProcessor.from_pretrained(
            self._path, local_files_only=True
        )
        self._model = Qwen3ASRForConditionalGeneration.from_pretrained(
            self._path,
            dtype=torch.bfloat16,
            device_map=self._device,
            attn_implementation="sdpa",
            local_files_only=True,
            use_safetensors=True,
        ).eval()
        # device_map is a request: a changed map or an offloaded layer would leave
        # (part of) a 1.7B model on the CPU cores the arm controller shares.
        self._real_device = model_device(self._model)
        if self._real_device != "cuda":
            raise StartupError(
                "internal",
                f"The speech model is not fully on the GPU (device: {self._real_device}).",
                EXIT_STARTUP,
            )
        tokenizer = self._processor.tokenizer
        self._asr_text_id = tokenizer.convert_tokens_to_ids("<asr_text>")
        eos = self._model.generation_config.eos_token_id
        self._stop_ids = {eos} if isinstance(eos, int) else set(eos or ())
        if tokenizer.eos_token_id is not None:
            self._stop_ids.add(tokenizer.eos_token_id)
        if self._asr_text_id in (None, tokenizer.unk_token_id) or not self._stop_ids:
            raise StartupError(
                "internal",
                "The speech model's tokenizer is not the expected one.",
                EXIT_STARTUP,
            )
        self._detector = SileroDetector()

    # ------------------------------------------------------------- requests
    def transcribe(self, request_id: str, clip: WavClip) -> dict[str, Any]:
        samples = np.frombuffer(clip.pcm, dtype="<i2").astype(np.float32) / 32768.0
        assert self._detector is not None  # load() ran before the first request
        return run_gates(samples, self._detector, self)

    def recognise(self, samples: np.ndarray) -> Recognition:
        import torch

        inputs = self._processor.apply_transcription_request(
            audio=samples, language=None
        )
        inputs = inputs.to(self._model.device, self._model.dtype)
        with torch.inference_mode():
            out = self._model.generate(
                **inputs,
                max_new_tokens=MAX_NEW_TOKENS,
                do_sample=False,
                output_scores=True,
                return_dict_in_generate=True,
            )
            start = inputs["input_ids"].shape[1]
            new = out.sequences[0, start:].tolist()
            token_scores = self._model.compute_transition_scores(
                out.sequences, out.scores, normalize_logits=True
            )[0].tolist()
        parsed = self._processor.decode(
            until_stop(new, self._stop_ids), return_format="parsed"
        )
        return Recognition(
            parsed["language"],
            parsed["transcription"],
            text_logprob(new, token_scores, self._asr_text_id, self._stop_ids),
            hit_token_limit(new, self._stop_ids, MAX_NEW_TOKENS),
        )

    def close(self) -> None:
        import gc

        self._model = None
        self._processor = None
        self._detector = None
        gc.collect()
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001, S110 - best effort; exit frees the GPU anyway
            pass


def model_device(model: Any) -> str:
    """Where the weights really are: the device type every parameter shares,
    "mixed" when they are split (for example offloaded layers), "none" when
    the model has no parameters."""
    kinds = {parameter.device.type for parameter in model.parameters()}
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed" if kinds else "none"


def until_stop(tokens: list[int], stop_ids: set[int]) -> list[int]:
    """The generated tokens before the first end token."""
    for position, token in enumerate(tokens):
        if token in stop_ids:
            return tokens[:position]
    return tokens


def text_logprob(
    tokens: list[int], scores: list[float], asr_text_id: int, stop_ids: set[int]
) -> float | None:
    """Mean log-probability of the transcription tokens only: those after the
    ``<asr_text>`` tag (not the language tag before it) and before the first
    end token. None when there are none."""
    begin = tokens.index(asr_text_id) + 1 if asr_text_id in tokens else 0
    picked = scores[begin : len(until_stop(tokens, stop_ids))]
    return float(np.mean(picked)) if picked else None


def hit_token_limit(tokens: list[int], stop_ids: set[int], limit: int) -> bool:
    """Generation ran to the token limit without an end token: the text is cut
    off (a 10 s command needs about 33 tokens), so it is not trusted."""
    return len(tokens) >= limit and not stop_ids.intersection(tokens)


def _warmup_audio() -> np.ndarray:
    return (
        np.random.default_rng(0).normal(0.0, 0.02, 2 * SAMPLE_RATE).astype(np.float32)
    )
