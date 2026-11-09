"""Calibration, reliability, and the pre-registered threshold rule (design section 2).

Only tune items calibrate: an item whose ``split`` is anything but ``tune`` is refused,
``confirm`` with its own error. The artifact is bound to the model, the prompt and the
harness snapshot by sha256; a run whose key differs is refused.

Threshold rule: among grid values tau (and ESC_ALL), keep those whose paired difference
to the teacher-only arm has a clustered-bootstrap one-sided lower bound >= -delta, and
take the one with the fewest external tokens (ties: the larger, more conservative tau).
ESC_ALL has difference 0 by construction, so the rule always has an answer.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from .contracts import CALIBRATION_CONTRACT

ESC_ALL: Final = "ESC_ALL"
DEFAULT_GRID: Final = (0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.99)
EPS = 1e-6
_SHA = re.compile(r"[0-9a-f]{64}")


class ConfirmDataError(ValueError):
    """Confirm data reached calibration (exit code 3 for a CLI)."""

    exit_code = 3


class KeyMismatch(ValueError):
    """The calibration artifact was made for another model, prompt or harness snapshot."""


@dataclass(frozen=True)
class RunKey:
    model_id: str
    model_sha256: str
    prompt_sha256: str
    harness_sha256: str

    def __post_init__(self):
        for name in ("model_sha256", "prompt_sha256", "harness_sha256"):
            if not _SHA.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a lowercase sha256 hex digest")
        if not self.model_id:
            raise ValueError("model_id is required")

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "model_sha256": self.model_sha256,
            "prompt_sha256": self.prompt_sha256,
            "harness_sha256": self.harness_sha256,
        }


@dataclass(frozen=True)
class Item:
    """One tune question. Teacher fields come from the panel arm on the same question."""

    episode: str
    split: str
    p_raw: float
    local_correct: bool
    teacher_correct: bool
    teacher_tokens: int  # external tokens the teacher spent on this question (known)


def _clip(p: float) -> float:
    return min(1.0 - EPS, max(EPS, p))


def apply_temperature(p_raw: float, temperature: float) -> float:
    p = _clip(p_raw)
    z = math.log(p / (1.0 - p)) / temperature
    return 1.0 / (1.0 + math.exp(-z))


def nll(items: Sequence[Item], temperature: float) -> float:
    total = 0.0
    for it in items:
        p = _clip(apply_temperature(it.p_raw, temperature))
        total -= math.log(p if it.local_correct else 1.0 - p)
    return total / len(items)


def fit_temperature(items: Sequence[Item]) -> float:
    """Minimise NLL over T in [0.05, 20] by golden-section on log T. Deterministic."""
    if not items:
        raise ValueError("no items to fit")
    lo, hi = math.log(0.05), math.log(20.0)
    g = (math.sqrt(5) - 1) / 2
    a, b = hi - g * (hi - lo), lo + g * (hi - lo)
    fa, fb = nll(items, math.exp(a)), nll(items, math.exp(b))
    for _ in range(80):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - g * (hi - lo)
            fa = nll(items, math.exp(a))
        else:
            lo, a, fa = a, b, fb
            b = lo + g * (hi - lo)
            fb = nll(items, math.exp(b))
    return math.exp((lo + hi) / 2)


def _check_split(items: Sequence[Item], confirm_episodes: Iterable[str] = ()) -> None:
    confirm = set(confirm_episodes)
    for it in items:
        if it.split == "confirm" or it.episode in confirm:
            raise ConfirmDataError(
                f"confirm data cannot calibrate (episode {it.episode!r})"
            )
        if it.split != "tune":
            raise ValueError(f"only split=tune calibrates, got {it.split!r}")


def loeo_probabilities(items: Sequence[Item]) -> list[float]:
    """Calibrated probability of each item from a temperature fitted without its episode."""
    episodes = sorted({it.episode for it in items})
    if len(episodes) < 2:
        raise ValueError("leave-one-episode-out needs at least two episodes")
    temps = {e: fit_temperature([i for i in items if i.episode != e]) for e in episodes}
    return [apply_temperature(it.p_raw, temps[it.episode]) for it in items]


def reliability_bins(
    probs: Sequence[float], correct: Sequence[bool], n_bins: int = 5
) -> list[dict]:
    """Equal-mass bins: count, mean confidence, accuracy."""
    order = sorted(range(len(probs)), key=lambda i: (probs[i], i))
    out = []
    for b in range(n_bins):
        idx = order[b * len(order) // n_bins : (b + 1) * len(order) // n_bins]
        if not idx:
            continue
        out.append(
            {
                "n": len(idx),
                "confidence": sum(probs[i] for i in idx) / len(idx),
                "accuracy": sum(1 for i in idx if correct[i]) / len(idx),
            }
        )
    return out


def ece(probs: Sequence[float], correct: Sequence[bool], n_bins: int = 5) -> float:
    n = len(probs)
    return sum(
        b["n"] / n * abs(b["confidence"] - b["accuracy"])
        for b in reliability_bins(probs, correct, n_bins)
    )


def accuracy_coverage(
    probs: Sequence[float], correct: Sequence[bool]
) -> list[tuple[float, float]]:
    """(coverage, accuracy) when answering the k most confident items, k = 1..n."""
    order = sorted(range(len(probs)), key=lambda i: (-probs[i], i))
    out, hit = [], 0
    for k, i in enumerate(order, 1):
        hit += bool(correct[i])
        out.append((k / len(order), hit / k))
    return out


def aurc(probs: Sequence[float], correct: Sequence[bool]) -> float:
    """Area under the risk-coverage curve (mean of 1 - accuracy over coverage): lower is better."""
    curve = accuracy_coverage(probs, correct)
    return sum(1.0 - acc for _, acc in curve) / len(curve)


def _quality(items: Sequence[Item], probs: Sequence[float], tau) -> float:
    """Graded system quality: local correctness if accepted, teacher correctness if escalated."""
    s = 0
    for it, p in zip(items, probs, strict=True):
        s += it.local_correct if (tau != ESC_ALL and p >= tau) else it.teacher_correct
    return s / len(items)


def _external_tokens(items: Sequence[Item], probs: Sequence[float], tau) -> int:
    return sum(
        it.teacher_tokens
        for it, p in zip(items, probs, strict=True)
        if tau == ESC_ALL or p < tau
    )


def _lcb(items, probs, tau, delta_level: float, boots: int, seed: int) -> float:
    """One-sided lower bound of D(tau) = Q(tau) - Q_T, clustered bootstrap over episodes."""
    by_ep: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        by_ep.setdefault(it.episode, []).append(i)
    eps = sorted(by_ep)
    diff = [
        (it.local_correct if p >= tau else it.teacher_correct) - it.teacher_correct
        for it, p in zip(items, probs, strict=True)
    ]
    rng = random.Random(seed)
    stats = []
    for _ in range(boots):
        total = n = 0
        for _ in eps:
            idx = by_ep[eps[rng.randrange(len(eps))]]
            total += sum(diff[i] for i in idx)
            n += len(idx)
        stats.append(total / n)
    stats.sort()
    k = max(0, min(boots - 1, math.floor((1.0 - delta_level) * boots)))
    return stats[k]


@dataclass(frozen=True)
class Calibration:
    shape: str
    temperature: float
    tau: float | str  # a grid value or ESC_ALL
    key: RunKey
    delta: float
    lcb_level: float
    grid: tuple[float, ...]
    seed: int
    boots: int
    n_items: int
    n_episodes: int
    episodes_sha256: str
    table: tuple[
        dict, ...
    ]  # per grid value: tau, lcb, feasible, external_tokens, quality
    ece_loeo: float
    ece_full: float
    contract: str = CALIBRATION_CONTRACT

    def require_key(self, key: RunKey) -> None:
        if self.key != key:
            raise KeyMismatch(
                "calibration was made for another model, prompt or harness snapshot"
            )

    def to_dict(self) -> dict:
        return {
            "contract": self.contract,
            "shape": self.shape,
            "temperature": self.temperature,
            "tau": self.tau,
            "key": self.key.as_dict(),
            "delta": self.delta,
            "lcb_level": self.lcb_level,
            "grid": list(self.grid),
            "seed": self.seed,
            "boots": self.boots,
            "n_items": self.n_items,
            "n_episodes": self.n_episodes,
            "episodes_sha256": self.episodes_sha256,
            "table": list(self.table),
            "ece_loeo": self.ece_loeo,
            "ece_full": self.ece_full,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


def fit(
    items: Sequence[Item],
    *,
    shape: str,
    key: RunKey,
    delta: float,
    grid: Sequence[float] = DEFAULT_GRID,
    lcb_level: float = 0.90,
    boots: int = 2000,
    seed: int = 0,
    confirm_episodes: Iterable[str] = (),
) -> Calibration:
    """Fit the temperature and choose tau by the registered rule. Tune items only."""
    _check_split(items, confirm_episodes)
    if delta < 0:
        raise ValueError("delta must be >= 0")
    probs = loeo_probabilities(items)
    rows, best = [], None
    for tau in [*sorted(grid), ESC_ALL]:
        tokens = _external_tokens(items, probs, tau)
        if tau == ESC_ALL:
            lcb, feasible = 0.0, True  # D is identically 0
        else:
            lcb = _lcb(items, probs, tau, lcb_level, boots, seed)
            feasible = lcb >= -delta
        rows.append(
            {
                "tau": tau,
                "lcb": lcb,
                "feasible": feasible,
                "external_tokens": tokens,
                "quality": _quality(items, probs, tau),
            }
        )
        rank = (tokens, -(math.inf if tau == ESC_ALL else tau))
        if feasible and (best is None or rank < best[0]):
            best = (rank, tau)
    temperature = fit_temperature(items)
    correct = [it.local_correct for it in items]
    full = [apply_temperature(it.p_raw, temperature) for it in items]
    eps = sorted({it.episode for it in items})
    return Calibration(
        shape=shape,
        temperature=temperature,
        tau=best[1],
        key=key,
        delta=delta,
        lcb_level=lcb_level,
        grid=tuple(sorted(grid)),
        seed=seed,
        boots=boots,
        n_items=len(items),
        n_episodes=len(eps),
        episodes_sha256=hashlib.sha256("\n".join(eps).encode()).hexdigest(),
        table=tuple(rows),
        ece_loeo=ece(probs, correct),
        ece_full=ece(full, correct),
    )
