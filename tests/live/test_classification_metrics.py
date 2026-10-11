"""Synthetic confusion matrices; these do not recount historical rollout data."""

import pytest

from levi.automatic.analysis import verdicts
from levi.live import stats


def test_known_synthetic_matrix_has_unrounded_binary_metrics():
    got = stats.binary_classification(34, 10, 5, 60)
    assert got["n"] == 109
    assert (got["tp"], got["fn"], got["fp"], got["tn"]) == (34, 10, 5, 60)
    assert got["success"]["precision"] == pytest.approx(34 / 39)
    assert got["success"]["recall"] == pytest.approx(34 / 44)
    assert got["success"]["f1"] == pytest.approx(68 / 83)
    assert got["failure"]["f1"] == pytest.approx(120 / 135)
    assert got["macro_f1"] == pytest.approx((68 / 83 + 120 / 135) / 2)
    assert got["balanced_accuracy"] == pytest.approx((34 / 44 + 60 / 65) / 2)
    assert got["accuracy"] == pytest.approx(94 / 109)


@pytest.mark.parametrize("bad", [True, False, -1, 0.5, "2", None, float("inf")])
def test_counts_are_nonnegative_integer_members_not_flags(bad):
    for position in range(4):
        values = [1, 1, 1, 1]
        values[position] = bad
        with pytest.raises(ValueError):
            stats.binary_classification(*values)


def test_empty_metrics_have_no_synthetic_zero_success():
    got = stats.binary_classification(0, 0, 0, 0)
    assert got["n"] == 0 and got["accuracy"] is None
    assert got["macro_f1"] is None and got["balanced_accuracy"] is None
    for label in ("success", "failure"):
        assert got[label]["support"] == 0
        assert got[label]["precision"] is None
        assert got[label]["recall"] is None
        assert got[label]["f1"] is None


def test_absent_class_keeps_two_class_averages_undefined():
    got = stats.binary_classification(0, 0, 0, 6)
    assert got["failure"]["f1"] == 1
    assert got["success"]["f1"] is None
    assert got["macro_f1"] is None and got["balanced_accuracy"] is None


def test_no_success_predictions_still_has_zero_success_f1():
    got = stats.binary_classification(0, 4, 0, 6)
    assert got["success"]["precision"] is None
    assert got["success"]["recall"] == 0 and got["success"]["f1"] == 0
    assert got["failure"]["f1"] == pytest.approx(0.75)
    assert got["macro_f1"] == pytest.approx(0.375)
    assert got["balanced_accuracy"] == pytest.approx(0.5)


def test_live_and_evaluation_share_binary_metrics_keep_old_unknown_rates():
    pairs = [
        ("success", "success"),
        ("success", "failure"),
        ("failure", "success"),
        ("failure", "failure"),
        ("success", "undecided"),
        ("failure", None),
        (None, "success"),
    ]
    automatic = verdicts.agreement(pairs)
    live = stats.agreement(
        [
            (
                person,
                {"undecided": True}
                if pred == "undecided"
                else None
                if pred is None
                else {"outcome": pred},
            )
            for person, pred in pairs
        ]
    )
    assert automatic["classification"] == live["classification"]
    assert automatic["classification"]["n"] == 4
    assert automatic["labelled"] == live["pairs"] == 6
    assert automatic["unlabelled"] == 1
    assert automatic["undecided"] == live["undecided"] == 1
    assert automatic["no_verdict"] == live["no_agent"] == 1
    assert automatic["missed_success"]["rate"] == pytest.approx(2 / 3)
    assert automatic["false_success"]["rate"] == pytest.approx(0.5)


def test_ended_by_groups_use_the_same_metrics_without_mixing_populations():
    live = stats.agreement(
        [
            ({"outcome": "success", "ended_by": "budget"}, {"outcome": "failure"}),
            (
                {"outcome": "failure", "ended_by": "operator_key"},
                {"outcome": "failure"},
            ),
        ]
    )
    budget = live["by_ended_by"]["budget"]["classification"]
    key = live["by_ended_by"]["operator_key"]["classification"]
    assert budget["n"] == key["n"] == 1
    assert budget["fn"] == 1 and budget["success"]["f1"] == 0
    assert key["tn"] == 1 and key["success"]["f1"] is None
