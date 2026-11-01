"""Constants shared by the serve loop, the gates and the installer.

Standard library only, so the ``fake`` provider and the protocol tests run
with LEVI's own Python (no Torch, no NumPy). Every threshold here was
measured on the 2026-10-20 benchmark and is recorded in the voice design
record (levi-hub programs/rig-console/reviews/voice-design-20261020.md).
"""

from __future__ import annotations

import re
import sys
from typing import Any, TextIO

# ---- audio contract (design record 5.5) -------------------------------------
SAMPLE_RATE = 16000
WAV_HEADER_BYTES = 44
MIN_SECONDS = 0.3
# The browser records at most 10 s and adds a 200 ms tail; the worker accepts
# 0.5 s over the configured limit and no more.
TAIL_ALLOWANCE_S = 0.5
MAX_REQUEST_BYTES = 400_000
MAX_HEADER_BYTES = 1024

# ---- gates (design record 5.2) ----------------------------------------------
VAD_THRESHOLD = 0.35
MIN_SPEECH_S = 0.25
CONTEXT_S = 1.0
MIN_AVG_LOGPROB = -0.5
MAX_NEW_TOKENS = 64
# Language names exactly as the model writes them after "language ".
LANGUAGES = {"english": "English", "chinese": "Chinese"}
REASONS = ("no_speech", "language", "low_confidence", "empty")

# ---- models -----------------------------------------------------------------
# folder name -> (Hugging Face repo, pinned commit, sha256 of model.safetensors
# (the LFS object id of that commit)). The install step refuses a download
# whose weights differ from the pinned hash.
MODELS: dict[str, tuple[str, str, str]] = {
    "qwen3-asr-1.7b-hf": (
        "Qwen/Qwen3-ASR-1.7B-hf",
        "bcd2b5b7f32b480ab5790554cfa8347f246a14f3",
        "2db53c7d81bd9b8cbc6a074e89be2c968a0d373fb4ee68bb1b1e14f7042dfee1",
    ),
    "qwen3-asr-0.6b-hf": (
        "Qwen/Qwen3-ASR-0.6B-hf",
        "7f1569a48a89f3e3f4dc3a5c9d28bddd903bc76c",
        "ef4efceba30de680fbe5647923a57acc91d4ed202dd40a69c73e4a018b7e5f41",
    ),
}
DEFAULT_MODEL = "qwen3-asr-1.7b-hf"
MANIFEST_NAME = "MANIFEST.sha256"

# ---- object-name translation (voice input, generated sentences) ---------------
# folder name -> (Hugging Face repo, pinned commit, {weight file: sha256 (LFS
# object id of that commit)}). Chosen on 2026-10-31 from four local candidates on
# 81 Chinese object names plus a frozen held-out set of 40 (levi-hub
# programs/rig-console/reviews/voice-translate-20261031.md): 31 of the 40
# held-out names passed the checks, none of them wrongly.
TRANSLATION_MODELS: dict[str, tuple[str, str, dict[str, str]]] = {
    # Default since 2026-11-01: as good as the 4B on the evaluation (69/81 new
    # names, 0 wrong) in 4.4 GB instead of 9.4 GB. Loaded text-only
    # (AutoModelForCausalLM): the vision tower's weights are not read.
    "qwen3.5-2b": (
        "Qwen/Qwen3.5-2B",
        "15852e8c16360a2fea060d615a32b45270f8a8fc",
        {
            "model.safetensors-00001-of-00001.safetensors": "aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1",
            # Hugging Face's LFS etag of the pinned commit (a sha256).
            "tokenizer.json": "5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42",
        },
    ),
    "qwen3-4b-instruct-2507": (
        "Qwen/Qwen3-4B-Instruct-2507",
        "cdbee75f17c01a7cc42f958dc650907174af0554",
        {
            "model-00001-of-00003.safetensors": "75311d91bb08cf0b882913da464a1e722a31fb44db35208663487efb7a3d8ed6",
            "model-00002-of-00003.safetensors": "0b48adbb1f60e901153d91907ba11ce63bd4b8b584482e730f48808d055dfba1",
            "model-00003-of-00003.safetensors": "7dd39ccca5e4de123c74c14af44c9bf2eb75df33b4614382af0134528e060d5d",
            # Hugging Face's LFS etag of the pinned commit (a sha256).
            "tokenizer.json": "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4",
        },
    ),
}
DEFAULT_TRANSLATION_MODEL = "qwen3.5-2b"
TRANSLATE_MAX_NAMES = 4  # object names in one request (a spoken task has two per step)
TRANSLATE_MAX_CHARS = 16  # characters of one Chinese name
TRANSLATE_MAX_BYTES = 1024  # the request body (UTF-8 JSON)
TRANSLATE_MAX_NEW_TOKENS = 24  # one short JSON answer
TRANSLATE_ANSWER_CHARS = 64  # longest answer field passed back

REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# Exit codes of ``serve`` (0 is a clean EOF or shutdown).
EXIT_PROTOCOL = 2
EXIT_MANIFEST = 3
EXIT_STARTUP = 4
EXIT_CRASH = 5


def log(message: str, stream: TextIO | None = None) -> None:
    """One line on stderr. Callers pass hashes, ids and sizes, never a
    transcript or a path (the stream is captured by the LEVI service)."""
    out = stream if stream is not None else sys.stderr
    print(f"levi-stt-worker: {message}", file=out, flush=True)


def clean_text(value: Any) -> str:
    """Whitespace-normalised text: the transcript never carries a newline."""
    return " ".join(str(value).split())
