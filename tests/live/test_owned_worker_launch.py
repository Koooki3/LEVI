"""Owned worker interpreter command: tiny real CPU interpreters, no SDK/model.

Each child imports only a private tmp probe module (or exits at argument
validation), never Torch/vLLM, GPU, HTTP or the robot.
"""

import inspect
import os
import py_compile
import subprocess
import sys
from pathlib import Path

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


def _shadow_levi(root, marker):
    package = root / "levi" / "live"
    package.mkdir(parents=True)
    record = f"open({str(marker)!r}, 'a').write({str(root)!r} + '\\n')\n"
    (root / "levi" / "__init__.py").write_text(record)
    (package / "__init__.py").write_text("")
    (package / "owned_worker.py").write_text(record)


def test_worker_executes_the_supervisor_levi_origin_not_a_shadow(tmp_path):
    """OWNORIGIN: cwd/PYTHONPATH cannot choose which loader the worker runs.

    The owned loader, barrier checks and ACK loop in the child are the code
    that binds SDK origins; a shadow levi package would replace all of them.
    """
    cwd, path_shadow = tmp_path / "cwd", tmp_path / "pythonpath"
    marker = tmp_path / "executed-shadow"
    _shadow_levi(cwd, marker)
    _shadow_levi(path_shadow, marker)
    env = dict(os.environ, PYTHONPATH=str(path_shadow))
    # Control: the former "-m levi.live.owned_worker" launch runs a shadow.
    subprocess.run(
        [sys.executable, "-B", "-m", "levi.live.owned_worker", "x"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    executed = set(marker.read_text().split())
    assert executed and executed <= {str(cwd), str(path_shadow)}
    marker.unlink()
    # A closed private pipe: the child passes interpreter, argument and origin
    # checks, then stops at the first bootstrap read with EOF.
    read, write = os.pipe()
    os.close(write)
    try:
        result = subprocess.run(
            owned._worker_command(read),
            cwd=cwd,
            env=env,
            pass_fds=(read,),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        os.close(read)
    assert not marker.exists()
    assert result.returncode != 0 and "EOFError" in result.stderr
    worker = Path(owned.__file__).resolve().with_name("owned_worker.py")
    assert str(worker) in result.stderr


def test_worker_refuses_levi_modules_outside_its_pinned_root(tmp_path):
    from levi.live import owned_worker

    with pytest.raises(ValueError, match="origin"):
        owned_worker._require_origin(str(tmp_path))
    owned_worker._require_origin(str(Path(owned.__file__).resolve().parents[2]))


def _factory_root(root):
    """A frozen factory plus fake torch SDK outside the repo and site-packages."""
    import hashlib

    for relative, text in {
        "torch/__init__.py": (
            "from . import cuda\n__version__ = 'frozen'\n"
            "class device:\n    def __init__(self, name): self.name = name\n"
            "from contextlib import nullcontext as inference_mode\n"
        ),
        "torch/cuda/__init__.py": (
            "def is_available(): return True\n"
            "def synchronize(device=None): return None\n"
        ),
        "torch-frozen.dist-info/METADATA": (
            "Metadata-Version: 2.1\nName: torch\nVersion: frozen\n"
        ),
        "closed_factory.py": (
            "def build(device=None, sdk=None):\n    return lambda payload: payload\n"
        ),
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def sha(relative):
        return hashlib.sha256((root / relative).read_bytes()).hexdigest()

    return owned.FrozenTorchFactory(
        "closed_factory",
        "build",
        sha("closed_factory.py"),
        "frozen",
        "cuda:0",
        source_root=str(root),
        torch_source_hashes=(
            ("torch", sha("torch/__init__.py")),
            ("torch.cuda", sha("torch/cuda/__init__.py")),
        ),
    )


def test_pinned_worker_loads_factory_from_its_explicit_source_root(tmp_path):
    """Item 2: the -I worker sees the factory root only through the bootstrap.

    Real child interpreter with the actual worker command; the parent sends
    the same bootstrap/enrolled/stop messages as OwnedModelWorker.launch.
    """
    import multiprocessing

    factory = _factory_root(tmp_path / "factory-root")
    parent, child = multiprocessing.Pipe()
    process = subprocess.Popen(
        owned._worker_command(child.fileno()),
        cwd=tmp_path,
        pass_fds=(child.fileno(),),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child.close()
    try:
        owned._send(parent, ("bootstrap", factory, "launch"))
        owned._send(parent, ("enrolled", "launch"))
        assert parent.poll(60)
        kind, ack, detail = owned._receive(parent)
        if kind == "ready":
            owned._send(parent, ("stop", 1, None))
        _out, err = process.communicate(timeout=60)
    finally:
        parent.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    assert (kind, detail) == ("ready", None), err
    assert ack.request_epoch == 0 and ack.capability_id == factory.capability_id
    assert process.returncode == 0, err
