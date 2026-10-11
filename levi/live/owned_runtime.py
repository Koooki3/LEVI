"""Explicit owned, serial Torch SDK workers. Nothing is launched by default.

This is not a websocket/HTTP policy-server adapter. A delegated empty cgroup,
a frozen local model factory and an existing resource-driver GPU lease are
mandatory. Only the private parent pipe admits inference into each worker.
No PID supplied by a caller can become an owned model worker.
"""

import hashlib
import json
import multiprocessing
import os
import pickle
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from levi.live import gpumgr
from levi.live.runtime_bridge import DriverIdentity, DriverObservation
from levi.live.runtime_ownership import RequestToken
from levi.live.workload_broker import WorkerIdentity


def process_identity(pid):
    value = gpumgr.identity_strict(pid)
    if value is None:
        raise ValueError("owned process is not alive")
    return WorkerIdentity(pid, int(value["start_ticks"]), value["boot"])


def incarnation_state(worker):
    """Keep unknown distinct from a proven exit of this exact incarnation."""
    return gpumgr.process_state(
        worker.pid, {"start_ticks": str(worker.start_ticks), "boot": worker.boot_id}
    )


def process_alive(worker):
    return incarnation_state(worker) == "alive"


def _descends_from(worker, ancestor):
    if not process_alive(worker) or not process_alive(ancestor):
        return False
    pid, seen = worker.pid, set()
    while pid not in seen and pid > 1:
        if pid == ancestor.pid:
            return process_alive(worker) and process_alive(ancestor)
        seen.add(pid)
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        pid = int(fields[1])
    return False


def _inherited_flock(pid, owner_pid, path):
    """fdinfo identifies the actual inherited locked open-file description.

    Opening the same inode independently is insufficient: that descriptor has
    no matching FLOCK lock row. Nothing is opened for writing or lock-probed.
    """
    stat = path.stat()
    for info in Path(f"/proc/{pid}/fdinfo").iterdir():
        for line in info.read_text().splitlines():
            if not line.startswith("lock:"):
                continue
            values = line.split()
            if len(values) < 8 or values[2:5] != ["FLOCK", "ADVISORY", "WRITE"]:
                continue
            major, minor, inode = values[6].split(":")
            if (
                int(values[5]) == owner_pid
                and int(inode) == stat.st_ino
                and int(major, 16) == os.major(stat.st_dev)
                and int(minor, 16) == os.minor(stat.st_dev)
            ):
                return True
    return False


class ResourceDriverObserver:
    """Read the existing scheduler and exact kernel flock; never acquire either.

    root is explicitly supplied by the trusted launcher. The driver identity is
    the run wrapper holding the flock, not an arbitrary model-server PID. A
    missing/malformed registry or unreadable kernel evidence raises closed.
    """

    def __init__(
        self, root, driver, clock_domain, supervisor, *, clock=time.monotonic_ns
    ):
        if not isinstance(driver, DriverIdentity):
            raise TypeError("exact existing resource-driver token required")
        if not isinstance(supervisor, WorkerIdentity):
            raise TypeError("actual registered child supervisor required")
        self.root, self.driver = Path(root), driver
        self.supervisor = supervisor
        self.clock_domain, self.clock = clock_domain, clock

    def driver_observation(self):
        data = json.loads((self.root / "SCHEDULER.json").read_text())
        if data.get("schema") != 1 or not isinstance(data.get("leases"), list):
            raise ValueError("unsupported scheduler snapshot")
        rows = [r for r in data["leases"] if r.get("id") == self.driver.lease_id]
        if len(rows) != 1:
            raise ValueError("original driver lease missing or ambiguous")
        row = rows[0]
        identity = row.get("identity", {})
        exact = (
            type(row.get("generation")) is int
            and row["generation"] == self.driver.generation
            and row.get("session") == self.driver.session
            and identity.get("pid") == self.driver.owner.pid
            and str(identity.get("start_time")) == str(self.driver.owner.start_ticks)
            and identity.get("boot_id") == self.driver.owner.boot_id
            and process_alive(self.driver.owner)
        )
        child = row.get("child", {})
        child_worker = WorkerIdentity(
            child.get("pid"), int(child.get("start_time", -1)), child.get("boot_id")
        )
        exact = (
            exact
            and row.get("launch_phase") == "registered"
            and process_alive(child_worker)
        )
        exact = exact and _descends_from(self.supervisor, child_worker)
        holders = gpumgr.flock_holders(self.root / ".gpu.lock")
        inherited = _inherited_flock(
            self.supervisor.pid, self.driver.owner.pid, self.root / ".gpu.lock"
        )
        return DriverObservation(
            self.driver,
            self.clock(),
            self.clock_domain,
            exact and row.get("state") == "active",
            row.get("cancel_requested", False),
            exact
            and isinstance(row.get("resources"), list)
            and "gpu" in row["resources"],
            exact and set(holders or ()) == {self.driver.owner.pid} and inherited,
        )

    def require_held(self):
        observed = self.driver_observation()
        if observed.driver_cancelled is not False or any(
            value is not True
            for value in (
                observed.driver_active,
                observed.gpu_resource_held,
                observed.exact_flock_held,
            )
        ):
            raise ValueError(
                "exact active GPU driver and sole kernel flock holder required"
            )
        return observed


@dataclass(frozen=True)
class FrozenTorchFactory:
    """A reviewed factory module hash and installed SDK version, not idle flags.

    Factory returns one synchronous model callable. It must not launch hidden
    background computation, escape its cgroup or expose another ingress. This
    reviewed-code boundary is necessary; arbitrary SDKs are not supported.
    """

    module: str
    name: str
    code_sha256: str
    torch_version: str
    device: str
    source_root: str | None = field(default=None, kw_only=True)
    source_hashes: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)
    torch_source_hashes: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)

    def __post_init__(self):
        if (
            not re.fullmatch(r"[A-Za-z_][\w.]*", self.module)
            or not re.fullmatch(r"[A-Za-z_]\w*", self.name)
            or not re.fullmatch(r"[0-9a-f]{64}", self.code_sha256)
            or not self.torch_version
            or not re.fullmatch(r"cuda:\d+", self.device)
        ):
            raise ValueError(
                "frozen local Torch factory, version and explicit CUDA device required"
            )

    @property
    def capability_id(self):
        return "torch-owned:" + hashlib.sha256(repr(self).encode()).hexdigest()

    def reviewed_factory(self, *, allowed_bundles=(), load_transaction=None):
        from levi.live.owned_source import FrozenSourceBundle, SourceLoadTransaction

        if self.source_root is None:
            raise ValueError(
                "frozen factory source root and complete manifest required"
            )
        hashes = self.source_hashes or ((self.module, self.code_sha256),)
        if dict(hashes).get(self.module) != self.code_sha256:
            raise ValueError(
                "factory entrypoint must match its frozen closure manifest"
            )
        bundle = FrozenSourceBundle(
            self.source_root, hashes, allowed_bundles=allowed_bundles
        )
        transaction = load_transaction or SourceLoadTransaction()
        try:
            transaction.install(bundle)
            module = bundle.import_module(self.module)
            factory = getattr(module, self.name)
            if (
                not callable(factory)
                or getattr(factory, "__module__", None) != self.module
                or getattr(getattr(factory, "__code__", None), "co_filename", None)
                != bundle.sources[self.module][0]
            ):
                raise ValueError(
                    "factory entrypoint is outside its frozen execution module"
                )
            bundle.require_loaded()
            if load_transaction is None:
                factory._owned_source_lifetime = transaction.commit()
            return factory
        except BaseException:
            if load_transaction is None:
                transaction.close()
            raise

    def load(self):
        from levi.live.owned_source import (
            FrozenCriticalBindings,
            SourceLoadTransaction,
            frozen_distribution_bundle,
        )

        with SourceLoadTransaction() as transaction:
            bundle = transaction.install(
                frozen_distribution_bundle(
                    "torch",
                    "torch",
                    self.torch_version,
                    self.torch_source_hashes,
                )
            )
            torch = bundle.import_module("torch")
            critical = FrozenCriticalBindings((bundle,))
            factory = self.reviewed_factory(
                allowed_bundles=(bundle,), load_transaction=transaction
            )
            if (
                str(torch.__version__) != self.torch_version
                or not torch.cuda.is_available()
            ):
                raise ValueError("installed Torch/device differs from frozen SDK")
            bundle.require_function(torch.cuda.synchronize)
            device = torch.device(self.device)
            model = factory(device=device, sdk=torch)
            if not callable(model):
                raise TypeError("factory must return a synchronous model callable")
            critical.require()
            bundle.require_function(torch.cuda.synchronize)
            model._owned_sdk_critical = critical
            model._owned_source_lifetime = transaction.commit()
            return torch, device, model


class CgroupScope:
    """Dedicated pre-delegated cgroup v2 leaf; no creation, sudo or host setup.

    The trusted supervisor provisions one empty leaf per worker. No external
    process is adopted. Its inode and no-nested-cgroup membership stay fixed.
    Any additional process invalidates idle evidence, even when the caller
    thread has already returned. Missing delegation means launch is refused.
    """

    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        root = Path("/sys/fs/cgroup").resolve(strict=True)
        if self.path == root or not self.path.is_relative_to(root):
            raise ValueError("dedicated delegated cgroup v2 leaf required")
        if not (root / "cgroup.controllers").is_file():
            raise ValueError("cgroup v2 required")
        self.inode = self.path.stat().st_ino
        self.require_members(())

    def members(self):
        if self.path.stat().st_ino != self.inode or any(
            p.is_dir() for p in self.path.iterdir()
        ):
            raise ValueError("containment changed or has unowned subgroups")
        values = tuple(int(x) for x in (self.path / "cgroup.procs").read_text().split())
        events = dict(
            line.split()
            for line in (self.path / "cgroup.events").read_text().splitlines()
        )
        if events.get("populated") != ("1" if values else "0"):
            raise ValueError("inconsistent cgroup membership")
        return values

    def require_members(self, expected):
        if set(self.members()) != set(expected):
            raise ValueError("complete owned cgroup membership differs")

    def enroll(self, pid):
        self.require_members(())
        (self.path / "cgroup.procs").write_text(str(pid))
        self.require_members((pid,))


class CgroupGroupScope:
    """Already-delegated supervisor root plus every declared role leaf.

    Constructed by the actual executing registered supervisor, before model
    launch. Extra root processes/subgroups are refused. The caller must itself
    have been explicitly launched into this delegated root; this class never
    moves/adopts a running external process or creates a systemd unit.
    """

    def __init__(self, path, supervisor, role_scopes):
        self.path = Path(path).resolve(strict=True)
        self.supervisor, self.scopes = supervisor, dict(role_scopes)
        self._launches, self._lock = {}, threading.Lock()
        root = Path("/sys/fs/cgroup").resolve(strict=True)
        if (
            self.path == root
            or not self.path.is_relative_to(root)
            or not (root / "cgroup.controllers").is_file()
        ):
            raise ValueError("existing delegated cgroup v2 supervisor root required")
        if supervisor != process_identity(os.getpid()) or not self.scopes:
            raise ValueError(
                "actual executing supervisor and complete declared role leaves required"
            )
        location = "0::/" + str(self.path.relative_to(root))
        if (
            location
            not in Path(f"/proc/{supervisor.pid}/cgroup").read_text().splitlines()
        ):
            raise ValueError(
                "supervisor was not launched into the delegated group root"
            )
        if len({scope.path for scope in self.scopes.values()}) != len(self.scopes):
            raise ValueError("distinct role containment leaves required")
        if any(
            type(scope) is not CgroupScope or scope.path.parent != self.path
            for scope in self.scopes.values()
        ):
            raise ValueError("every owned role leaf must belong to original group root")
        self.inode = self.path.stat().st_ino
        if set(self.members()) != {supervisor.pid}:
            raise ValueError(
                "initial group root must contain only the actual supervisor"
            )

    def require_supervisor(self):
        members = self.members()
        roots = tuple(int(x) for x in (self.path / "cgroup.procs").read_text().split())
        if set(roots) != {self.supervisor.pid} or not process_alive(self.supervisor):
            raise ValueError("live original supervisor must remain in the group root")
        return members

    def reserve_launch(self, role, receipt):
        with self._lock:
            if role not in self.scopes or role in self._launches:
                raise ValueError(
                    "original role already has a launched or unresolved receipt"
                )
            if any(
                worker._pending is not None or worker._idle is None
                for worker in self._launches.values()
            ):
                raise ValueError(
                    "previous model startup must have a real SDK idle ACK before another starts"
                )
            self._launches[role] = receipt

    def members(self):
        if self.path.stat().st_ino != self.inode:
            raise ValueError("original supervisor containment replaced")
        directories = {p for p in self.path.iterdir() if p.is_dir()}
        if directories != {scope.path for scope in self.scopes.values()}:
            raise ValueError("unowned or missing role subgroup")
        roots = tuple(int(x) for x in (self.path / "cgroup.procs").read_text().split())
        if set(roots) - {self.supervisor.pid}:
            raise ValueError("unowned process at supervisor root")
        values = roots + tuple(
            pid for scope in self.scopes.values() for pid in scope.members()
        )
        events = dict(
            line.split()
            for line in (self.path / "cgroup.events").read_text().splitlines()
        )
        if events.get("populated") != ("1" if values else "0"):
            raise ValueError("inconsistent complete group containment")
        return values


@dataclass(frozen=True)
class SdkIdleAck:
    worker: WorkerIdentity
    launch_id: str
    request_epoch: int
    capability_id: str


@dataclass(frozen=True)
class SdkCompletion:
    request: RequestToken
    sdk_ack: SdkIdleAck
    result: object


MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def _cpu_payload(value):
    if value is None or type(value) in (str, bytes, bool, int, float):
        return
    if type(value) in (list, tuple):
        for item in value:
            _cpu_payload(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("CPU payload keys must be strings")
            _cpu_payload(item)
        return
    import numpy as np

    if type(value) is np.ndarray and not value.dtype.hasobject:
        return
    raise TypeError("only CPU scalars/containers/NumPy arrays may cross owned IPC")


def _send(connection, value):
    encoded = pickle.dumps(value, protocol=5)
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("owned IPC message exceeds frozen bound")
    connection.send_bytes(encoded)


def _receive(connection):
    # Private inherited pipe between trusted processes, never a network pickle.
    return pickle.loads(connection.recv_bytes(MAX_MESSAGE_BYTES))


def _serve(connection, factory, launch_id):
    """A gated child: model imports start only after supervisor containment.

    Device synchronization happens in the actual model process. ACKs are sent
    after inference exits and all Python request workers have ended. Transport
    completion and a parent thread joining never synthesize an SDK idle ACK.
    """
    model = None
    try:
        if _receive(connection) != ("enrolled", launch_id):
            return
        torch, device, model = factory.load()
        worker = process_identity(os.getpid())

        def idle(epoch):
            if type(factory) is FrozenTorchFactory:
                bundle = torch.__spec__.loader.bundle
                model._owned_sdk_critical.require()
                bundle.require_loaded()
                bundle.require_function(torch.cuda.synchronize)
            torch.cuda.synchronize(device)
            if type(factory) is FrozenTorchFactory:
                model._owned_sdk_critical.require()
                bundle.require_loaded()
                bundle.require_function(torch.cuda.synchronize)
            if any(
                t is not threading.current_thread() and t.is_alive()
                for t in threading.enumerate()
            ):
                raise ValueError("model still has local request threads")
            return SdkIdleAck(worker, launch_id, epoch, factory.capability_id)

        _send(connection, ("ready", idle(0), None))
        while True:
            operation, epoch, payload = _receive(connection)
            if operation == "stop":
                idle(epoch)
                return
            if operation == "barrier":
                _send(connection, ("idle", idle(epoch), None))
            elif operation == "infer":
                with torch.inference_mode():
                    result = model(payload)
                _cpu_payload(result)
                # Results must be CPU/pickle-safe; asynchronous output copies
                # are covered by the device synchronization before serialization.
                _send(connection, ("result", idle(epoch), result))
            else:
                raise ValueError("unknown owned worker command")
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:  # noqa: BLE001 - unknown backend never emits idle
        try:
            _send(connection, ("error", None, type(exc).__name__))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        try:
            if type(factory) is FrozenTorchFactory and model is not None:
                model._owned_source_lifetime.close()
        finally:
            connection.close()


class OwnedModelWorker:
    """Owned launch receipt plus exclusive request pipe; no attach/adopt method.

    A timeout keeps the pending command and invalidates idle. The same epoch
    may later be collected; a new command cannot overwrite unresolved work.
    close_ingress is nonblocking and never purports to stop model computation.
    """

    def __init__(self):
        raise TypeError("use explicit owned launch after original broker admission")

    @classmethod
    def launch(
        cls,
        factory,
        scope,
        driver_observer,
        broker,
        lease,
        role,
        group_scope,
        state,
        *,
        timeout_s=1.0,
    ):
        from levi.live.owned_vllm import FrozenVllmFactory

        if not 0 < timeout_s <= 30:
            raise ValueError("bounded owned startup wait required")
        if (
            type(factory) not in (FrozenTorchFactory, FrozenVllmFactory)
            or type(scope) is not CgroupScope
            or type(driver_observer) is not ResourceDriverObserver
        ):
            raise TypeError("frozen factory and delegated owned containment required")
        driver_observer.require_held()
        if (
            type(group_scope) is not CgroupGroupScope
            or group_scope.supervisor != driver_observer.supervisor
            or group_scope.scopes.get(role) is not scope
            or process_identity(os.getpid()) != driver_observer.supervisor
        ):
            raise ValueError(
                "actual registered supervisor and original complete group containment required"
            )
        group_scope.require_supervisor()
        with broker._lock:
            active = broker._token(lease)
            declaration = active.request.spec.resident_group
            if active.owned_group is not None:
                raise ValueError(
                    "bound original group cannot launch replacement members"
                )
            if (
                not broker.accepts_result(lease, now_ns=driver_observer.clock())
                or declaration is None
                or not any(
                    m.role == role and m.capability_id == factory.capability_id
                    for m in declaration.members
                )
            ):
                raise ValueError(
                    "original admitted resident member declaration required"
                )
        scope.require_members(())
        self = object.__new__(cls)
        self.factory, self.scope, self.driver_observer = factory, scope, driver_observer
        self.role, self.group_scope, self.state = role, group_scope, Path(state)
        self._authority = self._observer = None
        self.launch_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._idle = None
        self._pending = ("ready", 0)
        self._submitted_request = self._completed_request = None
        self._received_reply = self._sdk_completion = None
        self._reply_withdrawn = False
        self.worker = self._process = None
        group_scope.reserve_launch(role, self)
        self._connection, child = multiprocessing.Pipe()
        # A fresh module entrypoint avoids multiprocessing.spawn importing an
        # operator's __main__ (which could initialize a model before the gate).
        try:
            self._process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "levi.live.owned_worker",
                    str(child.fileno()),
                ],
                pass_fds=(child.fileno(),),
                start_new_session=True,
            )
        except Exception as exc:
            self._connection.close()
            self._closed.set()
            raise OwnedLaunchError(self) from exc
        finally:
            child.close()
        self.worker = None
        self._known_members = set()
        # Remember the actual child before enrollment can fail. Do not clear an
        # unresolved launch or release the broker on an enrollment/ready error.
        try:
            self.worker = process_identity(self._process.pid)
            from levi import children

            record = children.track(
                self._process, "owned-model", role, state=self.state
            )
            if (
                record is None
                or record.get("identity", {}).get("boot") != self.worker.boot_id
                or str(record.get("identity", {}).get("start_ticks"))
                != str(self.worker.start_ticks)
            ):
                raise ValueError("owned process registration failed")
            scope.enroll(self.worker.pid)
            _send(self._connection, ("bootstrap", factory, self.launch_id))
            _send(self._connection, ("enrolled", self.launch_id))
            self.collect(timeout_s=timeout_s)
        except Exception as exc:
            self._closed.set()
            raise OwnedLaunchError(self) from exc
        return self

    def _healthy(self):
        self.driver_observer.require_held()
        if not process_alive(self.worker):
            raise ValueError("original model incarnation is not alive")
        self.group_scope.require_supervisor()
        members = self.scope.members()
        if self.worker.pid not in members:
            raise ValueError("owned leader escaped containment")
        # vLLM has resident engine descendants. All must belong to this actual
        # launched leader; an arbitrary PID in a shared cgroup is not adopted.
        identities = tuple(process_identity(pid) for pid in members)
        for identity in identities:
            if not _descends_from(identity, self.worker):
                raise ValueError("unowned engine process in containment")
        if set(self.scope.members()) != set(members):
            raise ValueError("model worker inventory changed during probe")
        self._known_members.update(identities)
        for identity in self._known_members:
            state = incarnation_state(identity)
            if state == "unknown":
                raise ValueError("known model descendant identity is unknown")
            if state == "alive" and identity.pid not in members:
                raise ValueError("known model descendant escaped containment")

    def idle(self):
        with self._lock:
            self._healthy()
            return self._idle is not None and self._pending is None

    def bind_authority(self, authority, observer):
        from levi.live.runtime_ownership import RuntimeOwnership

        if (
            not isinstance(authority, RuntimeOwnership)
            or observer.workers.get(self.role) is not self
        ):
            raise ValueError("original group request authority required")
        with self._lock:
            if self._authority is not None and (
                self._authority is not authority or self._observer is not observer
            ):
                raise ValueError("owned worker cannot migrate to another authority")
            self._authority, self._observer = authority, observer

    def submit(self, request, payload):
        # Verify against the original authority before locking the worker: the
        # group observer probes every member, including this one's idle lock.
        from levi.live.runtime_ownership import RequestToken

        if (
            type(request) is not RequestToken
            or self._authority is None
            or request.role != self.role
            or not self._authority.accepts_result(
                request,
                self._observer.observe(self._authority._lease),
                now_ns=self.driver_observer.clock(),
            )
        ):
            raise ValueError("exact active original role authority required")
        _cpu_payload(payload)
        with self._lock:
            self._healthy()
            if self._closed.is_set() or self._pending is not None or self._idle is None:
                raise ValueError("owned worker closed, busy or idle unproven")
            if (
                request.role is None
                or request.request_epoch <= self._idle.request_epoch
            ):
                raise ValueError("current unique role request epoch required")
            self._idle = None
            self._submitted_request = request
            self._completed_request = self._sdk_completion = self._received_reply = None
            self._reply_withdrawn = False
            self._pending = ("result", request.request_epoch)
            _send(self._connection, ("infer", request.request_epoch, payload))

    def barrier(self, epoch):
        with self._lock:
            self._healthy()
            if self._pending is not None:
                raise ValueError("unresolved request cannot be replaced by barrier")
            self._idle = None
            self._pending = ("idle", epoch)
            _send(self._connection, ("barrier", epoch, None))

    def _collect_sdk(self, timeout_s, request=None):
        if not 0 < timeout_s <= 30:
            raise ValueError("bounded worker wait required")
        with self._lock:
            if (
                request is not None
                and request is not getattr(self, "_submitted_request", None)
                and request is not getattr(self, "_completed_request", None)
            ):
                raise ValueError("exact submitted SDK request required")
            try:
                self._healthy()
                if request is not None:
                    if request is getattr(self, "_completed_request", None):
                        completion = self._sdk_completion
                        return SdkCompletion(
                            request,
                            completion.sdk_ack,
                            None
                            if self._closed.is_set() or self._reply_withdrawn
                            else completion.result,
                        )
                    if request is not getattr(self, "_submitted_request", None):
                        raise ValueError("exact submitted SDK request required")
                if self._pending is None:
                    raise ValueError("no pending owned command")
                expected, epoch = self._pending
                reply = getattr(self, "_received_reply", None)
                if reply is None:
                    if not self._connection.poll(timeout_s):
                        raise TimeoutError(
                            "owned SDK idle ACK not received; slot retained"
                        )
                    kind, ack, result = _receive(self._connection)
                    if (
                        kind != expected
                        or type(ack) is not SdkIdleAck
                        or type(ack.request_epoch) is not int
                        or type(ack.worker) is not WorkerIdentity
                        or ack
                        != SdkIdleAck(
                            self.worker,
                            self.launch_id,
                            epoch,
                            self.factory.capability_id,
                        )
                    ):
                        raise ValueError("exact current owned SDK idle ACK required")
                    _cpu_payload(result)
                    # Cache a genuine reply before the post-ACK inventory probe.
                    # An unknown probe may recover only this same request drain.
                    self._received_reply = reply = (ack, result)
                ack, result = reply
                self._healthy()
                submitted = getattr(self, "_submitted_request", None)
                result = (
                    None
                    if self._closed.is_set() or getattr(self, "_reply_withdrawn", False)
                    else result
                )
                self._pending = None
                self._idle = ack
                self._received_reply = None
                self._submitted_request = None
                self._completed_request = submitted
                self._sdk_completion = (
                    SdkCompletion(submitted, ack, result)
                    if submitted is not None
                    else None
                )
                return self._sdk_completion if request is not None else result
            except TimeoutError:
                raise
            except Exception:
                self._reply_withdrawn = True
                self._closed.set()
                raise

    def collect(self, *, timeout_s=1.0):
        return self._collect_sdk(timeout_s)

    def collect_sdk_completion(self, request, *, timeout_s=1.0):
        from levi.live.runtime_ownership import RequestToken

        if type(request) is not RequestToken:
            raise ValueError("exact submitted SDK request required")
        return self._collect_sdk(timeout_s, request)

    def close_ingress(self):
        self._closed.set()

    def stop(self, epoch):
        self.close_ingress()
        # Never wait for the collection/model mutex from a stop caller.
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self._healthy()
            if self._pending is not None:
                return False
            self._idle = None
            self._pending = ("stop", epoch)
            _send(self._connection, ("stop", epoch, None))
            return True
        finally:
            self._lock.release()

    def exited(self):
        # Empty kernel containment, not parent poll/client done/free VRAM, is
        # the complete process exit and CUDA-context destruction condition.
        state = gpumgr.process_state(
            self.worker.pid,
            {"start_ticks": str(self.worker.start_ticks), "boot": self.worker.boot_id},
        )
        return (
            state == "gone"
            and not self.scope.members()
            and all(incarnation_state(m) == "gone" for m in self._known_members)
        )


class OwnedLaunchError(RuntimeError):
    def __init__(self, receipt):
        super().__init__(
            "owned launch unresolved; original receipt and physical lease retained"
        )
        self.receipt = receipt


class OwnedGroupObserver:
    """Fresh exact evidence from only workers this supervisor actually launched."""

    def __init__(
        self,
        group,
        workers,
        driver_observer,
        clock_domain,
        group_scope,
        *,
        clock=time.monotonic_ns,
    ):
        from levi.live.runtime_ownership import OwnedGroup

        if (
            not isinstance(group, OwnedGroup)
            or group.supervisor != driver_observer.supervisor
        ):
            raise ValueError("original supervisor-owned group required")
        if (
            type(group_scope) is not CgroupGroupScope
            or group_scope.supervisor != group.supervisor
        ):
            raise ValueError("exact complete supervisor containment required")
        self.group_scope = group_scope
        self.group, self.workers = group, dict(workers)
        self.driver_observer, self.clock_domain, self.clock = (
            driver_observer,
            clock_domain,
            clock,
        )
        if set(self.workers) != {m.role for m in group.members}:
            raise ValueError("complete owned role worker set required")
        for member in group.members:
            worker = self.workers[member.role]
            if (
                not isinstance(worker, OwnedModelWorker)
                or worker.worker != member.worker
                or worker.factory.capability_id != member.capability_id
                or worker.driver_observer is not driver_observer
                or worker.group_scope is not group_scope
                or group_scope._launches.get(member.role) is not worker
            ):
                raise ValueError("member must be an exact owned launch receipt")

    def observe(self, lease):
        from levi.live.runtime_ownership import (
            GroupObservation,
            MemberObservation,
            RuntimeObservation,
        )

        driver = self.driver_observer.require_held()
        self.group_scope.require_supervisor()
        members = []
        for member in self.group.members:
            worker = self.workers[member.role]
            idle = worker.idle()
            members.append(MemberObservation(member, True, True, idle, idle))
        # Recheck the driver after all member probes. No external idle claims
        # enter this observation; every worker has an exact private-pipe ACK.
        self.driver_observer.require_held()
        self.group_scope.require_supervisor()
        if not process_alive(self.group.supervisor):
            raise ValueError("original supervisor is gone or unknown")
        idle = all(m.backend_idle is True for m in members)
        return RuntimeObservation(
            lease.lease_id,
            lease.lease_epoch,
            self.group.supervisor,
            self.clock(),
            self.clock_domain,
            True,
            True,
            idle,
            idle,
            GroupObservation(
                self.group,
                tuple(members),
                driver.driver.lease_id,
                driver.driver.generation,
                driver.driver.session,
                True,
                True,
                True,
            ),
        )

    def exit_proof(self, broker, lease):
        from levi.live.runtime_ownership import GroupExitProof, MemberExitProof

        exits = []
        for member in self.group.members:
            exited = self.workers[member.role].exited()
            exits.append(MemberExitProof(member, exited, exited, exited))
        exits = tuple(exits)
        state = gpumgr.process_state(
            self.group.supervisor.pid,
            {
                "start_ticks": str(self.group.supervisor.start_ticks),
                "boot": self.group.supervisor.boot_id,
            },
        )
        all_exited = (
            state == "gone"
            and not self.group_scope.members()
            and all(m.process_exited is True for m in exits)
        )
        # This can be collected by a surviving trusted recovery supervisor;
        # it does not require resurrecting the original driver's permissions.
        return GroupExitProof(
            broker.broker_id,
            lease.lease_id,
            lease.lease_epoch,
            self.group.supervisor,
            self.clock(),
            self.clock_domain,
            state == "gone",
            all_exited,
            all_exited,
            self.group,
            exits,
            True,
        )
