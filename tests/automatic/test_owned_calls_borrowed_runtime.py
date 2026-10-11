"""Fixed owned classes + original Kernel + actual Orchestrator; hardware is fake.

A fresh interpreter extracts explicit fixed peer blobs into immutable private
evidence artifacts before loading. It never executes a moving peer worktree. No SDK, process, GPU, physical driver, cgroup or IPC is started.
"""

import hashlib
import importlib
import inspect
import json
import marshal
import os
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_event_pause_orchestrator_runtime as kernel_fixture
import test_owned_calls_orchestrator as local
from aeri_harness import config, fake, rig

from levi.automatic.adapters import events as ev
from levi.automatic.intervention import (
    CompletionEvidence,
    InferenceCycle,
    InterventionHooks,
    PhysicalHoldProof,
    hold_contract_sha256,
)
from levi.automatic.orchestrator import Orchestrator
from levi.automatic.recorder import EvidenceStore
from levi.automatic.termination import TerminationConfig

PEER = Path(os.environ["LEVI_BORROWED_ROOT"]).resolve()
FIXED = os.environ["LEVI_BORROWED_COMMIT"]
if not re.fullmatch(r"[0-9a-f]{40}", FIXED):
    raise RuntimeError("an explicit full fixed peer commit is required")
FILES = (
    "levi/live/owned_runtime.py",
    "levi/automatic/owned_runtime.py",
    "tests/automatic/test_owned_role_execution.py",
)
SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / (".state/tmp/intervention-evidence-20261011/fixed-borrowed")
    / FIXED
)
hashes = {}
for name in FILES:
    fixed = subprocess.check_output(
        ["git", "show", FIXED + ":" + name], cwd=PEER, timeout=1
    )
    artifact = SNAPSHOT / name
    if artifact.exists():
        if artifact.read_bytes() != fixed:
            raise RuntimeError("fixed source artifact changed: " + name)
    else:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(fixed)
    hashes[name] = hashlib.sha256(fixed).hexdigest()
print(
    json.dumps(
        {
            "fixed_borrowed_commit": FIXED,
            "source_sha256": hashes,
            "fixed_source_artifacts": str(SNAPSHOT),
        },
        sort_keys=True,
    )
)
ROOT = Path(os.environ["LEVI_JOINT_KERNEL_ROOT"])
kernel_fixture.source_module(
    "levi.live.runtime_bridge", ROOT / "levi/live/runtime_bridge.py"
)
sdk = kernel_fixture.source_module(
    "levi.live.owned_runtime", SNAPSHOT / "levi/live/owned_runtime.py"
)
borrowed = kernel_fixture.source_module(
    "levi.automatic.owned_runtime", SNAPSHOT / "levi/automatic/owned_runtime.py"
)
sys.path.insert(0, str(SNAPSHOT / "tests/automatic"))
helpers = importlib.import_module("test_owned_role_execution")


def fixed_loaded_source(module, repository, commit, relative):
    """Prove loaded bytes against git objects; never copy a kernel or grant."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("an explicit full source commit is required")
    path = Path(module.__file__).resolve()
    expected = subprocess.check_output(
        ["git", "show", commit + ":" + relative], cwd=repository, timeout=1
    )
    actual = path.read_bytes()
    if actual != expected:
        raise RuntimeError("loaded source differs from fixed commit: " + str(path))
    return {
        "module": module.__name__,
        "loaded_path": str(path),
        "commit": commit,
        "git_path": relative,
        "source_sha256": hashlib.sha256(actual).hexdigest(),
    }


def loaded_provenance():
    kernel_commit = os.environ["LEVI_JOINT_KERNEL_COMMIT"]
    implementation_commit = os.environ["LEVI_JOINT_IMPLEMENTATION_COMMIT"]
    implementation = Path(__file__).resolve().parents[2]
    schema, broker, runtime = kernel_fixture.KERNEL
    modules = (
        (schema, ROOT, kernel_commit, "levi/domain/aeri.py"),
        (broker, ROOT, kernel_commit, "levi/live/workload_broker.py"),
        (runtime, ROOT, kernel_commit, "levi/live/runtime_ownership.py"),
        (
            sys.modules["levi.live.runtime_bridge"],
            ROOT,
            kernel_commit,
            "levi/live/runtime_bridge.py",
        ),
        (
            sys.modules[InferenceCycle.__module__],
            implementation,
            implementation_commit,
            "levi/automatic/intervention.py",
        ),
        (
            sys.modules[Orchestrator.__module__],
            implementation,
            implementation_commit,
            "levi/automatic/orchestrator.py",
        ),
        (
            sys.modules["levi.automatic.adapters.owned_calls"],
            implementation,
            implementation_commit,
            "levi/automatic/adapters/owned_calls.py",
        ),
        (sdk, PEER, FIXED, "levi/live/owned_runtime.py"),
        (borrowed, PEER, FIXED, "levi/automatic/owned_runtime.py"),
    )
    functions = (
        runtime.RuntimeOwnership.begin_role_request,
        runtime.RuntimeOwnership.finish_request,
        runtime.RuntimeOwnership.owns_exact_request,
        runtime.RuntimeOwnership.accepts_exact_request,
        InferenceCycle.invoke,
        Orchestrator._drive_intervention,
        borrowed.BorrowedRoleExecution.dispatch_existing,
        borrowed.BorrowedRoleExecution.collect_existing,
    )
    return {
        "loaded_sources": [fixed_loaded_source(*entry) for entry in modules],
        "loaded_functions": [
            {
                "qualified_name": function.__module__ + "." + function.__qualname__,
                "source_path": inspect.getsourcefile(function),
                "first_line": function.__code__.co_firstlineno,
                "code_sha256": hashlib.sha256(
                    marshal.dumps(function.__code__)
                ).hexdigest(),
            }
            for function in functions
        ],
        "hardware_and_sdk": "explicit in-memory fakes; production bindings remain closed",
    }


print(json.dumps(loaded_provenance(), sort_keys=True))


class AutoPipe(helpers.Pipe):
    """One in-memory SDK ACK source; original worker validates all fields."""

    def __init__(self, worker, r, state):
        super().__init__(worker)
        self.r, self.state = r, state

    def send_bytes(self, value):
        super().send_bytes(value)
        operation, _epoch, payload = self.sent[-1]
        if operation != "infer":
            raise AssertionError("only explicit borrowed inference expected")
        packet = json.loads(payload)
        kind = packet["kind"]
        if kind in ("goal", "scene"):
            decisions = self.state[kind]
            default = "confirmed" if kind == "goal" else "ready"
            result = {"decision": decisions.pop(0) if decisions else default}
        else:
            result = {"actions": [[0.0] * self.r.config.action_dims for _ in range(16)]}
        self.finish(result)
        if self.state.get("bad_ack"):
            op, ack, result = self.answer[-1]
            self.answer[-1] = (
                op,
                replace(ack, request_epoch=ack.request_epoch + 1),
                result,
            )
        if self.state.get("stop"):
            self.r.orch.stop(
                "stop-during-original-borrowed",
                expected_seq=self.r.orch.journal.next_seq,
            )


@unittest.skipUnless(
    kernel_fixture.KERNEL is not None, "requires frozen original kernel"
)
class ActualBorrowedTests(unittest.TestCase):
    mount = local.OwnedCallsTests.mount

    def make(self, *, reset=False, bad_ack=False, stop=False):
        patch = pytest.MonkeyPatch()
        self.addCleanup(patch.undo)
        domain, _broker_module, rt = kernel_fixture.KERNEL
        patch.setattr(helpers, "aeri", domain)
        runner, workers, _deadline = helpers.execution.__wrapped__(patch)
        now = helpers.NOW
        # Renew through the original idle kernel to cover this fake rollout.
        renewed_deadline = domain.Deadline(
            due_ns=now + 300_000_000_000,
            clock_domain=helpers.CLOCK,
            budget_ms=300_000,
            hardness="hard",
        )
        runner.renew(
            renewed_deadline,
            runner._test_pressure,
            replace(runner._test_window, expires_ns=renewed_deadline.due_ns),
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        cfg = config(
            run_id="run",
            episodes=1,
            reset_strategy="single_reset_policy" if reset else "human_assisted",
            termination=TerminationConfig(
                inference_mode="event_pause_judge",
                physical_hold=kernel_fixture.HOLD,
                min_steps=5,
                settle_steps=3,
                cooldown_steps=5,
            ),
        )
        r = rig(
            Path(temporary.name) / "run",
            cfg=cfg,
            create=False,
            clock=fake.FakeClock(now, helpers.CLOCK),
        )
        r.events = ev.FakeEventStream(
            {
                "forward": [{"step": 8, "event_type": "object_settled"}],
                "reset": [{"step": 4, "event_type": "object_settled"}],
            },
            r.clock,
            cfg.run_id,
        )
        r.owner, r.lease = runner.authority, runner.lease
        runner.observer.clock = r.clock.now
        runner.observer.driver_observer.clock = r.clock.now
        # The in-memory driver's timestamp follows the same explicit fake clock.
        original_driver = runner.observer.driver_observer.require_held
        runner.observer.driver_observer.require_held = lambda: (
            patch.setattr(helpers, "NOW", r.clock.now()) or original_driver()
        )
        state = {
            "scene": ["reset_required", "ready"] if reset else ["ready"],
            "goal": ["confirmed"] if reset else ["rejected", "confirmed"],
            "bad_ack": bad_ack,
            "stop": stop,
        }
        for worker in workers.values():
            worker._connection = AutoPipe(worker, r, state)
        executor = borrowed.BorrowedRoleExecution(
            runner.authority,
            runner.runtime,
            runner.lease,
            runner.observer,
            clock=r.clock.now,
        )
        self.finished, self.begin_attempts = [], []
        grants = []
        begin, finish = r.owner.begin_role_request, r.owner.finish_request

        def begun(*args, **kwargs):
            self.begin_attempts.append((args, kwargs))
            request = begin(*args, **kwargs)
            grants.append(request)
            return request

        def finished(request, proof, observation, **kwargs):
            self.finished.append((request, proof))
            return finish(request, proof, observation, **kwargs)

        patch.setattr(r.owner, "begin_role_request", begun)
        patch.setattr(r.owner, "finish_request", finished)
        self.fixture = SimpleNamespace(grants=grants)

        def observe_runtime():
            patch.setattr(helpers, "NOW", r.clock.now())
            return runner.observer.observe(runner.lease)

        def window(role, episode, epoch, authorization, deadline_ns):
            return rt.RoleWindow(
                r.owner.broker.broker_id,
                cfg.run_id,
                episode,
                helpers.PROFILE,
                r.clock.now(),
                deadline_ns,
                r.clock.domain,
                role,
                epoch,
                authorization,
            )

        def physical(episode, epoch, deadline_ns):
            start = r.clock.now()
            r.clock.advance(kernel_fixture.HOLD.stable_window_ns)
            return PhysicalHoldProof(
                cfg.run_id,
                episode,
                epoch,
                cfg.plan_sha256,
                hold_contract_sha256(kernel_fixture.HOLD),
                r.clock.domain,
                start,
                r.clock.now(),
                deadline_ns,
                True,
                True,
                True,
            )

        def stop_admission(episode, old, new):
            r.policy.quiesce(policy_epoch=old)
            return True

        def unused_stop(*args):
            self.fail("explicit completion path must not use old stop hook")

        r.cycle = InferenceCycle(
            authority=r.owner,
            runtime=runner.runtime,
            hooks=InterventionHooks(
                stop_admission,
                lambda *args: True,
                physical,
                observe_runtime,
                window,
                unused_stop,
                lambda *args: True,
            ),
            clock=r.clock,
            run_id=cfg.run_id,
            plan_sha256=cfg.plan_sha256,
            condition_sha256=hold_contract_sha256(kernel_fixture.HOLD),
        )
        r.orch = Orchestrator.create(
            r.directory,
            cfg,
            intervention=r.cycle,
            evidence=EvidenceStore(r.directory),
            **r.parts(),
        )
        self.addCleanup(r.orch.close)
        executor.dispatches = []
        dispatch = executor.dispatch_existing

        def dispatched(request, payload, **kwargs):
            self.assertIs(request, grants[-1])
            returned = dispatch(request, payload, **kwargs)
            self.assertIs(returned, request)
            executor.dispatches.append((request, json.loads(payload)))
            return returned

        patch.setattr(executor, "dispatch_existing", dispatched)

        def completion(request, returned):
            self.assertIs(request, returned.request)
            worker = workers[request.role]
            self.assertIs(type(returned.sdk_ack), sdk.SdkIdleAck)
            self.assertEqual(
                (
                    returned.sdk_ack.worker,
                    returned.sdk_ack.launch_id,
                    returned.sdk_ack.request_epoch,
                    returned.sdk_ack.capability_id,
                ),
                (
                    worker.worker,
                    worker.launch_id,
                    request.request_epoch,
                    worker.factory.capability_id,
                ),
            )
            return CompletionEvidence(
                request, returned.result, returned.stop_proof, returned.observation
            )

        self.mount(r, execution=executor, completion_adapter=completion)
        self.workers = workers
        return r

    def test_exact_original_borrowed_three_paths_not_goal_resume(self):
        r = self.make()
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        self.assertEqual(r.robot.step_calls, 16)
        self.assertEqual(set(self.decodes), {"scene", "goal", "chunk"})
        self.assertEqual(len(self.begin_attempts), len(self.finished))
        self.assertEqual(len(self.finished), len(self.executor.dispatches))
        self.assertEqual(
            sum(len(w._connection.sent) for w in self.workers.values()),
            len(self.finished),
        )
        self.assertIsNone(r.owner.snapshot(now_ns=r.clock.now())["request"])
        chunks = [p for kind, obs, p in self.preparations if kind == "chunk"]
        self.assertGreater(
            chunks[1].binding.policy_epoch, chunks[0].binding.policy_epoch
        )
        self.assertGreater(chunks[1].binding.observed_ns, chunks[0].binding.observed_ns)

    def test_initial_reset_required_uses_reset_and_judge_then_manual_reset(self):
        r = self.make(reset=True)
        self.assertEqual(r.orch.run(), "WAIT_HUMAN")
        self.assertEqual(
            {req.role for req, payload in self.executor.dispatches}, {"judge", "reset"}
        )
        self.assertEqual(len(self.begin_attempts), len(self.finished))
        self.assertEqual(len(self.finished), len(self.executor.dispatches))

    def test_bad_actual_sdk_ack_no_decode_and_original_slot_retained(self):
        r = self.make(bad_ack=True)
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.robot.step_calls, 0)
        self.assertEqual(self.decodes, [])
        self.assertEqual(self.finished, [])
        snap = r.owner.snapshot(now_ns=r.clock.now())
        self.assertIs(snap["request"], self.fixture.grants[0])
        self.assertTrue(snap["request_revoked"])
        self.assertTrue(r.cycle.failed)

    def test_stop_during_original_borrowed_dispatch_never_publishes(self):
        r = self.make(stop=True)
        self.assertEqual(r.orch.run(), "FAULT_LOCKED")
        self.assertEqual(r.robot.step_calls, 0)
        self.assertEqual(self.decodes, [])
        self.assertTrue(r.cycle.failed)
        self.assertTrue(r.owner.snapshot(now_ns=r.clock.now())["request_revoked"])


if __name__ == "__main__":
    unittest.main()
