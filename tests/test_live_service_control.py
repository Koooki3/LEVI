"""Starting and stopping the live service from the product LEVI's page
(``levi/live/service_control.py``, ``/api/levi/live/service*``).

Fakes only: a fake ``systemctl`` (a script that records its arguments and
answers from a state file), a synthetic ``/proc`` (socket, lock, cgroup and
process tables), a fake ``nvidia-smi`` answer and fake evaluation-session
files. No live service, no systemd unit, no GPU, no real port is touched; the
only real processes are this test's own and the ``sleep`` children it starts
as stand-in supervisors (never the test process itself: a classification gone
wrong must not let a stop signal pytest)."""

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from levi.live import api, auto, gpumgr, jsonio, service_control

UI = {"x-levi-ui-token": "test-ui-token"}
AGENT = {"Authorization": "Bearer test-scoped-token"}
OTHER_BEARER = {"Authorization": "Bearer hf_not_a_levi_credential", **UI}
UNIT = "levi-live.service"
LIVE_ARGV = "/x/levi live start --auto-approve --prewarm"
SERVICE_CMDLINE = ("/x/python", "/x/levi", "live", "start", "--prewarm")
FAKE = r"""#!{python}
import json, os, sys, time
from pathlib import Path
here = Path(os.environ["FAKE_SYSTEMCTL_DIR"])
args = sys.argv[1:]
with open(here / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\n")
state = json.loads((here / "state.json").read_text())
verb = args[1] if len(args) > 1 else ""
delay = (state.get("delay") or {{}}).get(verb, 0)
if delay:
    time.sleep(delay)
fail = (state.get("fail") or {{}}).get(verb)
if fail:
    sys.stderr.write(fail["stderr"])
    sys.exit(fail.get("rc", 1))
def save():
    (here / "state.json").write_text(json.dumps(state))
if verb == "show":
    argv = state.get("argv", "")
    print("LoadState=" + state.get("load", "loaded"))
    print("ActiveState=" + state.get("active", "inactive"))
    print("SubState=" + state.get("sub", "dead"))
    print("MainPID=" + str(state.get("main_pid", 0)))
    print("ExecMainStatus=" + str(state.get("exec_status", 0)))
    print("NRestarts=" + str(state.get("restarts", 0)))
    print("ExecStart={{ path=/x/levi ; argv[]=" + argv + " ; ignore_errors=no ; start_time=[n/a] }}")
elif verb == "start":
    state["active"] = state.get("after_start", "active")
    state["sub"] = state.get("after_start_sub", "running")
    state["restarts"] = state.get("restarts", 0) + state.get("after_start_restarts", 0)
    record = state.get("on_start_pidfile")
    if record:
        Path(record["path"]).write_text(json.dumps(record["value"]))
        state["main_pid"] = record["value"]["pid"]
    save()
elif verb == "stop":
    state["active"] = "inactive"
    state["main_pid"] = 0
    gone = state.get("on_stop_remove")
    if gone and os.path.exists(gone):
        os.unlink(gone)
    save()
elif verb == "reset-failed":
    state["active"] = "inactive"
    save()
"""


class Systemctl:
    """A fake ``systemctl``: its calls, and the unit's state it answers."""

    def __init__(self, root: Path):
        self.dir = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "systemctl"
        self.path.write_text(FAKE.format(python=sys.executable))
        self.path.chmod(0o755)
        self.set(load="loaded", active="inactive", argv=LIVE_ARGV)

    def set(self, **state):
        current = jsonio.read(self.dir / "state.json") or {}
        current.update(state)
        (self.dir / "state.json").write_text(json.dumps(current))

    def state(self):
        return json.loads((self.dir / "state.json").read_text())

    def calls(self):
        try:
            lines = (self.dir / "calls.jsonl").read_text().splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines]

    def verbs(self):
        return [c[1] for c in self.calls()]


class Proc:
    """A synthetic ``/proc``: listening sockets, socket descriptors, process
    parents and names, cgroups and the lock table."""

    def __init__(self, root: Path):
        self.root = root
        (root / "net").mkdir(parents=True)
        self.tcp = []
        self.locks = []
        self._write()

    def _write(self):
        head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        rows = [
            f"   {n}: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 {inode} 1 0000\n"
            for n, (port, inode) in enumerate(self.tcp)
        ]
        (self.root / "net/tcp").write_text(head + "".join(rows))
        (self.root / "net/tcp6").write_text(head)
        (self.root / "locks").write_text("".join(self.locks))

    def process(
        self,
        pid,
        *,
        ppid=1,
        name="proc",
        cgroup="/user.slice/session-2.scope",
        cmdline=None,
    ):
        folder = self.root / str(pid)
        (folder / "fd").mkdir(parents=True, exist_ok=True)
        (folder / "stat").write_text(f"{pid} ({name}) S {ppid} {pid} {pid} 0 -1\n")
        (folder / "comm").write_text(name + "\n")
        (folder / "cgroup").write_text(f"0::{cgroup}\n")
        (folder / "cmdline").write_text("".join(a + "\0" for a in cmdline or (name,)))
        return folder

    def listen(self, port, inode, pid=None):
        """A socket listening on ``port``; ``pid`` holds it (None: a process
        whose descriptors this user cannot read)."""
        self.tcp.append((port, inode))
        if pid is not None:
            fd = self.root / str(pid) / "fd" / str(10 + len(self.tcp))
            os.symlink(f"socket:[{inode}]", fd)
        self._write()

    def lock(self, path, pid, waiter=False):
        st = os.stat(path)
        where = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
        arrow = "-> " if waiter else ""
        self.locks.append(f"1: {arrow}FLOCK  ADVISORY  WRITE {pid} {where} 0 EOF\n")
        self._write()


def unit_cgroup(unit=UNIT):
    return f"/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}"


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        monkeypatch.setenv("LEVI_LIVE_HOME", str(self.home))
        self.ws = tmp_path / "live-ws"
        (self.ws / "live").mkdir(parents=True)
        (self.ws / "live" / auto.MARKER).write_text("{}")
        self.roots = tmp_path / "rollouts"
        (self.roots / ".eval_sessions").mkdir(parents=True)
        self.gpu_lock = tmp_path / "gpu.lock"
        self.gpu_lock.write_text("")
        (self.ws / "live.toml").write_text(
            f'[watch]\nroots = ["{self.roots}"]\n[gpu]\nlock_file = "{self.gpu_lock}"\n'
        )
        self.product = tmp_path / "product"
        self.product.mkdir()
        monkeypatch.setattr(api, "_own", lambda: self.product)
        monkeypatch.setattr(api, "_workspace", lambda: self.ws)
        self.systemctl = Systemctl(tmp_path / "fake-systemctl")
        monkeypatch.setenv("FAKE_SYSTEMCTL_DIR", str(self.systemctl.dir))
        monkeypatch.setattr(service_control, "SYSTEMCTL", str(self.systemctl.path))
        self.proc = Proc(tmp_path / "proc")
        monkeypatch.setattr(service_control, "PROC", self.proc.root)
        self.gpu = []
        monkeypatch.setattr(service_control, "gpu_processes", lambda: self.gpu)
        monkeypatch.setattr(service_control, "sleep", lambda s: time.sleep(0.01))
        monkeypatch.setattr(service_control, "SETTLE_S", 0.3)
        monkeypatch.setattr(service_control, "OPERATIONS", service_control.Operations())
        # Every look is fresh in these tests (the cache has its own test).
        monkeypatch.setattr(service_control, "PROBE_CACHE_S", 0.0, raising=False)
        if hasattr(service_control, "Probe"):
            monkeypatch.setattr(service_control, "GPU_PROBE", service_control.Probe())
            monkeypatch.setattr(service_control, "PORT_PROBE", service_control.Probe())
        self.children = []

    def child(self):
        """A real process standing in for a supervisor: its identity (start
        time, boot) is real, everything else about it is synthetic."""
        child = subprocess.Popen(["sleep", "60"])
        self.children.append(child)
        return child.pid

    def close(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()

    def record(self, pid):
        return {"pid": pid, "identity": gpumgr.identity(pid)}

    def running(self, pid=None, *, by_unit=True, cgroup=None, cmdline=SERVICE_CMDLINE):
        """The supervisor's pid record in the live home: a ``sleep`` child
        by default, whose identity is real."""
        pid = pid or self.child()
        jsonio.write(
            self.home / "live.pid",
            {"pid": pid, "identity": gpumgr.identity(pid), "started_at": time.time()},
        )
        self.proc.process(
            pid,
            name="levi",
            cgroup=cgroup or "/user.slice/session-2.scope",
            cmdline=cmdline,
        )
        if by_unit:
            self.systemctl.set(active="active", sub="running", main_pid=pid)
        return pid

    def session(self, state="running"):
        jsonio.write(
            self.roots / ".eval_sessions" / "grp__task.json",
            {"state": state, "updated_at": time.time(), "pid": os.getpid()},
        )

    def audit(self):
        path = self.ws / "live" / service_control.AUDIT
        try:
            return [json.loads(x) for x in path.read_text().splitlines()]
        except OSError:
            return []

    def target(self):
        return api._service_target()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("LEVI_LIVE_UNIT", raising=False)
    monkeypatch.setenv("LEVI_LIVE_SERVICE_CONTROL", "1")
    w = World(tmp_path, monkeypatch)
    yield w
    try:
        assert service_control.OPERATIONS.wait_idle(20)
    finally:
        w.close()


@pytest.fixture
def http(client, world, monkeypatch):
    monkeypatch.setenv("LEVI_UI_TOKEN", "test-ui-token")
    monkeypatch.setenv("LEVI_AGENT_TOKEN", "test-scoped-token")
    return client


def start_body(**extra):
    return {"request_id": "req-start-0001", "confirm": "start-live", **extra}


def stop_body(**extra):
    return {"request_id": "req-stop-0001", "confirm": "stop-live", **extra}


def wait_op(http, op_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = http.get(f"/api/levi/live/service/operations/{op_id}", headers=UI).json()
        if row["state"] != "running":
            return row
        time.sleep(0.02)
    raise AssertionError("the operation did not end")


# --- discovery -------------------------------------------------------------------------


def test_not_running_unit_installed_ports_free(world):
    seen = service_control.status(world.target())
    assert seen["holder"] == {
        "pid": None,
        "alive": False,
        "started_by": None,
        "cgroup_unit": None,
        "name": None,
        "started_at": None,
    }
    assert seen["unit"]["installed"] and seen["unit"]["state"] == "inactive"
    assert {r["port"]: r["state"] for r in seen["ports"]} == {
        7881: "free",
        7882: "free",
        8100: "free",
        7880: "free",
    }
    assert seen["gpu"]["lock"]["state"] == "free"
    assert seen["gpu"]["vllm_state"] == "stopped"
    assert seen["can_start"] and not seen["can_stop"]
    assert seen["refusals"] == [] and seen["needs_confirmation"]["start"] == []
    # Only `systemctl --user show` was asked; the unit's path is not shown.
    assert world.systemctl.verbs() == ["show"]
    assert "workspace" not in seen["unit"] and "/x/levi" not in json.dumps(seen)


def test_a_service_the_unit_runs_is_found_by_its_main_pid(world):
    pid = world.running(by_unit=True)
    seen = service_control.status(world.target())
    assert seen["holder"]["started_by"] == "unit" and seen["holder"]["pid"] == pid
    assert "already_running" in seen["refusals"] and seen["can_stop"]


def test_a_service_in_the_units_cgroup_counts_as_the_units(world):
    # MainPID is another process (a wrapper), the holder lies in the cgroup.
    world.running(by_unit=False, cgroup=unit_cgroup())
    world.systemctl.set(active="active", main_pid=999999)
    seen = service_control.status(world.target())
    assert seen["holder"]["started_by"] == "unit"
    assert seen["holder"]["cgroup_unit"] == UNIT


def test_a_service_started_from_a_terminal_is_told_apart(world):
    pid = world.running(by_unit=False)
    seen = service_control.status(world.target())
    assert seen["holder"]["started_by"] == "terminal" and seen["holder"]["pid"] == pid
    assert "terminal_instance" in seen["refusals"]
    assert seen["can_stop"]


def test_a_foreign_listener_is_named_and_refuses_the_start(world, http):
    world.proc.process(4242, name="python3")
    world.proc.listen(7881, 777, pid=4242)
    # Another user's process: its descriptors cannot be read.
    world.proc.listen(8100, 778, pid=None)
    seen = http.get("/api/levi/live/service", headers=UI).json()
    rows = {r["port"]: r for r in seen["ports"]}
    assert rows[7881] == {
        "port": 7881,
        "role": "core",
        "state": "foreign",
        "pid": 4242,
        "name": "python3",
    }
    assert rows[8100]["state"] == "foreign" and rows[8100]["pid"] is None
    assert seen["refusals"] == ["port_foreign"] and not seen["can_start"]
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 409
    detail = answer.json()["detail"]
    assert (
        detail["code"] == "port_foreign" and "pid 4242 (python3)" in detail["message"]
    )
    assert "start" not in world.systemctl.verbs()
    assert [r["phase"] for r in world.audit()] == ["refused"]


def test_the_services_own_listeners_and_vllm_child_are_ours(world):
    pid = world.running(by_unit=True)
    world.proc.listen(7881, 900, pid=pid)
    world.proc.process(5000, ppid=pid, name="vllm")
    world.proc.listen(8100, 901, pid=5000)
    # A process in the unit's cgroup (reparented) is the service's too.
    world.proc.process(5001, ppid=1, name="worker", cgroup=unit_cgroup())
    world.proc.listen(7882, 902, pid=5001)
    seen = service_control.status(world.target())
    states = {r["port"]: r["state"] for r in seen["ports"]}
    assert states == {7881: "ours", 7882: "ours", 8100: "ours", 7880: "free"}


def test_the_gpu_lock_holder_comes_from_proc_locks(world):
    world.proc.process(6000, name="trainer")
    world.proc.lock(world.gpu_lock, 6100, waiter=True)  # a waiter: not a holder
    seen = service_control.status(world.target())
    assert seen["gpu"]["lock"]["state"] == "free"
    world.proc.lock(world.gpu_lock, 6000)
    seen = service_control.status(world.target())
    assert seen["gpu"]["lock"] == {
        "configured": True,
        "state": "other",
        "pid": 6000,
        "name": "trainer",
    }
    assert any(
        c["code"] == "gpu_lock_held" and c["level"] == "warn"
        for c in seen["start_checks"]
    )
    assert seen["can_start"]  # a warning: vLLM waits for the lock


def test_a_lock_the_live_service_holds_is_its_own(world):
    pid = world.running()
    world.proc.process(5000, ppid=pid, name="vllm")
    world.proc.lock(world.gpu_lock, 5000)
    jsonio.write(
        world.home / "status.json",
        {"pid": pid, "updated_at": time.time(), "gpu": {"vllm_state": "asleep"}},
    )
    seen = service_control.status(world.target())
    assert seen["gpu"]["lock"]["state"] == "live"
    assert seen["gpu"]["vllm_state"] == "asleep"


def test_the_unit_command_line_is_read_for_ui_and_workspace(world):
    world.systemctl.set(
        argv=f"/x/levi live start --prewarm --ui --workspace {world.tmp}/elsewhere"
    )
    seen = service_control.status(world.target())
    assert seen["unit"]["ui"] is True
    assert any(c["code"] == "unit_workspace_mismatch" for c in seen["start_checks"])
    # With --ui the viewer's port counts too.
    world.proc.process(4343, name="node")
    world.proc.listen(7880, 555, pid=4343)
    seen = service_control.status(world.target())
    assert "port_foreign" in seen["refusals"]


def test_reading_the_status_writes_nothing(world, http):
    before = sorted(p for p in world.tmp.rglob("*") if "fake-systemctl" not in str(p))
    for _ in range(3):
        assert http.get("/api/levi/live/service", headers=UI).status_code == 200
    after = sorted(p for p in world.tmp.rglob("*") if "fake-systemctl" not in str(p))
    # The product LEVI remembers the live workspace it was shown (as every
    # live GET does); nothing else appears.
    new = {str(p.relative_to(world.tmp)) for p in set(after) - set(before)}
    assert all(n.startswith("product/pool") for n in new), new


# --- who may ask -----------------------------------------------------------------------


@pytest.mark.parametrize("route", ["/service/start", "/service/stop"])
def test_only_a_person_may_start_or_stop(http, world, route):
    body = start_body() if route.endswith("start") else stop_body()
    url = "/api/levi/live" + route
    assert http.post(url, json=body).status_code == 401
    assert http.post(url, json=body, headers=AGENT).status_code == 403
    assert http.post(url, json=body, headers={**AGENT, **UI}).status_code == 403
    # A Bearer that is no LEVI credential passes the middleware; the route
    # itself still refuses it.
    assert http.post(url, json=body, headers=OTHER_BEARER).status_code == 403
    assert world.systemctl.verbs() == []
    assert world.audit() == []


def test_an_agent_cannot_even_read_the_status(http):
    assert http.get("/api/levi/live/service", headers=AGENT).status_code == 403


def test_off_by_default(http, world, monkeypatch):
    monkeypatch.delenv("LEVI_LIVE_SERVICE_CONTROL")
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 403 and answer.json()["detail"]["code"] == "disabled"
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    assert answer.status_code == 403
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert seen["enabled"] is False
    assert world.systemctl.verbs() == ["show"]


def test_the_confirmation_word_is_required(http, world):
    for body in (start_body(confirm=""), start_body(confirm="yes")):
        answer = http.post("/api/levi/live/service/start", json=body, headers=UI)
        assert answer.status_code == 400
        assert answer.json()["detail"]["code"] == "confirm_required"
    answer = http.post(
        "/api/levi/live/service/start", json=start_body(request_id="x"), headers=UI
    )
    assert answer.status_code == 400
    assert "start" not in world.systemctl.verbs()


# --- starting ------------------------------------------------------------------------------


def test_a_start_runs_systemctl_start_and_nothing_else(http, world):
    world.systemctl.set(
        on_start_pidfile={
            "path": str(world.home / "live.pid"),
            "value": world.record(world.child()),
        }
    )
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 202, answer.text
    op = answer.json()
    assert op["created"] and op["method"] == "systemctl"
    done = wait_op(http, op["operation_id"])
    assert done["state"] == "done" and done["result"] == "started"
    starts = [c for c in world.systemctl.calls() if c[1] == "start"]
    assert starts == [["--user", "start", UNIT]]
    assert all(c[0] == "--user" for c in world.systemctl.calls())
    lines = world.audit()
    assert [(r["action"], r["phase"]) for r in lines] == [
        ("start", "accepted"),
        ("start", "finished"),
    ]
    assert lines[1]["result"] == "started" and lines[1]["actor"] == "person"
    assert str(world.tmp) not in json.dumps(lines)


def test_no_child_process_of_the_core_but_systemctl(world, monkeypatch):
    """The core never becomes the service's parent: every process it starts
    for a start is one `systemctl --user` call."""
    started = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, argv, *args, **kwargs):
            started.append(list(argv))
            super().__init__(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    service_control.start(
        world.target(), request_id="req-only-systemctl", confirm="start-live"
    )
    assert service_control.OPERATIONS.wait_idle(10)
    assert started and all(
        a[0] == str(world.systemctl.path) and a[1] == "--user" for a in started
    )


def test_the_same_request_id_is_the_same_operation(http, world):
    world.systemctl.set(delay={"start": 0.5})
    first = http.post(
        "/api/levi/live/service/start", json=start_body(), headers=UI
    ).json()
    again = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert again.status_code == 202
    assert again.json()["operation_id"] == first["operation_id"]
    assert again.json()["created"] is False
    wait_op(http, first["operation_id"])
    later = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert later.json()["operation_id"] == first["operation_id"]
    assert world.systemctl.verbs().count("start") == 1
    # The same id for another action is refused.
    reused = http.post(
        "/api/levi/live/service/stop",
        json=stop_body(request_id="req-start-0001"),
        headers=UI,
    )
    assert reused.status_code in (409,)


def test_two_browsers_at_once_start_only_once(world):
    world.systemctl.set(delay={"start": 0.6})
    target = world.target()
    results, barrier = [], threading.Barrier(8)

    def click(n):
        barrier.wait()
        try:
            op, created = service_control.start(
                target, request_id=f"browser-{n:04d}", confirm="start-live"
            )
            results.append(("ok", op.id, created))
        except service_control.Refused as exc:
            results.append((exc.status, exc.extra.get("operation_id"), exc.code))

    threads = [threading.Thread(target=click, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = [r for r in results if r[0] == "ok"]
    assert len(accepted) == 1, results
    busy = [r for r in results if r[0] == 423]
    assert len(busy) == 7
    # Those that came after the reservation name the running operation.
    assert {r[1] for r in busy} <= {accepted[0][1], None}
    assert service_control.OPERATIONS.wait_idle(10)
    assert world.systemctl.verbs().count("start") == 1


def test_another_core_holding_the_operation_lock_gets_423(http, world):
    with open(world.home / service_control.OP_LOCK, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        answer = http.post(
            "/api/levi/live/service/start", json=start_body(), headers=UI
        )
    assert answer.status_code == 423
    assert answer.json()["detail"]["code"] == "busy"
    assert "start" not in world.systemctl.verbs()


def test_a_systemctl_that_hangs_times_out(http, world, monkeypatch):
    monkeypatch.setattr(service_control, "START_TIMEOUT_S", 0.5)
    world.systemctl.set(delay={"start": 5})
    op = http.post("/api/levi/live/service/start", json=start_body(), headers=UI).json()
    done = wait_op(http, op["operation_id"])
    assert done["state"] == "failed" and done["result"] == "systemd_timeout"
    # The slot is free again.
    assert service_control.OPERATIONS.current() is None
    assert world.audit()[-1]["result"] == "systemd_timeout"


def test_a_unit_that_fails_to_start_is_reported(http, world):
    world.systemctl.set(after_start="failed")
    op = http.post("/api/levi/live/service/start", json=start_body(), headers=UI).json()
    done = wait_op(http, op["operation_id"])
    assert done["state"] == "failed" and done["result"] == "start_failed"


def test_a_unit_that_is_not_installed(http, world):
    world.systemctl.set(load="not-found")
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert (
        seen["unit"]["installed"] is False and "unit_not_installed" in seen["refusals"]
    )
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 412
    assert answer.json()["detail"]["code"] == "unit_not_installed"


def test_no_systemctl_or_no_user_bus(http, world, monkeypatch):
    world.systemctl.set(
        fail={"show": {"stderr": "Failed to connect to bus: No medium found\n"}}
    )
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert seen["unit"]["problem"] == "systemd_unavailable"
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 503
    monkeypatch.setattr(
        service_control, "SYSTEMCTL", str(world.tmp / "no-such-systemctl")
    )
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert seen["unit"]["problem"] == "systemd_unavailable"
    answer = http.post(
        "/api/levi/live/service/start",
        json=start_body(request_id="req-start-0002"),
        headers=UI,
    )
    assert answer.status_code == 503


def test_a_systemctl_that_cannot_be_run(world, monkeypatch):
    world.systemctl.path.chmod(0o644)
    seen = service_control.status(world.target())
    assert seen["unit"]["problem"] == "permission_denied"
    with pytest.raises(service_control.Refused) as caught:
        service_control.start(
            world.target(), request_id="req-perm-0001", confirm="start-live"
        )
    assert caught.value.status == 503


def test_a_failed_unit_needs_reset_failed(http, world):
    world.systemctl.set(active="failed", sub="failed")
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert (
        answer.status_code == 409 and answer.json()["detail"]["code"] == "unit_failed"
    )
    answer = http.post(
        "/api/levi/live/service/start",
        json=start_body(request_id="req-start-0002", reset_failed=True),
        headers=UI,
    )
    assert answer.status_code == 202
    wait_op(http, answer.json()["operation_id"])
    assert [v for v in world.systemctl.verbs() if v != "show"] == [
        "reset-failed",
        "start",
    ]


def test_a_terminal_instance_blocks_the_start(http, world):
    world.running(by_unit=False)
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "terminal_instance"


def test_a_running_unit_is_not_started_twice(http, world):
    world.systemctl.set(active="activating")
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "already_running"


def test_others_on_the_gpu_need_a_confirmation(http, world):
    world.gpu.append({"pid": 3131, "name": "python", "memory_mib": 20000})
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 409
    detail = answer.json()["detail"]
    assert (
        detail["code"] == "gpu_shared_unconfirmed" and "pid 3131" in detail["message"]
    )
    answer = http.post(
        "/api/levi/live/service/start",
        json=start_body(request_id="req-start-0002", confirm_gpu_shared=True),
        headers=UI,
    )
    assert answer.status_code == 202


def test_an_unreadable_gpu_needs_the_confirmation_too(world, monkeypatch):
    monkeypatch.setattr(service_control, "gpu_processes", lambda: None)
    seen = service_control.status(world.target())
    assert seen["needs_confirmation"]["start"] == ["gpu_shared"]
    assert seen["gpu"]["others"] is None


def test_the_live_services_own_vllm_is_not_another_gpu_user(world):
    pid = world.running()
    world.proc.process(5000, ppid=pid, name="vllm")
    world.gpu.append({"pid": 5000, "name": "vllm", "memory_mib": 18000})
    seen = service_control.status(world.target())
    assert seen["gpu"]["others"] == []


def test_an_unwritable_home_is_refused(http, world):
    world.home.chmod(0o500)
    try:
        answer = http.post(
            "/api/levi/live/service/start", json=start_body(), headers=UI
        )
    finally:
        world.home.chmod(0o700)
    assert answer.status_code == 412
    assert answer.json()["detail"]["code"] == "not_writable"


def test_no_live_workspace_no_start(http, world, monkeypatch):
    monkeypatch.setattr(api, "_workspace", lambda: None)
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert seen["workspace_found"] is False and "no_live_workspace" in seen["refusals"]
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 412
    # The refusal is written in the live home, not the product workspace.
    assert (world.home / service_control.AUDIT).is_file()
    assert not list(world.product.rglob(service_control.AUDIT))


def test_the_live_services_own_core_does_not_start_or_stop_itself(
    http, world, monkeypatch
):
    monkeypatch.setattr(api, "_own", lambda: world.ws)
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert seen["served_by_live_service"] is True
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "served_by_live_service"


def test_the_unit_name_setting(world, monkeypatch):
    monkeypatch.setenv("LEVI_LIVE_UNIT", "levi-live-cold")
    assert service_control.unit_name() == ("levi-live-cold.service", None)
    for bad in ("-H host.service", "a b.service", "../x.service"):
        monkeypatch.setenv("LEVI_LIVE_UNIT", bad)
        assert service_control.unit_name() == (None, "unit_name_invalid")
    with pytest.raises(service_control.Refused) as caught:
        service_control.start(
            world.target(), request_id="req-name-0001", confirm="start-live"
        )
    assert caught.value.status == 412 and caught.value.code == "unit_name_invalid"
    assert world.systemctl.calls() == []


# --- stopping ------------------------------------------------------------------------------


def test_a_unit_service_is_stopped_with_systemctl(http, world):
    world.running(by_unit=True)
    world.systemctl.set(on_stop_remove=str(world.home / "live.pid"))
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    assert answer.status_code == 202 and answer.json()["method"] == "systemctl"
    done = wait_op(http, answer.json()["operation_id"])
    assert done["state"] == "done" and done["result"] == "stopped", done
    assert [c for c in world.systemctl.calls() if c[1] == "stop"] == [
        ["--user", "stop", UNIT]
    ]


def test_a_terminal_service_gets_the_signal_levi_live_stop_sends(http, world):
    child = subprocess.Popen(["sleep", "60"])
    try:
        world.running(child.pid, by_unit=False)
        reaper = threading.Thread(target=child.wait, daemon=True)
        reaper.start()
        answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
        assert answer.status_code == 202 and answer.json()["method"] == "signal"
        done = wait_op(http, answer.json()["operation_id"])
        assert done["result"] == "stopped", done
        reaper.join(5)
        assert child.returncode == -15
        assert "stop" not in world.systemctl.verbs()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_a_terminal_record_whose_process_changed_is_not_signalled(world, monkeypatch):
    # os.kill is replaced below: no child here (its cleanup needs os.kill).
    world.running(os.getpid(), by_unit=False)
    jsonio.write(
        world.home / "live.pid",
        {"pid": os.getpid(), "identity": {"start_ticks": "1", "boot": "x"}},
    )
    killed = []
    monkeypatch.setattr(service_control.os, "kill", lambda *a: killed.append(a))
    # The holder check fails already: nothing runs, nothing is signalled.
    with pytest.raises(service_control.Refused) as caught:
        service_control.stop(
            world.target(), request_id="req-stop-0009", confirm="stop-live"
        )
    assert caught.value.code == "not_running" and killed == []


def test_stopping_during_an_evaluation_needs_the_phrase(http, world):
    world.running(by_unit=True)
    world.systemctl.set(on_stop_remove=str(world.home / "live.pid"))
    world.session("running")
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "evaluation_active"
    answer = http.post(
        "/api/levi/live/service/stop",
        json=stop_body(request_id="req-stop-0002", force_phrase="yes"),
        headers=UI,
    )
    assert answer.status_code == 409
    answer = http.post(
        "/api/levi/live/service/stop",
        json=stop_body(
            request_id="req-stop-0003", force_phrase=service_control.FORCE_PHRASE
        ),
        headers=UI,
    )
    assert answer.status_code == 202
    wait_op(http, answer.json()["operation_id"])
    assert world.systemctl.verbs().count("stop") == 1


@pytest.mark.parametrize("state", ["homing", "waiting_reset"])
def test_homing_and_waiting_reset_count_as_an_evaluation(world, state):
    world.running(by_unit=True)
    world.session(state)
    seen = service_control.status(world.target())
    assert seen["evaluation"]["active"] and seen["needs_confirmation"]["stop"] == [
        "evaluation_active"
    ]


def test_nothing_to_stop(http, world):
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    assert (
        answer.status_code == 409 and answer.json()["detail"]["code"] == "not_running"
    )


def test_a_stop_that_leaves_the_service_running_says_so(http, world):
    world.running(by_unit=True)  # the fake stop does not remove the pid file
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    done = wait_op(http, answer.json()["operation_id"])
    assert done["state"] == "failed" and done["result"] == "stop_incomplete"


def test_unknown_operation(http):
    answer = http.get("/api/levi/live/service/operations/nope", headers=UI)
    assert answer.status_code == 404


# --- the audit -----------------------------------------------------------------------------


def test_the_audit_is_bounded(world, monkeypatch):
    monkeypatch.setattr(service_control, "AUDIT_MAX_BYTES", 2000)
    world.systemctl.set(load="not-found")
    target = world.target()
    for n in range(80):
        with pytest.raises(service_control.Refused):
            service_control.start(
                target, request_id=f"req-bound-{n:04d}", confirm="start-live"
            )
    path = world.ws / "live" / service_control.AUDIT
    assert path.stat().st_size <= 2000 + 600
    assert path.with_name(path.name + ".1").is_file()
    assert not path.with_name(path.name + ".2").exists()


def test_an_audit_that_cannot_be_written_blocks_nothing(world, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service_control.jsonio, "append_line", broken)
    op, _ = service_control.start(
        world.target(), request_id="req-audit-0001", confirm="start-live"
    )
    assert service_control.OPERATIONS.wait_idle(10)
    assert op.state == "done"


# --- small parts ---------------------------------------------------------------------------


def test_exec_argv_and_scrub():
    value = "{ path=/a/levi ; argv[]=/a/levi live start --ui --workspace /w ; ignore_errors=no }"
    assert service_control.exec_argv(value) == [
        "/a/levi",
        "live",
        "start",
        "--ui",
        "--workspace",
        "/w",
    ]
    assert service_control.exec_argv("") == []
    text = service_control.scrub(
        "Unit /home/u/.config/systemd/user/levi-live.service failed\n x"
    )
    assert text == "Unit levi-live.service failed x"
    assert len(service_control.scrub("y" * 1000)) == service_control.DETAIL_MAX


def test_cgroup_and_lineage(tmp_path):
    proc = Proc(tmp_path / "proc")
    proc.process(10, ppid=1, name="a", cgroup=unit_cgroup())
    proc.process(11, ppid=10, name="b")
    assert service_control.lineage(11, proc.root) == [11, 10]
    assert service_control.cgroup_unit(10, proc.root) == UNIT
    assert service_control.cgroup_unit(11, proc.root) is None
    assert service_control.cgroup_unit(12, proc.root) is None


def test_a_terminal_start_racing_the_unit_is_not_reported_as_the_units(
    world, monkeypatch
):
    """Between the preflight and systemctl a terminal start took the live
    home: the operation says so instead of claiming that pid."""
    pid = world.child()
    world.systemctl.set(
        on_start_pidfile={
            "path": str(world.home / "live.pid"),
            "value": world.record(pid),
        }
    )
    real = service_control.unit_state
    # The unit's MainPID is not the holder, and the holder is not in its cgroup.
    monkeypatch.setattr(
        service_control,
        "unit_state",
        lambda unit, **kw: {**real(unit, **kw), "main_pid": None},
    )
    world.proc.process(pid, name="levi", cmdline=SERVICE_CMDLINE)
    op, _ = service_control.start(
        world.target(), request_id="req-race-0001", confirm="start-live"
    )
    assert service_control.OPERATIONS.wait_idle(10)
    assert op.state == "failed" and op.result == "start_failed"
    assert "terminal" in op.detail


# --- review fixes (review-CL10) ----------------------------------------------------------


def test_i1_unknown_sessions_count_as_an_evaluation(http, world, monkeypatch):
    """No live workspace found (or no rollout roots): whether an evaluation
    runs cannot be told, so a stop needs the phrase, as during one."""
    world.running(by_unit=True)
    world.systemctl.set(on_stop_remove=str(world.home / "live.pid"))
    world.session("running")
    monkeypatch.setattr(api, "_workspace", lambda: None)
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert seen["evaluation"]["active"] is None
    assert seen["needs_confirmation"]["stop"] == ["evaluation_unknown"]
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "evaluation_unknown"
    assert "stop" not in world.systemctl.verbs()
    answer = http.post(
        "/api/levi/live/service/stop",
        json=stop_body(
            request_id="req-stop-0002", force_phrase=service_control.FORCE_PHRASE
        ),
        headers=UI,
    )
    assert answer.status_code == 202


def test_i1_no_rollout_roots_is_unknown_too(world):
    world.running(by_unit=True)
    (world.ws / "live.toml").write_text("[watch]\nroots = []\n")
    seen = service_control.status(world.target())
    assert seen["evaluation"]["active"] is None
    assert "evaluation_unknown" in seen["needs_confirmation"]["stop"]


def test_i2_a_unit_in_a_restart_loop_is_a_failed_start(http, world):
    world.systemctl.set(
        after_start="activating", after_start_sub="auto-restart", after_start_restarts=2
    )
    op = http.post("/api/levi/live/service/start", json=start_body(), headers=UI).json()
    done = wait_op(http, op["operation_id"])
    assert done["state"] == "failed" and done["result"] == "flapping", done
    assert "journalctl" in done["detail"]
    # And the page says so instead of "already running".
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert seen["unit"]["flapping"] is True
    assert "unit_flapping" in seen["refusals"]


def test_i2_a_restart_counted_during_the_start_is_a_failure(http, world):
    world.systemctl.set(after_start="active", after_start_restarts=1)
    op = http.post("/api/levi/live/service/start", json=start_body(), headers=UI).json()
    done = wait_op(http, op["operation_id"])
    assert done["state"] == "failed" and done["result"] == "flapping", done


def test_i3_a_levi_live_once_is_never_stopped(http, world):
    pid = world.running(
        by_unit=False,
        cmdline=("/x/python", "-m", "levi.live", "once", "--workspace", "/w"),
    )
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert seen["holder"]["started_by"] == "terminal_once"
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    assert answer.status_code == 409
    detail = answer.json()["detail"]
    assert detail["code"] == "once_instance" and f"pid {pid}" in detail["message"]
    # Not even with the phrase.
    answer = http.post(
        "/api/levi/live/service/stop",
        json=stop_body(
            request_id="req-stop-0002", force_phrase=service_control.FORCE_PHRASE
        ),
        headers=UI,
    )
    assert answer.status_code == 409
    assert gpumgr.same_process(pid, gpumgr.identity(pid))  # still running
    # A start is refused as well, naming it.
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.json()["detail"]["code"] == "once_instance"


def test_i3_a_holder_that_is_not_levi_live_start_is_not_signalled(http, world):
    pid = world.running(by_unit=False, cmdline=("/x/python", "something-else"))
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert seen["holder"]["started_by"] == "other"
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "holder_unidentified"
    assert gpumgr.same_process(pid, gpumgr.identity(pid))


@pytest.mark.parametrize(
    "name",
    [
        "levi-product",
        "levi-product.service",
        "levi-live-levi-product.service",
        "other.service",
        "levi-livex.service",
        "-H host.service",
    ],
)
def test_i4_the_unit_must_be_a_levi_live_unit(world, monkeypatch, name):
    monkeypatch.setenv("LEVI_LIVE_UNIT", name)
    assert service_control.unit_name()[1] == "unit_name_invalid"


def test_i4_good_names(monkeypatch):
    for name, want in (
        ("levi-live", "levi-live.service"),
        ("levi-live-cold", "levi-live-cold.service"),
        ("levi-live@ws2.service", "levi-live@ws2.service"),
    ):
        monkeypatch.setenv("LEVI_LIVE_UNIT", name)
        assert service_control.unit_name() == (want, None)


def test_i4_the_product_cannot_stop_itself(http, world, monkeypatch):
    world.running(by_unit=True)
    monkeypatch.setenv("LEVI_LIVE_UNIT", "levi-product")
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    assert answer.status_code == 412
    assert answer.json()["detail"]["code"] == "unit_name_invalid"
    assert "stop" not in world.systemctl.verbs()


def test_i4_the_unit_this_core_runs_in_is_refused(http, world, monkeypatch):
    world.proc.process(
        os.getpid(), name="python", cgroup=unit_cgroup("levi-live-x.service")
    )
    monkeypatch.setenv("LEVI_LIVE_UNIT", "levi-live-x")
    assert service_control.unit_name() == (None, "unit_is_self")
    answer = http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert answer.status_code == 412
    assert answer.json()["detail"]["code"] == "unit_is_self"


def test_i4_a_unit_that_does_not_run_levi_live_start_is_refused(http, world):
    world.running(by_unit=True)
    world.systemctl.set(argv="/x/levi serve")
    seen = http.get("/api/levi/live/service", headers=UI).json()
    assert "unit_not_live" in seen["refusals"]
    for url, body in (("stop", stop_body()), ("start", start_body())):
        answer = http.post(f"/api/levi/live/service/{url}", json=body, headers=UI)
        assert answer.status_code == 412, url
        assert answer.json()["detail"]["code"] == "unit_not_live"
    assert [v for v in world.systemctl.verbs() if v != "show"] == []


def test_i5_a_hanging_nvidia_smi_does_not_block_the_status(world, monkeypatch):
    release = threading.Event()
    calls = []

    def hanging():
        calls.append(1)
        release.wait(10)
        return []

    monkeypatch.setattr(service_control, "gpu_processes", hanging)
    monkeypatch.setattr(service_control, "PROBE_TIMEOUT_S", 0.3)
    try:
        began = time.monotonic()
        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(service_control.status(world.target()))
            )
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert time.monotonic() - began < 3
        assert len(results) == 5
        assert all(r["gpu"]["state"] == "unknown" for r in results)
        assert all(r["gpu"]["others"] is None for r in results)
        assert len(calls) == 1  # one nvidia-smi at a time
    finally:
        release.set()


def test_i5_a_slow_proc_scan_answers_unknown_and_refuses_the_start(world, monkeypatch):
    release = threading.Event()
    real = service_control.scan_ports

    def slow(wanted):
        release.wait(10)
        return real(wanted)

    monkeypatch.setattr(service_control, "scan_ports", slow)
    monkeypatch.setattr(service_control, "PROBE_TIMEOUT_S", 0.3)
    try:
        seen = service_control.status(world.target())
        assert {r["state"] for r in seen["ports"]} == {"unknown"}
        assert "ports_unknown" in seen["refusals"]
        with pytest.raises(service_control.Refused) as caught:
            service_control.start(
                world.target(), request_id="req-scan-0001", confirm="start-live"
            )
        assert caught.value.code == "ports_unknown" and caught.value.status == 503
    finally:
        release.set()


def test_i5_the_status_reuses_a_fresh_look_but_a_start_looks_again(world, monkeypatch):
    calls = []

    def counted():
        calls.append(1)
        return []

    monkeypatch.setattr(service_control, "gpu_processes", counted)
    monkeypatch.setattr(service_control, "PROBE_CACHE_S", 30.0)
    for _ in range(3):
        service_control.status(world.target())
    assert len(calls) == 1
    service_control.start(
        world.target(), request_id="req-fresh-0001", confirm="start-live"
    )
    assert len(calls) == 2


def test_s1_the_live_services_own_core_does_not_stop_it(http, world, monkeypatch):
    world.running(by_unit=True)
    monkeypatch.setattr(api, "_own", lambda: world.ws)
    answer = http.post("/api/levi/live/service/stop", json=stop_body(), headers=UI)
    assert answer.status_code == 409
    assert answer.json()["detail"]["code"] == "served_by_live_service"
    assert "stop" not in world.systemctl.verbs()


def test_s1_the_signal_rechecks_the_identity(world, monkeypatch):
    pid = os.getpid()  # os.kill is replaced: nothing can be signalled
    killed = []
    monkeypatch.setattr(service_control.os, "kill", lambda *a: killed.append(a))
    ok, result, _ = service_control._signal_stop(
        {"pid": pid, "identity": {"start_ticks": "1", "boot": "other"}}
    )
    assert killed == [] and ok and result == "stopped"


def test_s3_the_same_request_during_the_preflight_is_the_same_operation(
    world, monkeypatch
):
    real = service_control.discover
    entered = threading.Event()

    def slow(target, **kwargs):
        entered.set()
        time.sleep(0.4)
        return real(target, **kwargs)

    monkeypatch.setattr(service_control, "discover", slow)
    world.proc.process(4242, name="python3")
    world.proc.listen(7881, 777, pid=4242)  # the preflight will refuse
    target = world.target()
    first = {}

    def click():
        try:
            service_control.start(
                target, request_id="req-dup-0001", confirm="start-live"
            )
        except service_control.Refused as exc:
            first["code"] = exc.code

    thread = threading.Thread(target=click)
    thread.start()
    entered.wait(5)
    op, created = service_control.start(
        target, request_id="req-dup-0001", confirm="start-live"
    )
    assert created is False and op.state == "preparing"
    assert service_control.operation(op.id)["state"] == "preparing"
    thread.join(5)
    assert first["code"] == "port_foreign"
    row = service_control.operation(op.id)
    assert row["state"] == "refused" and row["result"] == "port_foreign"
    # The same request again gets the same answer.
    with pytest.raises(service_control.Refused) as caught:
        service_control.start(target, request_id="req-dup-0001", confirm="start-live")
    assert caught.value.code == "port_foreign"


def test_s4_refusals_before_the_preflight_are_audited(http, world, monkeypatch):
    http.post("/api/levi/live/service/start", json=start_body(confirm=""), headers=UI)
    http.post(
        "/api/levi/live/service/start", json=start_body(request_id="x"), headers=UI
    )
    codes = [r.get("code") for r in world.audit()]
    assert codes == ["confirm_required", "request_id_invalid"]
    monkeypatch.delenv("LEVI_LIVE_SERVICE_CONTROL")
    http.post("/api/levi/live/service/start", json=start_body(), headers=UI)
    assert len(world.audit()) == 2  # switched off: nothing is written


def test_s5_switched_off_the_routes_touch_nothing(http, world, monkeypatch):
    monkeypatch.delenv("LEVI_LIVE_SERVICE_CONTROL")
    for route, body in (("start", start_body()), ("stop", stop_body())):
        answer = http.post(f"/api/levi/live/service/{route}", json=body, headers=UI)
        assert answer.status_code == 403
    assert not (world.product / "pool").exists()
    assert world.systemctl.calls() == []
