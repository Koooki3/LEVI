"""CPU-only fake pipes/SDKs/proc. No process, HTTP, GPU or real flock probes."""

import contextlib
import json
import pickle
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from levi.live import owned_runtime as owned
from levi.live.owned_jax import JaxDependencyFence
from levi.live.owned_vllm import BARRIER_FILES, FrozenVllmFactory, serve_vllm
from levi.live.runtime_bridge import DriverIdentity
from levi.live.workload_broker import WorkerIdentity

WORKER = WorkerIdentity(101, 1, "fake")
FACTORY = owned.FrozenTorchFactory(
    "reviewed_factory", "build", "ab" * 32, "frozen", "cuda:0"
)


class Pipe:
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def poll(self, timeout):
        return bool(self.incoming)

    def recv_bytes(self, limit):
        value = pickle.dumps(self.incoming.pop(0))
        if len(value) > limit:
            raise OSError("message too long")
        return value

    def send_bytes(self, value):
        self.sent.append(pickle.loads(value))

    def close(self):
        self.closed = True


@pytest.fixture
def receipt(monkeypatch):
    worker = object.__new__(owned.OwnedModelWorker)
    worker.worker = WORKER
    worker.factory, worker.launch_id, worker.role = FACTORY, "launch", "judge"
    worker._lock, worker._closed = threading.Lock(), threading.Event()
    worker._pending, worker._idle = ("result", 1), None
    worker._connection = Pipe()
    worker._healthy = lambda: None  # explicit fake: never reads real /proc
    worker._authority = worker._observer = None
    return worker


def test_timeout_and_caller_completion_do_not_emit_idle(receipt):
    with pytest.raises(TimeoutError):
        receipt.collect(timeout_s=0.001)
    assert receipt._pending == ("result", 1)
    assert not receipt.idle()
    with pytest.raises(ValueError, match="unresolved"):
        receipt.barrier(2)
    assert receipt._connection.sent == []


@pytest.mark.parametrize(
    "change",
    [
        {"request_epoch": 2},
        {"worker": WorkerIdentity(102, 1, "fake")},
        {"launch_id": "external"},
        {"capability_id": "declared"},
    ],
)
def test_late_or_foreign_ack_cannot_clear_slot(receipt, change):
    ack = owned.SdkIdleAck(WORKER, "launch", 1, FACTORY.capability_id)
    receipt._connection.incoming = [("result", replace(ack, **change), "value")]
    with pytest.raises(ValueError, match="exact current"):
        receipt.collect(timeout_s=0.001)
    assert receipt._pending == ("result", 1)
    assert receipt._idle is None


def test_late_matching_sdk_ack_can_drain_without_new_epoch(receipt):
    with pytest.raises(TimeoutError):
        receipt.collect(timeout_s=0.001)
    ack = owned.SdkIdleAck(WORKER, "launch", 1, FACTORY.capability_id)
    receipt._connection.incoming = [("result", ack, "value")]
    assert receipt.collect(timeout_s=0.001) == "value"
    assert receipt.idle()


def test_bare_receipt_cannot_bypass_original_role_authority(receipt):
    receipt._pending = None
    receipt._idle = owned.SdkIdleAck(WORKER, "launch", 0, FACTORY.capability_id)
    with pytest.raises(ValueError, match="original role authority"):
        receipt.submit(SimpleNamespace(role="judge", request_epoch=1), {})
    assert receipt._connection.sent == []


def test_stop_with_busy_mutex_is_nonblocking_and_not_an_exit(receipt):
    receipt._lock.acquire()
    try:
        assert receipt.stop(1) is False
        assert receipt._closed.is_set()
        assert receipt._pending == ("result", 1)
    finally:
        receipt._lock.release()


def test_worker_waits_sdk_sync_before_ack(monkeypatch):
    events = []
    torch = SimpleNamespace(
        cuda=SimpleNamespace(synchronize=lambda device: events.append("sync")),
        inference_mode=contextlib.nullcontext,
    )
    factory = SimpleNamespace(
        load=lambda: (torch, "device", lambda value: events.append("infer") or value),
        capability_id="owned",
    )
    pipe = Pipe([("enrolled", "launch"), ("infer", 1, "cpu-result"), ("stop", 1, None)])
    monkeypatch.setattr(owned, "process_identity", lambda pid: WORKER)
    owned._serve(pipe, factory, "launch")
    assert events == ["sync", "infer", "sync", "sync"]
    assert [x[0] for x in pipe.sent] == ["ready", "result"]
    assert pipe.sent[-1][1] == owned.SdkIdleAck(WORKER, "launch", 1, "owned")


def test_sdk_sync_error_never_becomes_result_or_idle(monkeypatch):
    calls = []

    def sync(device):
        calls.append(device)
        if len(calls) == 2:
            raise RuntimeError("still-running-device")

    torch = SimpleNamespace(
        cuda=SimpleNamespace(synchronize=sync), inference_mode=contextlib.nullcontext
    )
    factory = SimpleNamespace(
        load=lambda: (torch, "device", lambda value: value), capability_id="owned"
    )
    pipe = Pipe([("enrolled", "launch"), ("infer", 1, "result")])
    monkeypatch.setattr(owned, "process_identity", lambda pid: WORKER)
    owned._serve(pipe, factory, "launch")
    assert [x[0] for x in pipe.sent] == ["ready", "error"]
    assert pipe.sent[-1][1] is None


def test_vllm_sdk_generate_exhaustion_precedes_device_pause_ack(monkeypatch):
    events = []

    class Engine:
        async def pause_generation(self, **kwargs):
            events.append("pause-device-sync")

        async def resume_generation(self):
            events.append("resume")

        async def generate(self, *args, **kwargs):
            events.append("generate")
            yield SimpleNamespace(outputs=[SimpleNamespace(text="yes")])
            events.append("generate-exited")

        def shutdown(self):
            events.append("shutdown")

    factory = SimpleNamespace(
        load=Engine,
        require_engine=lambda engine: None,
        close_engine=lambda engine: None,
        capability_id="owned-vllm",
    )
    pipe = Pipe(
        [
            ("enrolled", "launch"),
            ("infer", 2, {"prompt": "image", "sampling": {}}),
            ("stop", 2, None),
        ]
    )
    monkeypatch.setattr(
        sys.modules[serve_vllm.__module__], "process_identity", lambda pid: WORKER
    )
    monkeypatch.setitem(
        sys.modules, "vllm", SimpleNamespace(SamplingParams=lambda **kwargs: kwargs)
    )
    serve_vllm(pipe, factory, "launch")
    assert events == [
        "pause-device-sync",
        "resume",
        "generate",
        "generate-exited",
        "pause-device-sync",
        "pause-device-sync",
        "shutdown",
    ]
    assert pipe.sent[1][0] == "result"
    assert pipe.sent[1][1].request_epoch == 2


def test_incomplete_vllm_manifest_is_refused():
    with pytest.raises(ValueError, match="complete ordered"):
        FrozenVllmFactory(
            "reviewed_factory",
            "build",
            "ab" * 32,
            "frozen",
            "cuda:0",
            "version",
            ((BARRIER_FILES[0], "cd" * 32),),
        )


def test_driver_requires_registered_child_exact_token_and_inherited_flock(
    tmp_path, monkeypatch
):
    wrapper, supervisor = WorkerIdentity(100, 1, "fake"), WorkerIdentity(101, 2, "fake")
    token = DriverIdentity("lease", 1, "session", wrapper)
    row = {
        "id": "lease",
        "generation": 1,
        "session": "session",
        "state": "active",
        "resources": ["gpu"],
        "identity": {"pid": 100, "start_time": "1", "boot_id": "fake"},
        "child": {"pid": 101, "start_time": "2", "boot_id": "fake"},
        "launch_phase": "registered",
    }
    (tmp_path / "SCHEDULER.json").write_text(json.dumps({"schema": 1, "leases": [row]}))
    monkeypatch.setattr(owned, "process_alive", lambda worker: True)
    monkeypatch.setattr(
        owned, "_descends_from", lambda worker, ancestor: worker == ancestor
    )
    monkeypatch.setattr(owned, "_inherited_flock", lambda *args: True)
    monkeypatch.setattr(owned.gpumgr, "flock_holders", lambda path: [100])
    observer = owned.ResourceDriverObserver(
        tmp_path, token, "fake", supervisor, clock=lambda: 1
    )
    assert observer.require_held().exact_flock_held
    monkeypatch.setattr(owned, "_inherited_flock", lambda *args: False)
    with pytest.raises(ValueError, match="sole kernel flock"):
        observer.require_held()
    monkeypatch.setattr(owned, "_inherited_flock", lambda *args: True)
    row["child"]["start_time"] = "3"
    (tmp_path / "SCHEDULER.json").write_text(json.dumps({"schema": 1, "leases": [row]}))
    with pytest.raises(ValueError):
        observer.require_held()


def test_unknown_driver_child_does_not_look_active(tmp_path, monkeypatch):
    token = DriverIdentity("lease", 1, "session", WORKER)
    (tmp_path / "SCHEDULER.json").write_text(
        json.dumps({"schema": 1, "leases": [{"id": "lease"}]})
    )
    observer = owned.ResourceDriverObserver(tmp_path, token, "fake", WORKER)
    with pytest.raises((ValueError, TypeError)):
        observer.require_held()


def test_gpu_object_cannot_escape_worker_ipc():
    class GpuTensor:
        pass

    with pytest.raises(TypeError, match="CPU"):
        owned._cpu_payload({"result": GpuTensor()})


def test_jax_dependency_readiness_explicitly_cannot_emit_backend_idle():
    with pytest.raises(ValueError, match="closure is not frozen"):
        JaxDependencyFence(jax_version="x", jaxlib_version="y").backend_idle()


def test_complete_supervisor_containment_rejects_extra_process_and_subgroup(tmp_path):
    scope = object.__new__(owned.CgroupGroupScope)
    scope.path, scope.inode = tmp_path, tmp_path.stat().st_ino
    scope.supervisor, scope.scopes = WORKER, {}
    (tmp_path / "cgroup.procs").write_text("101")
    (tmp_path / "cgroup.events").write_text("populated 1\n")
    assert scope.members() == (101,)
    (tmp_path / "cgroup.procs").write_text("101\n999")
    with pytest.raises(ValueError, match="unowned process"):
        scope.members()
    (tmp_path / "cgroup.procs").write_text("101")
    (tmp_path / "foreign").mkdir()
    with pytest.raises(ValueError, match="unowned or missing"):
        scope.members()


def test_reservation_cannot_reuse_failed_or_live_role_receipt():
    scope = object.__new__(owned.CgroupGroupScope)
    scope.scopes, scope._launches, scope._lock = (
        {"judge": object()},
        {},
        threading.Lock(),
    )
    receipt = SimpleNamespace(_pending=("ready", 0), _idle=None)
    scope.reserve_launch("judge", receipt)
    with pytest.raises(ValueError, match="unresolved receipt"):
        scope.reserve_launch("judge", object())
    assert scope._launches["judge"] is receipt


def test_inherited_fdinfo_requires_exact_exclusive_flock_inode(tmp_path, monkeypatch):
    target = tmp_path / "gpu.lock"
    target.touch()
    stat = target.stat()
    lines = [
        f"lock: 1: FLOCK ADVISORY WRITE 100 {owned.os.major(stat.st_dev):x}:{owned.os.minor(stat.st_dev):x}:{stat.st_ino} 0 EOF"
    ]
    fake = SimpleNamespace(
        iterdir=lambda: [SimpleNamespace(read_text=lambda: lines[0])]
    )
    original_path = owned.Path
    monkeypatch.setattr(
        owned,
        "Path",
        lambda value: (
            fake if str(value) == "/proc/101/fdinfo" else original_path(value)
        ),
    )
    assert owned._inherited_flock(101, 100, target)
    lines[0] = lines[0].replace("WRITE 100", "WRITE 999")
    assert not owned._inherited_flock(101, 100, target)


def test_factory_subclass_cannot_manufacture_sdk_barrier():
    class Forged(owned.FrozenTorchFactory):
        def load(self):
            return "pretend"

    factory = Forged("fake", "build", "ab" * 32, "version", "cuda:0")
    with pytest.raises(TypeError, match="frozen factory"):
        owned.OwnedModelWorker.launch(
            factory,
            object(),
            object(),
            object(),
            object(),
            "judge",
            object(),
            "private",
        )


def test_factory_hash_mismatch_is_refused_before_import(tmp_path, monkeypatch):
    module = tmp_path / "reviewed_factory.py"
    module.write_text("raise RuntimeError('must-not-import-changed-source')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ValueError, match="differs from frozen source"):
        replace(FACTORY, source_root=str(tmp_path)).reviewed_factory()
    assert "reviewed_factory" not in sys.modules


def test_live_supervisor_cannot_escape_group_root(tmp_path, monkeypatch):
    scope = object.__new__(owned.CgroupGroupScope)
    scope.path, scope.supervisor = tmp_path, WORKER
    scope.members = lambda: (WORKER.pid,)
    (tmp_path / "cgroup.procs").write_text(str(WORKER.pid))
    monkeypatch.setattr(owned, "process_alive", lambda worker: True)
    assert scope.require_supervisor() == (WORKER.pid,)
    (tmp_path / "cgroup.procs").write_text("")
    with pytest.raises(ValueError, match="remain in the group root"):
        scope.require_supervisor()


def test_active_observation_refuses_unknown_previously_seen_descendant(
    receipt, monkeypatch
):
    receipt.scope = SimpleNamespace(members=lambda: (WORKER.pid,))
    receipt.group_scope = SimpleNamespace(require_supervisor=lambda: None)
    receipt.driver_observer = SimpleNamespace(require_held=lambda: None)
    receipt._known_members = {owned.WorkerIdentity(999, 2, "fake")}
    monkeypatch.setattr(owned, "process_identity", lambda pid: WORKER)
    monkeypatch.setattr(owned, "_descends_from", lambda worker, ancestor: True)
    monkeypatch.setattr(
        owned.gpumgr,
        "process_state",
        lambda pid, recorded: "alive" if pid == WORKER.pid else "unknown",
    )
    with pytest.raises(ValueError, match="identity is unknown"):
        owned.OwnedModelWorker._healthy(receipt)


FDINFO_PROBE = """
import fcntl, os, sys
from pathlib import Path
from levi.live import owned_runtime as owned
lock = Path(sys.argv[1]); lock.touch()
other = lock.with_name("other.lock"); other.touch()
# A descriptor above the one the fdinfo directory listing borrows, in a fresh
# process, so the result does not depend on pytest's own descriptor numbers.
fd = os.open(lock, os.O_RDWR); os.dup2(fd, 200); os.close(fd)
fcntl.flock(200, fcntl.LOCK_EX)
me = os.getpid()
print(owned._inherited_flock(me, me, lock),
      owned._inherited_flock(me, me + 1, lock),
      owned._inherited_flock(me, me, other))
"""


def test_inherited_flock_skips_fdinfo_entries_that_vanish(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", FDINFO_PROBE, str(tmp_path / "gpu.lock")],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.stdout.split() == ["True", "False", "False"], result.stderr
