"""Deterministic checks of one candidate action block against a contract.

``validate_action_block(actions, contract_id, metadata)`` returns a
:class:`ValidationResult` with one entry per check. The checks only look at
the numbers and the metadata handed in: nothing is read from disk, no pickle
is opened and no external code runs.

What a pass means is narrow (``LIMITS``): the block has the shape, finite
values, gripper values, representation, time scale and mask the contract asks
for. It does not show the motion is physically possible, safe on a robot or
that it carries out any instruction.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import schema

LIMITS = (
    "A pass only shows that the properties listed under checks hold: shape, "
    "finite values, gripper values, declared representation, time scale and "
    "action mask. It does not show that the motion is physically feasible or "
    "safe, or that it corresponds to any language instruction."
)

PASS, FAIL, WARN, SKIP = "pass", "fail", "warning", "skipped"
KNOWN_METADATA = ("representation", "time_scale", "action_mask")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class ValidationResult:
    contract_id: str
    contract_version: int | None
    passed: bool
    checks: tuple[Check, ...]
    limits: str = LIMITS

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status == FAIL)

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status == WARN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "limits": self.limits,
        }


def validate_action_block(
    actions: Any,
    contract_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> ValidationResult:
    """Check ``actions`` (a numpy array ``[H, D]``) against a registered
    contract. ``metadata`` may hold ``representation`` (``"absolute"`` or
    ``"delta"``), ``time_scale`` (the block's control rate divided by the
    contract's: 1.0 is exactly 10 Hz, 0.95 is 9.5 Hz) and ``action_mask``
    (``H`` flags, which steps count). A check that cannot run because an
    earlier one failed is reported as skipped; ``passed`` is false when any
    check fails (warnings do not fail)."""
    try:
        contract = schema.get(contract_id)
    except schema.UnknownContract as exc:
        return ValidationResult(
            str(contract_id),
            None,
            False,
            (Check("contract", FAIL, str(exc)),),
        )
    metadata = metadata if isinstance(metadata, Mapping) else {}
    checks = [Check("contract", PASS, f"{contract.ref}")]
    shaped = _array_and_shape(actions, contract, checks)
    values = actions if shaped else None
    finite = _finite(values, checks)
    _gripper(values if finite else None, contract, checks)
    _representation(metadata, contract, checks)
    _time_scale(metadata, contract, checks)
    _mask(metadata, contract, checks, values if shaped else None)
    _ranges(
        values if finite else None,
        metadata.get("representation"),
        contract,
        checks,
    )
    extra = sorted(k for k in metadata if k not in KNOWN_METADATA)
    if extra:
        checks.append(
            Check(
                "metadata", WARN, "ignored metadata keys: " + ", ".join(map(str, extra))
            )
        )
    return ValidationResult(
        contract.id,
        contract.version,
        not any(c.status == FAIL for c in checks),
        tuple(checks),
    )


def _array_and_shape(actions, contract, checks) -> bool:
    if not isinstance(actions, np.ndarray) or actions.dtype.kind not in "fiu":
        kind = (
            f"{type(actions).__name__} of dtype {actions.dtype}"
            if isinstance(actions, np.ndarray)
            else type(actions).__name__
        )
        checks.append(
            Check("array", FAIL, f"expected a numeric numpy array, got {kind}")
        )
        checks.append(Check("shape", SKIP, "not an array"))
        return False
    checks.append(Check("array", PASS, f"dtype {actions.dtype}"))
    want = (contract.action_horizon, contract.dimension)
    if actions.shape != want:
        checks.append(
            Check("shape", FAIL, f"shape {tuple(actions.shape)}, contract needs {want}")
        )
        return False
    checks.append(Check("shape", PASS, f"{want[0]} steps x {want[1]} dimensions"))
    return True


def _finite(values, checks) -> bool:
    if values is None:
        checks.append(Check("finite", SKIP, "no block of the right shape"))
        return False
    bad = int(np.size(values) - np.count_nonzero(np.isfinite(values)))
    if bad:
        checks.append(Check("finite", FAIL, f"{bad} value(s) are NaN or infinite"))
        return False
    checks.append(Check("finite", PASS, "all values finite"))
    return True


def _gripper(values, contract, checks) -> None:
    if values is None:
        checks.append(
            Check("gripper_values", SKIP, "no finite block of the right shape")
        )
        return
    column = values[:, contract.gripper_index]
    allowed = np.asarray(contract.gripper_values, dtype=float)
    odd = column[~np.isin(column, allowed)]
    if odd.size:
        shown = ", ".join(f"{v:g}" for v in sorted(set(odd.tolist()))[:5])
        checks.append(
            Check(
                "gripper_values",
                FAIL,
                f"gripper value(s) {shown}; the contract allows "
                + ", ".join(f"{v:g}" for v in contract.gripper_values),
            )
        )
        return
    checks.append(Check("gripper_values", PASS, "only allowed gripper values"))


def _representation(metadata, contract, checks) -> None:
    declared = metadata.get("representation")
    if declared is None:
        checks.append(
            Check(
                "representation",
                FAIL,
                f"not declared; the contract's block is {contract.representation}",
            )
        )
    elif declared != contract.representation:
        checks.append(
            Check(
                "representation",
                FAIL,
                f"declared {declared!r}, the contract's block is "
                f"{contract.representation!r}",
            )
        )
    else:
        checks.append(Check("representation", PASS, f"declared {declared}"))


def _time_scale(metadata, contract, checks) -> None:
    value = metadata.get("time_scale")
    if value is None:
        checks.append(
            Check(
                "time_scale",
                WARN,
                f"not recorded; the contract assumes {contract.control_hz:g} Hz",
            )
        )
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float | np.integer | np.floating)
        or not math.isfinite(value)
        or value <= 0
    ):
        checks.append(
            Check(
                "time_scale",
                FAIL,
                f"time_scale must be a positive number, got {value!r}",
            )
        )
        return
    deviation = abs(float(value) - 1.0)
    # Rounded so 9.5 Hz against 10 Hz sits exactly on the tolerance.
    if round(deviation, 9) > contract.time_scale_tolerance:
        checks.append(
            Check(
                "time_scale",
                WARN,
                f"time_scale {float(value):g} deviates {deviation:.1%} from the "
                f"declared {contract.control_hz:g} Hz (tolerance "
                f"{contract.time_scale_tolerance:.0%})",
            )
        )
    else:
        checks.append(
            Check("time_scale", PASS, f"time_scale {float(value):g} within tolerance")
        )


def _mask(metadata, contract, checks, values) -> None:
    mask = metadata.get("action_mask")
    if mask is None:
        if contract.requires_mask:
            checks.append(
                Check("action_mask", FAIL, "the contract requires an action mask")
            )
        else:
            checks.append(Check("action_mask", SKIP, "no mask given"))
        return
    try:
        flags = np.asarray(mask)
    except Exception:  # noqa: BLE001 - anything unusable is a failed check
        flags = None
    if (
        flags is None
        or flags.ndim != 1
        or (flags.dtype.kind not in "b" and not _zero_one(flags))
    ):
        checks.append(
            Check("action_mask", FAIL, "the mask must be a 1-D list of 0/1 flags")
        )
        return
    steps = values.shape[0] if values is not None else contract.action_horizon
    if flags.shape[0] != steps:
        checks.append(
            Check(
                "action_mask",
                FAIL,
                f"mask has {flags.shape[0]} entries, the block {steps} steps",
            )
        )
        return
    valid = int(np.count_nonzero(flags))
    if valid == 0:
        checks.append(Check("action_mask", FAIL, "no step is marked valid"))
        return
    checks.append(Check("action_mask", PASS, f"{valid} of {steps} steps valid"))


def _zero_one(flags) -> bool:
    return flags.dtype.kind in "iu" and bool(np.isin(flags, (0, 1)).all())


def _ranges(values, representation, contract, checks) -> None:
    if values is None or representation != "absolute":
        checks.append(
            Check(
                "value_ranges", SKIP, "needs a finite absolute block of the right shape"
            )
        )
        return
    xyz = np.abs(values[:, :3]).max()
    rot = np.abs(values[:, 3:6]).max()
    notes = []
    if xyz > contract.position_warn_abs_max:
        notes.append(
            f"a position of {xyz:g} {contract.position_unit} is beyond "
            f"{contract.position_warn_abs_max:g}; millimetres instead of metres?"
        )
    if rot > contract.rotation_warn_abs_max:
        notes.append(
            f"an angle of {rot:g} {contract.rotation_unit} is beyond "
            f"{contract.rotation_warn_abs_max:g}; degrees instead of radians?"
        )
    checks.append(
        Check("value_ranges", WARN, "; ".join(notes))
        if notes
        else Check("value_ranges", PASS, "positions and angles within plausible bounds")
    )
