"""RELAXED[2026-11-17]: policy chunk timing. The first chunk of an episode may
take 1.5 s (was 0.5), a first policy_error before any step is retried once,
and five chunk failures in a row (was three) stop an episode."""

import pytest
from aeri_harness import build, committed, fake, results
from test_aeri_orchestrator import check_invariants, one_episode

from levi.automatic.orchestrator import ConfigError, RunConfig


def test_the_defaults_are_the_relaxed_ones():
    cfg = RunConfig.__dataclass_fields__
    assert cfg["chunk_timeout_ns"].default == 500_000_000  # later chunks unchanged
    assert cfg["chunk_first_timeout_ns"].default == 1_500_000_000
    assert cfg["chunk_failure_limit"].default == 5


def test_the_first_chunk_budget_is_validated():
    with pytest.raises(ConfigError, match="chunk_first_timeout_ns"):
        one_episode(chunk_first_timeout_ns=0)


def _budgets(r):
    """(request_id, budget_ms) of every chunk request the policy saw."""
    seen = []
    original = r.policy.request

    def request(req):
        seen.append((req["request_id"], req["deadline"]["budget_ms"]))
        return original(req)

    r.policy.request = request
    return seen


def test_only_the_first_chunk_of_an_episode_gets_the_long_budget(tmp_path):
    r = build(tmp_path, cfg=one_episode(forward_max_steps=40))
    seen = _budgets(r)
    r.orch.run()
    first = [ms for rid, ms in seen if rid.endswith(":c0")]
    later = [ms for rid, ms in seen if not rid.endswith(":c0")]
    assert first and set(first) == {1500}
    assert later and set(later) == {500}


def test_a_first_chunk_that_needs_one_second_arrives_in_time(tmp_path):
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=one_episode(),
        clock=clock,
        policy={"slow_ns": 1_000_000_000, "chunk_faults": {0: "slow"}},
    )
    r.orch.run()
    path = committed(r.orch)
    assert ("FORWARD_ACTIVE", "FORWARD_STOPPING", "policy_error") not in path
    assert "chunk_timeout" not in {code for code, _ in r.orch.notes}  # no retry needed
    (result,) = results(r.orch)
    assert result.rollout.sealed == "complete"
    check_invariants(r)


def test_a_chunk_slower_than_the_first_budget_still_fails(tmp_path):
    r = build(
        tmp_path,
        cfg=one_episode(chunk_failure_limit=1),
        policy={"slow_ns": 2_000_000_000, "chunk_faults": {0: "slow"}},
    )
    r.orch.run()
    assert ("FORWARD_ACTIVE", "FORWARD_STOPPING", "policy_error") in committed(r.orch)
    assert "chunk_timeout" in {code for code, _ in r.orch.notes}


def test_a_first_policy_error_before_any_step_is_retried_once(tmp_path):
    """RELAXED[2026-11-17] (was: the first one ended the episode and locked)."""
    r = build(tmp_path, cfg=one_episode(), policy={"chunk_faults": {0: "policy_error"}})
    r.orch.run()
    path = committed(r.orch)
    assert "FAULT_LOCKED" not in {to for _, to, _ in path}
    assert ("FORWARD_ACTIVE", "FORWARD_STOPPING", "policy_error") not in path
    assert "chunk_retry_first" in {code for code, _ in r.orch.notes}
    (result,) = results(r.orch)
    assert result.rollout.sealed == "complete" and r.robot.step_calls > 0
    check_invariants(r)


def test_a_second_policy_error_before_any_step_still_locks(tmp_path):
    r = build(
        tmp_path,
        cfg=one_episode(),
        policy={"chunk_faults": {0: "policy_error", 1: "policy_error"}},
    )
    assert r.orch.run() == "FAULT_LOCKED"
    path = committed(r.orch)
    assert path[-1] == ("FORWARD_FINALIZE", "FAULT_LOCKED", "policy_error")
    assert r.robot.step_calls == 0
    assert [c for c, _ in r.orch.notes].count("chunk_retry_first") == 1


def test_a_policy_error_after_a_step_is_not_retried_by_this_rule(tmp_path):
    r = build(tmp_path, cfg=one_episode(), policy={"chunk_faults": {2: "policy_error"}})
    r.orch.run()
    assert "chunk_retry_first" not in {code for code, _ in r.orch.notes}


def test_a_busy_first_request_is_asked_again(tmp_path):
    r = build(tmp_path, cfg=one_episode(), policy={"chunk_faults": {0: "busy"}})
    r.orch.run()
    assert "FAULT_LOCKED" not in {to for _, to, _ in committed(r.orch)}
    assert r.robot.step_calls > 0
    check_invariants(r)


@pytest.mark.parametrize("failures,locked", [(4, False), (5, True)])
def test_five_chunk_failures_in_a_row_stop_the_episode(tmp_path, failures, locked):
    """RELAXED[2026-11-17]: the limit was 3, so 3 and 4 failures stopped it."""
    r = build(
        tmp_path,
        cfg=one_episode(),
        policy={"chunk_faults": {n: "timeout" for n in range(failures)}},
    )
    r.orch.run()
    stopped = ("FORWARD_ACTIVE", "FORWARD_STOPPING", "policy_error") in committed(
        r.orch
    )
    assert stopped is locked


def test_a_first_policy_error_then_five_timeouts_homes_and_does_not_lock(tmp_path):
    """The retry clears the before-motion flag: when the retry then fails by
    timeouts the episode ends as an ordinary policy_error (home, no lock); only
    a SECOND policy_error before any step locks."""
    faults = {0: "policy_error", **{n: "timeout" for n in range(1, 6)}}
    r = build(tmp_path, cfg=one_episode(), policy={"chunk_faults": faults})
    r.orch.run()
    path = committed(r.orch)
    assert ("FORWARD_ACTIVE", "FORWARD_STOPPING", "policy_error") in path
    assert "FAULT_LOCKED" not in {to for _, to, _ in path}
    assert ("FORWARD_FINALIZE", "ROBOT_HOME", "policy_error") in path  # homed
    assert (
        r.policy.requests >= 6
        and [c for c, _ in r.orch.notes].count("chunk_retry_first") == 1
    )
