"""Owned Ollama process lifecycle for Linux/WSL; never manages shared services."""

import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path

from levi import children

_CHILDREN: dict[int, subprocess.Popen] = {}


# Linux x86_64/aarch64 syscall numbers, used when the interpreter was built
# without the os/signal wrappers (some standalone Python builds). The kernel
# feature is what matters; the wrapper is a convenience.
_SYSCALLS = {"pidfd_open": 434, "pidfd_send_signal": 424}


def _syscall(name, *args):
    import ctypes
    import platform

    if platform.system() != "Linux":
        raise ValueError("Safe process ownership control requires Linux pidfd support")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(ctypes.c_long(_SYSCALLS[name]), *args)
    if result < 0:
        errno = ctypes.get_errno()
        if errno == 3:  # ESRCH
            raise ProcessLookupError(errno, os.strerror(errno))
        raise ValueError(
            f"Safe process ownership control requires Linux pidfd support "
            f"({name}: {os.strerror(errno)})"
        )
    return result


def pidfd_open(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    import ctypes

    return _syscall("pidfd_open", ctypes.c_int(pid), ctypes.c_uint(0))


def pidfd_send_signal(fd, sig):
    if hasattr(signal, "pidfd_send_signal"):
        return signal.pidfd_send_signal(fd, sig)
    import ctypes

    _syscall(
        "pidfd_send_signal",
        ctypes.c_int(fd),
        ctypes.c_int(int(sig)),
        ctypes.c_void_p(None),
        ctypes.c_uint(0),
    )
    return None


class OllamaRuntimeManager:
    def __init__(self, workspace: Path, state: Path):
        self.workspace = workspace.resolve()
        self.root = (state / "models/ollama").resolve()
        if not self.root.is_relative_to(self.workspace):
            raise ValueError("Model runtime state must remain inside LEVI_WORKSPACE")
        self.models = (self.workspace / "checkpoints/ollama").resolve()
        if not self.models.is_relative_to(self.workspace):
            raise ValueError("Model storage must remain inside LEVI_WORKSPACE")
        self.record = self.root / "instance.json"

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "instance.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    identity = staticmethod(children.identity)

    def _record(self):
        try:
            return json.loads(self.record.read_text())
        except FileNotFoundError:
            return None

    def status(self):
        for pid, child in list(_CHILDREN.items()):
            if child.poll() is not None:
                _CHILDREN.pop(pid, None)
        record = self._record()
        running = bool(record and self.identity(record["pid"]) == record["identity"])
        return {
            "ownership": "levi",
            "running": running,
            "url": record["url"] if record else "http://127.0.0.1:11435",
            "models_path": str(self.models),
            "log_path": str(self.root / "server.log"),
            "installed": shutil.which("ollama") is not None,
            "cloud_disabled": True,
            "network_isolated": False,
        }

    def start(self, port=11435):
        if not 1024 <= port <= 65535:
            raise ValueError("Managed model port must be between 1024 and 65535")
        with self.lock():
            if self.status()["running"]:
                if self._record()["url"] != f"http://127.0.0.1:{port}":
                    raise ValueError("Stop the owned service before changing its port")
                return self.status()
            binary = shutil.which("ollama")
            if not binary:
                raise ValueError(
                    "Ollama is not installed; install it explicitly before starting a managed instance"
                )
            binary = str(Path(binary).resolve(strict=True))
            # Refuse to reuse a shared instance or kill its owner.
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    raise ValueError(
                        "Model port is already occupied; choose a dedicated port"
                    ) from None
            self.models.mkdir(parents=True, exist_ok=True)
            home = (self.root / "home").resolve()
            if not home.is_relative_to(self.root):
                raise ValueError("Runtime home must remain inside its state directory")
            home.mkdir(exist_ok=True)
            # A minimal environment prevents provider/HF/W&B credentials and
            # unrelated service settings from being inherited by the child.
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home),
                "OLLAMA_HOST": f"127.0.0.1:{port}",
                "OLLAMA_MODELS": str(self.models),
                "OLLAMA_NO_CLOUD": "1",
                "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_MAX_LOADED_MODELS": "1",
            }
            with (self.root / "server.log").open("ab") as log:
                process = subprocess.Popen(
                    [binary, "serve"],
                    env=env,
                    cwd=self.root,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            _CHILDREN[process.pid] = process
            identity = self.identity(process.pid)
            if identity is None or process.poll() is not None:
                raise ValueError(
                    "Owned Ollama process did not start; inspect its local server log"
                )
            record = {
                "pid": process.pid,
                "identity": identity,
                "url": f"http://127.0.0.1:{port}",
            }
            temporary = self.record.with_suffix(".pending")
            try:
                with temporary.open("w") as handle:
                    json.dump(record, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.record)
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                # Our direct, unreaped child cannot be confused with a reused
                # PID. Failed ownership persistence must not orphan it.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                _CHILDREN.pop(process.pid, None)
                temporary.unlink(missing_ok=True)
                raise
            return self.status()

    def stop(self):
        with self.lock():
            record = self._record()
            if not record or self.identity(record["pid"]) != record["identity"]:
                return self.status()
            try:
                fd = pidfd_open(record["pid"])
            except ProcessLookupError:
                return self.status()
            try:
                # Re-check identity after pinning the process handle to prevent
                # PID reuse from delivering SIGTERM to an unrelated process.
                if self.identity(record["pid"]) != record["identity"]:
                    raise ValueError("Process identity changed; stop was refused")
                pidfd_send_signal(fd, signal.SIGTERM)
            finally:
                os.close(fd)
            return {**self.status(), "stop_requested": True}
