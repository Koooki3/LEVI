"""L2 of the fast start: the page's real code, real child processes (the
stand-ins of ``fake_stack/``), real threads, a real listening socket on the
loopback. Nothing but a stand-in is ever run, nothing touches a robot, and no
protected port is used.

* The FCI check leaves no socket behind in the kernel's table (a plain close
  leaves one for a minute; that is what kept A from starting).
* The check of the ROS graph is a real process that takes its time; the checks
  before C are two real processes that run at the same time.
* The whole chain, driven by the real ticker thread, with processes.

These tests start processes and wait for them; they take a few seconds each."""

import socket
import threading
import time

import pytest
from fake_stack import fake_specs
from test_marvel0_stack_act_chain_processes import ChainMachine, a_record, procs
from test_marvel0_stack_act_processes import PORT

from levi.marvel0 import net, stack_act

# --- the FCI check and the kernel's table ------------------------------------------------


class Listener:
    """A loopback server that takes every connection and closes it once the
    client has closed or reset it (the arm's FCI port is the real one; this is a
    stand-in on an unprotected port). It never closes first: the side that
    closes first keeps the TIME_WAIT socket, and on a loaded machine a server
    that closed at once sometimes beat the client to it, so the client's socket
    these tests count was not there (2026-10-27, pytest -n 4)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        assert self.port not in stack_act.PROTECTED_PORTS
        self.sock.settimeout(0.05)
        self.alive = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while self.alive:
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    while conn.recv(1024):
                        pass
                except OSError:  # a reset, or a client that never closes
                    pass

    def close(self):
        self.alive = False
        self.thread.join(2)
        self.sock.close()


def leftovers(port):
    """The sockets of the kernel's table that are connections to ``port`` that
    ended and wait out their time (TIME_WAIT, state 06)."""
    table = net.read()
    assert table.ok
    return [s for s in table.socks if s.remote_port == port and s.state == "06"]


@pytest.fixture
def listener():
    server = Listener()
    yield server
    server.close()


def test_a_plain_close_leaves_a_socket_the_arm_guard_counts_for_a_minute(listener):
    """The control: this is what the old check did to the machine."""
    client = socket.create_connection(("127.0.0.1", listener.port), timeout=2)
    client.close()
    time.sleep(0.3)
    assert len(leftovers(listener.port)) == 1


def test_the_fci_check_leaves_no_socket_behind(listener):
    answer = stack_act.tcp_probe("127.0.0.1", listener.port)
    assert answer == {"state": "open"}
    time.sleep(0.3)
    assert leftovers(listener.port) == [], "a TIME_WAIT socket would hold A back"
    # ... however often it is asked
    for _ in range(5):
        assert stack_act.tcp_probe("127.0.0.1", listener.port)["state"] == "open"
    time.sleep(0.3)
    assert leftovers(listener.port) == []


def test_a_port_nobody_listens_on_is_closed_and_leaves_nothing():
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    assert stack_act.tcp_probe("127.0.0.1", port) == {"state": "closed"}
    assert leftovers(port) == []


@pytest.mark.parametrize("port", stack_act.PROTECTED_PORTS)
def test_a_protected_port_is_never_connected_to(port):
    with pytest.raises(ValueError):
        stack_act.tcp_probe("127.0.0.1", port)


# --- the checks as real processes ---------------------------------------------------------


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


def probe_request(n=1):
    return {"request_id": f"fast-proc-probe-{n:05d}"}


def timed(call):
    began = time.monotonic()
    out = call()
    return out, time.monotonic() - began


def test_the_check_of_the_graph_is_a_real_process_that_takes_its_time(make):
    m = make(nodes_delay_s=0.4)
    m.m_ready()
    _, took = timed(lambda: m.act.probe_b(probe_request()))
    # (a check a person asks for is two answers of the graph, one after the
    # other: B starts only on a confirmed absence)
    assert took >= 0.8
    probe = m.act._b_probe
    assert (probe["state"], probe["by"], probe["graph"]) == (
        "absent",
        "nodes",
        "absent",
    )
    assert probe["confirmed"] is True
    # the stand-in printed the monitor's node and nothing of the gripper's
    cmds = [c for c in m.processes.commands if "fake_oneshot.py" in " ".join(c)]
    # (the same command; the unit names differ: unique per call)
    assert (
        len(cmds) == 2
        and cmds[0][cmds[0].index("--") :] == cmds[1][cmds[1].index("--") :]
    )
    cmd = cmds[0]
    assert "nodes" in cmd[cmd.index("--") + 1 :] or any("nodes" in a for a in cmd)
    assert m.terminal("b")["reason"] == "b.not_running"


def test_a_gripper_stack_started_by_hand_is_seen_in_the_graph(make):
    m = make(nodes_delay_s=0.1)
    m.m_ready()
    (m.tmp / "b.alive").write_text("")  # (a gripper stack runs somewhere)
    m.act.probe_b(probe_request())
    assert m.act._b_probe["state"] == "present" and m.act._b_probe["by"] == "nodes"
    assert m.terminal("b")["state"] == "foreign_running"


def test_a_graph_that_was_not_discovered_falls_back_to_the_manager(make):
    m = make(nodes="blind")
    m.m_ready()
    m.act.probe_b(probe_request())
    probe = m.act._b_probe
    # (the manager's stand-in warned and was ended: the slow question's answer)
    assert (probe["state"], probe["by"], probe["graph"]) == (
        "absent",
        "probe",
        "unknown",
    )


def test_the_two_checks_before_c_are_two_processes_at_the_same_time(make):
    m = make(controllers_delay_s=1.2, ee_delay_s=1.2)
    m.m_ready()
    m.act.host.parallel = True
    m.m_ready()
    m.act.marker_op("touch", m.body("marker_touch", "fast-touch-proc01"))
    time.sleep(m.specs.marker_wait_s + 0.1)
    m.preflight_a()
    m.act.start("a", m.start_body("a"))
    m.wait_for("a", "ready")
    m.act.start("b", m.start_body("b"))
    m.wait_for("b", "ready")
    _, took = timed(lambda: m.act.preflight_c({"request_id": "fast-proc-pre-c01"}))
    assert m.act._c_pre["ok"] is True
    assert 1.2 <= took < 2.2, "two checks of 1.2 s each must overlap"


def test_one_after_the_other_the_same_checks_take_the_sum(make):
    m = make(controllers_delay_s=1.2, ee_delay_s=1.2)
    m.act.host.parallel = False
    m.m_ready()
    m.act.marker_op("touch", m.body("marker_touch", "fast-touch-proc02"))
    time.sleep(m.specs.marker_wait_s + 0.1)
    m.preflight_a()
    m.act.start("a", m.start_body("a"))
    m.wait_for("a", "ready")
    m.act.start("b", m.start_body("b"))
    m.wait_for("b", "ready")
    _, took = timed(lambda: m.act.preflight_c({"request_id": "fast-proc-pre-c02"}))
    assert took >= 2.4


# --- the whole chain, on the real ticker thread --------------------------------------------


def test_the_chain_runs_to_c_on_the_real_ticker_with_real_processes(make):
    m = make(nodes_delay_s=0.8, controllers_delay_s=0.4, ee_delay_s=0.9)
    m.act.host.background = True
    m.act.host.parallel = True
    m.m_ready()
    stop = threading.Event()

    def monitor():  # (the health monitor keeps writing until A takes it over)
        while not stop.wait(0.1):
            if not procs(m)["a"]:
                m.no_manager()

    threading.Thread(target=monitor, daemon=True).start()
    began = time.monotonic()
    assert m.act.sequence_start(m.chain_body("chain-fast-proc01")) == {
        "done": "sequence_started"
    }
    started = {}
    seq = {}
    try:
        while time.monotonic() - began < 30:
            seq = m.doc()["sequence"]
            for key in "abc":
                if key not in started and procs(m)[key] is not None:
                    started[key] = time.monotonic() - began
            if seq["state"] != "running":
                break
            time.sleep(0.02)
    finally:
        stop.set()
    took = time.monotonic() - began
    assert (seq["state"], seq["reason"]) == ("done", None), seq
    assert {k: m.terminal(k)["state"] for k in "abc"} == {k: "ready" for k in "abc"}
    # A did not wait for the marker's old fixed wait, nor for the slow question:
    # it waited for the check of the graph (0.8 s), the marker's age (0.3 s) and
    # little else
    assert 0.8 <= started["a"] < 2.5, started
    assert took < 12, (took, started)
    assert [a_record(m, k)["request_id"] for k in "abc"] == ["chain-fast-proc01"] * 3
    assert all(p.signals == [] for p in procs(m).values())  # (nothing was stopped)
    # the ticker has let go
    deadline = time.monotonic() + 3
    while m.act._ticker is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert m.act._ticker is None
    # every step says what it took
    took_s = {i["step"]: i["took_s"] for i in seq["steps"]}
    assert all(isinstance(v, float) for v in took_s.values()), took_s
    assert took_s["c_preflight"] >= 0.9  # (the slower of the two checks)
    assert PORT not in (5000, 5001, 5100, 8000)  # never a robot port
    # and the way out is the same as ever. The monitor writes again once A is
    # gone (as drive() does in the chain tests): B is stopped last, and on a slow
    # runner the health file of A's time was stale by then (CI run 39721849462,
    # both legs, B not idle; never on this machine)
    stop.clear()
    threading.Thread(target=monitor, daemon=True).start()
    try:
        assert m.act.sequence_stop(m.stop_all_body("stop-all-fast01")) == {
            "done": "sequence_stop_started"
        }
        for _ in range(300):
            seq = m.act.advance()
            if seq["state"] != "running":
                break
            time.sleep(0.05)
    finally:
        stop.set()
    assert (seq["state"], seq["reason"]) == ("done", None), seq
    assert {k: m.terminal(k)["state"] for k in "abc"} == {k: "idle" for k in "abc"}
