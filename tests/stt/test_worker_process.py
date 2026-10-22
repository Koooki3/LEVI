"""The speech worker as a process (``python -m levi_stt_worker.cli serve``),
exactly as LEVI's service starts it, but with the ``fake`` provider or with a
model folder that fails before any model code runs: no Torch, no GPU.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import scaled, with_netguard

WORKER_SRC = Path(__file__).resolve().parents[2] / "integrations" / "stt"
READY_KEYS = {"event", "model", "device", "load_s", "vram_mib"}


def wav_bytes(seconds: float = 1.0) -> bytes:
    pcm = b"\x01\x00" * round(seconds * 16000)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def request(request_id: str, payload: bytes | None = None) -> bytes:
    payload = wav_bytes() if payload is None else payload
    return (
        json.dumps(
            {"op": "transcribe", "id": request_id, "bytes": len(payload)}
        ).encode()
        + b"\n"
        + payload
    )


class Worker:
    """One worker process with line-wise, time-limited reads."""

    def __init__(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        python: str = sys.executable,
    ) -> None:
        full_env = {
            **os.environ,
            "PYTHONPATH": with_netguard(str(WORKER_SRC)),
            **(env or {}),
        }
        self.proc = subprocess.Popen(
            [python, "-m", "levi_stt_worker.cli", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            bufsize=0,
        )
        self._buffer = b""
        self.finished = False

    def send(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)

    def event(self, timeout: float = 20) -> dict | None:
        """The next event, or None at EOF; fails the test after ``timeout``."""
        assert self.proc.stdout is not None
        deadline = time.monotonic() + scaled(timeout)
        # Full-suite runs can allocate valid pipes above select()'s FD_SETSIZE.
        with selectors.DefaultSelector() as reader:
            reader.register(self.proc.stdout, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                assert remaining > 0, "no event from the worker in time"
                if not reader.select(remaining):
                    continue
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    return None
                self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line)

    def finish(self, timeout: float = 20) -> tuple[int, list[dict], str]:
        """Close stdin, collect every remaining event, the exit code and stderr."""
        out, err = self.proc.communicate(timeout=scaled(timeout))  # closes stdin first
        self.finished = True
        rest = (self._buffer + out).split(b"\n")
        return (
            self.proc.returncode,
            [json.loads(line) for line in rest if line],
            err.decode(),
        )


@pytest.fixture
def worker():
    started: list[Worker] = []

    def start(*args, **kwargs) -> Worker:
        started.append(Worker(*args, **kwargs))
        return started[-1]

    yield start
    for item in started:
        if item.proc.poll() is None:
            item.proc.kill()
        if not item.finished:
            item.proc.communicate()


@pytest.fixture
def fake(worker, tmp_path):
    def start(script, *extra, env=None):
        path = tmp_path / "script.json"
        path.write_text(json.dumps(script))
        return worker(
            "serve",
            "--provider",
            "fake",
            *extra,
            env={"LEVI_STT_FAKE_SCRIPT": str(path), **(env or {})},
        )

    return start


def test_ready_event_and_results_follow_the_contract(fake):
    script = [
        {
            "heard": True,
            "language": "Chinese",
            "text": "把茄子放到蓝色盘子里。",
            "avg_logprob": -0.12,
            "speech_s": 1.8,
        },
        {"heard": False, "reason": "low_confidence"},
    ]
    proc = fake(
        script,
        "--model",
        "qwen3-asr-1.7b-hf",
        "--max-seconds",
        "10",
        "--cpu-affinity",
        "",
    )
    ready = proc.event()
    assert set(ready) == READY_KEYS
    assert (
        ready["event"] == "ready"
        and ready["model"] == "qwen3-asr-1.7b-hf"
        and ready["device"] == "cuda"
    )
    proc.send(request("req-1") + request("req-2"))
    first, second = proc.event(), proc.event()
    assert (
        first["event"] == "result" and first["id"] == "req-1" and first["heard"] is True
    )
    assert (
        first["language"],
        first["text"],
        first["avg_logprob"],
        first["speech_s"],
    ) == (
        "Chinese",
        "把茄子放到蓝色盘子里。",
        -0.12,
        1.8,
    )
    assert second == {
        "event": "result",
        "id": "req-2",
        "heard": False,
        "reason": "low_confidence",
    }
    proc.send(b'{"op":"shutdown"}\n')
    code, rest, err = proc.finish()
    assert (code, rest) == (0, [])
    assert (
        "把茄子" not in err and "eggplant" not in err
    )  # the transcript never goes to stderr


def test_worker_events_support_a_pipe_above_select_fd_setsize(fake):
    fcntl = pytest.importorskip("fcntl")
    resource = pytest.importorskip("resource")
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 1024:
        pytest.skip("this platform cannot allocate a descriptor above FD_SETSIZE")
    proc = fake([{"heard": False, "reason": "low_confidence"}])
    assert proc.proc.stdout is not None
    high_fd = fcntl.fcntl(proc.proc.stdout.fileno(), fcntl.F_DUPFD, 1024)
    proc.proc.stdout.close()
    proc.proc.stdout = os.fdopen(high_fd, "rb", buffering=0)
    assert proc.proc.stdout.fileno() >= 1024
    assert proc.event()["event"] == "ready"
    proc.send(request("high-fd"))
    assert proc.event() == {
        "event": "result",
        "id": "high-fd",
        "heard": False,
        "reason": "low_confidence",
    }
    assert proc.finish()[:2] == (0, [])


def test_eof_on_stdin_is_a_clean_exit(fake):
    proc = fake([])
    assert proc.event()["event"] == "ready"
    assert proc.finish()[:2] == (0, [])


def test_the_worker_does_not_exit_while_the_stream_is_open(fake):
    proc = fake([])
    proc.event()
    time.sleep(scaled(0.4))
    assert proc.proc.poll() is None
    proc.finish()


def test_ready_fields_can_be_scripted(fake):
    proc = fake({"answers": [], "ready": {"load_s": 1.25, "vram_mib": 4700}})
    ready = proc.event()
    assert (ready["load_s"], ready["vram_mib"], ready["event"]) == (1.25, 4700, "ready")
    proc.finish()


def test_delay_s_delays_only_that_answer(fake):
    proc = fake(
        [
            {"heard": False, "reason": "no_speech", "delay_s": 0.8},
            {"heard": False, "reason": "empty"},
        ]
    )
    proc.event()
    started = time.monotonic()
    proc.send(request("slow") + request("fast"))
    assert proc.event()["id"] == "slow"
    assert time.monotonic() - started >= 0.75
    assert proc.event()["id"] == "fast"
    proc.finish()


def test_crash_entry_kills_the_worker_before_it_answers(fake):
    from_code = 5  # levi_stt_worker.common.EXIT_CRASH
    proc = fake([{"crash": True}, {"heard": False, "reason": "empty"}])
    assert proc.event()["event"] == "ready"
    proc.send(request("doomed"))
    assert proc.event() is None  # EOF: no result, no error event
    code, rest, _ = proc.finish()
    assert (code, rest) == (from_code, [])


def test_scripted_error_and_exhausted_script_are_error_events_and_the_worker_goes_on(
    fake,
):
    proc = fake([{"error": {"code": "internal", "message": "scripted"}}])
    proc.event()
    proc.send(request("a") + request("b"))
    assert proc.event() == {
        "event": "error",
        "code": "internal",
        "message": "scripted",
        "id": "a",
    }
    exhausted = proc.event()
    assert (
        exhausted["event"] == "error"
        and exhausted["id"] == "b"
        and exhausted["code"] == "internal"
    )
    proc.finish()


def test_the_wav_check_is_real_even_with_the_fake_provider(fake):
    proc = fake([{"heard": False, "reason": "empty"}])
    proc.event()
    stereo = bytearray(wav_bytes())
    stereo[22:24] = struct.pack("<H", 2)
    proc.send(
        request("bad", bytes(stereo)) + request("long", wav_bytes(10.6)) + request("ok")
    )
    codes = [proc.event(), proc.event(), proc.event()]
    assert [(e["event"], e["id"], e.get("code")) for e in codes] == [
        ("error", "bad", "audio_invalid"),
        ("error", "long", "too_long"),
        ("result", "ok", None),
    ]
    proc.finish()


def test_max_seconds_option_changes_the_limit(fake):
    proc = fake([{"heard": False, "reason": "empty"}], "--max-seconds", "3")
    proc.event()
    proc.send(request("a", wav_bytes(3.4)) + request("b", wav_bytes(3.6)))
    assert proc.event()["event"] == "result"
    assert proc.event()["code"] == "too_long"
    proc.finish()


def test_a_malformed_header_is_answered_and_never_resynchronised(fake):
    proc = fake(
        [{"heard": True, "language": "English", "text": "pick", "avg_logprob": -0.1}]
    )
    proc.event()
    proc.send(b"this is not a header\n" + request("would-be-valid"))
    error = proc.event()
    assert (
        error["event"] == "error" and error["code"] == "internal" and "id" not in error
    )
    code, rest, err = proc.finish()
    assert (
        code == 2 and rest == []
    )  # exited, and the "valid" request after the junk was never served
    assert "would-be-valid" not in err


def test_a_short_audio_stream_ends_the_worker_with_an_audio_error(fake):
    proc = fake([])
    proc.event()
    payload = wav_bytes()
    proc.send(
        json.dumps({"op": "transcribe", "id": "cut", "bytes": len(payload)}).encode()
        + b"\n"
        + payload[:1000]
    )
    code, rest, _ = proc.finish()  # stdin closes mid-payload
    assert code == 2
    assert [(e["code"], e["id"]) for e in rest] == [("audio_invalid", "cut")]


def test_an_oversized_announcement_ends_the_worker(fake):
    proc = fake([])
    proc.event()
    proc.send(b'{"op":"transcribe","id":"big","bytes":400001}\n')
    error = proc.event()
    assert error["code"] == "too_long"
    assert proc.finish()[0] == 2


def test_sigterm_is_a_clean_exit(fake):
    proc = fake([])
    proc.event()
    proc.proc.send_signal(signal.SIGTERM)
    code, _, _ = proc.finish()
    assert code == 0


def test_a_scripted_startup_failure_is_reported_and_non_zero(fake):
    proc = fake({"startup_error": {"code": "internal", "message": "no gpu"}})
    assert proc.event() == {"event": "error", "code": "internal", "message": "no gpu"}
    assert proc.finish()[0] == 4


def test_an_unreadable_fake_script_is_a_startup_error(worker, tmp_path):
    (tmp_path / "bad.json").write_text("{not json")
    proc = worker(
        "serve",
        "--provider",
        "fake",
        env={"LEVI_STT_FAKE_SCRIPT": str(tmp_path / "bad.json")},
    )
    error = proc.event()
    assert error["event"] == "error" and error["code"] == "internal"
    assert str(tmp_path) not in json.dumps(error)
    assert proc.finish()[0] == 4


def test_library_prints_cannot_corrupt_the_protocol_channel(tmp_path):
    """fd 1 is pointed at stderr after the worker takes a private copy for events."""
    code = (
        "import sys, json\n"
        "from levi_stt_worker import serve as s\n"
        "class P:\n"
        "    name = 'p'\n"
        "    def load(self): print('noise during load'); return {}\n"
        "    def transcribe(self, rid, clip): print('noise while serving'); return {'heard': False, 'reason': 'empty'}\n"
        "    def close(self): pass\n"
        "s.make_provider = lambda options: P()\n"
        "sys.exit(s.serve(s.Options('fake', None, 'm', 'cuda', 10.0, '')))\n"
    )
    env = {**os.environ, "PYTHONPATH": with_netguard(str(WORKER_SRC))}
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=request("r") + b'{"op":"shutdown"}\n',
        capture_output=True,
        check=False,
        env=env,
        timeout=scaled(30),
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [e["event"] for e in events] == ["ready", "result"]
    assert (
        b"noise during load" in result.stderr
        and b"noise while serving" in result.stderr
    )


def serve_with(provider_source: str, stdin: bytes = b'{"op":"shutdown"}\n'):
    """``serve`` in a child Python with ``make_provider`` replaced by the
    expression ``provider_source`` (which may use ``qwen``, ``Path`` and the
    classes defined before it)."""
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from levi_stt_worker import qwen, serve as s\n"
        f"{provider_source}\n"
        "s.make_provider = lambda options: PROVIDER\n"
        "sys.exit(s.serve(s.Options('qwen3-asr', None, 'm', 'cuda', 10.0, '')))\n"
    )
    env = {**os.environ, "PYTHONPATH": with_netguard(str(WORKER_SRC))}
    done = subprocess.run(
        [sys.executable, "-c", code],
        input=stdin,
        capture_output=True,
        check=False,
        env=env,
        timeout=scaled(30),
    )
    return (
        done.returncode,
        [json.loads(l) for l in done.stdout.splitlines()],
        done.stderr.decode(),
    )


def test_the_ready_event_carries_the_device_the_provider_reports():
    """Not the --device option: the manager's device check compares this value."""
    code, events, _ = serve_with(
        "class P:\n"
        "    name = 'p'\n"
        "    def load(self): return {'device': 'cpu', 'vram_mib': 7}\n"
        "    def close(self): pass\n"
        "PROVIDER = P()"
    )
    assert code == 0
    assert (events[0]["event"], events[0]["device"], events[0]["vram_mib"]) == (
        "ready",
        "cpu",
        7,
    )


def test_a_failing_warm_up_is_a_startup_error_with_exit_4_not_a_traceback():
    """Out of memory (or a kernel error) in the first inference after the load:
    the service must see an error event and exit code 4, so the page can say the
    speech worker failed to start instead of 'stopped unexpectedly'."""
    secret = "CUDA out of memory: transcript-secret /home/marvel/work/x"
    code, events, err = serve_with(
        "class P(qwen.QwenAsrProvider):\n"
        "    def _load(self): self._real_device = 'cuda'\n"
        f"    def recognise(self, samples): raise RuntimeError({secret!r})\n"
        "PROVIDER = P(Path('m'), 'cuda')"
    )
    assert code == 4
    assert events == [
        {
            "event": "error",
            "code": "internal",
            "message": "The speech model could not be loaded (RuntimeError).",
        }
    ]
    assert "Traceback" not in err and "transcript-secret" not in err


def test_the_process_lowers_its_priority_and_applies_the_cpu_set():
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 2:
        pytest.skip("needs at least two CPUs to see an affinity change")
    pick = allowed[-1]
    code = (
        "import os\n"
        "from levi_stt_worker.serve import harden_process\n"
        f"harden_process({str(pick)!r})\n"
        "print(os.getpriority(os.PRIO_PROCESS, 0), sorted(os.sched_getaffinity(0)), os.environ['HF_HUB_OFFLINE'], "
        "os.environ['TRANSFORMERS_OFFLINE'], os.environ['OMP_NUM_THREADS'])\n"
    )
    env = {**os.environ, "PYTHONPATH": with_netguard(str(WORKER_SRC))}
    env.pop("OMP_NUM_THREADS", None)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=scaled(30),
    )
    assert out.stdout.split() == ["19", f"[{pick}]", "1", "1", "2"], out.stderr


def test_a_bad_cpu_set_is_logged_and_not_fatal():
    code = (
        "from levi_stt_worker.serve import harden_process\n"
        "harden_process('not-a-cpu-set')\n"
        "harden_process('9999')\n"
        "print('alive')\n"
    )
    env = {**os.environ, "PYTHONPATH": with_netguard(str(WORKER_SRC))}
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=scaled(30),
    )
    assert out.stdout.strip() == "alive"
    assert out.stderr.count("CPU affinity was not applied") == 2


# ------------------------------------------------- model folder, no model code


def model_folder(tmp_path: Path, name: str = "qwen3-asr-1.7b-hf") -> Path:
    from levi_stt_worker.manifest import write_manifest

    folder = tmp_path / "models" / name
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}")
    (folder / "model.safetensors").write_bytes(b"not a real model")
    write_manifest(folder)
    return folder


@pytest.fixture
def manifest_tools():
    sys.path.insert(0, str(WORKER_SRC))
    yield
    sys.path.remove(str(WORKER_SRC))
    for name in [n for n in sys.modules if n.startswith("levi_stt_worker")]:
        del sys.modules[name]


def test_a_tampered_model_stops_the_worker_before_anything_is_loaded(
    worker, tmp_path, manifest_tools
):
    folder = model_folder(tmp_path)
    (folder / "model.safetensors").write_bytes(b"swapped weights")
    proc = worker("serve", "--model-dir", str(tmp_path / "models"))
    error = proc.event()
    assert error["event"] == "error" and error["code"] == "manifest_mismatch"
    assert (
        "model.safetensors" in error["message"]
        and str(tmp_path) not in error["message"]
    )
    code, rest, _ = proc.finish()
    assert code == 3 and rest == []  # no ready event ever


def test_an_unlisted_file_in_the_model_folder_is_a_mismatch(
    worker, tmp_path, manifest_tools
):
    (model_folder(tmp_path) / "modeling_evil.py").write_text("raise SystemExit")
    proc = worker("serve", "--model-dir", str(tmp_path / "models"))
    assert proc.event()["code"] == "manifest_mismatch"
    assert proc.finish()[0] == 3


def test_a_missing_model_folder_has_its_own_code(worker, tmp_path):
    (tmp_path / "models").mkdir()
    proc = worker("serve", "--model-dir", str(tmp_path / "models"))
    assert proc.event()["code"] == "model_missing"
    assert proc.finish()[0] == 3


def test_a_verified_model_folder_goes_on_to_load_and_fails_there_not_at_the_manifest(
    worker, tmp_path, manifest_tools
):
    """The folder verifies, so the failure comes from the loader (bogus weights
    or no Torch in this Python): a startup error with a different code and exit."""
    model_folder(tmp_path)
    proc = worker("serve", "--model-dir", str(tmp_path / "models"))
    error = proc.event(timeout=120)
    assert error["event"] == "error" and error["code"] == "internal"
    assert str(tmp_path) not in error["message"]
    assert proc.finish()[0] == 4


# ------------------------------------------------------------------- the cli


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": with_netguard(str(WORKER_SRC))}
    return subprocess.run(
        [sys.executable, "-m", "levi_stt_worker.cli", *args],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=scaled(60),
    )


def test_check_prints_versions_and_exits_by_completeness():
    result = run_cli("--check")
    report = json.loads(result.stdout)
    assert set(report["packages"]) == {
        "numpy",
        "torch",
        "transformers",
        "accelerate",
        "silero-vad",
    }
    assert result.returncode == (0 if report["complete"] else 1)
    assert report["complete"] == all(report["packages"].values())


@pytest.mark.parametrize(
    "args",
    [
        ["serve"],  # qwen provider needs --model-dir
        ["serve", "--model-dir", "/x", "--model", "../escape"],
        ["serve", "--model-dir", "/x", "--model", "a/b"],
        ["serve", "--model-dir", "/x", "--model", ".hidden"],
        ["serve", "--model-dir", "/x", "--device", "cpu"],
        ["serve", "--model-dir", "/x", "--provider", "whisper"],
        [],
    ],
)
def test_cli_rejects_bad_arguments(args):
    assert run_cli(*args).returncode == 2


def test_verify_model_command(tmp_path, manifest_tools):
    folder = model_folder(tmp_path)
    ok = run_cli("verify-model", "--model-dir", str(tmp_path / "models"))
    assert ok.returncode == 0 and "2 files verified" in ok.stdout
    (folder / "config.json").write_text('{"x": 1}')
    bad = run_cli("verify-model", "--model-dir", str(tmp_path / "models"))
    assert bad.returncode == 3 and "manifest_mismatch" in bad.stderr
