"""``levi/stt/manager.py`` where two threads meet: a release in the middle of a
request, a worker that another thread is still ending, a policy deploy's hold,
the cold-start check and the gate guard. Same stand-in worker and fixture as
``test_manager.py`` (real processes and pipes, no model)."""

import os
import signal
import threading

import pytest
from conftest import scaled
from test_manager import REQ, WAV, alive, pids, wait_until
from test_manager import make as make_fixture

from levi import children
from levi.stt import manager as manager_module
from levi.stt.errors import SttError

# Reuse the process-owning fixture; pytest injects it by this module name.
make = make_fixture

# --------------------------------------------------------------- pipes (#6)


def test_a_release_between_lookup_and_request_never_writes_into_a_reused_fd(
    make, tmp_path
):
    """The worker is retired after the request found it and before it uses the
    pipes. Its descriptor numbers are free again, and the next file opened
    takes them; the request must fail without writing a byte into that file."""
    manager = make()
    manager.transcribe(REQ, WAV)  # a ready worker
    worker = manager._current()
    in_fd, out_fd = worker.in_fd, worker.out_fd
    opened = []
    original = manager._ready_worker

    def released_meanwhile(*args, **kwargs):
        found = original(*args, **kwargs)
        manager.release()  # another thread's release (or a deploy's hold)
        for index in range(256):
            fd = os.open(tmp_path / f"other-{index}", os.O_RDWR | os.O_CREAT, 0o600)
            opened.append(fd)
            if fd in (in_fd, out_fd) and {in_fd, out_fd} <= set(opened):
                break
        return found

    manager._ready_worker = released_meanwhile
    try:
        with pytest.raises(SttError):
            manager.transcribe("req-00000002", WAV)
        reused = [fd for fd in opened if fd in (in_fd, out_fd)]
        assert reused, "the test needs a file on a freed descriptor number"
        for fd in reused:
            assert os.fstat(fd).st_size == 0, "audio went into somebody's file"
    finally:
        for fd in opened:
            os.close(fd)
    assert manager.status()["state"] == "off"


# --------------------------------------------------------------- ending (#7)


def test_release_waits_for_a_worker_that_another_thread_is_ending(make, monkeypatch):
    manager = make()
    manager.transcribe(REQ, WAV)
    worker = manager._current()
    pid = worker.proc.pid
    slow = threading.Event()
    polite = manager_module.Manager._ask_shutdown

    def slow_shutdown(target):
        slow.set()
        threading.Event().wait(scaled(0.6))  # the process is still there meanwhile
        polite(target)

    monkeypatch.setattr(
        manager_module.Manager, "_ask_shutdown", staticmethod(slow_shutdown)
    )
    ending = threading.Thread(target=manager.release_idle)  # the idle exit
    ending.start()
    try:
        assert slow.wait(scaled(5))
        assert manager.status()["state"] == "off"  # already forgotten ...
        assert alive(pid)  # ... but not gone
        assert manager.release() is True  # waits for it
        assert not alive(pid), "release returned while the old worker held memory"
    finally:
        ending.join(scaled(10))


# --------------------------------------------------------------- the hold (#4/#7)


def test_inside_the_hold_a_running_worker_is_gone_and_none_starts(make):
    manager = make()
    manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    with manager.held_off():
        assert not alive(pid) and manager.status()["state"] == "off"
        with pytest.raises(SttError) as refused:
            manager.transcribe("req-00000002", WAV)
        assert refused.value.code == "held"
        assert len(pids(manager)) == 1, "nothing was spawned inside the hold"
    assert manager.transcribe("req-00000003", WAV).loaded_now is True


def test_a_hold_that_begins_while_a_worker_is_being_spawned_ends_that_worker(make):
    """The fence is checked again where the worker is published: a deploy that
    began after the first check and before the publication is seen there."""
    manager = make()
    build = manager._command

    def deploy_arrives(cpu_affinity):
        argv = build(cpu_affinity)
        with manager._lock:
            manager._holds += 1  # what held_off() does first
        return argv

    manager._command = deploy_arrives
    with pytest.raises(SttError) as refused:
        manager.transcribe(REQ, WAV)
    assert refused.value.code == "held"
    assert manager.worker_pid() is None and manager.status()["state"] == "off"
    with manager._lock:
        manager._holds -= 1


# --------------------------------------------------------------- may_start (#16)


def test_the_cold_start_check_runs_only_when_a_worker_has_to_start(make):
    manager = make()
    asked = []
    manager.transcribe(REQ, WAV, may_start=lambda: asked.append("cold"))
    manager.transcribe("req-00000002", WAV, may_start=lambda: asked.append("warm"))
    assert asked == ["cold"]


def test_a_worker_that_died_after_the_callers_check_is_restarted_only_if_allowed(make):
    manager = make()
    manager.transcribe(REQ, WAV)
    os.kill(manager.worker_pid(), signal.SIGKILL)
    wait_until(lambda: manager._current().proc.poll() is not None, what="the exit")

    class GateClosed(Exception):
        pass

    def refuse():
        raise GateClosed

    with pytest.raises(GateClosed):
        manager.transcribe("req-00000002", WAV, may_start=refuse)
    assert len(pids(manager)) == 1, "no worker was started past the refusal"
    assert manager.worker_pid() is None
    # the request lock was given back
    assert manager.transcribe("req-00000003", WAV).loaded_now is True


# --------------------------------------------------------------- spawn failures (#19)


def test_an_unexpected_error_after_popen_retires_the_worker(make, tmp_path):
    manager = make(watch=True)

    started = []

    def no_thread(worker):
        started.append(worker.proc.pid)
        raise RuntimeError("can't start new thread")

    manager._start_watcher = no_thread
    with pytest.raises(SttError) as failed:
        manager.transcribe(REQ, WAV)
    assert failed.value.code == "failed"
    assert manager.worker_pid() is None
    assert manager.status()["reason"]["code"] == "start_failed"
    (pid,) = started
    wait_until(lambda: not alive(pid), what="the worker to be gone")
    rows = children.listed(state=tmp_path / "processes.json")
    assert [r for r in rows if r["running"]] == []


# --------------------------------------------------------------- the gate guard (#20)


def test_the_guard_releases_a_loaded_worker_when_the_gate_closes(make):
    manager = make(watch=True, idle_seconds=60, guard_every=0.05)
    closed = threading.Event()
    manager.set_guard(closed.is_set)
    manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    threading.Event().wait(scaled(0.3))
    assert manager.worker_pid() == pid, "an open gate keeps the worker"
    closed.set()
    wait_until(lambda: manager.worker_pid() is None, what="the release")
    wait_until(lambda: not alive(pid), what="the worker to be gone")
    assert manager.status()["state"] == "off"


def test_a_guard_that_cannot_be_read_counts_as_closed(make):
    manager = make(watch=True, idle_seconds=60, guard_every=0.05)

    def broken():
        raise RuntimeError("status unreadable")

    manager.set_guard(broken)
    manager.transcribe(REQ, WAV)
    wait_until(lambda: manager.worker_pid() is None, what="the release")


def test_the_guard_never_releases_a_request_in_flight(make):
    manager = make([{"delay": 1.0, "text": "kept"}], watch=True, guard_every=0.05)
    manager.set_guard(lambda: True)
    assert manager.transcribe(REQ, WAV).text == "kept"


@pytest.mark.parametrize("hold_deploy", [False, True])
def test_release_waits_for_spawn_publication_before_entering(make, hold_deploy):
    manager = make()
    preparing, proceed, entered, finish = (threading.Event() for _ in range(4))
    command = manager._command
    errors = []

    def blocked_command(affinity):
        preparing.set()
        assert proceed.wait(scaled(5))
        return command(affinity)

    def transcribe():
        try:
            manager.transcribe(REQ, WAV)
        except SttError as error:
            errors.append(error.code)

    def deploy():
        if not hold_deploy:
            manager.release()
            assert manager.worker_pid() is None
            entered.set()
            return
        with manager.held_off():
            assert manager.worker_pid() is None
            entered.set()
            assert finish.wait(scaled(5))

    manager._command = blocked_command
    request = threading.Thread(target=transcribe)
    hold = threading.Thread(target=deploy)
    request.start()
    try:
        assert preparing.wait(scaled(5))
        hold.start()
        assert not entered.wait(scaled(0.1)), (
            "deploy entered before late Popen was fenced"
        )
        proceed.set()
        assert entered.wait(scaled(5))
        assert manager.worker_pid() is None
    finally:
        proceed.set()
        finish.set()
        request.join(scaled(10))
        if hold.ident is not None:
            hold.join(scaled(10))
    assert not request.is_alive() and not hold.is_alive()


def test_worker_constructor_failure_after_popen_closes_process_and_pipes(
    make, monkeypatch
):
    manager = make()
    created = []

    def broken(proc, _clock):
        created.append(proc)
        raise RuntimeError("cannot create worker wrapper")

    monkeypatch.setattr(manager_module, "_Worker", broken)
    with pytest.raises(SttError) as failed:
        manager.transcribe(REQ, WAV)
    assert failed.value.code == "failed"
    assert len(created) == 1
    proc = created[0]
    assert proc.poll() is not None
    assert proc.stdin.closed and proc.stdout.closed
    assert manager.worker_pid() is None
    assert manager.status()["reason"]["code"] == "start_failed"
