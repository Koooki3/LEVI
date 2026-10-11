"""Strict exit proofs: fake GPU sources, owned CPU processes and scratch flocks."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
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


def launch_controller(vllm, lock):
    ctl = object.__new__(Controller)
    ctl.config, ctl.vllm, ctl.lock = vllm.config, vllm, lock
    ctl.sessions, ctl._waiting_since, ctl._standby_since = {}, {}, {}
    ctl.policy_up, ctl.policy_changed_at = False, None
    ctl.started_at, ctl.next_start_at = time.time() - 1000, 0
    ctl._check_resume, ctl._vllm_failed = Mock(), Mock()
    ctl.event, ctl._say_lock = Mock(), Mock()
    ctl.attention = None
    ctl.yield_state = SimpleNamespace(active=False)
    ctl.free_mib = lambda now: 32607
    ctl._vram, ctl._policy_mib = (0, {"total_mib": 32607}), (0, None)
    return ctl


@pytest.mark.parametrize(
    "case",
    [
        "identity_permission",
        "launch_timeout",
        "missing_pidfile",
        "script_nonzero",
        "record_write_failure",
        "first_record_write_failure",
    ],
)
def test_failed_launch_retains_ownership_through_controller(
    tmp_path, monkeypatch, case
):
    """Review counterexamples: real owned CPU child, scratch flock, fake GPU."""
    config = Config()
    config.service.workspace = str(tmp_path)
    config.vllm.pid_dir = str(tmp_path / "pids")
    config.vllm.script = "/fake-launcher"
    config.vllm.stop_script = "/never-executed"
    config.vllm_pid_dir.mkdir()
    lock, contender = (
        gpumgr.GpuLock(tmp_path / "scratch.lock"),
        gpumgr.GpuLock(tmp_path / "scratch.lock"),
    )
    assert lock.acquire()
    state = {}
    real_read, real_write = gpumgr.Path.read_text, jsonio.write

    def launcher(argv, **kwargs):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(120)"],
            start_new_session=True,
            pass_fds=kwargs.get("pass_fds", ()),
        )
        state["child"], state["identity"] = child, gpumgr.identity_strict(child.pid)
        if case != "missing_pidfile":
            vllm._pidfile().write_text(str(child.pid))
        process = Mock()
        if case == "launch_timeout":
            process.wait.side_effect = subprocess.TimeoutExpired(argv, 120)
        else:
            process.wait.return_value = 1 if case == "script_nonzero" else 0
        return process

    def read(path, *args, **kwargs):
        if (
            case == "identity_permission"
            and state.get("child")
            and str(path) == f"/proc/{state['child'].pid}/stat"
        ):
            raise PermissionError("fake owner identity unreadable")
        return real_read(path, *args, **kwargs)

    def write(path, value, **kwargs):
        if Path(path) == vllm.record_path and (
            case == "first_record_write_failure"
            or (case == "record_write_failure" and value.get("pid"))
        ):
            raise PermissionError("fake owner record write denied")
        return real_write(path, value, **kwargs)

    vllm = gpumgr.Vllm(
        config,
        popen=Mock(side_effect=launcher),
        runner=Mock(side_effect=AssertionError("actual stop script")),
    )
    ctl = launch_controller(vllm, lock)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(gpumgr, "healthy", lambda port: False)
            patch.setattr(gpumgr, "script_env", lambda config: {})
            patch.setattr(gpumgr.Path, "read_text", read)
            patch.setattr(jsonio, "write", write)
            patch.setattr(
                gpumgr,
                "compute_apps",
                Mock(side_effect=OSError("explicit fake GPU unavailable")),
            )
            patch.setattr(gpumgr, "STOP_CONFIRM_S", 0)
            patch.setattr(
                gpumgr.os,
                "killpg",
                Mock(side_effect=AssertionError("unconfirmed child signalled")),
            )
            assert not ctl._gpu_step(time.time(), True)
            assert not ctl._stop_vllm()
            assert lock.held and vllm.leaving()
            ctl._vllm_failed.assert_not_called()
            count = vllm.popen.call_count
            assert not vllm.start(config.vllm_profile(), fd=lock.fileno())
            assert vllm.popen.call_count == count
            if case == "first_record_write_failure":
                assert count == 0 and "child" not in state
            else:
                assert state["child"].poll() is None and not contender.acquire()
                assert vllm.record_path.exists()
                if case != "missing_pidfile":
                    assert vllm._launch_record["pid"] == state["child"].pid
                if case not in ("identity_permission", "missing_pidfile"):
                    assert vllm._launch_record["identity"] == state["identity"]
            vllm.runner.assert_not_called()
            if (
                case != "first_record_write_failure"
            ):  # No child exists when the prelaunch write fails.
                again = gpumgr.Vllm(
                    config,
                    popen=Mock(side_effect=AssertionError("pending restart launched")),
                )
                assert again.leaving() and not again.start(config.vllm_profile())
            print(
                json.dumps(
                    {
                        "case": case,
                        "source": "owned CPU + fake GPU",
                        "child_pid": state.get("child").pid
                        if state.get("child")
                        else None,
                        "child_alive": bool(
                            state.get("child") and state["child"].poll() is None
                        ),
                        "pending": vllm.leaving(),
                        "stop_result": False,
                        "controller_lock_held": lock.held,
                        "launch_calls": count,
                        "actual_stop_scripts": vllm.runner.call_count,
                    }
                )
            )
        child = state.get("child")
        if child:
            child.terminate()
            child.wait(timeout=5)
        if case not in ("first_record_write_failure", "missing_pidfile"):
            with monkeypatch.context() as patch:
                patch.setattr(
                    gpumgr, "compute_apps", list
                )  # explicit fake complete scan
                patch.setattr(gpumgr, "STOP_CONFIRM_S", 0)
                assert ctl._stop_vllm() and not lock.held
                assert not vllm.record_path.exists() and not vllm.leaving()
    finally:
        child = state.get("child")
        if child and gpumgr.process_state(child.pid, state["identity"]) == "alive":
            child.terminate()
        if child:
            child.wait(timeout=5)
        lock.release()
        assert contender.acquire()
        contender.release()


def test_yield_does_not_exempt_a_changed_pidfile(tmp_path, monkeypatch):
    """Two real CPU owners with explicitly fake GPU usage on the foreign one."""
    config = Config()
    config.service.workspace = str(tmp_path)
    config.vllm.pid_dir = str(tmp_path)
    owned, foreign = [
        subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(120)"],
            start_new_session=True,
        )
        for _ in range(2)
    ]
    identities = [gpumgr.identity_strict(proc.pid) for proc in (owned, foreign)]
    try:
        jsonio.write(
            config.live_dir / "vllm.json", {"pid": owned.pid, "identity": identities[0]}
        )
        pidfile = tmp_path / f"vllm_{config.vllm.port}.pid"
        pidfile.write_text(str(owned.pid))
        monkeypatch.setattr(gpumgr, "healthy", lambda port: False)
        vllm = gpumgr.Vllm(config)
        ctl = launch_controller(vllm, SimpleNamespace(held=True))
        ctl._ports_now, ctl._ports_error, ctl._apps = set(), "", (0, None, "")
        ctl.probes = SimpleNamespace(
            apps=lambda: [
                {"pid": foreign.pid, "name": "fake foreign GPU", "memory_mib": 20000}
            ]
        )
        pidfile.write_text(str(foreign.pid))
        with pytest.raises(ValueError, match="pid file"):
            ctl._our_gpu_pids()
        assert [reason.code for reason in ctl._yield_reasons(time.time(), True)] == [
            "probe_error"
        ]
        pidfile.write_text(str(owned.pid))
        assert (
            owned.pid in ctl._our_gpu_pids() and foreign.pid not in ctl._our_gpu_pids()
        )
        reasons = ctl._yield_reasons(time.time(), True)
        assert reasons
        print(
            json.dumps(
                {
                    "case": "yield_changed_pidfile",
                    "source": "two owned CPU processes + fake foreign GPU",
                    "recorded_pid": owned.pid,
                    "foreign_pid": foreign.pid,
                    "foreign_exempt": foreign.pid in ctl._our_gpu_pids(),
                    "mismatch_yield_reason": "probe_error",
                    "matching_file_yield_reasons": [reason.code for reason in reasons],
                }
            )
        )
    finally:
        for proc, ident in zip((owned, foreign), identities, strict=True):
            if gpumgr.process_state(proc.pid, ident) == "alive":
                proc.terminate()
            proc.wait(timeout=5)


def test_launch_preparation_failure_still_backs_off(tmp_path, monkeypatch):
    config = Config()
    config.service.workspace = str(tmp_path)
    config.vllm.script = "/never-launched"
    lock = gpumgr.GpuLock(tmp_path / "scratch.lock")
    vllm = gpumgr.Vllm(
        config, popen=Mock(side_effect=AssertionError("command must not run"))
    )
    ctl = launch_controller(vllm, lock)
    monkeypatch.setattr(gpumgr, "script_env", lambda config: {})
    monkeypatch.setattr(gpumgr, "healthy", lambda port: False)
    monkeypatch.setattr(
        gpumgr,
        "launch_prefix",
        Mock(side_effect=ValueError("invalid launch preparation")),
    )
    try:
        assert not ctl._gpu_step(time.time(), True)
        assert not vllm.leaving() and not lock.held and not vllm.record_path.exists()
        vllm.popen.assert_not_called()
        ctl._vllm_failed.assert_called_once()
    finally:
        lock.release()


def test_failed_launch_remembers_real_launcher_and_detached_server(
    tmp_path, monkeypatch
):
    config = Config()
    config.service.workspace = str(tmp_path)
    config.vllm.pid_dir = str(tmp_path)
    config.vllm.script = "/explicit-fake-launcher"
    config.vllm.stop_script = "/never-executed"
    lock, contender = (
        gpumgr.GpuLock(tmp_path / "scratch.lock"),
        gpumgr.GpuLock(tmp_path / "scratch.lock"),
    )
    assert lock.acquire()
    state = {}
    pidfile = tmp_path / f"vllm_{config.vllm.port}.pid"

    def launcher(argv, **kwargs):
        # Both are owned CPU sleepers. The server deliberately leaves the launcher's group.
        source = (
            "import os,time; from pathlib import Path; pid=os.fork(); "
            f"Path({str(pidfile)!r}).write_text(str(pid)) if pid else os.setsid(); "
            "time.sleep(120)"
        )
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", source],
            start_new_session=True,
            pass_fds=kwargs.get("pass_fds", ()),
        )
        state["launcher"] = process
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        state["server_pid"] = int(pidfile.read_text())
        state["launcher_identity"] = gpumgr.identity_strict(process.pid)
        state["server_identity"] = gpumgr.identity_strict(state["server_pid"])
        return SimpleNamespace(
            pid=process.pid, wait=Mock(side_effect=subprocess.TimeoutExpired(argv, 120))
        )

    vllm = gpumgr.Vllm(
        config,
        popen=launcher,
        runner=Mock(side_effect=AssertionError("stop script must not run")),
    )
    ctl = launch_controller(vllm, lock)
    try:
        monkeypatch.setattr(gpumgr, "script_env", lambda config: {})
        monkeypatch.setattr(gpumgr, "healthy", lambda port: False)
        monkeypatch.setattr(gpumgr, "compute_apps", list)  # explicit fake GPU scan
        monkeypatch.setattr(gpumgr, "STOP_CONFIRM_S", 0)
        assert not ctl._gpu_step(time.time(), True) and lock.held
        record = vllm._record()
        assert record["launcher"] == {
            "pid": state["launcher"].pid,
            "identity": state["launcher_identity"],
            "group": state["launcher"].pid,
        }
        assert (
            record["pid"] == state["server_pid"]
            and record["identity"] == state["server_identity"]
        )
        assert not ctl._stop_vllm() and not contender.acquire()
        assert (
            gpumgr.process_state(state["server_pid"], state["server_identity"])
            == "alive"
        )
        os.kill(state["server_pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while (
            gpumgr.process_state(state["server_pid"], state["server_identity"])
            == "alive"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert (
            not ctl._stop_vllm() and lock.held
        )  # launcher can still create descendants
        print(
            json.dumps(
                {
                    "case": "real_launcher_retained",
                    "source": "owned CPU launcher + detached CPU child + fake GPU",
                    "launcher": record["launcher"],
                    "server_pid": record["pid"],
                    "server_identity": record["identity"],
                    "server_exited": True,
                    "launcher_alive": state["launcher"].poll() is None,
                    "controller_lock_held": lock.held,
                }
            )
        )
        state["launcher"].terminate()
        state["launcher"].wait(timeout=5)
        assert ctl._stop_vllm() and not lock.held and contender.acquire()
        contender.release()
        vllm.runner.assert_not_called()
    finally:
        server_pid = state.get("server_pid")
        if (
            server_pid
            and gpumgr.process_state(server_pid, state["server_identity"]) == "alive"
        ):
            os.kill(server_pid, signal.SIGTERM)
        process = state.get("launcher")
        if process:
            if (
                gpumgr.process_state(process.pid, state.get("launcher_identity"))
                == "alive"
            ):
                process.terminate()
            process.wait(timeout=5)
        lock.release()
        assert contender.acquire()
        contender.release()


def test_exited_launcher_pid_is_in_gpu_confirmation(model, monkeypatch):
    vllm, ctl = model
    record = vllm._record()
    record["launcher"] = {"pid": PID + 10, "identity": OLD, "group": PID + 10}
    jsonio.write(vllm.record_path, record)
    monkeypatch.setattr(gpumgr, "compute_apps", lambda: [{"pid": PID + 10}])
    assert_kept(vllm, ctl)
    assert PID + 10 in vllm._leaving
