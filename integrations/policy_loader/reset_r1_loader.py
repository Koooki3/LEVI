"""Loader module for the reset_r1 checkpoint (``serve_loader.py --loader reset_r1_loader:load``).

Replaces the prototype ``openpi/lab/reset_r1_serve/reset_policy.py`` (2026-11-14 supervised real-robot test) with the
same behaviour behind the generic entry point: the checkpoint's own transforms do the Euler rebase and the
delta/absolute conversion inside ``policy.infer`` (its README: "do not add an rx +-pi or a delta inverse"), so this
module only renames the wire keys to the training names. ``CONTRACT`` repeats the facts the FR3 client checks; they
describe what those transforms do (rebase of rx by pi, 7D absolute output, ``franka_hand_tcp`` frame) and are to be
verified offline against the checkpoint before any motion (design STEP5-AERI-RESET-DESIGN-20261114, V1).

Import of this module needs no GPU, no JAX and no checkpoint: everything heavy is inside ``load``.

The reset model takes the training sentence of its task ("Reset: pick the eggplant into the blue plate") and only
that: other wordings moved the arm 1-4 mm in ten steps. The prompt is therefore never composed here; LEVI freezes the
table of accepted sentences in the job (``subtask.reset_prompts``).

Where the pieces are (nothing is created next to the checkpoint, which stays read-only):

* the checkpoint folder is the ``--checkpoint-dir`` argument (``runtime/train_reset_r1.py`` inside it holds
  ``load_reset_r1_policy``);
* the unpacked OpenPI source of that checkpoint (``runtime/openpi_reset_source.tar.gz``) is read from
  ``$RESET_R1_OPENPI_ROOT``, default ``<this folder>/openpi_reset``;
* caches and temporary files go to ``$RESET_R1_RUN_DIR``, default ``<this folder>/run``.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

CONTRACT = {
    "rebase_euler_dim": 3,
    "rebase_euler_offset": math.pi,
    "action_dim": 7,
    "action_horizon": 10,
    "discrete_state_input": True,
    "ee_frame": "franka_hand_tcp",
    "gripper_convention": "1.0=open, 0.0=closed, commanded not measured",
}

# The training names of the two cameras and the state (the checkpoint's data transform reads these).
KEY_MAP = {
    "observation/image": "observation.images.view1",
    "observation/wrist_image": "observation.images.hand",
    "observation/state": "observation.state",
}


def to_training_observation(obs: dict) -> dict:
    """The wire observation renamed to the training keys; nothing is converted."""
    out = {training: obs[wire] for wire, training in KEY_MAP.items()}
    out["prompt"] = str(obs["prompt"])
    return out


def _caches(run_dir: Path) -> None:
    for env, sub in (
        ("TMPDIR", "tmp"),
        ("HF_HOME", "hf"),
        ("XDG_CACHE_HOME", "xdg"),
        ("TRITON_CACHE_DIR", "triton"),
        ("WANDB_DIR", "wandb"),
    ):
        path = run_dir / "cache" / sub
        path.mkdir(parents=True, exist_ok=True)
        os.environ[env] = str(path)
    # The PaliGemma tokenizer is in openpi's normal cache (serve_policy.py reads it there); only read.
    os.environ.setdefault("OPENPI_DATA_HOME", str(Path("~/.cache/openpi").expanduser()))


class ResetR1:
    """``infer(obs) -> {"actions": (10, 7)}`` over the checkpoint's own policy."""

    def __init__(self, policy) -> None:
        self._policy = policy

    def infer(self, obs: dict) -> dict:
        import numpy as np

        result = self._policy.infer(to_training_observation(obs))
        return {"actions": np.asarray(result["actions"], dtype=np.float32)}


def load(checkpoint_dir: Path) -> ResetR1:
    checkpoint_dir = Path(checkpoint_dir)
    run_dir = Path(os.environ.get("RESET_R1_RUN_DIR", HERE / "run"))
    openpi_root = Path(os.environ.get("RESET_R1_OPENPI_ROOT", HERE / "openpi_reset"))
    if not (checkpoint_dir / "runtime" / "train_reset_r1.py").is_file():
        raise FileNotFoundError(
            f"{checkpoint_dir} has no runtime/train_reset_r1.py: not a reset_r1 checkpoint"
        )
    if not openpi_root.is_dir():
        raise FileNotFoundError(
            f"{openpi_root} is missing: unpack runtime/openpi_reset_source.tar.gz there (README, policy_loader)"
        )
    _caches(run_dir)
    sys.path.insert(0, str(checkpoint_dir / "runtime"))
    from train_reset_r1 import (
        load_reset_r1_policy,
    )

    return ResetR1(
        load_reset_r1_policy(
            run_dir=run_dir, openpi_root=openpi_root, checkpoint_dir=checkpoint_dir
        )
    )
