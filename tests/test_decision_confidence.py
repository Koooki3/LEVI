"""Confidence extraction from an injected logprobs structure (P2). No model, no service."""

import json
import math

import pytest

from levi.decision.confidence import (
    TokenLogprob,
    closed_field,
    run_vetoes,
    sample_agreement,
    typed_answer,
)
from levi.decision.contracts import Source

ALLOWED = ("true", "false", "null")


def lp(**probs):
    return {k: math.log(v) for k, v in probs.items()}


def reply(top, value="true", extra=""):
    """Strict-JSON output cut into tokens; the value token carries ``top``."""
    head = '{"answers":[{"index":0,"value":'
    tail = f',"camera":"wrist"{extra},"reason":"gripper open"}}]}}'
    content = head + value + tail
    toks = [TokenLogprob(head, {}), TokenLogprob(value, top), TokenLogprob(tail, {})]
    return content, toks


def test_probabilities_are_renormalised_over_the_allowed_values():
    content, toks = reply(lp(true=0.72, false=0.18, null=0.02, **{" maybe": 0.08}))
    c = closed_field(content, toks, "value", ALLOWED)
    assert c.value == "true"
    assert c.p_raw == pytest.approx(0.72 / 0.92)
    assert sum(c.probs.values()) == pytest.approx(1.0)
    assert c.out_of_set_mass == pytest.approx(0.08)


def test_a_masked_distribution_has_no_out_of_set_mass():
    content, toks = reply(lp(true=0.9, false=0.1))
    c = closed_field(content, toks, "value", ALLOWED)
    assert c.out_of_set_mass == pytest.approx(0.0, abs=1e-9)
    assert c.p_raw == pytest.approx(0.9)


def test_a_quoted_string_value_and_a_split_first_token_are_found():
    head = '{"v":'
    content = head + '"yes"}'
    toks = [
        TokenLogprob(head, {}),
        TokenLogprob('"y', lp(**{'"y': 0.8, '"n': 0.2})),
        TokenLogprob('es"}', {}),
    ]
    c = closed_field(content, toks, "v", ("yes", "no"))
    assert c.value == "yes" and c.p_raw == pytest.approx(0.8)


def test_U1_everything_that_cannot_be_located_is_unknown():
    good_content, good = reply(lp(true=0.9, false=0.1))
    top = lp(true=0.9, false=0.1)
    assert closed_field(good_content, [], "value", ALLOWED) is None  # no logprobs
    assert closed_field(good_content, good, "missing", ALLOWED) is None  # field absent
    assert (
        closed_field(good_content + "x", good, "value", ALLOWED) is None
    )  # tokens differ from text
    dup = '{"value":1,"value":2}'
    assert (
        closed_field(dup, [TokenLogprob(dup, top)], "value", ALLOWED) is None
    )  # repeated field
    only_head = [good[0], TokenLogprob(good[1].token, {}), good[2]]
    assert (
        closed_field(good_content, only_head, "value", ALLOWED) is None
    )  # empty alternatives
    assert closed_field(good_content, good, "value", ("true", "true")) is None
    assert closed_field(good_content, good, "value", ()) is None


def test_unknown_when_no_allowed_value_is_among_the_alternatives():
    content, toks = reply(lp(**{" maybe": 0.6, " unsure": 0.4}))
    assert closed_field(content, toks, "value", ALLOWED) is None


def test_unknown_when_an_alternative_starts_two_allowed_values():
    content, toks = reply(lp(t=0.9), value="tr")
    assert closed_field(content, toks, "value", ("true", "tree")) is None


def test_unknown_for_impossible_probabilities():
    content, toks = reply({"true": 0.1, "false": math.log(0.3)})  # logprob > 0
    assert closed_field(content, toks, "value", ALLOWED) is None
    content, toks = reply(lp(true=0.9, false=0.4))  # sums above 1
    assert closed_field(content, toks, "value", ALLOWED) is None


def test_a_self_reported_confidence_field_is_never_read():
    top = lp(true=0.6, false=0.4)
    content, toks = reply(top, extra=',"confidence":0.99')
    assert '"confidence":0.99' in content
    assert closed_field(content, toks, "value", ALLOWED).p_raw == pytest.approx(0.6)
    assert (
        closed_field(content, toks, "confidence", ALLOWED) is None
    )  # not a closed field either
    json.loads(content)


def test_typed_answer_wraps_unknown_and_abstention():
    assert typed_answer(None).p_raw is None and typed_answer(None).source is None
    content, toks = reply(lp(null=0.8, true=0.2), value="null")
    a = typed_answer(closed_field(content, toks, "value", ALLOWED))
    assert (
        a.value is None
        and a.p_raw == pytest.approx(0.8)
        and a.source is Source.LOGPROBS
    )
    content, toks = reply(lp(true=0.8, false=0.2))
    a = typed_answer(
        closed_field(content, toks, "value", ALLOWED),
        vetoes=["v"],
        out_of_distribution=True,
    )
    assert a.value == "true" and a.vetoes == ("v",) and a.out_of_distribution


def test_vetoes_come_from_caller_rules_and_a_failing_rule_vetoes():
    def closed_but_released(ctx):
        return "gripper_closed_but_released" if ctx["gripper"] == "closed" else None

    def broken(_ctx):
        raise KeyError("signal missing")

    assert run_vetoes([closed_but_released], {"gripper": "open"}) == ()
    assert run_vetoes([closed_but_released], {"gripper": "closed"}) == (
        "gripper_closed_but_released",
    )
    assert run_vetoes([broken], {}) == ("veto_check_error:KeyError",)


def test_sample_agreement_is_the_modal_share():
    assert sample_agreement([]) is None
    assert sample_agreement(["a", "a", "b"]) == ("a", pytest.approx(2 / 3))
