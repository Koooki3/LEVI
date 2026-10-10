"""Independent reference computations for the analysis-library tests.

Every function here is written a different way from the library (plain
Python loops, exact fractions or brute-force enumeration, no shared helper)
so a test that compares the two is not the implementation checking itself.
Nothing here imports ``levi.automatic.analysis``.
"""

from __future__ import annotations

import math
from fractions import Fraction
from itertools import combinations, product


def binom_pmf(j: int, n: int, p: float) -> float:
    return math.comb(n, j) * p**j * (1 - p) ** (n - j)


def binom_upper_tail(k: int, n: int, p: float) -> float:
    """P(X >= k), summed term by term."""
    return sum(binom_pmf(j, n, p) for j in range(k, n + 1))


def binom_lower_tail(k: int, n: int, p: float) -> float:
    return sum(binom_pmf(j, n, p) for j in range(k + 1))


def fisher_p_float(a: int, n1: int, c: int, n2: int) -> float:
    """Two-sided Fisher p-value the way R's fisher.test does it: float
    hypergeometric probabilities and a 1 + 1e-7 relative tolerance."""
    k = a + c
    lo, hi = max(0, k - n2), min(n1, k)
    denom = math.comb(n1 + n2, k)
    probs = {
        x: math.comb(n1, x) * math.comb(n2, k - x) / denom for x in range(lo, hi + 1)
    }
    obs = probs[a]
    return min(1.0, sum(p for p in probs.values() if p <= obs * (1 + 1e-7)))


def mcnemar_exact_p(f: int, g: int) -> Fraction:
    """Exact conditional McNemar p-value as an exact fraction."""
    m = f + g
    if m == 0:
        return Fraction(1)
    small = min(f, g)
    tail = Fraction(sum(math.comb(m, j) for j in range(small + 1)), 2**m)
    return min(Fraction(1), 2 * tail)


def mcnemar_power_bruteforce(
    n: int, p0: float, p1: float, rho: float, alpha: float
) -> float:
    """Sum over every (f, g) of the trinomial probability times the
    rejection indicator: no conditioning on the discordant total."""
    p11 = p0 * p1 + rho * math.sqrt(p0 * (1 - p0) * p1 * (1 - p1))
    pf, pg = p0 - p11, p1 - p11
    pc = 1 - pf - pg
    total = 0.0
    for f in range(n + 1):
        for g in range(n + 1 - f):
            if mcnemar_exact_p(f, g) <= Fraction(alpha).limit_denominator(10**9):
                c = n - f - g
                coef = math.factorial(n) // (
                    math.factorial(f) * math.factorial(g) * math.factorial(c)
                )
                total += coef * pf**f * pg**g * pc**c
    return total


def fisher_power_bruteforce(n: int, p0: float, p1: float, alpha: float) -> float:
    total = 0.0
    for x0 in range(n + 1):
        w0 = binom_pmf(x0, n, p0)
        for x1 in range(n + 1):
            if fisher_p_float(x0, n, x1, n) <= alpha:
                total += w0 * binom_pmf(x1, n, p1)
    return total


def signed_rank_p_bruteforce(diffs: list[float]) -> float:
    """Two-sided exact signed-rank p-value by listing every sign pattern."""
    nz = [d for d in diffs if d != 0]
    absd = [abs(d) for d in nz]
    ranks = []
    for v in absd:
        less = sum(1 for w in absd if w < v)
        equal = sum(1 for w in absd if w == v)
        ranks.append(less + (equal + 1) / 2)
    observed = sum(r for r, d in zip(ranks, nz, strict=True) if d > 0)
    centre = sum(ranks) / 2
    hits = 0
    total = 0
    for signs in product((0, 1), repeat=len(nz)):
        s = sum(r for r, keep in zip(ranks, signs, strict=True) if keep)
        total += 1
        if abs(s - centre) >= abs(observed - centre) - 1e-9:
            hits += 1
    return hits / total


def rank_sum_p_bruteforce(a: list[float], b: list[float]) -> float:
    """Two-sided exact rank-sum p-value by listing every relabelling."""
    pooled = a + b
    ranks = []
    for v in pooled:
        less = sum(1 for w in pooled if w < v)
        equal = sum(1 for w in pooled if w == v)
        ranks.append(less + (equal + 1) / 2)
    nb = len(b)
    centre = nb * (len(pooled) + 1) / 2
    observed = sum(ranks[len(a) :])
    hits = 0
    total = 0
    for chosen in combinations(range(len(pooled)), nb):
        s = sum(ranks[i] for i in chosen)
        total += 1
        if abs(s - centre) >= abs(observed - centre) - 1e-9:
            hits += 1
    return hits / total


def cliffs_delta_bruteforce(a, b) -> float:
    more = sum(1 for x in a for y in b if y > x)
    less = sum(1 for x in a for y in b if y < x)
    return (more - less) / (len(a) * len(b))


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def beta_prob_greater_numeric(aa, ba, ab, bb, grid: int = 20000) -> float:
    """P(Beta(ab, bb) > Beta(aa, ba)) by midpoint integration of
    pdf_A(x) * (1 - CDF_B(x)), CDF by a running sum (no closed form)."""

    def logpdf(x, a, b):
        return (
            (a - 1) * math.log(x)
            + (b - 1) * math.log1p(-x)
            - (math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b))
        )

    h = 1.0 / grid
    xs = [(i + 0.5) * h for i in range(grid)]
    pdf_b = [math.exp(logpdf(x, ab, bb)) for x in xs]
    cdf_b = []
    running = 0.0
    for v in pdf_b:
        cdf_b.append(running + v * h / 2)
        running += v * h
    return sum(
        math.exp(logpdf(x, aa, ba)) * (1 - c) * h
        for x, c in zip(xs, cdf_b, strict=True)
    )
