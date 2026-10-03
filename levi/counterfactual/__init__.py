"""Counterfactual data: the action contract and its checks.

Today this package holds only the contract of an action block and a
deterministic validator (docs/COUNTERFACTUAL.md). There is no generation,
import, storage or interface yet; the plan is levi-hub/PLAN-cast.md.
"""

from .schema import ActionContract, UnknownContract, get, ids, register
from .validation import ValidationResult, validate_action_block

__all__ = [
    "ActionContract",
    "UnknownContract",
    "ValidationResult",
    "get",
    "ids",
    "register",
    "validate_action_block",
]
