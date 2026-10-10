"""Failure modes and early termination, per arm.

Failure modes: level 1 is the closed ``stop_reason``; level 2 is the
post-hoc review's ``failure_mode`` (``unclassified`` when absent). Each
count gets a Wilson interval over the arm's trials; no test is run, and the
report adds a qualitative description (Kress-Gazit et al. 2024,
arXiv:2409.09491).

Early termination: the false early stop rate is measured only on control
episodes (the stop was recorded, not executed, and the episode ran to its
end) that a person labelled failed: the share in which the judge *would*
have stopped. Without such episodes it is ``unavailable`` and carries no
number; the treated episodes give only a lower bound. Arms are compared by
layout slot with McNemar's test on slots where both arms have such a
control episode (see ``paired.mcnemar``).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from levi.live.stats import wilson as _live_wilson

from . import _core, paired
from ._core import AnalysisInputError, caveat, result

MODULE = "failures"
UNCLASSIFIED = "unclassified"


def _share(k: int, n: int, z: float) -> dict:
    return {
        "k": k,
        "n": n,
        "rate": k / n if n else None,
        "wilson": _live_wilson(k, n, z) if n else None,
    }


def failure_modes(trials, *, level: float = _core.DEFAULT_LEVEL) -> dict:
    """``trials``: mappings with ``arm``, ``success`` (bool or None),
    ``stop_reason`` and optional ``failure_mode``. Failed trials are
    counted by ``stop_reason`` and by ``failure_mode``; trials with an
    unknown outcome are counted apart."""
    z = _core.z_of(_core.check_level(level))
    by_arm: dict = defaultdict(
        lambda: {"n": 0, "unknown": 0, "reasons": Counter(), "modes": Counter()}
    )
    for row in trials:
        arm = row.get("arm")
        if arm is None:
            raise AnalysisInputError("every trial needs an arm")
        entry = by_arm[str(arm)]
        entry["n"] += 1
        outcome = row.get("success")
        if outcome is None:
            entry["unknown"] += 1
            continue
        if outcome:
            continue
        entry["reasons"][str(row.get("stop_reason") or "unknown")] += 1
        entry["modes"][str(row.get("failure_mode") or UNCLASSIFIED)] += 1
    arms = {}
    for arm, entry in sorted(by_arm.items()):
        n = entry["n"]
        arms[arm] = {
            "trials": n,
            "unknown_outcome": entry["unknown"],
            "failures": sum(entry["reasons"].values()),
            "by_stop_reason": {
                k: _share(v, n, z) for k, v in sorted(entry["reasons"].items())
            },
            "by_failure_mode": {
                k: _share(v, n, z) for k, v in sorted(entry["modes"].items())
            },
        }
    return result(
        "summary",
        "failure_modes",
        MODULE,
        references=["kressgazit2024", "wilson1927"],
        exploratory=True,
        caveats=[caveat("descriptive_only", "counts with intervals; no test is run")],
        level=level,
        arms=arms,
    )


def early_stop(episodes, *, level: float = _core.DEFAULT_LEVEL) -> dict:
    """``episodes``: mappings with ``arm``, ``control`` (bool), ``truth``
    (``success``/``failure``/None), for controls ``would_stop`` (bool) and
    ``complete`` (ran to its end and its control record is sealed), for
    treated episodes ``early_stop`` (bool), and optionally ``slot`` (the
    layout slot, for pairing) and ``saved_steps``."""
    z = _core.z_of(_core.check_level(level))
    arms: dict = defaultdict(list)
    for row in episodes:
        if row.get("arm") is None:
            raise AnalysisInputError("every episode needs an arm")
        arms[str(row["arm"])].append(row)
    out = {}
    slots: dict = defaultdict(dict)
    for arm, rows in sorted(arms.items()):
        controls = [r for r in rows if r.get("control")]
        failed = [
            r
            for r in controls
            if r.get("truth") == "failure" and r.get("complete", True)
        ]
        left_out = sum(
            1
            for r in controls
            if r.get("truth") == "failure" and not r.get("complete", True)
        )
        treated = [
            r
            for r in rows
            if not r.get("control") and r.get("truth") in ("success", "failure")
        ]
        fp = sum(1 for r in treated if r.get("early_stop") and r["truth"] == "failure")
        tn = sum(
            1 for r in treated if not r.get("early_stop") and r["truth"] == "failure"
        )
        tp = sum(1 for r in treated if r.get("early_stop") and r["truth"] == "success")
        saved = [
            float(r["saved_steps"])
            for r in rows
            if r.get("early_stop") and r.get("saved_steps") is not None
        ]
        if failed:
            k = sum(1 for r in failed if r.get("would_stop"))
            rate = {
                "status": "available",
                "source": "control",
                **_share(k, len(failed), z),
            }
        else:
            rate = {
                "status": "unavailable",
                "source": "control",
                "reason": "no labelled control episode that truly failed and ran to its end",
                "k": None,
                "n": 0,
                "rate": None,
                "wilson": None,
            }
        rate["left_out_incomplete"] = left_out
        for r in failed:
            if r.get("slot") is not None:
                slots[r["slot"]][arm] = 1 if r.get("would_stop") else 0
        out[arm] = {
            "episodes": len(rows),
            "controls": len(controls),
            "false_early_stop_rate": rate,
            "treated_false_early_stop_lower_bound": _share(fp, fp + tn, z),
            "precision": _share(tp, tp + fp, z),
            "saved_steps": {
                "episodes": len(saved),
                "total": sum(saved) if saved else None,
                "mean": sum(saved) / len(saved) if saved else None,
            },
        }
    comparisons = {}
    names = sorted(arms)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            pairs = [
                (v[first], v[second])
                for v in slots.values()
                if first in v and second in v
            ]
            if pairs:
                comparisons[f"{first}|{second}"] = paired.mcnemar(pairs)
            else:
                comparisons[f"{first}|{second}"] = {
                    "status": "unavailable",
                    "reason": "no slot with failed controls in both arms",
                }
    return result(
        "summary",
        "early_stop",
        MODULE,
        references=["wilson1927", "mcnemar1947"],
        exploratory=True,
        caveats=[
            caveat(
                "truncation",
                "an early stop hides failures that would have happened after it; only controls show them",
            )
        ],
        level=level,
        arms=out,
        paired_false_early_stop=comparisons,
    )
