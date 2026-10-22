"""``levi/stt/manager.py`` against the stand-in worker of ``fake_worker.py``:
real processes, real pipes, no model. Each test ends by stopping its managers
and checks that no process of the worker's group, and no record in
``children``, is left."""

import hashlib
import os
import signal
import stat
import sys
import threading
import time

import fake_worker
import pytest
from conftest import scaled

from levi import children
from levi.stt import settings
from levi.stt.errors import SttError
from levi.stt.manager import Manager

WAV = bytes(range(256)) * 8
REQ = "req-00000001"
FAST = 0.05  # seconds, before scaling, between polls


def wait_until(check, seconds=5.0, what="the condition"):
    deadline = time.monotonic() + scaled(seconds)
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(FAST)
    raise AssertionError(f"timed out waiting for {what}")


def alive(pid):
    """Whether the process still exists and is not a zombie (``children.identity``
    also answers None in the moment a killed process loses its executable
    link, before it has exited)."""
    try:
        with open(f"/proc/{pid}/stat") as stat_file:
            return stat_file.read().rsplit(")", 1)[1].split()[0] not in "ZXx"
    except OSError:
        return False


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def make(tmp_path):
    built = []

    def build(script=(), *, start="ok", record=True, **options):
        folder = tmp_path / f"m{len(built)}"
        folder.mkdir()
        log = folder / "record.jsonl" if record else None
        manager = Manager(
            command=fake_worker.command(folder, script, start=start, record=log),
            installed=lambda: settings.WorkerState(True, True),
            cwd=folder,
            log_dir=folder / "log",
            children_state=tmp_path / "processes.json",
            **{"watch": False, "request_timeout": 5, "start_timeout": 5, **options},
        )
        manager.record = log
        built.append(manager)
        return manager

    yield build
    for manager in built:
        manager.stop()
    leftovers = []
    for manager in built:
        for row in fake_worker.records(manager.record) if manager.record else []:
            pid = row.get("pid") or row.get("grandchild")
            if pid and alive(pid):
                leftovers.append(pid)
    assert leftovers == [], "a worker process outlived its test"
    rows = children.listed(state=tmp_path / "processes.json")
    assert [r for r in rows if r["running"]] == []


def pids(manager):
    return [r["pid"] for r in fake_worker.records(manager.record) if "pid" in r]


def grandchildren(manager):
    return [
        r["grandchild"]
        for r in fake_worker.records(manager.record)
        if "grandchild" in r
    ]


# --------------------------------------------------------------- starting


def test_nothing_runs_and_nothing_is_created_until_the_first_request(make, tmp_path):
    manager = make()
    view = manager.status()
    assert view["state"] == "off" and view["reason"] is None
    assert view["idle_in_s"] is None
    assert manager.worker_pid() is None
    assert not (tmp_path / "m0" / "log").exists()
    assert fake_worker.records(manager.record) == []


def test_the_first_request_starts_the_worker_and_the_next_reuses_it(make):
    manager = make([{"text": "one"}, {"text": "two"}])
    first = manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    second = manager.transcribe("req-00000002", WAV)
    assert (first.heard, first.text, first.language) == (True, "one", "English")
    assert first.loaded_now is True and first.load_ms >= 0
    assert (second.text, second.loaded_now, second.load_ms) == ("two", False, 0)
    assert manager.worker_pid() == pid and len(pids(manager)) == 1
    assert manager.status()["state"] == "ready"


def test_the_worker_gets_the_exact_bytes_and_the_request_id(make):
    manager = make()
    manager.transcribe(REQ, WAV)
    sent = [r for r in fake_worker.records(manager.record) if r.get("op") == "request"]
    assert sent == [
        {
            "op": "request",
            "id": REQ,
            "bytes": len(WAV),
            "sha256": hashlib.sha256(WAV).hexdigest(),
        }
    ]


def test_a_large_upload_goes_through_the_pipe_whole(make):
    manager = make()
    big = os.urandom(settings.MAX_BYTES)
    manager.transcribe(REQ, big)
    sent = [r for r in fake_worker.records(manager.record) if r.get("op") == "request"]
    assert sent[0]["sha256"] == hashlib.sha256(big).hexdigest()


def test_a_not_heard_answer_is_a_result_not_an_error(make):
    manager = make([{"do": "heard_false", "reason": "low_confidence"}])
    answer = manager.transcribe(REQ, WAV)
    assert (answer.heard, answer.reason, answer.text, answer.language) == (
        False,
        "low_confidence",
        "",
        None,
    )
    assert manager.status()["state"] == "ready"


def test_a_blank_transcript_counts_as_empty(make):
    manager = make([{"text": "   "}])
    answer = manager.transcribe(REQ, WAV)
    assert (answer.heard, answer.reason) == (False, "empty")


def test_the_worker_is_started_as_the_contract_says(make, tmp_path, monkeypatch):
    for name in ("LEVI_UI_TOKEN", "HF_TOKEN", "LEVI_STT_SENTINEL_SECRET"):
        monkeypatch.setenv(name, "must-not-reach-the-worker")
    manager = make()
    manager.transcribe(REQ, WAV, "8-15")
    start = fake_worker.records(manager.record)[0]
    assert start["env"]["HF_HUB_OFFLINE"] == "1"
    assert start["env"]["TRANSFORMERS_OFFLINE"] == "1"
    assert start["env"]["OMP_NUM_THREADS"] == "2"
    assert start["env"]["LEVI_GPU_LOCK_FILE"] == ""  # set, and empty
    assert start["env"]["PYTHONUNBUFFERED"] == "1"
    assert start["env"]["LEVI_UI_TOKEN"] is None
    assert start["env"]["HF_TOKEN"] is None
    assert start["env"]["LEVI_STT_SENTINEL_SECRET"] is None
    argv = start["argv"]
    assert argv[argv.index("--cpu-affinity") + 1] == "8-15"
    assert start["cwd"] == str((tmp_path / "m0").resolve())


def test_stderr_goes_to_a_private_file_of_the_workspace_with_lengths_only(make):
    manager = make()
    manager.transcribe(REQ, WAV)
    folder = manager.record.parent / "log"
    log = folder / "worker.log"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert f"request bytes={len(WAV)}" in log.read_text()


def test_the_worker_runs_in_its_own_group_and_is_tracked(make, tmp_path):
    manager = make()
    manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    assert os.getpgid(pid) == pid != os.getpgid(0)
    rows = children.listed(state=tmp_path / "processes.json")
    assert [(r["pid"], r["kind"], r["running"]) for r in rows] == [(pid, "stt", True)]
    manager.release()
    assert children.listed(state=tmp_path / "processes.json") == []


def test_a_device_that_is_not_the_gpu_is_refused_for_a_real_model(make, monkeypatch):
    manager = make(start="cpu")
    with pytest.raises(SttError) as caught:
        manager.transcribe(REQ, WAV)
    assert caught.value.code == "failed"
    assert manager.status()["reason"]["code"] == "device_mismatch"
    assert not alive(pids(manager)[0])
    monkeypatch.setenv("LEVI_STT_MODEL", settings.FAKE_MODEL)
    assert make(start="cpu").transcribe(REQ, WAV).heard is False


def test_a_command_that_cannot_run_is_a_failed_start(tmp_path):
    manager = Manager(
        command=lambda affinity: [str(tmp_path / "no-such-python")],
        installed=lambda: settings.WorkerState(True, True),
        cwd=tmp_path,
        log_dir=tmp_path / "log",
        children_state=tmp_path / "processes.json",
        watch=False,
    )
    with pytest.raises(SttError) as caught:
        manager.transcribe(REQ, WAV)
    assert caught.value.code == "failed"
    view = manager.status()
    assert view["state"] == "failed" and view["reason"]["code"] == "start_failed"
    assert manager.release() is False and manager.status()["state"] == "off"


# --------------------------------------------------------------- one at a time


def test_a_second_request_is_refused_at_once_while_one_runs(make):
    manager = make([{"delay": 1.0, "text": "slow"}])
    outcome = {}

    def first():
        outcome["answer"] = manager.transcribe(REQ, WAV)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        wait_until(lambda: manager.status()["state"] == "busy", what="a busy worker")
        began = time.monotonic()
        with pytest.raises(SttError) as caught:
            manager.transcribe("req-00000002", WAV)
        assert caught.value.code == "busy"
        assert time.monotonic() - began < 0.5, "a refused caller must not wait"
    finally:
        thread.join(scaled(10))
    assert outcome["answer"].text == "slow"
    assert len(pids(manager)) == 1 and manager.status()["state"] == "ready"


def test_status_does_not_wait_for_a_request_in_flight(make):
    manager = make([{"delay": 1.0}])
    thread = threading.Thread(target=lambda: manager.transcribe(REQ, WAV))
    thread.start()
    try:
        wait_until(lambda: manager.status()["state"] == "busy", what="busy")
        began = time.monotonic()
        manager.status()
        assert time.monotonic() - began < 0.3
    finally:
        thread.join(scaled(10))


# --------------------------------------------------------------- deadlines


def test_a_slow_answer_kills_the_whole_group_and_the_next_request_starts_clean(make):
    manager = make(
        [{"do": "sleep", "seconds": 60, "grandchild": True}, {"text": "after"}],
        request_timeout=0.8,
    )
    with pytest.raises(SttError) as caught:
        manager.transcribe(REQ, WAV)
    assert caught.value.code == "timeout"
    (pid,), (child,) = pids(manager), grandchildren(manager)
    assert not alive(pid) and not alive(child), "the group must be killed whole"
    view = manager.status()
    assert view["state"] == "failed" and view["reason"]["code"] == "request_timeout"
    assert manager.worker_pid() is None
    answer = manager.transcribe("req-00000002", WAV)
    assert (answer.text, answer.loaded_now) == ("after", True)
    assert manager.worker_pid() != pid


def test_a_worker_that_never_says_ready_is_killed_at_the_start_timeout(make):
    manager = make(start="never", start_timeout=0.6)
    began = time.monotonic()
    with pytest.raises(SttError) as caught:
        manager.transcribe(REQ, WAV)
    assert caught.value.code == "timeout"
    assert 0.5 < time.monotonic() - began < scaled(6)
    assert not alive(pids(manager)[0])
    assert manager.status()["reason"]["code"] == "start_timeout"


def test_a_manifest_mismatch_is_reported_by_name(make):
    manager = make(start="manifest")
    with pytest.raises(SttError) as caught:
        manager.transcribe(REQ, WAV)
    assert caught.value.code == "manifest_mismatch"
    assert manager.status()["reason"]["code"] == "manifest_mismatch"
    assert not alive(pids(manager)[0])


def test_a_worker_that_exits_before_ready_is_a_failed_start(make):
    manager = make(start="crash")
    with pytest.raises(SttError) as caught:
        manager.transcribe(REQ, WAV)
    assert caught.value.code == "failed"
    assert manager.status()["reason"]["code"] == "worker_exited"


def test_a_worker_that_speaks_garbage_instead_of_ready_is_killed(make):
    manager = make(start="garbage")
    with pytest.raises(SttError):
        manager.transcribe(REQ, WAV)
    assert manager.status()["reason"]["code"] == "protocol_error"
    assert not alive(pids(manager)[0])


# --------------------------------------------------------------- crashes and desync


def test_a_crash_during_a_request_fails_it_and_the_next_one_restarts(make):
    manager = make([{"do": "crash"}, {"text": "back"}])
    with pytest.raises(SttError) as caught:
        manager.transcribe(REQ, WAV)
    assert caught.value.code == "failed"
    view = manager.status()
    assert view["state"] == "failed" and view["reason"]["code"] == "worker_exited"
    answer = manager.transcribe("req-00000002", WAV)
    assert (answer.text, answer.loaded_now) == ("back", True)
    assert manager.status()["reason"] is None and len(pids(manager)) == 2


def test_a_worker_killed_while_idle_shows_as_failed_and_restarts_on_the_next_request(
    make,
):
    manager = make([{"text": "one"}, {"text": "two"}])
    manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    os.kill(pid, signal.SIGKILL)
    wait_until(
        lambda: manager.status()["state"] == "failed", what="the crash to be noticed"
    )
    assert manager.status()["reason"]["code"] == "worker_exited"
    answer = manager.transcribe("req-00000002", WAV)
    assert answer.loaded_now is True and manager.worker_pid() != pid


def test_a_worker_found_dead_at_the_next_request_is_replaced_in_that_request(make):
    manager = make([{"text": "one"}, {"text": "two"}])
    manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    os.kill(pid, signal.SIGKILL)
    wait_until(lambda: not alive(pid), what="the kill")
    answer = manager.transcribe("req-00000002", WAV)
    assert (answer.text, answer.loaded_now) == ("two", True)


@pytest.mark.parametrize(
    "step",
    [
        {"do": "garbage"},
        {"do": "oversize"},
        {"do": "short"},
        {"do": "wrong_id"},
        {"do": "error", "code": "internal"},
        {"do": "error", "code": "no-such-code"},
    ],
    ids=[
        "garbage",
        "oversize-line",
        "short-read",
        "wrong-id",
        "internal",
        "unknown-code",
    ],
)
def test_a_protocol_break_kills_the_worker_and_the_next_request_is_clean(make, step):
    manager = make([step, {"text": "clean"}])
    with pytest.raises(SttError) as caught:
        manager.transcribe(REQ, WAV)
    assert caught.value.code == "failed"
    (pid,) = pids(manager)
    wait_until(lambda: not alive(pid), what="the worker to be killed")
    assert manager.status()["state"] == "failed"
    assert manager.transcribe("req-00000002", WAV).text == "clean"


@pytest.mark.parametrize("step", [{"do": "extra_line"}, {"do": "late_line"}])
def test_a_line_nobody_asked_for_gets_the_worker_replaced_before_the_next_request(
    make, step
):
    manager = make([step, {"text": "second"}])
    assert manager.transcribe(REQ, WAV).heard is True  # the answer itself was fine
    pid = manager.worker_pid()
    time.sleep(0.5)  # (the late line arrives)
    answer = manager.transcribe("req-00000002", WAV)
    assert (answer.text, answer.loaded_now) == ("second", True)
    wait_until(lambda: not alive(pid), what="the old worker to be killed")


def test_a_refusal_of_the_audio_keeps_the_worker(make):
    manager = make(
        [
            {"do": "error", "code": "audio_invalid", "message": "x"},
            {"do": "error", "code": "too_long"},
            {"text": "fine"},
        ]
    )
    with pytest.raises(SttError) as invalid:
        manager.transcribe(REQ, WAV)
    with pytest.raises(SttError) as long:
        manager.transcribe("req-00000002", WAV)
    assert (invalid.value.code, long.value.code) == ("audio_invalid", "audio_too_long")
    answer = manager.transcribe("req-00000003", WAV)
    assert (answer.text, answer.loaded_now) == ("fine", False)
    assert len(pids(manager)) == 1


def test_the_workers_own_words_never_reach_an_exception(make):
    manager = make([{"do": "error", "code": "audio_invalid", "message": "ZEBRAQUARTZ"}])
    with pytest.raises(SttError) as caught:
        manager.transcribe(REQ, WAV)
    assert "ZEBRAQUARTZ" not in repr(caught.value) + str(caught.value.extra)


def test_a_bug_of_ours_mid_request_retires_the_worker_and_still_raises(
    make, monkeypatch
):
    manager = make([{"text": "one"}, {"text": "two"}])

    def broken(event, request_id):
        raise RuntimeError("a bug")

    monkeypatch.setattr(Manager, "_answer", staticmethod(broken))
    with pytest.raises(RuntimeError):
        manager.transcribe(REQ, WAV)
    view = manager.status()
    assert view["state"] == "failed" and view["reason"]["code"] == "protocol_error"
    assert manager.worker_pid() is None
    assert not alive(pids(manager)[0])


# --------------------------------------------------------------- idle


def test_the_idle_worker_is_stopped_after_the_idle_period_by_the_clock(make):
    clock = Clock()
    manager = make([{"text": "a"}, {"text": "b"}], clock=clock, idle_seconds=120)
    manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    clock.now += 119
    assert manager.expire_idle() is False and alive(pid)
    assert 0 < manager.status()["idle_in_s"] <= 1.0
    clock.now += 2
    assert manager.expire_idle() is True
    assert not alive(pid)
    assert manager.status()["state"] == "off"
    assert manager.expire_idle() is False
    assert manager.transcribe("req-00000002", WAV).loaded_now is True


def test_a_request_resets_the_idle_period(make):
    clock = Clock()
    manager = make(clock=clock, idle_seconds=120)
    manager.transcribe(REQ, WAV)
    clock.now += 100
    manager.transcribe("req-00000002", WAV)
    clock.now += 100
    assert manager.expire_idle() is False
    clock.now += 21
    assert manager.expire_idle() is True


def test_the_idle_watcher_stops_the_worker_by_itself(make):
    manager = make(watch=True, idle_seconds=0.4)
    manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    wait_until(lambda: manager.status()["state"] == "off", what="the idle exit")
    wait_until(lambda: not alive(pid), what="the process to be gone")
    wait_until(
        lambda: not [t for t in threading.enumerate() if t.name == "levi-stt-idle"],
        what="the watcher thread to end",
    )


def test_the_idle_watcher_never_stops_a_request_in_flight(make):
    manager = make([{"delay": 1.2, "text": "kept"}], watch=True, idle_seconds=0.3)
    assert manager.transcribe(REQ, WAV).text == "kept"


def test_the_idle_watcher_notices_a_crash(make):
    manager = make(watch=True, idle_seconds=60)
    manager.transcribe(REQ, WAV)
    os.kill(manager.worker_pid(), signal.SIGKILL)
    wait_until(lambda: manager.status()["state"] == "failed", what="the crash")
    assert manager.worker_pid() is None


# --------------------------------------------------------------- release


def test_release_stops_the_worker_and_is_idempotent(make):
    manager = make()
    assert manager.release() is False
    manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    assert manager.release() is True
    assert not alive(pid) and manager.status()["state"] == "off"
    assert manager.release() is False and manager.release() is False
    assert manager.transcribe("req-00000002", WAV).loaded_now is True


def test_release_clears_a_shown_failure(make):
    manager = make([{"do": "crash"}])
    with pytest.raises(SttError):
        manager.transcribe(REQ, WAV)
    assert manager.status()["state"] == "failed"
    manager.release()
    assert manager.status()["state"] == "off"


def test_release_cancels_a_request_in_flight(make):
    manager = make([{"do": "sleep", "seconds": 60, "grandchild": True}])
    outcome = {}

    def request():
        try:
            manager.transcribe(REQ, WAV)
        except SttError as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=request)
    thread.start()
    try:
        wait_until(lambda: manager.status()["state"] == "busy", what="busy")
        assert manager.release() is True
    finally:
        thread.join(scaled(10))
    assert not thread.is_alive()
    assert outcome["error"].code == "failed"
    assert manager.status()["state"] == "off"
    assert not alive(pids(manager)[0]) and not alive(grandchildren(manager)[0])


def test_release_while_the_worker_loads_ends_the_start(make):
    manager = make(start="never", start_timeout=30)
    outcome = {}

    def request():
        try:
            manager.transcribe(REQ, WAV)
        except SttError as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=request)
    thread.start()
    try:
        wait_until(lambda: manager.status()["state"] == "loading", what="loading")
        assert manager.release() is True
    finally:
        thread.join(scaled(10))
    assert outcome["error"].code == "failed"
    assert manager.status()["state"] == "off"


def test_stop_is_release(make):
    manager = make()
    manager.transcribe(REQ, WAV)
    pid = manager.worker_pid()
    manager.stop()
    assert not alive(pid)


def test_the_python_of_the_tests_is_the_one_that_runs_the_stand_in(make):
    manager = make()
    manager.transcribe(REQ, WAV)
    exe = os.readlink(f"/proc/{manager.worker_pid()}/exe")
    assert os.path.realpath(exe) == os.path.realpath(sys.executable)
