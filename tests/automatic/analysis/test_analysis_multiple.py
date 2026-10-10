"""Multiplicity adjustments and block-wise omnibus tests.

Known answers: the 15 p-values of Benjamini and Hochberg (1995, §4): at
0.05, BH rejects the 4 smallest, Bonferroni and Holm 3 (the threshold
arithmetic is written out below); hand-computed Holm and BH adjustments;
Cochran's Q with two arms equals McNemar's chi-square (b - c)^2 / (b + c);
hand-computed Friedman statistic.
"""

from itertools import permutations, product

import numpy as np
import pytest

from levi.automatic.analysis import multiple
from levi.automatic.analysis._core import AnalysisInputError

BH_EXAMPLE = [
    0.0001,
    0.0004,
    0.0019,
    0.0095,
    0.0201,
    0.0278,
    0.0298,
    0.0344,
    0.0459,
    0.3240,
    0.4262,
    0.5719,
    0.6528,
    0.7590,
    1.000,
]


def test_benjamini_hochberg_example_rejections():
    # BH: largest i with p(i) <= 0.05 i / 15 is i = 4 (0.0095 <= 0.01333;
    # 0.0201 > 0.01667, 0.0278 > 0.02, ..., 0.0459 > 0.03).
    assert sum(multiple.benjamini_hochberg(BH_EXAMPLE)["reject"]) == 4
    # Bonferroni: 0.05 / 15 = 0.00333 keeps 3. Holm: 0.0095 > 0.05 / 12.
    assert sum(multiple.bonferroni(BH_EXAMPLE)["reject"]) == 3
    assert sum(multiple.holm(BH_EXAMPLE)["reject"]) == 3


def test_hand_computed_adjustments():
    p = [0.01, 0.04, 0.03, 0.005]
    # Holm: sorted 0.005, 0.01, 0.03, 0.04 times 4, 3, 2, 1 = 0.02, 0.03,
    # 0.06, 0.04; running max 0.02, 0.03, 0.06, 0.06.
    assert multiple.holm(p)["adjusted"] == pytest.approx([0.03, 0.06, 0.06, 0.02])
    # BH: times 4/1, 4/2, 4/3, 4/4 = 0.02, 0.02, 0.04, 0.04; running min
    # from the top keeps them.
    assert multiple.benjamini_hochberg(p)["adjusted"] == pytest.approx(
        [0.02, 0.04, 0.04, 0.02]
    )
    assert multiple.bonferroni(p)["adjusted"] == pytest.approx([0.04, 0.16, 0.12, 0.02])


def test_adjustment_properties_on_random_families():
    rng = np.random.Generator(np.random.PCG64(1))
    for _ in range(200):
        p = rng.random(rng.integers(1, 12)) ** 2
        holm = np.array(multiple.holm(p)["adjusted"])
        bonf = np.array(multiple.bonferroni(p)["adjusted"])
        bh = np.array(multiple.benjamini_hochberg(p)["adjusted"])
        assert (holm >= p - 1e-15).all() and (bh >= p - 1e-15).all()
        assert (holm <= bonf + 1e-15).all()
        assert (bh <= holm + 1e-15).all()
        perm = rng.permutation(len(p))
        assert np.allclose(np.array(multiple.holm(p[perm])["adjusted"]), holm[perm])
        assert np.allclose(
            np.array(multiple.benjamini_hochberg(p[perm])["adjusted"]), bh[perm]
        )


def test_adjustment_edges():
    out = multiple.holm([0.01, None, float("nan"), 0.2])
    assert (
        out["family_size"] == 2
        and out["adjusted"][1] is None
        and out["adjusted"][2] is None
    )
    assert out["adjusted"][0] == pytest.approx(0.02)
    assert multiple.holm([])["adjusted"] == []
    assert multiple.benjamini_hochberg([0.5])["adjusted"] == [0.5]
    assert multiple.bonferroni([1.0, 1.0])["adjusted"] == [1.0, 1.0]
    with pytest.raises(AnalysisInputError):
        multiple.holm([1.2])
    with pytest.raises(AnalysisInputError):
        multiple.holm([float("inf")])
    assert multiple.benjamini_hochberg([0.01])["exploratory"] is True
    assert multiple.holm([0.01])["exploratory"] is False


def test_cochran_q_with_two_arms_equals_mcnemar_chi_square():
    # 9 blocks only A succeeds, 3 only B, 10 both, 8 neither: (9-3)^2/12 = 3.
    rows = [[1, 0]] * 9 + [[0, 1]] * 3 + [[1, 1]] * 10 + [[0, 0]] * 8
    assert multiple.cochran_q_statistic(np.array(rows, dtype=float)) == pytest.approx(
        3.0
    )


def test_cochran_q_hand_computed_and_permutation_p_against_full_enumeration():
    x = np.array([[1, 1, 0], [1, 0, 0], [1, 1, 1], [1, 0, 0], [0, 1, 0]], dtype=float)
    # Column totals C = 4, 3, 1; row totals R = 2, 1, 3, 1, 1; N = 8.
    # Q = 2 (3 (16 + 9 + 1) - 64) / (3 * 8 - (4 + 1 + 9 + 1 + 1)) = 28 / 8.
    assert multiple.cochran_q_statistic(x) == pytest.approx(3.5)
    out = multiple.cochran_q(x.tolist(), seed=0, permutations=50_000)
    observed = 3.5
    per_row = [sorted(set(permutations(r))) for r in x.tolist()]
    hits = total = 0
    weights = [
        6 // len(rows) for rows in per_row
    ]  # each distinct order has 3!/len multiplicity
    for combo in product(*per_row):
        w = int(np.prod(weights))
        total += w
        if (
            multiple.cochran_q_statistic(np.array(combo, dtype=float))
            >= observed - 1e-9
        ):
            hits += w
    exact = hits / total
    assert out["p_permutation"] == pytest.approx(exact, abs=0.01)
    assert out["statistic"] == pytest.approx(3.5) and out["df"] == 2


def test_friedman_hand_computed_without_ties():
    x = np.array([[1.0, 2.0, 3.0], [1.0, 3.0, 2.0], [2.0, 3.0, 1.0], [1.0, 2.0, 3.0]])
    # Rank sums 5, 10, 9; 12 / (b k (k+1)) sum R^2 - 3 b (k+1)
    # = 12 / 48 * (25 + 100 + 81) - 48 = 51.5 - 48 = 3.5.
    assert multiple.friedman_statistic(x) == pytest.approx(3.5)


def test_friedman_tie_correction_hand_computed():
    x = np.array([[1.0, 1.0, 2.0], [1.0, 2.0, 3.0]])
    # Mid-ranks: (1.5, 1.5, 3), (1, 2, 3); sums 2.5, 3.5, 6; b = 2, k = 3.
    # sum (R - 4)^2 = 2.25 + 0.25 + 4 = 6.5; ties: one pair, 2^3 - 2 = 6.
    # 12 * 6.5 / (2 * 3 * 4 - 6 / 2) = 78 / 21.
    assert multiple.friedman_statistic(x) == pytest.approx(78 / 21)


def test_omnibus_determinism_and_edges():
    rows = [[1, 0, 1], [0, 0, 1], [1, 1, 1], [0, 0, 1], [1, 0, 1], [0, 1, 1]]
    a = multiple.cochran_q(rows, seed=4)
    assert a == multiple.cochran_q(rows, seed=4)
    assert a["p_permutation"] > 0
    constant = multiple.cochran_q([[1, 1], [0, 0]], seed=0)
    assert constant["available"] is False
    small = multiple.friedman([[1.0, 2.0]], seed=0)
    assert small["available"] is False
    dropped = multiple.friedman(
        [[1.0, 2.0], [None, 1.0], [3.0, 1.0], [2.0, 5.0]], seed=0
    )
    assert dropped["dropped"] == 1 and dropped["blocks"] == 3
    with pytest.raises(AnalysisInputError):
        multiple.cochran_q([[1, 2], [0, 1]], seed=0)
    with pytest.raises(AnalysisInputError):
        multiple.friedman([[1.0, 2.0], [1.0]], seed=0)
    with pytest.raises(TypeError):
        multiple.friedman([[1.0, 2.0], [2.0, 1.0]])


def test_permutation_p_is_near_the_chi_square_for_many_blocks():
    rng = np.random.Generator(np.random.PCG64(8))
    x = (rng.random((200, 3)) < [0.4, 0.5, 0.6]).astype(float)
    out = multiple.cochran_q(x.tolist(), seed=1, permutations=20_000)
    assert out["p_permutation"] == pytest.approx(out["p_chi_square"], abs=0.02)
