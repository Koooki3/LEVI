"""FR3 adapter over the existing guarded openpi client. Imports do not connect.

Only the separate operator entry point constructs hardware. The launch runner's
real-mode refusal stays in place. Every command is fenced, including each
interpolated home step; uncertain delivery locks motion until a fresh process.
"""

import hashlib
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from levi.domain import aeri


@dataclass(frozen=True)
class StepResult:
    executed: str
    detail: str
    step: int = 0


@dataclass(frozen=True)
class HomeResult:
    executed: str
    detail: str
    error_mm: float | None = None


class Clock:
    def __init__(self):
        self.domain = "fr3-" + uuid.uuid4().hex[:20]

    def now(self):
        return time.monotonic_ns()

    def advance(self, ns):
        time.sleep(max(0, ns) / 1e9)


def checkpoint_path(value):
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError("deployment.checkpoint_dir must be absolute")
    return os.path.realpath(value)


def check_identity(expected, metadata):
    if not isinstance(metadata, dict):
        raise TypeError("policy metadata unavailable")
    if expected["config"] != metadata.get("config") or checkpoint_path(
        expected["checkpoint_dir"]
    ) != checkpoint_path(metadata.get("checkpoint_dir")):
        raise ValueError("policy deployment identity mismatch")


class Robot:
    """Requires Fr3Guard.attach(env) before construction; never calls reset()."""

    def __init__(self, env, guard, client_module, args, clock, fence, *, tolerance_mm):
        self.env, self.guard, self.client = env, guard, client_module
        self.args, self.clock, self.fence = args, clock, fence
        self.tolerance_mm = tolerance_mm
        self.raw = None
        self.latched = None
        self.held = False
        self.steps = 0
        self.stop_event = threading.Event()
        self.stop_probe = lambda: False
        self._sending = False
        self._motion = None
        original_post = env._post

        def fenced_post(route, payload=None):
            if route in (
                "pose",
                "open_gripper",
                "close_gripper",
                "close_gripper_slow",
                "move_gripper",
                "clearerr",
            ):
                if self._motion is None or self.stopped() or self.latch_state():
                    raise ValueError("motion HTTP route has no authority")
                token, kind = self._motion
                self.fence.check(token, kind, self.clock.now())
            elif route in (
                "jointreset",
                "startimp",
                "activate_gripper",
                "reset_gripper",
            ):
                raise ValueError("recovery/setup motion is not part of AERI")
            response = original_post(route, payload)
            if (
                route == "getstate"
                and isinstance(response, dict)
                and response.get("state_fresh") is False
            ):
                self.latched = "robot state explicitly stale"
                raise ValueError(self.latched)
            return response

        env._post = fenced_post

    def latch_state(self):
        return self.latched or (str(self.guard.tripped) if self.guard.tripped else None)

    def health(self):
        try:
            reading = self.guard.check(force=True)
            # An offline C3 is insufficient for this entry point's preflight.
            if reading.status != "ok":
                return {"state": "red", "reasons": list(reading.reasons)}
        except Exception as exc:  # noqa: BLE001 - hardware failure must lock motion  # a guard fault is a latched refusal
            self.latched = str(exc)
            return {"state": "red", "reasons": [str(exc)]}
        return {"state": "ok", "reasons": []}

    def preflight(self):
        health = self.health()
        return {
            "ok": health["state"] == "ok" and not self.latch_state(),
            "reasons": health["reasons"],
        }

    def observe(self):
        raw = self.env.get_observation()
        pose = [float(x) for x in raw["robot_state"]["cartesian_position"]]
        if len(pose) != 7 or not all(math.isfinite(x) for x in pose):
            raise ValueError("invalid robot observation")
        self.raw = raw
        images = {"side": raw["image"]["view1"], "wrist": raw["image"]["hand"]}
        signatures = {
            k: hashlib.sha256(v.tobytes()).hexdigest() for k, v in images.items()
        }
        return {
            "observed_ns": self.clock.now(),
            "pose": pose,
            "frames": signatures,
            "state_fresh": True,
            "images_bgr": images,
            "robot_state": raw["robot_state"],
            "raw": raw,
        }

    def stopped(self):
        return self.stop_event.is_set() or self.stop_probe()

    def hold(self):
        # For the impedance server, hold means cease all new pose commands.
        self.held = True

    def emergency_hold(self):
        self.stop_event.set()
        self.hold()

    def _send(self, action, token, kind):
        self.fence.check(token, kind, self.clock.now())
        if self.stopped() or self.latch_state() or self.health()["state"] != "ok":
            raise ValueError("motion is stopped or fault locked")
        current = self.env.get_observation()["robot_state"]["cartesian_position"]
        fixed, notes = self.client._clamp_action(action, current, self.args)
        # Reject rather than change the frozen motion or home target silently.
        if notes or len(fixed) != 7 or not all(math.isfinite(float(x)) for x in fixed):
            raise ValueError(
                "action refused by workspace/step guard: " + ", ".join(notes)
            )
        self.fence.check(token, kind, self.clock.now())
        if self.stopped():
            raise ValueError("operator stop")
        self.held = False
        # env is already guarded. Any exception/None after this call may have
        # delivered a pose: report unknown, never retry the command.
        self._sending = True
        self._motion = (token, kind)
        try:
            result = self.env.step(fixed)
        finally:
            self._motion = None
        if result is None:
            raise RuntimeError("guarded step has no delivery acknowledgement")
        self._sending = False
        self.steps += 1
        return result

    def step(self, action, *, token):
        self._sending = False
        try:
            self._send(action, token, "policy_steps")
        except Exception as exc:  # noqa: BLE001 - hardware failure must lock motion
            self.latched = str(exc)
            return StepResult(
                "unknown" if self._sending else "no", str(exc), self.steps
            )
        return StepResult("yes", "guarded_step", self.steps)

    def home(self, *, token):
        self._sending = False
        moved = False
        adapter = self

        class HomingEnv:
            def get_observation(self):
                return adapter.env.get_observation()

            def step(self, action):
                nonlocal moved
                result = adapter._send(action, token, "home")
                moved = True
                return result

        try:
            self.client._go_home(HomingEnv(), self.args)
            final_observation = self.env.get_observation()
            observed = final_observation["robot_state"]["cartesian_position"]
            expected = self.client._resolve_home(self.args)
            error = 1000 * math.sqrt(
                sum(
                    (float(a) - float(b)) ** 2
                    for a, b in zip(observed[:3], expected[:3], strict=True)
                )
            )
            rotation = max(
                abs(
                    math.atan2(
                        math.sin(float(a) - float(b)), math.cos(float(a) - float(b))
                    )
                )
                for a, b in zip(observed[3:6], expected[3:6], strict=True)
            )
            if (
                error > self.tolerance_mm
                or rotation > 0.05
                or float(final_observation["robot_state"]["gripper_position"]) < 0.9
            ):
                self.latched = "home tolerance or open gripper not verified"
                return HomeResult(
                    "unknown", self.latched, max(error, self.tolerance_mm + 1)
                )
            return HomeResult("yes", "home_verified", error)
        except Exception as exc:  # noqa: BLE001 - hardware failure must lock motion
            self.latched = str(exc)
            return HomeResult("unknown" if moved or self._sending else "no", str(exc))


class Policy:
    """One worker at a time; a still-running query prevents quiesce ack."""

    def __init__(
        self, clients, deployments, prompts, robot, client_module, args, clock
    ):
        self.clients, self.deployments, self.prompts = clients, deployments, prompts
        self.robot, self.client, self.args, self.clock = (
            robot,
            client_module,
            args,
            clock,
        )
        self.role = None
        self.epoch = -1
        self.ticket = None
        self.lock = threading.Lock()

    def acquire(self, role, *, policy_epoch):
        if self.ticket is not None and not self.ticket.done.is_set():
            return {"kind": "unavailable", "code": "query_still_running"}
        try:
            metadata = self.clients[role].get_server_metadata()
            check_identity(self.deployments[role], metadata)
            self.client._check_server(metadata, self.args)
            self.client._check_prompt(self.prompts[role], self.args, metadata)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - legacy client refuses with SystemExit
            return {
                "kind": "unavailable",
                "code": "deployment_mismatch",
                "detail": str(exc),
            }
        self.role, self.epoch = role, policy_epoch
        return {
            "handle_id": f"real-{role}-{policy_epoch}",
            "policy_epoch": policy_epoch,
            "role": role,
        }

    def request(self, request):
        with self.lock:
            if (
                self.role is None
                or request["policy_epoch"] != self.epoch
                or self.robot.raw is None
            ):
                return {"kind": "unavailable", "code": "not_acquired"}
            if self.ticket is not None and not self.ticket.done.is_set():
                return {"kind": "unavailable", "code": "busy"}
            ticket = SimpleNamespace(
                request=dict(request), done=threading.Event(), result=None
            )
            self.ticket = ticket
            role, raw = self.role, self.robot.raw

        def work():
            began = time.monotonic()
            try:
                actions = self.client._query_policy(
                    self.clients[role],
                    raw,
                    raw["robot_state"]["cartesian_position"],
                    self.prompts[role],
                )
                ticket.result = {
                    "schema": aeri.SCHEMAS["runtime"],
                    "minor": 0,
                    "run_id": request["run_id"],
                    "emitted_wall_ns": time.time_ns(),
                    "kind": "chunk_response",
                    "request_id": request["request_id"],
                    "episode_id": request["episode_id"],
                    "policy_epoch": request["policy_epoch"],
                    "action_contract": "fr3-robotiq@1",
                    "actions": actions.tolist(),
                    "valid_from_action_index": request["action_start_index"],
                    "prefix_conditioning_applied": "none",
                    "model_version": {
                        "config": self.deployments[role]["config"],
                        "checkpoint_dir_name": Path(
                            self.deployments[role]["checkpoint_dir"]
                        ).name,
                    },
                    "timing": {"total_ms": int((time.monotonic() - began) * 1000)},
                    "clock_domain": request["clock_domain"],
                }
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - legacy client refuses with SystemExit
                ticket.result = {
                    "kind": "unavailable",
                    "code": "policy_error",
                    "detail": str(exc),
                }
            finally:
                ticket.done.set()

        threading.Thread(target=work, name="aeri-policy-query", daemon=True).start()
        return ticket

    def collect(self, ticket, *, timeout_ns):
        if not ticket.done.wait(timeout_ns / 1e9):
            return None
        return ticket.result

    def quiesce(self, *, policy_epoch):
        with self.lock:
            busy = self.ticket is not None and not self.ticket.done.is_set()
            self.role = None
            self.epoch = max(policy_epoch, self.epoch)
            return {
                "ok": not busy,
                "fenced_epoch": policy_epoch,
                "inflight_cancelled": 0,
            }
