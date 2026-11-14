"""Serve any policy behind a loader module on the FR3 client's wire contract.

    python lab/policy_loader/serve_loader.py --config <code name> --checkpoint-dir <absolute dir> \
        --loader <module>:<function> --port <N> [--host 127.0.0.1] [--check]

Why this exists (design STEP5-AERI-RESET-DESIGN-20261114, R1). LEVI's policy switch (``levi/automatic/fr3_policy_swap``)
starts a service from a frozen argv and then checks, passively and again at the handshake, that the service names the
frozen deployment: ``--config`` and the absolute ``--checkpoint-dir`` must both appear in argv, and the metadata's
``config`` and ``checkpoint_dir`` must equal the deployment's (``levi/automatic/fr3_bridge.check_identity``). A prototype
server that reports a descriptive config string and no ``checkpoint_dir`` cannot pass either check. This entry point
takes both as arguments and puts them in the metadata, so one frozen argv names what the service reports.

What the loader module provides (the only code that knows the model):

* ``CONTRACT``: a dict of the client-contract facts the model's transforms guarantee (``REQUIRED_CONTRACT`` below). It
  must be importable without a GPU or the model's runtime: the heavy imports belong inside the factory.
* ``<function>(checkpoint_dir: pathlib.Path)``: returns the policy, an object whose ``infer(obs) -> {"actions": ...}``
  takes the wire observation (``observation/image``, ``observation/wrist_image``, ``observation/state``, ``prompt``)
  and returns an ``(action_horizon, >=7)`` chunk of absolute pose plus gripper.

This file checks, before it loads anything, that ``CONTRACT`` says what ``run_robotiq_client._check_server`` requires,
and on every request that the observation and the chunk have the shapes the contract names. ``--check`` does the
argument, loader-import and contract checks and prints the metadata, without loading the model or opening a socket
(no GPU, no port). It never connects to a robot: it only listens, on a loopback address.

Stdlib and numpy only at import; ``openpi.serving`` is imported when a server really starts, from the openpi
environment this file is installed into (``install.py``; nothing here edits an openpi checkout).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

LOG = logging.getLogger("policy_loader")
PORTS_NEVER_SERVED = (
    5000,
    5001,
    5100,
    7470,
)  # robot server, gripper, learner: never a policy port
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# A code-style name: the swap's argv check needs it as one argument, and the client prints it.
CONFIG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
LOADER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
# What run_robotiq_client._check_server compares (examples/fr3_local/run_robotiq_client.py): EXPECTED_REBASE_DIM,
# the rebase offset (+-pi), 7D actions and the end-effector frame the client's --min-z and home pose assume.
EXPECTED_REBASE_DIM = 3
EXPECTED_ACTION_DIM = 7
EXPECTED_EE_FRAME = "franka_hand_tcp"
REQUIRED_CONTRACT = (
    "rebase_euler_dim",
    "rebase_euler_offset",
    "action_dim",
    "action_horizon",
    "ee_frame",
    "discrete_state_input",
)
OPTIONAL_CONTRACT = ("gripper_convention", "supports_rtc")
# The two keys the serve arguments own: a loader may not set them.
IDENTITY = ("config", "checkpoint_dir")
MAX_HORIZON = 64
OBSERVATION_KEYS = (
    "observation/image",
    "observation/wrist_image",
    "observation/state",
    "prompt",
)


class LoaderError(ValueError):
    """What is wrong with the arguments, the loader or its contract (the fix is in the message)."""


def client_problems(meta) -> list[str]:
    """The reasons ``run_robotiq_client._check_server`` would refuse ``meta`` (empty: it passes).

    Mirrors that function's checks one to one; ``ee_frame`` is stricter here (required, not merely not-different),
    because a service that does not say its frame has not been reviewed against the client's home pose."""
    if not isinstance(meta, dict) or not meta:
        return ["the policy reported no metadata"]
    problems = []
    if meta.get("rebase_euler_dim") != EXPECTED_REBASE_DIM:
        problems.append(
            f"rebase_euler_dim={meta.get('rebase_euler_dim')!r}, want {EXPECTED_REBASE_DIM}"
        )
    offset = meta.get("rebase_euler_offset")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, (int, float))
        or not math.isfinite(offset)
        or abs(abs(float(offset)) - math.pi) > 1e-6
    ):
        problems.append(f"rebase_euler_offset={offset!r}, want +-pi")
    action_dim = meta.get("action_dim")
    if type(action_dim) is not int or action_dim != EXPECTED_ACTION_DIM:
        problems.append(f"action_dim={action_dim!r}, want {EXPECTED_ACTION_DIM}")
    if meta.get("ee_frame") != EXPECTED_EE_FRAME:
        problems.append(
            f"ee_frame={meta.get('ee_frame')!r}, want {EXPECTED_EE_FRAME!r}"
        )
    horizon = meta.get("action_horizon")
    if type(horizon) is not int or not 1 <= horizon <= MAX_HORIZON:
        problems.append(
            f"action_horizon={horizon!r}, want a whole number in 1..{MAX_HORIZON}"
        )
    if type(meta.get("discrete_state_input")) is not bool:
        problems.append(
            f"discrete_state_input={meta.get('discrete_state_input')!r}, want true or false"
        )
    return problems


def check_contract(contract) -> dict:
    """``CONTRACT`` of a loader module, validated; unknown or missing keys are refused (fail closed)."""
    if not isinstance(contract, dict):
        raise LoaderError("the loader module must define CONTRACT as a dict")
    missing = [k for k in REQUIRED_CONTRACT if k not in contract]
    if missing:
        raise LoaderError("CONTRACT lacks " + ", ".join(missing))
    unknown = sorted(set(contract) - set(REQUIRED_CONTRACT) - set(OPTIONAL_CONTRACT))
    if unknown:
        raise LoaderError(
            "CONTRACT has keys this server does not know: "
            + ", ".join(unknown)
            + (
                " (config and checkpoint_dir come from the arguments)"
                if set(unknown) & set(IDENTITY)
                else ""
            )
        )
    problems = client_problems(contract)
    if problems:
        raise LoaderError(
            "CONTRACT does not match the FR3 client: " + "; ".join(problems)
        )
    if "supports_rtc" in contract and type(contract["supports_rtc"]) is not bool:
        raise LoaderError("CONTRACT supports_rtc must be true or false")
    return dict(contract)


def metadata_for(config: str, checkpoint_dir: str, contract: dict) -> dict:
    """The metadata the websocket server hands every client: the identity from the arguments plus the contract."""
    if not isinstance(config, str) or not CONFIG.fullmatch(config):
        raise LoaderError(
            f"--config must be a code-style name (letters, digits, _ . -), got {config!r}"
        )
    if not isinstance(checkpoint_dir, str) or not os.path.isabs(checkpoint_dir):
        raise LoaderError("--checkpoint-dir must be an absolute path")
    meta = {k: v for k, v in contract.items() if k != "supports_rtc"}
    meta["config"] = config
    meta["checkpoint_dir"] = os.path.realpath(checkpoint_dir)
    return meta


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--config",
        required=True,
        help="the frozen deployment config (a code-style name)",
    )
    parser.add_argument(
        "--checkpoint-dir",
        required=True,
        help="the frozen deployment checkpoint folder (absolute)",
    )
    parser.add_argument(
        "--loader",
        required=True,
        help="<module>:<function>, importable from this file's folder",
    )
    parser.add_argument(
        "--port",
        required=True,
        type=int,
        help="the port to listen on (no default: the caller owns it)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="loopback only (default 127.0.0.1)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check the arguments and the loader's contract, print the metadata, exit",
    )
    args = parser.parse_args(argv)
    if not CONFIG.fullmatch(args.config):
        parser.error(
            f"--config must be a code-style name (letters, digits, _ . -), got {args.config!r}"
        )
    if (
        not os.path.isabs(args.checkpoint_dir)
        or ".." in Path(args.checkpoint_dir).parts
    ):
        parser.error("--checkpoint-dir must be an absolute path without '..'")
    if not LOADER.fullmatch(args.loader):
        parser.error("--loader must look like module:function")
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be in 1024..65535")
    if args.port in PORTS_NEVER_SERVED:
        parser.error(
            f"--port {args.port} is a robot or learner port; a policy is never served there"
        )
    if args.host not in LOOPBACK:
        parser.error("--host must be a loopback address")
    return args


def resolve_loader(spec: str, search: Path | None = None):
    """``(CONTRACT, factory)`` of ``module:function``; the module is imported from this file's folder first."""
    module_name, function = spec.split(":", 1)
    folder = str(search if search is not None else Path(__file__).resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise LoaderError(
            f"cannot import the loader module {module_name!r}: {exc}"
        ) from exc
    factory = getattr(module, function, None)
    if not callable(factory):
        raise LoaderError(f"{module_name} has no callable {function!r}")
    return check_contract(getattr(module, "CONTRACT", None)), factory


class ServedPolicy:
    """What the websocket server serves: the loader's policy behind the contract's request and answer checks."""

    def __init__(self, policy, contract: dict, metadata: dict) -> None:
        self._policy = policy
        self._contract = contract
        self.metadata = metadata
        self.last_infer_ms: float | None = None
        self.calls = 0

    def infer(self, obs: dict) -> dict:
        if "rtc_prev_actions" in obs and not self._contract.get("supports_rtc", False):
            raise ValueError(
                "this server is plain chunking only: start the client without RTC/VLASH"
            )
        for key in OBSERVATION_KEYS:
            if key not in obs:
                raise ValueError(f"the observation lacks {key!r}")
        state = np.asarray(obs["observation/state"], dtype=np.float32)
        if state.shape != (EXPECTED_ACTION_DIM,) or not np.all(np.isfinite(state)):
            raise ValueError(
                f"observation/state must be {EXPECTED_ACTION_DIM} finite numbers, got {state.shape}"
            )
        if not isinstance(obs["prompt"], str) or not obs["prompt"].strip():
            raise ValueError("the observation's prompt must be a non-empty string")
        start = time.perf_counter()
        result = self._policy.infer(obs)
        self.last_infer_ms = (time.perf_counter() - start) * 1000.0
        actions = np.asarray(result["actions"], dtype=np.float32)
        horizon = self._contract["action_horizon"]
        if (
            actions.ndim != 2
            or actions.shape[0] != horizon
            or actions.shape[1] < EXPECTED_ACTION_DIM
        ):
            raise ValueError(
                f"unexpected action chunk {actions.shape}, want ({horizon}, >={EXPECTED_ACTION_DIM})"
            )
        actions = actions[:, :EXPECTED_ACTION_DIM]
        if not np.all(np.isfinite(actions)):
            raise ValueError("the action chunk has a non-finite value")
        self.calls += 1
        LOG.info(
            "infer #%d %.1f ms prompt=%r first=%s grip=%s",
            self.calls,
            self.last_infer_ms,
            obs["prompt"],
            [round(float(x), 4) for x in actions[0]],
            [round(float(x), 2) for x in actions[:, 6]],
        )
        return {"actions": actions, "policy_timing": {"infer_ms": self.last_infer_ms}}

    def reset(self) -> None:
        reset = getattr(self._policy, "reset", None)
        if callable(reset):
            reset()


def prepare(args: argparse.Namespace):
    """``(metadata, factory, contract)`` after every check that needs no model."""
    contract, factory = resolve_loader(args.loader)
    if not os.path.isdir(args.checkpoint_dir):
        raise LoaderError(f"--checkpoint-dir {args.checkpoint_dir} is not a directory")
    return metadata_for(args.config, args.checkpoint_dir, contract), factory, contract


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        metadata, factory, contract = prepare(args)
    except LoaderError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print(json.dumps(metadata, indent=2, sort_keys=True))
        return 0
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True
    )
    policy = ServedPolicy(factory(Path(args.checkpoint_dir)), contract, metadata)
    from openpi.serving import (
        websocket_policy_server,
    )

    LOG.info(
        "serving %s on %s:%d metadata=%s", args.config, args.host, args.port, metadata
    )
    websocket_policy_server.WebsocketPolicyServer(
        policy=policy, host=args.host, port=args.port, metadata=metadata
    ).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
