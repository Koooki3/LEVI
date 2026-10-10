"""The termination arbiter (levi/automatic/termination.py): candidate ->
evidence -> confirmation -> stop; unknown and unavailable never stop an
episode; stale, foreign and repeated judgements are dropped."""

import itertools

import pytest
from aeri_harness import fake

from levi.automatic.adapters import events as ev
from levi.automatic.termination import (
    ConfigError,
    TerminationArbiter,
    TerminationConfig,
    verification_of,
)
from levi.domain import aeri

RUN = "r-term"
EP = f"{RUN}.forward.0004"
CFG = TerminationConfig(min_steps=5, settle_steps=3, cooldown_steps=5, max_requests=4)


@pytest.fixture
def clock():
    return fake.FakeClock()


def arbiter(config=CFG, episode=EP, target="forward_goal"):
    return TerminationArbiter(
        config, run_id=RUN, episode_id=episode, role="forward", target=target
    )


def events(clock, items, episode=EP):
    stream = ev.FakeEventStream({episode: items}, clock, RUN)
    sid = stream.open(episode, "forward")
    return stream.poll(sid, through_step=10**6, max_events=1000)


def answer(clock, want, spec, episode=EP, target="forward_goal"):
    """Ask the fake verifier about ``want`` and return its bytes."""
    verifier = ev.FakeGoalVerifier([spec], clock, RUN)
    request = ev.make_request(
        run_id=RUN,
        episode_id=episode,
        episode_role="forward",
        target=target,
        observed_through_ns=clock.now(),
        **want,
    )
    return verifier.collect(verifier.submit(request), timeout_ns=10**10)


def result(arb, clock, raw, expected=None):
    return arb.on_result(
        raw,
        now_ns=clock.now(),
        clock_domain=clock.domain,
        specs=ev.SPECS,
        expected=expected,
    )


def ask(arb, step):
    want = arb.want_request(step)
    assert want is not None, step
    arb.issued(want["request_id"], step)
    return want


def test_four_stages_lead_to_a_stop(clock):
    arb = arbiter()
    assert arb.want_request(50) is None  # no candidate, no request
    (raw,) = events(clock, [{"step": 6, "event_type": "object_settled"}])
    assert arb.on_event(raw) == [] and arb.stage == "candidate"
    assert arb.want_request(7) is None  # still settling (6 + 3)
    want = ask(arb, 9)
    assert want["observed_from_step"] == 6 and want["observed_through_step"] == 9
    assert arb.stage == "evidence" and arb.want_request(30) is None  # one at a time
    verdict = result(arb, clock, answer(clock, want, {"decision": "confirmed"}))
    assert verdict.stop and verdict.reason == "goal_verified"
    assert arb.stage == "stop_requested" and arb.want_request(99) is None


def test_no_request_before_min_steps(clock):
    arb = arbiter()
    (raw,) = events(clock, [{"step": 0, "event_type": "gripper_open"}])
    arb.on_event(raw)
    assert arb.want_request(4) is None
    assert arb.want_request(5) is not None


# --- 11.1 row 1: a wrong success never stops early ------------------------------------------


def test_a_contradictory_confirmation_is_a_contract_violation(clock):
    arb = arbiter()
    arb.on_event(events(clock, [{"step": 6, "event_type": "object_settled"}])[0])
    want = ask(arb, 9)
    verdict = result(
        arb, clock, answer(clock, want, {"decision": "confirmed", "contradict": True})
    )
    assert not verdict.stop and verdict.notes[0].code == "contract_violation"
    assert arb.violations == 1


def test_confirmation_must_cover_the_settling_window(clock):
    arb = arbiter()
    arb.on_event(events(clock, [{"step": 6, "event_type": "object_settled"}])[0])
    want = ask(arb, 9)
    raw = answer(clock, want, {"decision": "confirmed", "observed_through_step": 7})
    verdict = result(arb, clock, raw)
    assert not verdict.stop and verdict.notes[0].code == "judgement_dropped_stale"


def test_a_retracted_candidate_voids_its_confirmation(clock):
    arb = arbiter()
    first, retraction = events(
        clock,
        [
            {"step": 6, "event_type": "object_settled", "id": "ev-a"},
            {"step": 8, "event_type": "object_settled", "retracts": "ev-a"},
        ],
    )
    arb.on_event(first)
    want = ask(arb, 9)
    notes = arb.on_event(retraction)
    assert notes[0].code == "candidate_retracted"
    verdict = result(arb, clock, answer(clock, want, {"decision": "confirmed"}))
    assert not verdict.stop and verdict.notes[0].code == "judgement_dropped_retracted"


def test_two_confirmations_in_a_row_when_configured(clock):
    arb = arbiter(
        TerminationConfig(
            min_steps=0, settle_steps=0, cooldown_steps=2, confirmations=2
        )
    )
    arb.on_event(events(clock, [{"step": 1, "event_type": "object_settled"}])[0])
    want = ask(arb, 2)
    assert not result(arb, clock, answer(clock, want, {"decision": "confirmed"})).stop
    want = ask(arb, 4)
    assert result(arb, clock, answer(clock, want, {"decision": "confirmed"})).stop


def test_a_rejection_between_confirmations_resets_the_count(clock):
    arb = arbiter(
        TerminationConfig(
            min_steps=0, settle_steps=0, cooldown_steps=1, confirmations=2
        )
    )
    arb.on_event(events(clock, [{"step": 1, "event_type": "object_settled"}])[0])
    for step, decision in ((2, "confirmed"), (3, "rejected"), (4, "confirmed")):
        want = ask(arb, step)
        assert not result(arb, clock, answer(clock, want, {"decision": decision})).stop


# --- 11.1 rows 2 and 3: unknown, timeout, offline ---------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        {"decision": "unknown"},
        {"unavailable": "timeout"},
        {"unavailable": "model_error"},
        {"unavailable": "gate_closed"},
    ],
)
def test_unknown_and_unavailable_never_stop(clock, spec):
    arb = arbiter()
    arb.on_event(events(clock, [{"step": 6, "event_type": "object_settled"}])[0])
    want = ask(arb, 9)
    verdict = result(arb, clock, answer(clock, want, spec))
    assert not verdict.stop and arb.stage != "stop_requested"
    assert arb.unknowns + arb.unavailables == 1
    # The episode goes on: another request after the cooldown.
    assert arb.want_request(14) is not None


def test_a_withdrawn_request_frees_the_arbiter(clock):
    arb = arbiter()
    arb.on_event(events(clock, [{"step": 6, "event_type": "object_settled"}])[0])
    want = ask(arb, 9)
    arb.withdraw(want["request_id"])  # the caller's timeout
    late = answer(clock, want, {"decision": "confirmed"})
    verdict = result(arb, clock, late)
    assert not verdict.stop and verdict.notes[0].code == "judgement_dropped_unsolicited"


# --- 11.1 row 4: a flapping detector cannot flood the judge -------------------------------------


def test_jittery_events_merge_and_requests_are_spaced_and_capped(clock):
    arb = arbiter()
    flaps = [
        {
            "step": 10 + i,
            "event_type": "gripper_open" if i % 2 == 0 else "gripper_close",
            "priority": "goal_candidate" if i % 2 == 0 else "routine",
        }
        for i in range(10)  # 5 opens and 5 closes within 1 s at 10 Hz
    ]
    for raw in events(clock, flaps):
        arb.on_event(raw)
    asked = []
    for step in range(10, 200):
        want = arb.want_request(step)
        if want:
            arb.issued(want["request_id"], step)
            asked.append(step)
            result(arb, clock, answer(clock, want, {"decision": "unknown"}))
    assert asked[0] >= 18 + CFG.settle_steps - 1  # merged into the latest candidate
    assert all(b - a >= CFG.cooldown_steps for a, b in itertools.pairwise(asked))
    assert len(asked) == CFG.max_requests


def test_duplicate_and_foreign_events_are_dropped_and_gaps_noted(clock):
    arb = arbiter()
    raws = events(
        clock,
        [
            {"step": 1, "event_type": "gripper_open"},
            {"step": 2, "event_type": "gripper_open", "fault": "duplicate"},
            {"step": 3, "event_type": "gripper_open", "fault": "skip_seq"},
            {"step": 4, "event_type": "gripper_open", "fault": "wrong_episode"},
            {"step": 5, "event_type": "gripper_open", "fault": "control_key"},
        ],
    )
    codes = [[n.code for n in arb.on_event(raw)] for raw in raws]
    assert codes == [
        [],
        ["event_dropped_duplicate"],
        ["event_gap"],
        ["event_dropped_stale"],
        ["contract_violation"],
    ]


# --- fences on judgements -------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, code",
    [
        (
            {"decision": "confirmed", "episode_id": f"{RUN}.forward.0003"},
            "judgement_dropped_stale",
        ),
        ({"decision": "confirmed", "run_id": "r-other"}, "contract_violation"),
        (
            {"decision": "confirmed", "request_id": "never-asked"},
            "judgement_dropped_unsolicited",
        ),
        ({"decision": "confirmed", "target": "reset_goal"}, "judgement_dropped_stale"),
        ({"decision": "confirmed", "expired": True}, "judgement_dropped_stale"),
        ({"decision": "confirmed", "other_clock": True}, "judgement_dropped_stale"),
        ({"decision": "confirmed", "future": True}, "judgement_dropped_stale"),
        ({"decision": "confirmed", "minor": 1}, "contract_violation"),
        ({"decision": "confirmed", "bad_predicate": True}, "contract_violation"),
    ],
)
def test_foreign_stale_or_bad_judgements_are_dropped(clock, spec, code):
    arb = arbiter()
    arb.on_event(events(clock, [{"step": 6, "event_type": "object_settled"}])[0])
    want = ask(arb, 9)
    verdict = result(arb, clock, answer(clock, want, spec))
    assert not verdict.stop and verdict.notes[0].code == code


def test_an_answer_is_used_once(clock):
    arb = arbiter(
        TerminationConfig(
            min_steps=0, settle_steps=0, cooldown_steps=0, confirmations=2
        )
    )
    arb.on_event(events(clock, [{"step": 1, "event_type": "object_settled"}])[0])
    want = ask(arb, 2)
    raw = answer(clock, want, {"decision": "confirmed"})
    assert not result(arb, clock, raw).stop
    again = result(arb, clock, raw)  # the same answer twice
    assert not again.stop and again.notes[0].code == "judgement_dropped_unsolicited"


# --- the control group --------------------------------------------------------------------


def test_control_episodes_never_stop_early_and_are_chosen_deterministically(clock):
    config = TerminationConfig(min_steps=0, settle_steps=0, control_fraction=0.3)
    ids = [f"{RUN}.forward.{n:04d}" for n in range(1, 2001)]
    picked = [config.is_control(i) for i in ids]
    assert picked == [config.is_control(i) for i in ids]
    assert 0.25 < sum(picked) / len(ids) < 0.35
    control = ids[picked.index(True)]
    arb = arbiter(config, episode=control)
    assert arb.control
    arb.on_event(
        events(clock, [{"step": 1, "event_type": "object_settled"}], control)[0]
    )
    want = ask(arb, 2)
    verdict = result(
        arb, clock, answer(clock, want, {"decision": "confirmed"}, control)
    )
    assert not verdict.stop and verdict.notes[0].code == "early_stop_withheld"
    assert arb.confirmed_ids  # still recorded for the false-early-stop measure
    assert TerminationConfig().is_control(control) is False  # default: no control group


def test_early_stop_can_be_switched_off(clock):
    arb = arbiter(
        TerminationConfig(min_steps=0, settle_steps=0, allow_early_stop=False)
    )
    arb.on_event(events(clock, [{"step": 1, "event_type": "object_settled"}])[0])
    want = ask(arb, 2)
    assert not result(arb, clock, answer(clock, want, {"decision": "confirmed"})).stop


def test_the_judge_is_ignored_after_repeated_contract_violations(clock):
    arb = arbiter(
        TerminationConfig(
            min_steps=0,
            settle_steps=0,
            cooldown_steps=0,
            violation_limit=2,
            max_requests=10,
        )
    )
    arb.on_event(events(clock, [{"step": 1, "event_type": "object_settled"}])[0])
    for step in (2, 3):
        want = ask(arb, step)
        raw = answer(clock, want, {"decision": "confirmed", "minor": 1})
        result(arb, clock, raw, expected=want["request_id"])
    assert arb.disabled and arb.want_request(10) is None


@pytest.mark.parametrize(
    "spec",
    [
        {"decision": "confirmed", "minor": 1},
        {"decision": "confirmed", "episode_id": f"{RUN}.forward.0003"},
        {"decision": "confirmed", "request_id": "someone-else"},
    ],
)
def test_a_dropped_answer_withdraws_its_request(clock, spec):
    """Regression: a malformed or foreign answer used to leave its request
    outstanding, so no further request was ever made in the episode."""
    arb = arbiter()
    arb.on_event(events(clock, [{"step": 6, "event_type": "object_settled"}])[0])
    want = ask(arb, 9)
    verdict = result(arb, clock, answer(clock, want, spec), expected=want["request_id"])
    assert not verdict.stop and not arb.outstanding
    assert arb.want_request(9 + CFG.cooldown_steps) is not None


# --- configuration and the final verdict -----------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"on_unknown": "wait_human"},
        {"confirmations": 0},
        {"control_fraction": 1.5},
        {"min_steps": -1},
        {"cooldown_steps": 1.5},
        {"allow_early_stop": 1},
    ],
)
def test_bad_configuration_is_refused(over):
    with pytest.raises(ConfigError):
        TerminationConfig(**over)


def test_only_a_confirmation_verifies_a_goal(clock):
    assert verification_of(None) == "unavailable"
    for spec, expected in (
        ({"decision": "confirmed"}, "verified"),
        ({"decision": "rejected"}, "contradicted"),
        ({"decision": "unknown"}, "undecided"),
        ({"unavailable": "timeout"}, "unavailable"),
    ):
        want = {
            "request_id": "q",
            "observed_from_step": 0,
            "observed_through_step": 1,
            "event_ids": [],
        }
        message = aeri.parse(answer(clock, want, spec), "judgement")
        assert verification_of(message) == expected
        assert aeri.OUTCOME_OF_VERIFICATION[expected] != "success" or spec == {
            "decision": "confirmed"
        }
