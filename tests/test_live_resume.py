"""The live gate's blocked runs go on by themselves: only a person's run, only
the ones the gate stopped, only once the gate has stayed open, with back-off and
an audit line -- and nothing else is ever resumed."""

# ruff: noqa: F811

import json
import time

import pytest
from test_agent_economy import bench  # noqa: F401
from test_live_auto import audit, live_ws, write_gate  # noqa: F401

from levi.live import auto, gating, resumer


class Clock:
    """Monotonic and wall time that only move when the test says so."""

    def __init__(self):
        self.now = time.time()  # real-time based: a real executor reads the gate too

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Store:
    """Just what the resumer asks of the run store."""

    def __init__(self, runs):
        self.runs = {r["id"]: r for r in runs}
        self.events = []

    def get(self, kind, id):
        return self.runs[id]

    def list(self, kind):
        return list(self.runs.values())

    def mutate(self, kind, id, change):
        change(self.runs[id])
        return self.runs[id]

    def event(self, run_id, type, **data):
        self.events.append((run_id, type, data))


class Bench:
    def __init__(self, store, fail=None):
        self.store = store
        self.launched = []
        self.fail = fail
        self.before = None  # something that happens between the read and the launch

    def launch(self, run_id, pilot=True, expect=None):
        from levi.agent.store import Conflict

        if self.before:
            self.before(self.store.runs[run_id])
        if expect is not None and not expect(self.store.runs[run_id]):
            raise Conflict("The run changed before it could be resumed")
        if self.fail:
            raise self.fail
        self.launched.append((run_id, pilot))
        # What a launch does: the run is queued, the gate mark is dropped.
        self.store.runs[run_id].update(status="queued", blocked_gate=None)


def blocked(id="run-a", **over):
    return {
        "id": id,
        "status": "blocked",
        "blocked_by": "gpu",
        "blocked_gate": "live",
        "principal": "local-human",
        "control": None,
        "reason": "the evaluation is inferring",
        "plan": {"pilot_episode": 0, "pilot_review": {"accepted": True}},
        "completed": [],
        **over,
    }


@pytest.fixture(autouse=True)
def clean_notes():
    resumer._PENDING.clear()
    yield
    resumer._PENDING.clear()


@pytest.fixture
def rig(live_ws):
    clock = Clock()

    def make(*runs, stable_s=3.0, max_bounces=3, fail=None):
        store = Store(runs)
        wb = Bench(store, fail)
        res = resumer.GateResumer(
            store,
            wb,
            live_ws,
            stable_s=stable_s,
            max_bounces=max_bounces,
            clock=clock,
            wall=clock,
        )
        res.scan()
        return res, store, wb

    def gate(open_, opened_ago=None, **kw):
        write_gate(live_ws, open_, **kw)
        # write_gate stamps the real time; the resumer lives on the fake one.
        path = live_ws / "gate.json"
        value = json.loads(path.read_text())
        value["updated_at"] = clock.now - kw.get("age", 0.0)
        if opened_ago is not None:  # what the supervisor writes since this change
            value["opened_at"] = clock.now - opened_ago
        path.write_text(json.dumps(value))

    clock.make, clock.gate = make, gate
    return clock


def open_for(clock, res, seconds, step=1.0):
    """Keep the gate open and fresh for ``seconds`` of fake time; returns what
    was resumed."""
    out = []
    waited = 0.0
    while waited < seconds:
        clock.gate(True)
        out += res.tick()
        clock.advance(step)
        waited += step
    clock.gate(True)
    return out + res.tick()


def test_a_run_the_gate_stopped_goes_on_once_the_gate_has_stayed_open(rig, live_ws):
    res, store, wb = rig.make(blocked())
    rig.gate(False)
    assert res.tick() == [] and not wb.launched  # closed: nothing
    rig.advance(1)
    rig.gate(True)
    assert res.tick() == []  # open just now: not yet
    rig.advance(2.5)
    rig.gate(True)
    assert res.tick() == []  # 2.5 s: still inside the unsafe margin
    rig.advance(1)
    rig.gate(True)
    assert res.tick() == ["run-a"]
    assert wb.launched == [("run-a", False)]
    (row,) = audit_lines(live_ws)
    assert row["principal"] == "live-resume" and row["tool"] == "runs.resume"
    assert row["decision"] == "auto_resumed" and row["run_id"] == "run-a"
    assert row["run_principal"] == "local-human" and row["pilot"] is False
    assert row["gate"]["open"] is True and row["gate"]["code"] == "ok"
    assert row["gate"]["open_for_s"] >= 3.0
    assert "inferring" in row["reason"]
    assert store.runs["run-a"]["live_resume"]["bounces"] == 1
    # It is in the run's own event history too (a person looking at the run).
    (event,) = store.events
    assert event[:2] == ("run-a", "auto_resumed")
    assert event[2]["by"] == "live-resume" and event[2]["bounces"] == 1


def audit_lines(live):
    path = live / auto.AUDIT
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_a_closed_gate_never_resumes_and_a_flicker_starts_the_count_again(rig, live_ws):
    res, _store, wb = rig.make(blocked())
    for _ in range(2):  # open 2 s ...
        rig.gate(True)
        res.tick()
        rig.advance(1)
    rig.gate(False)  # ... closes ...
    res.tick()
    rig.advance(1)
    rig.gate(True)  # ... opens: the count starts over
    res.tick()
    rig.advance(2)
    rig.gate(True)
    assert res.tick() == [] and not wb.launched
    rig.advance(1.5)
    rig.gate(True)
    assert res.tick() == ["run-a"]


def test_a_stale_or_missing_gate_file_resumes_nothing(rig, live_ws):
    res, _store, wb = rig.make(blocked())
    # No file: no supervisor says go.
    for _ in range(6):
        assert res.tick() == []
        rig.advance(1)
    # A file nobody has refreshed for STALE_S, however "open" it says it is.
    rig.gate(True, age=gating.STALE_S + 1)
    for _ in range(6):
        assert res.tick() == []
        rig.advance(1)
        rig.gate(True, age=gating.STALE_S + 1)
    # Even one whose last word was idle (a person is let through; software is not).
    rig.gate(True, age=gating.STALE_S + 1, idle=True)
    assert res.tick() == [] and not wb.launched


def test_only_the_runs_the_gate_stopped_are_resumed(rig, live_ws):
    runs = [
        blocked("paused-by-a-person", status="paused", control="pause"),
        blocked("cancelled", status="cancelled", control="cancel"),
        blocked("model-error", blocked_by=None, blocked_gate=None, reason="bad json"),
        blocked("guardian", blocked_gate=None),  # the GPU guardian's block
        blocked("teacher", blocked_by=None, blocked_gate=None),
        blocked("pause-pending", control="pause"),
        blocked("waiting", status="waiting_for_review"),
    ]
    res, _store, wb = rig.make(*runs)
    assert open_for(rig, res, 6) == [] and not wb.launched
    assert audit_lines(live_ws) == []


def test_only_a_person_s_runs_not_the_worker_s_or_an_agent_s(rig, live_ws):
    runs = [
        blocked("auto", principal="live-auto"),
        blocked("planner", principal="live-planner"),
        blocked("agent", principal="external"),
    ]
    res, _store, wb = rig.make(*runs)
    assert open_for(rig, res, 6) == [] and not wb.launched
    summary = resumer.read_blocked(live_ws, rig.now)
    assert summary["count"] == 3
    assert summary["waiting"] == [] and len(summary["needs_person"]) == 3


def test_a_pilot_waiting_for_a_person_is_not_resumed(rig, live_ws):
    run = blocked(
        plan={"pilot_episode": 0, "pilot_review": None}, completed=[0], status="blocked"
    )
    res, _store, wb = rig.make(run)
    assert open_for(rig, res, 6) == [] and not wb.launched
    # Before the pilot is done it resumes as a pilot.
    res, _store, wb = rig.make(
        blocked("early", plan={"pilot_episode": 0, "pilot_review": None})
    )
    assert open_for(rig, res, 6) == ["early"] and wb.launched == [("early", True)]


def test_each_run_is_resumed_at_most_once_per_opening(rig, live_ws):
    res, store, wb = rig.make(blocked())
    assert open_for(rig, res, 4) == ["run-a"]
    # The gate stopped it again within the same opening (it closed and opened
    # between two ticks): not again until the gate really closes and reopens.
    store.runs["run-a"].update(status="blocked", blocked_gate="live")
    resumer.note_blocked("run-a")
    for _ in range(5):
        rig.gate(True)
        assert res.tick() == []
        rig.advance(1)
    assert len(wb.launched) == 1
    rig.gate(False)
    res.tick()
    assert open_for(rig, res, 4) == ["run-a"]  # the next opening: once more
    assert len(wb.launched) == 2


def test_after_three_bounces_in_a_row_it_stops_and_says_so(rig, live_ws):
    res, store, wb = rig.make(blocked())
    for n in range(3):
        assert open_for(rig, res, 4) == ["run-a"], n
        store.runs["run-a"].update(status="blocked", blocked_gate="live")  # bounced
        resumer.note_blocked("run-a")
        rig.gate(False)
        res.tick()
    assert open_for(rig, res, 8) == [] and len(wb.launched) == 3
    given_up = [r for r in audit_lines(live_ws) if r["decision"] == "given_up"]
    assert len(given_up) == 1 and given_up[0]["run_id"] == "run-a"
    summary = resumer.read_blocked(live_ws, rig.now)
    assert summary["count"] == 1 and summary["needs_person"] == ["run-a"]


def test_an_episode_finished_in_between_resets_the_count(rig, live_ws):
    res, store, wb = rig.make(blocked(), max_bounces=2)
    for episode in range(5):  # far more than the bounces allowed, each with progress
        assert open_for(rig, res, 4) == ["run-a"], episode
        store.runs["run-a"].update(
            status="blocked", blocked_gate="live", completed=list(range(episode + 1))
        )
        resumer.note_blocked("run-a")
        rig.gate(False)
        res.tick()
    assert len(wb.launched) == 5


def test_progress_is_a_finished_episode_or_tokens_spent_not_a_reserved_request(
    rig, live_ws
):
    """``requests`` is counted when a call is reserved, so a call the gate
    stopped has already raised it: only tokens (settled when an answer came) or
    a finished episode count as getting somewhere."""
    res, store, wb = rig.make(blocked(requests=0, tokens=0), max_bounces=2)
    for n in range(2):
        assert open_for(rig, res, 4) == ["run-a"], n
        # Bounced at the first request: the reservation counted, nothing was spent.
        store.runs["run-a"].update(
            status="blocked", blocked_gate="live", requests=n + 1, tokens=0
        )
        resumer.note_blocked("run-a")
        rig.gate(False)
        res.tick()
    assert open_for(rig, res, 8) == [] and len(wb.launched) == 2  # given up
    # A call that came back before the next stop is progress, with no episode.
    res, store, wb = rig.make(blocked(requests=0, tokens=0), max_bounces=2)
    for n in range(5):
        assert open_for(rig, res, 4) == ["run-a"], n
        store.runs["run-a"].update(
            status="blocked", blocked_gate="live", requests=n + 1, tokens=500 * (n + 1)
        )
        resumer.note_blocked("run-a")
        rig.gate(False)
        res.tick()
    assert len(wb.launched) == 5


def test_a_person_s_pause_between_the_read_and_the_launch_is_not_undone(rig, live_ws):
    res, store, wb = rig.make(blocked())

    def pause(run):  # the person presses Pause after the resumer looked
        run.update(status="paused", control="pause")

    wb.before = pause
    assert open_for(rig, res, 8) == []
    assert store.runs["run-a"]["status"] == "paused" and not wb.launched
    rows = audit_lines(live_ws)
    assert [r["decision"] for r in rows] == ["skipped"]  # once, not every tick
    assert "changed" in rows[0]["reason"]


def test_launch_re_checks_inside_its_own_transaction(client, dataset):
    """The real ``Workbench.launch``: ``expect`` is evaluated on the record in
    the same transaction that queues the run, so a pause that landed after the
    caller's read is not overwritten."""
    from test_ollama_integration import _unsupervised_run

    from levi.agent.store import Conflict

    wb, run_id = _unsupervised_run(client, dataset)
    wb.store.release(run_id, "test-run")
    wb.store.mutate(
        "runs",
        run_id,
        lambda r: r.update(
            status="blocked", blocked_by="gpu", blocked_gate="live", control=None
        ),
    )
    wb.control(run_id, "pause")  # the person, after the caller's read
    assert wb.store.get("runs", run_id)["status"] == "paused"
    with pytest.raises(Conflict, match="changed"):
        wb.launch(
            run_id,
            pilot=False,
            expect=lambda r: resumer.GateResumer._gated(r) and not r.get("control"),
        )
    run = wb.store.get("runs", run_id)
    assert run["status"] == "paused" and run["control"] == "pause"
    assert wb.store.claim(run_id, "someone")  # the lease was given back


def test_a_failed_launch_is_audited_and_not_retried_in_that_opening(rig, live_ws):
    res, _store, _wb = rig.make(blocked(), fail=ValueError("plan changed"))
    assert open_for(rig, res, 8) == []
    rows = audit_lines(live_ws)
    assert [r["decision"] for r in rows] == ["failed"]
    assert "plan changed" in rows[0]["error"]


def test_a_lease_that_is_still_being_released_is_tried_again(rig, live_ws):
    from levi.agent.store import Conflict

    res, _store, wb = rig.make(blocked(), fail=Conflict("active executor"))
    assert open_for(rig, res, 4) == []
    wb.fail = None
    rig.gate(True)
    assert res.tick() == ["run-a"]


def test_zero_bounces_switches_the_automatic_resume_off(rig, live_ws):
    res, _store, wb = rig.make(blocked(), max_bounces=0)
    assert open_for(rig, res, 8) == [] and not wb.launched
    assert audit_lines(live_ws) == []
    assert resumer.read_blocked(live_ws, rig.now)["needs_person"] == ["run-a"]


@pytest.mark.parametrize(
    "toml,message",
    [
        ("[gpu]\nresume_stable_s = 0.1\n", "resume_stable_s"),
        ("[gpu]\nresume_stable_s = 120\n", "resume_stable_s"),
        ("[gpu]\nresume_max_bounces = -1\n", "resume_max_bounces"),
    ],
)
def test_the_resume_settings_are_range_checked(tmp_path, toml, message):
    from levi.live import config as live_config

    path = tmp_path / "live.toml"
    path.write_text(toml)
    with pytest.raises(ValueError, match=message):
        live_config.load(path)
    path.write_text("[gpu]\nresume_stable_s = 0.5\nresume_max_bounces = 0\n")
    assert live_config.load(path).gpu.resume_max_bounces == 0


def test_the_gate_s_own_opening_time_decides_when_it_has_been_open_long_enough(
    rig, live_ws
):
    res, _store, wb = rig.make(blocked())
    # Sampled as open for ages, but the supervisor says it opened 1 s ago (it
    # closed and opened again between two samples): not yet.
    for _ in range(6):
        rig.gate(True, opened_ago=1.0)
        assert res.tick() == []
        rig.advance(1)
    rig.gate(True, opened_ago=3.2)
    assert res.tick() == ["run-a"]
    row = audit_lines(live_ws)[0]
    assert row["gate"]["open_for_s"] >= 3.0 and wb.launched


def test_a_blocked_runs_file_that_cannot_be_written_does_not_stop_the_resume(
    rig, live_ws, monkeypatch
):
    res, _store, wb = rig.make(blocked())
    real = resumer.jsonio.write

    def refuse(path, *args, **kwargs):
        if str(path).endswith(resumer.FILE):
            raise OSError("disk full")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(resumer.jsonio, "write", refuse)
    assert open_for(rig, res, 4) == ["run-a"] and wb.launched
    assert res.report_failures >= 1


def test_a_workspace_that_is_not_live_is_left_alone(rig, live_ws):
    res, _store, wb = rig.make(blocked())
    (live_ws / auto.MARKER).unlink()
    assert open_for(rig, res, 6) == [] and not wb.launched
    assert not (live_ws / resumer.FILE).exists()
    assert not (live_ws / auto.AUDIT).exists()


def test_the_thread_exists_only_in_a_live_workspace(tmp_path, live_ws):
    plain = tmp_path / "plain"
    plain.mkdir()
    built = []
    assert resumer.start(plain, lambda: built.append(1)) is None
    assert built == []  # nothing is even constructed outside a live workspace
    live_root = tmp_path / "root"
    (live_root / "live").mkdir(parents=True)
    auto.write_marker(live_root / "live")
    thread = resumer.start(live_root, lambda: (Store([]), Bench(Store([]))))
    try:
        assert thread is not None and thread.thread.is_alive()
    finally:
        thread.close()


def test_the_blocked_runs_file_follows_the_runs(rig, live_ws):
    res, store, _wb = rig.make(blocked())
    rig.gate(False)
    res.tick()
    assert resumer.read_blocked(live_ws, rig.now)["waiting"] == ["run-a"]
    # Gone when the run goes on; a file nobody refreshes is not believed.
    store.runs["run-a"].update(status="running")
    res.tick()
    assert resumer.read_blocked(live_ws, rig.now)["count"] == 0
    store.runs["run-a"].update(status="blocked")
    resumer.note_blocked("run-a")  # what the executor does when it blocks a run
    res.tick()
    assert resumer.read_blocked(live_ws, rig.now)["count"] == 1
    assert resumer.read_blocked(live_ws, rig.now + 60)["count"] == 0


def test_the_guardian_leaves_the_live_gate_s_runs_to_the_resumer(monkeypatch):
    from levi.inference import gpu

    launched = []

    class Wb:
        def launch(self, run_id, pilot):
            launched.append(run_id)

    class Runs:
        def list(self, kind):
            return [
                blocked("live-gated"),
                blocked("guardian", blocked_gate=None),
            ]

    monkeypatch.setattr(gpu, "status", lambda servers=(): {"state": "free"})
    watch = gpu.Watch(Runs(), workbench=Wb())
    assert watch.resume_blocked() == "guardian" and launched == ["guardian"]


# --- through the real executor ------------------------------------------------------


def test_the_executor_marks_a_gate_block_and_a_resume_finishes_the_run(
    client, dataset, live_ws, monkeypatch
):
    """A person's run, started with the gate open, is stopped at its next
    request when the gate closes (``blocked``, marked as the live gate's), and
    when the gate has stayed open the resumer launches it again and it
    completes."""
    from test_ollama_integration import (
        FakeTransport,
        _answers_by_episode,
        _unsupervised_run,
    )

    from levi.inference import gpu, models, provider
    from levi.inference.ollama import OllamaClient

    transport = FakeTransport()
    factory = lambda *args, **kwargs: OllamaClient(transport)
    monkeypatch.setattr(models, "client_for", factory)
    monkeypatch.setattr(provider, "client_for", factory)
    monkeypatch.delenv(gating.WORKER_ENV, raising=False)
    _answers_by_episode(monkeypatch, bad_episodes=set())
    answer = provider.LocalProvider.generate

    def through_the_gate(self, *args, **kwargs):
        gpu.require_free(None)  # every real request starts here
        return answer(self, *args, **kwargs)

    monkeypatch.setattr(provider.LocalProvider, "generate", through_the_gate)
    monkeypatch.setattr(gating, "CACHE_S", 0.0)
    wb, run_id = _unsupervised_run(client, dataset)
    # The run is the person's: planned through the page's principal.
    wb.store.mutate("runs", run_id, lambda r: r.update(principal="local-human"))
    write_gate(live_ws, False)
    wb.execute(run_id, "test-run", pilot=False)
    run = wb.store.get("runs", run_id)
    assert run["status"] == "blocked" and run["blocked_by"] == "gpu"
    assert run["blocked_gate"] == "live"
    assert "continues by itself" in run["reason"]
    assert run_id in resumer._PENDING
    # The resumer takes it up again once the gate has stayed open.
    clock = Clock()
    res = resumer.GateResumer(
        wb.store, wb, live_ws, stable_s=3.0, clock=clock, wall=clock
    )
    res.scan()
    resumed = []
    for _ in range(5):
        write_gate(live_ws, True)
        value = json.loads((live_ws / "gate.json").read_text())
        value["updated_at"] = clock.now
        (live_ws / "gate.json").write_text(json.dumps(value))
        resumed += res.tick()
        clock.advance(1)
    assert resumed == [run_id]
    deadline = time.time() + 30
    while time.time() < deadline:
        run = wb.store.get("runs", run_id)
        if run["status"] not in {"queued", "running"}:
            break
        time.sleep(0.1)
    assert run["status"] == "waiting_for_review", run["reason"]
    assert run["blocked_gate"] is None
    assert [r["decision"] for r in audit_lines(live_ws)] == ["auto_resumed"]


def test_a_person_s_pause_is_a_pause_not_a_gate_block(
    client, dataset, live_ws, monkeypatch
):
    from test_ollama_integration import (
        FakeTransport,
        _answers_by_episode,
        _unsupervised_run,
    )

    from levi.inference import gpu, models, provider
    from levi.inference.ollama import OllamaClient

    transport = FakeTransport()
    factory = lambda *args, **kwargs: OllamaClient(transport)
    monkeypatch.setattr(models, "client_for", factory)
    monkeypatch.setattr(provider, "client_for", factory)
    monkeypatch.delenv(gating.WORKER_ENV, raising=False)
    _answers_by_episode(monkeypatch, bad_episodes=set())

    wb, run_id = _unsupervised_run(client, dataset)

    def pause_then_gate(self, *args, **kwargs):
        wb.store.mutate("runs", run_id, lambda r: r.update(control="pause"))
        gpu.require_free(None)  # the gate is closed too: the pause wins
        raise AssertionError("not reached")

    monkeypatch.setattr(provider.LocalProvider, "generate", pause_then_gate)
    monkeypatch.setattr(gating, "CACHE_S", 0.0)
    write_gate(live_ws, False)
    wb.execute(run_id, "test-run", pilot=False)
    run = wb.store.get("runs", run_id)
    assert run["status"] == "paused"
    assert run_id not in resumer._PENDING
    res = resumer.GateResumer(wb.store, wb, live_ws, stable_s=0.0)
    assert res._gated(run) is False
