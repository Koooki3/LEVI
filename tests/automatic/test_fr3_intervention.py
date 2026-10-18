"""FR3 implementation of the seven event-pause hooks (AERI slice D).

Everything here is in-process: a fake /proc tree, the bridge Robot over a fake
env, the real RuntimeOwnership kernel and the fake AERI rig. No socket, port,
service, GPU or robot. The real entry still refuses event_pause_judge.

The direction under test is the failure direction: any hook that raises, runs
out of time or cannot prove its fact must leave the orchestrator latched
(FAULT_LOCKED, no motion token, no further step), never moving.
"""

import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from aeri_harness import config, results, rig
from aeri_harness import fake as fk
from test_fr3_home_gate import EP, HOME, PLAN, RUN, Rig

from levi.automatic import fr3_bridge as B
from levi.automatic import fr3_run as R
from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.automatic.adapters.physical_hold import RobotAdmissionWitness
from levi.automatic.fr3_intervention import (
    HOOK_NAMES,
    Fr3InterventionHooks,
    ProcReader,
    SlotBoundPolicy,
    SoleClientGuard,
    hooks_report,
)
from levi.automatic.intervention import (
    InferenceCycle,
    InterventionHooks,
    InterventionStopped,
    InterventionUnproven,
    PhysicalHoldProof,
    hold_contract_sha256,
)
from levi.automatic.orchestrator import Orchestrator
from levi.automatic.reset_manager import SettleConfig
from levi.automatic.termination import TerminationConfig
from levi.domain import aeri
from levi.live import runtime_ownership as rt
from levi.live import workload_broker as wb

HOLD = SettleConfig(1_000_000, 2_000_000, 100_000_000, 0.001, 0.01, 0.01)
PORT, UNIT, AERI_PID, SERVER_PID = 18765, "levi-aeri-policy-forward", 4242, 300
EVENT_STEP = 8  # the fixture's forward event: 8 actions run before the pause

pytestmark = pytest.mark.skipif(
    not hasattr(aeri, "ResidentGroupSpec"), reason="needs the resident-group kernel"
)


# --- a fake /proc ----------------------------------------------------------------------


def hexaddr(ip, port):
    a, b, c, d = (int(x) for x in ip.split("."))
    return f"{d:02X}{c:02X}{b:02X}{a:02X}:{port:04X}"


class Proc:
    """Writes /proc/net/tcp and /proc/<pid>/{cgroup,fd} the way the reader parses
    them. Defaults: one listener on 127.0.0.1:PORT owned by the unit's server and
    one connection from the AERI process."""

    def __init__(self, root: Path):
        self.root = root
        self.sockets = []  # (local, remote, state, inode)
        self.sockets6 = []  # the same, written to /proc/net/tcp6
        self.procs = {}  # pid -> (cgroup, [inodes])
        self.root.joinpath("net").mkdir(parents=True, exist_ok=True)

    def sock(self, local, remote, state, inode):
        self.sockets.append((local, remote, state, inode))

    def sock6(self, local, remote, state, inode):
        self.sockets6.append((local, remote, state, inode))

    def proc(self, pid, cgroup, inodes=()):
        self.procs[pid] = (cgroup, list(inodes))

    def write(self):
        for name, sockets in (("tcp", self.sockets), ("tcp6", self.sockets6)):
            lines = [
                "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
            ]
            for n, (local, remote, state, inode, *tx) in enumerate(sockets):
                queues = f"{tx[0] if tx else 0:08X}:00000000"
                lines.append(
                    f"{n:4d}: {local} {remote} {state} {queues} 00:00000000 "
                    f"00000000  1000        0 {inode} 1 0000000000000000 100 0 0 10 0"
                )
            (self.root / "net" / name).write_text("\n".join(lines) + "\n")
        unit_pids = [p for p, (cg, _) in self.procs.items() if f"/{UNIT}.service" in cg]
        (self.root.parent / "unit.procs").write_text(
            "".join(f"{p}\n" for p in unit_pids)
        )
        for pid, (cgroup, inodes) in self.procs.items():
            folder = self.root / str(pid)
            (folder / "fd").mkdir(parents=True, exist_ok=True)
            (folder / "cgroup").write_text(cgroup)
            for n, inode in enumerate(inodes):
                link = folder / "fd" / str(n + 3)
                if not link.is_symlink():
                    link.symlink_to(f"socket:[{inode}]")


def hexaddr6_mapped(ip, port):
    """An IPv4-mapped address (::ffff:a.b.c.d) as /proc/net/tcp6 prints it."""
    return "0000000000000000FFFF0000" + hexaddr(ip, port)


def unit_cgroup(unit=UNIT):
    return (
        f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}.service\n"
    )


def sole_proc(tmp_path, **over):
    """The accepted layout: loopback listener of the unit, one client = AERI."""
    p = Proc(tmp_path / "proc")
    port = over.get("port", PORT)
    p.sock(hexaddr("127.0.0.1", port), hexaddr("0.0.0.0", 0), "0A", 1001)
    p.sock(hexaddr("127.0.0.1", port), hexaddr("127.0.0.1", 40000), "01", 1002)
    p.sock(hexaddr("127.0.0.1", 40000), hexaddr("127.0.0.1", port), "01", 1003)
    p.proc(SERVER_PID, unit_cgroup(), [1001, 1002])
    p.proc(AERI_PID, "0::/user.slice/session-1.scope\n", [1003])
    return p


def guard(tmp_path, clock=None, **over):
    clock = clock or fk.FakeClock()
    kw = {
        "proc_root": tmp_path / "proc",
        "port": PORT,
        "server_unit": UNIT,
        "client_pids": (AERI_PID,),
        "clock": clock,
        "unit_procs_file": tmp_path / "unit.procs",
    }
    kw.update(over)
    return SoleClientGuard(**kw)


# --- G1: the sole-client proof ----------------------------------------------------------


def test_sole_client_accepted_layout_proves_listener_and_client(tmp_path):
    sole_proc(tmp_path).write()
    proof = guard(tmp_path).prove()
    assert proof.port == PORT and proof.server_pids == (SERVER_PID,)
    assert proof.client_pids == (AERI_PID,) and proof.connections == 1


def test_sole_client_idle_server_with_no_connection_is_still_private(tmp_path):
    p = Proc(tmp_path / "proc")
    p.sock(hexaddr("127.0.0.1", PORT), hexaddr("0.0.0.0", 0), "0A", 1001)
    p.proc(SERVER_PID, unit_cgroup(), [1001])
    p.proc(AERI_PID, "0::/user.slice/session-1.scope\n", [])
    p.write()
    assert guard(tmp_path).prove().connections == 0


@pytest.mark.parametrize(
    "case",
    [
        "foreign_client",
        "unknown_client_owner",
        "public_bind",
        "listener_outside_unit",
        "no_listener",
        "second_listener",
        "peer_missing",
        "remote_peer",
        "proc_unreadable",
        "known_and_foreign_client",
    ],
)
def test_sole_client_proof_fails_closed(tmp_path, case):
    p = sole_proc(tmp_path)
    if case == "foreign_client":
        p.proc(555, "0::/user.slice/session-2.scope\n", [1003])
        p.procs[AERI_PID] = (p.procs[AERI_PID][0], [])
    elif case == "known_and_foreign_client":
        p.sock(hexaddr("127.0.0.1", 40001), hexaddr("127.0.0.1", PORT), "01", 1004)
        p.sock(hexaddr("127.0.0.1", PORT), hexaddr("127.0.0.1", 40001), "01", 1005)
        p.proc(555, "0::/user.slice/session-2.scope\n", [1004])
        p.procs[SERVER_PID] = (p.procs[SERVER_PID][0], [1001, 1002, 1005])
    elif case == "unknown_client_owner":
        p.procs[AERI_PID] = (p.procs[AERI_PID][0], [])  # nobody holds inode 1003
    elif case == "public_bind":
        p.sockets[0] = (hexaddr("0.0.0.0", PORT), hexaddr("0.0.0.0", 0), "0A", 1001)
    elif case == "listener_outside_unit":
        p.procs[SERVER_PID] = ("0::/user.slice/session-9.scope\n", [1001, 1002])
    elif case == "no_listener":
        del p.sockets[0]
    elif case == "second_listener":
        p.sock(hexaddr("127.0.0.1", PORT), hexaddr("0.0.0.0", 0), "0A", 2001)
        p.proc(777, unit_cgroup("someone-else"), [2001])
    elif case == "peer_missing":
        del p.sockets[2]  # accepted socket without its client end
    elif case == "remote_peer":
        p.sockets[1] = (
            hexaddr("127.0.0.1", PORT),
            hexaddr("10.1.2.3", 40000),
            "01",
            1002,
        )
    p.write()
    if case == "proc_unreadable":
        os.remove(tmp_path / "proc" / "net" / "tcp")
        os.remove(tmp_path / "proc" / "net" / "tcp6")
    with pytest.raises(InterventionUnproven):
        guard(tmp_path).prove()


@pytest.mark.parametrize("port", [8000, 5000, 5001, 5100, 7470, 8100, 80, 70000, True])
def test_sole_client_refuses_shared_or_invalid_ports_at_construction(tmp_path, port):
    sole_proc(tmp_path).write()
    with pytest.raises(InterventionUnproven):
        guard(tmp_path, port=port)


def test_sole_client_needs_an_explicit_unit_and_client(tmp_path):
    sole_proc(tmp_path).write()
    for bad in ({"server_unit": ""}, {"client_pids": ()}, {"client_pids": (0,)}):
        with pytest.raises(InterventionUnproven):
            guard(tmp_path, **bad)


# --- G1 by counting: only this run's own processes are ever opened ----------------------

CHILD_PID, FOREIGN_PID, OTHER_PID = 4243, 555, 556


class RecordingReader(ProcReader):
    """Records every pid whose /proc/<pid>/ entries the guard opens."""

    def __init__(self, root):
        super().__init__(root)
        self.pids = []

    def socket_inodes(self, pid):
        self.pids.append(pid)
        return super().socket_inodes(pid)

    def children(self, pid):
        self.pids.append(pid)
        return super().children(pid)


def counting(
    tmp_path, *, foreign_client=False, child_client=False, unit_pids=(SERVER_PID,)
):
    """The accepted layout plus decoy processes of other people that must never
    be opened. Returns (guard, reader)."""
    p = sole_proc(tmp_path)
    p.proc(FOREIGN_PID, "0::/user.slice/session-2.scope\n", [])
    p.proc(OTHER_PID, "0::/user.slice/session-3.scope\n", [])
    if child_client:
        p.procs[AERI_PID] = (p.procs[AERI_PID][0], [])
        p.proc(CHILD_PID, "0::/user.slice/session-1.scope\n", [1003])
    if foreign_client:
        p.sock(hexaddr("127.0.0.1", 40001), hexaddr("127.0.0.1", PORT), "01", 1004)
        p.sock(hexaddr("127.0.0.1", PORT), hexaddr("127.0.0.1", 40001), "01", 1005)
        p.proc(FOREIGN_PID, "0::/user.slice/session-2.scope\n", [1004])
        p.procs[SERVER_PID] = (p.procs[SERVER_PID][0], [1001, 1002, 1005])
    p.write()
    task = tmp_path / "proc" / str(AERI_PID) / "task" / str(AERI_PID)
    task.mkdir(parents=True)
    task.joinpath("children").write_text(f"{CHILD_PID} " if child_client else "")
    procs = tmp_path / "unit.procs"
    procs.write_text("".join(f"{x}\n" for x in unit_pids))
    reader = RecordingReader(tmp_path / "proc")
    g = guard(tmp_path, reader=reader, unit_procs_file=procs)
    return g, reader


def test_counting_proof_opens_only_this_runs_own_pids(tmp_path):
    g, reader = counting(tmp_path, child_client=True)
    proof = g.prove()
    assert proof.connections == 1
    assert set(reader.pids) <= {AERI_PID, CHILD_PID, SERVER_PID}
    assert SERVER_PID in reader.pids and AERI_PID in reader.pids
    assert not {FOREIGN_PID, OTHER_PID} & set(reader.pids)


def test_counting_proof_refuses_a_foreign_connection_without_opening_its_owner(
    tmp_path,
):
    g, reader = counting(tmp_path, foreign_client=True)
    with pytest.raises(InterventionUnproven):
        g.prove()
    assert not {FOREIGN_PID, OTHER_PID} & set(reader.pids)


def test_counting_proof_ignores_connections_to_other_ports(tmp_path):
    p = sole_proc(tmp_path)
    p.sock(hexaddr("127.0.0.1", 41000), hexaddr("127.0.0.1", 9999), "01", 3001)
    p.write()
    procs = tmp_path / "unit.procs"
    procs.write_text(f"{SERVER_PID}\n")
    g = guard(tmp_path, reader=ProcReader(tmp_path / "proc"), unit_procs_file=procs)
    assert g.prove().connections == 1


def test_counting_proof_needs_the_listener_inode_inside_the_units_processes(tmp_path):
    g, reader = counting(tmp_path, unit_pids=())
    with pytest.raises(InterventionUnproven):
        g.prove()
    assert not {FOREIGN_PID, OTHER_PID} & set(reader.pids)


@pytest.mark.parametrize("what", ["tcp", "tcp6", "unit_procs"])
def test_counting_proof_fails_closed_when_a_table_cannot_be_read(tmp_path, what):
    g, _ = counting(tmp_path)
    target = {
        "tcp": tmp_path / "proc" / "net" / "tcp",
        "tcp6": tmp_path / "proc" / "net" / "tcp6",
        "unit_procs": tmp_path / "unit.procs",
    }[what]
    target.unlink()
    with pytest.raises(InterventionUnproven):
        g.prove()


def test_counting_proof_fails_closed_when_an_own_pid_cannot_be_read(tmp_path):
    g, _ = counting(tmp_path)
    import shutil

    shutil.rmtree(tmp_path / "proc" / str(AERI_PID) / "fd")
    with pytest.raises(InterventionUnproven):
        g.prove()


# --- the kernel and the hooks -----------------------------------------------------------


def kernel(clock, run_id):
    now, domain, profile = clock.now(), clock.domain, "ab" * 32
    broker = wb.WorkloadBroker(
        run_id=run_id, clock_domain=domain, profile_sha256=profile
    )
    broker.prove_idle(
        wb.IdleProof(broker.broker_id, now, domain, True, True, True), now_ns=now
    )
    members = tuple(
        rt.OwnedMember(role, wb.WorkerIdentity(100 + i, 1, "fake"), role + ":owned")
        for i, role in enumerate(("forward", "judge", "reset"))
    )
    group = rt.OwnedGroup("test-owned", wb.WorkerIdentity(99, 1, "fake"), members)
    env = {
        "schema": aeri.SCHEMAS["runtime"],
        "minor": aeri.MINOR,
        "run_id": run_id,
        "emitted_wall_ns": 0,
    }
    workload = aeri.WorkloadSpec(
        **env,
        kind="workload_spec",
        workload_id="models",
        workload_class="resident_group",
        requester="c",
        device_preference="gpu",
        memory_estimate_mib=1000,
        memory_source="declared",
        duration_p99_ms=10,
        duration_source="declared",
        preemptibility="request_boundary",
        resident_group=aeri.ResidentGroupSpec(
            group_id=group.group_id,
            members=[
                {"role": m.role, "capability_id": m.capability_id} for m in members
            ],
        ),
    )
    due = aeri.Deadline(
        due_ns=now + 500_000 * 1_000_000,
        clock_domain=domain,
        budget_ms=500_000,
        hardness="hard",
    )
    pressure = aeri.ResourcePressure(
        **env,
        kind="pressure",
        observed_ns=now,
        clock_domain=domain,
        source="fake",
        free_mib=5000,
        vllm_state="awake",
        gate_open=True,
        gate_code="open",
        policy_server_seen=False,
        age_ms=0,
    )
    assert broker.submit(workload, due, profile_sha256=profile, now_ns=now) is None
    lease = broker.admit(
        pressure,
        wb.SafeWindow(broker.broker_id, run_id, None, profile, now, due.due_ns, domain),
        now_ns=now,
    )
    observed = rt.RuntimeObservation(
        lease.lease_id,
        lease.lease_epoch,
        group.supervisor,
        now,
        domain,
        True,
        True,
        True,
        True,
        rt.GroupObservation(
            group,
            tuple(rt.MemberObservation(m, True, True, True, True) for m in members),
            "fake-driver",
            1,
            "fake-session",
            True,
            True,
            True,
        ),
    )
    owner = rt.RuntimeOwnership(broker)
    runtime = owner.bind_group(lease, group, observed, now_ns=now)
    return NS(
        broker=broker,
        owner=owner,
        runtime=runtime,
        lease=lease,
        observed=observed,
        profile=profile,
        due=due,
    )


class Parts:
    """Everything a Fr3InterventionHooks needs, each one a seam a test can break."""

    def __init__(
        self, tmp_path, *, robot, fence, policy, clock, frames=None, run=RUN, plan=PLAN
    ):
        sole_proc(tmp_path).write()
        self.clock, self.robot, self.fence, self.policy = clock, robot, fence, policy
        self.run, self.plan = run, plan
        self.k = kernel(clock, run)
        self.sole = guard(tmp_path, clock)
        self.stopped = False
        self.runtime_probe = lambda: replace(self.k.observed, observed_ns=clock.now())
        self.stop_probe = lambda request, deadline: (True, True)
        self.frames = frames or NS(prepare_evidence=lambda request, obs: True)
        self.hold_calls = []

        def hold_factory(mark):
            witness = RobotAdmissionWitness(
                robot,
                fence,
                clock,
                mark,
                run_id=run,
                plan_sha256=plan,
                validity_ns=10**9,
            )

            def observe(episode, epoch, deadline):
                self.hold_calls.append((episode, epoch))
                closed = witness(episode, epoch, deadline)
                began = clock.now()
                clock.advance(HOLD.stable_window_ns)
                return PhysicalHoldProof(
                    run,
                    episode,
                    epoch,
                    plan,
                    hold_contract_sha256(HOLD),
                    clock.domain,
                    began,
                    clock.now(),
                    closed.expires_ns,
                    True,
                    True,
                    True,
                )

            return observe

        self.hold_factory = hold_factory

    def build(self, **over):
        kw = {
            "robot": self.robot,
            "fence": self.fence,
            "policy": self.policy,
            "clock": self.clock,
            "run_id": self.run,
            "plan_sha256": self.plan,
            "authority": self.k.owner,
            "runtime": self.k.runtime,
            "broker": self.k.broker,
            "profile_sha256": self.k.profile,
            "runtime_probe": lambda: self.runtime_probe(),
            "stop_probe": lambda request, deadline: self.stop_probe(request, deadline),
            "hold_factory": lambda mark: self.hold_factory(mark),
            "frames": self.frames,
            "sole_client": self.sole,
            "budget_ns": 50_000_000,
            "probe_wait_s": 5,
            "interrupted": lambda: self.stopped,
        }
        kw.update(over)
        return Fr3InterventionHooks(**kw)


class QuietPolicy:
    def __init__(self, ok=True):
        self.ok, self.quiesced = ok, []

    def quiesce(self, *, policy_epoch):
        self.quiesced.append(policy_epoch)
        return {"ok": self.ok, "fenced_epoch": policy_epoch, "inflight_cancelled": 0}


@pytest.fixture
def unit(tmp_path):
    r = Rig()
    r.fence.revoke()  # the orchestrator's _tx revoked the policy token already
    policy = QuietPolicy()
    parts = Parts(tmp_path, robot=r.robot, fence=r.fence, policy=policy, clock=r.clock)
    parts.rig, parts.quiet = r, policy
    return parts


def test_there_are_exactly_the_seven_hooks_of_the_interface():
    assert HOOK_NAMES == (
        "stop_admission",
        "resume_admission",
        "observe_hold",
        "observe_runtime",
        "role_window",
        "stop_proof",
        "prepare_evidence",
    )
    assert tuple(InterventionHooks.__dataclass_fields__) == HOOK_NAMES


def test_all_seven_hooks_are_bound_and_the_binding_names_the_objects(unit):
    hooks = unit.build()
    bound = hooks.as_hooks()
    assert isinstance(bound, InterventionHooks)
    assert all(callable(getattr(bound, n)) for n in HOOK_NAMES)
    names = hooks.bindings()
    assert set(names) == set(HOOK_NAMES) | {"physical_hold", "signal_events"}
    assert "Robot.stop_admission" in names["stop_admission"]
    assert "reopen_admission_if" in names["resume_admission"]
    assert "reopen_admission()" not in names["resume_admission"]
    assert "RuntimeOwnership" in names["role_window"]


def test_construction_refuses_anything_missing(unit):
    for name in (
        "runtime_probe",
        "stop_probe",
        "hold_factory",
        "frames",
        "sole_client",
    ):
        with pytest.raises(InterventionUnproven):
            unit.build(**{name: None})
    for bad in (0, -1, True, 1.5):
        with pytest.raises(InterventionUnproven):
            unit.build(budget_ns=bad)
    with pytest.raises(InterventionUnproven):
        unit.build(robot=NS(admission_snapshot=lambda: None))  # no gate to drive


# --- stop_admission ---------------------------------------------------------------------


def pause(unit, hooks, epoch=2):
    return hooks.as_hooks().stop_admission(EP, epoch - 1, epoch)


def test_stop_admission_closes_the_gate_and_quiesces_the_old_epoch(unit):
    hooks = unit.build()
    assert pause(unit, hooks) is True
    snap = unit.rig.robot.admission_snapshot()
    assert snap.closed and snap.mode == "closed" and snap.generation == 1
    assert unit.quiet.quiesced == [1]


@pytest.mark.parametrize(
    "case", ["quiesce_busy", "token_alive", "gate_raises", "sole_client", "slow"]
)
def test_stop_admission_failure_raises_and_leaves_the_gate_closed(unit, tmp_path, case):
    r = unit.rig
    if case == "quiesce_busy":
        unit.quiet.ok = False
    elif case == "token_alive":
        r.token("policy_steps", transaction="tx2")
    elif case == "gate_raises":

        def boom():
            raise OSError("gate")

        r.robot.stop_admission = boom
    elif case == "sole_client":
        os.remove(tmp_path / "proc" / "net" / "tcp")
    elif case == "slow":
        original = unit.quiet.quiesce

        def slow(**kw):
            r.clock.advance(10**9)
            return original(**kw)

        unit.quiet.quiesce = slow
    hooks = unit.build()
    with pytest.raises(InterventionUnproven):
        pause(unit, hooks)
    if case != "gate_raises":
        assert r.robot.admission_snapshot().closed  # closing is the safe direction
    # A failed proof is irreversible for every hook.
    with pytest.raises(InterventionUnproven):
        hooks.as_hooks().observe_runtime()


def test_stop_admission_returns_true_only_for_a_closed_snapshot(unit):
    r = unit.rig
    open_mark = r.robot.admission_snapshot()  # an open gate is not a closure
    r.robot.stop_admission = lambda: open_mark
    with pytest.raises(InterventionUnproven):
        pause(unit, unit.build())


# --- resume_admission: a specific reopen, never the generic one -------------------------


def resume(hooks, rig, episode=EP, epoch=3, observed_ns=None):
    obs = {"observed_ns": rig.clock.now() if observed_ns is None else observed_ns}
    return hooks.as_hooks().resume_admission(episode, epoch, obs)


def no_generic_reopen(robot):
    def refuse():
        raise AssertionError("the generic reopen_admission() must not be used")

    robot.reopen_admission = refuse


def test_resume_reopens_only_the_closure_this_intervention_made(unit):
    r = unit.rig
    hooks = unit.build()
    pause(unit, hooks)
    no_generic_reopen(r.robot)
    r.clock.advance(1_000)
    assert resume(hooks, r) is True
    snap = r.robot.admission_snapshot()
    assert not snap.closed and snap.mode == "open" and snap.generation == 2


def test_resume_of_the_initial_episode_changes_nothing_on_an_open_gate(unit):
    r = unit.rig
    hooks = unit.build()
    before = r.robot.admission_snapshot().generation
    no_generic_reopen(r.robot)
    assert resume(hooks, r, epoch=1) is True
    assert r.robot.admission_snapshot().generation == before


def test_resume_refuses_a_closed_gate_this_intervention_never_closed(unit):
    r = unit.rig
    r.robot.stop_admission()  # somebody else's closure (e.g. an emergency hold)
    hooks = unit.build()
    with pytest.raises(InterventionUnproven):
        resume(hooks, r, epoch=1)
    assert r.robot.admission_snapshot().closed


@pytest.mark.parametrize(
    "case",
    [
        "reopened_and_reclosed",
        "other_episode",
        "request_open",
        "observation_older_than_closure",
        "reopen_raises",
        "two_windows_since",
    ],
)
def test_resume_with_a_mismatched_identity_is_refused_and_stays_closed(unit, case):
    r = unit.rig
    hooks = unit.build()
    pause(unit, hooks)
    r.clock.advance(1_000)
    episode, observed_ns = EP, None
    if case == "reopened_and_reclosed":
        r.robot.reopen_admission()
        r.robot.stop_admission()  # a new interval: same mode, other generation
    elif case == "other_episode":
        episode = "other.forward.0001"
    elif case == "request_open":
        obs = unit.runtime_probe()
        window = hooks.as_hooks().role_window(
            "judge", EP, 2, 1, unit.clock.now() + 10**9
        )
        unit.k.owner.begin_role_request(
            unit.k.runtime,
            "judge",
            aeri.Deadline(
                due_ns=unit.clock.now() + 10**9,
                clock_domain=unit.clock.domain,
                budget_ms=1000,
                hardness="hard",
            ),
            obs,
            window,
            episode_id=EP,
            policy_epoch=2,
            now_ns=unit.clock.now(),
        )
    elif case == "observation_older_than_closure":
        observed_ns = r.robot.admission_snapshot().closed_from_ns - 1
    elif case == "reopen_raises":

        def raises(**kw):
            raise B.AdmissionRefused("interval differs")

        r.robot.reopen_admission_if = raises
    elif case == "two_windows_since":
        for n in range(2):
            r.robot.home_under_closed_gate(token=r.token(transaction=f"h{n}"))
            r.fence.revoke()
    with pytest.raises(InterventionUnproven) as caught:
        resume(hooks, r, episode=episode, observed_ns=observed_ns)
    assert r.robot.admission_snapshot().closed
    if case == "reopened_and_reclosed":
        # generation +2 with unchanged home counters, but no home-only window:
        # not the executed=no home of decision 6, so it latches.
        assert not isinstance(caught.value, InterventionStopped)
        assert hooks.failed is True


def test_resume_after_the_closed_gate_home_adopts_the_new_interval(unit):
    r = unit.rig
    hooks = unit.build()
    pause(unit, hooks)
    r.robot.home_under_closed_gate(token=r.token())  # a new closed interval
    r.fence.revoke()
    no_generic_reopen(r.robot)
    r.clock.advance(1_000)
    assert resume(hooks, r, episode="hg.forward.0002", epoch=5) is True
    assert r.robot.admission_snapshot().mode == "open"


def test_a_policy_command_released_inside_the_interval_blocks_the_resume(unit):
    r = unit.rig
    hooks = unit.build()
    pause(unit, hooks)
    genuine = r.robot.admission_snapshot

    def snapshot():  # a command passed the gate inside the interval
        real = genuine()
        return replace(real, pose_attempts=real.pose_attempts + 1)

    r.robot.admission_snapshot = snapshot
    with pytest.raises(InterventionUnproven):
        resume(hooks, r)


# --- observe_hold -----------------------------------------------------------------------


def test_observe_hold_is_the_measured_hold_of_this_closure(unit):
    hooks = unit.build()
    pause(unit, hooks)
    proof = hooks.as_hooks().observe_hold(EP, 2, unit.clock.now() + 10**9)
    assert isinstance(proof, PhysicalHoldProof) and proof.policy_epoch == 2
    assert unit.hold_calls == [(EP, 2)]


@pytest.mark.parametrize("case", ["foreign_closure", "gate_reopened", "bad_type"])
def test_observe_hold_failure_raises(unit, case):
    r = unit.rig
    hooks = unit.build()
    if case == "foreign_closure":
        r.robot.stop_admission()  # closed by somebody else: not an interval we own
    else:
        pause(unit, hooks)
    if case == "gate_reopened":
        r.robot.reopen_admission()
    if case == "bad_type":
        unit.hold_factory = lambda mark: lambda e, p, d: True
        hooks = unit.build()
        pause(unit, hooks)
    with pytest.raises(InterventionUnproven):
        hooks.as_hooks().observe_hold(EP, 2, unit.clock.now() + 10**9)


# --- observe_runtime --------------------------------------------------------------------


def test_observe_runtime_returns_the_trusted_observation_of_this_runtime(unit):
    hooks = unit.build()
    seen = hooks.as_hooks().observe_runtime()
    assert isinstance(seen, rt.RuntimeObservation)
    assert seen.lease_id == unit.k.runtime.lease_id


@pytest.mark.parametrize(
    "case", ["probe_raises", "wrong_lease", "wrong_type", "client"]
)
def test_observe_runtime_failure_raises(unit, tmp_path, case):
    if case == "probe_raises":

        def boom():
            raise OSError("probe")

        unit.runtime_probe = boom
    elif case == "wrong_lease":
        unit.runtime_probe = lambda: replace(unit.k.observed, lease_id="other-lease")
    elif case == "wrong_type":
        unit.runtime_probe = lambda: object()
    else:  # a second client connected to the private port
        p = sole_proc(tmp_path)
        p.sock(hexaddr("127.0.0.1", 40001), hexaddr("127.0.0.1", PORT), "01", 1004)
        p.sock(hexaddr("127.0.0.1", PORT), hexaddr("127.0.0.1", 40001), "01", 1005)
        p.proc(555, "0::/user.slice/session-2.scope\n", [1004])
        p.procs[SERVER_PID] = (p.procs[SERVER_PID][0], [1001, 1002, 1005])
        p.write()
    with pytest.raises(InterventionUnproven):
        unit.build().as_hooks().observe_runtime()


# --- role_window ------------------------------------------------------------------------


def test_role_window_is_a_kernel_window_for_exactly_this_grant(unit):
    hooks = unit.build()
    pause(unit, hooks)
    deadline = unit.clock.now() + 10**9
    window = hooks.as_hooks().role_window("judge", EP, 2, 1, deadline)
    assert isinstance(window, rt.RoleWindow)
    assert (window.role, window.policy_epoch, window.authorization_epoch) == (
        "judge",
        2,
        1,
    )
    assert window.expires_ns == deadline


@pytest.mark.parametrize(
    "case",
    [
        "past_lease",
        "unknown_role",
        "judge_on_open_gate",
        "forward_on_closed_gate",
        "auth",
    ],
)
def test_role_window_failure_raises(unit, case):
    hooks = unit.build()
    role, deadline, auth = "judge", unit.clock.now() + 10**9, 1
    if case == "past_lease":
        deadline = unit.k.due.due_ns + 1
        pause(unit, hooks)
    elif case == "unknown_role":
        role = "operator"
        pause(unit, hooks)
    elif case == "judge_on_open_gate":
        pass  # no pause: a judge may only run while admission is closed
    elif case == "forward_on_closed_gate":
        role = "forward"
        pause(unit, hooks)
    elif case == "auth":
        auth = 0
        pause(unit, hooks)
    with pytest.raises(InterventionUnproven):
        hooks.as_hooks().role_window(role, EP, 2, auth, deadline)


def test_forward_window_is_granted_while_admission_is_open(unit):
    window = (
        unit.build()
        .as_hooks()
        .role_window("forward", EP, 1, 1, unit.clock.now() + 10**9)
    )
    assert window.role == "forward"


# --- stop_proof -------------------------------------------------------------------------


def granted(unit, hooks, role="judge"):
    obs = unit.runtime_probe()
    deadline = unit.clock.now() + 10**9
    window = hooks.as_hooks().role_window(role, EP, 2, 1, deadline)
    return unit.k.owner.begin_role_request(
        unit.k.runtime,
        role,
        aeri.Deadline(
            due_ns=deadline,
            clock_domain=unit.clock.domain,
            budget_ms=1000,
            hardness="hard",
        ),
        obs,
        window,
        episode_id=EP,
        policy_epoch=2,
        now_ns=unit.clock.now(),
    )


def test_stop_proof_binds_the_exact_request_to_a_trusted_probe(unit):
    hooks = unit.build()
    pause(unit, hooks)
    request = granted(unit, hooks)
    proof = hooks.as_hooks().stop_proof(request, unit.clock.now() + 10**9)
    assert isinstance(proof, rt.RequestStopProof) and proof.request is request
    assert (
        proof.backend_request_stopped is True and proof.request_workers_exited is True
    )


@pytest.mark.parametrize(
    "answer",
    [(False, True), (True, False), (None, True), (True, None), "raise", "shape"],
)
def test_stop_proof_failure_raises_and_never_returns_a_false_flag(unit, answer):
    hooks = unit.build()
    pause(unit, hooks)
    request = granted(unit, hooks)
    if answer == "raise":

        def boom(request, deadline):
            raise TimeoutError("backend")

        unit.stop_probe = boom
    elif answer == "shape":
        unit.stop_probe = lambda request, deadline: True
    else:
        unit.stop_probe = lambda request, deadline: answer
    with pytest.raises(InterventionUnproven):
        hooks.as_hooks().stop_proof(request, unit.clock.now() + 10**9)


def test_stop_proof_refuses_a_request_that_is_not_the_one_in_the_slot(unit):
    hooks = unit.build()
    pause(unit, hooks)
    request = granted(unit, hooks)
    with pytest.raises(InterventionUnproven):
        hooks.as_hooks().stop_proof(replace(request, request_epoch=99), 10**12)


# --- prepare_evidence -------------------------------------------------------------------


def test_prepare_evidence_delegates_to_the_paused_frames_binding(unit):
    seen = []
    unit.frames = NS(prepare_evidence=lambda request, obs: seen.append(request) or True)
    assert (
        unit.build().as_hooks().prepare_evidence({"id": 1}, {"observed_ns": 1}) is True
    )
    assert seen == [{"id": 1}]


@pytest.mark.parametrize("answer", [False, None, 1, "raise"])
def test_prepare_evidence_failure_raises(unit, answer):
    def frames(request, obs):
        if answer == "raise":
            raise ValueError("not bound")
        return answer

    unit.frames = NS(prepare_evidence=frames)
    with pytest.raises(InterventionUnproven):
        unit.build().as_hooks().prepare_evidence({}, {})


# --- the request slot of RuntimeOwnership -----------------------------------------------


class Recorded:
    def __init__(self):
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return NS(ticket=True)

    def acquire(self, role, *, policy_epoch):
        return {"handle_id": "h"}


def test_policy_request_needs_the_granted_unrevoked_slot_of_its_own_role(unit):
    inner = Recorded()
    bound = SlotBoundPolicy(
        inner, authority=unit.k.owner, guard=unit.sole, clock=unit.clock
    )
    request = {"episode_id": EP, "policy_epoch": 2}
    with pytest.raises(InterventionUnproven):  # no request in the slot
        bound.request(request)
    hooks = unit.build()
    token = granted(unit, hooks, role="forward")
    assert bound.request(request).ticket and inner.requests == [request]
    with pytest.raises(InterventionUnproven):  # wrong episode epoch
        bound.request({"episode_id": EP, "policy_epoch": 7})
    with pytest.raises(InterventionUnproven):  # wrong role for the episode
        bound.request({"episode_id": "adm.reset.0001", "policy_epoch": 2})
    unit.k.owner.revoke_request(token, now_ns=unit.clock.now())
    with pytest.raises(InterventionUnproven):  # a revoked slot permits nothing
        bound.request(request)
    assert inner.requests == [request]
    assert bound.acquire("forward", policy_epoch=1) == {"handle_id": "h"}


def test_policy_request_refuses_when_the_sole_client_proof_fails(unit, tmp_path):
    inner = Recorded()
    bound = SlotBoundPolicy(
        inner, authority=unit.k.owner, guard=unit.sole, clock=unit.clock
    )
    granted(unit, unit.build(), role="forward")
    os.remove(tmp_path / "proc" / "net" / "tcp")
    with pytest.raises(InterventionUnproven):
        bound.request({"episode_id": EP, "policy_epoch": 2})
    assert inner.requests == []


# --- the whole orchestrator over these hooks --------------------------------------------


class Full:
    """Orchestrator + RuntimeOwnership + the FR3 hooks over the fake AERI rig."""

    def __init__(
        self, tmp_path, goal_script=None, episodes=1, scene_script=None, **hook_kw
    ):
        cfg = config(
            episodes=episodes,
            reset_strategy="human_assisted",
            termination=TerminationConfig(
                inference_mode="event_pause_judge",
                physical_hold=HOLD,
                min_steps=5,
                settle_steps=3,
                cooldown_steps=5,
            ),
        )
        clock = fk.FakeClock()
        r = rig(tmp_path / "run", cfg=cfg, clock=clock, create=False)
        r.scene = ev.FakeSceneAssessor(scene_script or [], clock, cfg.run_id)
        r.verifier = ev.FakeGoalVerifier(
            goal_script or [], clock, cfg.run_id, default={"decision": "confirmed"}
        )
        r.events = ev.FakeEventStream(
            {
                "forward": [{"step": EVENT_STEP, "event_type": "object_settled"}],
                "reset": [{"step": 4, "event_type": "object_settled"}],
            },
            clock,
            cfg.run_id,
        )
        r.robot = fk.GateFakeRobot(
            clock,
            r.fence.check,
            snapshot_type=B.AdmissionSnapshot,
            refused_type=B.AdmissionRefused,
        )  # gate record + snapshot
        self.r, self.cfg = r, cfg
        self.sub = tmp_path / "sub"
        self.sub.mkdir()
        # The kernel and hooks speak for cfg.run_id, whatever the harness names it.
        self.parts = Parts(
            self.sub,
            robot=r.robot,
            fence=r.fence,
            policy=r.policy,
            clock=clock,
            run=cfg.run_id,
            plan=cfg.plan_sha256,
        )
        self.parts.hold_factory = self._hold_factory(r, clock, cfg)
        self.hooks = self.parts.build(**hook_kw)
        self.cycle = InferenceCycle(
            authority=self.parts.k.owner,
            runtime=self.parts.k.runtime,
            clock=clock,
            run_id=cfg.run_id,
            plan_sha256=cfg.plan_sha256,
            condition_sha256=hold_contract_sha256(HOLD),
            hooks=self.hooks.as_hooks(),
        )
        self.inner = r.policy
        r.policy = SlotBoundPolicy(
            self.inner,
            authority=self.parts.k.owner,
            guard=self.parts.sole,
            clock=clock,
        )
        r.orch = Orchestrator.create(
            r.directory, cfg, intervention=self.cycle, **r.parts()
        )

    @staticmethod
    def _hold_factory(r, clock, cfg):
        def factory(mark):
            witness = RobotAdmissionWitness(
                r.robot,
                r.fence,
                clock,
                mark,
                run_id=cfg.run_id,
                plan_sha256=cfg.plan_sha256,
                validity_ns=10**9,
            )

            def observe(episode, epoch, deadline):
                closed = witness(episode, epoch, deadline)
                began = clock.now()
                clock.advance(HOLD.stable_window_ns)
                return PhysicalHoldProof(
                    cfg.run_id,
                    episode,
                    epoch,
                    cfg.plan_sha256,
                    hold_contract_sha256(HOLD),
                    clock.domain,
                    began,
                    clock.now(),
                    closed.expires_ns,
                    True,
                    True,
                    True,
                )

            return observe

        return factory


@pytest.fixture
def full(tmp_path):
    f = Full(tmp_path)
    yield f
    f.r.orch.close()


def test_complete_run_over_the_fr3_hooks_pauses_judges_seals_and_homes(full):
    r = full.r
    assert r.orch.run() == "COMPLETED"
    assert r.robot.step_calls == EVENT_STEP and r.robot.home_calls == 1
    assert results(r.orch)[0].task_outcome == "success"
    assert sm.check_journal(r.orch.journal.events) == []
    assert not full.cycle.failed and not full.cycle.request_open


def locked_without_motion(full, steps_before):
    r = full.r
    assert r.orch.state == "FAULT_LOCKED"
    assert full.cycle.failed
    assert r.orch.fence.current is None
    assert r.robot.step_calls <= steps_before  # not one action after the failure
    assert r.robot.admission_snapshot().mode != "open" or steps_before == 0
    assert sm.check_journal(r.orch.journal.events) == []


def fail_hook(full, name, how="raise", **kw):
    parts = full.parts
    if name == "stop_admission":
        original = full.r.robot.stop_admission

        def broken():
            original()
            raise OSError("gate")

        full.r.robot.stop_admission = broken
    elif name == "resume_admission":
        full.r.robot.reopen_admission_if = lambda **kw: (_ for _ in ()).throw(
            B.AdmissionRefused("identity")
        )
    elif name == "observe_hold":
        parts.hold_factory = lambda mark: (
            lambda e, p, d: (_ for _ in ()).throw(InterventionUnproven("hold"))
        )
    elif name == "observe_runtime":
        parts.runtime_probe = lambda: (_ for _ in ()).throw(OSError("probe"))
    elif name == "stop_proof":
        parts.stop_probe = lambda request, deadline: (False, True)
    elif name == "prepare_evidence":
        parts.frames.prepare_evidence = lambda request, obs: (_ for _ in ()).throw(
            ValueError("frames")
        )


@pytest.mark.parametrize(
    "name",
    [
        "stop_admission",
        "observe_hold",
        "observe_runtime",
        "stop_proof",
        "prepare_evidence",
        "resume_admission",
    ],
)
def test_a_failing_hook_latches_the_run_and_the_arm_never_moves_again(full, name):
    if name == "resume_admission":
        # A not-goal answer forces a resume; its specific reopen then fails.
        full.r.verifier = ev.FakeGoalVerifier(
            [{"decision": "rejected"}],
            full.r.clock,
            full.cfg.run_id,
            default={"decision": "confirmed"},
        )
        full.r.orch.verifier = full.r.verifier
    fail_hook(full, name)
    try:
        full.r.orch.run()
    except (RuntimeError, InterventionUnproven):
        pass
    locked_without_motion(full, EVENT_STEP)
    assert full.r.robot.home_calls == 0
    assert full.r.robot.step_calls <= EVENT_STEP


def test_a_role_window_the_kernel_rejects_latches_the_run(full):
    parts = full.parts
    original = full.hooks.as_hooks().role_window
    full.cycle.hooks = replace(
        full.cycle.hooks,
        role_window=lambda role, ep, epoch, auth, dl: original(
            role, ep, epoch - 1, auth, dl
        ),
    )
    assert full.r.orch.run() == "FAULT_LOCKED"
    assert full.r.robot.step_calls == 0 and full.cycle.failed
    assert parts.k.owner.snapshot(now_ns=full.r.clock.now())["request"] is None


def test_a_hook_that_runs_out_of_its_time_budget_latches_the_run(full):
    clock, original = full.r.clock, full.parts.runtime_probe

    def slow():
        clock.advance(10**9)  # far beyond budget_ns
        return original()

    full.parts.runtime_probe = slow
    assert full.r.orch.run() == "FAULT_LOCKED"
    assert full.cycle.failed and full.r.robot.step_calls == 0


def test_the_sole_client_proof_failing_midway_latches_the_run(full, tmp_path):
    proc = full.sub / "proc" / "net" / "tcp"
    original = full.inner.request
    state = {"n": 0}

    def request(req):
        state["n"] += 1
        if state["n"] == 1:
            proc.unlink()  # the proof can no longer be read
        return original(req)

    full.inner.request = request
    assert full.r.orch.run() == "FAULT_LOCKED"
    assert full.cycle.failed and full.r.robot.step_calls <= EVENT_STEP


# --- the probes run in a bounded daemon thread --------------------------------------------


class Hang:
    """A probe that blocks until released; counts and remembers its callers."""

    def __init__(self, result):
        self.release, self.entered = threading.Event(), threading.Event()
        self.result, self.calls = result, 0

    def __call__(self, *args):
        self.calls += 1
        self.entered.set()
        self.release.wait(30)
        return self.result() if callable(self.result) else self.result


def probe_threads():
    return [t for t in threading.enumerate() if t.name.startswith("levi-probe-")]


def test_a_hung_runtime_probe_times_out_latches_and_runs_in_a_daemon_thread(unit):
    hang = Hang(lambda: unit.k.observed)
    unit.runtime_probe = lambda: hang()
    hooks = unit.build(probe_wait_s=0.2)
    began = time.monotonic()
    with pytest.raises(InterventionUnproven):
        hooks.as_hooks().observe_runtime()
    assert time.monotonic() - began < 5
    assert hooks.failed is True
    assert hang.entered.is_set()
    threads = probe_threads()
    assert threads and all(t.daemon for t in threads)
    hang.release.set()
    for t in threads:
        t.join(5)
    with pytest.raises(InterventionUnproven):  # a late result never revives it
        hooks.as_hooks().observe_runtime()


def test_a_late_stop_probe_answer_is_dropped_and_changes_no_state(unit):
    hooks = unit.build(probe_wait_s=0.2)
    pause(unit, hooks)
    bound = hooks.as_hooks()
    request = granted(unit, hooks)
    hang = Hang((True, True))
    unit.stop_probe = lambda request, deadline: hang()
    with pytest.raises(InterventionUnproven):
        bound.stop_proof(request, unit.clock.now() + 10**9)
    before = unit.k.owner.snapshot(now_ns=unit.clock.now())
    hang.release.set()
    for t in probe_threads():
        t.join(5)
    assert unit.k.owner.snapshot(now_ns=unit.clock.now()) == before
    assert unit.k.owner.owns_exact_request(request)  # still unproven, slot untouched
    assert hooks.failed is True
    with pytest.raises(InterventionUnproven):
        bound.stop_proof(request, unit.clock.now() + 10**9)


def test_a_stale_probe_result_is_never_taken_by_the_next_call(unit):
    """Once the abandoned probe has ended, the next call runs its own probe and
    never takes the late answer."""
    first = Hang(lambda: replace(unit.k.observed, observed_ns=1))  # a stale answer
    answers = [first, lambda: replace(unit.k.observed, observed_ns=unit.clock.now())]
    unit.runtime_probe = lambda: answers.pop(0)()
    hooks = unit.build(probe_wait_s=2)
    threading.Timer(0.1, lambda: setattr(unit, "stopped", True)).start()
    # the stop interrupts the wait; a stop does not latch the binding
    with pytest.raises(InterventionStopped):
        hooks.as_hooks().observe_runtime()
    unit.stopped = False
    assert hooks.failed is False
    first.release.set()
    for t in probe_threads():
        t.join(5)
    seen = hooks.as_hooks().observe_runtime()
    assert seen.observed_ns == unit.clock.now() != 1


def test_a_call_while_the_abandoned_probe_still_runs_is_refused(unit):
    """Inside its wait the abandoned probe still runs: the next call of that hook
    is refused (and latches) instead of starting a second probe next to it."""
    first = Hang(lambda: unit.k.observed)
    answers = [first, lambda: unit.k.observed]
    unit.runtime_probe = lambda: answers.pop(0)()
    hooks = unit.build(probe_wait_s=5)
    threading.Timer(0.05, lambda: setattr(unit, "stopped", True)).start()
    with pytest.raises(InterventionStopped):
        hooks.as_hooks().observe_runtime()
    unit.stopped = False
    try:
        with pytest.raises(InterventionUnproven) as caught:
            hooks.as_hooks().observe_runtime()
        assert not isinstance(caught.value, InterventionStopped)
        assert hooks.failed is True and len(answers) == 1
    finally:
        first.release.set()
        for t in probe_threads():
            t.join(5)


@pytest.mark.parametrize("hook", ["observe_runtime", "stop_proof"])
def test_a_probe_still_hung_past_its_wait_latches_the_next_call(unit, hook):
    """Decision 4: a probe abandoned by a stop that is still running when its
    wait is over is hung; the next call of that hook latches without starting
    another probe."""
    hooks = unit.build(probe_wait_s=0.3)
    bound = hooks.as_hooks()
    first = Hang(lambda: unit.k.observed if hook == "observe_runtime" else (True, True))
    if hook == "observe_runtime":
        answers = [
            first,
            lambda: replace(unit.k.observed, observed_ns=unit.clock.now()),
        ]
        unit.runtime_probe = lambda: answers.pop(0)()

        def call():
            return bound.observe_runtime()
    else:
        pause(unit, hooks)
        request = granted(unit, hooks)
        answers = [first, lambda: (True, True)]
        unit.stop_probe = lambda request, deadline: answers.pop(0)()

        def call():
            return bound.stop_proof(request, unit.clock.now() + 10**9)

    threading.Timer(0.05, lambda: setattr(unit, "stopped", True)).start()
    with pytest.raises(InterventionStopped):
        call()
    unit.stopped = False
    assert hooks.failed is False and first.entered.is_set()
    time.sleep(0.45)  # the first probe still runs, past its 0.3 s wait
    try:
        with pytest.raises(InterventionUnproven) as caught:
            call()
        assert not isinstance(caught.value, InterventionStopped)
        assert hooks.failed is True
        assert len(answers) == 1  # no second probe was started
    finally:
        first.release.set()
        for t in probe_threads():
            t.join(5)


def test_the_stop_path_runs_while_a_probe_hangs_and_holds_no_lock_of_it(unit):
    hang = Hang(lambda: unit.k.observed)
    unit.runtime_probe = lambda: hang()
    hooks = unit.build(probe_wait_s=30)
    bound = hooks.as_hooks()
    outcome = []

    def worker():
        try:
            bound.observe_runtime()
        except InterventionUnproven as exc:
            outcome.append(type(exc))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert hang.entered.wait(5)
    began = time.monotonic()
    assert pause(unit, hooks) is True  # the stop path: close, quiesce, prove
    assert time.monotonic() - began < 2
    assert unit.rig.robot.admission_snapshot().closed
    unit.stopped = True  # the operator stop ends the worker's wait
    t.join(5)
    assert not t.is_alive() and outcome == [InterventionStopped]
    hang.release.set()


def test_a_hung_runtime_probe_in_a_run_latches_and_the_arm_never_steps(tmp_path):
    f = Full(tmp_path, probe_wait_s=0.2)
    try:
        hang = Hang(lambda: f.parts.k.observed)
        f.parts.runtime_probe = lambda: hang()
        assert f.r.orch.run() == "FAULT_LOCKED"
        assert f.cycle.failed and f.r.robot.step_calls == 0
        assert f.r.orch.fence.current is None
    finally:
        hang.release.set()
        f.r.orch.close()


def test_a_hung_stop_probe_in_a_run_latches_and_holds_without_more_steps(tmp_path):
    f = Full(tmp_path, probe_wait_s=0.2)
    try:
        hang = Hang((True, True))
        f.parts.stop_probe = lambda request, deadline: hang()
        assert f.r.orch.run() == "FAULT_LOCKED"
        assert f.cycle.failed and f.r.robot.step_calls <= EVENT_STEP
        assert f.r.robot.home_calls == 0 and f.r.orch.fence.current is None
        assert f.r.robot.admission_snapshot().mode != "open"
        assert all(t.daemon for t in probe_threads())
    finally:
        hang.release.set()
        f.r.orch.close()


def test_an_operator_stop_during_a_hung_probe_is_handled_in_bounded_time(tmp_path):
    f = Full(tmp_path, probe_wait_s=30)
    try:
        hang = Hang(lambda: f.parts.k.observed)
        f.parts.runtime_probe = lambda: hang()
        threading.Timer(0.2, lambda: setattr(f.parts, "stopped", True)).start()
        began = time.monotonic()
        state = f.r.orch.run()
        assert time.monotonic() - began < 10
        assert state in ("FAULT_LOCKED", "WAIT_HUMAN")  # never ran to the end
        assert f.r.robot.step_calls == 0 and f.r.orch.fence.current is None
        assert f.r.robot.admission_snapshot().mode == "closed"
    finally:
        hang.release.set()
        f.r.orch.close()


# --- --check --hooks and the real entry -------------------------------------------------


def event_job():
    cfg = config(
        termination=TerminationConfig(
            inference_mode="event_pause_judge", physical_hold=HOLD
        )
    )
    return {"config": cfg, "plan_sha256": PLAN}


def test_report_lists_the_seven_hooks_and_their_bindings():
    report = hooks_report(event_job()["config"], binding=None)
    assert tuple(report["hooks"]) == HOOK_NAMES
    assert report["complete"] is True
    assert report["connects_to_robot"] is False
    assert report["real_entry"] == "refused"
    for name in HOOK_NAMES:
        assert report["hooks"][name]["implemented"] is True
        assert report["hooks"][name]["bound_to"]
    assert report["physical_hold"]["declared"] is True
    assert report["signal_events"]["declared"] is False
    assert report["sole_client"]["ready"] is False  # no policy_swap unit to prove
    json.dumps(report)


def test_the_real_entry_still_refuses_event_pause_judge(tmp_path):
    job = event_job()
    with pytest.raises(ValueError, match="real execution refused"):
        R.load_binding(tmp_path / "unused.json", job)


# --- more: scenes, stops, the factory, the CLI report ---------------------------------


def test_a_scene_judge_before_any_pause_closes_the_gate_then_the_episode_resumes(unit):
    r = unit.rig
    hooks = unit.build().as_hooks()
    proof = hooks.observe_hold("hg.initial.0001", 1, unit.clock.now() + 10**9)
    assert isinstance(proof, PhysicalHoldProof)
    assert r.robot.admission_snapshot().mode == "closed"  # closed here, safe direction
    no_generic_reopen(r.robot)
    r.clock.advance(1_000)
    assert hooks.resume_admission(EP, 2, {"observed_ns": r.clock.now()}) is True
    assert r.robot.admission_snapshot().mode == "open"


def test_a_scene_judge_after_the_closed_gate_home_holds_on_the_new_interval(unit):
    r = unit.rig
    hooks = unit.build()
    pause(unit, hooks)
    r.robot.home_under_closed_gate(token=r.token())
    r.fence.revoke()
    r.clock.advance(1_000)
    bound = hooks.as_hooks()
    scene = unit.clock.now() + 10**9
    assert isinstance(bound.observe_hold("hg.scene.0001", 4, scene), PhysicalHoldProof)
    # the next episode then reopens exactly that interval
    r.clock.advance(1_000)
    assert bound.resume_admission("hg.forward.0002", 5, {"observed_ns": r.clock.now()})
    assert r.robot.admission_snapshot().mode == "open"


def test_an_operator_stop_before_a_resume_or_window_is_a_stop_not_a_failed_proof(unit):
    hooks = unit.build()
    pause(unit, hooks)
    unit.stopped = True
    bound = hooks.as_hooks()
    with pytest.raises(InterventionStopped):
        bound.resume_admission(EP, 3, {"observed_ns": unit.clock.now()})
    with pytest.raises(InterventionStopped):
        bound.role_window("judge", EP, 2, 1, unit.clock.now() + 10**9)
    assert hooks.failed is False
    unit.stopped = False
    assert bound.observe_runtime() is not None  # the binding is still usable
    assert unit.rig.robot.admission_snapshot().closed  # a stop never reopens


def test_stop_admission_still_closes_the_gate_during_an_operator_stop(unit):
    unit.stopped = True
    assert pause(unit, unit.build()) is True
    assert unit.rig.robot.admission_snapshot().closed


def test_measured_hold_factory_builds_a_measured_hold_over_the_admission_witness(unit):
    from levi.automatic.adapters.physical_hold import MeasuredPhysicalHold
    from levi.automatic.fr3_intervention import measured_hold_factory

    r = unit.rig
    factory = measured_hold_factory(
        observer_robot=r.robot,
        bridge_robot=r.robot,
        fence=r.fence,
        clock=r.clock,
        config=HOLD,
        run_id=RUN,
        plan_sha256=PLAN,
        validity_ns=10**9,
        interrupted=lambda: False,
    )
    observer = factory(r.robot.stop_admission())
    assert isinstance(observer, MeasuredPhysicalHold)
    assert isinstance(observer.observe_admission, RobotAdmissionWitness)


def test_two_episodes_over_the_hooks_chain_pause_home_scene_and_resume(tmp_path):
    f = Full(tmp_path, episodes=2)
    try:
        r = f.r
        assert r.orch.run() in ("COMPLETED", "WAIT_HUMAN")
        assert not f.cycle.failed
        assert r.robot.step_calls > EVENT_STEP and r.robot.home_calls >= 1
        assert sm.check_journal(r.orch.journal.events) == []
    finally:
        f.r.orch.close()


def check_run(tmp_path, monkeypatch, capsys, job, *argv):
    binding = tmp_path / "binding.json"
    binding.write_text("{}")
    monkeypatch.setattr(R.cli, "load_job", lambda p: job)
    monkeypatch.setattr(R, "execute", lambda *a, **k: pytest.fail("executed"))
    code = R.main(["--config", "job.yaml", "--binding", str(binding), "--check", *argv])
    return code, capsys.readouterr()


def test_check_hooks_prints_the_report_and_still_refuses_the_event_mode(
    tmp_path, monkeypatch, capsys
):
    job = event_job()
    code, out = check_run(tmp_path, monkeypatch, capsys, job, "--hooks")
    assert code == 2  # the real entry keeps refusing event_pause_judge
    assert "real execution refused" in out.err
    report = json.loads(out.out)
    assert tuple(report["hooks"]) == HOOK_NAMES and report["complete"] is True
    assert report["real_entry"] == "refused" and report["connects_to_robot"] is False


def test_check_without_hooks_is_unchanged_for_the_event_mode(
    tmp_path, monkeypatch, capsys
):
    code, out = check_run(tmp_path, monkeypatch, capsys, event_job())
    assert code == 2 and out.out == ""
    assert "real execution refused" in out.err


def test_hooks_flag_needs_check(tmp_path, monkeypatch, capsys):
    binding = tmp_path / "binding.json"
    binding.write_text("{}")
    monkeypatch.setattr(R.cli, "load_job", lambda p: event_job())
    argv = ["--config", "j", "--binding", str(binding), "--execute", "--hooks"]
    assert R.main(argv) == 2
    assert "only valid together with --check" in capsys.readouterr().err


def test_a_rejected_candidate_resumes_over_the_specific_reopen_and_pauses_again(
    tmp_path,
):
    f = Full(tmp_path, goal_script=[{"decision": "rejected"}])
    try:
        r = f.r

        def refuse():
            raise AssertionError("the generic reopen_admission() must not be used")

        r.robot.reopen_admission = refuse
        assert r.orch.run() == "COMPLETED"
        assert not f.cycle.failed
        assert r.robot.step_calls > EVENT_STEP  # it ran on after the first pause
        resumed = [
            e
            for e in r.orch.journal.events
            if e.record == "committed" and e.reason == "inference_resumed"
        ]
        assert len(resumed) == 1
        assert sm.check_journal(r.orch.journal.events) == []
    finally:
        f.r.orch.close()


# --- GateFakeRobot keeps the same gate record as the bridge ------------------------------


def gate_fake():
    clock = fk.FakeClock()
    robot = fk.GateFakeRobot(
        clock,
        lambda token, kind, now: None,
        snapshot_type=B.AdmissionSnapshot,
        refused_type=B.AdmissionRefused,
    )
    return robot, clock


def test_gate_fake_refuses_a_stale_specific_reopen_with_admission_refused():
    robot, _clock = gate_fake()
    mark = robot.stop_admission()
    for gen, start in (
        (mark.generation + 1, mark.closed_from_ns),
        (mark.generation, mark.closed_from_ns + 1),
    ):
        with pytest.raises(B.AdmissionRefused):
            robot.reopen_admission_if(generation=gen, closed_from_ns=start)
    assert robot.admission_snapshot().mode == "closed"
    robot.reopen_admission_if(
        generation=mark.generation, closed_from_ns=mark.closed_from_ns
    )
    assert robot.admission_snapshot().mode == "open"


def test_gate_fake_home_window_is_generation_plus_two_like_the_bridge():
    robot, clock = gate_fake()
    mark = robot.stop_admission()
    clock.advance(1_000)
    began = clock.now()
    robot.home_under_closed_gate(token=NS(episode_id=EP, transaction_id="h"))
    after = robot.admission_snapshot()
    assert after.mode == "closed" and after.generation == mark.generation + 2
    assert after.home_only_from_ns == began
    assert after.home_pose_released == mark.home_pose_released + 1


def test_gate_fake_stop_inside_the_window_keeps_the_stop_time_as_closure():
    """As in Robot.home_under_closed_gate: entering is +1, the stop inside the
    window is +1 and starts the closure; leaving an already closed gate changes
    neither the generation nor the closure time."""
    robot, clock = gate_fake()
    mark = robot.stop_admission()
    clock.advance(1_000)
    motion = robot._home_motion
    stopped = {}

    def move_then_stop(token, faults):
        result = motion(token, faults)
        clock.advance(500)
        stopped["at"] = robot.stop_admission().closed_from_ns
        clock.advance(500)
        return result

    robot._home_motion = move_then_stop
    robot.home_under_closed_gate(token=NS(episode_id=EP, transaction_id="h"))
    clock.advance(500)
    after = robot.admission_snapshot()
    assert after.mode == "closed" and after.generation == mark.generation + 2
    assert after.closed_from_ns == stopped["at"] < clock.now()


# --- S1: closing connections, other hosts and IPv4-mapped addresses -------------------

FIN_WAIT1, FIN_WAIT2, CLOSE_WAIT, LAST_ACK, CLOSING = "04", "05", "08", "09", "0B"


@pytest.mark.parametrize("orphan", [FIN_WAIT1, FIN_WAIT2, CLOSING])
def test_a_client_that_closed_first_is_neither_counted_nor_foreign(tmp_path, orphan):
    """The client closed its socket: its end is an orphan (inode 0) in FIN_WAIT,
    the server's end is in CLOSE_WAIT, still held by the unit."""
    p = sole_proc(tmp_path)
    p.sockets[1] = (p.sockets[1][0], p.sockets[1][1], CLOSE_WAIT, 1002)
    p.sockets[2] = (p.sockets[2][0], p.sockets[2][1], orphan, 0)
    p.procs[AERI_PID] = (p.procs[AERI_PID][0], [])
    p.write()
    assert guard(tmp_path).prove().connections == 0


@pytest.mark.parametrize("orphan", [FIN_WAIT1, FIN_WAIT2, LAST_ACK, CLOSING])
def test_a_server_that_closed_first_leaves_no_end_outside_the_unit(tmp_path, orphan):
    """The server closed its end: an orphan (inode 0) that no process holds; the
    client's end is in CLOSE_WAIT and is still this run's."""
    p = sole_proc(tmp_path)
    p.sockets[1] = (p.sockets[1][0], p.sockets[1][1], orphan, 0)
    p.sockets[2] = (p.sockets[2][0], p.sockets[2][1], CLOSE_WAIT, 1003)
    p.procs[SERVER_PID] = (p.procs[SERVER_PID][0], [1001])
    p.write()
    assert guard(tmp_path).prove().connections == 1


def test_an_orphan_with_an_inode_is_still_checked(tmp_path):
    """Only inode 0 makes a closing socket an orphan; a held FIN_WAIT end of a
    foreign process is still a foreign client."""
    p = sole_proc(tmp_path)
    p.sock(hexaddr("127.0.0.1", 40001), hexaddr("127.0.0.1", PORT), FIN_WAIT2, 1004)
    p.sock(hexaddr("127.0.0.1", PORT), hexaddr("127.0.0.1", 40001), CLOSE_WAIT, 1005)
    p.procs[SERVER_PID] = (p.procs[SERVER_PID][0], [1001, 1002, 1005])
    p.write()
    with pytest.raises(InterventionUnproven):
        guard(tmp_path).prove()


def test_a_connection_to_the_same_port_on_another_host_is_not_a_client(tmp_path):
    p = sole_proc(tmp_path)
    p.sock(hexaddr("10.0.0.2", 41000), hexaddr("10.0.0.5", PORT), "01", 3002)
    p.write()
    assert guard(tmp_path).prove().connections == 1


def test_an_ipv4_mapped_client_end_matches_its_ipv4_server_end(tmp_path):
    p = sole_proc(tmp_path)
    del p.sockets[2]  # the client end is an IPv6 socket on ::ffff:127.0.0.1
    p.sock6(
        hexaddr6_mapped("127.0.0.1", 40000),
        hexaddr6_mapped("127.0.0.1", PORT),
        "01",
        1003,
    )
    p.write()
    assert guard(tmp_path).prove().connections == 1


# --- decision 6: a closed-gate home that sent nothing (executed=no) ---------------------


def home_after_pause(unit, go_home):
    """Pause, then one closed-gate home whose _go_home is ``go_home(rig)``."""
    r = unit.rig
    hooks = unit.build()
    pause(unit, hooks)
    mark = r.robot.admission_snapshot()
    r.robot.client._go_home = lambda env, args: go_home(r, env)
    r.robot.home_under_closed_gate(token=r.token())
    r.fence.revoke()
    reopened = []
    real = r.robot.reopen_admission_if

    def recording(**kw):
        reopened.append(kw)
        return real(**kw)

    r.robot.reopen_admission_if = recording
    no_generic_reopen(r.robot)
    r.clock.advance(1_000)
    return hooks, mark, reopened


def sends_nothing(r, env):
    return None


def stops_before_the_first_command(r, env):
    r.clock.advance(7)
    r.robot.stop_admission()


@pytest.mark.parametrize("go_home", [sends_nothing, stops_before_the_first_command])
def test_a_home_that_sent_nothing_does_not_latch_and_keeps_the_gate_closed(
    unit, go_home
):
    r = unit.rig
    hooks, mark, reopened = home_after_pause(unit, go_home)
    snap = r.robot.admission_snapshot()
    # The bridge's own record: a new interval (+2) and not one fenced home POST.
    assert snap.mode == "closed" and snap.generation == mark.generation + 2
    assert (snap.home_pose_released, snap.home_gripper_released) == (
        mark.home_pose_released,
        mark.home_gripper_released,
    )
    with pytest.raises(InterventionStopped):
        resume(hooks, r, episode="hg.forward.0002", epoch=5)
    with pytest.raises(InterventionStopped):
        hooks.as_hooks().observe_hold("hg.forward.0002", 5, r.clock.now() + 10**9)
    assert hooks.failed is False  # not latched
    assert reopened == []  # not followed, not reopened
    after = r.robot.admission_snapshot()
    assert (after.mode, after.generation, after.closed_from_ns) == (
        "closed",
        snap.generation,
        snap.closed_from_ns,
    )
    hooks.as_hooks().observe_runtime()  # the binding still answers


def test_a_home_that_sent_commands_still_follows_the_new_interval(unit):
    def moves(r, env):
        env.step(np.array(HOME))

    r = unit.rig
    hooks, mark, reopened = home_after_pause(unit, moves)
    assert r.robot.admission_snapshot().home_pose_released > mark.home_pose_released
    assert resume(hooks, r, episode="hg.forward.0002", epoch=5) is True
    assert len(reopened) == 1 and r.robot.admission_snapshot().mode == "open"


def test_a_home_whose_pose_count_did_not_grow_but_sent_still_latches(unit):
    """A fenced home POST that is not a pose (here: the gripper counter grew) is
    not "executed=no": the binding latches as before."""
    r = unit.rig
    hooks, mark, reopened = home_after_pause(unit, sends_nothing)
    genuine = r.robot.admission_snapshot

    def snapshot():
        real = genuine()
        return replace(real, home_gripper_released=real.home_gripper_released + 1)

    r.robot.admission_snapshot = snapshot
    assert r.robot.admission_snapshot().home_pose_released == mark.home_pose_released
    with pytest.raises(InterventionUnproven) as caught:
        resume(hooks, r, episode="hg.forward.0002", epoch=5)
    assert not isinstance(caught.value, InterventionStopped)
    assert hooks.failed is True and reopened == []
    assert r.robot.admission_snapshot().mode == "closed"


# --- L2-HOME-EXECNO-ORCH: the orchestrator after a home that sent nothing ---------------


def refuse_first_home(robot, *, grows=False, executed="no"):
    """The first closed-gate home opens its home-only window (generation +1) and
    is refused before its first command, as ``Robot._run_home`` returns on a
    token revoked before the first POST: not one fenced home POST is counted
    (``grows`` counts one, i.e. the home did send). Later homes run normally."""
    original = robot._home_motion
    calls = {"n": 0}
    # SHOULD-4: the read-only start pose check after the wait (Robot.start_pose);
    # the arm stands at home here unless a test says otherwise.
    robot.start_pose_calls = 0

    def at_home():
        robot.start_pose_calls += 1
        return {"ok": True, "text": "error_mm=0.0 tolerance_mm=10.0"}

    robot.start_pose = at_home

    def motion(token, faults):
        calls["n"] += 1
        if calls["n"] > 1:
            return original(token, faults)
        assert robot.admission == "home_only"
        with robot._gate_lock:
            robot._generation += 1
            robot._closed_from = None
            robot._home_only_from = robot.clock.now()
            if grows:
                robot._home_count += 1
        return fk.HomeResult(executed, "MotionRefused: token revoked", None)

    robot._home_motion = motion
    return calls


def operator_resume(r, command="op-home"):
    found = r.orch.resume(
        command,
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    )
    assert found.ok, found
    return found


def committed_lines(orch):
    return [
        (e.from_state, e.to_state, e.reason)
        for e in orch.journal.events
        if e.record == "committed"
    ]


def test_a_home_refused_before_its_first_command_waits_and_the_run_goes_on(tmp_path):
    """(a) executed=no by the bridge's own counters -> WAIT_HUMAN with its own
    reason, the cycle and the binding unlatched; after the operator's resume the
    next episode rebuilds the interval with stop_admission and resumes over it.
    (d) until that resume the gate stays closed and nothing moves."""
    f = Full(tmp_path, episodes=2)
    try:
        r = f.r
        refuse_first_home(r.robot)
        assert r.orch.run() == "WAIT_HUMAN"
        assert committed_lines(r.orch)[-1] == (
            "ROBOT_HOME",
            "WAIT_HUMAN",
            "home_not_executed",
        )
        assert not f.cycle.failed and not f.cycle.proof_failed
        assert f.hooks.failed is False
        assert r.orch.fence.current is None
        steps, waiting = r.robot.step_calls, r.robot.admission_snapshot()
        assert steps == EVENT_STEP and r.robot.home_calls == 1
        # (d) the gate stays closed while the run waits: time passes, nothing
        # reopens it, and the binding still refuses to follow the new interval.
        r.clock.advance(10**9)
        with pytest.raises(InterventionStopped):
            f.hooks.as_hooks().resume_admission(
                f"{f.cfg.run_id}.forward.0002", 99, {"observed_ns": r.clock.now()}
            )
        held = r.robot.admission_snapshot()
        assert held.mode == "closed"
        assert (held.generation, held.closed_from_ns) == (
            waiting.generation,
            waiting.closed_from_ns,
        )
        assert r.robot.step_calls == steps and f.hooks.failed is False
        operator_resume(r)
        assert r.robot.admission_snapshot().mode == "closed"  # resume alone: shut
        assert r.orch.run() == "COMPLETED"
        assert r.robot.start_pose_calls == 1  # SHOULD-4: checked once, at home
        assert not f.cycle.failed and f.hooks.failed is False
        assert r.robot.step_calls > EVENT_STEP and r.robot.home_calls == 2
        lines = committed_lines(r.orch)
        assert ("WAIT_HUMAN", "PREFLIGHT", "human_resumed") in lines
        assert lines[-1][1] == "COMPLETED"
        assert sm.check_journal(r.orch.journal.events) == []
    finally:
        f.r.orch.close()


@pytest.mark.parametrize("case", ["counter_grew", "unknown", "robot_latched"])
def test_a_home_that_sent_or_failed_otherwise_still_locks(tmp_path, case):
    """(b) a home whose fenced POST counter grew, one whose result is unknown,
    and one the robot latched on: home_failed and FAULT_LOCKED, as before."""
    f = Full(tmp_path, episodes=2)
    try:
        r = f.r
        refuse_first_home(
            r.robot,
            grows=case == "counter_grew",
            executed="unknown" if case == "unknown" else "no",
        )
        if case == "robot_latched":
            original = r.robot._home_motion

            def latching(token, faults):
                result = original(token, faults)
                r.robot.latched = "hardware failure before the first command"
                return result

            r.robot._home_motion = latching
        assert r.orch.run() == "FAULT_LOCKED"
        assert committed_lines(r.orch)[-1] == (
            "ROBOT_HOME",
            "FAULT_LOCKED",
            "home_failed",
        )
        assert r.robot.admission_snapshot().mode == "closed"
        assert r.robot.step_calls == EVENT_STEP and r.orch.fence.current is None
        assert sm.check_journal(r.orch.journal.events) == []
    finally:
        f.r.orch.close()


@pytest.mark.parametrize("case", ["raises", "not_an_int", "missing_before"])
def test_a_home_whose_counters_cannot_be_read_locks(tmp_path, case):
    """(c) the bridge's counters unreadable or inconsistent: taken as executed,
    home_failed, FAULT_LOCKED (fail-closed)."""
    f = Full(tmp_path, episodes=2)
    try:
        r = f.r
        calls = refuse_first_home(r.robot)
        genuine = r.robot.admission_snapshot
        orch_reads = {"n": 0}

        def snapshot():
            # Only the orchestrator's own reads around the home are broken.
            import inspect

            caller = inspect.stack()[1].function
            if caller in ("_gate_counts", "_home_sent_nothing"):
                orch_reads["n"] += 1
                before = calls["n"] == 0
                if case == "missing_before" and before:
                    raise OSError("snapshot unreadable")
                if not before and case == "raises":
                    raise OSError("snapshot unreadable")
                if not before and case == "not_an_int":
                    return replace(genuine(), home_gripper_released=None)
            return genuine()

        r.robot.admission_snapshot = snapshot
        assert r.orch.run() == "FAULT_LOCKED"
        assert orch_reads["n"] >= 1
        assert committed_lines(r.orch)[-1] == (
            "ROBOT_HOME",
            "FAULT_LOCKED",
            "home_failed",
        )
        assert genuine().mode == "closed"
        assert r.robot.step_calls == EVENT_STEP
    finally:
        f.r.orch.close()


def test_a_home_refused_on_a_signal_locks(tmp_path):
    """NIT-1: a home refused while the robot reports a stop or signal
    (``Robot.stopped()``, e.g. SIGINT) is not home_not_executed: it locks."""
    f = Full(tmp_path, episodes=2)
    try:
        r = f.r
        refuse_first_home(r.robot)
        r.robot.stopped = lambda: True
        assert r.orch.run() == "FAULT_LOCKED"
        assert committed_lines(r.orch)[-1] == (
            "ROBOT_HOME",
            "FAULT_LOCKED",
            "home_failed",
        )
        assert r.robot.admission_snapshot().mode == "closed"
    finally:
        f.r.orch.close()


# The binding's own adoption of the interval a home that sent nothing left.


def test_adopt_unexecuted_home_takes_the_new_interval_and_resume_follows(unit):
    r = unit.rig
    hooks, _mark, reopened = home_after_pause(unit, sends_nothing)
    bound = hooks.as_hooks()
    assert bound.adopt_unexecuted_home("hg.forward.0002", 4, 5) is True
    assert hooks.failed is False and r.robot.admission_snapshot().mode == "closed"
    r.clock.advance(1_000)
    assert resume(hooks, r, episode="hg.forward.0002", epoch=5) is True
    assert len(reopened) == 1 and r.robot.admission_snapshot().mode == "open"


def test_adopt_on_the_unchanged_interval_is_accepted(unit):
    """A home refused before its window opened leaves the closed interval as
    it was: the same interval is adopted."""
    r = unit.rig
    hooks = unit.build()
    pause(unit, hooks)
    r.clock.advance(1_000)
    assert hooks.as_hooks().adopt_unexecuted_home("hg.forward.0002", 4, 5) is True
    r.clock.advance(1_000)
    assert resume(hooks, r, episode="hg.forward.0002", epoch=5) is True


@pytest.mark.parametrize(
    "case", ["pose_released", "gripper_released", "home_gripper", "reopen_then_stop"]
)
def test_adopt_refuses_a_broken_continuity_closes_and_latches(unit, case):
    """SHOULD-1: the adoption keeps the binding's continuity checks: the
    released counters equal the closure mark and the new interval is the one
    home window that sent nothing; anything else closes the gate and latches."""
    r = unit.rig
    if case == "reopen_then_stop":
        hooks = unit.build()
        pause(unit, hooks)
        r.robot.reopen_admission()  # someone else's generic reopen
        r.clock.advance(5)
        r.robot.stop_admission()  # +2 with no home window
    else:
        hooks, _mark, _reopened = home_after_pause(unit, sends_nothing)
        genuine = r.robot.admission_snapshot
        field = {
            "pose_released": "pose_attempts",
            "gripper_released": "gripper_attempts",
            "home_gripper": "home_gripper_released",
        }[case]

        def snapshot():
            real = genuine()
            return replace(real, **{field: getattr(real, field) + 1})

        r.robot.admission_snapshot = snapshot
    with pytest.raises(InterventionUnproven) as caught:
        hooks.as_hooks().adopt_unexecuted_home("hg.forward.0002", 4, 5)
    assert not isinstance(caught.value, InterventionStopped)
    assert hooks.failed is True
    assert r.robot.admission_snapshot().mode == "closed"
    with pytest.raises(InterventionUnproven):
        resume(hooks, r, episode="hg.forward.0002", epoch=5)


def bad_adopt(how):
    def adopt(episode, old_epoch, new_epoch):
        if how == "raises":
            raise OSError("rebuild")
        return {"false": False, "none": None}[how]

    return adopt


def wait_after_unexecuted_home(f):
    r = f.r
    refuse_first_home(r.robot)
    assert r.orch.run() == "WAIT_HUMAN"
    operator_resume(r)
    return r


def via_start(f):
    """The rebuild in _start: unreachable in the current flow (every _start
    comes after _decide's _assess, which rebuilds first), so the scene check
    is answered without the hooks here to reach it."""
    f.r.orch._assess = lambda target, episode: "ready"


@pytest.mark.parametrize("path", ["assess", "start"])
@pytest.mark.parametrize("how", ["raises", "false", "none"])
def test_a_rebuild_that_fails_locks_and_nothing_moves(tmp_path, path, how):
    """SHOULD-2: the binding's rebuild raising, returning False or None, on the
    scene-check path and on the episode-start path: FAULT_LOCKED, the gate
    stays closed, not one more step."""
    f = Full(tmp_path, episodes=2)
    try:
        r = wait_after_unexecuted_home(f)
        steps = r.robot.step_calls
        f.cycle.hooks = replace(f.cycle.hooks, adopt_unexecuted_home=bad_adopt(how))
        if path == "start":
            via_start(f)
        try:
            r.orch.run()
        except (RuntimeError, InterventionUnproven):
            pass
        assert r.orch.state == "FAULT_LOCKED"
        assert r.robot.admission_snapshot().mode == "closed"
        assert r.robot.step_calls == steps and r.orch.fence.current is None
        # The lock comes from the failed rebuild itself, not from a later hook
        # that refuses the stale interval (the binding's second line).
        details = [e.note.detail for e in r.orch.journal.events if e.record == "note"]
        assert any("not rebuilt after home" in str(d) for d in details), details
        assert sm.check_journal(r.orch.journal.events) == []
    finally:
        f.r.orch.close()


def test_the_rebuild_on_the_start_path_lets_the_episode_run(tmp_path):
    f = Full(tmp_path, episodes=2)
    try:
        r = wait_after_unexecuted_home(f)
        original, calls = f.cycle.hooks.adopt_unexecuted_home, []

        def recording(*args):
            calls.append(args)
            return original(*args)

        f.cycle.hooks = replace(f.cycle.hooks, adopt_unexecuted_home=recording)
        via_start(f)
        assert r.orch.run() == "COMPLETED"
        assert len(calls) == 1 and calls[0][0].endswith(".forward.0002")
        assert r.robot.step_calls > EVENT_STEP and not f.cycle.failed
    finally:
        f.r.orch.close()


@pytest.mark.parametrize(
    "case", ["off_home", "raises", "not_a_dict", "ok_not_true", "no_method"]
)
def test_after_the_wait_an_arm_off_home_or_unread_does_not_resume(tmp_path, case):
    """SHOULD-4: the operator's resume after home_not_executed checks the start
    pose once, read-only (Robot.start_pose, the start gate's own check and
    tolerance). Off home, unreadable or an odd answer: PREFLIGHT -> WAIT_HUMAN
    home_pose_not_reached, nothing moves, the gate stays closed; once the arm
    is home the next resume runs on."""
    f = Full(tmp_path, episodes=2)
    try:
        r = f.r
        refuse_first_home(r.robot)
        assert r.orch.run() == "WAIT_HUMAN"
        steps, homes = r.robot.step_calls, r.robot.home_calls
        motions = list(r.robot.motions)
        answers = {
            "off_home": lambda: {
                "ok": False,
                "text": "error_mm=42.0 tolerance_mm=10.0 rotation_rad=0.3",
                "error_mm": 42.0,
                "rotation_rad": 0.3,
            },
            "not_a_dict": lambda: "ok",
            "ok_not_true": lambda: {"ok": 1, "text": "?"},
        }
        if case == "raises":

            def broken():
                raise OSError("pose unreadable")

            r.robot.start_pose = broken
        elif case == "no_method":
            del r.robot.start_pose
        else:
            r.robot.start_pose = answers[case]
        operator_resume(r)
        assert r.orch.run() == "WAIT_HUMAN"
        assert committed_lines(r.orch)[-1] == (
            "PREFLIGHT",
            "WAIT_HUMAN",
            "home_pose_not_reached",
        )
        assert r.robot.step_calls == steps and r.robot.home_calls == homes
        assert r.robot.motions == motions  # read-only: no command was sent
        assert r.robot.admission_snapshot().mode == "closed"
        checked = [
            e.note.detail
            for e in r.orch.journal.events
            if e.record == "note" and e.note.code == "start_pose_checked"
        ]
        assert checked, "the measurement or the refusal is in the journal"
        if case == "off_home":
            assert "error_mm=42.0" in checked[-1]
        # The arm is brought home by hand: the next resume checks again, runs on.
        r.robot.start_pose = lambda: {"ok": True, "text": "error_mm=0.5"}
        operator_resume(r, "op-home-2")
        assert r.orch.run() == "COMPLETED"
        assert sm.check_journal(r.orch.journal.events) == []
    finally:
        f.r.orch.close()


def restored(f):
    """The orchestrator process restarts in place: the same robot, binding and
    cycle, a new Orchestrator over the journal (Orchestrator.restore)."""
    r = f.r
    r.orch.close()
    r.orch = Orchestrator.restore(r.directory, f.cfg, intervention=f.cycle, **r.parts())
    return r


@pytest.mark.parametrize("wait", ["home_not_executed", "home_pose_not_reached"])
@pytest.mark.parametrize("pose", ["off_home", "at_home"])
def test_a_restore_in_the_home_wait_keeps_the_pose_check_and_the_rebuild(
    tmp_path, wait, pose
):
    """Review NIT-1: restored while waiting on home_not_executed or
    home_pose_not_reached, the run still checks the start pose before going on
    (off home: it waits again, nothing moves) and still rebuilds the binding's
    interval (at home: it runs to the end) instead of leaning on the binding's
    later latch."""
    f = Full(tmp_path, episodes=2)
    try:
        r = f.r
        refuse_first_home(r.robot)
        assert r.orch.run() == "WAIT_HUMAN"
        if wait == "home_pose_not_reached":
            r.robot.start_pose = lambda: {"ok": False, "text": "error_mm=30.0"}
            operator_resume(r, "op-before")
            assert r.orch.run() == "WAIT_HUMAN"
        assert committed_lines(r.orch)[-1][2] == wait
        r = restored(f)
        # A restart always locks first (recovery_ambiguous); the person resumes.
        assert r.orch.state == "FAULT_LOCKED"
        assert committed_lines(r.orch)[-1] == (
            "WAIT_HUMAN",
            "FAULT_LOCKED",
            "recovery_ambiguous",
        )
        steps, checks = r.robot.step_calls, []
        ok = pose == "at_home"

        def start_pose():
            checks.append(ok)
            return {"ok": ok, "text": f"error_mm={0.5 if ok else 25.0}"}

        r.robot.start_pose = start_pose
        adopted = []
        original = f.cycle.hooks.adopt_unexecuted_home

        def recording(*args):
            adopted.append(args)
            return original(*args)

        f.cycle.hooks = replace(f.cycle.hooks, adopt_unexecuted_home=recording)
        operator_resume(r, "op-after")
        if ok:
            assert r.orch.run() == "COMPLETED"
            assert checks == [True] and len(adopted) == 1
            assert r.robot.step_calls > steps and not f.cycle.failed
        else:
            assert r.orch.run() == "WAIT_HUMAN"
            assert committed_lines(r.orch)[-1] == (
                "PREFLIGHT",
                "WAIT_HUMAN",
                "home_pose_not_reached",
            )
            assert checks == [False] and adopted == []
            assert r.robot.step_calls == steps
            assert r.robot.admission_snapshot().mode == "closed"
        assert sm.check_journal(r.orch.journal.events) == []
    finally:
        f.r.orch.close()


# --- S4: a refusal that sees the gate open closes it first ------------------------------


def reopened_by_someone_else(unit, hooks):
    pause(unit, hooks)
    unit.rig.robot.reopen_admission()  # the generic reopen, not this binding's
    assert unit.rig.robot.admission_snapshot().mode == "open"


@pytest.mark.parametrize(
    "case",
    [
        "observe_hold_new_witness",
        "resume_on_a_reopened_gate",
        "judge_window_on_an_open_gate",
        "stop_admission_bad_epochs",
        "stop_admission_other_run",
        "latched_binding",
    ],
)
def test_a_refusal_that_sees_the_gate_open_closes_it_first(unit, case):
    r = unit.rig
    hooks = unit.build()
    h = hooks.as_hooks()
    if case == "observe_hold_new_witness":
        reopened_by_someone_else(unit, hooks)
        call = lambda: h.observe_hold(EP, 7, r.clock.now() + 10**9)
    elif case == "resume_on_a_reopened_gate":
        reopened_by_someone_else(unit, hooks)
        call = lambda: resume(hooks, r)
    elif case == "judge_window_on_an_open_gate":
        call = lambda: h.role_window("judge", EP, 2, 1, r.clock.now() + 10**9)
    elif case == "stop_admission_bad_epochs":
        call = lambda: h.stop_admission(EP, 2, 2)
    elif case == "stop_admission_other_run":
        call = lambda: h.stop_admission("other.forward.0001", 1, 2)
    else:
        unit.runtime_probe = lambda: object()
        with pytest.raises(InterventionUnproven):
            h.observe_runtime()
        assert hooks.failed is True
        r.robot.reopen_admission()  # somebody reopens the gate afterwards
        call = h.observe_runtime
    with pytest.raises(InterventionUnproven):
        call()
    assert r.robot.admission_snapshot().mode == "closed"


# --- S5: more role_window and probe_wait_s cases ------------------------------------------


@pytest.mark.parametrize("role", ["judge", "forward"])
def test_role_window_is_refused_inside_a_home_only_window(unit, role):
    r = unit.rig
    hooks = unit.build()
    pause(unit, hooks)
    seen = {}

    def go_home(env, args):
        seen["mode"] = r.robot.admission_snapshot().mode
        try:
            hooks.as_hooks().role_window(role, EP, 2, 1, r.clock.now() + 10**9)
        except InterventionUnproven as exc:
            seen["refused"] = exc

    r.robot.client._go_home = go_home
    r.robot.home_under_closed_gate(token=r.token())
    r.fence.revoke()
    assert seen["mode"] == "home_only"
    assert isinstance(seen["refused"], InterventionUnproven)
    assert not isinstance(seen["refused"], InterventionStopped)
    assert hooks.failed is True
    assert r.robot.admission_snapshot().mode == "closed"


def test_probe_wait_s_none_bounds_a_hung_probe_by_budget_ns(unit):
    hang = Hang(lambda: unit.k.observed)
    unit.runtime_probe = lambda: hang()
    hooks = unit.build(probe_wait_s=None, budget_ns=100_000_000)  # 0.1 s
    began = time.monotonic()
    try:
        with pytest.raises(InterventionUnproven):
            hooks.as_hooks().observe_runtime()
        assert time.monotonic() - began < 3
        assert hooks.failed is True
    finally:
        hang.release.set()
        for t in probe_threads():
            t.join(5)


def test_probe_wait_s_none_bounds_a_stop_probe_by_its_deadline(unit):
    hooks = unit.build(probe_wait_s=None)
    pause(unit, hooks)
    request = granted(unit, hooks)
    hang = Hang((True, True))
    unit.stop_probe = lambda request, deadline: hang()
    began = time.monotonic()
    try:
        with pytest.raises(InterventionUnproven):
            hooks.as_hooks().stop_proof(request, unit.clock.now() + 200_000_000)
        assert time.monotonic() - began < 3  # 0.2 s to the deadline, not budget
        assert hooks.failed is True
    finally:
        hang.release.set()
        for t in probe_threads():
            t.join(5)


# --- S2: the unit name and the unit's cgroup ---------------------------------------------


@pytest.mark.parametrize(
    "unit_name",
    ["my-policy", "levi-aeri-policy-", "levi-aeri-policy-X", "xlevi-aeri-policy-a"],
)
def test_sole_client_refuses_a_unit_that_is_not_a_policy_swap_unit(tmp_path, unit_name):
    sole_proc(tmp_path).write()
    with pytest.raises(InterventionUnproven):
        guard(tmp_path, server_unit=unit_name)


def test_the_unit_cgroup_cannot_be_found_from_a_session_scope_and_says_why(tmp_path):
    p = sole_proc(tmp_path)
    p.write()
    own = tmp_path / "proc" / str(os.getpid())
    own.mkdir(parents=True, exist_ok=True)
    own.joinpath("cgroup").write_text(
        "0::/user.slice/user-1000.slice/session-7.scope\n"
    )
    g = guard(tmp_path, unit_procs_file=None, cgroup_root=str(tmp_path / "cg"))
    with pytest.raises(InterventionUnproven) as caught:
        g.prove()
    text = str(caught.value)
    assert "session-7.scope" in text and "unit_procs_file" in text


# --- B-new1: no literal "/proc" root; the real paths are built per entry ----------------


def test_no_proc_root_default_the_real_paths_are_one_entry_each(tmp_path):
    import inspect

    assert inspect.signature(ProcReader).parameters["root"].default is None
    assert inspect.signature(SoleClientGuard).parameters["proc_root"].default is None
    real = ProcReader()
    assert real.path("net", "tcp6") == Path("/proc/net/tcp6")
    assert real.path(AERI_PID, "fd") == Path(f"/proc/{AERI_PID}/fd")
    assert real.path(AERI_PID, "task") == Path(f"/proc/{AERI_PID}/task")
    fake = ProcReader(tmp_path / "proc")
    assert fake.path("net", "tcp") == tmp_path / "proc" / "net" / "tcp"


# --- B-new2: decision 6 needs a home-only window since the closure -----------------------


@pytest.mark.parametrize("hook", ["resume_admission", "observe_hold"])
def test_reopen_then_stop_is_not_an_unexecuted_home_and_latches(unit, hook):
    """pause -> generic reopen -> stop on the real Robot: generation +2 and no
    home POST, like a home that sent nothing, but no home-only window opened
    after the closure. User 21:1x: every other case is refused and latches."""
    r = unit.rig
    hooks = unit.build()
    pause(unit, hooks)
    mark = r.robot.admission_snapshot()
    r.robot.reopen_admission()
    r.clock.advance(5)
    r.robot.stop_admission()
    snap = r.robot.admission_snapshot()
    assert snap.generation == mark.generation + 2
    assert snap.home_pose_released == mark.home_pose_released
    assert snap.home_only_from_ns is None
    r.clock.advance(1_000)
    with pytest.raises(InterventionUnproven) as caught:
        if hook == "resume_admission":
            resume(hooks, r, episode="hg.forward.0002", epoch=5)
        else:
            hooks.as_hooks().observe_hold("hg.forward.0002", 5, r.clock.now() + 10**9)
    assert not isinstance(caught.value, InterventionStopped)
    assert hooks.failed is True
    assert r.robot.admission_snapshot().mode == "closed"


# --- S1b: a closing orphan with unsent data is still a connection ----------------------


@pytest.mark.parametrize(
    "orphan, tx, proven",
    [
        (FIN_WAIT1, 0, True),
        (CLOSING, 0, True),
        (FIN_WAIT1, 0x40, False),
        (CLOSING, 0x40, False),
        (FIN_WAIT2, 0x40, True),
        (LAST_ACK, 0x40, True),
    ],
)
def test_an_orphan_client_end_with_unsent_data_is_not_skipped(
    tmp_path, orphan, tx, proven
):
    """FIN_WAIT1 and CLOSING orphans are skipped only with an empty send queue
    (tx_queue, the half before the colon of column 5): with data still queued
    the closed client's bytes are still on their way to the server."""
    p = sole_proc(tmp_path)
    p.sockets[1] = (p.sockets[1][0], p.sockets[1][1], CLOSE_WAIT, 1002)
    p.sockets[2] = (p.sockets[2][0], p.sockets[2][1], orphan, 0, tx)
    p.procs[AERI_PID] = (p.procs[AERI_PID][0], [])
    p.write()
    if proven:
        assert guard(tmp_path).prove().connections == 0
    else:
        with pytest.raises(InterventionUnproven):
            guard(tmp_path).prove()


# --- NIT: the gate-close failure is reported, never raised over the refusal ------------


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_a_gate_that_cannot_be_closed_is_named_in_the_refusal(unit, error):
    r = unit.rig
    hooks = unit.build()
    unit.runtime_probe = lambda: object()  # the hook fails

    def broken():
        raise error("gate")

    r.robot.stop_admission = broken
    with pytest.raises(InterventionUnproven) as caught:
        hooks.as_hooks().observe_runtime()
    assert "could not be closed: " + error.__name__ in str(caught.value)
    assert hooks.failed is True and hooks.close_error == error.__name__
    with pytest.raises(InterventionUnproven) as again:  # the latched branch too
        hooks.as_hooks().observe_runtime()
    assert "could not be closed: " + error.__name__ in str(again.value)
