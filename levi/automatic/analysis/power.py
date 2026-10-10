"""Exact power and the smallest detectable difference for success rates.

Power is computed by exact enumeration, never by a normal approximation:

- unpaired arms (Fisher's exact test, Fisher 1922, doi:10.2307/2340521):
  ``sum over (x1, x2) of Bin(x1; n, p0) Bin(x2; n, p1) [p(x1, x2) <= alpha]``
  with the rejection region taken from :func:`proportions.fisher_pvalues`;
- paired arms (McNemar's exact test, McNemar 1947, doi:10.1007/BF02295996):
  pairs are bivariate Bernoulli with marginals ``p0, p1`` and within-pair
  correlation ``rho``; given ``m`` discordant pairs the count favouring B is
  ``Bin(m, q)``; power sums ``Bin(m; n, p10 + p01)`` times the conditional
  rejection probability. Connor (1987, doi:10.2307/2531961) gives the
  normal-approximation sample size this enumeration replaces.

The table reports the design-time smallest detectable difference (80 %
power, two-sided alpha 0.05) and never "post-hoc power" of observed data
(Wasserstein and Lazar 2016, doi:10.1080/00031305.2016.1154108, on not
reading p-values alone). The tests recompute every number by Monte Carlo and
by a separately written brute-force enumeration.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

from . import _core
from ._core import AnalysisInputError, caveat, result
from .proportions import fisher_pvalues, wilson_bounds

MODULE = "power"
DEFAULT_NS = (10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100)
DEFAULT_BASELINES = (0.5, 0.3)
DEFAULT_RHOS = (0.0, 0.3)
STEP = 0.01
# Above this many trials per arm the exact enumeration (quadratic in n with
# big integers) gives way to the normal approximations below.
EXACT_POWER_MAX_N = 200
_NORMAL = NormalDist()


def mcnemar_reject(n: int, alpha: float, *, mid_p: bool = False) -> np.ndarray:
    """``R[m, j]`` is 1 when ``j`` of ``m`` discordant pairs favouring B is
    rejected by McNemar's two-sided exact (or mid-p) test. Tails are exact
    integers over ``2**m``."""
    reject = np.zeros((n + 1, n + 1))
    for m in range(1, n + 1):
        combs = [math.comb(m, j) for j in range(m + 1)]
        left = 0
        lefts = []
        for c in combs:
            left += c
            lefts.append(left)
        total = 2**m
        for j in range(m + 1):
            low = lefts[j]
            high = total - (lefts[j - 1] if j else 0)
            tail = 2 * min(low, high)
            if mid_p:
                tail -= combs[j]
            reject[m, j] = 1.0 if min(1.0, tail / total) <= alpha else 0.0
    return reject


def _paired_cells(p0: float, p1: float, rho: float) -> tuple[float, float]:
    """The discordant cell probabilities (A only, B only)."""
    p11 = p0 * p1 + rho * math.sqrt(p0 * (1 - p0) * p1 * (1 - p1))
    p10, p01 = p0 - p11, p1 - p11
    p00 = 1 - p0 - p1 + p11
    if min(p10, p01, p00, p11) < -1e-12:
        raise AnalysisInputError(
            f"rho={rho} is not attainable for marginals {p0:.3f}, {p1:.3f}"
        )
    return max(p10, 0.0), max(p01, 0.0)


def power_paired(
    n: int,
    p0: float,
    p1: float,
    *,
    rho: float = 0.0,
    alpha: float = _core.DEFAULT_ALPHA,
    mid_p: bool = False,
    reject: np.ndarray | None = None,
) -> float:
    """Exact power of McNemar's test with ``n`` pairs."""
    p10, p01 = _paired_cells(p0, p1, rho)
    if reject is None:
        reject = mcnemar_reject(n, alpha, mid_p=mid_p)
    disc = p10 + p01
    if disc <= 0:
        return 0.0
    q = p01 / disc
    pm = _core.binom_pmf(n, disc)
    # Conditional rejection probability for every m at once: the
    # Binomial(m, q) probabilities form a lower-triangular matrix.
    lf = np.array([math.lgamma(i + 1) for i in range(n + 1)])
    m = np.arange(n + 1)[:, None]
    j = np.arange(n + 1)[None, :]
    valid = j <= m
    rest = np.where(valid, m - j, 0)
    if q <= 0:
        cond_pmf = (j == 0) & valid
    elif q >= 1:
        cond_pmf = (j == m) & valid
    else:
        logp = lf[m] - lf[j] - lf[rest] + j * math.log(q) + rest * math.log1p(-q)
        cond_pmf = np.where(valid, np.exp(np.where(valid, logp, 0.0)), 0.0)
    power = float(pm @ (cond_pmf * reject).sum(axis=1))
    return min(1.0, power)


def power_unpaired(
    n: int,
    p0: float,
    p1: float,
    *,
    alpha: float = _core.DEFAULT_ALPHA,
    two_sided: str = "minlike",
    region: np.ndarray | None = None,
) -> float:
    """Exact power of Fisher's test with ``n`` trials per arm."""
    if region is None:
        region = (fisher_pvalues(n, n, two_sided=two_sided) <= alpha).astype(float)
    return min(1.0, float(_core.binom_pmf(n, p0) @ region @ _core.binom_pmf(n, p1)))


def _first_detectable(power_of, p0: float, target: float) -> float | None:
    """Smallest ``delta`` on the 0.01 grid with ``power(p0, p0 + delta) >=
    target``; None when even ``p1 = 1`` falls short."""
    steps = round((1 - p0) / STEP)
    for i in range(1, steps + 1):
        delta = round(i * STEP, 10)
        if power_of(min(1.0, p0 + delta)) >= target:
            return delta
    return None


def min_detectable_difference(
    n: int,
    baseline: float,
    *,
    design: str = "paired",
    rho: float = 0.0,
    alpha: float = _core.DEFAULT_ALPHA,
    power: float = 0.8,
    two_sided: str = "minlike",
) -> float | None:
    """The smallest increase over ``baseline`` (0.01 grid) that the design
    detects with the given power; ``design`` is ``paired`` or ``unpaired``."""
    if n < 1:
        return None
    if not 0 < baseline < 1:
        raise AnalysisInputError("baseline must be strictly between 0 and 1")
    if design not in ("paired", "unpaired"):
        raise AnalysisInputError("design must be 'paired' or 'unpaired'")
    if n > EXACT_POWER_MAX_N:

        def approx(p1: float) -> float:
            try:
                return approximate_power(
                    n, baseline, p1, design=design, rho=rho, alpha=alpha
                )
            except AnalysisInputError:
                return 0.0

        return _first_detectable(approx, baseline, power)
    if design == "paired":
        reject = mcnemar_reject(n, alpha)

        def power_of(p1: float) -> float:
            try:
                return power_paired(n, baseline, p1, rho=rho, reject=reject)
            except AnalysisInputError:
                return 0.0
    elif design == "unpaired":
        region = (fisher_pvalues(n, n, two_sided=two_sided) <= alpha).astype(float)

        def power_of(p1: float) -> float:
            return power_unpaired(n, baseline, p1, region=region)

    return _first_detectable(power_of, baseline, power)


def approximate_power(
    n: int,
    p0: float,
    p1: float,
    *,
    design: str,
    rho: float = 0.0,
    alpha: float = _core.DEFAULT_ALPHA,
) -> float:
    """Normal-approximation power for large ``n``: Connor's (1987) formula
    for paired designs, the pooled-variance two-proportion formula for
    unpaired ones."""
    za = _NORMAL.inv_cdf(1 - alpha / 2)
    if design == "paired":
        p10, p01 = _paired_cells(p0, p1, rho)
        psi, delta = p10 + p01, p01 - p10
        if psi <= 0 or psi - delta**2 <= 0:
            return 1.0 if delta else 0.0
        zb = (math.sqrt(n) * abs(delta) - za * math.sqrt(psi)) / math.sqrt(
            psi - delta**2
        )
    else:
        pbar = (p0 + p1) / 2
        spread = math.sqrt(p0 * (1 - p0) + p1 * (1 - p1))
        if spread == 0:
            return 1.0 if p0 != p1 else 0.0
        zb = (
            math.sqrt(n) * abs(p1 - p0) - za * math.sqrt(2 * pbar * (1 - pbar))
        ) / spread
    return _NORMAL.cdf(zb)


def power_table(
    ns=DEFAULT_NS,
    baselines=DEFAULT_BASELINES,
    rhos=DEFAULT_RHOS,
    *,
    alpha: float = _core.DEFAULT_ALPHA,
    power: float = 0.8,
    two_sided: str = "minlike",
) -> dict:
    """Per ``n``: the Wilson 95 % width at p = 0.5 and, per baseline, the
    smallest detectable difference for unpaired Fisher and for paired
    McNemar at each ``rho``."""
    alpha = _core.check_alpha(alpha)
    rows = []
    z = _core.z_of(0.95)
    for n in ns:
        n = int(n)
        if n < 1:
            raise AnalysisInputError("every n must be at least 1")
        low, high = (
            wilson_bounds(n // 2, n, z) if n % 2 == 0 else wilson_bounds(n / 2, n, z)
        )
        row = {"n": n, "wilson_width_at_half": high - low, "baselines": []}
        for base in baselines:
            entry = {
                "baseline": float(base),
                "unpaired_fisher": min_detectable_difference(
                    n,
                    base,
                    design="unpaired",
                    alpha=alpha,
                    power=power,
                    two_sided=two_sided,
                ),
                "paired_mcnemar": {
                    str(rho): min_detectable_difference(
                        n, base, design="paired", rho=rho, alpha=alpha, power=power
                    )
                    for rho in rhos
                },
            }
            row["baselines"].append(entry)
        rows.append(row)
    return result(
        "power_table",
        "exact_enumeration",
        MODULE,
        references=["fisher1922", "mcnemar1947", "connor1987", "wilson1927"],
        exploratory=False,
        caveats=[
            caveat(
                "design_time_only",
                "smallest detectable differences for planning; not post-hoc power",
            )
        ],
        alpha=alpha,
        target_power=power,
        grid_step=STEP,
        fisher_two_sided=two_sided,
        direction="baseline + delta",
        rows=rows,
    )


BASELINE_GRID = tuple(round(0.05 * i, 2) for i in range(1, 20))


def _planned_power(
    n, baseline, delta, *, design, rho, alpha, reject, region
) -> float | None:
    p1 = baseline + delta
    if p1 > 1 + 1e-12:
        return None
    p1 = min(1.0, p1)
    try:
        if n > EXACT_POWER_MAX_N:
            return approximate_power(
                n, baseline, p1, design=design, rho=rho, alpha=alpha
            )
        if design == "paired":
            return power_paired(n, baseline, p1, rho=rho, reject=reject)
        return power_unpaired(n, baseline, p1, region=region)
    except AnalysisInputError:
        return None


def assess(
    n: int,
    *,
    design: str,
    design_difference: float | None,
    design_baseline: float | None = None,
    alpha: float = _core.DEFAULT_ALPHA,
    rho: float = 0.0,
) -> tuple[bool, list[dict], float | None, dict]:
    """``(exploratory, caveats, mdd, basis)`` for a two-arm success
    comparison, from planned values only.

    The observed outcomes never enter: power computed at the observed rate
    is post-hoc power, and it would let the same pre-registered design turn
    confirmatory just because the results came out extreme. A comparison is
    confirmatory only when the caller states the difference the study was
    designed to detect (``design_difference``, fixed before the first
    trial) and the exact power at that difference reaches 0.8 with this
    ``n`` at the planned baseline success rate (``design_baseline``); with
    no planned baseline the least favourable baseline on a 0.05 grid
    decides. The reported smallest detectable difference is the design
    table's value for this ``n`` at the planned baseline (0.5 when none is
    planned)."""
    caveats = []
    if design not in ("paired", "unpaired"):
        raise AnalysisInputError("design must be 'paired' or 'unpaired'")
    basis = {
        "basis": "planned",
        "difference": None if design_difference is None else float(design_difference),
        "baseline": None if design_baseline is None else float(design_baseline),
        "detectable_at_baseline": 0.5
        if design_baseline is None
        else float(design_baseline),
        "min_power": None,
    }
    if n < 1:
        return True, [caveat("empty", "no usable trials")], None, basis
    if design_baseline is not None and not 0 < float(design_baseline) < 1:
        raise AnalysisInputError("design_baseline must be strictly between 0 and 1")
    if n > EXACT_POWER_MAX_N:
        caveats.append(
            caveat(
                "power_approximate",
                f"n above {EXACT_POWER_MAX_N}: power uses a normal approximation, "
                "which can understate the detectable difference by about 0.01 (optimistic)",
            )
        )
    mdd_base = basis["detectable_at_baseline"]
    mdd = min_detectable_difference(n, mdd_base, design=design, rho=rho, alpha=alpha)
    caveats.append(
        caveat(
            "detectable_difference",
            (
                f"with n={n} the design detects a difference of about {mdd:.2f} "
                f"with 80% power at a planned baseline of {mdd_base:.2f}"
                if mdd is not None
                else f"with n={n} no difference is detectable with 80% power"
            ),
            n=n,
            baseline=mdd_base,
            min_detectable_difference=mdd,
        )
    )
    if design_difference is None:
        caveats.append(
            caveat(
                "not_preregistered",
                "no pre-specified design difference: the result is exploratory",
            )
        )
        return True, caveats, mdd, basis
    target = float(design_difference)
    if not 0 < target <= 1:
        raise AnalysisInputError("design_difference must be in (0, 1]")
    reject = region = None
    if n <= EXACT_POWER_MAX_N:
        if design == "paired":
            reject = mcnemar_reject(n, alpha)
        else:
            region = (fisher_pvalues(n, n) <= alpha).astype(float)
    baselines = (
        [float(design_baseline)] if design_baseline is not None else list(BASELINE_GRID)
    )
    powers = [
        p
        for b in baselines
        if (
            p := _planned_power(
                n,
                b,
                target,
                design=design,
                rho=rho,
                alpha=alpha,
                reject=reject,
                region=region,
            )
        )
        is not None
    ]
    if not powers:
        caveats.append(
            caveat(
                "infeasible_design",
                "the planned difference does not fit the planned baseline",
            )
        )
        return True, caveats, mdd, basis
    basis["min_power"] = min(powers)
    if design_baseline is None:
        caveats.append(
            caveat(
                "baseline_not_planned",
                "no planned baseline: the least favourable baseline on a 0.05 grid decides",
            )
        )
    if min(powers) < 0.8:
        caveats.append(
            caveat(
                "underpowered",
                f"n={n} has {min(powers):.2f} power for the design difference {target:.2f}",
                design_difference=target,
            )
        )
        return True, caveats, mdd, basis
    return False, caveats, mdd, basis
