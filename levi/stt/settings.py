"""What the speech worker is run with.

Four environment settings, as for the segmentation worker (a site-file key
would turn every existing site file into a 412):

- ``LEVI_STT_WORKER_PYTHON``: the worker environment's Python (default
  ``integrations/stt/.venv/bin/python``);
- ``LEVI_STT_MODEL_DIR``: the folder that holds the model folders (default
  ``<workspace>/checkpoints/stt``, confined to the workspace);
- ``LEVI_STT_MODEL``: the model folder's name (default ``qwen3-asr-1.7b-hf``).
  The name ``fake`` selects the worker's ``fake`` provider: LEVI's own Python,
  no model and no GPU (tests and demonstrations).
- ``LEVI_STT_TRANSLATE_MODEL``: the folder (in the same model folder) of the
  model that translates Chinese object names for the voice card's generated
  sentence (default ``qwen3-4b-instruct-2507``); ``off`` turns translation
  off. A folder that is not installed also leaves it off. With the ``fake``
  speech model, any name other than ``off`` selects the fake translator.

Everything else is a constant. ``worker_state`` only looks at files: it never
imports the model stack, and it names no path.
"""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import paths

DEFAULT_MODEL = "qwen3-asr-1.7b-hf"
FAKE_MODEL = "fake"
DEFAULT_TRANSLATE_MODEL = "qwen3-4b-instruct-2507"
# The text models whose prompts were evaluated (the worker's TRANSLATION_MODELS).
TRANSLATE_MODELS = ("qwen3-4b-instruct-2507",)
TRANSLATE_OFF = "off"
MANIFEST = "MANIFEST.sha256"
DEVICE = "cuda"
MAX_SECONDS = 10
# 16 kHz mono 16-bit: 32000 bytes a second, plus the 44-byte header, with a
# little room over ten seconds.
MAX_BYTES = 400_000
IDLE_SECONDS = 120
# A transcription that has to start the worker takes up to START_TIMEOUT_S +
# REQUEST_TIMEOUT_S on the server; the page gives up at 75 s
# (SPEECH_DEADLINES.transcribe in src/components/marvel0/speech-api.ts), and the
# server must answer ``speech_timeout`` first or the page's abort leaves the
# request lock and a loading worker behind. The measured cold start is 1 s.
START_TIMEOUT_S = 45
REQUEST_TIMEOUT_S = 20
# The free GPU memory a cold start needs besides the site's margin (measured
# peak of the 1.7B model: 4878 MiB).
STT_VRAM_MIB = 5000
# The free GPU memory loading the translation model needs besides the site's
# margin (measured peak of Qwen3-4B-Instruct-2507 in bf16: 9359 MiB, 2026-10-31).
TRANSLATE_VRAM_MIB = 9500
# A translate request answers up to three short questions per name (at most
# four names of at most 16 characters, a body of at most 1024 bytes: the
# worker's own limits in integrations/stt/levi_stt_worker/common.py). Measured
# 2026-11-01 on the RTX 5090 next to a training job: 0.75 s a name; the first
# request also loads the text model, 5.0 s (3.5 s of it the manifest's sha256
# of 7.6 GB).
TRANSLATE_TIMEOUT_S = 20
TRANSLATE_MAX_NAMES = 4
TRANSLATE_MAX_CHARS = 16
TRANSLATE_MAX_BYTES = 1024
# The page gives up on a transcription at 75 s and on a match at 15 s
# (SPEECH_DEADLINES in src/components/marvel0/speech-api.ts). A translation
# after either gets only what is left of these budgets (at most
# TRANSLATE_TIMEOUT_S); a worker that misses it is stopped, which takes up to
# 2 * GRACE_S (4 s) more, and the budgets leave room for that, so the server
# answers before the page stops waiting and the request lock is free again.
# With less than TRANSLATE_MIN_S left no translation is tried; one is begun
# only with TRANSLATE_NAME_S a name left, plus TRANSLATE_LOAD_S for the load.
TRANSCRIBE_BUDGET_S = 68
MATCH_BUDGET_S = 10
TRANSLATE_MIN_S = 3
TRANSLATE_LOAD_S = 5.5
TRANSLATE_NAME_S = 1.0
RATE_PER_MINUTE = 10
OMP_THREADS = 2

WORKER_MODULE = "levi_stt_worker.cli"
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def worker_project() -> Path:
    return paths.PROJECT / "integrations" / "stt"


def worker_python() -> Path:
    default = worker_project() / ".venv" / "bin" / "python"
    return Path(os.getenv("LEVI_STT_WORKER_PYTHON") or default).expanduser()


def model_dir() -> Path:
    """The folder of model folders; ``ValueError`` outside the workspace."""
    configured = os.getenv("LEVI_STT_MODEL_DIR")
    return paths.inside(configured or paths.CHECKPOINTS / "stt")


def model_name() -> str:
    return os.getenv("LEVI_STT_MODEL") or DEFAULT_MODEL


def provider() -> str:
    return "fake" if model_name() == FAKE_MODEL else "qwen3-asr"


@dataclass(frozen=True)
class WorkerState:
    """Whether the worker and its model are there. ``reason`` is a code, never
    a path."""

    worker: bool
    model: bool
    reason: str | None = None


def worker_state() -> WorkerState:
    """Look at the files only: the worker source, its Python, the model folder
    and its manifest. The worker checks the manifest's hashes itself when it
    loads the model."""
    if not (worker_project() / "levi_stt_worker" / "cli.py").is_file():
        return WorkerState(False, False, "worker_source_missing")
    fake = provider() == "fake"
    if not fake and not worker_python().is_file():
        return WorkerState(False, False, "worker_python_missing")
    if fake:
        return WorkerState(True, True)
    name = model_name()
    if not _MODEL_NAME.fullmatch(name) or ".." in name:
        return WorkerState(True, False, "model_name_invalid")
    try:
        folder = model_dir() / name
    except ValueError:
        return WorkerState(True, False, "model_dir_outside_workspace")
    if not (folder / MANIFEST).is_file():
        return WorkerState(True, False, "model_missing")
    return WorkerState(True, True)


def translate_model_name() -> str:
    return os.getenv("LEVI_STT_TRANSLATE_MODEL") or DEFAULT_TRANSLATE_MODEL


def translation_state() -> str:
    """``ready`` (installed and on), ``off`` (turned off) or a reason code it is
    not available: ``model_name_invalid``, ``model_dir_outside_workspace``,
    ``model_missing``. Files only, like ``worker_state``."""
    name = translate_model_name()
    if name == TRANSLATE_OFF:
        return "off"
    if name not in TRANSLATE_MODELS:
        return "model_name_invalid"
    if provider() == "fake":
        return "ready"
    try:
        folder = model_dir() / name
    except ValueError:
        return "model_dir_outside_workspace"
    if not (folder / MANIFEST).is_file():
        return "model_missing"
    return "ready"


def command(cpu_affinity: str) -> list[str]:
    """The worker's command line (``cpu_affinity`` is the site's
    ``unit.cpu_affinity``; empty leaves the worker unpinned)."""
    fake = provider() == "fake"
    python = sys.executable if fake else str(worker_python())
    return [
        python,
        "-m",
        WORKER_MODULE,
        "serve",
        "--model-dir",
        str(model_dir()),
        "--model",
        model_name(),
        "--device",
        DEVICE,
        "--max-seconds",
        str(MAX_SECONDS),
        "--cpu-affinity",
        cpu_affinity,
        *(
            ["--translate-model", translate_model_name()]
            if translation_state() == "ready"
            else []
        ),
        "--provider",
        provider(),
    ]
