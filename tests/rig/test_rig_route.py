"""``GET /api/levi/rig/status`` (``levi/rig/api.py``): the exact schema, a
part that fails or hangs never fails the others, the short result cache and
the wait for a read in flight, no socket is opened, no ``/proc/<pid>`` path is
read, and nothing is written. The machine is fake: a temporary hub folder,
rollout root and ``systemctl``, a manual monotonic clock, and the setup
probes' answer is injected, so no process, port or device is looked at."""

import builtins
import functools
import io
import json
import os
import re
import socket
import subprocess
import threading
import time

import pytest
from conftest import scaled
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_rig_status import assert_schema, write_session
from test_setup_probes import FakeProc, probe

from levi.rig import api
from levi.rig import status as S
from levi.setup import probes

REAL_STATUS = probes.status  # the shipped function, before any test replaces it
# How long a wait for another thread may take before the test calls it hung;
# generous on purpose, it never decides a result.
PATIENCE = scaled(30)
ROUTE = "/api/levi/rig/status"
UNITS_TEXT = (
    "levi-aeri-policy-pi05.service loaded active running LEVI AERI policy pi05\n"
    "levi-aeri-run-7.service loaded failed failed LEVI AERI run 7\n"
)
QUIET_GUARD = {"state": "ok", "quiet": True, "robot": [], "truncated": False}


class Clock:
    """The route's monotonic clock, moved by hand."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Rig:
    """The fake machine behind the route."""

    def __init__(self, tmp_path, monkeypatch):
        self.hub = tmp_path / "hub"
        self.hub.mkdir()
        self.marker = self.hub / ".arm-controller-active"
        self.root = tmp_path / "rollouts"
        self.root.mkdir()
        self.units_text = ""
        self.guard = dict(QUIET_GUARD)
        self.systemctl_calls = []
        self.probe_calls = 0
        self.marker_calls = 0
        self.roots_calls = 0
        self.clock = Clock()
        # The reads of earlier tests (and their cached results) are not ours.
        monkeypatch.setattr(api, "_READS", {})
        monkeypatch.setattr(api, "monotonic", self.clock)
        monkeypatch.setattr(api, "marker_path", self.marker_path)
        monkeypatch.setattr(api, "rollout_roots", self.rollout_roots)
        monkeypatch.setattr(S, "run_systemctl", self.systemctl)
        monkeypatch.setattr(probes, "status", self.probes_status)

    def marker_path(self):
        self.marker_calls += 1
        return self.marker

    def rollout_roots(self):
        self.roots_calls += 1
        return [str(self.root)], None

    def systemctl(self, argv, timeout):
        self.systemctl_calls.append(list(argv))
        return 0, self.units_text

    def probes_status(self):
        self.probe_calls += 1
        return {
            "sampled_at": time.time(),
            "guard": self.guard,
            "cached": False,
            "age_s": 0.0,
        }

    @property
    def client(self):
        app = FastAPI()
        app.include_router(api.router)
        return TestClient(app)

    def get(self):
        return self.client.get(ROUTE)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_a_quiet_rig_answers_the_contract(rig):
    reply = rig.get()
    assert reply.status_code == 200
    doc = reply.json()
    assert_schema(doc)
    assert doc["marker"] == {"state": "absent", "age_s": None, "detail": None}
    assert doc["eval_sessions"] == {
        "state": "ok",
        "roots": 1,
        "sessions": [],
        "detail": None,
    }
    assert doc["units"] == {"state": "ok", "items": [], "detail": None}
    assert doc["robot_idle"] == {"state": "idle", "reasons": []}
    assert rig.probe_calls == 1 and len(rig.systemctl_calls) == 1


def test_a_busy_rig_names_every_source(rig):
    now = time.time()
    rig.marker.write_text("")
    os.utime(rig.marker, (now - 120, now - 120))
    write_session(rig.root, "g1__plates", now, 3, state="running")
    rig.units_text = UNITS_TEXT
    rig.guard = {**QUIET_GUARD, "robot": [{"pid": 1, "name": "franka_server"}]}
    doc = rig.get().json()
    assert_schema(doc)
    assert doc["marker"]["state"] == "present"
    assert doc["marker"]["age_s"] == pytest.approx(120.0, abs=2)
    assert doc["eval_sessions"]["sessions"][0]["group"] == "g1"
    assert [u["name"] for u in doc["units"]["items"]] == [
        "levi-aeri-policy-pi05.service",
        "levi-aeri-run-7.service",
    ]
    assert doc["robot_idle"]["state"] == "busy"
    assert [r["source"] for r in doc["robot_idle"]["reasons"]] == [
        "guard",
        "marker",
        "eval_sessions",
        "units",
    ]


def test_the_route_is_read_only_by_method(rig):
    assert rig.client.post(ROUTE).status_code == 405
    assert rig.client.put(ROUTE).status_code == 405
    assert rig.client.delete(ROUTE).status_code == 405


def failing(exc):
    def boom(*args, **kwargs):
        raise exc

    return boom


@pytest.mark.parametrize(
    "owner, name, part, code",
    [
        (api, "marker_path", "marker", "marker.unreadable"),
        (api, "rollout_roots", "eval_sessions", "sessions.unreadable"),
        (S, "run_systemctl", "units", "units.unreadable"),
    ],
)
@pytest.mark.parametrize("exc", [RuntimeError("boom"), OSError(5, "I/O error")])
def test_a_part_that_fails_is_unknown_and_the_others_still_answer(
    rig, monkeypatch, owner, name, part, code, exc
):
    monkeypatch.setattr(owner, name, failing(exc))
    reply = rig.get()
    assert reply.status_code == 200
    doc = reply.json()
    assert_schema(doc)
    assert doc[part]["state"] == "unknown" and doc[part]["detail"]
    healthy = {"marker", "eval_sessions", "units"} - {part}
    assert all(doc[p]["state"] in ("absent", "ok") for p in healthy)
    assert doc["robot_idle"]["state"] == "unknown"
    assert [r["source"] for r in doc["robot_idle"]["reasons"]] == [part]
    assert [r["code"] for r in doc["robot_idle"]["reasons"]] == [code]


@pytest.mark.parametrize(
    "answer",
    [
        RuntimeError("probes broke"),
        {"state": "sampling", "detail": "another request is sampling"},
        {
            "guard": {"state": "unknown", "detail": "probe failed: OSError"},
            "age_s": 0.0,
        },
        ["not", "a", "dict"],
        None,
    ],
)
def test_a_bad_setup_probe_answer_makes_the_guard_unknown(rig, monkeypatch, answer):
    def status():
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(probes, "status", status)
    doc = rig.get().json()
    assert_schema(doc)
    assert doc["robot_idle"]["state"] == "unknown"
    assert [r["source"] for r in doc["robot_idle"]["reasons"]] == ["guard"]
    assert [r["code"] for r in doc["robot_idle"]["reasons"]] == ["guard.unreadable"]
    assert doc["marker"]["state"] == "absent"


@pytest.mark.parametrize(
    "extra",
    [
        {"stale": True},
        {"stale": True, "age_s": 0.5},
        {"stale": True, "age_s": 900},
        {"age_s": 900.0},
        {"age_s": "not a number"},
        {"age_s": float("nan")},
        {"age_s": True},
        # An answer that does not say how old it is may be any age.
        {},
        {"age_s": None},
    ],
)
def test_a_stale_setup_sample_makes_the_guard_unknown(rig, monkeypatch, extra):
    # probes.status() hands back its previous sample (stale=True) when the sampler
    # is still busy; an old quiet guard must not read as a quiet robot now.
    monkeypatch.setattr(
        probes, "status", lambda: {"guard": dict(QUIET_GUARD), "cached": True, **extra}
    )
    doc = rig.get().json()
    assert_schema(doc)
    assert doc["robot_idle"]["state"] == "unknown"
    assert [r["source"] for r in doc["robot_idle"]["reasons"]] == ["guard"]
    assert [r["code"] for r in doc["robot_idle"]["reasons"]] == ["guard.old_sample"]


def test_a_fresh_cached_setup_sample_is_accepted(rig, monkeypatch):
    monkeypatch.setattr(
        probes,
        "status",
        lambda: {"guard": dict(QUIET_GUARD), "cached": True, "age_s": 1.4},
    )
    assert rig.get().json()["robot_idle"] == {"state": "idle", "reasons": []}


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_read_that_dies_without_an_exception_still_gives_a_valid_part(
    rig, monkeypatch
):
    # SystemExit is not an Exception: the read thread ends without a value.
    monkeypatch.setattr(api, "marker_path", failing(SystemExit(3)))
    doc = rig.get().json()
    assert_schema(doc)
    assert doc["marker"]["state"] == "unknown" and doc["marker"]["detail"]
    assert doc["robot_idle"]["state"] == "unknown"


def test_everything_failing_at_once_still_answers(rig, monkeypatch):
    monkeypatch.setattr(api, "marker_path", failing(RuntimeError("a")))
    monkeypatch.setattr(api, "rollout_roots", failing(RuntimeError("b")))
    monkeypatch.setattr(S, "run_systemctl", failing(FileNotFoundError("c")))
    monkeypatch.setattr(probes, "status", failing(ValueError("d")))
    reply = rig.get()
    assert reply.status_code == 200
    doc = reply.json()
    assert_schema(doc)
    assert doc["robot_idle"]["state"] == "unknown"
    assert {r["source"] for r in doc["robot_idle"]["reasons"]} == {
        "guard",
        "marker",
        "eval_sessions",
        "units",
    }


def test_a_hung_part_times_out_and_is_not_started_twice(rig, monkeypatch):
    monkeypatch.setitem(api.LIMITS, "eval_sessions", 0.3)
    gate = threading.Event()
    calls = []

    def stuck():
        calls.append(1)
        gate.wait(PATIENCE)
        return [str(rig.root)], None

    monkeypatch.setattr(api, "rollout_roots", stuck)
    try:
        first = rig.get().json()
        assert_schema(first)
        assert first["eval_sessions"]["state"] == "unknown"
        assert "timed out" in first["eval_sessions"]["detail"]
        assert first["marker"]["state"] == "absent"
        # Past its limit the read counts as stuck: a new request answers for
        # that part at once and does not start another.
        rig.clock.advance(0.5)
        second = rig.get().json()
        assert second["eval_sessions"]["state"] == "unknown"
        assert "not finished" in second["eval_sessions"]["detail"]
        assert len(calls) == 1
    finally:
        gate.set()
    # The late read finishes within the cache time of its start: its result
    # is the answer, and nothing is read a second time.
    assert api._READS["eval_sessions"].done.wait(PATIENCE)
    third = rig.get().json()
    assert third["eval_sessions"]["state"] == "ok"
    assert len(calls) == 1
    # Once the cache time has passed a new request reads again.
    rig.clock.advance(api.CACHE_S["eval_sessions"] + 0.1)
    assert rig.get().json()["eval_sessions"]["state"] == "ok"
    assert len(calls) == 2


def test_a_part_is_stuck_only_after_its_limit_has_passed(rig, monkeypatch):
    monkeypatch.setitem(api.LIMITS, "eval_sessions", 0.4)
    gate = threading.Event()
    calls = []

    def stuck():
        calls.append(1)
        gate.wait(PATIENCE)
        return [str(rig.root)], None

    monkeypatch.setattr(api, "rollout_roots", stuck)
    try:
        first = rig.get().json()
        assert "timed out after 0.4 s" in first["eval_sessions"]["detail"]
        # Inside the limit (by the clock) the later request still waits for
        # the same read, for the time that is left of it: it ends in the wait's
        # own "timed out", not in the answer given to a part that is stuck.
        rig.clock.advance(0.2)
        second = rig.get().json()
        assert "timed out after 0.4 s" in second["eval_sessions"]["detail"]
        # After it, the part is stuck: answered from the record, no wait.
        rig.clock.advance(0.3)
        third = rig.get().json()
        assert "not finished" in third["eval_sessions"]["detail"]
        assert len(calls) == 1
    finally:
        gate.set()


def test_a_request_during_a_slow_read_waits_for_it_and_reuses_its_result(
    rig, monkeypatch
):
    monkeypatch.setitem(api.LIMITS, "units", PATIENCE)
    entered, gate, waiting = threading.Event(), threading.Event(), threading.Event()
    waits = []

    def slow(argv, timeout):
        rig.systemctl_calls.append(list(argv))
        entered.set()
        assert gate.wait(PATIENCE)
        return 0, UNITS_TEXT

    real_await = api._await

    def watched(name, task, fallback):
        # Seen from the inside: which read each request waits for.
        if name == "units":
            waits.append(task)
            if len(waits) == 2:
                waiting.set()
        return real_await(name, task, fallback)

    monkeypatch.setattr(S, "run_systemctl", slow)
    monkeypatch.setattr(api, "_await", watched)
    answers = []
    first = threading.Thread(target=lambda: answers.append(rig.get().json()))
    second = threading.Thread(target=lambda: answers.append(rig.get().json()))
    try:
        first.start()
        assert entered.wait(PATIENCE)
        second.start()
        assert waiting.wait(PATIENCE)  # both requests wait; the read is still held
        assert len(rig.systemctl_calls) == 1
    finally:
        gate.set()
    first.join(PATIENCE)
    second.join(PATIENCE)
    assert not first.is_alive() and not second.is_alive()
    assert len(answers) == 2 and waits[0] is waits[1]
    for doc in answers:
        assert doc["units"]["state"] == "ok" and len(doc["units"]["items"]) == 2
    assert len(rig.systemctl_calls) == 1


def test_sessions_and_units_are_reused_for_two_seconds(rig):
    now = time.time()
    rig.units_text = UNITS_TEXT
    first = rig.get().json()
    assert (
        len(first["units"]["items"]) == 2 and first["eval_sessions"]["sessions"] == []
    )
    # What changes inside the cache time is not seen yet, and nothing is read.
    rig.units_text = ""
    write_session(rig.root, "g1__t", now, 5)
    rig.clock.advance(1.9)
    again = rig.get().json()
    assert again["units"] == first["units"]
    assert again["eval_sessions"] == first["eval_sessions"]
    assert len(rig.systemctl_calls) == 1 and rig.roots_calls == 1
    rig.clock.advance(0.2)
    later = rig.get().json()
    assert later["units"]["items"] == []
    assert [r["group"] for r in later["eval_sessions"]["sessions"]] == ["g1"]
    assert len(rig.systemctl_calls) == 2 and rig.roots_calls == 2


def test_the_marker_and_the_guard_are_read_for_every_request(rig):
    assert rig.get().json()["marker"]["state"] == "absent"
    rig.marker.write_text("")
    doc = rig.get().json()
    assert doc["marker"]["state"] == "present"
    assert doc["robot_idle"]["state"] == "busy"
    assert rig.marker_calls == 2 and rig.probe_calls == 2


def test_a_failed_read_is_reused_too_and_does_not_fail_the_others(rig, monkeypatch):
    monkeypatch.setattr(S, "run_systemctl", failing(OSError(5, "I/O error")))
    first = rig.get().json()
    assert first["units"]["state"] == "unknown" and first["marker"]["state"] == "absent"
    rig.clock.advance(5)
    rig.units_text = UNITS_TEXT
    monkeypatch.setattr(S, "run_systemctl", rig.systemctl)
    assert rig.get().json()["units"]["state"] == "ok"


def test_a_thread_that_cannot_be_started_gives_unknown_parts_not_an_error(
    rig, monkeypatch
):
    real_start = threading.Thread.start
    refused = []

    def start(self):
        if self.name.startswith("levi-rig-"):
            refused.append(self.name)
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    reply = rig.get()
    assert reply.status_code == 200
    doc = reply.json()
    assert_schema(doc)
    assert sorted(refused) == [
        "levi-rig-eval_sessions",
        "levi-rig-guard",
        "levi-rig-marker",
        "levi-rig-units",
    ]
    for part in ("marker", "eval_sessions", "units"):
        assert doc[part]["state"] == "unknown"
        assert "RuntimeError" in doc[part]["detail"]
    assert doc["robot_idle"]["state"] == "unknown"
    assert {r["source"] for r in doc["robot_idle"]["reasons"]} == {
        "guard",
        "marker",
        "eval_sessions",
        "units",
    }
    # No read is left waiting for a thread that never ran.
    assert all(read.done.is_set() for read in api._READS.values())
    # The next request, with threads again, reads afresh once the short cache
    # of the failed read has passed.
    monkeypatch.setattr(threading.Thread, "start", real_start)
    rig.clock.advance(max(api.CACHE_S.values()) + 0.1)
    again = rig.get().json()
    assert again["units"]["state"] == "ok" and again["eval_sessions"]["state"] == "ok"
    assert again["robot_idle"] == {"state": "idle", "reasons": []}


def test_a_marker_whose_folder_is_missing_leaves_the_rig_unknown(rig):
    rig.hub.rmdir()
    doc = rig.get().json()
    assert_schema(doc)
    assert doc["marker"] == {
        "state": "unknown",
        "age_s": None,
        "detail": "the marker folder does not exist",
    }
    assert doc["robot_idle"]["state"] == "unknown"
    assert [r["code"] for r in doc["robot_idle"]["reasons"]] == ["marker.no_folder"]


def test_an_old_marker_leaves_the_rig_unknown_and_is_kept(rig):
    now = time.time()
    rig.marker.write_text("")
    os.utime(rig.marker, (now - 30 * 3600, now - 30 * 3600))
    doc = rig.get().json()
    assert_schema(doc)
    assert doc["marker"]["state"] == "present"
    assert doc["robot_idle"]["state"] == "unknown"
    assert [r["code"] for r in doc["robot_idle"]["reasons"]] == ["marker.old"]
    assert rig.marker.exists()


# ------------------------------------------------------------------ the real setup probes


@pytest.fixture
def real_probes(rig, tmp_path, monkeypatch):
    """``probes.status()`` as shipped, over a quiet fake /proc and a clock held
    by hand (the same helpers ``tests/test_setup_probes.py`` uses)."""
    proc = FakeProc(tmp_path / "proc")
    proc.process(7, "bash", ["bash"])
    proc.host()
    sampler = probe()
    context = probes.Context(proc=str(proc.root))
    monkeypatch.setattr(probes, "PROBES", sampler)
    monkeypatch.setattr(probes, "context_from_environment", lambda: context)
    monkeypatch.setattr(probes, "status", REAL_STATUS)
    return sampler


def test_the_real_setup_probes_answer_carries_the_age_the_route_relies_on(
    real_probes,
):
    fresh = probes.status()
    assert fresh["cached"] is False and fresh["age_s"] == 0.0 and "stale" not in fresh
    assert fresh["guard"]["state"] == "ok" and fresh["guard"]["quiet"] is True
    real_probes.clock.t += 1.0
    cached = probes.status()
    assert cached["cached"] is True and cached["age_s"] == 1.0
    assert "stale" not in cached
    assert not api._old_sample(fresh) and not api._old_sample(cached)
    assert api._guard() == cached["guard"]


def test_a_stale_answer_of_the_real_setup_probes_makes_the_guard_unknown(
    rig, real_probes, monkeypatch
):
    assert probes.status()["guard"]["quiet"] is True  # a sample to fall back on
    # Its cache has run out and another request holds the sampler: the next
    # request waits (shortened here) and is handed the old sample as stale.
    real_probes.clock.t += probes.DEFAULT_TTL_S + 1.0
    monkeypatch.setattr(probes, "WAIT_S", 0.05)
    with real_probes._cond:
        real_probes._sampling = True
    try:
        answer = probes.status()
        assert answer["stale"] is True and answer["guard"]["quiet"] is True
        # Its age alone would pass: only the stale flag says it is old.
        assert answer["age_s"] < api.GUARD_MAX_AGE_S
        guard = api._guard()
        assert guard["state"] == "unknown" and guard["code"] == "guard.old_sample"
        doc = api.collect()
        assert_schema(doc)
        assert doc["robot_idle"]["state"] == "unknown"
        assert [r["code"] for r in doc["robot_idle"]["reasons"]] == ["guard.old_sample"]
    finally:
        with real_probes._cond:
            real_probes._sampling = False
            real_probes._cond.notify_all()
    # The sampler is free: the next request takes a new sample and the quiet
    # guard counts again.
    assert api.collect()["robot_idle"] == {"state": "idle", "reasons": []}


# ------------------------------------------------------------------ safety


def test_no_socket_is_ever_opened(rig, monkeypatch):
    attempts = []

    def refuse(self, *args, **kwargs):
        attempts.append(args)
        raise AssertionError("the rig status route must not connect to anything")

    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    spawned = []

    def no_process(self, *args, **kwargs):
        spawned.append(args)
        raise AssertionError("no process may be started: systemctl is faked")

    monkeypatch.setattr(subprocess.Popen, "__init__", no_process)
    now = time.time()
    rig.marker.write_text("")
    write_session(rig.root, "g1__t", now, 5)
    rig.units_text = UNITS_TEXT
    doc = rig.get().json()
    assert_schema(doc)
    assert attempts == [] and spawned == []
    assert doc["robot_idle"]["state"] == "busy"


PID_PATH = re.compile(r"^/proc/\d+(/|$)")


@pytest.fixture
def path_spy(monkeypatch):
    """Every path handed to the file-system calls, from every thread."""
    seen = []

    def watch(owner, name):
        real = getattr(owner, name)

        @functools.wraps(real)
        def spy(*args, **kwargs):
            path = args[0] if args else kwargs.get("path")
            if isinstance(path, (str, bytes, os.PathLike)):
                seen.append(os.fsdecode(path))
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)

    for name in ("stat", "lstat", "open", "listdir", "scandir", "readlink", "access"):
        watch(os, name)
    watch(builtins, "open")
    watch(io, "open")
    return seen


def test_no_proc_pid_path_is_ever_read(rig, path_spy):
    # The spy sees such a path when one is touched (it would catch the
    # ``os.path.exists(f"/proc/{pid}")`` of ``levi.live.sessions``).
    os.path.exists(f"/proc/{os.getpid()}")
    assert any(PID_PATH.match(p) for p in path_spy)
    path_spy.clear()
    now = time.time()
    rig.marker.write_text("")
    # A session that names this very process: a careless liveness check
    # would look at its /proc entry.
    write_session(
        rig.root,
        "g1__t",
        now,
        5,
        pid=os.getpid(),
        host=socket.gethostname(),
    )
    rig.units_text = UNITS_TEXT
    doc = rig.get().json()
    assert_schema(doc)
    assert doc["eval_sessions"]["sessions"][0]["group"] == "g1"
    assert [p for p in path_spy if PID_PATH.match(p)] == []
    assert not any(p.startswith("/proc/") for p in path_spy)


def tree(path):
    return sorted(
        (str(p), p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else b"")
        for p in path.rglob("*")
    )


def test_the_route_writes_nothing(rig, tmp_path):
    now = time.time()
    rig.marker.write_text("armed")
    os.utime(rig.marker, (now - 40 * 3600, now - 40 * 3600))
    write_session(rig.root, "g1__t", now, 5)
    before = tree(tmp_path)
    for _ in range(2):
        assert rig.get().status_code == 200
    assert tree(tmp_path) == before
    assert rig.marker.exists()  # a stale marker is only reported


def test_the_response_holds_no_path_or_token(rig, tmp_path):
    rig.marker.write_text("")
    text = json.dumps(rig.get().json())
    assert str(tmp_path) not in text and ".arm-controller-active" not in text


def test_the_service_serves_the_route_behind_the_ui_token(rig, monkeypatch):
    from levi.service import app

    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    client = TestClient(app)
    assert client.get(ROUTE).status_code == 401
    ok = client.get(ROUTE, headers={"x-levi-ui-token": "test-ui-token"})
    assert ok.status_code == 200
    assert_schema(ok.json())
    assert (
        client.post(ROUTE, headers={"x-levi-ui-token": "test-ui-token"}).status_code
        == 405
    )


# ------------------------------------------------------------------ where things are


def test_the_marker_lives_in_the_main_checkouts_hub_even_from_a_worktree(
    tmp_path, monkeypatch
):
    from levi.live import locate

    main = tmp_path / "LEVI"
    work = main / "lab" / "worktrees" / "topic"
    (main / ".git").mkdir(parents=True)
    work.mkdir(parents=True)
    (work / ".git").write_text("gitdir: elsewhere\n")
    monkeypatch.setattr(locate, "checkouts", lambda top=None: [work, main])
    monkeypatch.setattr(api, "_MARKER", [])
    expected = main / "lab" / "levi-hub" / ".arm-controller-active"
    assert api.marker_path() == expected
    # Found once, it is remembered: the checkouts are not listed again.
    monkeypatch.setattr(locate, "checkouts", failing(AssertionError("looked again")))
    assert api.marker_path() == expected


def test_without_a_git_directory_the_marker_falls_back_to_this_checkout(
    tmp_path, monkeypatch
):
    from levi.live import locate

    monkeypatch.setattr(locate, "checkouts", lambda top=None: [tmp_path])
    monkeypatch.setattr(api, "_MARKER", [])
    assert api.marker_path() == tmp_path / "lab" / "levi-hub" / ".arm-controller-active"


def test_a_fallback_guess_of_the_marker_location_is_not_remembered(
    tmp_path, monkeypatch
):
    from levi.live import locate

    main = tmp_path / "LEVI"
    work = main / "lab" / "worktrees" / "topic"
    work.mkdir(parents=True)
    monkeypatch.setattr(api, "_MARKER", [])
    # No main checkout can be seen yet (no .git directory): this checkout is the
    # guess, and it is the wrong one for a worktree.
    monkeypatch.setattr(locate, "checkouts", lambda top=None: [work])
    guess = work / "lab" / "levi-hub" / ".arm-controller-active"
    assert api.marker_path() == guess
    # Once the main checkout can be seen the right path is given, not the guess.
    (main / ".git").mkdir()
    monkeypatch.setattr(locate, "checkouts", lambda top=None: [work, main])
    assert api.marker_path() == main / "lab" / "levi-hub" / ".arm-controller-active"


def live_workspace(tmp_path, monkeypatch, toml):
    from levi.live import locate

    ws = tmp_path / "live-ws"
    (ws / "live").mkdir(parents=True, exist_ok=True)
    if toml is not None:
        (ws / "live.toml").write_text(toml)
    monkeypatch.setattr(locate, "find", lambda own: locate.Found(ws, "env"))
    monkeypatch.setattr(locate, "is_live", lambda w: True)
    return ws


def test_rollout_roots_come_from_the_live_workspace_configuration(
    tmp_path, monkeypatch
):
    live_workspace(
        tmp_path, monkeypatch, "[watch]\nroots = ['/data/rollouts', '~/more']\n"
    )
    roots, why = api.rollout_roots()
    assert roots == ["/data/rollouts", os.path.expanduser("~/more")] and why is None


def test_the_effective_configuration_wins_over_live_toml(tmp_path, monkeypatch):
    ws = live_workspace(tmp_path, monkeypatch, "[watch]\nroots = ['/old']\n")
    (ws / "live" / "effective.toml").write_text("[watch]\nroots = ['/new']\n")
    assert api.rollout_roots() == (["/new"], None)


def test_no_live_workspace_gives_no_roots_and_a_reason_without_a_path(monkeypatch):
    from levi.live import locate

    monkeypatch.setattr(
        locate, "find", lambda own: locate.Found(None, "status", "not_configured")
    )
    roots, why = api.rollout_roots()
    assert roots == [] and "not_configured" in why and "/" not in why


def test_a_configuration_without_roots_or_unreadable_gives_no_roots(
    tmp_path, monkeypatch
):
    live_workspace(tmp_path, monkeypatch, "[gpu]\nlock_file = '/x'\n")
    roots, why = api.rollout_roots()
    assert roots == [] and "no rollout roots" in why
    live_workspace(tmp_path, monkeypatch, "[watch\nroots = ")
    roots, why = api.rollout_roots()
    assert roots == [] and "could not be read" in why
