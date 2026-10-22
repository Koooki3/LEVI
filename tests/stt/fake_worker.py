"""A stand-in speech worker for the manager and route tests: the protocol of
docs/SPEECH.md in about a hundred lines of standard library, no model and no
package of ``integrations/stt``.

As a script it prints ``ready``, then answers each request from a script of
steps (a JSON list, one step per request, consumed in order). A step is a
dict with ``do``. The steps count requests over restarts: a worker started
after a crash goes on with the next step, not the first (the position is kept
in a file beside the script):

- ``result`` (``text``, ``language``, ``infer``, ``delay``), ``heard_false``
  (``reason``), ``error`` (``code``, ``message``);
- misbehaviour: ``sleep`` (never answers), ``crash`` (exits), ``garbage`` (a
  line that is not JSON, or ``line``), ``oversize`` (a very long line without a newline),
  ``short`` (half a line, then exits), ``wrong_id``, ``extra_line`` (a result
  followed by a line nobody asked for), ``late_line`` (the same, a moment later);
- any step may carry ``grandchild: true`` to start a ``sleep`` process in the
  same group first (its pid is recorded), so a test can see the whole group go.

``--start`` changes how it starts: ``ok``, ``never`` (no ``ready``), ``manifest``
(the manifest error), ``crash``, ``garbage``, ``cpu`` (ready on the CPU).
Everything it did is appended to the ``--record`` file as JSON lines (pid,
argv, selected environment, one line per request with its byte count and
SHA-256); it writes only lengths to stderr.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

THIS = Path(__file__).resolve()
ENV_KEYS = (
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "OMP_NUM_THREADS",
    "LEVI_GPU_LOCK_FILE",
    "LEVI_UI_TOKEN",
    "HF_TOKEN",
    "LEVI_STT_SENTINEL_SECRET",
    "PYTHONUNBUFFERED",
)


# ------------------------------------------------------------ the test side


def command(tmp_path, script=(), *, start="ok", record=None, delay=0.0):
    """A ``command(cpu_affinity)`` for ``Manager`` that runs this file."""
    script_file = Path(tmp_path) / "worker-script.json"
    script_file.write_text(json.dumps(list(script)))

    def build(cpu_affinity):
        argv = [
            sys.executable,
            "-I",
            str(THIS),
            "serve",
            "--script",
            str(script_file),
            "--start",
            start,
            "--ready-delay",
            str(delay),
            "--cpu-affinity",
            cpu_affinity,
        ]
        return argv + (["--record", str(record)] if record else [])

    return build


def records(path):
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


# ------------------------------------------------------------ the worker side


def _emit(obj):
    sys.stdout.buffer.write((json.dumps(obj) + "\n").encode())
    sys.stdout.buffer.flush()


def _arg(name, default=None):
    argv = sys.argv
    return argv[argv.index(name) + 1] if name in argv else default


def _note(record, **fields):
    if record:
        with open(record, "a") as handle:
            handle.write(json.dumps(fields) + "\n")


def _result(request_id, step):
    if step.get("delay"):
        time.sleep(step["delay"])
    return {
        "event": "result",
        "id": request_id,
        "heard": True,
        "language": step.get("language", "English"),
        "text": step.get("text", "pick the eggplant in the blue plate"),
        "avg_logprob": -0.1,
        "speech_s": 1.8,
        "timing_ms": {"vad": 8, "infer": step.get("infer", 12)},
    }


def _act(step, request_id):
    do = step.get("do", "result")
    if step.get("grandchild"):
        child = subprocess.Popen(["sleep", "60"])
        _note(_arg("--record"), grandchild=child.pid)
    if do == "result":
        _emit(_result(request_id, step))
    elif do == "heard_false":
        _emit(
            {
                "event": "result",
                "id": request_id,
                "heard": False,
                "reason": step.get("reason", "no_speech"),
            }
        )
    elif do == "error":
        _emit(
            {
                "event": "error",
                "id": request_id,
                "code": step.get("code", "internal"),
                "message": step.get("message", "failed"),
            }
        )
    elif do == "sleep":
        time.sleep(step.get("seconds", 60))
    elif do == "crash":
        sys.exit(step.get("code", 9))
    elif do == "garbage":
        sys.stdout.buffer.write(step.get("line", "this is not json").encode() + b"\n")
        sys.stdout.buffer.flush()
    elif do == "oversize":
        sys.stdout.buffer.write(b"x" * 200_000)
        sys.stdout.buffer.flush()
        time.sleep(60)
    elif do == "short":
        sys.stdout.buffer.write(b'{"event":"result","id":"' + request_id.encode()[:3])
        sys.stdout.buffer.flush()
        sys.exit(0)
    elif do == "wrong_id":
        _emit(_result("some-other-id", step))
    elif do in ("extra_line", "late_line"):
        _emit(_result(request_id, step))
        if do == "late_line":
            time.sleep(0.3)
        _emit({"event": "log", "n": 1})


def serve():
    record = _arg("--record")
    script = json.loads(Path(_arg("--script")).read_text())
    start = _arg("--start", "ok")
    _note(
        record,
        pid=os.getpid(),
        argv=sys.argv[1:],
        cwd=os.getcwd(),
        env={k: os.environ.get(k) for k in ENV_KEYS},
    )
    time.sleep(float(_arg("--ready-delay", "0")))
    if start == "never":
        time.sleep(600)
    elif start == "manifest":
        _emit({"event": "error", "code": "manifest_mismatch", "message": "x"})
        sys.exit(2)
    elif start == "crash":
        sys.exit(3)
    elif start == "garbage":
        sys.stdout.buffer.write(b"hello\n")
        sys.stdout.buffer.flush()
    else:
        device = "cpu" if start == "cpu" else "cuda"
        _emit(
            {
                "event": "ready",
                "model": "fake",
                "device": device,
                "load_s": 0.01,
                "vram_mib": 1,
            }
        )
    cursor = Path(_arg("--script") + ".cursor")
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return
        message = json.loads(line)
        if message.get("op") == "shutdown":
            return
        data = stdin.read(message["bytes"])
        _note(
            record,
            op="request",
            id=message["id"],
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        print(f"request bytes={len(data)}", file=sys.stderr, flush=True)
        done = int(cursor.read_text()) if cursor.exists() else 0
        cursor.write_text(str(done + 1))
        _act(
            script[done] if done < len(script) else {"do": "heard_false"}, message["id"]
        )


if __name__ == "__main__":
    serve()
