"""Agreement between the automatic verdict and a person's label.

Per arm: the confusion matrix (person success/failure by verdict
success/failure/undecided/none), the agreement rate with a Wilson interval,
the false-success and missed-success rates (definitions as in
``levi.live.stats.agreement``: denominators include "undecided"), and
Cohen's kappa on the judged episodes (Cohen 1960,
doi:10.1177/001316446002000104). Kappa depends on the success rate (high
agreement with low kappa, Feinstein and Cicchetti 1990,
doi:10.1016/0895-4356(90)90158-L), so it is always shown beside the raw
agreement rate.

If the verdict errs more in one arm than in another, arm differences in the
automatic success rate partly come from the judge: ``misjudgement_by_arm``
tests that with a permutation of arm labels. ``rogan_gladen`` corrects an
observed rate by the judge's sensitivity and specificity (Rogan and Gladen
1978, doi:10.1093/oxfordjournals.aje.a112510); a sensitivity analysis only.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from levi.live.stats import wilson as _live_wilson

from . import _core
from ._core import AnalysisInputError, caveat, result

MODULE = "verdicts"
PERSON = ("success", "failure")
VERDICT = ("success", "failure", "undecided", "none")


def _norm(label, allowed, name):
    if label is None:
        return "none"
    if isinstance(label, bool):
        return "success" if label else "failure"
    label = str(label)
    if label not in allowed:
        raise AnalysisInputError(f"{name} label {label!r} is not one of {allowed}")
    return label


def _share(k: int, n: int, z: float) -> dict:
    return {
        "k": k,
        "n": n,
        "rate": k / n if n else None,
        "wilson": _live_wilson(k, n, z) if n else None,
    }


def cohen_kappa(rater_a, rater_b) -> dict:
    """Cohen's kappa of two label sequences over any categories; pairs with
    a None on either side are dropped and counted."""
    pairs = [(x, y) for x, y in zip(rater_a, rater_b, strict=True)]
    kept = [(x, y) for x, y in pairs if x is not None and y is not None]
    dropped = len(pairs) - len(kept)
    n = len(kept)
    po = pe = kappa = None
    caveats = [
        caveat(
            "kappa_prevalence",
            "kappa depends on the category rates; read it with the agreement rate",
        )
    ]
    if n:
        po = sum(x == y for x, y in kept) / n
        ca = Counter(x for x, _ in kept)
        cb = Counter(y for _, y in kept)
        pe = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / (n * n)
        kappa = None if pe == 1 else (po - pe) / (1 - pe)
    else:
        caveats.append(caveat("empty", "no pair with both labels"))
    if dropped:
        caveats.append(
            caveat("dropped_missing", f"{dropped} pairs dropped", count=dropped)
        )
    return result(
        "estimate",
        "cohen_kappa",
        MODULE,
        references=["cohen1960kappa", "feinstein1990kappa"],
        exploratory=True,
        caveats=caveats,
        available=n > 0 and kappa is not None,
        n=n,
        dropped=dropped,
        observed=po,
        expected=pe,
        kappa=kappa,
    )


def agreement(pairs, *, level: float = _core.DEFAULT_LEVEL) -> dict:
    """``pairs = [(person, verdict), ...]`` with ``person`` in success /
    failure (None: not labelled, left out and counted) and ``verdict`` in
    success / failure / undecided / None."""
    level = _core.check_level(level)
    z = _core.z_of(level)
    matrix = {p: dict.fromkeys(VERDICT, 0) for p in PERSON}
    unlabelled = 0
    for person, verdict in pairs:
        if person is None:
            unlabelled += 1
            continue
        matrix[_norm(person, PERSON, "person")][
            _norm(verdict, VERDICT[:3], "verdict")
        ] += 1
    s, f = matrix["success"], matrix["failure"]
    judged = s["success"] + s["failure"] + f["success"] + f["failure"]
    agree = s["success"] + f["failure"]
    kappa = cohen_kappa(
        [
            p
            for p in PERSON
            for v in ("success", "failure")
            for _ in range(matrix[p][v])
        ],
        [
            v
            for p in PERSON
            for v in ("success", "failure")
            for _ in range(matrix[p][v])
        ],
    )
    caveats = [
        caveat(
            "kappa_prevalence",
            "kappa depends on the success rate; read it with the agreement rate",
        )
    ]
    if unlabelled:
        caveats.append(
            caveat(
                "unlabelled",
                f"{unlabelled} episodes without a person label",
                count=unlabelled,
            )
        )
    return result(
        "estimate",
        "verdict_agreement",
        MODULE,
        references=["cohen1960kappa", "feinstein1990kappa", "wilson1927"],
        exploratory=True,
        caveats=caveats,
        available=judged > 0,
        level=level,
        matrix=matrix,
        labelled=sum(sum(r.values()) for r in matrix.values()),
        unlabelled=unlabelled,
        judged=judged,
        agreement=_share(agree, judged, z),
        false_success=_share(
            f["success"], f["success"] + f["failure"] + f["undecided"], z
        ),
        missed_success=_share(
            s["failure"] + s["undecided"],
            s["success"] + s["failure"] + s["undecided"],
            z,
        ),
        undecided=s["undecided"] + f["undecided"],
        no_verdict=s["none"] + f["none"],
        kappa=kappa["kappa"],
        kappa_expected=kappa["expected"],
    )


def misjudgement_by_arm(arms: dict, *, seed: int, permutations: int = 20_000) -> dict:
    """Per-arm agreement and a permutation test of whether the judge's
    error rate (on judged episodes) differs between arms. Statistic: the
    largest minus the smallest per-arm error rate."""
    seed = _core.check_seed(seed)
    per_arm = {name: agreement(pairs) for name, pairs in arms.items()}
    errors = []
    labels = []
    for idx, (name, pairs) in enumerate(arms.items()):
        for person, verdict in pairs:
            if person is None:
                continue
            p, v = (
                _norm(person, PERSON, "person"),
                _norm(verdict, VERDICT[:3], "verdict"),
            )
            if v in PERSON:
                errors.append(int(p != v))
                labels.append(idx)
    caveats = [
        caveat("diagnostic", "a diagnostic of the judge; it does not change any result")
    ]
    k = len(arms)
    err = np.array(errors, dtype=float)
    lab = np.array(labels, dtype=np.int64)
    counts = np.bincount(lab, minlength=k) if len(lab) else np.zeros(k)
    if k < 2 or (counts == 0).any():
        return result(
            "test",
            "misjudgement_by_arm",
            MODULE,
            references=[],
            exploratory=True,
            caveats=caveats
            + [caveat("too_small", "needs judged episodes in at least two arms")],
            available=False,
            arms=per_arm,
            p_value=None,
            seed=seed,
        )

    def spread(labels_) -> float:
        rates = np.bincount(labels_, weights=err, minlength=k) / counts
        return float(rates.max() - rates.min())

    observed = spread(lab)
    rng = _core.generator(seed)
    draws = min(int(permutations), 100_000)
    hits = sum(spread(rng.permutation(lab)) >= observed - 1e-12 for _ in range(draws))
    return result(
        "test",
        "misjudgement_by_arm",
        MODULE,
        references=[],
        exploratory=True,
        caveats=caveats,
        available=True,
        arms=per_arm,
        statistic=observed,
        p_value=(1 + hits) / (1 + draws),
        permutations=draws,
        seed=seed,
        warning=(1 + hits) / (1 + draws) < 0.05,
    )


def rogan_gladen(observed_rate: float, sensitivity: float, specificity: float) -> dict:
    """Corrected rate ``(p + Sp - 1) / (Se + Sp - 1)``, clipped to [0, 1]."""
    for name, v in (
        ("observed_rate", observed_rate),
        ("sensitivity", sensitivity),
        ("specificity", specificity),
    ):
        if v is None or not 0 <= float(v) <= 1:
            raise AnalysisInputError(f"{name} must be in [0, 1]")
    youden = sensitivity + specificity - 1
    caveats = [
        caveat("sensitivity_analysis", "a sensitivity analysis, not a primary result")
    ]
    if youden <= 0:
        return result(
            "estimate",
            "rogan_gladen",
            MODULE,
            references=["rogan1978"],
            exploratory=True,
            caveats=caveats
            + [caveat("uninformative_judge", "Se + Sp <= 1: no correction possible")],
            available=False,
            corrected_rate=None,
        )
    raw = (observed_rate + specificity - 1) / youden
    clipped = min(1.0, max(0.0, raw))
    if clipped != raw:
        caveats.append(
            caveat(
                "clipped",
                "the corrected rate fell outside [0, 1] and was clipped",
                raw=raw,
            )
        )
    return result(
        "estimate",
        "rogan_gladen",
        MODULE,
        references=["rogan1978"],
        exploratory=True,
        caveats=caveats,
        available=True,
        observed_rate=observed_rate,
        sensitivity=sensitivity,
        specificity=specificity,
        corrected_rate=clipped,
    )
