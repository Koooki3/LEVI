"""Action contracts: what an action block must look like for one embodiment.

A contract is a frozen record of the facts a candidate action block has to
agree with: the dimension order, the units, whether the numbers are absolute
poses or deltas, the gripper's values, the control period and the block
length. The facts are read from the code that makes the training data and
trains on it (``source``), not from memory; ``tests/test_counterfactual_contract.py``
compares the constants with those files so a drift fails a test.

Only registered contracts exist: an id that is not in the registry is refused.
This module has no I/O and executes nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


class UnknownContract(ValueError):
    """The contract id (or version) is not registered."""


@dataclass(frozen=True)
class SourceRef:
    """A file and line range a constant was read from, with the text that
    carries it. ``path`` is relative to the workspace root (the directory
    holding ``LEVI/``) for files outside this repository, and to the
    repository root for files inside it."""

    path: str
    first: int
    last: int
    snippet: str
    in_repository: bool = False

    def __str__(self) -> str:
        return f"{self.path}:{self.first}-{self.last}"


@dataclass(frozen=True)
class ActionContract:
    id: str
    version: int
    embodiment: str
    # Dimension order of one action step and of the state it is relative to.
    dimension_names: tuple[str, ...]
    action_horizon: int  # H: steps in one action block
    executed_horizon: int  # steps a client executes before asking again
    control_hz: float
    # What each step is: "absolute" end-effector pose of the next frame
    # (action[t] == state[t+1]), or "delta" against the current state.
    representation: str
    action_semantics: str
    position_unit: str
    rotation_unit: str
    rotation_convention: str
    frame: str
    gripper_index: int
    gripper_values: tuple[float, ...]
    gripper_open_value: float
    gripper_semantics: str
    # What training does to the stored (absolute) action before the model
    # sees it; a candidate block is stored-space, not model-space.
    training_transform: tuple[str, ...]
    rebase_euler_index: int
    rebase_euler_offset: float
    # A block whose time_scale differs from 1 by more than this gets a warning.
    time_scale_tolerance: float
    requires_mask: bool
    # Soft plausibility bounds (warnings only): metres, radians.
    position_warn_abs_max: float
    rotation_warn_abs_max: float
    source: tuple[SourceRef, ...]
    notes: tuple[str, ...] = field(default=())

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def dimension(self) -> int:
        return len(self.dimension_names)

    @property
    def control_period_s(self) -> float:
        return 1.0 / self.control_hz

    def describe(self) -> dict:
        """JSON-friendly form of the contract."""
        return {
            "id": self.id,
            "version": self.version,
            "ref": self.ref,
            "embodiment": self.embodiment,
            "dimension_names": list(self.dimension_names),
            "action_horizon": self.action_horizon,
            "executed_horizon": self.executed_horizon,
            "control_hz": self.control_hz,
            "control_period_s": self.control_period_s,
            "representation": self.representation,
            "action_semantics": self.action_semantics,
            "position_unit": self.position_unit,
            "rotation_unit": self.rotation_unit,
            "rotation_convention": self.rotation_convention,
            "frame": self.frame,
            "gripper_index": self.gripper_index,
            "gripper_values": list(self.gripper_values),
            "gripper_open_value": self.gripper_open_value,
            "gripper_semantics": self.gripper_semantics,
            "training_transform": list(self.training_transform),
            "rebase_euler_index": self.rebase_euler_index,
            "rebase_euler_offset": self.rebase_euler_offset,
            "time_scale_tolerance": self.time_scale_tolerance,
            "requires_mask": self.requires_mask,
            "position_warn_abs_max": self.position_warn_abs_max,
            "rotation_warn_abs_max": self.rotation_warn_abs_max,
            "source": [str(s) for s in self.source],
            "notes": list(self.notes),
        }


_REGISTRY: dict[tuple[str, int], ActionContract] = {}


def register(contract: ActionContract) -> ActionContract:
    """Add a contract. An (id, version) is never replaced."""
    key = (contract.id, contract.version)
    if key in _REGISTRY:
        raise ValueError(f"Contract {contract.ref} is already registered")
    if contract.dimension != len(set(contract.dimension_names)):
        raise ValueError("Dimension names must be unique")
    if not 0 <= contract.gripper_index < contract.dimension:
        raise ValueError("gripper_index is outside the dimensions")
    if contract.action_horizon < 1 or not math.isfinite(contract.control_hz):
        raise ValueError("action_horizon and control_hz must be positive")
    _REGISTRY[key] = contract
    return contract


def get(contract_id: str) -> ActionContract:
    """The contract for ``"<id>@<version>"``, or for ``"<id>"`` the newest
    version. Anything not registered raises :class:`UnknownContract`; pin the
    version in anything that must not change under you."""
    if not isinstance(contract_id, str) or not contract_id:
        raise UnknownContract(f"Not a contract id: {contract_id!r}")
    name, sep, version = contract_id.partition("@")
    if sep:
        try:
            found = _REGISTRY.get((name, int(version)))
        except ValueError:
            found = None
    else:
        versions = [v for (n, v) in _REGISTRY if n == name]
        found = _REGISTRY.get((name, max(versions))) if versions else None
    if found is None:
        raise UnknownContract(
            f"Action contract {contract_id!r} is not registered; known: "
            + (", ".join(ids()) or "none")
        )
    return found


def ids() -> list[str]:
    """Every registered ``id@version``."""
    return sorted(c.ref for c in _REGISTRY.values())


_RAW = "data_collection_robotiq/scripts/raw2lerobot.py"
_OPENPI = "openpi/src/openpi"

FR3_ROBOTIQ = register(
    ActionContract(
        id="fr3-robotiq",
        version=1,
        embodiment="Franka Research 3 with a Robotiq 2F-85 gripper",
        dimension_names=("x", "y", "z", "rx", "ry", "rz", "gripper"),
        action_horizon=10,
        executed_horizon=8,
        control_hz=10.0,
        representation="absolute",
        action_semantics="next_state",
        position_unit="m",
        rotation_unit="rad",
        rotation_convention="euler roll-pitch-yaw from the end-effector quaternion",
        frame="franka_hand_tcp",
        gripper_index=6,
        gripper_values=(0.0, 1.0),
        gripper_open_value=1.0,
        gripper_semantics="1.0 = open, 0.0 = closed; the command, not the measured width",
        training_transform=(
            "rx: add pi, wrap into [-pi, pi) (state and actions alike)",
            "x, y, z, rx, ry, rz: subtract the current state (delta action)",
            "rx, ry, rz deltas: wrap into [-pi, pi)",
            "gripper: stays absolute",
        ),
        rebase_euler_index=3,
        rebase_euler_offset=math.pi,
        time_scale_tolerance=0.05,
        requires_mask=True,
        position_warn_abs_max=2.0,
        rotation_warn_abs_max=4 * math.pi,
        source=(
            SourceRef(
                _RAW,
                202,
                213,
                "action[t] = state[t+1]",
            ),
            SourceRef(_RAW, 184, 192, "float(gripper_norm),"),
            SourceRef(
                _RAW,
                117,
                118,
                'return 0.0 if str(command).strip().lower() == "close" else 1.0',
            ),
            SourceRef(
                "levi/conversion/pipeline.py",
                116,
                126,
                'if options.action_mode == "next_state"',
                in_repository=True,
            ),
            SourceRef(
                "levi/conversion/raw.py",
                148,
                148,
                '(command == "open")',
                in_repository=True,
            ),
            SourceRef(
                "levi/conversion/options.py",
                31,
                41,
                'action_mode: Literal["next_state", "state"] = "next_state"',
                in_repository=True,
            ),
            SourceRef(
                f"{_OPENPI}/training/config.py",
                404,
                431,
                "rebase_euler_offset: float = np.pi",
            ),
            SourceRef(
                f"{_OPENPI}/training/config.py",
                1144,
                1183,
                "action_horizon=10, discrete_state_input=True",
            ),
            SourceRef(
                f"{_OPENPI}/transforms.py",
                204,
                318,
                "def wrap_to_pi(values: np.ndarray)",
            ),
            SourceRef(
                "openpi/examples/fr3_local/run_robotiq_client.py",
                154,
                154,
                'EXPECTED_EE_FRAME = "franka_hand_tcp"',
            ),
            SourceRef(
                "openpi/examples/fr3_local/run_robotiq_client.py",
                264,
                265,
                "control_hz: float = 10.0",
            ),
        ),
        notes=(
            (
                "The stored dataset action is the next frame's absolute pose; the "
                "delta form only exists inside training and inference transforms."
            ),
            (
                "rx is stored near the +-pi cut (raw2lerobot.py reports roll on "
                "[0, 2*pi), LEVI's conversion unwraps it along time), which is why "
                "training adds pi before taking deltas; a block is checked in "
                "stored space, before that shift."
            ),
            "There is no camera extrinsic calibration for this embodiment.",
        ),
    )
)
