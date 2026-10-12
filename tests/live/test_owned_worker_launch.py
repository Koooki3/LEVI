"""Owned worker interpreter command: tiny real CPU interpreters, no SDK/model.

Each child imports only a private tmp probe module (or exits at argument
validation), never Torch/vLLM, GPU, HTTP or the robot.
"""

import inspect
import os
import py_compile
import subprocess
import sys

import pytest

from levi.live import owned_runtime as owned


def _interpreter_flags(command):
    entry = next(i for i, value in enumerate(command) if value in ("-m", "-c"))
    assert command[0] == sys.executable
    return command[1:entry]


def _probe(flags, root, cwd):
    return subprocess.run(
        [
            sys.executable,
            *flags,
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(root)!r}); "
                "import stale_probe; print(stale_probe.VALUE)"
            ),
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()


def test_worker_interpreter_never_executes_same_stamp_stale_bytecode(tmp_path):
    """OWNFACTORY: -B only stops writing caches; the worker must not read them.

    The loader, barrier checks and worker loop run in this interpreter, so a
    stale same-mtime/same-size __pycache__ entry would replace reviewed code.
    """
    source = tmp_path / "stale_probe.py"
    old, frozen = "VALUE = 'unsafe'\n", "VALUE = 'frozen'\n"
    assert len(old) == len(frozen)
    source.write_text(old)
    stamp = source.stat()
    py_compile.compile(str(source), doraise=True)
    source.write_text(frozen)
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    caches = sorted((tmp_path / "__pycache__").iterdir())
    # Control: the former "-B" worker interpreter really executes the stale pyc.
    assert _probe(["-B"], tmp_path, tmp_path) == "unsafe"
    flags = _interpreter_flags(owned._worker_command(7))
    assert _probe(flags, tmp_path, tmp_path) == "frozen"
    assert sorted((tmp_path / "__pycache__").iterdir()) == caches


def test_worker_entry_refuses_interpreter_that_may_read_bytecode(monkeypatch):
    from levi.live import owned_worker

    # A private descriptor: an entry that wrongly wraps it closes only this pipe.
    read, write = os.pipe()
    os.close(write)
    monkeypatch.setattr(sys, "argv", ["owned_worker", str(read)])
    received = []
    monkeypatch.setattr(owned_worker, "_receive", received.append)
    with pytest.raises(ValueError, match="no-bytecode interpreter"):
        owned_worker.main()
    os.close(read)  # still ours: refused before any Connection wrapped it
    assert received == []


def test_owned_launch_uses_the_pinned_worker_command():
    source = inspect.getsource(owned.OwnedModelWorker.launch)
    assert "_worker_command(child.fileno())" in source
    assert '"-m"' not in source and "'-m'" not in source
