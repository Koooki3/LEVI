"""VLASH entry point; the existing FR3 collector owns all safety and gates."""

import dataclasses
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(ROOT / "examples/fr3_local"))
import run_robotiq_client_collect_data as collector
import tyro
from runtime import VlashChunker, check_server


@dataclasses.dataclass
class Args(collector.Args):
    vlash_overlap_steps: int = 2
    inference_mode: str = dataclasses.field(default="vlash", init=False)


def factory(client, args, prompt):
    meta = client.get_server_metadata()
    check_server(meta, args)

    def request(observation, future):
        state = np.asarray(
            observation["robot_state"]["cartesian_position"], dtype=np.float32
        )
        payload = collector.rc._policy_payload(observation, state.copy(), prompt)
        payload["vlash_protocol_version"] = 1
        if future is not None:
            payload["vlash_future_state"] = future.copy()

        def infer():
            response = client.infer(payload)
            return collector.rc._chunk_of(response), response

        return infer

    return VlashChunker(
        request,
        overlap_steps=args.vlash_overlap_steps,
        horizon=meta["vlash"]["action_horizon"],
        binarize_gripper=args.binarize_gripper,
    )


def main(args):
    if args.rtc or args.step_by_step:
        raise ValueError("VLASH requires RTC off and continuous stepping")
    collector.main(args, scheduler_factory=factory, server_validator=check_server)


if __name__ == "__main__":
    main(tyro.cli(Args))
