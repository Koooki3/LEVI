"""Strict exit proofs: fake GPU sources, owned CPU processes and scratch flocks."""

import contextlib
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from levi.live import gpumgr, jsonio
from levi.live.config import Config
from levi.live.controller import Controller

PID = 4_000_000
OLD = {"start_ticks": "100", "boot": "boot"}


@pytest.fixture
def model(tmp_path, monkeypatch):
    config = Config()
    config.service.workspace = str(tmp_path)
    config.vllm.stop_script = "/never-executed"
    monkeypatch.setattr(gpumgr, "gpu_holders", list)
    monkeypatch.setattr(gpumgr, "compute_apps", list)  # source=fake, never nvidia
    monkeypatch.setattr(gpumgr, "STOP_CONFIRM_S", 0)
    vllm = gpumgr.Vllm(config)
    jsonio.write(vllm.record_path, {"pid": PID, "identity": OLD})
    vllm.state = "ready"
    vllm.runner = Mock(side_effect=AssertionError("stop script must not run"))
    ctl = object.__new__(Controller)
    ctl.vllm = vllm
    ctl.lock = SimpleNamespace(release=Mock())
    return vllm, ctl


def assert_kept(vllm, ctl):
    assert not ctl._stop_vllm()
    assert jsonio.read(vllm.record_path)["identity"] == OLD
    assert vllm._confirm_pending and vllm.leaving()
    assert PID in vllm._leaving
    assert vllm.state == "error" and vllm.error
    ctl.lock.release.assert_not_called()
    vllm.runner.assert_not_called()


@pytest.mark.parametrize(
    "error", [OSError("unavailable"), subprocess.CalledProcessError(1, "nvidia-smi")]
)
def test_failed_gpu_query_is_unknown(model, monkeypatch, error):
    vllm, ctl = model
    monkeypatch.setattr(gpumgr, "compute_apps", Mock(side_effect=error))
    assert_kept(vllm, ctl)


@pytest.mark.parametrize(
    "text",
    [
        "bad row",
        "123, engine",
        "bad, engine, 20",
        "123, engine, 20\nbad, engine, 20",
        "0, engine, 20",
    ],
)
def test_strict_gpu_scan_rejects_incomplete_pid_rows(text):
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, text, ""))
    with pytest.raises(ValueError):
        gpumgr.compute_apps(runner=runner)


def test_status_gpu_scan_keeps_its_loose_interface():
    assert gpumgr.gpu_holders(runner=Mock(side_effect=OSError())) == []
    runner = Mock(
        return_value=subprocess.CompletedProcess([], 0, "bad\n123, engine, N/A", "")
    )
    assert gpumgr.gpu_holders(runner=runner)[0]["pid"] == 123


@pytest.mark.parametrize("which", ["pid", "boot", "group_root", "group_member"])
def test_unreadable_process_evidence_never_releases(model, monkeypatch, which):
    vllm, ctl = model
    real_read = gpumgr.Path.read_text
    real_scan = gpumgr.os.scandir

    def read(path, *args, **kwargs):
        if which == "pid" and str(path) == f"/proc/{PID}/stat":
            raise PermissionError("pid unknown")
        if which == "boot" and str(path) == f"/proc/{PID}/stat":
            return f"{PID} (engine) S 1 {PID} " + "0 " * 16 + "100"
        if which == "boot" and str(path).endswith("/boot_id"):
            raise PermissionError("boot unknown")
        if which == "group_member" and str(path) == "/proc/123/stat":
            raise PermissionError("member unknown")
        return real_read(path, *args, **kwargs)

    def scan(path):
        if str(path) == "/proc":
            if which == "group_root":
                raise PermissionError("group unknown")
            if which == "group_member":
                return contextlib.nullcontext(iter([SimpleNamespace(name="123")]))
        return real_scan(path)

    monkeypatch.setattr(gpumgr.Path, "read_text", read)
    monkeypatch.setattr(gpumgr.os, "scandir", scan)
    assert_kept(vllm, ctl)


def test_pid_reuse_does_not_signal_and_needs_gpu_proof(model, monkeypatch):
    vllm, ctl = model
    monkeypatch.setattr(
        gpumgr, "identity_strict", lambda pid: {"start_ticks": "200", "boot": "boot"}
    )
    monkeypatch.setattr(gpumgr, "compute_apps", Mock(side_effect=OSError()))
    kill = Mock(side_effect=AssertionError("reused PID signalled"))
    monkeypatch.setattr(gpumgr.os, "killpg", kill)
    assert_kept(vllm, ctl)
    kill.assert_not_called()


def test_pending_retry_checks_group_and_then_recovers(model, monkeypatch):
    vllm, ctl = model
    groups = Mock(return_value=[PID + 1])
    monkeypatch.setattr(gpumgr, "_group_alive", groups)
    assert_kept(vllm, ctl)
    assert not ctl._stop_vllm()
    assert groups.call_count >= 2
    groups.return_value = []
    assert ctl._stop_vllm()
    assert not vllm.record_path.exists()
    assert not vllm._confirm_pending and not vllm._leaving
    ctl.lock.release.assert_called_once()


def test_adopt_and_start_keep_unknown_owner(model, monkeypatch):
    vllm, ctl = model
    real = gpumgr.Path.read_text

    def denied(path, *args, **kwargs):
        if str(path) == f"/proc/{PID}/stat":
            raise PermissionError("identity unknown")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(gpumgr.Path, "read_text", denied)
    vllm._adopt()
    assert jsonio.read(vllm.record_path)["identity"] == OLD
    launch = Mock(side_effect=AssertionError("unresolved owner launched over"))
    vllm.popen = launch
    assert not vllm.start(vllm.config.vllm_profile())
    launch.assert_not_called()
    assert_kept(vllm, ctl)


def test_pending_survives_supervisor_restart(model, monkeypatch):
    vllm, ctl = model
    monkeypatch.setattr(gpumgr, "_group_alive", lambda pid: [PID + 1])
    assert_kept(vllm, ctl)
    again = gpumgr.Vllm(vllm.config)
    assert again.leaving() and PID + 1 in again._leaving
    assert jsonio.read(again.record_path)["identity"] == OLD
    assert not again.start(again.config.vllm_profile())


def test_poll_second_identity_unknown_does_not_unlink(model, monkeypatch):
    vllm, _ = model
    vllm.mine = Mock(return_value=True)
    vllm._healthy = Mock(return_value=False)
    vllm._pid = Mock(return_value=PID)
    monkeypatch.setattr(
        gpumgr, "identity_strict", Mock(side_effect=PermissionError("unknown"))
    )
    assert vllm.poll() == "error"
    assert jsonio.read(vllm.record_path)["identity"] == OLD
    assert vllm.leaving()


@pytest.mark.parametrize("text", ["{broken", "[]"])
def test_unreadable_record_blocks_start_and_stop(model, text):
    vllm, ctl = model
    vllm.record_path.write_text(text)
    vllm.popen = Mock(side_effect=AssertionError("launched"))
    assert not vllm.start(vllm.config.vllm_profile())
    assert not ctl._stop_vllm()
    assert vllm.record_path.read_text() == text
    assert vllm.leaving()
    ctl.lock.release.assert_not_called()


def test_pending_record_write_failure_cannot_clear_owner(model, monkeypatch):
    vllm, ctl = model
    monkeypatch.setattr(
        gpumgr, "compute_apps", Mock(side_effect=OSError("GPU unknown"))
    )
    monkeypatch.setattr(
        jsonio, "write", Mock(side_effect=PermissionError("record unwritable"))
    )
    assert_kept(vllm, ctl)
    assert not vllm.start(vllm.config.vllm_profile())


def test_shutdown_retains_unconfirmed_owner(model, monkeypatch):
    vllm, ctl = model
    monkeypatch.setattr(gpumgr, "compute_apps", Mock(side_effect=OSError()))
    ctl.wake = Mock()
    ctl.stop_online = Mock()
    ctl._stop_worker = Mock()
    ctl._stop_catalogue = Mock()
    ctl._note_gate = Mock()
    ctl.write_status = Mock()
    ctl.config = vllm.config
    ctl.shutdown()
    assert jsonio.read(vllm.record_path)["identity"] == OLD
    assert vllm.leaving()
    ctl.lock.release.assert_not_called()


def test_alive_descendant_inherits_cpu_flock_after_leader_exit(tmp_path, monkeypatch):
    """source=fake GPU; real CPU group and inherited scratch lock, no service."""
    config = Config()
    config.service.workspace = str(tmp_path)
    monkeypatch.setattr(gpumgr, "gpu_holders", list)
    monkeypatch.setattr(gpumgr, "compute_apps", list)
    monkeypatch.setattr(gpumgr, "STOP_CONFIRM_S", 0)
    lock = gpumgr.GpuLock(tmp_path / "scratch.lock")
    assert lock.acquire()
    contender = gpumgr.GpuLock(tmp_path / "scratch.lock")
    code = "import os,sys,time; p=os.fork(); print(p,flush=True) if p else None; sys.stdin.readline() if p else time.sleep(60)"
    leader = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
        pass_fds=(lock.fileno(),),
    )
    child = int(leader.stdout.readline())
    child_identity = gpumgr.identity(child)
    vllm = gpumgr.Vllm(config)
    record = {"pid": leader.pid, "identity": gpumgr.identity(leader.pid)}
    jsonio.write(vllm.record_path, record)
    vllm.runner = Mock(
        side_effect=AssertionError("CPU leader already gone; no stop script")
    )
    ctl = object.__new__(Controller)
    ctl.vllm, ctl.lock = vllm, lock
    try:
        leader.stdin.write("exit\n")
        leader.stdin.flush()
        leader.wait(timeout=5)
        assert not ctl._stop_vllm()
        assert child in vllm._leaving and not contender.acquire()
        lock.release()  # supervisor exit closes only its copy: child retains flock
        assert not contender.acquire()
        assert gpumgr.same_process(child, child_identity)
        os.kill(child, signal.SIGTERM)
        for _ in range(100):
            if gpumgr.identity(child) is None:
                break
            time.sleep(0.01)
        assert ctl._stop_vllm()
        assert contender.acquire()
        assert not vllm.record_path.exists()
    finally:
        if gpumgr.same_process(child, child_identity):
            os.kill(child, signal.SIGKILL)
        if leader.poll() is None:
            leader.terminate()
            leader.wait(timeout=5)
        leader.stdin.close()
        leader.stdout.close()
        lock.release()
        contender.release()


def test_error_not_mine_controller_path_cannot_release(model, monkeypatch):
    vllm, ctl = model
    monkeypatch.setattr(gpumgr, "compute_apps", Mock(side_effect=OSError("unknown")))
    ctl.config = vllm.config
    ctl.sessions = {}
    ctl.policy_up = False
    ctl.policy_changed_at = None
    ctl.started_at = time.time()
    ctl._waiting_since = {}
    ctl._check_resume = Mock()
    ctl._vllm_failed = Mock()
    vllm.state = "error"
    assert not ctl._gpu_step(time.time(), True)
    assert ctl.decision.code == "gpu_not_free" and vllm.leaving()
    ctl._vllm_failed.assert_not_called()
    ctl.lock.release.assert_not_called()


def test_only_proved_owner_is_signalled_when_pidfile_points_elsewhere(
    model, monkeypatch
):
    vllm, ctl = model
    state = {"alive": True}
    monkeypatch.setattr(
        gpumgr, "identity_strict", lambda pid: OLD if state["alive"] else None
    )
    monkeypatch.setattr(gpumgr, "_group_alive", lambda pid: [])
    vllm._pid = Mock(return_value=PID + 10)  # do not run a port/PID-file stop script
    calls = []

    def kill(pgid, sig):
        calls.append((pgid, sig))
        state["alive"] = False

    monkeypatch.setattr(gpumgr.os, "killpg", kill)
    assert ctl._stop_vllm()
    assert calls == [(PID, signal.SIGTERM)]
    vllm.runner.assert_not_called()


def test_unknown_identity_on_signal_recheck_never_signals(model, monkeypatch):
    vllm, ctl = model
    monkeypatch.setattr(
        gpumgr,
        "identity_strict",
        Mock(side_effect=[OLD, PermissionError("unknown"), PermissionError("unknown")]),
    )
    monkeypatch.setattr(gpumgr, "_group_alive", lambda pid: [])
    vllm._pid = Mock(return_value=PID + 10)
    kill = Mock(side_effect=AssertionError("unknown owner signalled"))
    monkeypatch.setattr(gpumgr.os, "killpg", kill)
    assert_kept(vllm, ctl)
    kill.assert_not_called()


def test_unlink_failure_keeps_stop_unconfirmed(model, monkeypatch):
    vllm, ctl = model
    real = gpumgr.Path.unlink

    def unlink(path, *args, **kwargs):
        if path == vllm.record_path:
            raise PermissionError("owner record cannot be removed")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(gpumgr.Path, "unlink", unlink)
    assert_kept(vllm, ctl)


def test_strict_gpu_scan_nonzero_runner_result_is_not_empty():
    runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", "error"))
    with pytest.raises(subprocess.CalledProcessError):
        gpumgr.compute_apps(runner)


def test_missing_pending_record_is_unknown(model):
    vllm, ctl = model
    vllm._confirm_pending = True
    vllm._leaving = {PID}
    vllm.record_path.unlink()
    assert not ctl._stop_vllm()
    assert not vllm.start(vllm.config.vllm_profile())
    assert vllm.leaving() and PID in vllm._leaving
    ctl.lock.release.assert_not_called()


def test_unreadable_record_retains_owner_and_blocks_launch(model, monkeypatch):
    vllm, ctl = model
    real = gpumgr.Path.read_text

    def read(path, *args, **kwargs):
        if path == vllm.record_path:
            raise PermissionError("record unknown")
        return real(path, *args, **kwargs)

    with monkeypatch.context() as records:
        records.setattr(gpumgr.Path, "read_text", read)
        vllm._adopt()
        assert not ctl._stop_vllm()
        assert not vllm.start(vllm.config.vllm_profile())
        assert vllm.leaving()
    assert jsonio.read(vllm.record_path)["identity"] == OLD
    ctl.lock.release.assert_not_called()
    vllm.runner.assert_not_called()


@pytest.mark.parametrize("which", ["not_held", "fd_unknown", "stat_unknown"])
def test_constructor_adoption_keeps_alive_owner_without_lock_proof(
    model, tmp_path, monkeypatch, which
):
    vllm, ctl = model
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    config = vllm.config
    config.gpu.lock_file = str(tmp_path / "adopt-scratch.lock")
    holder = gpumgr.GpuLock(config.gpu.lock_file)
    ctl.lock = gpumgr.GpuLock(config.gpu.lock_file)
    ctl.config = config
    ctl._say_lock = Mock()
    ctl.event = Mock()
    assert holder.acquire()
    owner = {"pid": sleeper.pid, "identity": gpumgr.identity(sleeper.pid)}
    assert owner["identity"]
    jsonio.write(vllm.record_path, owner)
    scan, stat = gpumgr.os.scandir, gpumgr.os.stat

    def scan_probe(path):
        if which == "fd_unknown" and str(path) == f"/proc/{sleeper.pid}/fd":
            raise PermissionError("fd identity unknown")
        return scan(path)

    def stat_probe(path, *args, **kwargs):
        if which == "stat_unknown" and str(path) == config.gpu.lock_file:
            raise PermissionError("lock inode unknown")
        return stat(path, *args, **kwargs)

    try:
        with monkeypatch.context() as probes:
            probes.setattr(gpumgr.os, "scandir", scan_probe)
            probes.setattr(gpumgr.os, "stat", stat_probe)
            ctl._adopt_vllm()
        assert jsonio.read(vllm.record_path)["identity"] == owner["identity"]
        assert vllm.leaving() and not ctl.lock.held
        assert sleeper.poll() is None
        assert not vllm.start(config.vllm_profile())
        vllm.runner.assert_not_called()
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)
        holder.release()
        ctl.lock.release()
