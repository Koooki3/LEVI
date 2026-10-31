"""In-chunk acceleration entry point (experimental): the existing FR3 collector, its policy query wrapped.

Every chunk the server returns goes through ChunkAccel.plan (accel.py) before the collector's open-loop loop executes
it; the collector keeps every gate, clamp, jump check, keyboard key and the recording. A hurried chunk is shorter, so
the loop executes it and re-queries sooner. Plain inference only: RTC and VLASH schedule chunks themselves.
Recorded: policy.chunk_accel (the settings) in every demo and in the session's eval log header, eval.chunk_accel (that
episode's counts, aborted episodes included; eval.abort_detail.chunk_accel in LEVI's unattended mode). The collector's
action check is wrapped too: an accepted action counts toward the gripper hold, a refused jump does not.
"""

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(ROOT / "examples/fr3_local"))
sys.path.insert(0, str(Path(__file__).absolute().parent))
import run_robotiq_client_collect_data as collector
import tyro
from accel import RULES, SPEED, TAU, ChunkAccel


@dataclasses.dataclass
class Args(collector.Args):
    accel_speed: float = SPEED  # m/s along a hurried chunk's ramp
    # s, the controller's setpoint low-pass: 0.2 at filter 0.005, 0.1 at 0.01
    accel_tau: float = TAU
    # v1: straight gripper-free chunks only; v2: also curved chunks (along their path) and the actions before a
    # gripper change (settled before the gripper command), with a 6 cm target-to-arm guard (accel.py)
    accel_rule: str = "v1"


def install(args) -> ChunkAccel:
    """Wrap the collector's policy query, action check, eval log and recorder; returns the episode's accelerator."""
    accel = ChunkAccel(
        args.open_loop_horizon,
        args.control_hz,
        speed=args.accel_speed,
        tau=args.accel_tau,
        rule=args.accel_rule,
    )
    rc, recorder = collector.rc, collector.RolloutRecorder
    query, clamp, eval_init, metadata, close, abort = (
        rc._query_policy,
        rc._clamp_action,
        rc.EvalLog.__init__,
        recorder._session_metadata,
        recorder.close,
        recorder.abort,
    )

    def settings():
        return {
            "experimental": True,
            "rule": accel.rule,
            "speed_m_s": accel.speed,
            "tau_s": accel.tau,
            "gap_m": accel.gap,
            "dev_m": accel.dev,
            "rot_max_rad": accel.rot_max,
            "grip_hold_ticks": accel.grip_hold,
        }

    def hurried_query(client, obs, state, prompt):
        return accel.plan(query(client, obs, state, prompt), state)

    def checked_action(action, current, args_):
        # the collector refuses a jump (notes STEP/ROT), executes nothing and re-queries; anything else is sent
        out, notes = clamp(action, current, args_)
        if any(str(n).startswith(("STEP", "ROT")) for n in notes):
            accel.rejected()
        else:
            accel.executed(out)
        return out, notes

    def eval_log_init(self, *a, **k):
        eval_init(self, *a, **k)
        self.header["chunk_accel"] = settings()

    def session_metadata(self):
        md = metadata(self)
        md["policy"]["chunk_accel"] = settings()
        return md

    def close_(
        self, outcome, verdict_by, counted, stats, discard_if_invalid=False, extra=None
    ):
        extra = {**(extra or {}), "chunk_accel": accel.summary()}
        accel.reset()
        return close(
            self,
            outcome,
            verdict_by,
            counted,
            stats,
            discard_if_invalid=discard_if_invalid,
            extra=extra,
        )

    def abort_(self, reason, stats=None, detail=None):
        detail = {**(detail or {}), "chunk_accel": accel.summary()}
        accel.reset()
        return abort(self, reason, stats, detail)

    run_episode = rc._run_episode

    def run_episode_(*args, **kwargs):
        accel.reset()  # every episode starts fresh, recorded or not (a failed recorder never calls close)
        return run_episode(*args, **kwargs)

    rc._query_policy, rc._run_episode, rc._clamp_action = (
        hurried_query,
        run_episode_,
        checked_action,
    )
    rc.EvalLog.__init__ = eval_log_init
    recorder._session_metadata, recorder.close, recorder.abort = (
        session_metadata,
        close_,
        abort_,
    )
    return accel


def main(args):
    if args.rtc or getattr(args, "inference_mode", None) == "vlash":
        raise ValueError(
            "chunk acceleration runs with plain inference only (RTC and VLASH off)"
        )
    # The rule's steps and timing are for the policy's own 10 Hz, the gripper test for binarized commands, and it
    # assumes every planned action is executed (step-by-step skips break that).
    if args.control_hz != 10 or args.step_by_step or not args.binarize_gripper:
        raise ValueError(
            "chunk acceleration needs --control-hz 10, --no-step-by-step and --binarize-gripper"
        )
    if not 0 < args.accel_speed <= 0.30 or not 0 <= args.accel_tau <= 0.3:
        raise ValueError(
            "accel_speed must be in (0, 0.30] m/s and accel_tau in [0, 0.3] s"
        )
    if args.accel_rule not in RULES:
        raise ValueError(f"accel_rule must be one of {RULES}")
    install(args)
    collector.main(args)


if __name__ == "__main__":
    main(tyro.cli(Args))
