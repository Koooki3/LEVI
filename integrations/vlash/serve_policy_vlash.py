"""Experimental VLASH scheduling capability over the existing openpi policy."""

import dataclasses
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).absolute().parents[2]
# Installed at <openpi>/lab/vlash; keep the original serving constructor.
sys.path.insert(0, str(ROOT / "scripts"))
import serve_policy as serving
import tyro
from openpi.serving import websocket_policy_server
from runtime import FutureStatePolicy


@dataclasses.dataclass
class Args(serving.Args):
    vlash_overlap_steps: int = 2


def main(args):
    if not isinstance(args.policy, serving.Checkpoint):
        raise TypeError("VLASH requires an explicit reviewed FR3 checkpoint")
    if args.record:
        raise ValueError(
            "VLASH server recording is unsupported; use the evaluation recorder"
        )
    policy = FutureStatePolicy(serving.create_policy(args), args.vlash_overlap_steps)
    metadata = dict(policy.metadata)
    metadata.setdefault("checkpoint_dir", str(args.policy.dir))
    logging.getLogger("openpi").info(
        "VLASH enabled: protocol=1 experimental openpi future-state adapter; delay training unverified"
    )
    websocket_policy_server.WebsocketPolicyServer(
        policy=policy, host="0.0.0.0", port=args.port, metadata=metadata
    ).serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, force=True)
    main(tyro.cli(Args))
