"""The speech worker with the real model on the GPU.

Skipped unless it is asked for and everything exists: ``LEVI_STT_REAL_TEST=1``,
the worker environment (``integrations/stt/.venv`` or
``LEVI_STT_WORKER_PYTHON``), the installed model folder
(``LEVI_STT_MODEL_DIR``, default ``<workspace>/checkpoints/stt``) and a CUDA
GPU. It uses about 5 GB of GPU memory, so run it through the resource driver
(``driver.py run --resource gpu -- pytest tests/stt/test_worker_real.py``) and
never while a policy server or learner needs the card.

Optional ``LEVI_STT_TEST_AUDIO``: a folder with ``en1.wav`` (16 kHz mono,
"pick the eggplant in the blue plate") to check a real transcription.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
from conftest import scaled

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "integrations" / "stt"
MODEL = "qwen3-asr-1.7b-hf"


def worker_python() -> Path | None:
    configured = os.getenv("LEVI_STT_WORKER_PYTHON")
    candidate = Path(configured) if configured else WORKER / ".venv/bin/python"
    return candidate if candidate.exists() else None


def model_dir() -> Path:
    configured = os.getenv("LEVI_STT_MODEL_DIR")
    return (
        Path(configured)
        if configured
        else Path(os.getenv("LEVI_WORKSPACE") or ROOT / ".state") / "checkpoints/stt"
    )


def cuda_available() -> bool:
    python = worker_python()
    if python is None or shutil.which("nvidia-smi") is None:
        return False
    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)",
        ],
        capture_output=True,
        check=False,
        timeout=120,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    os.getenv("LEVI_STT_REAL_TEST") != "1"
    or worker_python() is None
    or not (model_dir() / MODEL / "MANIFEST.sha256").exists()
    or not cuda_available(),
    reason="set LEVI_STT_REAL_TEST=1 with the worker environment, the installed model and a CUDA GPU",
)


def wav_bytes(samples: np.ndarray) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def request(request_id: str, payload: bytes) -> bytes:
    return (
        json.dumps(
            {"op": "transcribe", "id": request_id, "bytes": len(payload)}
        ).encode()
        + b"\n"
        + payload
    )


def run_worker(stdin: bytes, timeout: float = 180) -> tuple[int, list[dict], str]:
    env = {
        **os.environ,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": "2",
    }
    env["LEVI_GPU_LOCK_FILE"] = ""
    done = subprocess.run(
        [
            str(worker_python()),
            "-m",
            "levi_stt_worker.cli",
            "serve",
            "--model-dir",
            str(model_dir()),
            "--model",
            MODEL,
            "--device",
            "cuda",
            "--max-seconds",
            "10",
        ],
        input=stdin,
        capture_output=True,
        check=False,
        env={**env, "PYTHONPATH": str(WORKER)},
        timeout=scaled(timeout),
    )
    events = [json.loads(line) for line in done.stdout.splitlines()]
    return done.returncode, events, done.stderr.decode()


def test_real_model_loads_and_rejects_silence_noise_and_tone():
    rng = np.random.default_rng(1)
    tone = 0.3 * np.sin(2 * np.pi * 440 * np.arange(48000) / 16000)
    stream = (
        request("silence", wav_bytes(np.zeros(48000)))
        + request("noise", wav_bytes(0.1 * rng.standard_normal(48000)))
        + request("tone", wav_bytes(tone))
        + b'{"op":"shutdown"}\n'
    )
    code, events, err = run_worker(stream)
    assert code == 0, err
    ready, *results = events
    assert (
        ready["event"] == "ready"
        and ready["model"] == MODEL
        and ready["device"] == "cuda"
    )
    assert ready["vram_mib"] > 1000 and ready["load_s"] > 0
    assert [(r["id"], r["heard"], r.get("reason")) for r in results] == [
        ("silence", False, "no_speech"),
        ("noise", False, "no_speech"),
        ("tone", False, "no_speech"),
    ]


def test_real_model_survives_a_bad_wav_and_stops_on_a_bad_header():
    stream = (
        request("bad", b"RIFF" + b"\x00" * 100)
        + b"not a header\n"
        + request("never", wav_bytes(np.zeros(16000)))
    )
    code, events, _ = run_worker(stream)
    assert code == 2
    assert [e["event"] for e in events] == ["ready", "error", "error"]
    assert (events[1]["id"], events[1]["code"]) == ("bad", "audio_invalid")
    assert "id" not in events[2]


@pytest.mark.skipif(
    not os.getenv("LEVI_STT_TEST_AUDIO")
    or not (Path(os.getenv("LEVI_STT_TEST_AUDIO", "")) / "en1.wav").exists(),
    reason="LEVI_STT_TEST_AUDIO with en1.wav not set",
)
def test_real_model_transcribes_a_spoken_command():
    with wave.open(str(Path(os.environ["LEVI_STT_TEST_AUDIO"]) / "en1.wav")) as clip:
        assert (clip.getframerate(), clip.getnchannels(), clip.getsampwidth()) == (
            16000,
            1,
            2,
        )
        samples = (
            np.frombuffer(clip.readframes(clip.getnframes()), "<i2").astype(np.float32)
            / 32768
        )
    code, events, err = run_worker(
        request("speech", wav_bytes(samples)) + b'{"op":"shutdown"}\n'
    )
    assert code == 0, err
    result = events[-1]
    assert result["heard"] is True and result["language"] == "English"
    assert "eggplant" in result["text"].lower()
    assert result["text"].lower() not in err.lower()  # the transcript is not on stderr
