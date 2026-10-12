"""Frozen root Cycle/Kernel + candidate owned IPC; all hardware inventory fake.

Run in a fresh interpreter with LEVI_JOINT_KERNEL_ROOT set. The package overlay
selects original root core files and local owned adapter files, without copying
source or substituting a FakeAuthority. No SDK/model/hardware is launched.
"""

import hashlib
import inspect
import json
import marshal
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

root_value = os.environ.get("LEVI_JOINT_KERNEL_ROOT")
if not root_value:
    pytest.skip(
        "explicit fixed original Kernel/Cycle root required", allow_module_level=True
    )
ROOT = Path(root_value).resolve()
CANDIDATE = Path(__file__).resolve().parents[2]
for name in (
    "levi.domain.aeri",
    "levi.live.workload_broker",
    "levi.live.runtime_ownership",
    "levi.live.runtime_bridge",
    "levi.automatic.intervention",
    "levi.live.owned_runtime",
    "levi.automatic.owned_runtime",
):
    if name in sys.modules:
        raise RuntimeError(
            "fresh interpreter required for exact source selection: " + name
        )

import levi

levi.__path__ = [str(ROOT / "levi"), str(CANDIDATE / "levi")]
import levi.automatic
import levi.live

levi.live.__path__ = [str(ROOT / "levi/live"), str(CANDIDATE / "levi/live")]
levi.automatic.__path__ = [
    str(ROOT / "levi/automatic"),
    str(CANDIDATE / "levi/automatic"),
]

import test_owned_role_execution as owned_helpers

from levi.automatic import intervention
from levi.automatic import owned_runtime as borrowed_module
from levi.automatic.intervention import (
    InferenceCycle,
    InterventionHooks,
    InterventionUnproven,
)
from levi.automatic.owned_runtime import BorrowedRoleExecution
from levi.domain import aeri
from levi.live import owned_runtime, runtime_bridge, runtime_ownership, workload_broker
from levi.live.runtime_ownership import RoleWindow

execution = owned_helpers.execution
NOW, CLOCK, PROFILE = owned_helpers.NOW, owned_helpers.CLOCK, owned_helpers.PROFILE


def test_exact_original_root_sources_are_loaded():
    modules = [aeri, runtime_bridge, runtime_ownership, workload_broker, intervention]
    for module in modules:
        expected = ROOT / Path(module.__name__.replace(".", "/") + ".py")
        assert Path(module.__file__).resolve() == expected
        assert Path(inspect.getsourcefile(module)).resolve() == expected
    for module in (owned_runtime, borrowed_module):
        expected = CANDIDATE / Path(module.__name__.replace(".", "/") + ".py")
        assert Path(module.__file__).resolve() == expected
    assert "request_call" in inspect.signature(InferenceCycle.invoke).parameters
    functions = {
        "Cycle.invoke": InferenceCycle.invoke,
        "Kernel.begin": runtime_ownership.RuntimeOwnership.begin_role_request,
        "Kernel.finish": runtime_ownership.RuntimeOwnership.finish_request,
        "Kernel.owns_exact": runtime_ownership.RuntimeOwnership.owns_exact_request,
        "Kernel.accepts_exact": runtime_ownership.RuntimeOwnership.accepts_exact_request,
        "Worker.collect": owned_runtime.OwnedModelWorker.collect_sdk_completion,
        "Borrowed.dispatch": BorrowedRoleExecution.dispatch_existing,
    }
    print(
        json.dumps(
            {
                "loaded_sources": {
                    module.__name__: {
                        "path": str(Path(module.__file__).resolve()),
                        "sha256": hashlib.sha256(
                            Path(module.__file__).read_bytes()
                        ).hexdigest(),
                    }
                    for module in [*modules, owned_runtime, borrowed_module]
                },
                "executed_functions": {
                    name: {
                        "source": str(Path(inspect.getsourcefile(function)).resolve()),
                        "code_sha256": hashlib.sha256(
                            marshal.dumps(function.__code__)
                        ).hexdigest(),
                    }
                    for name, function in functions.items()
                },
            },
            sort_keys=True,
        )
    )


@pytest.mark.parametrize("late_group_busy", [False, True])
def test_actual_cycle_borrows_once_and_independently_checks_final_group(
    execution, monkeypatch, late_group_busy
):
    runner, workers, _deadline = execution
    executor = BorrowedRoleExecution(
        runner.authority,
        runner.runtime,
        runner.lease,
        runner.observer,
        clock=lambda: NOW,
    )
    counts = {"begin": 0, "finish": 0}
    original_begin = runner.authority.begin_role_request
    original_finish = runner.authority.finish_request

    def begin(*args, **kwargs):
        counts["begin"] += 1
        return original_begin(*args, **kwargs)

    def finish(*args, **kwargs):
        counts["finish"] += 1
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(runner.authority, "begin_role_request", begin)
    monkeypatch.setattr(runner.authority, "finish_request", finish)
    completions, cycle_observations, borrowed_requests = [], [], []

    def unused_physical_hook(*args):
        raise AssertionError("forward CPU fake must not manufacture physical evidence")

    def observe_runtime():
        observation = runner.observer.observe(runner.lease)
        cycle_observations.append(observation)
        return observation

    def role_window(role, episode, policy_epoch, authorization, deadline_ns):
        return RoleWindow(
            runner.authority.broker.broker_id,
            "run",
            episode,
            PROFILE,
            NOW,
            deadline_ns,
            CLOCK,
            role,
            policy_epoch,
            authorization,
        )

    def stop_proof(request, deadline_ns):
        assert completions[0].request is request
        assert deadline_ns == NOW + 100_000_000
        if late_group_busy:
            workers["judge"]._idle = None
        return completions[0].stop_proof

    hooks = InterventionHooks(
        unused_physical_hook,
        unused_physical_hook,
        unused_physical_hook,
        observe_runtime,
        role_window,
        stop_proof,
        unused_physical_hook,
    )
    cycle = InferenceCycle(
        authority=runner.authority,
        runtime=runner.runtime,
        hooks=hooks,
        clock=SimpleNamespace(now=lambda: NOW, domain=CLOCK),
        run_id="run",
        plan_sha256=PROFILE,
        condition_sha256=PROFILE,
    )

    def request_call(request):
        borrowed_requests.append(request)
        executor.dispatch_existing(
            request,
            {"obs": "cpu"},
            role="forward",
            episode_id="run.forward.0001",
            policy_epoch=1,
        )
        assert counts == {"begin": 1, "finish": 0}
        assert workers["forward"]._pending == ("result", request.request_epoch)
        workers["forward"]._connection.finish({"actions": [0.25, 0.5]})
        completion = executor.collect_existing(request, timeout_s=0.001)
        assert (
            completion.request is request and completion.stop_proof.request is request
        )
        assert completion.sdk_ack.worker == workers["forward"].worker
        assert completion.result == {"actions": [0.25, 0.5]}
        completions.append(completion)
        assert counts == {"begin": 1, "finish": 0}
        return completion.result

    arguments = {
        "deadline_ns": NOW + 100_000_000,
        "request_call": request_call,
        "interrupted": lambda: False,
    }
    if late_group_busy:
        with pytest.raises(InterventionUnproven):
            cycle.invoke("forward", "run.forward.0001", 1, **arguments)
        assert runner.authority.snapshot(now_ns=NOW)["request"] is borrowed_requests[0]
        assert cycle.failed is True
    else:
        assert cycle.invoke("forward", "run.forward.0001", 1, **arguments) == {
            "actions": [0.25, 0.5]
        }
        assert runner.authority.snapshot(now_ns=NOW)["request"] is None
    assert counts == {"begin": 1, "finish": 1}
    assert len(borrowed_requests) == len(completions) == 1
    assert len(workers["forward"]._connection.sent) == 1
    assert len(cycle_observations) == 2
    assert cycle_observations[-1] is not completions[0].observation
    assert runner.authority.snapshot(now_ns=NOW)["runtime"] == runner.runtime
    assert runner.authority.snapshot(now_ns=NOW)["lease"] == runner.lease


def test_original_legacy_kernel_without_exact_contract_is_refused(execution):
    runner, workers, _deadline = execution
    if callable(getattr(runner.authority, "accepts_exact_request", None)):
        pytest.skip("exact-contract Kernel present; legacy refusal checked separately")
    assert Path(inspect.getsourcefile(type(runner.authority))).resolve() == (
        ROOT / "levi/live/runtime_ownership.py"
    )
    with pytest.raises(ValueError, match="exact original"):
        BorrowedRoleExecution(
            runner.authority,
            runner.runtime,
            runner.lease,
            runner.observer,
            clock=lambda: NOW,
        )
    assert all(not worker._connection.sent for worker in workers.values())
    assert runner.authority.snapshot(now_ns=NOW)["request"] is None
