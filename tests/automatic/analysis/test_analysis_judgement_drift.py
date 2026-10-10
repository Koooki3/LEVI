"""Verdict agreement, failure modes, early stop and drift diagnostics.

Known answers are hand computations written per test. The agreement rates
are also compared with ``levi.live.stats.agreement``, which was written
separately for the live page and defines the same rates.
"""

import math
from itertools import permutations

import numpy as np
import pytest

from levi.automatic.analysis import drift, failures, verdicts
from levi.automatic.analysis._core import AnalysisInputError, z_of
from levi.live import stats


def test_cohen_kappa_hand_computed():
    # 20 agree on success, 15 on failure, 5 + 10 disagree: po = 0.7,
    # pe = (25 * 30 + 25 * 20) / 2500 = 0.5, kappa = 0.4.
    a = ["s"] * 25 + ["f"] * 25
    b = ["s"] * 20 + ["f"] * 5 + ["s"] * 10 + ["f"] * 15
    out = verdicts.cohen_kappa(a, b)
    assert out["observed"] == pytest.approx(0.7)
    assert out["expected"] == pytest.approx(0.5)
    assert out["kappa"] == pytest.approx(0.4)
    assert verdicts.cohen_kappa(a, a)["kappa"] == pytest.approx(1.0)
    assert verdicts.cohen_kappa(["s"] * 4, ["s"] * 4)["kappa"] is None  # pe = 1
    assert verdicts.cohen_kappa([None, "s"], ["s", "s"])["dropped"] == 1


def test_kappa_is_symmetric_and_invariant_to_label_names():
    rng = np.random.Generator(np.random.PCG64(1))
    a = rng.integers(0, 3, 60).tolist()
    b = rng.integers(0, 3, 60).tolist()
    k = verdicts.cohen_kappa(a, b)["kappa"]
    assert verdicts.cohen_kappa(b, a)["kappa"] == pytest.approx(k)
    rename = {0: "x", 1: "y", 2: "z"}
    assert verdicts.cohen_kappa([rename[v] for v in a], [rename[v] for v in b])[
        "kappa"
    ] == pytest.approx(k)


def test_agreement_rates_match_the_live_page_definitions():
    rng = np.random.Generator(np.random.PCG64(2))
    person = rng.choice(["success", "failure"], 80).tolist()
    verdict = rng.choice(["success", "failure", "undecided", None], 80).tolist()
    ours = verdicts.agreement(list(zip(person, verdict, strict=True)))

    def live_verdict(v):
        if v is None:
            return None
        return {"undecided": True} if v == "undecided" else {"outcome": v}

    live = stats.agreement(
        [(p, live_verdict(v)) for p, v in zip(person, verdict, strict=True)]
    )
    # The live page rounds its rates to 3 digits.
    assert ours["agreement"]["rate"] == pytest.approx(live["rate"], abs=5e-4)
    assert ours["false_success"]["rate"] == pytest.approx(
        live["false_success"]["rate"], abs=5e-4
    )
    assert ours["missed_success"]["rate"] == pytest.approx(
        live["missed_success"]["rate"], abs=5e-4
    )
    assert ours["false_success"]["wilson"] == live["false_success"]["wilson95"]
    assert ours["undecided"] == live["undecided"]


def test_agreement_matrix_and_edges():
    pairs = (
        [("success", "success")] * 3
        + [("failure", "success")] * 2
        + [("failure", "undecided")]
        + [(None, "success")]
    )
    out = verdicts.agreement(pairs)
    assert out["matrix"]["failure"]["success"] == 2 and out["unlabelled"] == 1
    assert out["false_success"]["rate"] == pytest.approx(2 / 3)
    assert out["agreement"]["rate"] == pytest.approx(3 / 5)
    assert verdicts.agreement([])["available"] is False
    with pytest.raises(AnalysisInputError):
        verdicts.agreement([("maybe", "success")])


def test_misjudgement_by_arm_flags_a_judge_that_errs_in_one_arm():
    good = [("success", "success")] * 20 + [("failure", "failure")] * 20
    bad = [("success", "failure")] * 15 + [("failure", "failure")] * 25
    out = verdicts.misjudgement_by_arm({"A": good, "B": bad}, seed=1)
    assert out["statistic"] == pytest.approx(15 / 40)
    assert out["p_value"] < 0.01 and out["warning"] is True
    same = verdicts.misjudgement_by_arm({"A": good, "B": good}, seed=1)
    assert same["p_value"] == pytest.approx(1.0) and same["warning"] is False
    assert verdicts.misjudgement_by_arm({"A": good}, seed=1)["available"] is False


def test_rogan_gladen_hand_computed():
    # (0.6 + 0.8 - 1) / (0.9 + 0.8 - 1) = 0.4 / 0.7.
    assert verdicts.rogan_gladen(0.6, 0.9, 0.8)["corrected_rate"] == pytest.approx(
        4 / 7
    )
    clipped = verdicts.rogan_gladen(0.1, 0.9, 0.8)
    assert clipped["corrected_rate"] == 0.0 and any(
        c["code"] == "clipped" for c in clipped["caveats"]
    )
    assert verdicts.rogan_gladen(0.5, 0.5, 0.5)["available"] is False
    perfect = verdicts.rogan_gladen(0.37, 1.0, 1.0)
    assert perfect["corrected_rate"] == pytest.approx(0.37)
    with pytest.raises(AnalysisInputError):
        verdicts.rogan_gladen(1.2, 0.9, 0.9)


def test_failure_modes_hand_counts():
    trials = (
        [{"arm": "A", "success": True}] * 6
        + [
            {
                "arm": "A",
                "success": False,
                "stop_reason": "budget",
                "failure_mode": "drop",
            }
        ]
        * 3
        + [{"arm": "A", "success": False, "stop_reason": "fault"}]
        + [{"arm": "A", "success": None}]
        + [
            {
                "arm": "B",
                "success": False,
                "stop_reason": "budget",
                "failure_mode": "miss",
            }
        ]
        * 2
    )
    out = failures.failure_modes(trials)
    a = out["arms"]["A"]
    assert a["trials"] == 11 and a["failures"] == 4 and a["unknown_outcome"] == 1
    assert (
        a["by_stop_reason"]["budget"]["k"] == 3
        and a["by_stop_reason"]["budget"]["n"] == 11
    )
    assert a["by_failure_mode"]["unclassified"]["k"] == 1
    assert a["by_stop_reason"]["budget"]["wilson"] == stats.wilson(3, 11, z_of(0.95))
    with pytest.raises(AnalysisInputError):
        failures.failure_modes([{"success": False}])


def test_early_stop_uses_only_failed_complete_controls():
    episodes = [
        {
            "arm": "A",
            "control": True,
            "truth": "failure",
            "would_stop": True,
            "slot": 1,
        },
        {
            "arm": "A",
            "control": True,
            "truth": "failure",
            "would_stop": False,
            "slot": 2,
        },
        {
            "arm": "A",
            "control": True,
            "truth": "failure",
            "would_stop": True,
            "complete": False,
            "slot": 3,
        },
        {
            "arm": "A",
            "control": True,
            "truth": "success",
            "would_stop": True,
            "slot": 4,
        },
        {
            "arm": "A",
            "control": False,
            "truth": "failure",
            "early_stop": True,
            "saved_steps": 40,
        },
        {"arm": "A", "control": False, "truth": "failure", "early_stop": False},
        {
            "arm": "A",
            "control": False,
            "truth": "success",
            "early_stop": True,
            "saved_steps": 20,
        },
        {
            "arm": "B",
            "control": True,
            "truth": "failure",
            "would_stop": False,
            "slot": 1,
        },
        {
            "arm": "B",
            "control": True,
            "truth": "failure",
            "would_stop": False,
            "slot": 2,
        },
        {"arm": "C", "control": False, "truth": "success", "early_stop": True},
    ]
    out = failures.early_stop(episodes)
    rate = out["arms"]["A"]["false_early_stop_rate"]
    assert rate["status"] == "available" and (rate["k"], rate["n"]) == (1, 2)
    assert rate["left_out_incomplete"] == 1
    assert out["arms"]["A"]["treated_false_early_stop_lower_bound"][
        "rate"
    ] == pytest.approx(0.5)
    assert out["arms"]["A"]["saved_steps"]["total"] == 60
    assert out["arms"]["C"]["false_early_stop_rate"]["status"] == "unavailable"
    assert out["arms"]["C"]["false_early_stop_rate"]["rate"] is None
    pair = out["paired_false_early_stop"]["A|B"]
    assert pair["n_pairs"] == 2 and pair["cells"]["only_a"] == 1
    assert out["paired_false_early_stop"]["A|C"]["status"] == "unavailable"


def test_mann_kendall_hand_computed_and_exact_enumeration():
    out = drift.mann_kendall([1, 2, 3, 4])
    # S = 6 (all 6 pairs increase); only the sorted and reversed orders of
    # four distinct values reach |S| = 6: p = 2 / 24.
    assert out["s"] == 6 and out["p_value"] == pytest.approx(2 / 24)
    values = [0.3, 0.5, 0.5, 0.2, 0.8]
    s = drift.mann_kendall_s(values)
    brute = [
        sum(np.sign(p[j] - p[i]) for i in range(5) for j in range(i + 1, 5))
        for p in permutations(values)
    ]
    assert drift.mann_kendall(values)["p_value"] == pytest.approx(
        np.mean(np.abs(brute) >= abs(s))
    )
    assert drift.mann_kendall([1, 2])["available"] is False


def test_mann_kendall_normal_mode_is_close_to_exact_at_eight():
    x = [0.2, 0.4, 0.3, 0.5, 0.6, 0.55, 0.7, 0.9]
    exact = drift.mann_kendall(x)["p_value"]
    s = drift.mann_kendall_s(x)
    var = 8 * 7 * 21 / 18
    approx = math.erfc((abs(s) - 1) / math.sqrt(var) / math.sqrt(2))
    assert exact == pytest.approx(approx, abs=0.03)
    assert drift.mann_kendall(x * 2)["mode"] == "normal"


def test_reference_drift_flags_a_strong_trend_and_not_a_flat_series():
    falling = [(10, 10), (9, 10), (9, 10), (7, 10), (5, 10), (4, 10), (2, 10), (1, 10)]
    out = drift.reference_drift(falling)
    assert out["warning"] is True
    assert out["second_minus_first"]["difference"] == pytest.approx(12 / 40 - 35 / 40)
    flat = drift.reference_drift([(5, 10)] * 8)
    assert flat["warning"] is False


def test_arm_time_interaction_exact_and_constant_case():
    constant = [(5, 10, 7, 10)] * 6
    out = drift.arm_time_interaction(constant, seed=0)
    assert out["statistic"] == pytest.approx(0.0) and out["p_value"] == pytest.approx(
        1.0
    )
    flipping = [(2, 10, 8, 10)] * 4 + [(8, 10, 2, 10)] * 4
    out = drift.arm_time_interaction(flipping, seed=0)
    # Only the true split and its mirror reach |statistic| = 1.2: 2 / C(8,4).
    assert out["statistic"] == pytest.approx(-1.2) and out["p_value"] == pytest.approx(
        2 / 70
    )
    assert drift.arm_time_interaction(constant[:3], seed=0)["available"] is False


def test_carryover_and_overall_warning():
    trials = (
        [{"arm": "A", "previous_arm": "B", "success": True}] * 9
        + [{"arm": "A", "previous_arm": "B", "success": False}] * 1
        + [{"arm": "A", "previous_arm": "C", "success": True}] * 1
        + [{"arm": "A", "previous_arm": "C", "success": False}] * 9
        + [{"arm": "A", "previous_arm": None, "success": True}]
    )
    out = drift.carryover(trials)
    assert out["arms"]["A"]["by_previous_arm"]["None"]["n"] == 1
    assert out["arms"]["A"]["fisher_p"] < 0.01 and out["warning"] is True
    summary = drift.drift_warning(out, drift.reference_drift([(5, 10)] * 4))
    assert summary["warning"] is True and summary["raised_by"] == ["carryover"]
    assert drift.drift_warning()["warning"] is False
