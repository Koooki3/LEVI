"""Reset arbitration (T-C-08): the Initial State Contract and its reader
(levi/automatic/scene_assessment.py), the reset strategies
(levi/automatic/reset_manager.py) and how the orchestrator uses them, on
fakes only."""

import pytest
from aeri_harness import build, committed, config, fake, results
from test_aeri_orchestrator import check_invariants, no_network  # noqa: F401

from levi.automatic import reset_manager as rm
from levi.automatic import scene_assessment as sa
from levi.automatic.adapters import events as ev
from levi.automatic.orchestrator import ConfigError, RunConfig
from levi.domain import aeri

EXAMPLE = """
# pipeline §6.1, minimal (HA-23)
initial_state:
  id: fake-initial-state
  version: "1"
  status: draft
  robot:
    home_pose: fr3_safe_home
    gripper: open
  predicates:
    required: [object_at_source, gripper_open]
    optional: []
  observations:
    preferred:
      - side
      - wrist
    require_visible_evidence: true
    min_evidence_refs: 2
"""


def contract(**over):
    base = {"text": EXAMPLE}
    base.update(over)
    return sa.load_contract(base["text"])


# --- the contract file ---------------------------------------------------------------------


def test_the_example_reads_into_a_draft_contract():
    found = contract()
    assert found.key == ("fake-initial-state", "1")
    assert found.required == ("object_at_source", "gripper_open")
    assert found.preferred_views == ("side", "wrist")
    assert found.min_evidence_refs == 2 and found.require_visible_evidence
    assert found.status == "draft" and "HA-23" in found.describe()["format"]
    assert found.spec() == {
        ("fake-initial-state", "1"): frozenset({"object_at_source", "gripper_open"})
    }
    # The same contract as JSON.
    again = sa.contract_from(
        {
            "initial_state": {
                "id": "fake-initial-state",
                "version": 1,
                "predicates": {"required": ["object_at_source", "gripper_open"]},
            }
        }
    )
    assert again.key == ("fake-initial-state", "1") and again.min_evidence_refs == 1


@pytest.mark.parametrize(
    "text, where",
    [
        ("initial_state:\n\tid: x\n", "tabs"),
        ("initial_state: &a\n  id: x\n", "anchor"),
        ("a: 1\na: 2\n", "duplicate"),
        ("initial_state:\n  id: x\n  version: 1\n  colour: red\n", "unknown"),
        ("a: {b: 1}\n", "flow mapping"),
        ("a: 1\n---\nb: 2\n", "two documents"),
        ("a: |\n  text\n", "block scalar"),
        ("a:\n  - b: 1\n", "list of mappings"),
        ('{"a": 1, "a": 2}', "json duplicate"),
        ('{"a": NaN}', "json nan"),
        ("a: 1e400\n", "infinite"),
        ("", "empty"),
        ("- a\n- b\n", "not a mapping"),
    ],
)
def test_the_reader_refuses_what_it_does_not_support(text, where):
    with pytest.raises(sa.ContractError):
        sa.contract_from(sa.parse_document(text))


@pytest.mark.parametrize(
    "body, key",
    [
        ({"version": 1, "predicates": {"required": ["a"]}}, "id"),
        ({"id": "c", "version": 1, "predicates": {"required": []}}, "required"),
        ({"id": "c", "version": 1, "predicates": {"required": ["Bad Name"]}}, "name"),
        ({"id": "c", "version": 1, "predicates": {"required": ["a", "a"]}}, "twice"),
        (
            {
                "id": "c",
                "version": 1,
                "predicates": {"required": ["a"]},
                "observations": {"min_evidence_refs": 0},
            },
            "evidence",
        ),
        (
            {
                "id": "c",
                "version": 1,
                "predicates": {"required": ["a"]},
                "observations": {"require_visible_evidence": "yes"},
            },
            "bool",
        ),
        ({"id": "c", "version": True, "predicates": {"required": ["a"]}}, "version"),
        (
            {
                "id": "c",
                "version": 1,
                "status": "final",
                "predicates": {"required": ["a"]},
            },
            "status",
        ),
    ],
)
def test_a_contract_that_does_not_hold_together_is_refused(body, key):
    with pytest.raises(sa.ContractError):
        sa.contract_from(body)


# --- the arbitration ---------------------------------------------------------------------------


def assessment(clock, **spec):
    provider = ev.FakeSceneAssessor([spec], clock, "r-sm")
    request = ev.make_request(
        request_id="q1",
        run_id="r-sm",
        episode_id="r-sm.forward.0001",
        target="initial_state",
    )
    raw = provider.collect(provider.submit(request), timeout_ns=10**10)
    return aeri.parse(raw, "scene", specs=ev.SPECS)


@pytest.mark.parametrize(
    "spec, with_contract, decision, reason",
    [
        # Without a contract a ready never skips (review C3, I3).
        ({"decision": "ready"}, False, "unknown", "no_contract"),
        ({"decision": "ready", "evidence": 2}, True, "ready", "provider"),
        ({"decision": "ready", "evidence": 1}, True, "unknown", "missing_view"),
        ({"decision": "ready", "evidence": 0}, True, "unknown", "missing_view"),
        (
            {"decision": "ready", "refs": ["side:0", "side:0", "wrist:0", "wrist:0"]},
            True,
            "ready",
            "provider",
        ),
        (
            {"decision": "ready", "refs": ["side:0", "wrist:0", "wrist:0"]},
            True,
            "ready",
            "provider",
        ),
        ({"decision": "unknown", "evidence": 5}, True, "unknown", "provider"),
        ({"decision": "reset_required"}, True, "reset_required", "provider"),
    ],
)
def test_only_ready_with_enough_evidence_may_skip(
    spec, with_contract, decision, reason
):
    found = sa.arbitrate(
        assessment(fake.FakeClock(), **spec), contract() if with_contract else None
    )
    assert (found.decision, found.reason) == (decision, reason)
    assert found.may_skip_reset == (decision == "ready")


def test_a_ready_that_left_out_a_required_predicate_is_unknown():
    text = EXAMPLE.replace(
        "required: [object_at_source, gripper_open]",
        "required: [object_at_source, gripper_open, door_closed]",
    )
    found = sa.arbitrate(
        assessment(fake.FakeClock(), decision="ready", evidence=3),
        sa.load_contract(text),
    )
    assert (found.decision, found.reason) == ("unknown", "missing_predicate")
    assert found.unknown == ("door_closed",)


def test_an_assessment_of_another_contract_is_unavailable():
    text = EXAMPLE.replace('version: "1"', 'version: "2"')
    found = sa.arbitrate(
        assessment(fake.FakeClock(), decision="ready", evidence=3),
        sa.load_contract(text),
    )
    assert (found.decision, found.reason) == ("unavailable", "contract_mismatch")
    assert sa.arbitrate(None, contract()).decision == "unavailable"


# --- the strategies ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decision, attempts, on_unknown, action, reason",
    [
        ("ready", 0, "reset", "forward", "scene_ready"),
        ("ready", 5, "reset", "forward", "scene_ready"),
        ("reset_required", 0, "reset", "reset", "scene_reset_required"),
        ("reset_required", 1, "reset", "wait_human", "scene_reset_required"),
        ("unknown", 0, "reset", "reset", "scene_unknown"),
        ("unavailable", 0, "reset", "reset", "scene_unknown"),
        ("unknown", 0, "wait_human", "wait_human", "scene_unknown"),
        ("reset_required", 0, "wait_human", "reset", "scene_reset_required"),
    ],
)
@pytest.mark.mode_matrix(
    "arbitration:plan:ready",
    "arbitration:plan:reset_required",
    "arbitration:plan:unknown",
    "arbitration:plan:unavailable",
    modes=("single_reset_policy",),
)
def test_single_reset_policy_plans(decision, attempts, on_unknown, action, reason):
    strategy = rm.SingleResetPolicy(True, 1, on_unknown)
    assert rm.check_plan(strategy, decision, attempts) == rm.ResetPlan(action, reason)


@pytest.mark.mode_matrix(
    "arbitration:plan:ready",
    "arbitration:plan:reset_required",
    "arbitration:plan:unknown",
    "arbitration:plan:unavailable",
    modes=("single_reset_policy", "human_assisted"),
)
def test_a_disabled_reset_policy_and_human_assisted_always_ask_a_person():
    for strategy in (rm.SingleResetPolicy(enabled=False), rm.HumanAssistedReset()):
        for decision in ("reset_required", "unknown", "unavailable"):
            assert rm.check_plan(strategy, decision, 0).action == "wait_human"
        assert rm.check_plan(strategy, "ready", 0).action == "forward"


@pytest.mark.mode_matrix(
    "arbitration:after_reset:reset_verified",
    "arbitration:after_reset:scene_unknown",
    "arbitration:after_reset:reset_horizon_exhausted",
    "arbitration:after_reset:operator_stop",
    "arbitration:after_reset:policy_error",
    modes=("single_reset_policy",),
)
def test_after_a_reset_only_a_verified_or_retryable_scene_is_checked_again():
    s = rm.SingleResetPolicy(max_attempts=2)
    assert rm.check_after(s, "reset_verified", 1, False) == "VERIFY_INITIAL"
    assert rm.check_after(s, "scene_unknown", 1, False) == "VERIFY_INITIAL"
    assert rm.check_after(s, "scene_unknown", 2, False) == "WAIT_HUMAN"
    assert rm.check_after(s, "scene_unknown", 1, True) == "WAIT_HUMAN"
    for outcome in ("reset_horizon_exhausted", "operator_stop", "policy_error"):
        assert rm.check_after(s, outcome, 0, False) == "WAIT_HUMAN"


class Rogue:
    name = "rogue"

    def plan(self, decision, attempts, **kw):
        return rm.ResetPlan("forward", "scene_ready")

    def after_reset(self, outcome, attempts, stop_pending):
        return "VERIFY_INITIAL"


def test_a_strategy_that_would_skip_on_unknown_is_refused():
    with pytest.raises(rm.StrategyError):
        rm.check_plan(Rogue(), "unknown", 0)
    with pytest.raises(rm.StrategyError):
        rm.check_after(Rogue(), "reset_horizon_exhausted", 0, False)
    with pytest.raises(rm.StrategyError):
        rm.check_after(Rogue(), "scene_unknown", 0, True)
    # A verified reset goes to the next scene check, where a stop waits.
    assert rm.check_after(Rogue(), "reset_verified", 0, True) == "VERIFY_INITIAL"


@pytest.mark.parametrize("name", ["atomic_skill_sequence", "scripted_safe_reset", "x"])
def test_strategies_not_in_v1_are_refused_by_the_config(name):
    with pytest.raises(ConfigError):
        config(reset_strategy=name)
    with pytest.raises(ConfigError):
        RunConfig(run_id="r", plan_sha256="ab" * 32, episodes=1, initial_state="x")


# --- in the orchestrator ------------------------------------------------------------------------


def one(**over):
    return config(episodes=1, **over)


def scene_of(clock, *specs, default=None):
    return ev.FakeSceneAssessor(
        list(specs),
        clock,
        "r-sm",
        default=default or {"decision": "ready", "evidence": 2},
    )


def both_roles(clock):
    return ev.FakeEventStream(
        {
            "forward": [{"step": 8, "event_type": "object_settled"}],
            "reset": [{"step": 6, "event_type": "object_settled"}],
        },
        clock,
        "r-sm",
    )


def test_a_ready_scene_without_evidence_runs_the_reset_instead_of_skipping(tmp_path):
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=one(initial_state=contract()),
        clock=clock,
        scene=scene_of(clock, {"decision": "ready", "evidence": 0}),  # no evidence
        events=both_roles(clock),
    )
    assert r.orch.run() == "COMPLETED"
    path = committed(r.orch)
    assert path[1] == ("VERIFY_INITIAL", "RESET_ACTIVE", "scene_unknown")
    assert r.orch.note_counts["scene_missing_view"] == 1
    check_invariants(r)


def test_a_ready_scene_with_evidence_skips_the_reset(tmp_path):
    clock = fake.FakeClock()
    r = build(
        tmp_path, cfg=one(initial_state=contract()), clock=clock, scene=scene_of(clock)
    )
    assert r.orch.run() == "COMPLETED"
    assert not any(b == "RESET_ACTIVE" for _, b, _ in committed(r.orch))
    assert not any(".reset." in m[1] for m in r.robot.motions)


@pytest.mark.parametrize("decision", ["unknown", "unavailable"])
def test_unknown_never_skips_the_reset(tmp_path, decision):
    clock = fake.FakeClock()
    first = (
        {"decision": "unknown", "evidence": 9}
        if decision == "unknown"
        else {"unavailable": "timeout"}
    )
    r = build(
        tmp_path,
        cfg=one(initial_state=contract(), on_scene_unknown="wait_human"),
        clock=clock,
        scene=scene_of(clock, first),
    )
    assert r.orch.run() == "WAIT_HUMAN"
    assert committed(r.orch)[-1] == ("VERIFY_INITIAL", "WAIT_HUMAN", "scene_unknown")
    assert r.robot.motions == []


@pytest.mark.mode_matrix(
    "transition:VERIFY_INITIAL->WAIT_HUMAN",
    "transition:WAIT_HUMAN->PREFLIGHT",
    "transition:PREFLIGHT->VERIFY_INITIAL",
    "transition:VERIFY_INITIAL->FORWARD_ACTIVE",
    modes=("human_assisted",),
)
def test_human_assisted_never_runs_a_reset_policy_and_resumes_through_a_fresh_check(
    tmp_path,
):
    clock = fake.FakeClock()
    scene = scene_of(clock, {"decision": "reset_required"})
    r = build(
        tmp_path,
        cfg=one(reset_strategy="human_assisted"),
        clock=clock,
        scene=scene,
    )
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.policy.acquired == [] and r.robot.motions == []
    seq = r.orch.journal.next_seq
    for _ in range(2):  # a double click resumes once
        found = r.orch.resume(
            "c-1", expected_seq=seq, environment_handled=True, health_rechecked=True
        )
        assert found.ok
    assert [x for x in committed(r.orch) if x[2] == "human_resumed"] == [
        ("WAIT_HUMAN", "PREFLIGHT", "human_resumed")
    ]
    assert r.orch.run() == "COMPLETED"
    path = committed(r.orch)
    # Back through PREFLIGHT and a fresh initial-state check, never into motion.
    at = path.index(("WAIT_HUMAN", "PREFLIGHT", "human_resumed"))
    assert path[at + 1] == ("PREFLIGHT", "VERIFY_INITIAL", "preflight_passed")
    assert [s["target"] for s in scene.submitted] == ["initial_state", "initial_state"]
    check_invariants(r)


@pytest.mark.mode_matrix(
    "transition:RESET_FINALIZE->WAIT_HUMAN", modes=("single_reset_policy",)
)
def test_a_reset_at_its_horizon_keeps_its_failed_rollout_and_waits(tmp_path):
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=one(initial_state=contract()),
        clock=clock,
        scene=scene_of(
            clock, {"decision": "reset_required"}, {"decision": "reset_required"}
        ),
        events=ev.FakeEventStream({}, clock, "r-sm"),
    )
    assert r.orch.run() == "WAIT_HUMAN"
    (result,) = results(r.orch)
    assert result.stop_reason == "horizon_exhausted"
    assert result.task_outcome == "failure" and result.scene_reset == "failed"
    assert result.rollout.sealed == "complete"
    rollout = r.recorder.rollouts["reset/demo_0001"]
    assert rollout.complete_marker and rollout.state == "complete"
    assert committed(r.orch)[-1] == (
        "RESET_FINALIZE",
        "WAIT_HUMAN",
        "reset_horizon_exhausted",
    )
    moved = len(r.robot.motions)
    assert r.orch.run() == "WAIT_HUMAN" and len(r.robot.motions) == moved


def test_a_failed_reset_home_never_continues(tmp_path):
    clock = fake.FakeClock()
    r = build(
        tmp_path,
        cfg=one(initial_state=contract(), max_reset_attempts=3),
        clock=clock,
        scene=scene_of(
            clock, {"decision": "reset_required"}, {"decision": "ready", "evidence": 2}
        ),
        robot={"home_faults": {0: "home_miss"}},
    )
    assert r.orch.run() == "FAULT_LOCKED"
    assert committed(r.orch)[-1][1:] == ("FAULT_LOCKED", "home_failed")
    moved = len(r.robot.motions)
    for _ in range(2):
        assert r.orch.run() == "FAULT_LOCKED"
    assert len(r.robot.motions) == moved
    assert not any(b == "FORWARD_ACTIVE" for _, b, _ in committed(r.orch))
    check_invariants(r)
