"""Routing order, physical safety, labels and the external-unavailable path (U1-U5, U9-U11, U18, U20)."""

import dataclasses

import pytest

from levi.decision import safety
from levi.decision.calibration import ESC_ALL, Calibration, KeyMismatch, RunKey
from levi.decision.contracts import (
    Context,
    EscalationRequest,
    EscalationResponse,
    LabelTier,
    Reason,
    Route,
    Source,
    State,
    TypedAnswer,
)
from levi.decision.router import finalize, route

H = "a" * 64
KEY = RunKey("m", H, H, H)


def cal(tau=0.8, temperature=1.0, key=KEY) -> Calibration:
    return Calibration(
        shape="binary",
        temperature=temperature,
        tau=tau,
        key=key,
        delta=0.02,
        lcb_level=0.9,
        grid=(0.5,),
        seed=0,
        boots=1,
        n_items=1,
        n_episodes=1,
        episodes_sha256=H,
        table=(),
        ece_loeo=0.0,
        ece_full=0.0,
    )


def ans(p=0.99, value="true", mass=0.0, vetoes=(), ood=False, source=Source.LOGPROBS):
    return TypedAnswer(value, p, source, mass, tuple(vetoes), ood)


def go(a, kind="boundary", ctx="offline", c=None):
    return route(a, kind, ctx, c or cal(), KEY)


def test_a_confident_non_physical_answer_stays_local_with_the_automatic_label():
    d = go(ans())
    assert (d.route, d.reason, d.label_tier) == (
        Route.LOCAL,
        Reason.AT_OR_ABOVE_THRESHOLD,
        LabelTier.AUTOMATIC,
    )


def test_below_threshold_goes_to_the_panel_with_the_agent_reviewed_label():
    d = go(ans(p=0.6))
    assert (d.route, d.reason, d.label_tier) == (
        Route.ESCALATE,
        Reason.BELOW_THRESHOLD,
        LabelTier.AGENT_REVIEWED,
    )


def test_U2_threshold_boundary_is_inclusive():
    tau = 0.8
    assert go(ans(p=tau), c=cal(tau=tau)).route is Route.LOCAL
    assert go(ans(p=tau + 1e-9), c=cal(tau=tau)).route is Route.LOCAL
    assert go(ans(p=tau - 1e-9), c=cal(tau=tau)).route is Route.ESCALATE


def test_the_threshold_applies_to_the_calibrated_probability_not_the_raw_one():
    # T=4 flattens 0.9 to about 0.57: raw would pass tau=0.8, calibrated must not
    d = go(ans(p=0.9), c=cal(tau=0.8, temperature=4.0))
    assert d.route is Route.ESCALATE and d.p_cal < 0.8


def test_U1_unknown_confidence_escalates():
    for a in (
        TypedAnswer("true", None, None, None),
        TypedAnswer("true", None, Source.LOGPROBS, 0.0),
    ):
        assert (
            go(a).reason is Reason.CONFIDENCE_UNKNOWN and go(a).route is Route.ESCALATE
        )
    # a logprobs confidence without its out-of-set mass is also unknown
    assert go(ans(mass=None)).reason is Reason.CONFIDENCE_UNKNOWN
    # a probability with no source is unknown, not trusted
    assert go(TypedAnswer("true", 0.99, None, 0.0)).reason is Reason.CONFIDENCE_UNKNOWN


def test_U10_abstention_escalates():
    d = go(ans(value=None))
    assert (d.route, d.reason) == (Route.ESCALATE, Reason.ABSTAINED)


def test_U11_out_of_set_mass_over_the_limit_escalates():
    assert go(ans(mass=0.06)).reason is Reason.OUT_OF_SET_MASS
    assert go(ans(mass=0.05)).route is Route.LOCAL
    assert (
        route(
            ans(mass=0.2), "boundary", "offline", cal(), KEY, max_out_of_set_mass=0.3
        ).route
        is Route.LOCAL
    )


def test_U9_a_physical_veto_beats_a_high_probability():
    d = go(ans(p=0.999, vetoes=("gripper_closed_but_released",)))
    assert (d.route, d.reason) == (Route.ESCALATE, Reason.PHYSICAL_VETO)


def test_out_of_distribution_escalates():
    assert go(ans(ood=True)).reason is Reason.OUT_OF_DISTRIBUTION


def test_escalate_all_never_answers_locally():
    d = go(ans(p=1.0), c=cal(tau=ESC_ALL))
    assert (d.route, d.reason) == (Route.ESCALATE, Reason.ESCALATE_ALL)


def test_U3_every_physical_pair_goes_to_a_person_even_at_p_0_999():
    physical = [
        k for k, v in safety.SAFETY_TABLE.items() if v is safety.Safety.PHYSICAL
    ]
    assert physical, "the table lists physical pairs"
    for kind, ctx in physical:
        d = go(ans(p=0.999), kind=kind, ctx=ctx)
        assert (d.route, d.reason, d.label_tier) == (
            Route.HUMAN,
            Reason.PHYSICAL_SAFETY,
            None,
        ), (kind, ctx)


def test_the_user_decision_live_completion_is_physical_offline_is_not():
    assert safety.is_physical("completion_check", Context.LIVE)
    assert not safety.is_physical("completion_check", Context.OFFLINE)


def test_U4_unregistered_kinds_and_contexts_are_physical():
    for kind in ("zzz-new-kind", "", "boundary "):
        assert go(ans(), kind=kind).route is Route.HUMAN
    assert (
        go(ans(), kind="boundary", ctx="live").route is Route.HUMAN
    )  # only offline is listed
    assert go(ans(), kind="boundary", ctx="sim").route is Route.HUMAN


def test_safety_is_decided_before_confidence_so_even_unknown_confidence_goes_to_a_person():
    assert (
        go(TypedAnswer(None, None), kind="collision", ctx="live").route is Route.HUMAN
    )


def test_a_calibration_for_another_prompt_is_refused():
    other = dataclasses.replace(KEY, prompt_sha256="b" * 64)
    with pytest.raises(KeyMismatch):
        go(ans(), c=cal(key=other))


def test_there_is_no_self_reported_source():
    assert {s.value for s in Source} == {"logprobs", "sample_agreement", "signal_check"}
    assert {t.value for t in LabelTier} == {
        "automatic",
        "agent-reviewed",
    }  # U18: no human tier


# --- outcomes -------------------------------------------------------------------------

REQ = EscalationRequest("r1", "boundary", "spec", (), {})


def resp(a="x", b="x", adj=None, rid="r1", answer="x"):
    return EscalationResponse(rid, answer, a, b, adj)


def test_U18_the_two_label_tiers_per_path():
    local = finalize(go(ans()), "true")
    assert (local.state, local.label_tier, local.answer) == (
        State.ANSWERED_LOCAL,
        LabelTier.AUTOMATIC,
        "true",
    )
    esc = finalize(go(ans(p=0.1)), "true", REQ, resp(answer="false"))
    assert (esc.state, esc.label_tier, esc.answer) == (
        State.ANSWERED_EXTERNAL,
        LabelTier.AGENT_REVIEWED,
        "false",
    )
    human = finalize(go(ans(), kind="collision", ctx="live"), "true")
    assert (human.state, human.label_tier, human.answer) == (
        State.WITH_HUMAN,
        None,
        None,
    )


def test_U5_external_unavailable_queues_without_label_or_local_fallback():
    d = go(ans(p=0.1))
    for response in (None,):
        o = finalize(d, "local-value", REQ, response)
        assert (o.state, o.label_tier, o.answer) == (State.PENDING_EXTERNAL, None, None)
    assert (
        finalize(d, "local-value").state is State.PENDING_EXTERNAL
    )  # no request either


def test_U20_disagreeing_reviewers_need_an_adjudicator():
    d = go(ans(p=0.1))
    assert finalize(d, "v", REQ, resp(a="x", b="y")).state is State.PENDING_EXTERNAL
    assert (
        finalize(d, "v", REQ, resp(a="x", b="y", adj="y", answer="y")).state
        is State.ANSWERED_EXTERNAL
    )
    assert finalize(d, "v", REQ, resp(a="x", b="x")).state is State.ANSWERED_EXTERNAL


def test_a_response_for_another_request_or_contract_is_not_an_answer():
    d = go(ans(p=0.1))
    assert finalize(d, "v", REQ, resp(rid="other")).state is State.PENDING_EXTERNAL
    bad = dataclasses.replace(resp(), contract="levi.decision.escalation_response.v0")
    assert finalize(d, "v", REQ, bad).state is State.PENDING_EXTERNAL


def test_the_request_the_panel_sees_carries_no_local_answer_or_confidence():
    names = {f.name for f in dataclasses.fields(EscalationRequest)}
    assert not names & {
        "local_answer",
        "local_confidence",
        "split",
        "family",
        "episode_ref",
        "item_ref",
    }


def test_unknown_token_counts_stay_unknown_and_negative_ones_are_refused():
    from levi.decision.contracts import TokenRow

    assert TokenRow("a", None, 3, None, 0, False).input_new is None
    with pytest.raises(ValueError):
        TokenRow("a", -1, 0, 0, 0, True)


REQUIRED_PHYSICAL = {
    ("completion_check", "live"),
    ("reset_ready", "live"),
    ("release_round", "live"),
    ("plan", "live"),
    ("workspace_clear", "live"),
    ("person_or_obstacle", "live"),
    ("collision", "live"),
    ("grip_force", "live"),
    ("wait_human_reason", "live"),
}


def test_U3_the_table_keeps_every_required_physical_pair():
    # iterating the table alone would not notice a deleted entry (it would become
    # "unlisted", which is also physical); this pins the list the user approved
    for kind, ctx in REQUIRED_PHYSICAL:
        assert (
            safety.SAFETY_TABLE.get((kind, Context(ctx))) is safety.Safety.PHYSICAL
        ), (kind, ctx)
