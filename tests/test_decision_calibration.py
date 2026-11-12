"""Calibration, reliability metrics and the registered threshold rule (P3; U6, U7, U17, U22)."""

import dataclasses
import math
import random

import pytest

from levi.decision.calibration import (
    ESC_ALL,
    Calibration,
    ConfirmDataError,
    Item,
    KeyMismatch,
    RunKey,
    accuracy_coverage,
    apply_temperature,
    aurc,
    ece,
    fit,
    fit_temperature,
    loeo_probabilities,
    reliability_bins,
)

H = "a" * 64
KEY = RunKey("m", H, H, H)


def sigmoid(z):
    return 1 / (1 + math.exp(-z))


def synth(n_eps=6, per=40, temp=1.0, seed=1, teacher_acc=0.95, tokens=100):
    """Raw confidence p; the answer is right with probability sigmoid(logit(p)/temp)."""
    rng = random.Random(seed)
    items = []
    for e in range(n_eps):
        for _ in range(per):
            p = rng.uniform(0.55, 0.995)
            true_p = sigmoid(math.log(p / (1 - p)) / temp)
            items.append(
                Item(
                    f"e{e}",
                    "tune",
                    p,
                    rng.random() < true_p,
                    rng.random() < teacher_acc,
                    tokens,
                )
            )
    return items


def test_temperature_recovers_the_known_value_on_synthetic_data():
    assert fit_temperature(synth(n_eps=10, per=200, temp=1.0)) == pytest.approx(
        1.0, abs=0.2
    )
    assert fit_temperature(synth(n_eps=10, per=200, temp=3.0)) == pytest.approx(
        3.0, abs=0.7
    )


def test_calibration_lowers_ece_of_an_overconfident_model():
    items = synth(n_eps=10, per=200, temp=3.0)
    correct = [i.local_correct for i in items]
    raw = [i.p_raw for i in items]
    T = fit_temperature(items)
    cal = [apply_temperature(p, T) for p in raw]
    assert ece(cal, correct) < ece(raw, correct) / 2


def test_ece_and_bins_on_known_values():
    probs, correct = [0.9] * 10, [True] * 8 + [False] * 2
    assert ece(probs, correct, n_bins=1) == pytest.approx(0.1)
    bins = reliability_bins(probs, correct, n_bins=5)
    assert [b["n"] for b in bins] == [2] * 5 and sum(b["n"] for b in bins) == 10
    assert ece([1.0, 1.0], [True, False], n_bins=1) == pytest.approx(0.5)


def test_accuracy_coverage_and_aurc():
    probs, correct = [0.9, 0.8, 0.7, 0.6], [True, True, False, True]
    assert accuracy_coverage(probs, correct) == [
        (0.25, 1.0),
        (0.5, 1.0),
        (0.75, pytest.approx(2 / 3)),
        (1.0, 0.75),
    ]
    assert aurc(probs, correct) == pytest.approx(1 - (1 + 1 + 2 / 3 + 0.75) / 4)
    assert aurc([0.9, 0.1], [True, False]) < aurc(
        [0.1, 0.9], [True, False]
    )  # good ranking is lower


def test_loeo_never_uses_the_items_own_episode():
    items = synth(n_eps=4, per=30, temp=2.0)
    got = loeo_probabilities(items)
    for idx in (0, 35, 70, 119):
        ep = items[idx].episode
        T = fit_temperature([i for i in items if i.episode != ep])
        assert got[idx] == pytest.approx(apply_temperature(items[idx].p_raw, T))
    with pytest.raises(ValueError):
        loeo_probabilities([i for i in items if i.episode == "e0"])


# --- U6: confirm data never calibrates ----------------------------------------------------


def test_U6_split_confirm_is_refused_with_its_own_error():
    items = synth(n_eps=4, per=10)
    items[7] = dataclasses.replace(items[7], split="confirm")
    with pytest.raises(ConfirmDataError) as e:
        fit(items, shape="binary", key=KEY, delta=0.02, boots=20)
    assert e.value.exit_code == 3


def test_U6_an_episode_on_the_confirm_ledger_is_refused_and_other_splits_too():
    items = synth(n_eps=4, per=10)
    with pytest.raises(ConfirmDataError):
        fit(
            items,
            shape="binary",
            key=KEY,
            delta=0.02,
            boots=20,
            confirm_episodes={"e2"},
        )
    items[0] = dataclasses.replace(items[0], split="production")
    with pytest.raises(ValueError, match="only split=tune"):
        fit(items, shape="binary", key=KEY, delta=0.02, boots=20)


# --- U7: the artifact is bound to model, prompt and harness snapshot ------------------------


def test_U7_a_one_character_change_in_any_bound_hash_is_refused():
    c = fit(synth(n_eps=4, per=10), shape="binary", key=KEY, delta=0.02, boots=20)
    c.require_key(KEY)
    for field in ("model_sha256", "prompt_sha256", "harness_sha256"):
        bad = dataclasses.replace(KEY, **{field: "a" * 63 + "b"})
        with pytest.raises(KeyMismatch):
            c.require_key(bad)
    with pytest.raises(KeyMismatch):
        c.require_key(dataclasses.replace(KEY, model_id="other"))


def test_a_key_needs_real_sha256_digests():
    for bad in ("", "abc", "A" * 64, "g" * 64):
        with pytest.raises(ValueError):
            RunKey("m", bad, H, H)


# --- the threshold rule -------------------------------------------------------------------


def test_U17_a_bad_local_model_degrades_to_escalate_everything():
    items = [Item(f"e{i % 5}", "tune", 0.99, False, True, 50) for i in range(100)]
    c = fit(items, shape="binary", key=KEY, delta=0.02, boots=100)
    assert c.tau == ESC_ALL
    assert c.table[-1]["tau"] == ESC_ALL and c.table[-1]["feasible"]


def test_U17_the_rule_uses_the_lower_bound_not_the_point_estimate():
    # one local error in 100 items over 5 episodes: point difference -0.01 passes
    # delta=0.015, the 90% lower bound (about -0.02) does not
    items = [Item(f"e{i % 5}", "tune", 0.999, i != 3, True, 10) for i in range(100)]
    c = fit(
        items, shape="binary", key=KEY, delta=0.015, grid=(0.5, 0.6), boots=2000, seed=0
    )
    first = c.table[0]
    assert first["quality"] >= 0.99  # the point estimate alone would accept it
    assert first["lcb"] < -0.015 and not first["feasible"]
    assert c.tau == ESC_ALL
    # with a margin wide enough for the lower bound, the cheap threshold is chosen
    c2 = fit(
        items, shape="binary", key=KEY, delta=0.05, grid=(0.5, 0.6), boots=2000, seed=0
    )
    assert c2.tau == 0.6  # same tokens (0) for 0.5 and 0.6: the larger one wins the tie


def test_the_fewest_external_tokens_among_feasible_thresholds_wins():
    # local is perfect, so every threshold is feasible; 0.99 escalates the fewest
    # cases only if nothing is below it, which is not the cheapest: the lowest escalates 0
    items = [
        Item(f"e{i % 4}", "tune", 0.6 + (i % 40) / 100, True, True, 10 + i)
        for i in range(120)
    ]
    c = fit(items, shape="binary", key=KEY, delta=0.0, boots=200)
    feasible = [r for r in c.table if r["feasible"]]
    cheapest = min(r["external_tokens"] for r in feasible)
    chosen = next(r for r in c.table if r["tau"] == c.tau)
    assert chosen["external_tokens"] == cheapest


def test_ties_go_to_the_larger_more_conservative_threshold():
    items = [Item(f"e{i % 4}", "tune", 0.999, True, True, 5) for i in range(80)]
    c = fit(items, shape="binary", key=KEY, delta=0.0, boots=100)
    zero_cost = [
        r["tau"] for r in c.table if r["feasible"] and r["external_tokens"] == 0
    ]
    assert len(zero_cost) > 1 and c.tau == max(zero_cost)


def test_U22_the_artifact_is_byte_identical_for_the_same_input():
    items = synth(n_eps=8, per=12, temp=2.0, seed=5)
    runs = [
        fit(items, shape="binary", key=KEY, delta=0.03, boots=40, seed=3)
        for _ in range(4)
    ]
    assert len({r.to_json() for r in runs}) == 1
    assert len({r.sha256() for r in runs}) == 1
    assert isinstance(runs[0], Calibration)


def test_the_artifact_records_loeo_and_full_fit_estimates_and_the_episode_set():
    c = fit(synth(n_eps=5, per=20), shape="binary", key=KEY, delta=0.02, boots=30)
    d = c.to_dict()
    assert d["contract"] == "levi.decision.calibration.v1"
    assert (
        d["n_episodes"] == 5 and d["n_items"] == 100 and len(d["episodes_sha256"]) == 64
    )
    assert d["ece_loeo"] >= 0 and d["ece_full"] >= 0
    assert d["key"]["prompt_sha256"] == H
