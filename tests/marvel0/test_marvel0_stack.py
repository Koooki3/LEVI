"""The arm stack of the marvel0 page (``levi/marvel0/stack.py``, S4-0): the
passive state of M, A, B, C, the policy server and the client, the dry-run
start gates, the page's management of the health monitor M, the marker
release, the product-only rule and the machine-wide stack lock.

The machine is a fake one: ``systemctl``, ``systemd-run`` and ``loginctl``
are answered by ``Machine.run`` (which records every command), the socket
tables and ``arm_guard`` are values (the setup probes' process scan is never
asked: the tests make it raise), and the
health file, the marker, the lock and ``/proc/locks`` are files under
``tmp_path``. No unit is started or stopped, no process is signalled and no
port is opened."""

import fcntl
import json
import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from levi.marvel0 import net, stack, stack_api

PRODUCT_CGROUP = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/levi-product.service\n"
)
SHELL_CGROUP = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/vte-spawn-1.scope\n"
)
NOW = 1_800_000_000.0


def _sock(port, state, remote_port=0, remote="127.0.0.1"):
    return net.Sock(
        "tcp", port, remote_port, state, 1, local_addr="127.0.0.1", remote_addr=remote
    )


class Machine:
    """The fake machine. Attributes say what it shows; ``calls`` what was run."""

    def __init__(self, tmp_path, *, product=True):
        self.tmp = tmp_path
        self.franka = tmp_path / "franka_control"
        (self.franka / "run").mkdir(parents=True)
        (self.franka / "scripts").mkdir()
        self.script = self.franka / "scripts" / "fr3_health_monitor.sh"
        self.script.write_text("#!/bin/sh\n")
        self.script.chmod(0o755)
        self.health_path = self.franka / "run" / "fr3_health.json"
        self.hub = tmp_path / "levi-hub"
        self.hub.mkdir()
        self.marker_path = self.hub / ".arm-controller-active"
        self.locks = tmp_path / "locks"
        self.locks.write_text("")
        self.cgroup_text = PRODUCT_CGROUP if product else SHELL_CGROUP
        self.main = True
        self.calls = []
        self.unlinked = []
        # What the machine shows.
        self.monitor = None  # None: no such unit; else (ActiveState, argv, pid)
        self.stack_units = {}  # "A"/"B"/"C" -> ActiveState
        self.systemd_down = False
        self.sockets = {"state": "inactive", "detail": "no socket to the arm"}
        self.socks = []
        self.table_ok = True
        self.sessions = {"state": "ok", "roots": 1, "sessions": [], "detail": None}
        self.rtprio, self.memlock, self.linger = "99", "infinity", "no"
        self.run_codes = {}  # first argv word after --user -> exit code
        # The reconcile tests run with the management on; its default (off) has its own test.
        self.environ = {stack.AUTO_ENV: "on"}
        self.clock = 1000.0

    # -- the health file
    def health(self, age=0.1, pid=4242, hardware=True, controller=True, red=False):
        self.health_path.write_text(
            json.dumps(
                {
                    "updated_at_epoch": NOW - age,
                    "monitor_pid": pid,
                    "hardware_active": hardware,
                    "controller_active": controller,
                    "robot_mode_name": "MOVE",
                    "red_light": red,
                }
            )
        )

    # -- the command runner
    def run(self, argv, timeout_s):
        self.calls.append(list(argv))
        if self.systemd_down and argv[0] == "systemctl":
            return 1, "Failed to connect to bus"
        if argv[0] == "loginctl":
            return 0, f"Linger={self.linger}\n"
        if argv[0] == "systemd-run":
            return self.run_codes.get("systemd-run", 0), ""
        verb = argv[2]
        if verb == "show" and len(argv) == 4:  # the manager's own properties
            return 0, (
                f"DefaultLimitRTPRIO={self.rtprio}\nDefaultLimitMEMLOCK={self.memlock}\n"
            )
        if verb == "show":
            unit = argv[3]
            if unit == stack.MONITOR_SERVICE:
                return 0, self._monitor_show()
            for key, name in stack.STACK_UNITS.items():
                if unit == name:
                    state = self.stack_units.get(key)
                    if state is None:
                        return (
                            0,
                            f"Id={name}\nLoadState=not-found\nActiveState=inactive\n",
                        )
                    return 0, f"Id={name}\nLoadState=loaded\nActiveState={state}\n"
            raise AssertionError(f"unexpected show of {unit}")
        if verb in ("stop", "reset-failed"):
            return self.run_codes.get(verb, 0), ""
        raise AssertionError(f"unexpected command {argv}")

    def _monitor_show(self):
        if self.monitor is None:
            return (
                f"Id={stack.MONITOR_SERVICE}\nLoadState=not-found\n"
                "ActiveState=inactive\nSubState=dead\nExecStart=\nMainPID=0\n"
            )
        active, argv, pid = self.monitor
        exec_start = (
            f"{{ path={argv[0]} ; argv[]={' '.join(argv)} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
        )
        return (
            f"Id={stack.MONITOR_SERVICE}\nLoadState=loaded\nActiveState={active}\n"
            f"SubState=running\nResult=success\nExecStart={exec_start}\n"
            f"MainPID={pid}\nNRestarts=0\n"
        )

    def ours(self, active="active", pid=4242):
        self.monitor = (active, [str(self.script)], pid)

    def host(self):
        return stack.Host(
            run=stack.guarded(self.run),
            marker=lambda: self.marker_path,
            lock_path=lambda: self.hub / stack.LOCK_FILE,
            health_file=lambda: (str(self.health_path), 3.0),
            arm_sockets=lambda: dict(self.sockets),
            sessions=lambda now: self.sessions,
            net_table=lambda: net.Table(self.table_ok, tuple(self.socks)),
            cgroup=lambda: self.cgroup_text,
            in_main_checkout=lambda: self.main,
            policy_port=lambda: 8000,
            environ=self.environ,
            locks_path=str(self.locks),
            uid=1000,
            now=lambda: NOW,
            monotonic=lambda: self.clock,
            unlink=self._unlink,
        )

    def _unlink(self, path):
        self.unlinked.append(path)
        os.unlink(path)

    def stack(self):
        return stack.Stack(self.host())

    # -- today's state: A and C started by hand, the marker set
    def arm_up(self):
        self.sockets = {"state": "active", "detail": "2 control sockets"}
        self.socks = [_sock(5000, net.LISTEN)]
        self.marker_path.write_text("")

    def starts(self):
        return [c for c in self.calls if c[0] == "systemd-run"]

    def stops(self):
        return [c for c in self.calls if c[:3] == ["systemctl", "--user", "stop"]]


@pytest.fixture
def m(tmp_path):
    return Machine(tmp_path)


def body(action, rid="req-00000001"):
    return {"request_id": rid, "confirm": stack.PHRASES[action]}


# --- product-only -----------------------------------------------------------------------


def test_real_only_inside_levi_product_from_the_main_checkout():
    assert stack.profile(PRODUCT_CGROUP, True)["profile"] == "real"
    assert stack.profile(PRODUCT_CGROUP, False) == {
        "profile": "fake",
        "code": "profile.not_main_checkout",
    }
    assert stack.profile(SHELL_CGROUP, True)["code"] == "profile.not_product_unit"
    assert stack.profile(None, True)["code"] == "profile.cgroup_unreadable"
    # A unit whose name merely contains the product's is not it.
    other = "0::/user.slice/app.slice/my-levi-product.service.d/x.scope\n"
    assert stack.profile(other, True)["profile"] == "fake"


def test_a_worktree_instance_refuses_every_write_and_runs_nothing(tmp_path):
    m = Machine(tmp_path, product=False)
    m.ours("failed")
    m.arm_up()
    sup = m.stack()
    doc = sup.view()
    assert doc["profile"] == "fake"
    assert not doc["monitor"]["can_start"] and not doc["monitor"]["can_stop"]
    assert "not_product" in doc["marker"]["clear_blockers"]
    for call, action in (
        (sup.start_monitor, "monitor_start"),
        (sup.stop_monitor, "monitor_stop"),
        (sup.clear_marker, "marker_clear"),
    ):
        with pytest.raises(stack.Refuse) as caught:
            call(body(action, f"req-{action}-1"))
        assert caught.value.code == "not_product"
    assert sup.reconcile() == {"action": "none", "code": "auto.not_product"}
    assert stack.Reconciler.start(sup) is None
    assert not m.starts() and not m.stops()
    assert m.marker_path.exists()
    assert not (m.hub / stack.LOCK_FILE).exists()  # the lock is not even created


def test_a_product_cgroup_outside_the_main_checkout_is_fake(m):
    m.main = False
    assert m.stack().view()["profile"] == "fake"


# --- the command guard ------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["kill", "-INT", "1"],
        ["pkill", "-f", "fr3"],
        ["bash", "-c", "true"],
        ["ros2", "control", "list_controllers"],
        ["systemctl", "stop", "x.service"],
        ["systemd-run", "--system", "true"],
        ["loginctl", "terminate-user", "1000"],
        [],
    ],
)
def test_the_runner_refuses_everything_but_user_systemd_and_loginctl_show(argv):
    ran = []
    call = stack.guarded(lambda a, t: ran.append(a) or (0, ""))
    with pytest.raises(ValueError):
        call(argv, 1)
    assert not ran


# --- reading M --------------------------------------------------------------------------


def test_the_running_monitor_is_adopted_by_id_and_exec_start(m):
    m.ours()
    m.health(pid=4242)
    doc = m.stack().view()
    assert doc["monitor"]["unit_state"] == "running" and doc["monitor"]["owned"]
    assert doc["terminals"]["M"]["state"] == "running"
    assert {"code": "m.pid_matches", "value": True} in doc["terminals"]["M"]["evidence"]
    assert not m.starts()


def test_a_unit_of_that_name_with_another_command_is_foreign(m):
    m.monitor = ("active", ["/usr/bin/python3", "other.py"], 7)
    m.health()
    doc = m.stack().view()
    assert doc["terminals"]["M"]["state"] == "foreign"
    assert not doc["monitor"]["owned"]
    assert "monitor_foreign_unit" in doc["monitor"]["start_blockers"]
    assert "monitor_not_ours" in doc["monitor"]["stop_blockers"]


def test_a_fresh_health_file_without_our_unit_is_a_monitor_elsewhere(m):
    m.health(age=0.5)
    doc = m.stack().view()
    assert doc["terminals"]["M"]["state"] == "elsewhere"
    assert "monitor_elsewhere" in doc["monitor"]["start_blockers"]


def test_an_unreadable_systemd_is_unknown_never_absent(m):
    m.systemd_down = True
    doc = m.stack().view()
    assert doc["terminals"]["M"]["state"] == "unknown"
    assert "systemd_unreadable" in doc["monitor"]["start_blockers"]
    assert doc["gates"]["A"]["verdict"] == "would_refuse"
    assert "marker_a_unit" in doc["marker"]["clear_blockers"]


# --- starting and stopping M ------------------------------------------------------------


def test_start_runs_one_transient_unit_like_the_policy_server(m):
    m.arm_up()
    answer = m.stack().start_monitor(body("monitor_start"))
    assert answer == {"done": "monitor_started", "unit": stack.MONITOR_SERVICE}
    [argv] = m.starts()
    assert argv[:3] == ["systemd-run", "--user", "--quiet"]
    for prop in (
        f"--unit={stack.MONITOR_UNIT}",
        "--collect",
        "--property=KillSignal=SIGINT",
        "--property=TimeoutStopSec=10",
        "--property=Nice=10",
        "--property=KillMode=control-group",
        "--property=Restart=no",
    ):
        assert prop in argv
    assert argv[-2:] == ["--", str(m.script)]
    # The lock was taken and says who took it.
    note = json.loads((m.hub / stack.LOCK_FILE).read_text())
    assert note["owner"] == "marvel0-page" and note["action"] == "monitor_start"


def test_start_is_refused_while_it_runs_or_elsewhere_or_without_a_script(m):
    m.ours()
    m.health()
    with pytest.raises(stack.Refuse) as caught:
        m.stack().start_monitor(body("monitor_start"))
    assert caught.value.code == "monitor_running"
    m.monitor = None
    with pytest.raises(stack.Refuse) as caught:
        m.stack().start_monitor(body("monitor_start"))
    assert caught.value.code == "monitor_elsewhere"
    m.health_path.unlink()
    m.script.chmod(0o644)
    with pytest.raises(stack.Refuse) as caught:
        m.stack().start_monitor(body("monitor_start"))
    assert caught.value.code == "monitor_script_missing"
    assert not m.starts()


def test_a_failed_monitor_of_ours_is_reset_before_it_starts_again(m):
    m.ours("failed")
    m.stack().start_monitor(body("monitor_start"))
    reset = [c for c in m.calls if "reset-failed" in c]
    assert reset == [["systemctl", "--user", "reset-failed", stack.MONITOR_SERVICE]]
    assert len(m.starts()) == 1


def test_a_failing_systemd_run_is_start_failed(m):
    m.run_codes["systemd-run"] = 1
    with pytest.raises(stack.Refuse) as caught:
        m.stack().start_monitor(body("monitor_start"))
    assert caught.value.code == "start_failed" and caught.value.status == 502


def test_stop_comes_last_after_a_and_c(m):
    m.ours()
    m.health()
    m.arm_up()
    with pytest.raises(stack.Refuse) as caught:
        m.stack().stop_monitor(body("monitor_stop"))
    assert caught.value.code == "stack_running"
    assert not m.stops()


def test_stop_names_only_the_monitor_unit(m):
    m.ours()
    m.health(hardware=False, controller=False)
    answer = m.stack().stop_monitor(body("monitor_stop"))
    assert answer["done"] == "monitor_stopped"
    assert m.stops() == [["systemctl", "--user", "stop", stack.MONITOR_SERVICE]]


def test_stop_is_refused_when_the_unit_is_not_ours(m):
    with pytest.raises(stack.Refuse) as caught:
        m.stack().stop_monitor(body("monitor_stop"))
    assert caught.value.code == "monitor_not_ours"


# --- requests ---------------------------------------------------------------------------


def test_a_repeated_request_is_answered_and_not_done_twice(m):
    sup = m.stack()
    first = sup.start_monitor(body("monitor_start"))
    again = sup.start_monitor(body("monitor_start"))
    assert again == {**first, "repeated": True}
    assert len(m.starts()) == 1
    with pytest.raises(stack.Refuse) as caught:
        sup.stop_monitor(body("monitor_stop"))  # the same id, another action
    assert caught.value.code == "request_id_used"


@pytest.mark.parametrize("rid", ["short", "x" * 81, "has space 1", 12345678])
def test_a_bad_request_id_is_refused(m, rid):
    with pytest.raises(stack.Refuse) as caught:
        m.stack().start_monitor({"request_id": rid, "confirm": "start health monitor"})
    assert caught.value.code == "request_id_invalid"


def test_the_phrase_must_be_typed_exactly(m):
    with pytest.raises(stack.Refuse) as caught:
        m.stack().start_monitor({"request_id": "req-00000002", "confirm": "start"})
    assert caught.value.code == "confirm_required"
    assert "start health monitor" in caught.value.message
    assert not m.starts()


# --- the machine-wide lock --------------------------------------------------------------


def test_a_held_stack_lock_refuses_the_action_without_waiting(m):
    path = m.hub / stack.LOCK_FILE
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(stack.Refuse) as caught:
            m.stack().start_monitor(body("monitor_start"))
        assert caught.value.code == "stack_busy" and caught.value.status == 423
    finally:
        os.close(fd)
    assert not m.starts()


def test_the_lock_state_comes_from_the_lock_table(m):
    path = m.hub / stack.LOCK_FILE
    lock = stack.StackLock(path, str(m.locks))
    assert lock.state() == {"state": "free", "holder": None}
    with lock.hold("someone", "monitor_start"):
        st = os.stat(path)
        m.locks.write_text(
            f"1: FLOCK  ADVISORY  WRITE {os.getpid()} "
            f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n"
        )
        held = lock.state()
    assert held["state"] == "held" and held["holder"]["owner"] == "someone"
    m.locks.write_text("")
    assert lock.state()["state"] == "free"
    (m.tmp / "nolocks").mkdir()
    assert stack.StackLock(path, str(m.tmp / "nolocks")).state()["state"] == "unknown"


def test_the_lock_file_lives_next_to_the_marker():
    assert stack.LOCK_FILE == ".marvel0-stack.lock"
    host = stack.production_host()
    assert host.lock_path().parent == host.marker().parent
    assert host.lock_path().parent.parts[-2:] == ("lab", "levi-hub")


# --- the automatic management of M ------------------------------------------------------


def test_m_is_started_when_the_stack_runs_and_left_alone_after(m):
    m.arm_up()
    sup = m.stack()
    result = sup.reconcile()
    assert result["action"] == "start" and result["ok"]
    assert result["code"] == "auto.start_for_stack"
    assert json.loads((m.hub / stack.LOCK_FILE).read_text())["owner"] == "marvel0-auto"
    m.ours()
    m.health()
    assert sup.reconcile() == {"action": "none", "code": "auto.running"}
    assert len(m.starts()) == 1


def test_a_monitor_that_died_while_the_stack_runs_is_started_again(m):
    m.arm_up()
    m.ours("failed")
    result = m.stack().reconcile()
    assert result["code"] == "auto.restart_failed" and result["ok"]
    assert any("reset-failed" in c for c in m.calls)


def test_restarts_back_off_after_three_in_ten_minutes(m):
    m.arm_up()
    sup = m.stack()
    for _ in range(stack.RESTART_MAX):
        assert sup.reconcile()["action"] == "start"
        m.clock += 30
    assert sup.reconcile() == {"action": "none", "code": "auto.backoff"}
    m.clock += stack.RESTART_WINDOW_S
    assert sup.reconcile()["action"] == "start"


def test_m_stops_last_only_after_the_stack_has_looked_down_for_the_grace(m):
    m.ours()
    m.health(hardware=False, controller=False)
    sup = m.stack()
    assert sup.reconcile() == {"action": "none", "code": "auto.wait_stack_down"}
    m.clock += stack.STOP_GRACE_S - 1
    assert sup.reconcile()["action"] == "none"
    m.arm_up()  # A came back: the countdown starts again
    assert sup.reconcile()["code"] == "auto.running"
    m.sockets = {"state": "inactive", "detail": ""}
    m.socks = []
    m.health(hardware=False, controller=False)
    sup.reconcile()
    m.clock += stack.STOP_GRACE_S
    result = sup.reconcile()
    assert result["action"] == "stop" and result["ok"]
    assert m.stops() == [["systemctl", "--user", "stop", stack.MONITOR_SERVICE]]


def test_nothing_is_done_when_the_stack_cannot_be_told(m):
    m.sockets = {"state": "unknown", "detail": "table unreadable"}
    assert m.stack().reconcile() == {"action": "none", "code": "auto.stack_unknown"}
    assert not m.starts()


def test_a_monitor_elsewhere_or_foreign_is_never_replaced(m):
    m.arm_up()
    m.health()
    assert m.stack().reconcile()["code"] == "auto.leave_elsewhere"
    m.monitor = ("active", ["/bin/other"], 9)
    assert m.stack().reconcile()["code"] == "auto.leave_foreign"
    assert not m.starts() and not m.stops()


def test_the_setting_turns_the_automatic_management_off(m):
    m.arm_up()
    m.environ[stack.AUTO_ENV] = "off"
    sup = m.stack()
    assert sup.reconcile() == {"action": "none", "code": "auto.off"}
    assert stack.Reconciler.start(sup) is None
    assert sup.view()["monitor"]["auto"] is False
    assert not m.starts()


@pytest.mark.parametrize("value", [None, "", "off", "auto", "true"])
def test_the_automatic_management_is_off_unless_set_to_on(m, value):
    m.arm_up()
    if value is None:
        del m.environ[stack.AUTO_ENV]
    else:
        m.environ[stack.AUTO_ENV] = value
    sup = m.stack()
    assert sup.reconcile() == {"action": "none", "code": "auto.off"}
    assert stack.Reconciler.start(sup) is None
    assert not m.starts()


def test_the_loop_runs_only_in_the_product(m):
    m.environ[stack.AUTO_ENV] = "on"
    loop = stack.Reconciler.start(m.stack())
    try:
        assert loop is not None and loop._thread.daemon
    finally:
        loop.close()


# --- the marker -------------------------------------------------------------------------


def test_the_marker_is_cleared_only_with_no_a_unit_and_no_control_socket(m):
    m.marker_path.write_text("")
    doc = m.stack().view()
    assert doc["marker"]["can_clear"], doc["marker"]["clear_blockers"]
    assert m.stack().clear_marker(body("marker_clear")) == {"done": "marker_cleared"}
    assert m.unlinked == [m.marker_path]
    assert not m.marker_path.exists()


@pytest.mark.parametrize(
    "setup, code",
    [
        (lambda m: m.arm_up(), "marker_arm_active"),
        (
            lambda m: setattr(m, "sockets", {"state": "unknown", "detail": ""}),
            "marker_arm_active",
        ),
        (lambda m: m.stack_units.update(A="inactive"), "marker_a_unit"),
        (lambda m: setattr(m, "systemd_down", True), "marker_a_unit"),
        (lambda m: m.health(hardware=True), "marker_arm_active"),
    ],
)
def test_the_marker_stays_while_a_may_run(m, setup, code):
    m.marker_path.write_text("")
    setup(m)
    with pytest.raises(stack.Refuse) as caught:
        m.stack().clear_marker(body("marker_clear"))
    assert caught.value.code == code
    assert m.marker_path.exists() and not m.unlinked


def test_no_marker_to_clear(m):
    with pytest.raises(stack.Refuse) as caught:
        m.stack().clear_marker(body("marker_clear"))
    assert caught.value.code == "marker_absent"


def test_a_marker_that_is_a_link_is_not_removed(m):
    target = m.tmp / "elsewhere"
    target.write_text("")
    m.marker_path.symlink_to(target)
    with pytest.raises(stack.Refuse) as caught:
        m.stack().clear_marker(body("marker_clear"))
    assert caught.value.code == "marker_unknown"
    assert target.exists() and m.marker_path.is_symlink()


def test_the_marker_age_includes_its_ctime(m):
    m.marker_path.write_text("")
    old = time.time() - 7200
    os.utime(m.marker_path, (old, old))  # what `touch -d` can do
    doc = m.stack().view()
    assert doc["marker"]["state"] == "present"
    assert doc["marker"]["ctime_age_s"] is not None


# --- the terminals and the dry-run gates ------------------------------------------------


def test_todays_hand_started_stack_is_seen_and_every_gate_refuses(m):
    m.arm_up()
    m.ours()
    m.health()
    m.socks.append(_sock(40000, net.ESTABLISHED, remote_port=8000))
    doc = m.stack().view()
    t = doc["terminals"]
    assert t["A"]["state"] == "running" and t["C"]["state"] == "running"
    assert t["B"] == {"state": "unknown", "evidence": [], "code": "b.not_observable"}
    assert t["client"]["state"] == "running"
    a = doc["gates"]["A"]
    assert a["verdict"] == "would_refuse" and a["route"] is False
    assert "gate.a_absent" in a["reasons"] and "gate.fci_probe" in a["reasons"]
    assert "gate.linger" not in a["reasons"]  # a warning never refuses
    assert "gate.b_absent" in doc["gates"]["B"]["reasons"]
    assert "gate.no_client" in doc["gates"]["C"]["reasons"]
    # Rule R: B is unknown, so every later step refuses on it.
    assert "gate.b_known" in doc["gates"]["C"]["reasons"]
    for gate in ("A", "B", "C"):
        assert "gate.layers_known" in doc["gates"][gate]["reasons"]
    assert doc["verdict"]["verdict"] == "busy"
    assert {"code": "notice.logout_stops_stack"} in doc["notices"]
    assert {"code": "notice.gates_scope"} in doc["notices"]


def test_a_stopped_c_with_the_impedance_controller_still_active(m):
    m.ours()
    m.health(hardware=True, controller=True)
    doc = m.stack().view()
    assert doc["terminals"]["C"]["state"] == "stopped"
    assert doc["terminals"]["C"]["code"] == "c.stopped_impedance"


def test_unreadable_tables_are_unknown(m):
    m.table_ok = False
    m.sockets = {"state": "unknown", "detail": ""}
    t = m.stack().view()["terminals"]
    assert {t[k]["state"] for k in ("A", "C", "policy", "client")} == {"unknown"}


def test_the_limits_and_linger_are_read_passively(m):
    m.rtprio, m.linger = "0", "yes"
    doc = m.stack().view()
    assert doc["rt_limits"] == {"state": "low", "rtprio": 0, "memlock": "infinity"}
    assert doc["linger"] == {"state": "yes"}
    a = {c["code"]: c for c in doc["gates"]["A"]["checks"]}
    assert a["gate.rt_limits"]["ok"] is False and a["gate.linger"]["ok"] is True


def test_the_document_names_no_path(m):
    m.arm_up()
    m.ours()
    m.health()
    text = json.dumps(m.stack().view())
    assert str(m.tmp) not in text and "/home/" not in text


# --- the routes -------------------------------------------------------------------------


@pytest.fixture
def client(m, monkeypatch):
    sup = m.stack()
    monkeypatch.setattr(stack_api, "supervisor", lambda: sup)
    monkeypatch.setattr(stack_api, "_VIEW", [])
    monkeypatch.delenv("LEVI_UI_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(stack_api.router)
    return TestClient(app)


def test_the_view_route(client, m):
    m.arm_up()
    answer = client.get("/api/levi/marvel0/stack")
    assert answer.status_code == 200
    assert answer.json()["schema"] == stack.SCHEMA


def test_a_refusal_has_the_error_shape(client):
    answer = client.post(
        "/api/levi/marvel0/stack/marker/clear",
        json={"request_id": "req-00000009", "confirm": "clear marker"},
    )
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "marker_absent"


def test_writes_refuse_an_agent(client, m):
    for path in ("monitor/start", "monitor/stop", "marker/clear"):
        answer = client.post(
            f"/api/levi/marvel0/stack/{path}",
            json={"request_id": "req-00000010", "confirm": "x"},
            headers={"Authorization": "Bearer agent"},
        )
        assert answer.status_code == 403
    assert not m.starts() and not m.stops()


def test_no_route_can_start_or_stop_a_b_or_c():
    paths = {route.path for route in stack_api.router.routes}
    assert paths == {
        "/api/levi/marvel0/stack",
        "/api/levi/marvel0/stack/monitor/start",
        "/api/levi/marvel0/stack/monitor/stop",
        "/api/levi/marvel0/stack/marker/clear",
    }
    source = Path(stack.__file__).read_text(encoding="utf-8")
    for unit in stack.STACK_UNITS.values():
        # The names are only ever asked about (show), never started or stopped.
        assert source.count(unit) == 1


def test_every_refusal_code_is_a_known_one():
    for code, (status, message) in stack.REFUSALS.items():
        assert (
            400 <= status < 600
            and message
            and "/" not in message.replace("A-Z, a-z", "")
        ), code


def test_arm_guards_socket_verdict_leaves_the_marker_out(tmp_path):
    """The lab's own arm_guard.py (outside the repository: skipped without it)
    judges the tables; the marker path given to it can never exist."""
    root = Path(stack.__file__).resolve().parents[2]
    hubs = [p / "lab" / "levi-hub" for p in (root, *root.parents)]
    hub = next((h for h in hubs if (h / "arm_guard.py").is_file()), None)
    if hub is None:
        pytest.skip("no lab arm_guard.py above this checkout")
    guard = stack._load_arm_guard(hub)
    setup = tmp_path / "setup.md"
    setup.write_text("robot_ip:=10.0.0.9\n")
    tables = tmp_path / "net"
    tables.mkdir()
    header = "  sl  local_address rem_address   st\n"
    for name in ("tcp6", "udp", "udp6"):
        (tables / name).write_text(header)
    # 10.0.0.9:1337 (0x0539), ESTABLISHED.
    (tables / "tcp").write_text(header + "   0: 0100007F:9C40 0900000A:0539 01\n")
    assert not stack.NO_MARKER.exists()
    state, _ = guard.status(setup=setup, net=tables, marker=stack.NO_MARKER)
    assert state == "active"
    (tables / "tcp").write_text(header)
    state, _ = guard.status(setup=setup, net=tables, marker=stack.NO_MARKER)
    assert state == "inactive"


def test_a_missing_unit_and_a_stale_health_file_are_named_as_such(m):
    """2026-10-20 13:5x: the unit was gone (not-found) and the file 13:38 old."""
    m.health(age=1200.0)
    terminal = m.stack().view()["terminals"]["M"]
    assert terminal["state"] == "stopped"
    assert terminal["code"] == "m.absent_health_stale"
    assert {"code": "health.age_s", "value": 1200.0} in terminal["evidence"]
    m.health_path.unlink()
    assert m.stack().view()["terminals"]["M"]["code"] == "m.absent_no_health"
    m.ours("inactive")
    m.health(age=60.0)
    assert m.stack().view()["terminals"]["M"]["code"] == "m.stopped_health_stale"


# --- no process scan, and rule R --------------------------------------------------------


@pytest.fixture
def no_scan(monkeypatch):
    """The setup probes' process scan must never be reached from here."""
    from levi.setup import probes

    def refuse(*args, **kwargs):
        raise AssertionError("the stack read the setup probes' process scan")

    monkeypatch.setattr(probes, "status", refuse)
    monkeypatch.setattr(probes.Probes, "_robot_processes", refuse)
    monkeypatch.setattr(probes.Probes, "_robot", refuse)


@pytest.mark.parametrize(
    "guard_robot",
    [
        [{"pid": 1, "name": "ros2_control_node"}, {"pid": 2, "name": "franka_server"}],
        [],
        None,  # no guard part at all
    ],
)
def test_every_layer_is_the_same_whatever_guard_robot_says(m, no_scan, guard_robot):
    """guard.robot (a scan of other processes' /proc/<pid>/cmdline) is not a
    source: full, empty or missing, the document is the same."""
    m.arm_up()
    m.ours()
    m.health()
    baseline = m.stack().view()
    host = m.host()
    sample = (
        {} if guard_robot is None else {"guard": {"state": "ok", "robot": guard_robot}}
    )
    assert not hasattr(host, "sample")
    # Even a host that carried the sample would not hand it on.
    object.__setattr__(host, "sample", lambda: sample)
    again = stack.Stack(host).view()
    for key in ("terminals", "gates", "verdict", "marker", "monitor"):
        assert again[key] == baseline[key], key


def test_the_module_does_not_name_the_process_scan():
    source = Path(stack.__file__).read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]  # past the module's docstring
    for name in (
        "guard.robot",
        '"robot"',
        "_robot_processes",
        "probes.status(",
        "cmdline",
    ):
        assert name not in code, name


def test_rule_r_never_idle_while_a_layer_is_unknown(m, no_scan):
    m.health(hardware=False, controller=False)
    doc = m.stack().view()
    # Everything that can be seen is stopped; B and the client cannot be.
    assert doc["terminals"]["A"]["state"] == "stopped"
    assert doc["terminals"]["C"]["state"] == "stopped"
    assert doc["terminals"]["B"]["state"] == "unknown"
    assert doc["terminals"]["client"]["code"] == "client.not_connected"
    # C and the policy server are stopped and a client not connected counts as
    # stopped. The fresh health file without a unit of ours is a monitor running
    # elsewhere, which rule R counts as a running layer (stack_verdict); B stays
    # unknown. What this test is for: with a layer unknown the verdict is never
    # idle (it was "unknown" with an empty active list before M elsewhere counted).
    assert doc["verdict"] == {"verdict": "busy", "active": ["M"], "unknown": ["B"]}
    assert doc["verdict"]["verdict"] != "idle"
    layers = {c["code"]: c for c in doc["gates"]["C"]["checks"]}
    assert layers["gate.layers_known"]["ok"] is False
    assert layers["gate.layers_known"]["params"] == {"unknown": "B"}
    assert layers["gate.b_known"]["ok"] is False
    assert {g: doc["gates"][g]["verdict"] for g in "ABC"} == dict.fromkeys(
        "ABC", "would_refuse"
    )


def test_a_is_seen_from_the_arm_sockets_or_fresh_health_only(m, no_scan):
    m.sockets = {"state": "inactive", "detail": ""}
    m.health(hardware=True)
    assert m.stack().view()["terminals"]["A"]["state"] == "running"
    m.health(age=60.0, hardware=True)  # stale health says nothing about now
    assert m.stack().view()["terminals"]["A"]["state"] == "stopped"
    m.sockets = {"state": "active", "detail": ""}
    assert m.stack().view()["terminals"]["A"]["state"] == "running"


def test_c_and_the_policy_server_are_listen_rows_and_the_client_a_connection(m):
    m.socks = [_sock(5001, net.LISTEN), _sock(8000, net.LISTEN)]
    t = m.stack().view()["terminals"]
    assert t["C"]["state"] == "running" and t["policy"]["state"] == "running"
    assert t["client"]["state"] == "unknown"
    m.socks.append(_sock(8000, net.ESTABLISHED, remote_port=51000))
    assert m.stack().view()["terminals"]["client"]["state"] == "running"


def _known_terms(client_code="client.not_connected", c="stopped", policy="stopped"):
    t = {
        k: {"state": "stopped", "code": f"x.{k}", "evidence": []} for k in stack.LAYERS
    }
    t["C"]["state"] = c
    t["policy"]["state"] = policy
    t["client"] = {"state": "unknown", "code": client_code, "evidence": []}
    return t


def test_a_client_not_yet_connected_does_not_lock_the_start_gates():
    """M, A, B and C known, the client not connected: layers_known passes
    (no deadlock: the client can only connect after C and the policy server)."""
    terms = _known_terms()
    assert stack.unknown_layers(terms) == []
    prof = {"profile": "real", "code": "profile.product"}
    out = stack.gates(
        prof,
        {"state": "free"},
        {"state": "present", "ctime_age_s": 100.0},
        {"state": "inactive"},
        {"state": "ok"},
        {"state": "ok", "rtprio": 99, "memlock": "infinity"},
        {"state": "no"},
        {"A": False, "B": False, "C": False},
        terms,
    )
    for gate in ("A", "B", "C"):
        checks = {c["code"]: c for c in out[gate]["checks"]}
        assert checks["gate.layers_known"]["ok"] is True, gate
    assert stack.stack_verdict(terms)["verdict"] == "idle"


@pytest.mark.parametrize("c, policy", [("running", "stopped"), ("stopped", "running")])
def test_a_client_not_connected_is_not_idle_while_c_or_the_policy_server_runs(
    c, policy
):
    verdict = stack.stack_verdict(_known_terms(c=c, policy=policy))
    assert verdict["verdict"] == "busy"
    assert "client" in verdict["unknown"]
    # With C or the policy server unknown, the client stays unknown too.
    terms = _known_terms()
    terms["policy"]["state"] = "unknown"
    assert stack.stack_verdict(terms)["verdict"] == "unknown"
    assert "client" in stack.stack_verdict(terms)["unknown"]


def test_cs_gate_asks_the_person_about_the_client(m):
    """The client is invisible until it connects: C's gate has the person
    confirm there is none, in words that say so."""
    checks = {c["code"]: c for c in m.stack().view()["gates"]["C"]["checks"]}
    assert checks["gate.person_no_client"]["level"] == "person"
    root = Path(stack.__file__).resolve().parents[2] / "src" / "i18n"
    en = json.loads((root / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((root / "zh.json").read_text(encoding="utf-8"))
    key = "marvel0.stack.check.gate.person_no_client"
    assert "terminal E" in en[key] and "before it connects" in en[key]
    assert "终端 E" in zh[key] and "连上之前页面看不到" in zh[key]


def test_a_client_not_connected_is_never_called_stopped(m):
    terminal = m.stack().view()["terminals"]["client"]
    assert terminal["state"] == "unknown"
    assert terminal["code"] == "client.not_connected"
    root = Path(stack.__file__).resolve().parents[2] / "src" / "i18n"
    for name, words in (
        ("en.json", "Cannot be seen until it connects"),
        ("zh.json", "连上之前看不到"),
    ):
        text = json.loads((root / name).read_text(encoding="utf-8"))[
            "marvel0.stack.code.client.not_connected"
        ]
        assert words in text
        assert "stopped" not in text.lower() and "已停" not in text


def test_the_main_checkout_check_compares_the_checkout_not_the_workspace(
    tmp_path, monkeypatch
):
    """``levi.paths.ROOT`` is the workspace (``<checkout>/.state``). Comparing it
    with the main checkout made every product instance read ``fake`` and refuse
    every write, which only the first real deploy showed (21 Oct): every other
    test here injects ``in_main_checkout``."""
    from levi import paths
    from levi.live import locate
    from levi.marvel0 import site

    checkout = tmp_path / "LEVI"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(paths, "ROOT", checkout / ".state")
    monkeypatch.setattr(locate, "checkout_root", lambda: checkout)
    monkeypatch.setattr(site, "main_checkout", lambda checkouts=None: checkout)
    assert stack._in_main_checkout() is True
    monkeypatch.setattr(locate, "checkout_root", lambda: tmp_path / "worktree")
    assert stack._in_main_checkout() is False
