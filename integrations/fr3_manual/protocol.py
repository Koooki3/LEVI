"""Offline-testable serialized manual admission. No ROS or network imports.

Adapter calls must be nonblocking and local. The lock spans admission and publish.
A publish receipt is explicitly not evidence of physical stop.
"""

import ipaddress
import math
import threading
import time
import uuid


class Refused(ValueError):
    pass


def finite(x):
    try:
        return type(x) in (int, float) and math.isfinite(x)
    except OverflowError:
        return False


def vector(x, n):
    return type(x) in (list, tuple) and len(x) == n and all(finite(v) for v in x)


def schema(data, keys):
    if type(data) is not dict or set(data) != set(keys):
        raise Refused("schema_invalid")


LIMIT_KEYS = {
    "box_min_m",
    "box_max_m",
    "max_delta_m",
    "max_step_m",
    "max_velocity_m_s",
    "period_s",
    "max_reading_age_s",
    "max_anchor_age_s",
    "deadman_timeout_s",
    "target_timeout_s",
    "max_orientation_drift_rad",
    "quaternion_norm_tolerance",
    "max_settle_age_s",
}


def limits_checked(value):
    schema(value, LIMIT_KEYS)
    result = dict(value)
    if not vector(result["box_min_m"], 3) or not vector(result["box_max_m"], 3):
        raise Refused("limits_invalid")
    if any(a >= b for a, b in zip(result["box_min_m"], result["box_max_m"])):
        raise Refused("limits_invalid")
    for key in LIMIT_KEYS - {"box_min_m", "box_max_m"}:
        if not finite(result[key]) or result[key] <= 0:
            raise Refused("limits_invalid")
    if (
        result["quaternion_norm_tolerance"] >= 1
        or result["max_orientation_drift_rad"] > math.pi
    ):
        raise Refused("limits_invalid")
    if not finite(result["max_velocity_m_s"] * result["period_s"]):
        raise Refused("limits_invalid")
    if not math.isfinite(math.dist(result["box_min_m"], result["box_max_m"])):
        raise Refused("limits_invalid")
    result["box_min_m"] = tuple(result["box_min_m"])
    result["box_max_m"] = tuple(result["box_max_m"])
    return result


class Protocol:
    def __init__(self, adapter, now=time.monotonic):
        self.adapter, self.now = adapter, now
        self.lock = threading.RLock()
        self.instance_id = str(uuid.uuid4())
        self.session_id = None
        self.open = False
        self.sequence = -1
        self.deadline = None
        self.stop_evidence = "none"
        self.closed_at = None
        self.in_flight = False
        self.reason = None
        self.anchor = None
        self.last_target = None
        self.last_send = None

    def capabilities(self):
        return {
            "protocol_version": 1,
            "instance_id": self.instance_id,
            "monotonic_time": self.now(),
            "clock_domain": "same_host_monotonic",
            "translation": True,
            "rotation": False,
            "gripper": self.adapter.gripper_supported(),
            "gripper_stop_provable": "measured_settle_and_terminal_result",
            "limitations": [
                "action cancellation does not prove physical gripper stop",
                "hold publication does not prove physical arm stop",
            ],
        }

    def _sample(self):
        sample = self.adapter.sample()
        if not vector(sample.get("pose7"), 7) or not vector(sample.get("joints"), 7):
            raise Refused("feedback_invalid")
        now = self.now()
        for field in ("monotonic_time", "joint_monotonic_time"):
            if (
                not finite(sample.get(field))
                or not 0 <= now - sample[field] <= self.limits["max_reading_age_s"]
            ):
                raise Refused("feedback_stale")
        if (
            type(sample.get("seq")) is not int
            or type(sample.get("joint_seq")) is not int
        ):
            raise Refused("feedback_invalid")
        if not sample.get("frame"):
            raise Refused("feedback_invalid")
        norm = math.sqrt(sum(v * v for v in sample["pose7"][3:]))
        if abs(norm - 1) > self.limits["quaternion_norm_tolerance"]:
            raise Refused("quaternion_invalid")
        return sample

    def _identity(self, data):
        if self.session_id is None or data.get("session_id") != self.session_id:
            raise Refused("session_mismatch")

    def _admit(self, data):
        self._identity(data)
        self.tick()
        if not self.open:
            raise Refused("admission_closed")
        seq, expiry = data.get("sequence"), data.get("expires_at_mono")
        if type(seq) is not int or seq <= self.sequence:
            raise Refused("sequence_replay")
        if (
            not finite(expiry)
            or not self.now() < expiry <= self.now() + self.limits["target_timeout_s"]
        ):
            raise Refused("deadline_invalid")
        self.sequence = seq
        return expiry

    def start(self, data):
        schema(data, {"session_id", "limits", "stop_limits"})
        if (
            type(data["session_id"]) is not str
            or not 0 < len(data["session_id"].strip()) <= 128
        ):
            raise Refused("session_invalid")
        limits = limits_checked(data["limits"])
        schema(
            data["stop_limits"],
            {
                "settle_window_s",
                "settle_position_m",
                "settle_angle_rad",
                "gripper_settle_rad",
            },
        )
        if any(not finite(v) or v <= 0 for v in data["stop_limits"].values()):
            raise Refused("stop_limits_invalid")
        with self.lock:
            if self.session_id is not None or not self.adapter.legacy_idle():
                raise Refused("ownership_busy")
            self.stop_limits = dict(data["stop_limits"])
            self.limits = limits
            sample = self._sample()
            if any(
                not lo <= v <= hi
                for v, lo, hi in zip(
                    sample["pose7"][:3], limits["box_min_m"], limits["box_max_m"]
                )
            ):
                raise Refused("anchor_outside_box")
            if self.adapter.gripper_supported():
                grip = self.adapter.gripper_state()
                if (
                    not finite(grip.get("monotonic_time"))
                    or not 0
                    <= self.now() - grip["monotonic_time"]
                    <= limits["max_reading_age_s"]
                ):
                    raise Refused("gripper_feedback_stale")
            self.session_id = data["session_id"]
            self.anchor = dict(sample)
            self.last_target = list(sample["pose7"])
            self.sequence = -1
            self.open = True
            self.stop_evidence = "none"
            self.reason = None
            self.deadline = None  # First explicit held control arms the deadman.
            self.last_send = None
            self.settle_samples = []
            self.gripper_deadline = None
            self.target_deadline = None
            return self.state()

    def heartbeat(self, data):
        schema(data, {"session_id", "sequence", "expires_at_mono"})
        with self.lock:
            expiry = self._admit(data)
            self.deadline = min(expiry, self.now() + self.limits["deadman_timeout_s"])
            return self.state()

    def pose(self, data):
        schema(data, {"session_id", "sequence", "expires_at_mono", "pose7"})
        with self.lock:
            expiry = self._admit(data)
            if self.deadline is None:
                raise Refused("deadman_required")
            target = data["pose7"]
            if not vector(target, 7):
                raise Refused("pose_invalid")
            sample = self._sample()
            if sample["frame"] != self.anchor["frame"]:
                raise Refused("frame_changed")
            # First implementation deliberately requires exact anchor quaternion.
            if list(target[3:]) != list(self.anchor["pose7"][3:]):
                raise Refused("rotation_unsupported")
            qa, qb = sample["pose7"][3:], target[3:]
            dot = abs(sum(a * b for a, b in zip(qa, qb))) / math.sqrt(
                sum(a * a for a in qa) * sum(b * b for b in qb)
            )
            if 2 * math.acos(min(1, dot)) > self.limits["max_orientation_drift_rad"]:
                raise Refused("orientation_drift")
            if (
                self.now() - self.anchor["monotonic_time"]
                > self.limits["max_anchor_age_s"]
            ):
                raise Refused("anchor_expired")
            if any(
                not lo <= v <= hi
                for v, lo, hi in zip(
                    target[:3], self.limits["box_min_m"], self.limits["box_max_m"]
                )
            ):
                raise Refused("outside_box")
            step = math.dist(target[:3], self.last_target[:3])
            if math.dist(target[:3], sample["pose7"][:3]) > self.limits["max_step_m"]:
                raise Refused("measured_step_limit")
            budget = min(
                self.limits["max_step_m"],
                self.limits["max_velocity_m_s"] * self.limits["period_s"],
            )
            # Subtracting world coordinates can round an exactly budgeted
            # increment upward. Permit only relative floating-point roundoff.
            if step > budget and not math.isclose(
                step, budget, rel_tol=1e-12, abs_tol=0.0
            ):
                raise Refused("step_limit")
            if (
                math.dist(target[:3], self.anchor["pose7"][:3])
                > self.limits["max_delta_m"]
            ):
                raise Refused("anchor_delta")
            if (
                self.last_send is not None
                and self.now() - self.last_send < self.limits["period_s"]
            ):
                raise Refused("rate_limit")
            if self.in_flight:
                raise Refused("busy")
            self.in_flight = True
            try:
                # Recheck immediately before physical dispatch, under the same lock.
                if self.now() >= expiry or self.now() >= self.deadline or not self.open:
                    self._stop("expired_before_dispatch")
                    raise Refused("expired_before_dispatch")
                self.adapter.publish(list(target))
                self.last_target, self.last_send = list(target), self.now()
                self.target_deadline = expiry
            except Refused:
                raise
            except Exception:  # noqa: BLE001 - unknown driver state retains ownership
                self._stop("publish_failed")
                raise Refused("publish_failed")
            finally:
                self.in_flight = False
            return self.state()

    def gripper(self, data):
        schema(data, {"session_id", "sequence", "expires_at_mono", "opening_ratio"})
        with self.lock:
            self._admit(data)
            if self.deadline is None:
                raise Refused("deadman_required")
            if not finite(data["opening_ratio"]) or not 0 <= data["opening_ratio"] <= 1:
                raise Refused("opening_invalid")
            if not self.adapter.gripper_supported():
                raise Refused("gripper_unavailable")
            if not self.adapter.gripper_terminal():
                raise Refused("gripper_busy")
            self.gripper_deadline = data["expires_at_mono"]
            # Recheck after all adapter callbacks, immediately before dispatch.
            if self.now() >= self.gripper_deadline or self.now() >= self.deadline:
                self._stop("expired_before_dispatch")
                raise Refused("expired_before_dispatch")
            # Nonblocking send only; no queued reversals.
            self.adapter.begin_gripper(data["opening_ratio"])
            return self.state()

    def _stop(self, reason):
        self.open = False
        self.reason, self.closed_at = reason, self.now()
        self.stop_evidence = "unproven"
        self.settle_samples = []
        self.adapter.cancel_gripper()
        try:
            sample = self._sample()
            self.stop_sample = (sample["seq"], sample["joint_seq"])
            self.adapter.publish(list(sample["pose7"]))
            self.stop_evidence = "hold_published_unproven"
        except Exception:  # noqa: BLE001 - unknown driver state retains ownership
            self.stop_evidence = "hold_failed_unproven"

    def stop(self, data):
        schema(data, {"session_id"})
        with self.lock:
            self._identity(data)
            if self.open:
                self._stop("operator_stop")
            return self.state()

    def end(self, data):
        schema(data, {"session_id"})
        with self.lock:
            self._identity(data)
            if self.open:
                raise Refused("stop_required")
            sample = self._sample()
            if (
                not hasattr(self, "stop_sample")
                or sample["seq"] <= self.stop_sample[0]
                or sample["joint_seq"] <= self.stop_sample[1]
            ):
                raise Refused("post_stop_feedback_required")
            # Adapter must prove driver-level settled/cancel completion, never HTTP ack.
            if not self._settled(sample):
                raise Refused("physical_stop_unproven")
            self.session_id = None
            self.stop_evidence = "proven"
            return self.state()

    def _reset_settle(self):
        self.settle_samples = []
        if self.stop_evidence == "measured_settled":
            self.stop_evidence = "hold_published_unproven"

    def _settled(self, sample):
        gripper = self.adapter.gripper_state()
        now = self.now()
        if self.stop_evidence not in ("hold_published_unproven", "measured_settled"):
            return False
        if self.adapter.gripper_supported():
            stamp = gripper.get("monotonic_time")
            if (
                not self.adapter.gripper_terminal()
                or not finite(stamp)
                or not 0 <= now - stamp <= self.limits["max_reading_age_s"]
                or stamp <= self.closed_at
                or not finite(gripper.get("theta"))
            ):
                self._reset_settle()
                return False
        if (
            sample["monotonic_time"] <= self.closed_at
            or sample["joint_monotonic_time"] <= self.closed_at
            or sample["frame"] != self.anchor["frame"]
        ):
            self._reset_settle()
            return False
        row = (
            sample["seq"],
            sample["joint_seq"],
            gripper.get("seq"),
            now,
            list(sample["pose7"]),
            list(sample["joints"]),
            gripper.get("theta", 0),
        )
        if self.settle_samples:
            prev = self.settle_samples[-1]
            if (
                row[0] == prev[0]
                or row[1] == prev[1]
                or (self.adapter.gripper_supported() and row[2] == prev[2])
            ):
                return self.stop_evidence == "measured_settled"
            if (
                row[0] < prev[0]
                or row[1] < prev[1]
                or (self.adapter.gripper_supported() and row[2] < prev[2])
                or now - prev[3] > self.limits["max_reading_age_s"]
            ):
                self._reset_settle()
        self.settle_samples.append(row)
        first = self.settle_samples[0]
        cfg = self.stop_limits
        unstable = any(
            math.dist(r[4][:3], first[4][:3]) > cfg["settle_position_m"]
            or 2
            * math.acos(
                min(
                    1,
                    abs(sum(a * b for a, b in zip(r[4][3:], first[4][3:])))
                    / math.sqrt(
                        sum(a * a for a in r[4][3:]) * sum(b * b for b in first[4][3:])
                    ),
                )
            )
            > cfg["settle_angle_rad"]
            or max(abs(a - b) for a, b in zip(r[5], first[5])) > cfg["settle_angle_rad"]
            or abs(r[6] - first[6]) > cfg["gripper_settle_rad"]
            for r in self.settle_samples
        )
        if unstable:
            self.stop_evidence = "hold_published_unproven"
            self.settle_samples = [row]
            return False
        # Each distinct sample was checked against the window's first sample.
        # Keep only the first and newest: long-held sessions stay constant-space
        # and the watchdog never accumulates an O(n^2) history scan.
        self.settle_samples = [first, row] if first is not row else [row]
        if now - first[3] < cfg["settle_window_s"]:
            return False
        self.stop_evidence = "measured_settled"
        return True

    def tick(self):
        with self.lock:
            if self.open:
                try:
                    self._sample()
                    if self.adapter.gripper_supported():
                        grip = self.adapter.gripper_state()
                        if (
                            not finite(grip.get("monotonic_time"))
                            or not 0
                            <= self.now() - grip["monotonic_time"]
                            <= self.limits["max_reading_age_s"]
                        ):
                            raise Refused("gripper_feedback_stale")
                except Exception:  # noqa: BLE001 - unknown driver state retains ownership
                    self._stop("feedback_stale_or_invalid")
            if (
                self.open
                and self.target_deadline is not None
                and self.now() >= self.target_deadline
            ):
                self._stop("target_deadline_expired")
            if self.open and self.deadline is not None and self.now() >= self.deadline:
                self._stop("deadman_expired")
            elif (
                self.open
                and self.gripper_deadline is not None
                and not self.adapter.gripper_terminal()
                and self.now() >= self.gripper_deadline
            ):
                self._stop("gripper_deadline_expired")
            if self.session_id is not None and not self.open:
                try:
                    self._settled(self._sample())
                except Refused:
                    self._reset_settle()

    def legacy(self, handler, *args, **kwargs):
        with self.lock:
            if self.session_id is not None:
                raise Refused("manual_owns_robot")
            return handler(*args, **kwargs)

    def state(self):
        with self.lock:
            self.tick()
            sample = self.adapter.sample()
            sample = dict(
                sample,
                age_s=None
                if sample.get("monotonic_time") is None
                else self.now() - sample["monotonic_time"],
            )
            return {
                "instance_id": self.instance_id,
                "session_id": self.session_id,
                "admission_open": self.open,
                "stop_evidence": self.stop_evidence,
                "reason": self.reason,
                "sample": sample,
                "gripper": self.adapter.gripper_state(),
                "in_flight": self.in_flight,
            }


def install_flask(app, protocol, request, jsonify):
    """Register routes and serialize legacy mutation dispatch atomically.

    Hook wraps actual view dispatch, rather than a racy before_request gate.
    All existing POST routes are conservatively treated as potential mutations.
    """
    old = app.dispatch_request

    def dispatch():
        if request.path.startswith("/manual/v1/") or request.method != "POST":
            return old()
        try:
            return protocol.legacy(old)
        except Refused as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    app.dispatch_request = dispatch

    def route(operation):
        def endpoint():
            try:
                if request.method == "POST":
                    try:
                        local = ipaddress.ip_address(request.remote_addr).is_loopback
                    except (ValueError, TypeError):
                        local = False
                    if not local:
                        raise Refused("loopback_required")
                    data = request.get_json(silent=True)
                    result = getattr(protocol, operation)(data)
                else:
                    result = getattr(protocol, operation)()
                return jsonify(dict(result, ok=True))
            except Refused as exc:
                return jsonify({"ok": False, "error": str(exc)}), 409

        return endpoint

    for name in (
        "capabilities",
        "state",
        "start",
        "heartbeat",
        "pose",
        "gripper",
        "stop",
        "end",
    ):
        app.add_url_rule(
            "/manual/v1/" + name,
            "manual_v1_" + name,
            route(name),
            methods=["GET" if name in ("capabilities", "state") else "POST"],
        )
    event = threading.Event()

    def watchdog():
        while not event.wait(0.01):
            protocol.tick()

    threading.Thread(target=watchdog, name="manual-deadman", daemon=True).start()
    return event


class AsyncGoal:
    """One action request; cancel late acceptance, never retain a reversal.

    send/cancel/get-result are asynchronous local API calls only. Unknown/error
    completion stays nonterminal and retains manual ownership.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.generation = 0
        self.terminal = True
        self.cancelled = False
        self.handle = None
        self.status = "idle"

    def begin(self, send):
        with self.lock:
            if not self.terminal:
                raise Refused("gripper_busy")
            self.generation += 1
            token = self.generation
            self.terminal, self.cancelled = False, False
            self.handle, self.status = None, "awaiting_acceptance"
            try:
                future = send()
                future.add_done_callback(lambda fut: self._accepted(fut, token))
            except Exception:  # noqa: BLE001 - unknown driver state retains ownership
                self.status = "send_unknown"
                raise Refused("gripper_send_unknown")

    def _accepted(self, future, token):
        with self.lock:
            if token != self.generation:
                return
            try:
                handle = future.result()
                if handle is None or not handle.accepted:
                    self.terminal, self.status = True, "rejected"
                    return
                self.handle, self.status = handle, "active"
                handle.get_result_async().add_done_callback(
                    lambda fut: self._result(fut, token)
                )
                if self.cancelled:
                    # Goal may be accepted after stop; cancel this exact handle.
                    handle.cancel_goal_async()
                    self.status = "cancel_requested"
            except Exception:  # noqa: BLE001 - unknown driver state retains ownership
                self.status = "acceptance_unknown"

    def _result(self, future, token):
        with self.lock:
            if token != self.generation:
                return
            try:
                result = future.result()
                # action_msgs GoalStatus SUCCEEDED/CANCELED/ABORTED only.
                if result.status in (4, 5, 6):
                    self.terminal, self.status = True, "terminal_" + str(result.status)
                else:
                    self.status = "result_nonterminal"
            except Exception:  # noqa: BLE001 - unknown driver state retains ownership
                self.status = "result_unknown"

    def cancel(self):
        with self.lock:
            self.cancelled = True
            if not self.terminal and self.handle is not None:
                try:
                    self.handle.cancel_goal_async()
                    self.status = "cancel_requested"
                except Exception:  # noqa: BLE001 - unknown driver state retains ownership
                    self.status = "cancel_unknown"
