"""L2 of the guided start through B and C ("start whole stack"): the page's real
code driving the real child processes of the stand-in programs (``fake_stack/``),
behind the runner of ``test_marvel0_stack_act_processes.py`` that maps
``systemctl`` and ``systemd-run`` onto ``Popen``. Nothing but a stand-in is ever
run, nothing touches a robot, no protected port is used.

These tests start processes and wait for them; they take a few seconds each."""

import dataclasses
import time

import pytest
from fake_stack import fake_specs
from test_marvel0_stack_act_processes import PORT, ProcessMachine, standin_table

from levi.marvel0 import net, record, stack


class ChainMachine(ProcessMachine):
    """The process machine with the health monitor M as the fake machine shows
    it (a unit of the page's, running): the guided start looks at M first, and
    the runner of the stand-ins knows no such unit."""

    def host(self):
        built = super().host()
        run = self.processes.run

        def routed(argv, timeout_s):
            if list(argv[:4]) == ["systemctl", "--user", "show", stack.MONITOR_SERVICE]:
                return 0, self._monitor_show()
            return run(argv, timeout_s)

        return dataclasses.replace(built, run=stack.guarded(routed))


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**options):
        m = ChainMachine(tmp_path / f"m{len(made)}", **options)
        made.append(m)
        return m

    yield build
    for m in made:
        m.processes.reap()
    leftover = fake_specs.leftover_standins(tmp_path)
    assert not leftover, f"a stand-in program was left running: {leftover}"


def drive(m, limit_s=40.0):
    """The ticker's job, by hand: advance until the sequence stops running (a
    pause, an end)."""
    end = time.monotonic() + limit_s
    seq = {}
    while time.monotonic() < end:
        if not procs(m)["a"]:
            m.no_manager()  # (the monitor keeps writing: no controller manager yet)
        seq = m.act.advance()
        if seq["state"] != "running":
            return seq
        time.sleep(0.05)
    raise AssertionError(f"the guided start did not settle: {m.doc()['sequence']}")


def chain(m, rid="chain-proc-0001", **extra):
    m.m_ready()
    return m.act.sequence_start(m.chain_body(rid, **extra))


def states(seq):
    return {i["step"]: i["state"] for i in seq["steps"]}


def procs(m):
    return {k: m.processes.units.get(m.specs.units[k].unit) for k in "abc"}


def test_one_press_brings_up_m_a_b_and_c_as_real_programs_and_ends_done(make):
    m = make()
    assert chain(m) == {"done": "sequence_started"}
    seq = drive(m)
    assert (seq["state"], seq["reason"]) == ("done", None), seq
    assert set(states(seq).values()) == {"done"}
    assert {k: m.terminal(k)["state"] for k in "abc"} == {k: "ready" for k in "abc"}
    assert net.listeners(standin_table(), PORT)
    # each program was started once, by the page, with the unit's own properties
    started = [
        c for c in m.processes.commands if c[0] == "systemd-run" and "--pipe" not in c
    ]
    assert len(started) == 3
    assert {a_record(m, k)["request_id"] for k in "abc"} == {"chain-proc-0001"}
    assert a_record(m, "b")["attest"] == {"hands_clear": True}
    assert all(p.signals == [] for p in procs(m).values())  # (nothing was stopped)
    # the next press has nothing to do, and the way out still ends all three
    assert chain_again(m) == {"done": "already_started"}
    m.act.sequence_stop(m.stop_all_body("stop-all-proc1"))
    assert drive(m)["state"] == "done"
    assert {k: m.terminal(k)["state"] for k in "abc"} == {k: "idle" for k in "abc"}


def a_record(m, key):
    return record.read_record(m.state_root / key)


def chain_again(m):
    return m.act.sequence_start(m.chain_body("chain-proc-0002"))


def test_a_b_that_never_becomes_ready_ends_the_chain_and_leaves_a_up(make):
    m = make(b="never-ready", ready_timeout_s=1.5)
    chain(m)
    seq = drive(m)
    assert (seq["state"], seq["reason"]) == ("failed", "b_failed"), seq
    got = states(seq)
    assert got["b"] == "failed" and got["c_preflight"] == "not_reached"
    assert m.terminal("a")["state"] == "ready"
    p = procs(m)
    assert p["a"].alive() and p["a"].signals == []
    assert p["b"].alive() and p["b"].signals == []  # (the page never stops on its own)
    assert p["c"] is None
    assert m.terminal("b")["can_stop"] is True


def test_pressed_again_after_b_was_fixed_it_starts_only_b_and_c(make):
    m = make(b="never-ready", ready_timeout_s=1.5)
    chain(m)
    assert drive(m)["reason"] == "b_failed"
    m.act.stop("b", m.stop_body("b"))  # the person's fix: stop the failed B ...
    m.specs = fake_specs.fake_specs(  # ... and the cause is gone (B now starts)
        m.health_path,
        port=PORT,
        qos_file=m.tmp / "qos.py",
        alive_file=m.tmp / "b.alive",
        ready_timeout_s=10.0,
    )
    a_process = procs(m)["a"]
    assert chain_again(m) == {"done": "sequence_started"}
    seq = drive(m)
    assert (seq["state"], seq["reason"]) == ("done", None), seq
    got = states(seq)
    # A is up: its checks, the marker, the wait and its start are skipped
    assert [got[s] for s in ("probe", "marker", "wait", "arm")] == ["skipped"] * 4
    assert procs(m)["a"] is a_process and a_process.signals == []  # (the same A)
    assert {k: m.terminal(k)["state"] for k in "abc"} == {k: "ready" for k in "abc"}
    started = [c for c in m.processes.commands if c[0] == "systemd-run"]

    def count(word):
        return len([c for c in started if word in " ".join(c)])

    assert (count("fake_a.py"), count("fake_b.py"), count("fake_c.py")) == (1, 2, 1)


def test_a_listing_that_gets_no_answer_once_is_asked_again_by_the_chain(make):
    m = make(controllers="flaky-once")
    chain(m)
    seq = drive(m)
    assert (seq["state"], seq["reason"]) == ("done", None), seq
    assert states(seq)["c_preflight"] == "done"
    assert (m.tmp / "b.alive.asked").exists()  # (the first listing got no answer)
    assert m.terminal("c")["state"] == "ready"


def test_a_c_that_never_becomes_ready_ends_the_chain_with_a_and_b_up(make):
    m = make(c="never-ready", ready_timeout_s=1.5)
    chain(m)
    seq = drive(m)
    assert (seq["state"], seq["reason"]) == ("failed", "c_failed"), seq
    assert {k: m.terminal(k)["state"] for k in "abc"} == {
        "a": "ready",
        "b": "ready",
        "c": "failed",
    }
    assert all(p.signals == [] for p in procs(m).values())
