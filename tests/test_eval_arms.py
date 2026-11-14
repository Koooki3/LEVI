"""The pre-registered "LEVI 2.0 is better" criteria (levi/eval/arms.py).

Only intervals and verdicts: no record, no dataset, no model.
"""

import math

import pytest

from levi.eval import arms
from levi.eval.arms import (
    ACHIEVED,
    BETTER,
    NOT_BETTER,
    PARTIAL,
    UNDETERMINED,
    Family,
    Verdict,
)

MARGIN = 0.02
NON_INFERIOR = (-0.01, 0.05)  # lower end >= -MARGIN
FASTER = (-30.0, -5.0)  # C - B wall seconds, whole interval below 0
SLOWER = (5.0, 30.0)
SAME = (-10.0, 10.0)


def prior(quality=NON_INFERIOR, costs=None, **kw):
    costs = {"wall_s": FASTER, "local_tokens": SAME} if costs is None else costs
    return arms.better_than_prior_levi(quality, MARGIN, costs, **kw)


def direct(quality=NON_INFERIOR, costs=None, reliability=None, **kw):
    costs = {"wall_s": FASTER} if costs is None else costs
    reliability = {"legal_rate": (0.02, 0.2)} if reliability is None else reliability
    return arms.better_than_direct(quality, MARGIN, costs, reliability, **kw)


# --------------------------------------------------------------- C against B


def test_c_is_better_than_b_with_non_inferior_quality_and_one_cost_better():
    verdict = prior()
    assert verdict.verdict == BETTER
    assert "cost better: wall_s" in verdict.reasons


def test_the_quality_bound_is_inclusive_at_minus_the_margin():
    assert prior((-MARGIN, 0.1)).verdict == BETTER
    assert prior((-MARGIN - 0.001, 0.1)).verdict == UNDETERMINED


def test_clearly_inferior_quality_is_not_better_whatever_the_costs():
    assert prior((-0.2, -MARGIN - 0.01)).verdict == NOT_BETTER
    # An interval that still reaches the margin is undetermined, not "no".
    assert prior((-0.2, -MARGIN)).verdict == UNDETERMINED


def test_an_interval_too_wide_to_decide_is_undetermined_not_better():
    verdict = prior((-0.3, 0.3))
    assert verdict.verdict == UNDETERMINED


def test_a_missing_quality_interval_is_undetermined():
    assert prior(None).verdict == UNDETERMINED


def test_a_cost_significantly_worse_than_b_is_not_better():
    costs = {"wall_s": FASTER, "local_tokens": (10.0, 50.0)}
    verdict = prior(costs=costs)
    assert verdict.verdict == NOT_BETTER
    assert "cost worse: local_tokens" in verdict.reasons
    # Worse even while quality is still undetermined.
    assert prior((-0.3, 0.3), costs=costs).verdict == NOT_BETTER


def test_a_cost_interval_touching_zero_is_neither_better_nor_worse():
    # low == 0 is not "worse"; high == 0 is not "better".
    assert prior(costs={"wall_s": FASTER, "x": (0.0, 5.0)}).verdict == BETTER
    assert prior(costs={"wall_s": (-5.0, 0.0)}).verdict == NOT_BETTER


def test_no_cost_significantly_better_is_not_better():
    verdict = prior(costs={"wall_s": SAME, "local_tokens": SAME})
    assert verdict.verdict == NOT_BETTER
    assert "no cost is significantly better" in verdict.reasons


def test_a_missing_cost_interval_leaves_it_undetermined():
    assert prior(costs={"wall_s": FASTER, "local_tokens": None}).verdict == UNDETERMINED
    assert prior(costs={}).verdict == UNDETERMINED


def test_external_tokens_count_as_worse_only_if_the_efficiency_condition_fails():
    costs = {"wall_s": FASTER, "external_tokens": (100.0, 900.0)}
    assert prior(costs=costs, external_efficiency_ok=True).verdict == BETTER
    assert prior(costs=costs, external_efficiency_ok=False).verdict == NOT_BETTER
    assert prior(costs=costs).verdict == UNDETERMINED
    # The exemption is for the named metrics only.
    other = {"wall_s": FASTER, "local_tokens": (100.0, 900.0)}
    assert prior(costs=other, external_efficiency_ok=True).verdict == NOT_BETTER
    named = prior(
        costs={"wall_s": FASTER, "ext_fresh": (1.0, 9.0)},
        external_costs=("ext_fresh",),
        external_efficiency_ok=True,
    )
    assert named.verdict == BETTER


def test_an_exempt_external_metric_is_never_counted_as_the_better_cost():
    only = {"external_tokens": (-9.0, -1.0)}
    assert prior(costs=only, external_efficiency_ok=True).verdict == BETTER
    assert (
        prior(
            costs={"external_tokens": (1.0, 9.0)}, external_efficiency_ok=True
        ).verdict
        == NOT_BETTER
    )


def test_for_an_error_metric_the_quality_interval_is_turned_around():
    # An error 0.04 lower to 0.01 higher than B's is, as merit, [-0.01, 0.04].
    assert prior((-0.04, 0.01), higher_is_better=False).verdict == BETTER
    assert prior((-0.01, 0.04), higher_is_better=False).verdict == UNDETERMINED


@pytest.mark.parametrize(
    "bad",
    [(1.0, 0.0), (math.nan, 1.0), (0.0, math.inf), (True, 1.0), ("a", 1.0), (1.0,), 5],
)
def test_malformed_intervals_are_refused_not_judged(bad):
    with pytest.raises((ValueError, TypeError)):
        prior(bad)
    with pytest.raises((ValueError, TypeError)):
        prior(costs={"wall_s": bad})


def test_the_margin_must_be_a_non_negative_number():
    for margin in (-0.1, True, "0.02"):
        with pytest.raises(ValueError, match="margin"):
            arms.better_than_prior_levi(NON_INFERIOR, margin, {})
        with pytest.raises(ValueError, match="margin"):
            arms.better_than_direct(NON_INFERIOR, margin, {}, {})


# --------------------------------------------------------------- C against A


def test_a_quality_gain_whose_lower_end_is_above_zero_is_enough():
    # Costs worse and reliability worse do not matter when quality is better.
    verdict = direct(
        (0.001, 0.3), costs={"wall_s": SLOWER}, reliability={"legal_rate": (-0.2, -0.1)}
    )
    assert verdict.verdict == BETTER
    assert direct((0.0, 0.3), costs={"wall_s": SLOWER}).verdict != BETTER


def test_without_a_quality_gain_it_takes_non_inferior_cheaper_and_more_reliable():
    assert direct().verdict == BETTER
    # each of the three conditions missing in turn, with the gain decided absent
    flat = (-0.01, 0.0)
    assert direct(flat, costs={"wall_s": SAME}).verdict == NOT_BETTER
    assert direct(flat, costs={"wall_s": SLOWER}).verdict == NOT_BETTER
    assert direct(flat, reliability={"legal_rate": (-0.1, 0.1)}).verdict == NOT_BETTER
    assert direct(flat, reliability={"legal_rate": (-0.3, -0.1)}).verdict == NOT_BETTER
    assert direct((-0.3, -0.1)).verdict == NOT_BETTER
    # A reliability interval that only touches zero is not "better".
    assert direct(flat, reliability={"legal_rate": (0.0, 0.2)}).verdict == NOT_BETTER


def test_a_missing_condition_is_undetermined_not_no():
    flat = (-0.01, 0.0)
    assert direct(flat, costs={"wall_s": None}).verdict == UNDETERMINED
    assert direct(flat, reliability={"legal_rate": None}).verdict == UNDETERMINED
    assert direct(flat, costs={}).verdict == UNDETERMINED
    assert direct(flat, reliability={}).verdict == UNDETERMINED
    assert direct(None).verdict == UNDETERMINED


def test_an_interval_spanning_zero_leaves_a_failed_second_path_undetermined():
    # The gain may still be real (the lower end is below 0 only by noise).
    assert direct(NON_INFERIOR, costs={"wall_s": SAME}).verdict == UNDETERMINED


def test_against_a_non_inferiority_that_is_undetermined_the_second_path_waits():
    assert direct((-0.3, 0.001)).verdict == UNDETERMINED


def test_for_direct_the_external_exemption_and_error_direction_apply_too():
    costs = {"wall_s": FASTER, "external_tokens": (100.0, 900.0)}
    assert direct(costs=costs, external_efficiency_ok=True).verdict == BETTER
    assert direct(costs=costs, external_efficiency_ok=False).verdict == UNDETERMINED
    # An error 0.3 to 0.001 lower than direct's: merit gain, lower end > 0.
    assert direct((-0.3, -0.001), higher_is_better=False).verdict == BETTER
    assert (
        direct((0.001, 0.3), higher_is_better=False, costs={"wall_s": SLOWER}).verdict
        != BETTER
    )


# ------------------------------------------------- significantly worse quality


def test_quality_is_significantly_worse_only_when_the_whole_interval_is_below_zero():
    assert arms.quality_significantly_worse((-0.3, -0.01)) is True
    assert arms.quality_significantly_worse((-0.3, 0.0)) is False
    assert arms.quality_significantly_worse((-0.3, 0.1)) is False
    assert arms.quality_significantly_worse(None) is None
    # An error metric is turned around: a higher error is worse.
    assert arms.quality_significantly_worse((0.01, 0.3), higher_is_better=False) is True
    assert (
        arms.quality_significantly_worse((-0.3, -0.01), higher_is_better=False) is False
    )
    assert arms.quality_significantly_worse((0.0, 0.3), higher_is_better=False) is False


def test_any_arm_with_significantly_worse_quality_makes_the_family_worse():
    worse, fine, unknown = (-0.3, -0.1), (-0.1, 0.1), None
    assert arms.any_quality_worse([fine, worse]) is True
    assert arms.any_quality_worse([fine, unknown]) is None
    assert arms.any_quality_worse([worse, unknown]) is True
    assert arms.any_quality_worse([fine, fine]) is False
    assert arms.any_quality_worse([]) is None
    assert arms.any_quality_worse([(0.1, 0.3)], higher_is_better=False) is True


# ---------------------------------------------------------------- the summary


def verdict(name):
    return Verdict(name, ())


def fam(a=BETTER, b=BETTER, worse=False):
    return Family(verdict(a), verdict(b), worse)


def test_a_majority_better_than_both_arms_and_no_worse_family_is_achieved():
    result = arms.overall({"t1": fam(), "t2": fam(), "t3": fam(UNDETERMINED)})
    assert result.verdict == ACHIEVED


def test_exactly_half_is_not_a_majority():
    assert arms.overall({"t1": fam(), "t2": fam(NOT_BETTER)}).verdict == PARTIAL
    assert arms.overall({"t1": fam(), "t2": fam(UNDETERMINED)}).verdict == UNDETERMINED


def test_better_than_only_one_of_the_two_arms_does_not_count():
    families = {
        "t1": fam(BETTER, NOT_BETTER),
        "t2": fam(NOT_BETTER, BETTER),
        "t3": fam(),
    }
    assert arms.overall(families).verdict == PARTIAL


def test_one_family_with_significantly_worse_quality_blocks_the_claim():
    families = {"t1": fam(), "t2": fam(), "t3": fam(), "t4": fam(worse=True)}
    result = arms.overall(families)
    assert result.verdict == PARTIAL
    assert "['t4']" in " ".join(result.reasons)


def test_unknown_worseness_with_a_majority_is_undetermined():
    families = {"t1": fam(), "t2": fam(), "t3": fam(worse=None)}
    assert arms.overall(families).verdict == UNDETERMINED


def test_open_families_that_could_still_make_a_majority_are_undetermined():
    families = {"t1": fam(), "t2": fam(UNDETERMINED), "t3": fam(UNDETERMINED)}
    assert arms.overall(families).verdict == UNDETERMINED
    families = {"t1": fam(NOT_BETTER), "t2": fam(UNDETERMINED), "t3": fam(NOT_BETTER)}
    assert arms.overall(families).verdict == PARTIAL


def test_an_empty_or_malformed_summary_is_not_a_claim():
    assert arms.overall({}).verdict == UNDETERMINED
    with pytest.raises(ValueError, match="unknown verdict"):
        arms.overall({"t1": fam("yes")})


def test_the_verdict_names_are_the_documented_ones():
    assert arms.VERDICTS == ("better", "not_better", "undetermined")
    assert (ACHIEVED, PARTIAL) == ("achieved", "partial")
