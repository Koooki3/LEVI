"""L2 of the guided stop, "stop whole stack": the page's real code stopping the
real child processes of the stand-in programs (``fake_stack/``), behind the
runner of ``test_marvel0_stack_act_processes.py`` that maps ``systemctl`` and
``systemd-run`` onto ``Popen`` (the stop signal, the time to stop and
``SendSIGKILL`` are honoured from the properties the page passed). Nothing but a
stand-in is ever run, nothing touches a robot, no protected port is used."""

import os
import signal
import socket
import subprocess
import time

import pytest
from fake_stack import fake_specs
from test_marvel0_stack_act_processes import (
    PORT,
    ProcessMachine,
    standin_table,
    start,
)

from levi.marvel0 import net, record, stack


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**options):
        m = ProcessMachine(tmp_path / f"m{len(made)}", **options)
        made.append(m)
        return m

    yield build
    for m in made:
        m.processes.reap()
    assert (
        subprocess.run(
            ["pgrep", "-f", str(fake_specs.HERE / "fake_")],
            capture_output=True,
            check=False,
        ).returncode
        == 1
    ), "a stand-in program was left running"


def bring_up(m):
    m.ready_for_a()
    start(m, "a")
    m.wait_for("a", "ready")
    start(m, "b")
    m.wait_for("b", "ready")
    m.act.preflight_c({"request_id": "pre-c-00000001"})
    assert m.doc()["checks"]["c_preflight"]["ok"] is True
    start(m, "c")
    m.wait_for("c", "ready")


def press(m, rid):
    return m.act.sequence_stop(m.stop_all_body(rid))


def run_to_the_end(m, limit_s=30.0):
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        seq = m.act.advance()
        if seq["state"] != "running":
            return seq
        time.sleep(0.05)
    raise AssertionError(f"the guided stop did not settle: {m.doc()['sequence']}")


def procs(m):
    return {k: m.processes.units[m.specs.units[k].unit] for k in "abc"}


def stopped_at(m, key):
    return record.read_record(m.state_root / key)["stopped_at"]


def test_stop_whole_stack_ends_three_real_programs_in_order_with_sigint_only(make):
    m = make()
    bring_up(m)
    p = procs(m)
    assert net.listeners(standin_table(), PORT)
    assert press(m, "stop-all-00001") == {"done": "sequence_stop_started"}
    seq = run_to_the_end(m)
    assert seq["state"] == "done"
    assert [(i["terminal"], i["state"]) for i in seq["steps"]] == [
        ("c", "done"),
        ("a", "done"),
        ("b", "done"),
    ]
    # each was told once, with the unit's own stop signal, and left on its own
    assert {k: (x.signals, x.popen.returncode) for k, x in p.items()} == {
        k: (["SIGINT"], 0) for k in "abc"
    }
    # C went before A, A before B (the records carry the time each was done)
    assert stopped_at(m, "c") <= stopped_at(m, "a") <= stopped_at(m, "b")
    assert not net.listeners(standin_table(), PORT)
    t = {k: m.terminal(k)["state"] for k in "abc"}
    assert t == {"a": "idle", "b": "idle", "c": "idle"}
    assert not m.marker_path.exists()  # (released: ours, A gone, the arm quiet)
    # pressed again with nothing left: nothing is stopped twice
    assert press(m, "stop-all-00002") == {"done": "already_stopped"}
    assert {k: x.signals for k, x in p.items()} == {k: ["SIGINT"] for k in "abc"}


def test_a_unit_that_ignores_sigint_ends_it_at_that_step_and_nothing_is_killed(make):
    m = make(a="ignore-sigint", stop_timeout_s=1)
    bring_up(m)
    p = procs(m)
    try:
        press(m, "stop-all-00001")
        seq = run_to_the_end(m)
        assert (seq["state"], seq["reason"]) == ("failed", "stop_failed")
        assert [(i["terminal"], i["state"]) for i in seq["steps"]] == [
            ("c", "done"),
            ("a", "failed"),
            ("b", "not_reached"),
        ]
        assert p["a"].alive() and p["a"].signals == ["SIGINT"]
        assert p["b"].alive() and p["b"].signals == []  # (B was not touched)
        # pressed again: C is skipped, A is sent one more SIGINT and refused
        press(m, "stop-all-00002")
        seq = run_to_the_end(m)
        assert (seq["state"], seq["reason"]) == ("failed", "stop_left_processes")
        assert p["a"].signals == ["SIGINT", "SIGINT"] and p["b"].signals == []
        assert m.doc()["sequence"]["steps"][0]["state"] == "skipped"
    finally:
        os.killpg(p["a"].popen.pid, signal.SIGKILL)  # (the test, not the page)
        p["a"].popen.wait()
    # the processes are gone: the same press acknowledges A and stops B
    press(m, "stop-all-00003")
    seq = run_to_the_end(m)
    assert seq["state"] == "done", str(seq)
    assert [i["state"] for i in seq["steps"]] == ["skipped", "done", "done"]
    assert p["b"].signals == ["SIGINT"] and p["b"].popen.returncode == 0
    # (A's record is closed and its unit is gone; what the page reads of it for
    # the next seconds is the stand-in's last health file, not a unit of ours)
    assert record.read_record(m.state_root / "a")["phase"] == "acknowledged"
    assert m.terminal("a")["owned"] is False


def test_the_stop_is_refused_while_a_client_is_connected_and_stops_nothing(make):
    m = make()
    bring_up(m)
    client = socket.create_connection(("127.0.0.1", PORT), timeout=2)
    try:
        end = time.monotonic() + 5
        codes = []
        while time.monotonic() < end and codes != ["stop_all_client_connected"]:
            codes = [b["code"] for b in m.doc()["stop_all_blockers"]]
            time.sleep(0.05)
        assert codes == ["stop_all_client_connected"]
        with pytest.raises(stack.Refuse) as caught:
            press(m, "stop-all-00001")
        assert caught.value.code == "stop_all_client_connected"
        assert all(x.signals == [] for x in procs(m).values())
    finally:
        client.close()
