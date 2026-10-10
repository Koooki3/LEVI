"""Movement smoothness of an end-effector trajectory (Balasubramanian,
Melendez-Calderon and Roby-Brami 2015, doi:10.1186/s12984-015-0090-9).

- SPARC, the spectral arc length of the speed profile: the arc length of
  the normalised Fourier magnitude spectrum up to an adaptive cut-off
  frequency (at most ``fc`` Hz, where the magnitude first stays below
  ``threshold``). Closer to 0 is smoother.
- LDLJ, the velocity-based log dimensionless jerk:
  ``-ln( T^3 / v_peak^2 * integral |d^2 v / dt^2|^2 dt )``; closer to 0 is
  smoother. A minimum-jerk point-to-point movement has LDLJ = -ln(720 /
  1.875^2) = -5.322 whatever its amplitude and duration.

Both are sensitive to the sampling rate and to where the movement is cut:
compare them only between trajectories sampled at the same rate and cut by
the same rule.
"""

from __future__ import annotations

import math

import numpy as np

from ._core import AnalysisInputError, caveat, result

MODULE = "movement"


def _positions(positions, rate_hz: float) -> np.ndarray:
    arr = np.asarray(positions, dtype=float)
    if arr.ndim == 1:
        arr = arr[:, None]
    if arr.ndim != 2:
        raise AnalysisInputError("positions must be (steps,) or (steps, dims)")
    if not np.isfinite(arr).all():
        raise AnalysisInputError(
            "positions must be finite (cut NaN gaps before calling)"
        )
    if rate_hz <= 0:
        raise AnalysisInputError("rate_hz must be positive")
    return arr


def sparc_of_speed(
    speed: np.ndarray,
    rate_hz: float,
    *,
    fc: float = 10.0,
    threshold: float = 0.05,
    pad_level: int = 4,
) -> float:
    """SPARC of a sampled speed profile."""
    speed = np.asarray(speed, dtype=float)
    n = len(speed)
    nfft = int(2 ** (math.ceil(math.log2(n)) + pad_level))
    freq = np.arange(nfft) * rate_hz / nfft
    mag = np.abs(np.fft.fft(speed, nfft))
    if mag.max() == 0:
        raise AnalysisInputError("the speed profile is all zero")
    mag = mag / mag.max()
    keep = freq <= fc
    freq, mag = freq[keep], mag[keep]
    above = np.nonzero(mag >= threshold)[0]
    last = above[-1] if len(above) else 0
    freq, mag = freq[: last + 1], mag[: last + 1]
    if len(freq) < 2:
        return 0.0
    dfreq = np.diff(freq) / (freq[-1] - freq[0])
    dmag = np.diff(mag)
    return float(-np.sqrt(dfreq**2 + dmag**2).sum())


def ldlj_of_velocity(velocity: np.ndarray, rate_hz: float) -> float:
    """Velocity-based LDLJ of a sampled velocity (steps x dims)."""
    dt = 1.0 / rate_hz
    duration = (len(velocity) - 1) * dt
    speed = np.linalg.norm(velocity, axis=1)
    peak = speed.max()
    if peak == 0 or duration <= 0:
        raise AnalysisInputError("the movement has no speed")
    jerk = np.gradient(np.gradient(velocity, dt, axis=0), dt, axis=0)
    integral = np.trapezoid((jerk**2).sum(axis=1), dx=dt)
    return float(-math.log(duration**3 / peak**2 * integral))


def smoothness(
    positions, *, rate_hz: float, fc: float = 10.0, threshold: float = 0.05
) -> dict:
    """SPARC and LDLJ of positions sampled at ``rate_hz``."""
    arr = _positions(positions, rate_hz)
    if len(arr) < 5:
        return result(
            "estimate",
            "smoothness",
            MODULE,
            references=["balasubramanian2015smoothness"],
            exploratory=True,
            caveats=[caveat("too_short", "needs at least 5 samples")],
            available=False,
            sparc=None,
            ldlj=None,
        )
    velocity = np.gradient(arr, 1.0 / rate_hz, axis=0)
    speed = np.linalg.norm(velocity, axis=1)
    return result(
        "estimate",
        "smoothness",
        MODULE,
        references=["balasubramanian2015smoothness"],
        exploratory=True,
        caveats=[
            caveat(
                "rate_sensitive",
                "compare only trajectories sampled at the same rate and cut by the same rule",
                rate_hz=rate_hz,
            )
        ],
        available=True,
        samples=len(arr),
        rate_hz=rate_hz,
        sparc=sparc_of_speed(speed, rate_hz, fc=fc, threshold=threshold),
        ldlj=ldlj_of_velocity(velocity, rate_hz),
        fc=fc,
        threshold=threshold,
    )
