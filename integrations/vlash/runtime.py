"""Future-state-aware chunk scheduling adapted from mit-han-lab/vlash.

Apache-2.0 upstream: https://github.com/mit-han-lab/vlash/blob/main/vlash/run.py
This openpi adapter is experimental. It does not certify delay-augmented training.
No robot/GPU imports; every action still passes through the existing FR3 loop.
"""

from __future__ import annotations

import copy
import threading
import time

import numpy as np

VERSION = 1
SPACE = "fr3_absolute_pose_gripper_v1"
CONFIGS = {"pi05_fr3_all_state", "pi05_fr3_all_state_cfg"}


def capabilities(meta, overlap):
    horizon = meta.get("action_horizon")
    if (
        meta.get("config") not in CONFIGS
        or meta.get("action_dim") != 7
        or meta.get("discrete_state_input") is not True
        or meta.get("ee_frame") != "franka_hand_tcp"
        or meta.get("gripper_convention")
        != "1.0=open, 0.0=closed, commanded not measured"
    ):
        raise ValueError(
            "VLASH requires the reviewed state-conditioned FR3 absolute-pose/gripper contract"
        )
    if (
        type(horizon) is not int
        or type(overlap) is not int
        or not 1 <= overlap < horizon
    ):
        raise ValueError("VLASH overlap must be an integer in [1, action_horizon)")
    return {
        "protocol_version": VERSION,
        "state_action_space": SPACE,
        "action_horizon": horizon,
        "overlap_steps": overlap,
        "experimental": True,
        "delay_training_verified": False,
        "implementation": "openpi_future_state_adapter",
    }


def check_server(meta, args):
    if getattr(args, "rtc", False):
        raise ValueError("RTC and VLASH are mutually exclusive")
    expected = capabilities(meta, args.vlash_overlap_steps)
    advertised = meta.get("vlash")
    if not isinstance(advertised, dict) or any(
        advertised.get(k) != expected[k]
        for k in (
            "protocol_version",
            "state_action_space",
            "action_horizon",
            "overlap_steps",
        )
    ):
        raise ValueError(
            "The policy server does not match the requested VLASH protocol/overlap"
        )


def vector(value):
    result = np.asarray(value, dtype=np.float32)
    if result.shape != (7,) or not np.isfinite(result).all():
        raise ValueError("expected a finite absolute FR3 state/action of shape (7,)")
    return result.copy()


class FutureStatePolicy:
    def __init__(self, policy, overlap):
        self.policy = policy
        self.metadata = dict(policy.metadata)
        self.metadata["vlash"] = capabilities(self.metadata, overlap)

    def infer(self, observation, **kwargs):
        protocol = observation.get("vlash_protocol_version")
        if type(protocol) is not int or protocol != VERSION:
            raise ValueError(
                "every request must explicitly identify VLASH protocol version 1"
            )
        if any(k.startswith("rtc_") for k in observation):
            raise ValueError("RTC requests cannot use a VLASH server")
        future = observation.get("vlash_future_state")
        payload = dict(observation)
        payload.pop("vlash_future_state", None)
        payload.pop("vlash_protocol_version", None)
        if future is not None:
            payload["observation/state"] = vector(future)
        else:
            vector(payload["observation/state"])
        result = self.policy.infer(payload, **kwargs)
        actions = np.asarray(result["actions"])
        if (
            actions.shape != (self.metadata["action_horizon"], 7)
            or not np.isfinite(actions).all()
        ):
            raise ValueError("VLASH received an invalid action chunk")
        return {**result, "vlash": {"future_state_used": future is not None}}

    def reset(self):
        self.policy.reset()


class _Request:
    def __init__(self, function):
        self.done = threading.Event()
        self.value = None
        self.error = None
        self.started = time.monotonic()
        self.thread = threading.Thread(
            target=self._run, args=(function,), daemon=True, name="vlash-inference"
        )
        self.thread.start()

    def _run(self, function):
        try:
            self.value = function()
        except BaseException as error:  # noqa: BLE001 - transfer every worker failure to caller
            self.error = error
        finally:
            self.done.set()

    def take(self, timeout):
        if not self.done.wait(timeout):
            raise TimeoutError(
                "VLASH inference did not finish; socket cannot be reused"
            )
        self.thread.join()
        if self.error is not None:
            raise self.error
        return self.value


class VlashChunker:
    """No early switching or skipped leading rows: consume each chunk fully.

    request(obs, future_state) snapshots a payload on the caller's thread and
    returns a zero-argument network call. Only one call may use the socket.
    """

    mode = "vlash"

    def __init__(
        self, request, *, overlap_steps, horizon, timeout_s=30.0, binarize_gripper=False
    ):
        if (
            type(overlap_steps) is not int
            or type(horizon) is not int
            or not 1 <= overlap_steps < horizon
        ):
            raise ValueError("invalid VLASH chunk horizon/overlap")
        self.request = request
        self.overlap = overlap_steps
        self.horizon = horizon
        self.timeout = timeout_s
        self.binarize_gripper = binarize_gripper
        self.chunk = None
        self.index = 0
        self.pending = None
        self.abandoned = []
        self.closed = False
        self.stats = {
            "requests": 0,
            "prefetches": 0,
            "starvations": 0,
            "discarded_replies": 0,
            "adjusted_targets": 0,
        }

    def _chunk(self, answer):
        chunk, _ = answer
        chunk = np.asarray(chunk, dtype=np.float32)
        if chunk.shape != (self.horizon, 7) or not np.isfinite(chunk).all():
            raise ValueError("invalid VLASH chunk shape/values")
        chunk = chunk.copy()
        if self.binarize_gripper:
            # Same command encoding as the existing FR3 safety loop.
            chunk[:, 6] = (chunk[:, 6] >= 0.5).astype(np.float32)
        return chunk

    def _drain(self):
        for req in self.abandoned:
            req.take(self.timeout)
            self.stats["discarded_replies"] += 1
        self.abandoned.clear()

    def next_action(self, observation):
        if self.closed:
            raise RuntimeError("VLASH scheduler already closed")
        if self.chunk is None:
            self._drain()
            self.stats["requests"] += 1
            self.chunk = self._chunk(self.request(copy.deepcopy(observation), None)())
            self.index = 0
        elif self.index == self.horizon:
            if self.pending is None:
                raise RuntimeError("missing VLASH prefetch")
            if not self.pending.done.is_set():
                self.stats["starvations"] += 1
            self.chunk = self._chunk(self.pending.take(self.timeout))
            self.pending = None
            self.index = 0
        if self.index == self.horizon - self.overlap and self.pending is None:
            future = vector(self.chunk[-1])
            function = self.request(copy.deepcopy(observation), future)
            self.pending = _Request(function)
            self.stats["requests"] += 1
            self.stats["prefetches"] += 1
        action = self.chunk[self.index].copy()
        self.index += 1
        return action

    def reset(self):
        if self.pending is not None:
            self.abandoned.append(self.pending)
            self.pending = None
        self.chunk = None
        self.index = 0

    def action_adjusted(self, raw, actual):
        if not np.array_equal(np.asarray(raw), np.asarray(actual)):
            self.stats["adjusted_targets"] += 1
            self.reset()

    def close(self):
        self.closed = True
        self.reset()
        self._drain()

    def summary(self):
        return {
            "mode": self.mode,
            "overlap_steps": self.overlap,
            "action_horizon": self.horizon,
            "experimental": True,
            "delay_training_verified": False,
            **self.stats,
        }
