"""FR3 implementation of the seven event-pause intervention hooks (AERI slice D).

``InferenceCycle`` (``intervention.py``) runs the pause/judge/resume protocol over
seven injected hooks. This module is what an FR3 run puts behind them. It adds no
scheduler and no authority of its own: every hook is a thin, fail-closed binding
of an existing object.

=================  ==========================================================
stop_admission     ``Robot.stop_admission()`` (closure mark) + ``Policy.quiesce``
                   of the old epoch + no motion token + sole-client proof
resume_admission   ``Robot.reopen_admission_if(generation, closed_from_ns)``:
                   a SPECIFIC reopen of the closed interval this intervention
                   made. Never the generic ``reopen_admission()``.
observe_hold       ``MeasuredPhysicalHold`` over a ``RobotAdmissionWitness``
                   (one observer per closure, from ``hold_factory``)
observe_runtime    a trusted ``RuntimeObservation`` of the owned group
role_window        a ``RoleWindow`` for ``RuntimeOwnership.begin_role_request``
stop_proof         a ``RequestStopProof`` for the exact request in the slot
prepare_evidence   ``PausedFrames.prepare_evidence`` (the ``signal_events`` ledger)
=================  ==========================================================

Failure direction. A hook never answers "probably". It returns the one value the
interface accepts or raises ``InterventionUnproven``; a hook that raises, runs
past its time bound, or finds its fact unproven also latches this binding, so
every later hook raises too. ``InferenceCycle`` turns the exception into its own
irreversible latch and the orchestrator into FAULT_LOCKED with the motion fence
revoked and the arm held. Operator stops (``InterventionStopped``) do not latch.

Sole client (gap G1). ``SoleClientGuard`` proves, at the instant it runs, that the
policy service listens only on a loopback private port, that the listener belongs
to the unit this run started, and that every connection to that port belongs to
this run: it counts the connections in the kernel-wide socket table (N) and the
ones held by this run's own processes (M) and refuses on N != M. It opens no
other process's ``/proc/<pid>/`` (the account is shared with other people). It is repeated by ``stop_admission``,
``observe_runtime``, ``role_window``, ``stop_proof`` and (through
``SlotBoundPolicy``) before every policy request. It is a point-in-time proof; a
process that connects between two checks is caught by the next one, not
prevented. See docs/AUTOMATIC_PIPELINE.md.

Nothing here connects to a port or the robot. The real entry still refuses
``event_pause_judge`` (``fr3_run.load_binding``); these hooks are assembled only
by tests until slice G.
"""

import ipaddress
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from levi.domain import aeri
from levi.live.runtime_ownership import RequestStopProof, RoleWindow, RuntimeObservation

from .adapters.physical_hold import assemble_measured_hold
from .fr3_bridge import AdmissionRefused, AdmissionSnapshot
from .fr3_policy_swap import UNIT as POLICY_UNIT
from .intervention import (
    InterventionHooks,
    InterventionStopped,
    InterventionUnproven,
    PhysicalHoldProof,
)

HOOK_NAMES = tuple(InterventionHooks.__dataclass_fields__)


@dataclass(frozen=True)
class Fr3BoundHooks(InterventionHooks):
    """The seven hooks plus the FR3 binding's one extra entry: the adoption of
    the closed interval a home that sent nothing left (L2-HOME-EXECNO-ORCH).
    Not an eighth hook of the interface; the orchestrator looks it up and locks
    without it."""

    adopt_unexecuted_home: Callable


# Ports other things own. The policy service of an AERI run must listen on its own.
SHARED_PORTS = frozenset(
    {5000, 5001, 5100, 7470, 8000, 8100}
    | set(range(7860, 7863))
    | {
        7864,
        7866,
        7868,
        7870,
        7871,
        7872,
        7873,
        7874,
        7875,
        7880,
        7881,
        7882,
        7890,
        7891,
    }
)

LISTEN, TIME_WAIT = "0A", "06"
# The closing states in which the kernel keeps a socket after its last file
# descriptor was closed (an orphan, inode 0): FIN_WAIT1, FIN_WAIT2, LAST_ACK and
# CLOSING. No process holds such a socket, so it cannot be anyone's client. In
# FIN_WAIT1 and CLOSING it may still hold unsent bytes: only with an empty send
# queue is it skipped.
ORPHAN_STATES = frozenset({"04", "05", "09", "0B"})
DRAINING_STATES = frozenset({"04", "0B"})


# --- the sole-client proof (G1) -------------------------------------------------------


@dataclass(frozen=True)
class SoleClientProof:
    port: int
    server_pids: tuple
    client_pids: tuple
    connections: int
    observed_ns: int
    clock_domain: str


@dataclass(frozen=True)
class _Socket:
    local_ip: object
    local_port: int
    remote_ip: object
    remote_port: int
    state: str
    inode: int
    tx_queue: int = 0


def _address(text):
    host, port = text.split(":")
    raw = bytes.fromhex(host)
    if len(raw) == 4:
        ip = ipaddress.IPv4Address(raw[::-1])
    elif len(raw) == 16:
        ip = ipaddress.IPv6Address(
            b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        )
    else:
        raise ValueError("address")
    return ip, int(port, 16)


def _plain(ip):
    """An IPv4-mapped IPv6 address (::ffff:a.b.c.d) as the IPv4 address it is, so
    the two ends of one connection compare equal across tcp and tcp6."""
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip if mapped is None else mapped


def _orphan(s):
    return (
        s.inode == 0
        and s.state in ORPHAN_STATES
        and (s.state not in DRAINING_STATES or s.tx_queue == 0)
    )


def _loopback(ip):
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


class ProcReader:
    """The only code that opens ``/proc``: the kernel-wide socket tables and the
    ``fd`` / ``task/*/children`` entries of ONE named pid. Callers pass only pids of
    this run (this process, its descendants, the policy unit's cgroup). It never
    lists ``/proc`` and never opens another process's folder."""

    def __init__(self, root=None):
        # None: the real kernel paths, built one entry at a time (never a "/proc"
        # root that could be listed). Tests pass a fake root.
        self.root = None if root is None else Path(root)

    def path(self, *parts):
        """One entry: ("net", "tcp"), (pid, "fd"), (pid, "task") or (pid, "cgroup")."""
        rel = "/".join(str(p) for p in parts)
        return Path(f"/proc/{rel}") if self.root is None else self.root / rel

    def table(self, name):
        return self.path("net", name).read_text()

    def socket_inodes(self, pid):
        out = set()
        for entry in self.path(pid, "fd").iterdir():
            target = os.readlink(entry)
            if target.startswith("socket:[") and target.endswith("]"):
                out.add(int(target[8:-1]))
        return out

    def children(self, pid):
        out = set()
        task = self.path(pid, "task")
        if not task.is_dir():
            return out
        for thread in task.iterdir():
            out.update(int(x) for x in (thread / "children").read_text().split())
        return out

    @staticmethod
    def unit_pids(procs_file):
        return {int(x) for x in Path(procs_file).read_text().split()}


def default_unit_procs_file(unit, *, reader, cgroup_root="/sys/fs/cgroup"):
    """``cgroup.procs`` of the transient user unit ``policy_swap`` started: it sits
    under the same ``user@<uid>.service`` manager as this process (read from this
    process's own cgroup)."""
    own = reader.path(os.getpid(), "cgroup").read_text()
    for line in own.splitlines():
        path = line.split(":", 2)[-1]
        head, sep, _ = path.partition(".service/")
        if sep and "/user@" in head + ".service":
            return Path(
                cgroup_root
                + head
                + ".service/app.slice/"
                + unit
                + ".service/cgroup.procs"
            )
    seen = ", ".join(line.split(":", 2)[-1] for line in own.splitlines()) or "nothing"
    raise InterventionUnproven(
        "the policy unit's cgroup.procs cannot be located: this process's cgroup ("
        + seen
        + ") is not under a user@<uid>.service manager (for example a session "
        "scope); start it under the user manager or pass unit_procs_file"
    )


class SoleClientGuard:
    """Proof that AERI alone talks to the policy service, on a private port.

    By counting, without looking at other people's processes. ``N`` = the
    connections to the policy port in the kernel-wide ``<proc_root>/net/tcp{,6}``
    (established or half-open client ends; the shared account's other users show
    up there too). ``M`` = how many of those client ends are held by this run's
    own processes (``client_pids``, default this process, and their descendants).
    ``N != M`` means a foreign client: refuse. The listener must be a loopback-only
    socket whose inode is open in a process of the unit this run started
    (``unit_procs_file``, that unit's ``cgroup.procs``), as must every accepted
    connection. Only this run's own pids' ``fd`` folders are opened, never the
    ``/proc`` listing or any other ``/proc/<pid>/``. Anything unreadable fails
    the proof. Opens no socket.
    """

    def __init__(
        self,
        *,
        proc_root=None,
        port,
        server_unit,
        client_pids=None,
        clock,
        shared_ports=SHARED_PORTS,
        reader=None,
        unit_procs_file=None,
        cgroup_root="/sys/fs/cgroup",
    ):
        client_pids = (os.getpid(),) if client_pids is None else client_pids
        if (
            type(port) is not int
            or not 1024 <= port <= 65535
            or port in shared_ports
            or not isinstance(server_unit, str)
            or not POLICY_UNIT.fullmatch(server_unit)
            or not isinstance(client_pids, tuple | list)
            or not client_pids
            or any(type(p) is not int or p < 1 for p in client_pids)
            or not callable(getattr(clock, "now", None))
            or not isinstance(getattr(clock, "domain", None), str)
        ):
            raise InterventionUnproven(
                "a private (not shared) policy port, the run's own levi-aeri-policy-<name> "
                "unit and a client pid are required"
            )
        self.reader = reader or ProcReader(proc_root)
        self.port, self.server_unit = port, server_unit
        self.client_pids = tuple(sorted(set(client_pids)))
        self.clock = clock
        self.unit_procs_file = unit_procs_file
        self.cgroup_root = cgroup_root

    def _sockets(self):
        out = []
        for name in ("tcp", "tcp6"):
            for line in self.reader.table(name).splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10:
                    raise ValueError("malformed socket table line")
                local_ip, local_port = _address(fields[1])
                remote_ip, remote_port = _address(fields[2])
                out.append(
                    _Socket(
                        _plain(local_ip),
                        local_port,
                        _plain(remote_ip),
                        remote_port,
                        fields[3],
                        int(fields[9]),
                        int(fields[4].split(":", 1)[0], 16),
                    )
                )
        return out

    def _own_pids(self):
        """This run's client processes and their descendants."""
        seen, todo = set(), list(self.client_pids)
        while todo:
            pid = todo.pop()
            if pid not in seen:
                seen.add(pid)
                todo.extend(self.reader.children(pid))
        return seen

    def prove(self) -> SoleClientProof:
        try:
            return self._prove()
        except InterventionUnproven:
            raise
        except Exception as exc:
            raise InterventionUnproven(
                "sole-client proof unavailable: " + type(exc).__name__
            ) from exc

    def _fail(self, why):
        raise InterventionUnproven("policy service is not provably private: " + why)

    def _prove(self):
        port = self.port
        sockets = self._sockets()
        live = (LISTEN, TIME_WAIT)
        listeners = [s for s in sockets if s.local_port == port and s.state == LISTEN]
        # Server ends; an orphan (the server closed it, inode 0) is held by nobody.
        accepted = [
            s
            for s in sockets
            if s.local_port == port and s.state not in live and not _orphan(s)
        ]
        # Client ends on this host of connections to this host's port: the peer is
        # a loopback address (a connection to the same port number on another
        # host is not one). Orphans are the client side of a closing connection.
        ends = [
            s
            for s in sockets
            if s.remote_port == port
            and s.local_port != port
            and s.state not in live
            and _loopback(s.remote_ip)
        ]
        clients = [s for s in ends if not _orphan(s)]
        if not listeners:
            self._fail("nothing listens on port " + str(port))
        if any(not _loopback(s.local_ip) for s in listeners):
            self._fail("the listener is not bound to loopback only")
        # The server end: inodes must be open in the unit this run started.
        procs_file = self.unit_procs_file or default_unit_procs_file(
            self.server_unit, reader=self.reader, cgroup_root=self.cgroup_root
        )
        server = self.reader.unit_pids(procs_file)
        server_inodes, holders = set(), set()
        for pid in sorted(server):
            inodes = self.reader.socket_inodes(pid)
            server_inodes |= inodes
            if inodes & {s.inode for s in listeners + accepted}:
                holders.add(pid)
        if any(not s.inode or s.inode not in server_inodes for s in listeners):
            self._fail("the listener is not held by a process of " + self.server_unit)
        if any(s.inode not in server_inodes for s in accepted):
            self._fail("a connection's server end is outside " + self.server_unit)
        for s in accepted:
            if not _loopback(s.remote_ip) or not any(
                c.local_ip == s.remote_ip and c.local_port == s.remote_port
                for c in ends
            ):
                self._fail("a connection has no local client end")
        # The client end: count N from the kernel table, M from our own processes.
        mine = set()
        for pid in sorted(self._own_pids()):
            mine |= self.reader.socket_inodes(pid)
        ours = [s for s in clients if s.inode in mine]
        if len(ours) != len(clients):
            self._fail(
                f"{len(clients)} connections to the port, only {len(ours)} are this run's"
            )
        return SoleClientProof(
            port,
            tuple(sorted(holders)),
            self.client_pids,
            len(clients),
            self.clock.now(),
            self.clock.domain,
        )


class SlotBoundPolicy:
    """A policy whose ``request`` runs only inside RuntimeOwnership's request slot.

    ``request`` needs the sole-client proof and a granted, unrevoked request in
    the single slot whose role, episode and policy epoch are this request's. Every
    other attribute is the wrapped policy's own. A violation raises
    ``InterventionUnproven`` (inside ``InferenceCycle.invoke`` that is a proof
    failure, so the run latches).
    """

    def __init__(self, policy, *, authority, guard, clock):
        if (
            not callable(getattr(policy, "request", None))
            or not callable(getattr(authority, "snapshot", None))
            or not callable(getattr(guard, "prove", None))
            or not callable(getattr(clock, "now", None))
        ):
            raise InterventionUnproven("policy, slot authority, guard and clock needed")
        self._policy, self._authority = policy, authority
        self._guard, self._clock = guard, clock

    def request(self, request):
        self._guard.prove()
        episode, epoch = request.get("episode_id"), request.get("policy_epoch")
        snap = self._authority.snapshot(now_ns=self._clock.now())
        token = snap["request"]
        if (
            token is None
            or snap["request_revoked"] is not False
            or token.episode_id != episode
            or token.policy_epoch != epoch
            or token.role != aeri.episode_parts(episode)[1]
        ):
            raise InterventionUnproven(
                "policy request without a granted, unrevoked slot of its own role/episode/epoch"
            )
        return self._policy.request(request)

    def __getattr__(self, name):
        return getattr(self._policy, name)


# --- the hooks ------------------------------------------------------------------------


@dataclass
class _Interval:
    """The latest closed gate interval this intervention made or adopted."""

    mark: AdmissionSnapshot
    witnesses: dict  # (episode, epoch) -> observe_hold callable


def measured_hold_factory(
    *,
    observer_robot,
    bridge_robot,
    fence,
    clock,
    config,
    run_id,
    plan_sha256,
    validity_ns,
    interrupted,
):
    """``hold_factory`` for a real run: a MeasuredPhysicalHold whose admission
    source is a RobotAdmissionWitness built from the closure mark."""

    def factory(mark):
        return assemble_measured_hold(
            observer_robot,
            bridge_robot,
            fence,
            clock,
            config,
            mark,
            run_id=run_id,
            plan_sha256=plan_sha256,
            validity_ns=validity_ns,
            interrupted=interrupted,
        )

    return factory


class _Answer:
    """One probe call's one-shot result slot. Its lock is private to the call (the
    probe thread and the waiter only); no other path ever takes it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.done = threading.Event()
        self._abandoned = False
        self._outcome = None

    def deliver(self, outcome):
        with self._lock:
            if self._abandoned:
                return  # late: dropped, written nowhere
            self._outcome = outcome
            self.done.set()

    def abandon(self):
        """Give up. Returns the outcome if it arrived at the last instant."""
        with self._lock:
            if self._outcome is None:
                self._abandoned = True
            return self._outcome


# Decision 2026-10-14 21:1x: a closed-gate home refused before its first command
# (executed=no, the arm did not move) is not followed and does not latch this
# binding; the gate stays closed and the orchestrator takes its own path.
_HOME_NOT_EXECUTED = (
    "the closed-gate home sent no command (executed=no): the gate stays closed, "
    "the binding does not follow the new interval"
)


class Fr3InterventionHooks:
    """The seven hooks over FR3 objects; ``as_hooks()`` is the InterventionHooks.

    robot          fr3_bridge.Robot (admission_snapshot / stop_admission /
                   reopen_admission_if)
    fence          the orchestrator's MotionFence (``current`` must be None)
    policy         has ``quiesce(policy_epoch=)`` (fr3_bridge.Policy)
    authority      the RuntimeOwnership of the run; runtime its RuntimeToken
    broker         its WorkloadBroker (identity of role windows)
    runtime_probe  () -> trusted RuntimeObservation of the owned group
    stop_probe     (request, deadline_ns) -> (backend_request_stopped,
                   request_workers_exited), both exactly True or the hook raises
    hold_factory   (closure mark) -> observe_hold callable; see
                   ``measured_hold_factory``
    frames         has ``prepare_evidence(request, observation)`` (PausedFrames)
    sole_client    has ``prove()`` (SoleClientGuard)
    budget_ns      time bound of the quick hooks, measured by ``clock`` after the
                   hook returns (the hooks with a deadline argument are bound by
                   that deadline)
    probe_wait_s   real seconds a probe may run before it counts as hung
                   (default ``budget_ns``; for ``stop_probe`` at most the time to
                   its deadline). Both probes run in a daemon thread: a probe that
                   never returns latches this binding when the wait ends, the
                   caller is never blocked longer, an operator stop ends the wait
                   at once, and a late answer is dropped
    interrupted    () -> truthy after an operator stop, signal or latch
    """

    def __init__(
        self,
        *,
        robot,
        fence,
        policy,
        clock,
        run_id,
        plan_sha256,
        authority,
        runtime,
        broker,
        profile_sha256,
        runtime_probe,
        stop_probe,
        hold_factory,
        frames,
        sole_client,
        budget_ns,
        interrupted,
        probe_wait_s=None,
    ):
        if (
            any(
                not callable(getattr(robot, n, None))
                for n in (
                    "admission_snapshot",
                    "stop_admission",
                    "reopen_admission_if",
                )
            )
            or not hasattr(fence, "current")
            or not callable(getattr(policy, "quiesce", None))
            or not callable(getattr(clock, "now", None))
            or not isinstance(getattr(clock, "domain", None), str)
            or not isinstance(run_id, str)
            or not run_id
            or not isinstance(plan_sha256, str)
            or len(plan_sha256) != 64
            or any(
                not callable(getattr(authority, n, None))
                for n in ("snapshot", "owns_exact_request")
            )
            or not hasattr(runtime, "lease_id")
            or not isinstance(getattr(broker, "broker_id", None), str)
            or not isinstance(profile_sha256, str)
            or not profile_sha256
            or not callable(runtime_probe)
            or not callable(stop_probe)
            or not callable(hold_factory)
            or not callable(getattr(frames, "prepare_evidence", None))
            or not callable(getattr(sole_client, "prove", None))
            or type(budget_ns) is not int
            or budget_ns < 1
            or not callable(interrupted)
            or (
                probe_wait_s is not None
                and (type(probe_wait_s) not in (int, float) or probe_wait_s <= 0)
            )
        ):
            raise InterventionUnproven(
                "robot gate, fence, policy, owned runtime authority, trusted probes, "
                "hold/evidence bindings, sole-client guard and time bound are all required"
            )
        self.robot, self.fence, self.policy, self.clock = robot, fence, policy, clock
        self.run_id, self.plan_sha256 = run_id, plan_sha256
        self.authority, self.runtime, self.broker = authority, runtime, broker
        self.profile_sha256 = profile_sha256
        self.runtime_probe, self.stop_probe = runtime_probe, stop_probe
        self.hold_factory, self.frames = hold_factory, frames
        self.sole_client, self.budget_ns = sole_client, budget_ns
        self.interrupted = interrupted
        self.probe_wait_s = probe_wait_s
        self.failed = False
        self.close_error = None  # why the last refusal could not close the gate
        # hook name -> (thread, monotonic end of its wait) of a probe abandoned
        # while still running; checked by the next call of that hook
        self._probe_threads = {}
        self._interval = None  # the latest closed interval this intervention owns

    # --- plumbing ---

    def as_hooks(self) -> InterventionHooks:
        return Fr3BoundHooks(
            **{name: getattr(self, "_" + name) for name in HOOK_NAMES},
            adopt_unexecuted_home=self._adopt_unexecuted_home,
        )

    def bindings(self) -> dict:
        """What each hook, physical_hold and signal_events are bound to now."""
        return {
            "stop_admission": "fr3_bridge.Robot.stop_admission (closure mark) + "
            "Policy.quiesce(old epoch) + no motion token + SoleClientGuard.prove",
            "resume_admission": "fr3_bridge.Robot.reopen_admission_if(generation, "
            "closed_from_ns) of the interval this intervention closed; the generic "
            "reopen is never called",
            "observe_hold": "hold_factory(closure mark) -> "
            + type(self.hold_factory).__name__
            + " (MeasuredPhysicalHold over RobotAdmissionWitness)",
            "observe_runtime": "runtime_probe (trusted RuntimeObservation) bound to "
            "the RuntimeOwnership runtime token + SoleClientGuard.prove",
            "role_window": "RuntimeOwnership role window (RoleWindow) for "
            "begin_role_request; judge needs a closed gate, forward/reset an open one",
            "stop_proof": "stop_probe -> RequestStopProof of the exact request in "
            "the RuntimeOwnership slot + SoleClientGuard.prove",
            "prepare_evidence": type(self.frames).__name__ + ".prepare_evidence",
            "physical_hold": "adapters.physical_hold.MeasuredPhysicalHold + "
            "RobotAdmissionWitness (limits frozen in termination.physical_hold)",
            "signal_events": "adapters.signal_events.SignalEvents -> "
            "event_window.Ledger -> " + type(self.frames).__name__,
        }

    def _close_gate(self):
        """Closing is the safe direction: a refusal closes the gate before it
        raises. ``stop_admission`` is idempotent on a closed gate (inside a
        home-only window it ends the window). A failure here is not raised over
        the refusal being raised, also a BaseException (KeyboardInterrupt): the
        refusal is kept and raised, and the failure is named in its text.
        Returns that text ("" when the gate was closed)."""
        try:
            self.robot.stop_admission()
        except BaseException as exc:  # noqa: BLE001 - the refusal is raised either way
            self.close_error = type(exc).__name__
            return "; the gate could not be closed: " + self.close_error
        return ""

    def _run(self, name, fn, *args, deadline=None):
        """Run one hook body: any failure closes the gate, latches this binding,
        then raises; a latched binding closes the gate again on every call."""
        if self.failed:
            note = self._close_gate()
            raise InterventionUnproven(
                name + ": an earlier hook failed; latched" + note
            )
        try:
            began = self.clock.now()
            result = fn(*args)
            ended = self.clock.now()
            limit = deadline if deadline is not None else began + self.budget_ns
            if ended > limit:
                raise InterventionUnproven(name + " ran past its time bound")
            return result
        except InterventionStopped:
            raise
        except BaseException as exc:
            self.failed = True
            note = self._close_gate()
            if isinstance(exc, InterventionUnproven):
                if not note:
                    raise
                raise InterventionUnproven(str(exc) + note) from exc
            if isinstance(exc, Exception):
                raise InterventionUnproven(
                    f"{name}: {type(exc).__name__}: unproven" + note
                ) from exc
            raise

    def _bounded(self, name, fn, *args, wait_s):
        """Call a probe in a daemon thread for at most ``wait_s`` real seconds.

        A hung probe cannot be killed, so it is abandoned: the wait ends with
        ``InterventionUnproven`` (``_run`` latches), an operator stop ends it with
        ``InterventionStopped``, and the thread's late answer goes to a one-shot
        slot nobody reads any more. The thread holds no lock of this binding, the
        robot or the kernel and sends nothing to the robot.
        """
        prev = self._probe_threads.get(name)
        if prev is not None:
            if not prev[0].is_alive():
                del self._probe_threads[name]
            elif time.monotonic() >= prev[1]:
                # Abandoned by a stop and still running after its own wait: hung.
                raise InterventionUnproven(
                    name + ": the previous probe is still running past its wait"
                )
            else:
                # Still inside its wait: never a second probe next to it.
                raise InterventionUnproven(
                    name + ": the previous probe is still running"
                )
        slot = _Answer()

        def body():
            try:
                slot.deliver(("ok", fn(*args)))
            except BaseException as exc:  # noqa: BLE001 - carried to the caller
                slot.deliver(("err", exc))

        thread = threading.Thread(target=body, name="levi-probe-" + name, daemon=True)
        end = time.monotonic() + max(0.0, wait_s)
        thread.start()
        while not slot.done.wait(min(0.02, max(0.0, end - time.monotonic()))):
            if self.interrupted():
                slot.abandon()
                self._probe_threads[name] = (thread, end)
                raise InterventionStopped(name + ": stop during the probe")
            if time.monotonic() >= end:
                late = slot.abandon()
                if late is None:
                    self._probe_threads[name] = (thread, end)
                    raise InterventionUnproven(name + ": the probe did not return")
                slot.done.set()
                break
        kind, value = slot._outcome
        if kind == "err":
            raise value
        return value

    def _wait_s(self, deadline=None):
        cap = self.probe_wait_s
        if deadline is None:
            return self.budget_ns / 1e9 if cap is None else cap
        left = max(0, deadline - self.clock.now()) / 1e9
        return left if cap is None else min(cap, left)

    def _stopped(self):
        if self.interrupted():
            raise InterventionStopped("stop or latch before the hook")

    @staticmethod
    def _episode_run(episode):
        return aeri.episode_parts(episode)[0]

    # --- the seven hooks ---

    def _stop_admission(self, episode, old_epoch, new_epoch):
        return self._run(
            "stop_admission", self._do_stop_admission, episode, old_epoch, new_epoch
        )

    def _do_stop_admission(self, episode, old_epoch, new_epoch):
        # Closing is the safe direction: it happens first, before the arguments
        # are checked, and stays on failure.
        mark = self.robot.stop_admission()
        if (
            type(old_epoch) is not int
            or type(new_epoch) is not int
            or new_epoch <= old_epoch
            or self._episode_run(episode) != self.run_id
        ):
            raise InterventionUnproven("pause must name this run and advance the epoch")
        self._closed_mark(mark)
        self.sole_client.prove()
        if self.fence.current is not None:
            raise InterventionUnproven("a motion token is still current")
        fenced = self.policy.quiesce(policy_epoch=old_epoch)
        if (
            not isinstance(fenced, dict)
            or fenced.get("ok") is not True
            or fenced.get("fenced_epoch") != old_epoch
        ):
            raise InterventionUnproven("old policy epoch/buffer fence unproven")
        self._interval = _Interval(
            mark, {(episode, new_epoch): self._observer_for(mark)}
        )
        return True

    def _closed_mark(self, mark):
        if not (
            isinstance(mark, AdmissionSnapshot)
            and mark.closed is True
            and mark.mode == "closed"
            and type(mark.generation) is int
            and mark.generation >= 1
            and type(mark.closed_from_ns) is int
            and mark.clock_domain == self.clock.domain
        ):
            raise InterventionUnproven(
                "admission gate closure not proven by a snapshot"
            )

    def _observer_for(self, mark):
        observer = self.hold_factory(mark)
        if not callable(observer):
            raise InterventionUnproven("hold_factory must return an observer")
        return observer

    def _resume_admission(self, episode, epoch, observation):
        return self._run(
            "resume_admission",
            self._do_resume_admission,
            episode,
            epoch,
            observation,
        )

    def _do_resume_admission(self, episode, epoch, observation):
        self._stopped()
        if (
            type(epoch) is not int
            or not isinstance(observation, dict)
            or type(observation.get("observed_ns")) is not int
            or self._episode_run(episode) != self.run_id
        ):
            raise InterventionUnproven(
                "resume needs this run's episode and a fresh observation"
            )
        if self.authority.snapshot(now_ns=self.clock.now())["request"] is not None:
            raise InterventionUnproven("a role request still holds the execution slot")
        self.sole_client.prove()
        snap = self.robot.admission_snapshot()
        interval = self._interval
        if interval is None:
            # Nothing was closed by this intervention: only an open gate may start.
            if snap.mode == "open" and not snap.closed:
                return True
            raise InterventionUnproven(
                "the closed gate was not closed by this intervention"
            )
        mark = interval.mark
        if (
            snap.mode != "closed"
            or snap.clock_domain != self.clock.domain
            or snap.pose_released != mark.pose_released
            or snap.gripper_released != mark.gripper_released
            or snap.closed_from_ns is None
            or observation["observed_ns"] < snap.closed_from_ns
        ):
            raise InterventionUnproven(
                "the gate is not the interval this intervention closed"
            )
        if (snap.generation, snap.closed_from_ns) != (
            mark.generation,
            mark.closed_from_ns,
        ):
            if self._home_sent_nothing(mark, snap):
                raise InterventionStopped(_HOME_NOT_EXECUTED)
            if not self._after_one_home(mark, snap):
                raise InterventionUnproven(
                    "the gate interval differs from the one closed"
                )
        try:
            self.robot.reopen_admission_if(
                generation=snap.generation, closed_from_ns=snap.closed_from_ns
            )
        except AdmissionRefused as exc:
            raise InterventionUnproven("specific reopen refused: " + str(exc)) from exc
        after = self.robot.admission_snapshot()
        if after.mode != "open" or after.closed:
            raise InterventionUnproven("gate not open after the specific reopen")
        self._interval = None
        return True

    def _adopt_unexecuted_home(self, episode, old_epoch, new_epoch):
        return self._run(
            "adopt_unexecuted_home",
            self._do_adopt_unexecuted_home,
            episode,
            old_epoch,
            new_epoch,
        )

    def _do_adopt_unexecuted_home(self, episode, old_epoch, new_epoch):
        """After a closed-gate home that sent nothing: take the gate's current
        closed interval as this binding's, but only when it continues the
        closure this intervention made: the same interval (the home was refused
        before its window opened), or the one home window that sent nothing
        (``_home_sent_nothing``), and in both cases the pose and gripper
        released counters equal the closure mark's. Anything else is refused;
        ``_run`` then closes the gate and latches. Nothing is reopened here."""
        if (
            type(old_epoch) is not int
            or type(new_epoch) is not int
            or new_epoch <= old_epoch
            or self._episode_run(episode) != self.run_id
        ):
            raise InterventionUnproven(
                "adoption must name this run and advance the epoch"
            )
        interval = self._interval
        if interval is None:
            raise InterventionUnproven("no closure of this intervention to adopt")
        mark = interval.mark
        snap = self.robot.admission_snapshot()
        if not (
            isinstance(snap, AdmissionSnapshot)
            and snap.mode == "closed"
            and snap.closed is True
            and snap.clock_domain == self.clock.domain
            and snap.pose_released == mark.pose_released
            and snap.gripper_released == mark.gripper_released
            and snap.home_pose_released == mark.home_pose_released
            and snap.home_gripper_released == mark.home_gripper_released
            and (
                (snap.generation, snap.closed_from_ns)
                == (mark.generation, mark.closed_from_ns)
                or self._home_sent_nothing(mark, snap)
            )
        ):
            raise InterventionUnproven(
                "the gate interval does not continue this intervention's closure"
            )
        self._closed_mark(snap)
        self.sole_client.prove()
        if self.fence.current is not None:
            raise InterventionUnproven("a motion token is still current")
        fenced = self.policy.quiesce(policy_epoch=old_epoch)
        if (
            not isinstance(fenced, dict)
            or fenced.get("ok") is not True
            or fenced.get("fenced_epoch") != old_epoch
        ):
            raise InterventionUnproven("old policy epoch/buffer fence unproven")
        self._interval = _Interval(
            snap, {(episode, new_epoch): self._observer_for(snap)}
        )
        return True

    @staticmethod
    def _after_one_home(mark, snap):
        """The one closed-gate home window (generation +2, opened after the
        closure) between the closure and now: its commands are the home
        counters, which only grew."""
        return (
            snap.generation == mark.generation + 2
            and snap.home_pose_released > mark.home_pose_released
            and snap.closed_from_ns >= mark.closed_from_ns
            and snap.home_only_from_ns is not None
            and snap.home_only_from_ns >= mark.closed_from_ns
        )

    @staticmethod
    def _home_sent_nothing(mark, snap):
        """The one closed-gate home window (generation +2) since the closure was
        refused before its first command (executed=no): by the bridge's own
        home-only counters, not one home POST passed the window."""
        return (
            snap.generation == mark.generation + 2
            and snap.home_pose_released == mark.home_pose_released
            and snap.home_gripper_released == mark.home_gripper_released
            and snap.closed_from_ns is not None
            and snap.closed_from_ns >= mark.closed_from_ns
            # a home-only window really opened after the closure (a generic
            # reopen then stop is also +2 with no home POST, and latches)
            and snap.home_only_from_ns is not None
            and snap.home_only_from_ns >= mark.closed_from_ns
        )

    def _observe_hold(self, episode, epoch, deadline):
        return self._run(
            "observe_hold",
            self._do_observe_hold,
            episode,
            epoch,
            deadline,
            deadline=deadline,
        )

    def _do_observe_hold(self, episode, epoch, deadline):
        if (
            type(epoch) is not int
            or type(deadline) is not int
            or self._episode_run(episode) != self.run_id
        ):
            raise InterventionUnproven(
                "hold needs this run's episode, epoch and deadline"
            )
        interval = self._interval
        observer = (
            None if interval is None else interval.witnesses.get((episode, epoch))
        )
        if observer is None:
            observer = self._new_witness(episode, epoch)
        proof = observer(episode, epoch, deadline)
        if not isinstance(proof, PhysicalHoldProof):
            raise InterventionUnproven(
                "the hold observer returned no PhysicalHoldProof"
            )
        return proof

    def _new_witness(self, episode, epoch):
        """A hold for a pause this binding has no observer for yet: a judge that
        runs after the closed-gate home (scene check) or before any pause (the
        first scene check). The gate is closed here if it is open (the safe
        direction); a closed gate must be the interval this intervention owns."""
        snap = self.robot.admission_snapshot()
        interval = self._interval
        if snap.mode == "open" and not snap.closed and interval is None:
            mark = self.robot.stop_admission()
            self._closed_mark(mark)
        elif (
            interval is not None
            and snap.mode == "closed"
            and snap.pose_released == interval.mark.pose_released
            and snap.gripper_released == interval.mark.gripper_released
            and (
                (snap.generation, snap.closed_from_ns)
                == (interval.mark.generation, interval.mark.closed_from_ns)
                or self._after_one_home(interval.mark, snap)
            )
        ):
            mark = snap
            self._closed_mark(mark)
        elif (
            interval is not None
            and snap.mode == "closed"
            and snap.pose_released == interval.mark.pose_released
            and snap.gripper_released == interval.mark.gripper_released
            and self._home_sent_nothing(interval.mark, snap)
        ):
            raise InterventionStopped(_HOME_NOT_EXECUTED)
        else:
            raise InterventionUnproven("no closure of this intervention to hold under")
        observer = self._observer_for(mark)
        if interval is None or mark is not interval.mark:
            self._interval = _Interval(mark, {})
        self._interval.witnesses[(episode, epoch)] = observer
        return observer

    def _observe_runtime(self):
        return self._run("observe_runtime", self._do_observe_runtime)

    def _do_observe_runtime(self):
        self._stopped()
        self.sole_client.prove()
        seen = self._bounded(
            "observe_runtime", self.runtime_probe, wait_s=self._wait_s()
        )
        runtime = self.runtime
        if not (
            isinstance(seen, RuntimeObservation)
            and seen.lease_id == runtime.lease_id
            and seen.lease_epoch == runtime.lease_epoch
            and seen.worker == runtime.worker
            and seen.clock_domain == self.clock.domain
            and type(seen.observed_ns) is int
            and seen.observed_ns <= self.clock.now()
        ):
            raise InterventionUnproven(
                "runtime observation is not of this owned runtime"
            )
        return seen

    def _role_window(self, role, episode, epoch, authorization, deadline):
        return self._run(
            "role_window",
            self._do_role_window,
            role,
            episode,
            epoch,
            authorization,
            deadline,
        )

    def _do_role_window(self, role, episode, epoch, authorization, deadline):
        self._stopped()
        if (
            role not in ("forward", "reset", "judge")
            or type(epoch) is not int
            or type(authorization) is not int
            or authorization < 1
            or type(deadline) is not int
            or self._episode_run(episode) != self.run_id
        ):
            raise InterventionUnproven(
                "role window needs a known role, epochs and a deadline"
            )
        self.sole_client.prove()
        snap = self.robot.admission_snapshot()
        closed = snap.mode == "closed" and snap.closed
        # A judge may run only while admission is closed; a policy role only while
        # it is open. The two never overlap.
        if (role == "judge") != closed or snap.mode == "home_only":
            raise InterventionUnproven("role does not match the admission gate")
        lease = self.authority.snapshot(now_ns=self.clock.now())["lease"]
        now = self.clock.now()
        if lease is None or not now < deadline <= lease.expires_ns:
            raise InterventionUnproven("window must end inside the runtime lease")
        return RoleWindow(
            self.broker.broker_id,
            self.run_id,
            episode,
            self.profile_sha256,
            now,
            deadline,
            self.clock.domain,
            role,
            epoch,
            authorization,
        )

    def _stop_proof(self, request, deadline):
        return self._run(
            "stop_proof", self._do_stop_proof, request, deadline, deadline=deadline
        )

    def _do_stop_proof(self, request, deadline):
        if type(deadline) is not int or not self.authority.owns_exact_request(request):
            raise InterventionUnproven(
                "stop proof for a request that is not in the slot"
            )
        self.sole_client.prove()
        answer = self._bounded(
            "stop_proof",
            self.stop_probe,
            request,
            deadline,
            wait_s=self._wait_s(deadline),
        )
        if (
            not isinstance(answer, tuple | list)
            or len(answer) != 2
            or answer[0] is not True
            or answer[1] is not True
        ):
            raise InterventionUnproven("backend stop / worker exit not proven")
        return RequestStopProof(
            request, self.clock.now(), self.clock.domain, True, True
        )

    def _prepare_evidence(self, request, observation):
        return self._run(
            "prepare_evidence", self._do_prepare_evidence, request, observation
        )

    def _do_prepare_evidence(self, request, observation):
        if self.frames.prepare_evidence(request, observation) is not True:
            raise InterventionUnproven("paused-frame evidence binding unproven")
        return True


# --- the offline report for ``fr3_run.py --check --hooks`` -----------------------------


def hooks_report(config, binding=None) -> dict:
    """Which hooks exist, what each is bound to, and what a run still needs.

    Reads only the job config and (when given) the parsed binding mapping. It
    connects to no robot, port or service, and does not enable anything: the real
    entry refuses ``event_pause_judge`` whatever this says.
    """
    term = config.termination
    mode = term.inference_mode
    binding = binding if isinstance(binding, dict) else None
    swap = binding.get("policy_swap") if binding else None
    declared = {
        "physical_hold": term.physical_hold is not None,
        "signal_events": bool(binding and binding.get("signal_events") is not None),
        "sole_client": isinstance(swap, dict) and bool(swap.get("services")),
    }
    names = {
        "stop_admission": ("Robot.stop_admission + Policy.quiesce", []),
        "resume_admission": ("Robot.reopen_admission_if (specific interval)", []),
        "observe_hold": (
            "MeasuredPhysicalHold + RobotAdmissionWitness",
            ["physical_hold"],
        ),
        "observe_runtime": ("RuntimeOwnership trusted observation", ["sole_client"]),
        "role_window": ("RuntimeOwnership RoleWindow", ["sole_client"]),
        "stop_proof": ("RuntimeOwnership RequestStopProof", ["sole_client"]),
        "prepare_evidence": (
            "PausedFrames over the signal_events ledger",
            ["signal_events"],
        ),
    }
    hooks = {}
    for name in HOOK_NAMES:
        bound, needs = names[name]
        hooks[name] = {
            "implemented": callable(getattr(Fr3InterventionHooks, "_" + name, None)),
            "bound_to": "levi.automatic.fr3_intervention.Fr3InterventionHooks._"
            + name
            + " -> "
            + bound,
            "needs": needs,
            "needs_declared": all(declared[n] for n in needs),
        }
    return {
        "schema": "levi.aeri.intervention_hooks.report.v1",
        "mode": mode,
        "real_entry": "refused" if mode == "event_pause_judge" else "not_applicable",
        "complete": all(h["implemented"] for h in hooks.values()),
        "connects_to_robot": False,
        "hooks": hooks,
        "physical_hold": {
            "bound_to": "adapters.physical_hold.MeasuredPhysicalHold + RobotAdmissionWitness",
            "declared": declared["physical_hold"],
        },
        "signal_events": {
            "bound_to": "adapters.signal_events.SignalEvents -> event_window.Ledger "
            "-> adapters.paused_frames.PausedFrames",
            "declared": declared["signal_events"],
        },
        "sole_client": {
            "bound_to": "SoleClientGuard (/proc) + SlotBoundPolicy (RuntimeOwnership slot)",
            "ready": declared["sole_client"],
            "needs": "a policy_swap unit on a private port; an externally started "
            "server cannot be proven private",
            "shared_ports_refused": sorted(SHARED_PORTS),
        },
    }
