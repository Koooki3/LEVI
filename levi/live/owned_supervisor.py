"""Host supervisor entry for owned model workers: `python -m levi.live.owned_supervisor`.

Started by the resource driver's run wrapper (scheduler.py `run`) as the lease
child, already placed in a delegated cgroup v2 root. It never creates cgroups,
units or locks. In order it:

1. verifies the launch evidence with `ResourceDriverObserver` (active lease,
   `launch_phase == "registered"`, this process descends from the registered
   child, the `.gpu.lock` flock is held by the driver and this process holds
   the inherited open-file description) and that `LEVI_GPU_LOCK_FD` is that lock;
2. builds `CgroupGroupScope` / one `CgroupScope` per declared role and checks
   the root holds only this process and every leaf is empty;
3. only then imports the caller's `setup` hook (the first point where a model
   library may be imported) and launches one worker per role;
4. holds the inherited flock and the cgroup root until every worker has been
   stopped and confirmed exited, and keeps checking them while it runs;
5. on SIGTERM/SIGINT, a lost lease, or an unexpected worker exit: stops every
   worker, confirms exit from kernel containment, writes `stop-receipt.json`,
   then exits.

A refusal logs `owned-supervisor: REFUSED [stage] reason` on stderr and exits
with a distinct non-zero code. No model is built in; the factory specification
comes from the `setup` hook. Physical-release proofs are still collected through
`OwnedGroupObserver.exit_proof` by a process other than this supervisor (see
`Supervisor.collect_exit_proof`): this supervisor cannot attest its own exit.
"""

import argparse
import importlib
import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from levi.domain import aeri
from levi.live import owned_runtime as owned
from levi.live.runtime_bridge import DriverIdentity
from levi.live.runtime_ownership import OwnedGroup, OwnedMember
from levi.live.workload_broker import WorkerIdentity

EX_OK = 0
EX_USAGE = 2
EX_EVIDENCE = 10  # launch evidence (lease / phase / flock / ancestry) refused
EX_CGROUP = 11  # delegated cgroup root or role leaves refused
EX_SETUP = 12  # injected setup hook missing/invalid
EX_LAUNCH = 13  # a worker could not be started
EX_WORKER_DIED = 14  # a worker exited while the supervisor was running
EX_STOP_UNCONFIRMED = 15  # a worker's exit could not be confirmed
EX_LEASE_LOST = 16  # the lease was lost/cancelled/released while running

_KEYS = {
    "scheduler_root",
    "clock_domain",
    "cgroup_root",
    "group_id",
    "state_dir",
    "roles",
    "setup",
    "poll_s",
    "stop_grace_s",
    "kill_wait_s",
    "launch_timeout_s",
}
_REQUIRED = _KEYS - {
    "setup",
    "poll_s",
    "stop_grace_s",
    "kill_wait_s",
    "launch_timeout_s",
}


class Refused(Exception):
    def __init__(self, code, stage, reason):
        super().__init__(reason)
        self.code, self.stage, self.reason = code, stage, reason


def _log(text):
    print("owned-supervisor: " + text, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class RoleConfig:
    role: str
    capability_id: str
    leaf: str


@dataclass(frozen=True)
class Config:
    scheduler_root: Path
    clock_domain: str
    cgroup_root: Path
    group_id: str
    state_dir: Path
    roles: tuple
    setup: str | None
    poll_s: float
    stop_grace_s: float
    kill_wait_s: float
    launch_timeout_s: float


def load_config(path):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError("config exceeds 64 KiB")
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) - _KEYS or _REQUIRED - set(data):
            raise ValueError("config keys differ from the supervisor contract")
        roles = tuple(RoleConfig(**r) for r in data["roles"])
        leaves = [r.leaf for r in roles]
        if any(
            not isinstance(v, str) or not v or "/" in v or v in (".", "..")
            for v in leaves
        ) or len(set(leaves)) != len(leaves):
            raise ValueError("distinct plain leaf names required")
        aeri.ResidentGroupSpec(
            group_id=data["group_id"],
            members=[{"role": r.role, "capability_id": r.capability_id} for r in roles],
        )
        numbers = {
            "poll_s": data.get("poll_s", 1.0),
            "stop_grace_s": data.get("stop_grace_s", 30.0),
            "kill_wait_s": data.get("kill_wait_s", 10.0),
            "launch_timeout_s": data.get("launch_timeout_s", 30.0),
        }
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 600
            for v in numbers.values()
        ):
            raise ValueError("bounded positive timings required")
        return Config(
            Path(data["scheduler_root"]),
            str(data["clock_domain"]),
            Path(data["cgroup_root"]),
            data["group_id"],
            Path(data["state_dir"]),
            roles,
            data.get("setup"),
            **{k: float(v) for k, v in numbers.items()},
        )
    except Refused:
        raise
    except Exception as exc:  # unreadable, malformed or contract-breaking config
        raise Refused(EX_USAGE, "config", f"{type(exc).__name__}: {exc}") from exc


def verify_launch_evidence(config, env):
    """Return (driver_observer, supervisor identity) or raise Refused(EX_EVIDENCE)."""

    def refuse(reason):
        raise Refused(EX_EVIDENCE, "evidence", reason)

    try:
        lease_id, session = env.get("LEVI_LEASE_ID"), env.get("LEVI_SESSION_ID")
        generation = env.get("LEVI_LEASE_GENERATION", "")
        if env.get("LEVI_GPU_LOCK_HELD") != "1":
            refuse("not started with a held GPU lock (LEVI_GPU_LOCK_HELD)")
        if not lease_id or not session or not generation.isdigit():
            refuse("lease environment (LEVI_LEASE_ID/GENERATION/SESSION_ID) missing")
        fd_text = env.get("LEVI_GPU_LOCK_FD", "")
        if not fd_text.isdigit():
            refuse("LEVI_GPU_LOCK_FD missing")
        lock = config.scheduler_root / ".gpu.lock"
        try:
            held, expected = os.fstat(int(fd_text)), lock.stat()
        except OSError as exc:
            refuse(f"inherited descriptor or GPU lock unreadable: {exc}")
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            refuse("LEVI_GPU_LOCK_FD is not the scheduler's GPU lock file")
        snapshot = json.loads((config.scheduler_root / "SCHEDULER.json").read_text())
        rows = [r for r in snapshot.get("leases", []) if r.get("id") == lease_id]
        if len(rows) != 1:
            refuse("driver lease missing or ambiguous in SCHEDULER.json")
        row = rows[0]
        if row.get("launch_phase") != "registered":
            refuse(f"launch_phase is {row.get('launch_phase')!r}, not 'registered'")
        owner = row.get("identity", {})
        driver = DriverIdentity(
            lease_id,
            int(generation),
            session,
            WorkerIdentity(
                owner.get("pid"), int(owner.get("start_time", -1)), owner.get("boot_id")
            ),
        )
        supervisor = owned.process_identity(os.getpid())
        if not owned._descends_from(supervisor, driver.owner):
            refuse("supervisor does not descend from the driver run wrapper")
        observer = owned.ResourceDriverObserver(
            config.scheduler_root, driver, config.clock_domain, supervisor
        )
        observer.require_held()
        return observer, supervisor
    except Refused:
        raise
    except Exception as exc:
        raise Refused(
            EX_EVIDENCE,
            "evidence",
            f"{exc}" if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}",
        ) from exc


def build_containment(config, supervisor):
    try:
        leaves = {
            r.role: owned.CgroupScope(config.cgroup_root / r.leaf) for r in config.roles
        }
        return owned.CgroupGroupScope(config.cgroup_root, supervisor, leaves), leaves
    except Exception as exc:
        raise Refused(EX_CGROUP, "cgroup", f"{type(exc).__name__}: {exc}") from exc


@dataclass
class SetupContext:
    """What the injected setup hook may use to build the launch plan."""

    config: Config
    roles: tuple
    state_dir: Path
    driver_observer: object
    group_scope: object
    supervisor: WorkerIdentity


@dataclass
class SupervisorPlan:
    """Returned by the setup hook: no model is built into this module.

    factories maps role -> Frozen*Factory for the declared capability; broker
    and lease are the original admitted WorkloadBroker/lease the launch checks;
    launcher defaults to OwnedModelWorker.launch; on_ready is called once with
    the Supervisor after every worker has its SDK ready ACK.
    """

    broker: object
    lease: object
    factories: dict
    state: Path
    launcher: object = None
    on_ready: object = None


def _atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".partial")
    temp.write_text(json.dumps(value, sort_keys=True))
    os.replace(temp, path)


class Supervisor:
    def __init__(self, config, observer, identity, group_scope, plan):
        self.config, self.driver_observer = config, observer
        self.identity, self.group_scope, self.plan = identity, group_scope, plan
        self.workers = {}
        self.stop_requested = threading.Event()
        self._signal_stop_requested = False
        self.reason = None

    def request_stop_from_signal(self, _number, _frame):
        # Python signal handlers may re-enter while Event.set/wait holds its
        # non-reentrant Condition lock. Only publish a lock-free latch here.
        self._signal_stop_requested = True

    def stop_is_requested(self):
        return self._signal_stop_requested or self.stop_requested.is_set()

    def _wait_for_stop(self):
        deadline = time.monotonic() + self.config.poll_s
        while not self.stop_is_requested():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # Preserve the monitor cadence while bounding signal-latch latency.
            # Thread callers of Event.set still wake this wait immediately.
            self.stop_requested.wait(min(0.05, remaining))
        return True

    def launch_all(self):
        launcher = self.plan.launcher or owned.OwnedModelWorker.launch
        for member in self.config.roles:
            factory = self.plan.factories.get(member.role)
            if getattr(factory, "capability_id", None) != member.capability_id:
                raise Refused(
                    EX_SETUP, "setup", f"factory for {member.role} differs from config"
                )
        for member in self.config.roles:
            if self.stop_is_requested():
                return
            try:
                self.workers[member.role] = launcher(
                    self.plan.factories[member.role],
                    self.group_scope.scopes[member.role],
                    self.driver_observer,
                    self.plan.broker,
                    self.plan.lease,
                    member.role,
                    self.group_scope,
                    self.plan.state,
                    timeout_s=self.config.launch_timeout_s,
                )
            except Exception as exc:
                receipt = getattr(exc, "receipt", None)
                if receipt is not None:
                    self.workers[member.role] = receipt  # retained, must be stopped
                raise Refused(
                    EX_LAUNCH,
                    "launch",
                    f"worker {member.role}: {type(exc).__name__}: {exc}",
                ) from exc

    def group(self):
        return OwnedGroup(
            self.config.group_id,
            self.identity,
            tuple(
                OwnedMember(r.role, self.workers[r.role].worker, r.capability_id)
                for r in self.config.roles
            ),
        )

    def group_observer(self):
        return owned.OwnedGroupObserver(
            self.group(),
            self.workers,
            self.driver_observer,
            self.config.clock_domain,
            self.group_scope,
        )

    def collect_exit_proof(self, broker, lease):
        """Delegate to OwnedGroupObserver.exit_proof.

        Only meaningful from a surviving trusted process after this supervisor
        has exited: the proof requires the original supervisor to be gone.
        """
        return self.group_observer().exit_proof(broker, lease)

    def monitor_once(self):
        """Return None while healthy, else (code, reason)."""
        try:
            self.driver_observer.require_held()
        except Exception as exc:  # noqa: BLE001 - fail closed
            return EX_LEASE_LOST, f"driver lease/flock no longer held: {exc}"
        try:
            self.group_scope.require_supervisor()
        except Exception as exc:  # noqa: BLE001 - fail closed
            return EX_LEASE_LOST, f"supervisor containment lost: {exc}"
        for role, worker in self.workers.items():
            state = owned.incarnation_state(worker.worker)
            if state != "alive":
                return EX_WORKER_DIED, f"worker {role} is {state}, not running"
        return None

    def run_until_stop(self):
        while not self._wait_for_stop():
            failure = self.monitor_once()
            if failure is not None:
                self.reason = failure
                return failure
        self.reason = (EX_OK, "stop requested")
        return self.reason

    def stop_all(self):
        """Stop every worker, escalate on the group, confirm exit. Returns role->bool."""
        sent, epoch = set(), 1
        deadline = time.monotonic() + self.config.stop_grace_s
        exited = {role: False for role in self.workers}
        while time.monotonic() < deadline:
            for role, worker in self.workers.items():
                if role not in sent:
                    try:
                        if worker.stop(epoch):
                            sent.add(role)
                    except Exception as exc:  # e.g. already dead: exit check decides  # noqa: BLE001 - fail closed
                        _log(f"stop of {role} not delivered ({exc}); checking exit")
                        sent.add(role)
                if not exited[role]:
                    exited[role] = self._exited(role, worker)
            if all(exited.values()):
                return exited
            time.sleep(min(0.05, self.config.poll_s))
        for role, worker in self.workers.items():
            if not exited[role]:
                self._kill_group(role, worker)
        deadline = time.monotonic() + self.config.kill_wait_s
        while time.monotonic() < deadline and not all(exited.values()):
            for role, worker in self.workers.items():
                if not exited[role]:
                    exited[role] = self._exited(role, worker)
            time.sleep(min(0.05, self.config.poll_s))
        return exited

    @staticmethod
    def _exited(role, worker):
        try:
            return worker.exited() is True
        except Exception as exc:  # noqa: BLE001 - fail closed
            _log(f"exit of {role} unproven: {exc}")
            return False

    @staticmethod
    def _kill_group(role, worker):
        try:
            identity = getattr(worker, "worker", None)
            if identity is None:
                _log(f"escalation for {role} refused: worker identity unavailable")
                return
            pid = identity.pid
            if (
                owned.incarnation_state(identity) == "alive"
                and os.getpgid(pid) == pid
                and pid != os.getpgrp()
            ):
                _log(f"worker {role} ignored stop; escalating SIGKILL to its group")
                os.killpg(pid, signal.SIGKILL)
        except Exception as exc:  # noqa: BLE001 - missing identity cannot authorize a signal
            _log(f"escalation for {role} failed: {exc}")

    def shutdown(self, code, reason):
        """Keep the inherited lock until fresh complete exit evidence is available."""
        self.stop_requested.set()
        try:
            self.stop_all()
        except Exception as exc:  # noqa: BLE001 - shutdown must retain the inherited lock
            _log(f"stop incomplete: {exc}; retaining lock until exit is proven")
        final = code
        while True:
            # Do not cache an earlier True: every receipt and the complete original
            # containment must agree at this observation, including unstarted leaves.
            exited = {
                role: self._exited(role, worker)
                for role, worker in self.workers.items()
            }
            containment_confirmed = False
            try:
                containment_confirmed = self.group_scope.require_supervisor() == (
                    self.identity.pid,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed
                _log(f"complete group exit unproven: {exc}")
            confirmed = all(exited.values()) and containment_confirmed
            if not confirmed:
                if final != EX_STOP_UNCONFIRMED:
                    _log("exit not confirmed; retaining supervisor and inherited lock")
                final = EX_STOP_UNCONFIRMED
            try:
                _atomic_json(
                    self.config.state_dir / "stop-receipt.json",
                    {
                        "group_id": self.config.group_id,
                        "supervisor": self.identity.__dict__,
                        "reason": reason,
                        "code": final,
                        "exited": exited,
                        "containment_confirmed": containment_confirmed,
                        "phase": "exit_confirmed" if confirmed else "holding_exit",
                        "observed_monotonic_ns": time.monotonic_ns(),
                    },
                )
            except OSError as exc:
                _log(f"stop receipt not written: {exc}")
            if confirmed:
                return final
            # A repeated SIGTERM/SIGINT or lost lease cannot shorten this wait.
            # No bare PGID escalation is safe after the original leader is gone.
            time.sleep(self.config.poll_s)


def _load_setup(spec):
    module, _, name = spec.partition(":")
    try:
        hook = getattr(importlib.import_module(module), name)
    except Exception as exc:
        raise Refused(
            EX_SETUP, "setup", f"{spec}: {type(exc).__name__}: {exc}"
        ) from exc
    if not callable(hook):
        raise Refused(EX_SETUP, "setup", f"{spec} is not callable")
    return hook


def run(config, env, setup=None):
    observer, identity = verify_launch_evidence(config, env)
    group_scope, _leaves = build_containment(config, identity)
    hook = setup
    if hook is None:
        if not config.setup:
            raise Refused(EX_SETUP, "setup", "no setup hook configured")
        hook = _load_setup(config.setup)
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        plan = hook(
            SetupContext(
                config, config.roles, config.state_dir, observer, group_scope, identity
            )
        )
    except Refused:
        raise
    except Exception as exc:
        raise Refused(EX_SETUP, "setup", f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(plan, SupervisorPlan):
        raise Refused(EX_SETUP, "setup", "setup must return a SupervisorPlan")
    supervisor = Supervisor(config, observer, identity, group_scope, plan)
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, supervisor.request_stop_from_signal)
    try:
        supervisor.launch_all()
    except Refused as exc:
        _log(f"REFUSED [{exc.stage}] {exc.reason}")
        return supervisor.shutdown(exc.code, exc.reason)
    if supervisor.stop_is_requested():
        return supervisor.shutdown(EX_OK, "stop requested during launch")
    try:
        supervisor.group_observer()  # complete exact membership before "ready"
        _atomic_json(
            config.state_dir / "ready.json",
            {
                "group_id": config.group_id,
                "supervisor": identity.__dict__,
                "workers": {
                    r: w.worker.__dict__ for r, w in supervisor.workers.items()
                },
            },
        )
        if plan.on_ready is not None:
            plan.on_ready(supervisor)
    except Exception as exc:  # noqa: BLE001 - fail closed
        _log(f"REFUSED [launch] group not complete: {type(exc).__name__}: {exc}")
        return supervisor.shutdown(EX_LAUNCH, str(exc))
    code, reason = supervisor.run_until_stop()
    _log(f"stopping workers: {reason}")
    return supervisor.shutdown(code, reason)


def main(argv=None, *, setup=None, env=None):
    parser = argparse.ArgumentParser(prog="python -m levi.live.owned_supervisor")
    parser.add_argument("--config", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EX_USAGE
    try:
        config = load_config(args.config)
        return run(config, os.environ if env is None else env, setup)
    except Refused as exc:
        _log(f"REFUSED [{exc.stage}] {exc.reason}")
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
