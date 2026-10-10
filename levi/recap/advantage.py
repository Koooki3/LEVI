"""RLinf's RECAP advantage, per episode, in numpy (no torch).

Transcribed from RLinf commit 807e5fd,
``examples/offline_rl/advantage_labeling/recap/process/compute_advantages.py``
(``compute_advantages_for_dataset``, phase 2, and ``save_advantages_to_dataset``)
and ``rlinf/algorithms/offline/process/advantage.py``. Apache-2.0, RLinf
Authors. For frame t of an episode of n frames, lookahead N:

    num_valid = min(N, n - t);  pad = t + N >= n
    V_next    = 0 if pad else V(o_{t+N})
    R_raw     = G_t if pad else G_t - G_{t+N}          (gamma == 1)
              = sum_i<num_valid gamma^i r_{t+i}         (gamma != 1)
    R         = (R_raw - ret_min) / (ret_max - ret_min) - 1   (-0.5 if the range is empty)
    A_t       = R + gamma^num_valid * V_next - V(o_t)

The label is ``A_t >= threshold`` (RECAP's inclusive rule), and every frame of
an ``sft`` dataset is positive regardless of the threshold.
"""

from __future__ import annotations

import numpy as np

from ..conversion.outputs.recap_value import episode_rewards

# The version of the computation LEVI itself does after the worker: returns,
# return-range normalisation, advantages, thresholds and labels (this module
# and jobs._publish). Every change to that logic MUST increment it: results
# of different versions are never merged (levi/recap/signature.py), while a
# plain LEVI upgrade that leaves it alone keeps subset merges possible.
RECAP_COMPUTE_VERSION = 1

__all__ = [
    "RECAP_COMPUTE_VERSION",
    "episode_advantages",
    "episode_rewards",
    "label",
    "normalizer",
    "quantile_threshold",
]


def normalizer(ret_min: float, ret_max: float):
    """RLinf's ``normalize``: maps [ret_min, ret_max] to [-1, 0]."""
    ret_range = ret_max - ret_min

    def normalize(x):
        if ret_range <= 0:
            return -0.5
        return (x - ret_min) / ret_range - 1.0

    return normalize


def episode_advantages(
    values,
    returns,
    rewards,
    *,
    lookahead: int,
    gamma: float,
    ret_min: float,
    ret_max: float,
    discount_next_value: bool = True,
) -> dict[str, np.ndarray]:
    """Per-frame advantage of one episode, frame by frame exactly as RLinf's
    loop (float64 throughout, like its Python floats)."""
    values = np.asarray(values, dtype=np.float64)
    returns = np.asarray(returns, dtype=np.float64)
    rewards = np.asarray(rewards, dtype=np.float64)
    n = len(values)
    if len(returns) != n or len(rewards) != n:
        raise ValueError("values, returns and rewards must have one entry per frame")
    if lookahead < 1:
        raise ValueError("lookahead must be at least 1")
    normalize = normalizer(ret_min, ret_max)
    gamma_powers = np.array([gamma**i for i in range(lookahead)], dtype=np.float64)
    out = {
        key: np.zeros(n, dtype=np.float64)
        for key in ("advantage", "value_next", "reward_sum", "reward_sum_raw")
    }
    out["num_valid_rewards"] = np.zeros(n, dtype=np.int64)
    for t in range(n):
        next_t = t + lookahead
        is_next_pad = next_t >= n
        num_valid = min(lookahead, n - t)
        v_curr = float(values[t])
        v_next = 0.0 if is_next_pad else float(values[next_t])
        if abs(gamma - 1.0) < 1e-8:
            raw = (
                float(returns[t])
                if is_next_pad
                else float(returns[t]) - float(returns[next_t])
            )
        else:
            raw = float(np.sum(gamma_powers[:num_valid] * rewards[t : t + num_valid]))
        reward_sum = normalize(raw)
        gamma_k = gamma**num_valid if discount_next_value else 1.0
        out["advantage"][t] = reward_sum + gamma_k * v_next - v_curr
        out["value_next"][t] = v_next
        out["reward_sum"][t] = reward_sum
        out["reward_sum_raw"][t] = raw
        out["num_valid_rewards"][t] = num_valid
    return out


def quantile_threshold(scores, positive_fraction: float) -> float:
    """RLinf ``quantile_threshold``: the top ``positive_fraction`` of scores
    lie at or above ``percentile(scores, (1 - positive_fraction) * 100)``."""
    return float(
        np.percentile(np.asarray(scores), (1.0 - float(positive_fraction)) * 100.0)
    )


def label(advantage, threshold: float, *, sft: bool = False) -> np.ndarray:
    """RECAP's inclusive label; an ``sft`` dataset is positive everywhere."""
    advantage = np.asarray(advantage)
    if sft:
        return np.ones(advantage.shape, dtype=bool)
    return advantage >= threshold
