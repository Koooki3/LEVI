"""Original Kernel + owned worker fake inventory/IPC; no fake authority."""

import ast
import inspect
from dataclasses import replace

import pytest
import test_owned_role_execution as owned_helpers

from levi.automatic.owned_runtime import BorrowedRoleExecution
from levi.live.runtime_ownership import RoleWindow

CLOCK, NOW, PROFILE = owned_helpers.CLOCK, owned_helpers.NOW, owned_helpers.PROFILE
execution = owned_helpers.execution


@pytest.fixture
def borrowed(execution):
    runner, workers, deadline = execution
    executor = BorrowedRoleExecution(
        runner.authority,
        runner.runtime,
        runner.lease,
        runner.observer,
        clock=lambda: NOW,
    )
    return executor, workers, deadline


def grant(executor, deadline, *, role="forward", authorization=1):
    window = RoleWindow(
        executor.authority.broker.broker_id,
        "run",
        "run.forward.0001",
        PROFILE,
        NOW,
        NOW + 100_000_000,
        CLOCK,
        role,
        1,
        authorization,
    )
    return executor.authority.begin_role_request(
        executor.runtime,
        role,
        deadline,
        executor.observer.observe(executor.lease),
        window,
        episode_id="run.forward.0001",
        policy_epoch=1,
        now_ns=NOW,
    )


def dispatch(executor, request):
    return executor.dispatch_existing(
        request,
        {"obs": "cpu"},
        role=request.role,
        episode_id=request.episode_id,
        policy_epoch=request.policy_epoch,
    )


def finish(executor, completion, *, now=NOW):
    return executor.authority.finish_request(
        completion.request,
        completion.stop_proof,
        executor.observer.observe(executor.lease),
        now_ns=now,
    )


def test_borrowed_source_has_no_second_grant_finish_or_renew():
    tree = ast.parse(inspect.getsource(BorrowedRoleExecution))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not calls & {
        "begin_request",
        "begin_role_request",
        "finish_request",
        "renew_resident",
    }
    assert "_request" not in {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


def test_original_grant_dispatch_ack_stop_proof_preserves_cycle_finish(
    borrowed, monkeypatch
):
    executor, workers, deadline = borrowed
    counts = {"begin": 0, "finish": 0}
    begin = executor.authority.begin_role_request
    end = executor.authority.finish_request

    def spy_begin(*args, **kwargs):
        counts["begin"] += 1
        return begin(*args, **kwargs)

    def spy_finish(*args, **kwargs):
        counts["finish"] += 1
        return end(*args, **kwargs)

    monkeypatch.setattr(executor.authority, "begin_role_request", spy_begin)
    monkeypatch.setattr(executor.authority, "finish_request", spy_finish)
    request = grant(executor, deadline)
    assert dispatch(executor, request) is request
    workers["forward"]._connection.finish({"actions": [1, 2]})
    completion = executor.collect_existing(request, timeout_s=0.001)
    assert completion.request is request and completion.stop_proof.request is request
    assert completion.sdk_ack.request_epoch == request.request_epoch
    assert completion.result == {"actions": [1, 2]}
    assert counts == {"begin": 1, "finish": 0}
    assert executor.authority.snapshot(now_ns=NOW)["request"] == request
    assert finish(executor, completion) is True
    assert counts == {"begin": 1, "finish": 1}


def test_timeout_retains_original_pending_and_blocks_duplicate_dispatch(borrowed):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    dispatch(executor, request)
    with pytest.raises(TimeoutError):
        executor.collect_existing(request, timeout_s=0.001)
    assert workers["forward"]._pending == ("result", request.request_epoch)
    assert executor.authority.snapshot(now_ns=NOW)["request"] == request
    with pytest.raises(ValueError, match="exact new"):
        dispatch(executor, request)
    assert len(workers["forward"]._connection.sent) == 1


@pytest.mark.parametrize(
    "change", ["role", "episode_id", "policy_epoch", "runtime", "runtime-bool"]
)
def test_foreign_role_episode_epoch_runtime_never_reaches_worker(borrowed, change):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    forged = {
        "role": replace(request, role="judge"),
        "episode_id": replace(request, episode_id="run.forward.0002"),
        "policy_epoch": replace(request, policy_epoch=True),
        "runtime": replace(request, runtime=replace(request.runtime, runtime_epoch=99)),
        "runtime-bool": replace(
            request, runtime=replace(request.runtime, runtime_epoch=True)
        ),
    }[change]
    with pytest.raises(ValueError):
        dispatch(executor, forged)
    assert all(not worker._connection.sent for worker in workers.values())
    assert executor.authority.snapshot(now_ns=NOW)["request"] == request


@pytest.mark.parametrize(
    "binding", ["workers", "launch", "factory", "connection", "scope"]
)
def test_original_worker_bindings_cannot_be_swapped(borrowed, binding):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    worker = workers["forward"]
    if binding == "workers":
        executor.observer.workers["forward"] = workers["judge"]
    elif binding == "launch":
        worker.launch_id = "replacement"
    elif binding == "factory":
        worker.factory = replace(worker.factory, module="replacement")
    elif binding == "connection":
        worker._connection = object()
    else:
        worker.group_scope = object()
    with pytest.raises(ValueError, match="binding"):
        dispatch(executor, request)
    assert executor.authority.snapshot(now_ns=NOW)["request"] == request


def test_public_runtime_lease_observer_are_readonly_original_bindings(borrowed):
    executor, _workers, _deadline = borrowed
    for field in ("runtime", "lease", "observer", "authority"):
        with pytest.raises(AttributeError):
            setattr(executor, field, object())
    lease = executor.lease
    with pytest.raises(ValueError):
        lease.lease_epoch += 1
    lease = lease.model_copy(update={"lease_epoch": lease.lease_epoch + 1})
    assert lease.lease_epoch != executor.lease.lease_epoch
    assert executor.lease.lease_epoch == executor.runtime.lease_epoch


def test_probe_unknown_after_ack_permanently_withdraws_cached_result(
    borrowed, monkeypatch
):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    dispatch(executor, request)
    workers["forward"]._connection.finish("no publication after unknown")
    original = executor.observer.observe

    def unknown(lease):
        raise ValueError("unknown complete group")

    monkeypatch.setattr(executor.observer, "observe", unknown)
    with pytest.raises(ValueError, match="unknown"):
        executor.collect_existing(request, timeout_s=0.001)
    assert executor.authority.snapshot(now_ns=NOW)["request"] == request
    monkeypatch.setattr(executor.observer, "observe", original)
    completion = executor.collect_existing(request, timeout_s=0.001)
    assert completion.result is None
    assert completion.stop_proof.backend_request_stopped is True
    assert finish(executor, completion) is False


def test_bad_ack_then_genuine_drain_keeps_original_slot_and_withdraws_result(borrowed):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    dispatch(executor, request)
    pipe = workers["forward"]._connection
    pipe.finish("bad")
    kind, ack, result = pipe.answer[0]
    pipe.answer[0] = (kind, replace(ack, launch_id="foreign"), result)
    with pytest.raises(ValueError, match="exact current"):
        executor.collect_existing(request, timeout_s=0.001)
    assert workers["forward"]._pending is not None
    assert executor.authority.snapshot(now_ns=NOW)["request"] == request
    pipe.finish("late valid drain")
    completion = executor.collect_existing(request, timeout_s=0.001)
    assert completion.result is None and finish(executor, completion) is False


def test_busy_or_incomplete_peer_group_cannot_form_stop_proof(borrowed):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    dispatch(executor, request)
    workers["forward"]._connection.finish("cpu")
    workers["judge"]._idle = None
    with pytest.raises(ValueError, match="complete"):
        executor.collect_existing(request, timeout_s=0.001)
    assert executor.authority.snapshot(now_ns=NOW)["request"] == request


def test_revoked_and_closed_request_can_drain_but_never_return_result(borrowed):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    dispatch(executor, request)
    executor.close()
    workers["forward"]._connection.finish("late")
    completion = executor.collect_existing(request, timeout_s=0.001)
    assert completion.result is None
    assert finish(executor, completion) is False


def test_new_exact_kernel_grant_can_replace_only_completed_local_record(borrowed):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    dispatch(executor, request)
    workers["forward"]._connection.finish("one")
    completion = executor.collect_existing(request, timeout_s=0.001)
    with pytest.raises(ValueError):
        dispatch(executor, replace(request, request_epoch=99))
    assert executor._active is request
    assert finish(executor, completion)
    next_request = grant(executor, deadline, role="reset", authorization=2)
    assert dispatch(executor, next_request) is next_request
    with pytest.raises(ValueError, match="exact active"):
        executor.collect_existing(request, timeout_s=0.001)
    assert workers["reset"]._pending == ("result", next_request.request_epoch)


def test_late_completion_returns_only_trusted_drain_proof(borrowed):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    dispatch(executor, request)
    workers["forward"]._connection.finish("expired")
    late = request.expires_ns + 1
    executor._clock = lambda: late
    executor.observer.clock = lambda: late
    completion = executor.collect_existing(request, timeout_s=0.001)
    assert completion.result is None
    assert executor.authority.snapshot(now_ns=late)["request"] == request
    assert finish(executor, completion, now=late) is False


def test_revocation_between_ack_and_fresh_group_probe_withdraws_publication(
    borrowed, monkeypatch
):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    dispatch(executor, request)
    workers["forward"]._connection.finish("stop before group proof")
    original = executor.observer.observe

    def stopped(lease):
        executor.revoke(request)
        return original(lease)

    monkeypatch.setattr(executor.observer, "observe", stopped)
    completion = executor.collect_existing(request, timeout_s=0.001)
    assert completion.result is None
    assert finish(executor, completion) is False


def test_borrowed_close_never_waits_for_collection_or_worker_mutex(borrowed):
    executor, workers, deadline = borrowed
    request = grant(executor, deadline)
    dispatch(executor, request)
    executor._lock.acquire()
    workers["forward"]._lock.acquire()
    try:
        executor.close()
        assert all(worker._closed.is_set() for worker in workers.values())
        assert executor.authority.snapshot(now_ns=NOW)["request"] == request
        assert workers["forward"]._pending is not None
    finally:
        workers["forward"]._lock.release()
        executor._lock.release()


def test_equal_copy_cannot_dispatch_or_revoke_original_grant(borrowed, monkeypatch):
    executor, workers, deadline = borrowed
    original = grant(executor, deadline)
    copied = replace(original)
    assert copied == original and copied is not original
    observation = executor.observer.observe(executor.lease)
    assert executor.authority.accepts_result(copied, observation, now_ns=NOW)
    revocations = []
    original_revoke = executor.authority.revoke_request

    def spy_revoke(*args, **kwargs):
        revocations.append(args[0])
        return original_revoke(*args, **kwargs)

    def untouched_probe(_lease):
        raise AssertionError("copy must reject before any observer side effect")

    monkeypatch.setattr(executor.authority, "revoke_request", spy_revoke)
    monkeypatch.setattr(executor.observer, "observe", untouched_probe)
    with pytest.raises(ValueError, match="original active"):
        dispatch(executor, copied)
    assert not revocations and not workers["forward"]._connection.sent
    assert executor.authority.snapshot(now_ns=NOW)["request"] is original
    assert executor.authority.accepts_exact_request(original, observation, now_ns=NOW)


@pytest.mark.parametrize("operation", ["collect_existing", "revoke"])
def test_equal_copy_cannot_collect_or_revoke_original_borrowed_work(
    borrowed, operation
):
    executor, workers, deadline = borrowed
    original = grant(executor, deadline)
    dispatch(executor, original)
    workers["forward"]._connection.finish("original")
    with pytest.raises(ValueError, match="exact active"):
        getattr(executor, operation)(replace(original))
    assert executor.authority.owns_exact_request(original)
    completion = executor.collect_existing(original, timeout_s=0.001)
    assert completion.request is original and completion.result == "original"
    assert finish(executor, completion)


def test_missing_exact_kernel_contract_refuses_without_value_fallback(
    execution, monkeypatch
):
    runner, _workers, _deadline = execution
    monkeypatch.setattr(runner.authority, "accepts_exact_request", None)
    with pytest.raises(ValueError, match="exact original"):
        BorrowedRoleExecution(
            runner.authority,
            runner.runtime,
            runner.lease,
            runner.observer,
            clock=lambda: NOW,
        )
