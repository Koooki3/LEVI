"""Nothing LEVI starts outlives it: worker groups are terminated when the
service stops or reclaimed after it was killed, a paused run stops its model
request, and a hung SAM3 worker is stopped."""

import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from levi import children


def group(script="sleep 60 & sleep 60"):
    """A process group with a leader and a child, like a worker that forks."""
    process = subprocess.Popen(
        ["/bin/sh", "-c", script], start_new_session=True, stdin=subprocess.DEVNULL
    )
    for _ in range(50):
        if (
            len(
                children._members(
                    {"pid": process.pid, "identity": children.identity(process.pid)}
                )
            )
            >= 2
        ):
            break
        time.sleep(0.05)
    return process


def alive(row):
    return bool(children._members(row))


def test_stopping_the_service_terminates_its_groups_including_grandchildren():
    process = group()
    row = children.track(process, "sam3", "job-1")
    members = children._members(row)
    assert len(members) >= 2, "the leader and its child"
    stopped = children.stop_owned(grace=2)
    process.wait(timeout=5)
    assert [r["label"] for r in stopped] == ["job-1"]
    assert not alive(row), "the child dies with the leader"
    assert children.listed() == []


def test_a_killed_owner_leaves_groups_the_next_start_reclaims():
    mine = group()
    theirs = group()
    try:
        children.track(mine, "conversion", "live-owner")
        orphan = children.track(theirs, "sam3", "dead-owner")
        # The owner of this row was a service that no longer exists.
        rows = json.loads(children._path().read_text())
        for row in rows:
            if row["label"] == "dead-owner":
                row["owner"]["identity"]["start_ticks"] = "0"
        children._path().write_text(json.dumps(rows))
        reclaimed = children.reclaim(grace=2)
        theirs.wait(timeout=5)
        assert [r["label"] for r in reclaimed] == ["dead-owner"]
        assert not alive(orphan)
        assert mine.poll() is None, "a live owner's worker is left alone"
        assert [r["label"] for r in children.listed()] == ["live-owner"]
    finally:
        children.stop_owned(grace=2)
        mine.wait(timeout=5)


def test_a_reused_pid_is_never_signalled():
    process = group("sleep 60")
    try:
        row = children.track(process, "pilot", "x")
        row["identity"] = {**row["identity"], "start_ticks": str(10**12)}
        assert children.terminate(row, grace=0.5) is False
        assert process.poll() is None
    finally:
        os.killpg(process.pid, 9)
        process.wait()


def test_the_service_lifespan_stops_what_it_started(client):
    process = group()
    row = children.track(process, "sam3", "during-service")
    with client:
        pass  # entering and leaving runs the lifespan once more
    process.wait(timeout=10)
    assert not alive(row)


# --------------------------------------------------------------- model request


class SlowModel(http.server.BaseHTTPRequestHandler):
    """An Ollama that takes a minute to answer."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        time.sleep(60)

    def log_message(self, *args):
        pass


@pytest.fixture
def slow_model():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowModel)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_pausing_a_run_cuts_its_model_request(slow_model):
    from levi.inference.ollama import OllamaError
    from levi.inference.transport import OllamaTransport, abort, requests_of

    class Config:
        base_url = slow_model
        allow_localhost = True

    outcome = {}

    def call():
        with requests_of("run-1"):
            began = time.monotonic()
            try:
                OllamaTransport(Config(), timeout=120).request(
                    "POST", "/api/chat", {"model": "m"}
                )
            except OllamaError as exc:
                outcome["error"] = str(exc)
            outcome["seconds"] = time.monotonic() - began

    worker = threading.Thread(target=call)
    worker.start()
    for _ in range(100):
        time.sleep(0.02)
        if abort("run-1"):
            break
    else:
        pytest.fail("the request never connected")
    worker.join(timeout=10)
    assert "paused or cancelled" in outcome["error"]
    assert outcome["seconds"] < 5
    assert abort("run-2") == 0, "other runs are not touched"


def test_a_request_after_the_stop_is_cut_at_once(slow_model):
    from levi.inference.ollama import OllamaError
    from levi.inference.transport import _OWNER, OllamaTransport, abort

    class Config:
        base_url = slow_model
        allow_localhost = True

    # Stopped between the control check and the connection.
    token = _OWNER.set("run-3")
    try:
        abort("run-3")
        began = time.monotonic()
        with pytest.raises(OllamaError, match="paused or cancelled"):
            OllamaTransport(Config(), timeout=120).request(
                "POST", "/api/chat", {"model": "m"}
            )
        assert time.monotonic() - began < 5
    finally:
        _OWNER.reset(token)


def test_keep_alive_is_explicit_and_configurable(monkeypatch):
    from levi.inference.ollama import keep_alive

    assert keep_alive() == "2m"
    monkeypatch.setenv("LEVI_OLLAMA_KEEP_ALIVE", "0")
    assert keep_alive() == 0
    monkeypatch.setenv("LEVI_OLLAMA_KEEP_ALIVE", "30s")
    assert keep_alive() == "30s"


# --------------------------------------------------------------------- SAM3


def test_a_sam3_worker_without_progress_is_stopped(tmp_path, monkeypatch):
    from backend import app

    monkeypatch.setenv("LEVI_SAM3_STALL_SECONDS", "1")
    log = tmp_path / "job.log"
    log.write_text("loading\n")
    job_path = tmp_path / "job.json"
    job = {
        "job_id": "20260923-120000-000",
        "status": "running",
        "progress_path": str(tmp_path / "job.progress.json"),
        "log_path": str(log),
    }
    job_path.write_text(json.dumps(job))
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    children.track(process, "sam3", job["job_id"])
    watcher = threading.Thread(target=app._watch_sam3, args=(process, job_path))
    watcher.start()
    watcher.join(timeout=20)
    assert not watcher.is_alive()
    assert process.poll() is not None
    value = json.loads(job_path.read_text())
    assert value["status"] == "failed"
    assert "no progress" in value["error"]
    assert children.listed() == []


def test_a_finished_sam3_worker_is_left_to_its_job(tmp_path):
    from backend import app

    job_path = tmp_path / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "status": "running",
                "progress_path": str(tmp_path / "p.json"),
                "log_path": str(tmp_path / "l.log"),
            }
        )
    )
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    app._watch_sam3(process, job_path)
    assert json.loads(job_path.read_text())["status"] == "running"


def test_what_a_finished_leader_started_is_stopped_when_it_is_forgotten():
    # The leader exits shortly after starting; its child keeps running in the
    # group. (It must still be alive when it is tracked, as a real worker is.)
    process = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 60 & sleep 0.5; exit 0"], start_new_session=True
    )
    row = children.track(process, "pilot", "adapter")
    process.wait(timeout=5)
    left = children._members(row)
    assert left, "the orphaned child is still in the group"
    children.untrack(process.pid, grace=2)
    assert not children._members(row)
    assert children.listed() == []


def test_a_reused_leader_pid_protects_its_new_group():
    process = group("sleep 60")
    try:
        row = children.track(process, "sam3", "x")
        stale = {**row, "identity": {**row["identity"], "start_ticks": "1"}}
        # A live process holds the PID but is not the recorded one.
        assert children._members(stale) == []
    finally:
        os.killpg(process.pid, 9)
        process.wait()
