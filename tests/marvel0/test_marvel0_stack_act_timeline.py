"""How long the guided start takes, on a clock the test moves (``sim_time.py``).

The page's real code runs: the real ticker loop (``Act._tick_loop``), the real
operations on real threads, the checks side by side. What the machine does
takes the time the owner measured on the real one: the check of the ROS graph
about 3.4 s, the FCI check 1 s, A ready 5 s after it starts, B 2 s (the gripper
calibrates), the listing of the gripper controllers 1.2 s, the end-effector
frame check 5 s, C 3 s. What these tests pin is what the page adds to those:
nothing before A beyond the marker's minimum age and the check it needs, no
wait between a stage that is ready and the start of the next, and a heavy job
that is really running holds A back until it ends or the cap.

Nothing here starts a process, opens a socket or sleeps in real time."""

import dataclasses
import os
import threading

import pytest
from sim_time import Clock
from stack_act_machine import (
    CONTROLLERS_OUT,
    KEY_OF,
    ActMachine,
)
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack import NOW

from levi.marvel0 import stack, stack_act

# What the real machine takes (seconds): see the module doc.
NODES_S, FCI_S, LISTING_S, EE_S = 3.4, 1.0, 1.2, 5.0
# (odd fractions on purpose: a stage that is ready between two looks is seen at
# the next one, and the tests below measure that wait)
READY_S = {"a": 5.1, "b": 2.07, "c": 3.13}
TICK_SLACK_S = (
    stack_act.SEQUENCE_TICK_S + 0.01
)  # what a ready stage may wait to be seen


class World:
    """The fake machine of L1 with its time and its threads on a ``Clock``."""

    def __init__(self, tmp_path, monkeypatch):
        self.clock = Clock(NOW, on_move=self._moved)
        m = self.m = ActMachine(tmp_path)
        self.t0 = self.clock.t
        self.starts, self.readied, self.passes = {}, {}, 0
        self.limit_s = 400.0
        host = m.act.host
        host.background, host.parallel = True, self.clock.run_calls
        monkeypatch.setattr(m.act, "_ensure_ticker", lambda: None)
        monkeypatch.setattr(
            m.act,
            "_start_thread",
            lambda op, work: self.clock.spawn(
                lambda: m.act._run_work(op, work), "operation"
            ),
        )
        monkeypatch.setattr(
            m.act, "_wait_tick", lambda s: self.clock.wait_tick(m.act._wake, s)
        )
        # the marker's age is the page's clock against the moment it was made
        real_view = stack.marker_view
        monkeypatch.setattr(
            stack,
            "marker_view",
            lambda path, now: {
                **(v := real_view(path, now)),
                "ctime_age_s": v["age_s"],
            },
        )
        touched = m.act._do_touch

        def touch(r):
            out = touched(r)
            os.utime(m.marker_path, (m.t, m.t))
            m.act._remember_marker("created")
            return out

        monkeypatch.setattr(m.act, "_do_touch", touch)
        advanced = m.act.advance

        def counted():
            self.passes += 1
            return advanced()

        monkeypatch.setattr(m.act, "advance", counted)
        self._machine(m)

    def _moved(self, t):
        self.m.t = t
        if t - self.t0 > self.limit_s:
            raise AssertionError("the guided start did not settle")

    def _machine(self, m):
        m.t = self.clock.t
        clock = self.clock
        last = {}
        health = m.health

        def kept(**kw):
            last.clear()
            last.update(kw)
            health(**kw)

        m.health = kept
        m.m_ready()

        def monitor():  # (the health monitor keeps writing, twice a second)
            while True:
                clock.sleep(0.5)
                health(**last)

        clock.spawn(monitor, "monitor")

        probe = m._probe

        def fci(ip, port):
            clock.sleep(FCI_S)
            return probe(ip, port)

        m.act.host.fci_probe = fci

        def oneshot(cmd):
            text = " ".join(cmd)
            if " node list " in f" {text} ":
                clock.sleep(NODES_S)
            elif "list_controllers" in text:
                clock.sleep(LISTING_S)
            elif "check_fr3_ee_frame" in text:
                clock.sleep(EE_S)

        m.on_oneshot = oneshot

        def came_up(key):
            m.t = clock.t
            {"a": m.a_up, "b": m.b_up, "c": m.c_up}[key]()
            if key == "b":
                m.controllers_out, m.controllers_code = CONTROLLERS_OUT, 0
            self.readied[key] = clock.t

        def systemd_run(argv):
            unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
            key = KEY_OF.get(unit)
            if key is None or "--pipe" in argv:
                return
            self.starts[key] = clock.t

            def ready():
                clock.sleep(READY_S[key])
                came_up(key)

            clock.spawn(ready, f"{key}-starting")

        m.on_systemd_run = systemd_run

    def press(self, **extra):
        self.t0 = self.clock.t
        out = self.m.act.sequence_start(self.m.chain_body("chain-sim-0001", **extra))
        assert out == {"done": "sequence_started"}

    def run(self):
        """The real ticker loop, until the sequence is over."""
        self.m.act._tick_loop()
        assert not self.clock.errors, self.clock.errors
        return self.m.act._seq_public(self.m.act._seq)

    def after(self, t):
        return round(t - self.t0, 2)


@pytest.fixture
def world(tmp_path, monkeypatch):
    made = World(tmp_path, monkeypatch)
    yield made
    made.clock.close()


def test_the_time_before_a_starts_is_under_five_seconds_when_no_heavy_job_runs(world):
    world.press()
    seq = world.run()
    assert (seq["state"], seq["reason"]) == ("done", None), seq
    pre_a = world.after(world.starts["a"])
    # the slowest of the checks A needs is the one of the ROS graph; the marker's
    # minimum age (3 s) runs beside it, begun right after M
    assert NODES_S <= pre_a <= 5.0, pre_a
    assert pre_a <= NODES_S + TICK_SLACK_S, "A waited for something besides the check"


def test_the_whole_chain_takes_what_the_machine_takes_and_no_more(world):
    world.press()
    seq = world.run()
    assert seq["state"] == "done"
    a_start, a_up = world.starts["a"], world.readied["a"]
    b_start, b_up = world.starts["b"], world.readied["b"]
    c_start, c_up = world.starts["c"], world.readied["c"]
    done = world.clock.t
    # each stage starts when the one before is seen ready: at most a pause of the
    # ticker (a stage that is ready is only seen at the next look)
    assert 0 <= b_start - a_up <= TICK_SLACK_S, world.after(b_start - a_up)
    assert c_start - b_up <= EE_S + TICK_SLACK_S  # (the checks before C, side by side)
    assert c_start - b_up >= EE_S  # (and not shorter than the slower of the two)
    assert 0 <= done - c_up <= TICK_SLACK_S
    # ... and every start-up took what it takes
    assert a_up - a_start == pytest.approx(READY_S["a"])
    assert b_up - b_start == pytest.approx(READY_S["b"])
    assert c_up - c_start == pytest.approx(READY_S["c"])
    total = world.after(done)
    machine = NODES_S + sum(READY_S.values()) + EE_S
    assert machine <= total <= machine + 4 * TICK_SLACK_S, (total, machine)


def test_the_checks_before_c_take_the_slower_of_the_two_not_the_sum(world):
    world.press()
    world.run()
    pre = world.m.act._c_pre
    assert pre["ok"] is True
    # started the moment B was ready, and C started after the end-effector check
    # alone (1.2 s of listing ran inside its 5 s)
    waited = world.starts["c"] - world.readied["b"]
    assert EE_S <= waited < EE_S + LISTING_S, waited


def test_the_ticker_does_not_spin_while_the_machine_works(world):
    world.press()
    world.run()
    seconds = world.clock.t - world.t0
    looks_per_s = world.passes / seconds
    # one look per 0.25 s of waiting, and a few at once when a step moved on
    assert world.passes <= 4 * seconds + 60, (world.passes, seconds)
    assert looks_per_s < 8


def test_every_step_says_how_long_it_took_and_they_add_up(world):
    world.press()
    seq = world.run()
    took = {i["step"]: i["took_s"] for i in seq["steps"]}
    assert all(isinstance(v, float) for v in took.values()), took
    # the stages that wait for the machine took what it took (plus a look)
    assert READY_S["a"] <= took["await_a"] <= READY_S["a"] + TICK_SLACK_S
    assert READY_S["b"] <= took["b"] <= READY_S["b"] + TICK_SLACK_S
    assert EE_S <= took["c_preflight"] <= EE_S + TICK_SLACK_S
    assert READY_S["c"] <= took["c"] <= READY_S["c"] + TICK_SLACK_S
    assert took["marker"] == 0 and took["monitor"] == 0
    assert seq["took_s"] == pytest.approx(sum(took.values()), abs=0.02)
    assert seq["took_s"] <= world.after(world.clock.t)


# --- a heavy job that really runs holds A back ---------------------------------------


def heavy_job(world, ends_at=None, count=1):
    """A heavy job of the gate runs (``heavy_jobs_running`` says so) until
    ``ends_at`` seconds after the press (for ever when None)."""
    m, clock = world.m, world.clock
    m.heavy = {
        "state": "running",
        "count": count,
        "kinds": ["pytest-dir"],
        "empty": 0,
        "why": None,
    }
    if ends_at is None:
        return

    def ends():
        clock.sleep(ends_at - (clock.t - world.t0))
        m.heavy = {"state": "clear", "count": 0, "kinds": [], "empty": 0, "why": None}

    world.ending = ends


def test_a_heavy_job_holds_a_back_until_it_ends(world):
    heavy_job(world, ends_at=20.0)
    world.press()
    world.clock.spawn(world.ending, "heavy-job")
    seq = world.run()
    assert seq["state"] == "done"
    pre_a = world.after(world.starts["a"])
    # A starts at the look after the job ended (two clean looks in a row)
    assert 20.0 <= pre_a <= 20.0 + 3 * TICK_SLACK_S, pre_a
    wait = next(i for i in seq["steps"] if i["step"] == "wait")
    assert wait["took_s"] >= 20.0 - NODES_S - 0.5


def test_the_document_says_what_the_wait_is_for_and_that_its_end_is_not_known(world):
    heavy_job(world, ends_at=None, count=2)
    world.press()
    m = world.m
    for _ in range(30):  # (by hand: the loop would not end)
        m.act.advance()
        if m.act._seq["step"] == "wait":
            break
        world.clock.step(world.clock.t + 0.25)
    doc = m.doc()["sequence"]
    assert doc["step"] == "wait" and doc["remaining_s"] is None
    wait = doc["wait"]
    assert wait["heavy"]["state"] == "running" and wait["heavy"]["count"] == 2
    assert "heavy_jobs_running" in wait["holds"] and "marker_young" not in wait["holds"]
    assert wait["remaining_s"] is None and wait["cap_s"] == stack_act.HEAVY_WAIT_CAP_S
    assert "heavy_jobs_running" in m.blockers("a")


def test_a_heavy_job_that_does_not_end_ends_the_start_with_its_count_at_the_cap(world):
    heavy_job(world, ends_at=None, count=2)
    world.limit_s = 200.0
    world.press()
    seq = world.run()
    assert (seq["state"], seq["reason"]) == ("failed", "heavy_job_still_running")
    assert seq["reason_params"] == {"count": 2}
    assert "a" not in world.starts, "A was never started"
    wait = next(i for i in seq["steps"] if i["step"] == "wait")
    assert wait["state"] == "failed"
    assert (
        stack_act.HEAVY_WAIT_CAP_S <= wait["took_s"] <= stack_act.HEAVY_WAIT_CAP_S + 1
    )
    # nothing is paused for a start that did not happen
    assert not world.m.marker_path.exists()


def test_when_the_heavy_jobs_cannot_be_told_the_fixed_wait_of_old_applies(world):
    world.m.heavy = {
        "state": "unknown",
        "count": 0,
        "kinds": [],
        "empty": 0,
        "why": "log_missing",
    }
    world.press()
    seq = world.run()
    assert seq["state"] == "done"
    legacy = stack_act.control_site.LEGACY_MARKER_WAIT_S
    pre_a = world.after(world.starts["a"])
    assert legacy <= pre_a <= legacy + 2 * TICK_SLACK_S, pre_a


# --- the single steps ---------------------------------------------------------------


def test_the_ticker_wakes_at_once_when_an_operation_ends(world):
    """The pause is cut short by the end of a check: the step after it is not
    taken a whole pause later."""
    world.press()
    world.run()
    # the first look after the check of the graph ended came at its end, not a
    # tick later
    assert world.starts["a"] - world.t0 == pytest.approx(NODES_S, abs=0.011)


def test_an_unrelated_thread_poking_the_page_does_not_break_the_loop(world):
    world.press()
    stop = threading.Event()
    world.clock.spawn(
        lambda: [world.clock.sleep(0.1) or world.m.act._poke() for _ in range(5)],
        "poker",
    )
    seq = world.run()
    stop.set()
    assert seq["state"] == "done"
    assert dataclasses.is_dataclass(world.m.act.host)
