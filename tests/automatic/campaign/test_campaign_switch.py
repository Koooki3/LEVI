"""Policy switching (campaign/switch.py, T-CP-04) with a fake systemd and a
fake ``/proc``: stop, start, passive readiness, foreign listeners, and the
conductor driving it. No socket is opened (the guard in conftest fails a
test that connects to 5000/5001/5100/7470/8000) and no real unit tool runs."""

import shutil
import socket
from pathlib import Path

import pytest
from campaign_fixtures import FakePlanner, write_job
from campaign_guard import aeri_home_fixture, guard_fixture  # noqa: F401
from campaign_world import FakeLauncher, Person, Session, World

from levi.automatic.campaign import spec
from levi.automatic.campaign.conductor import ArmRef, Conductor
from levi.automatic.campaign.switch import (
    ProcView,
    SystemdPolicyHost,
    runs_arm,
)


def exec_start_value(argv):
    """What ``systemctl show -p ExecStart --value`` prints for a started unit."""
    if not argv:
        return ""
    return (
        f"{{ path={argv[0]} ; argv[]={' '.join(argv)} ; ignore_errors=no ; "
        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
    "retrnsmt   uid  timeout inode\n"
)


class FakeProc:
    """A ``/proc`` tree: processes with a cgroup, a command line and socket
    descriptors, and the listening sockets in ``net/tcp``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        # The unit cgroups (``/sys/fs/cgroup`` in production).
        self.cgroup_root = self.root.parent / "cgroup"
        (self.root / "net").mkdir(parents=True, exist_ok=True)
        self.sockets = {}  # inode -> port
        self.cgroups = {}  # pid -> cgroup path
        self.next_pid = 1000
        self.next_inode = 50_000
        self._write_net()

    def _write_net(self):
        rows = []
        for n, (inode, port) in enumerate(sorted(self.sockets.items())):
            rows.append(
                f"   {n}: 00000000:{port:04X} 00000000:0000 0A 00000000:00000000 "
                f"00:00000000 00000000  1000        0 {inode} 1 0000 100 0 0 10 0\n"
            )
        (self.root / "net" / "tcp").write_text(HEADER + "".join(rows))
        (self.root / "net" / "tcp6").write_text(HEADER)

    def spawn(
        self,
        argv,
        cgroup="0::/user.slice/user@1000.service/app.slice/x.scope",
        port=None,
    ):
        pid = self.next_pid
        self.next_pid += 1
        folder = self.root / str(pid)
        (folder / "fd").mkdir(parents=True)
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (folder / "cgroup").write_text(cgroup + "\n")
        self.cgroups[pid] = cgroup.split(":", 2)[-1]
        self._write_procs(self.cgroups[pid])
        os_link = folder / "fd" / "0"
        os_link.symlink_to("/dev/null")
        if port is not None:
            inode = self.next_inode
            self.next_inode += 1
            (folder / "fd" / "3").symlink_to(f"socket:[{inode}]")
            self.sockets[inode] = port
            self._write_net()
        return pid

    def kill(self, pid):
        folder = self.root / str(pid)
        for link in (folder / "fd").iterdir():
            target = str(link.readlink())
            if target.startswith("socket:["):
                self.sockets.pop(int(target[8:-1]), None)
        shutil.rmtree(folder)
        group = self.cgroups.pop(pid, None)
        if group is not None:
            self._write_procs(group)
        self._write_net()

    def _procs_file(self, group):
        return self.cgroup_root / group.lstrip("/") / "cgroup.procs"

    def _write_procs(self, group):
        members = sorted(p for p, g in self.cgroups.items() if g == group)
        f = self._procs_file(group)
        if members:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("".join(f"{p}\n" for p in members))
        elif f.exists():
            shutil.rmtree(f.parent)

    def control_group(self, unit):
        group = f"/user.slice/user@1000.service/app.slice/{unit}.service"
        return group if self._procs_file(group).exists() else ""

    def unlisted_listener(self, port):
        """A socket nobody visible holds (another user's process)."""
        self.sockets[self.next_inode] = port
        self.next_inode += 1
        self._write_net()


class FakeSystemd:
    """``systemctl --user show/stop`` and ``systemd-run --user`` over the
    fake ``/proc``: a started unit is a process in the unit's cgroup that
    listens on the arm's port."""

    def __init__(self, proc: FakeProc, *, port_of=None, linger=0, fail=()):
        self.proc = proc
        self.units = {}  # unit -> pid
        self.calls = []
        self.port_of = port_of or (lambda unit: 8000)
        self.linger = linger  # polls before a stopped unit lets go
        self.fail = set(fail)
        self._lingering = {}
        self.exec_start = {}  # unit -> the argv systemd records as ExecStart

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["systemd-run", "--user"]:
            unit = next(a for a in argv if a.startswith("--unit="))[7:]
            if "start" in self.fail:
                return 1, ""
            command = argv[argv.index("--") + 1 :]
            cgroup = f"0::/user.slice/user@1000.service/app.slice/{unit}.service"
            self.exec_start[unit] = command
            self.units[unit] = self.proc.spawn(command, cgroup, self.port_of(unit))
            return 0, f"Running as unit: {unit}.service\n"
        assert argv[:2] == ["systemctl", "--user"], argv
        unit = argv[-1].removesuffix(".service")
        if argv[2] == "show" and "ControlGroup" in argv:
            return 0, self.proc.control_group(unit) + "\n"
        if argv[2] == "show" and "ExecStart" in argv:
            return 0, exec_start_value(self.exec_start.get(unit)) + "\n"
        if argv[2] == "show":
            return 0, ("active\n" if unit in self.units else "inactive\n")
        if argv[2] == "stop":
            if "stop" in self.fail:
                return 1, ""
            pid = self.units.pop(unit, None)
            if pid is not None:
                if self.linger:
                    self._lingering[pid] = self.linger
                else:
                    self.proc.kill(pid)
            return 0, ""
        raise AssertionError(argv)

    def tick(self):
        for pid in list(self._lingering):
            self._lingering[pid] -= 1
            if self._lingering[pid] <= 0:
                del self._lingering[pid]
                self.proc.kill(pid)


def recipe(arm: ArmRef) -> list:
    return [
        "uv",
        "run",
        "scripts/serve_policy.py",
        "--port",
        str(arm.port),
        "policy:checkpoint",
        "--policy.config",
        arm.config,
        "--policy.dir",
        arm.checkpoint_dir,
    ]


def arm(letter="A", config="pi05_fr3_all_state", ckpt="/ckpt/pi05_fr3_all_step49999"):
    return ArmRef("c1", letter, f"levi-policy-c1-{letter}", ckpt, config, 8000)


CFG = {
    "config": "pi05_fr3_all_state_cfg",
    "ckpt": "/ckpt/recap_cfg_r2_best_step14300_jax",
}


@pytest.fixture
def proc(tmp_path):
    return FakeProc(tmp_path / "proc")


def host_for(proc, systemd, **over):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        systemd.tick()

    return SystemdPolicyHost(
        systemd=systemd,
        proc_root=proc.root,
        cgroup_root=proc.cgroup_root,
        recipe=over.pop("recipe", recipe),
        clock=lambda: now[0],
        sleep=sleep,
        **over,
    )


def test_start_then_passive_ready_reads_proc_only(proc, campaign_guard):
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd)
    a = arm()
    assert host.passive_ready(a).status == "not_ready"
    result = host.start(a)
    assert result.executed == "yes" and not result.mismatch
    started = systemd.calls[-1]
    assert started[:4] == [
        "systemd-run",
        "--user",
        "--unit=levi-policy-c1-A",
        "--collect",
    ]
    assert host.passive_ready(a).status == "ready"
    assert campaign_guard.connects == []


def test_stop_waits_for_the_port_to_be_free(proc):
    systemd = FakeSystemd(proc, linger=3)
    host = host_for(proc, systemd)
    a, b = arm(), arm("B", **CFG)
    host.start(a)
    result = host.stop([a, b])
    assert result.executed == "yes" and result.detail_code == "stopped"
    assert ProcView(proc.root, systemd=systemd).listeners(8000) == []
    stops = [c for c in systemd.calls if c[2] == "stop"]
    assert stops == [["systemctl", "--user", "stop", "levi-policy-c1-A.service"]]


def test_stop_reports_a_port_that_never_frees(proc):
    systemd = FakeSystemd(proc, linger=10_000)
    host = host_for(proc, systemd, stop_timeout_s=5)
    a = arm()
    host.start(a)
    result = host.stop([a])
    assert result.executed == "unknown" and not result.mismatch


def test_a_foreign_listener_is_a_mismatch_for_stop_start_and_ready(proc):
    # A policy server started by hand in a terminal holds the port.
    proc.spawn(["python", "serve_policy.py", "--port", "8000"], port=8000)
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd)
    a = arm()
    assert host.stop([a]).mismatch
    assert host.start(a).mismatch
    assert host.passive_ready(a).status == "mismatch"
    assert not any(c[0] == "systemd-run" for c in systemd.calls)


def test_a_listener_whose_owner_cannot_be_read_is_never_ready(proc):
    proc.unlisted_listener(8000)
    host = host_for(proc, FakeSystemd(proc))
    assert host.passive_ready(arm()).status == "mismatch"
    assert host.stop([arm()]).mismatch


def test_the_unit_must_serve_the_arms_config_and_checkpoint(proc):
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd)
    cfg = arm("B", **CFG)
    host.start(cfg)
    assert host.passive_ready(cfg).status == "ready"
    # Same unit name, but the plain config (a prefix of the CFG one).
    plain = arm("B", config="pi05_fr3_all_state", ckpt=CFG["ckpt"])
    assert host.passive_ready(plain).status == "mismatch"
    other_ckpt = arm("B", config=CFG["config"], ckpt="/ckpt/recap_cfg_r1_step10000_jax")
    assert host.passive_ready(other_ckpt).status == "mismatch"
    # Another arm's unit on the port is a mismatch for this arm.
    assert host.passive_ready(arm("A")).status == "mismatch"


def test_without_a_recipe_nothing_is_started(proc):
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd, recipe=None)
    result = host.start(arm())
    assert (result.executed, result.detail_code) == ("no", "no_recipe")
    assert not any(c[0] == "systemd-run" for c in systemd.calls)


def test_a_failed_systemd_run_is_not_executed(proc):
    host = host_for(proc, FakeSystemd(proc, fail=("start",)))
    assert host.start(arm()).executed == "no"


def test_command_line_matching():
    a = arm(ckpt="/home/x/openpi/checkpoints/pi05_fr3_all_step49999")
    relative = [
        "serve",
        "--policy.config",
        "pi05_fr3_all_state",
        "--policy.dir",
        "checkpoints/pi05_fr3_all_step49999",
    ]
    assert runs_arm(relative, a)
    assert runs_arm(
        [
            "serve",
            "--policy.config=pi05_fr3_all_state",
            "--policy.dir=/home/x/openpi/checkpoints/pi05_fr3_all_step49999/",
        ],
        a,
    )
    assert not runs_arm(
        [
            "serve",
            "--policy.config",
            "pi05_fr3_all_state_cfg",
            "--policy.dir",
            "checkpoints/pi05_fr3_all_step49999",
        ],
        a,
    )
    assert not runs_arm(
        [
            "serve",
            "--policy.config",
            "pi05_fr3_all_state",
            "--policy.dir",
            "elsewhere/pi05_fr3_all_step4999",
        ],
        a,
    )


def test_the_socket_guard_fails_a_connect_to_a_robot_port(campaign_guard):
    for port in (5000, 5001, 5100, 7470, 8000):
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=0.1)
    assert len(campaign_guard.robot_connects()) == 5
    campaign_guard.connects.clear()  # the guard itself is under test here


# --- the conductor with the systemd host --------------------------------------------------


def campaign(tmp_path, proc, systemd, person=None, session=None):
    path = write_job(tmp_path / "job")
    found = spec.plan_campaign(path, job_root=tmp_path / "jobs", planner=FakePlanner())
    world = World(tmp_path / "world")
    conductor = Conductor.create(
        found.directory,
        host=host_for(proc, systemd),
        launcher=FakeLauncher(world),
        confirmations=person or Person(),
        session=session or Session(),
    )
    return conductor, found, world


def test_a_campaign_switches_policies_through_systemd(tmp_path, proc, campaign_guard):
    systemd = FakeSystemd(proc)
    session = Session()
    conductor, found, world = campaign(tmp_path, proc, systemd, session=session)
    conductor.run()
    assert conductor.state == "ANALYZING"
    started = [
        next(a for a in c if a.startswith("--unit="))[7:]
        for c in systemd.calls
        if c[0] == "systemd-run"
    ]
    arms = [c.arm for c in sorted(found.children, key=lambda c: c.segment)]
    expected = [a for i, a in enumerate(arms) if i == 0 or arms[i - 1] != a]
    assert started == [f"levi-policy-c1-{a}" for a in expected]
    # waiting_reset is written before each real stop and released after the
    # new policy is found ready.
    resets = [i for i, c in enumerate(session.calls) if c[0] == "waiting_reset"]
    assert len(resets) == len(expected)
    for index in resets:
        assert ("release",) in session.calls[index:]
    assert sorted(world.launches()) == sorted(c.run_id for c in found.children)
    assert campaign_guard.connects == []
    conductor.close()


def test_a_hand_started_policy_server_locks_the_campaign(tmp_path, proc):
    proc.spawn(["python", "serve_policy.py", "--port", "8000"], port=8000)
    systemd = FakeSystemd(proc)
    conductor, _, world = campaign(tmp_path, proc, systemd, person=Person(auto=False))
    conductor.run()
    assert conductor.state == "FAULT_LOCKED"
    assert conductor.replay.wait_reason == "listener_mismatch"
    assert world.launches() == []
    assert not any(c[0] == "systemd-run" for c in systemd.calls)
    conductor.close()


def test_a_unit_serving_the_wrong_config_locks_the_campaign(tmp_path, proc):
    systemd = FakeSystemd(proc)

    def wrong(arm_ref):
        command = recipe(arm_ref)
        command[command.index("--policy.config") + 1] = "pi05_fr3_all_state_typo"
        return command

    path = write_job(tmp_path / "job")
    found = spec.plan_campaign(path, job_root=tmp_path / "jobs", planner=FakePlanner())
    world = World(tmp_path / "world")
    conductor = Conductor.create(
        found.directory,
        host=host_for(proc, systemd, recipe=wrong),
        launcher=FakeLauncher(world),
        confirmations=Person(auto=False),
    )
    conductor.run()
    assert conductor.state == "FAULT_LOCKED" and world.launches() == []
    conductor.close()


def test_t1_a_hand_started_server_with_the_same_config_is_not_ready(proc):
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd)
    a = arm()
    systemd.units[a.unit] = proc.spawn(["sleep"])  # the unit is active
    proc.spawn(recipe(a), port=8000)  # same command line, outside the unit
    assert host.passive_ready(a).status == "mismatch"


def _record_root_listings(monkeypatch, root):
    seen = []
    real = Path.iterdir

    def iterdir(self):
        if Path(self) == Path(root):
            seen.append(self)
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    return seen


def test_ownership_never_lists_the_proc_root(proc, monkeypatch):
    # The root AGENTS.md execution boundary: only our own unit's cgroup.procs.
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd)
    a = arm()
    host.start(a)
    stranger = proc.spawn(["nc", "-l", "9000"], port=9000)
    seen = _record_root_listings(monkeypatch, proc.root)
    assert host.passive_ready(a).status == "ready"
    held = ProcView(proc.root, systemd=systemd, cgroup_root=proc.cgroup_root).listeners(
        8000, (a.unit,)
    )
    assert [lsn.pids for lsn in held] == [(systemd.units[a.unit],)]
    assert host.stop([a]).detail_code == "stopped"
    assert seen == []
    assert stranger in proc.cgroups  # untouched


def test_a_visible_foreign_listener_is_refused_without_naming_its_pid(proc):
    # Its fd folder is readable, but it is not in our unit: never ours.
    stranger = proc.spawn(["python", "serve_policy.py", "--port", "8000"], port=8000)
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd)
    a = arm()
    result = host.stop([a])
    assert result.mismatch and result.detail_code == "foreign_listener"
    assert str(stranger) not in (result.detail or "")
    assert host.start(a).mismatch
    ready = host.passive_ready(a)
    assert ready.status == "mismatch" and str(stranger) not in ready.detail
    assert not any(c[0] == "systemd-run" for c in systemd.calls)


def test_a_foreign_process_beside_our_unit_on_the_port_is_refused(proc):
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd)
    a = arm()
    host.start(a)
    proc.spawn(["nc", "-l", "8000"], port=8000)  # a second listener, not ours
    assert host.passive_ready(a).status == "mismatch"
    assert host.start(a).mismatch


def test_an_unreadable_unit_cgroup_owns_nothing(proc):
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd)
    a = arm()
    host.start(a)
    shutil.rmtree(proc.cgroup_root)  # ControlGroup named, cgroup.procs unreadable
    systemd.proc.control_group = lambda unit: "/gone/x.service"
    ready = host.passive_ready(a)
    assert ready.status == "mismatch"
    # Our own cgroup could not be read: say so, not "a foreign process".
    assert "owner unknown: levi-policy-c1-A cgroup unreadable" in ready.detail
    assert "not levi-policy-c1-A" not in ready.detail


def test_a_lingering_listener_with_an_unreadable_cgroup_is_owner_unknown(proc):
    systemd = FakeSystemd(proc, linger=3)
    host = host_for(proc, systemd)
    a = arm()
    host.start(a)
    real = systemd.run

    def broken(argv):
        if "ControlGroup" in argv:
            return 1, ""
        return real(argv)

    systemd.run = broken
    result = host.stop([a])
    assert result.mismatch and result.detail_code == "foreign_listener"
    assert result.detail == (
        "port 8000: owner unknown: levi-policy-c1-A cgroup unreadable"
    )


def test_a_foreign_listener_detail_says_not_ours(proc):
    proc.spawn(["nc", "-l", "8000"], port=8000)
    host = host_for(proc, FakeSystemd(proc))
    result = host.stop([arm()])
    assert result.detail == "port 8000 is held by a process that is not ours"


def test_a_recipe_with_whitespace_is_refused(proc):
    # systemd records ExecStart space-separated; passive readiness splits it
    # on whitespace, so such an argument could never be recognised as ours.
    systemd = FakeSystemd(proc)
    host = host_for(proc, systemd, recipe=lambda a: [*recipe(a), "--note=a b"])
    result = host.start(arm())
    assert (result.executed, result.detail_code) == ("no", "recipe_unsafe")
    assert "whitespace" in result.detail
    assert not any(c[0] == "systemd-run" for c in systemd.calls)


def test_the_start_command_never_delegates_the_cgroup(proc):
    # Ownership reads only the unit's top-level cgroup.procs: a delegated unit
    # could move its processes into sub-cgroups we would not see.
    systemd = FakeSystemd(proc)
    host_for(proc, systemd).start(arm())
    (started,) = [c for c in systemd.calls if c[0] == "systemd-run"]
    assert not any("Delegate" in part for part in started)


@pytest.mark.parametrize(
    "group", ["user.slice/x.service", "/user.slice/../x.service", "/a/../../etc"]
)
def test_an_odd_control_group_is_unreadable(proc, group):
    class Show:
        def run(self, argv):
            return 0, group + "\n"

    view = ProcView(proc.root, systemd=Show(), cgroup_root=proc.cgroup_root)
    with pytest.raises(OSError):
        view.unit_pids("levi-policy-c1-A")
