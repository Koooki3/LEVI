"""Original Kernel precise-object query; old public value semantics retained."""

from dataclasses import replace

import pytest
import test_runtime_ownership as base

NOW, MS = base.NOW, base.MS
resident = base.resident


def test_exact_original_query_rejects_equal_copy_without_revoking_grant(resident):
    owner, runtime, observation, _lease = resident
    original = owner.begin_request(runtime, base.due(), observation, now_ns=NOW)
    copied = replace(original)
    assert copied == original and copied is not original
    assert owner.accepts_result(copied, observation, now_ns=NOW)
    assert not owner.owns_exact_request(copied)
    assert not owner.accepts_exact_request(copied, observation, now_ns=NOW)
    assert owner.owns_exact_request(original)
    assert owner.accepts_exact_request(original, observation, now_ns=NOW)
    assert owner.snapshot(now_ns=NOW)["request"] is original
    # Old finish remains value-compatible; the new borrowed path never uses copy.
    assert owner.finish_request(copied, base.stopped(copied), observation, now_ns=NOW)
    assert not owner.owns_exact_request(original)
    assert not owner.owns_exact_request(None)


@pytest.mark.parametrize("loss", ["revoked", "expired", "unknown"])
def test_owned_original_drain_identity_is_separate_from_result_permission(
    resident, loss
):
    owner, runtime, observation, _lease = resident
    original = owner.begin_request(
        runtime, base.due(budget_ms=10), observation, now_ns=NOW
    )
    now = NOW
    if loss == "revoked":
        owner.revoke_request(original, now_ns=now)
    elif loss == "expired":
        now += 10 * MS
        observation = replace(observation, observed_ns=now)
    else:
        observation = replace(observation, process_running=None)
    assert not owner.accepts_exact_request(original, observation, now_ns=now)
    assert owner.owns_exact_request(original)
    assert not owner.owns_exact_request(replace(original))
    assert owner.snapshot(now_ns=now)["request"] is original


def test_new_grant_never_matches_old_exact_object(resident):
    owner, runtime, observation, _lease = resident
    first = owner.begin_request(runtime, base.due(), observation, now_ns=NOW)
    assert owner.finish_request(first, base.stopped(first), observation, now_ns=NOW)
    second = owner.begin_request(runtime, base.due(), observation, now_ns=NOW)
    assert owner.owns_exact_request(second)
    assert not owner.owns_exact_request(first)
    assert not owner.accepts_exact_request(first, observation, now_ns=NOW)
    assert owner.accepts_exact_request(second, observation, now_ns=NOW)
