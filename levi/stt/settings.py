"""What the speech worker is run with.

Three environment settings, as for the segmentation worker (a site-file key
would turn every existing site file into a 412):

- ``LEVI_STT_WORKER_PYTHON``: the worker environment's Python (default
  ``integrations/stt/.venv/bin/python``);
- ``LEVI_STT_MODEL_DIR``: the folder that holds the model folders (default
  ``<workspace>/checkpoints/stt``, confined to the workspace);
- ``LEVI_STT_MODEL``: the model folder's name (default ``qwen3-asr-1.7b-hf``).
  The name ``fake`` selects the worker's ``fake`` provider: LEVI's own Python,
  no model and no GPU (tests and demonstrations).

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
        "--provider",
        provider(),
    ]
