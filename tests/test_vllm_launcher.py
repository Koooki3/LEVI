"""The shipped vLLM launcher (``scripts/vllm/serve.sh``) against the contract
``levi live`` relies on (docs/VLLM.md): launch with LEVI's environment and
arguments, the pid file in ``vllm.pid_dir``, ``--stop PORT``. A stand-in
``vllm`` executable runs the fake model server, so no GPU is needed."""

import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from levi.live import config as live_config
from levi.live import gpumgr

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts/vllm/serve.sh"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def venv(tmp_path, monkeypatch):
    """A vLLM environment whose ``vllm`` records how it was called, then
    becomes the fake model server on the requested port."""
    root = tmp_path / "venv-vllm"
    (root / "bin").mkdir(parents=True)
    record = tmp_path / "called.json"
    fake = root / "bin/vllm"
    fake.write_text(
        f"""#!/usr/bin/env bash
port=""; prev=""
for a in "$@"; do [[ "$prev" == "--port" ]] && port="$a"; prev="$a"; done
"{sys.executable}" -c 'import json,os,sys; json.dump({{"argv": sys.argv[1:], "env": {{k: v for k, v in os.environ.items() if k.startswith(("LEVI_VLLM", "VLLM_SERVER", "GPU_UTIL", "PORT"))}}}}, open("{record}", "w"))' "$@"
exec -a vllm "{sys.executable}" -m levi.live.fakevlm --port "$port"
"""
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PYTHONPATH", str(PROJECT))
    monkeypatch.setenv("LEVI_VLLM_VENV", str(root))
    monkeypatch.setenv("SERVE_SKIP_PREFLIGHT", "1")  # no nvidia-smi in the test
    return root, record


def config_for(tmp_path):
    c = live_config.Config()
    c.service.workspace = str(tmp_path / "ws")
    c.service.home = str(tmp_path / "home")
    c.vllm.pid_dir = str(tmp_path / "pids")
    c.vllm.port = free_port()
    return c


def test_the_default_launcher_is_the_shipped_script_and_it_is_executable():
    c = live_config.Config()
    assert c.vllm_script == SCRIPT == c.vllm_stop_script
    assert os.access(SCRIPT, os.X_OK)
    text = SCRIPT.read_text()
    assert "/home/" not in text and "/Users/" not in text


def test_the_shipped_launcher_keeps_the_contract_levi_live_relies_on(tmp_path, venv):
    _, record = venv
    c = config_for(tmp_path)
    vllm = gpumgr.Vllm(c)
    profile = {**c.vllm_profile(), "max_model_len": 32768}
    try:
        assert vllm.start(profile), vllm.error
        pidfile = Path(c.vllm.pid_dir) / f"vllm_{c.vllm.port}.pid"
        pid = int(pidfile.read_text())
        assert vllm.mine()
        # The pid is the process-group leader (LEVI signals the group).
        assert os.getpgid(pid) == pid
        deadline = time.time() + 20
        while time.time() < deadline and not gpumgr.healthy(c.vllm.port):
            time.sleep(0.1)
        assert gpumgr.healthy(c.vllm.port)
        called = json.loads(record.read_text())
        argv, env = called["argv"], called["env"]
        assert argv[:2] == ["serve", c.vllm.model]
        assert argv[argv.index("--host") + 1] == "127.0.0.1"
        assert argv[argv.index("--port") + 1] == str(c.vllm.port)
        assert argv[argv.index("--served-model-name") + 1] == c.vllm.served_model
        # LEVI's arguments come after the script's own, so they win.
        assert argv[len(argv) - argv[::-1].index("--max-model-len")] == "32768"
        assert argv.index("--structured-outputs-config") > argv.index(
            "--served-model-name"
        )
        assert "--enable-sleep-mode" in argv
        assert env["LEVI_VLLM_PID_DIR"] == str(Path(c.vllm.pid_dir))
        assert env["VLLM_SERVER_DEV_MODE"] == "1"
        assert env["GPU_UTIL"] == str(profile["gpu_memory_utilization"])
    finally:
        assert vllm.stop()
    assert not (Path(c.vllm.pid_dir) / f"vllm_{c.vllm.port}.pid").exists()
    assert not gpumgr.healthy(c.vllm.port)


def test_the_launcher_refuses_without_a_model_and_check_says_why(tmp_path, venv):
    env = {**os.environ, "LEVI_VLLM_PID_DIR": str(tmp_path / "pids")}
    env.pop("LEVI_VLLM_MODEL", None)
    run = subprocess.run(
        [str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert run.returncode == 2 and "LEVI_VLLM_MODEL" in run.stderr
    check = subprocess.run(
        [str(SCRIPT), "--check"], env=env, capture_output=True, text=True, check=False
    )
    assert check.returncode == 1 and "NOT SET" in check.stdout
    stop = subprocess.run(
        [str(SCRIPT), "--stop", "65000"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert stop.returncode == 1 and "no pid file" in stop.stderr


def test_a_launch_that_cannot_run_names_the_script(tmp_path):
    c = config_for(tmp_path)
    c.vllm.script = str(tmp_path / "missing.sh")
    vllm = gpumgr.Vllm(c)
    assert not vllm.start(c.vllm_profile())
    assert "missing.sh" in vllm.error and "docs/VLLM.md" in vllm.error


def _script(args, env):
    return subprocess.run(
        [str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def test_stop_signals_only_a_vllm_named_by_a_valid_pid_file(tmp_path):
    pids = tmp_path / "pids"
    pids.mkdir()
    env = {**os.environ, "LEVI_VLLM_PID_DIR": str(pids)}
    (pids / "vllm_65001.pid").write_text("-1\n")  # would signal every process
    run = _script(["--stop", "65001"], env)
    assert run.returncode == 1 and "does not hold a process id" in run.stderr
    stranger = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        (pids / "vllm_65002.pid").write_text(f"{stranger.pid}\n")
        run = _script(["--stop", "65002"], env)
        assert run.returncode == 1 and "not a vLLM server" in run.stderr
        assert stranger.poll() is None  # left alone
    finally:
        stranger.kill()
        stranger.wait()


def test_an_unreadable_nvidia_smi_is_named(tmp_path, venv):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    smi = fakebin / "nvidia-smi"
    smi.write_text("#!/bin/sh\necho 'NVIDIA-SMI has failed' >&2\nexit 9\n")
    smi.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "LEVI_VLLM_MODEL": "some/model",
        "LEVI_VLLM_PID_DIR": str(tmp_path / "pids"),
    }
    env.pop("SERVE_SKIP_PREFLIGHT")
    run = _script([], env)
    assert run.returncode == 3 and "cannot read free GPU memory" in run.stderr
    assert not (tmp_path / "pids" / "vllm_8100.pid").exists()


def test_stop_recognises_a_vllm_with_a_long_command_line(tmp_path):
    """The command-line check reads ``/proc/PID/cmdline`` before matching:
    with ``pipefail``, ``tr | grep -q`` failed when grep stopped reading
    early and ``tr`` got SIGPIPE, and a real server was called a stranger."""
    pids = tmp_path / "pids"
    pids.mkdir()
    env = {**os.environ, "LEVI_VLLM_PID_DIR": str(pids)}
    # About 1 MB of short lines after the name: grep -q matches on the first
    # line and stops reading while tr still writes.
    filler = ["x\n" * 50_000] * 10
    server = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "vllm", *filler],
        start_new_session=True,
    )
    # Reap it as soon as it ends, or the script would wait 60 s on a zombie.
    import threading

    reaper = threading.Thread(target=server.wait, daemon=True)
    reaper.start()
    try:
        (pids / "vllm_65003.pid").write_text(f"{server.pid}\n")
        run = _script(["--stop", "65003"], env)
        assert run.returncode == 0, run.stderr
        assert "stopping vllm" in run.stdout and "SIGKILL" not in run.stdout
        reaper.join(timeout=30)
        assert server.returncode is not None
        assert not (pids / "vllm_65003.pid").exists()
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def test_stop_is_quiet_when_the_command_line_cannot_be_read(tmp_path):
    """A process whose command line reads empty (here a zombie): nothing is
    signalled, the pid file stays, and no shell error about ``/proc``."""
    pids = tmp_path / "pids"
    pids.mkdir()
    env = {**os.environ, "LEVI_VLLM_PID_DIR": str(pids)}
    child = os.fork() if hasattr(os, "fork") else None
    if child == 0:
        os._exit(0)
    try:
        deadline = time.time() + 10
        status = Path(f"/proc/{child}/status")
        while time.time() < deadline and "zombie" not in status.read_text():
            time.sleep(0.05)
        assert Path(f"/proc/{child}/cmdline").read_bytes() == b""
        (pids / "vllm_65004.pid").write_text(f"{child}\n")
        run = _script(["--stop", "65004"], env)
        assert run.returncode == 1 and "cannot read the command line" in run.stderr
        assert "No such file" not in run.stderr and "/proc/" not in run.stderr
        assert (pids / "vllm_65004.pid").exists()
    finally:
        os.waitpid(child, 0)
