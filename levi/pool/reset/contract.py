"""What an action means, and how to rebuild it for reversed time.

A reversed episode needs new actions: reversing the action column puts every
action one step off, and the gripper cannot simply be inverted. Only an action
whose meaning is known (a registered contract in ``levi.counterfactual``, and
checked against the data) is rebuilt; anything else is refused.

For the ``next_state`` meaning (``action[t] = state[t + 1]``, absolute pose and
gripper command, the form LEVI's conversion writes), the reversed action of
row ``k`` is the reversed state of row ``k + 1``. The last row keeps its own
state, as the conversion does.
"""

import numpy as np

from ...counterfactual import schema

ATOL = 1e-5
MATCH_FRACTION = 0.99


class ContractProblem(ValueError):
    """The data does not follow the contract it was said to follow."""


def resolve(contract_id: str) -> schema.ActionContract:
    try:
        return schema.get(contract_id)
    except schema.UnknownContract as exc:
        raise ValueError(str(exc)) from None


def detect_semantics(state: np.ndarray, action: np.ndarray) -> tuple[str | None, float]:
    """``next_state``, ``state`` or None, and how many rows agree. The test is
    on the data, so a dataset converted some other way is not misread."""
    if len(state) < 2 or state.shape != action.shape:
        return None, 0.0
    nxt = np.isclose(action[:-1], state[1:], atol=ATOL).all(axis=1).mean()
    same = np.isclose(action, state, atol=ATOL).all(axis=1).mean()
    if nxt >= MATCH_FRACTION and nxt >= same:
        return "next_state", float(nxt)
    if same >= MATCH_FRACTION:
        return "state", float(same)
    return None, float(max(nxt, same))


def check(contract: schema.ActionContract, state: np.ndarray, action: np.ndarray):
    """Raises ``ContractProblem`` unless the data follows the contract:
    dimensions, gripper values and the action's meaning."""
    dims = len(contract.dimension_names)
    if state.ndim != 2 or state.shape[1] != dims or action.shape != state.shape:
        raise ContractProblem(
            f"state/action have shape {state.shape}/{action.shape}, the contract "
            f"{contract.ref} has {dims} dimensions"
        )
    grip = state[:, contract.gripper_index]
    low, high = contract.gripper_values
    if not np.isin(np.round(grip, 4), (low, high)).all():
        raise ContractProblem(
            f"the gripper dimension holds values other than {low:g} and {high:g}"
        )
    found, share = detect_semantics(state, action)
    if found is None:
        raise ContractProblem(
            f"the action is neither the next state nor the state "
            f"({share:.0%} of rows agree at best): its meaning is unknown"
        )
    if found != contract.action_semantics:
        raise ContractProblem(
            f"the action is {found!r}, the contract {contract.ref} says "
            f"{contract.action_semantics!r}"
        )


def reversed_actions(state: np.ndarray, semantics: str) -> np.ndarray:
    """Actions for states already in reversed order."""
    if semantics == "next_state":
        return np.vstack([state[1:], state[-1:]]).astype(np.float32)
    if semantics == "state":
        return state.astype(np.float32).copy()
    raise ContractProblem(f"cannot rebuild actions of meaning {semantics!r}")


def reversed_states(
    state: np.ndarray, keep: list[int], edits: dict[int, float], gripper_index: int
) -> np.ndarray:
    """The state rows of the reversed episode: the kept source rows from last
    to first, with the gripper command rewritten where ``edits`` says (the
    reversed command has to lead the finger motion, which a plain reversal puts
    after it)."""
    out = state[list(keep)[::-1]].astype(np.float32).copy()
    for position, row in enumerate(list(keep)[::-1]):
        if row in edits:
            out[position, gripper_index] = edits[row]
    return out
