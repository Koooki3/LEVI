"""Explicit person-started FR3 v1 transport. Construction performs no I/O.

No proxy, redirect, retry or URL from a browser. GET session views use cached
state only. Unknown start delivery creates a retained fault owner.
"""

import http.client
import ipaddress
import json
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .manual import (
    Dependencies,
    Engine,
    Limits,
    ManualRefused,
    Reading,
    _number,
    _vector,
)
from .manual_worker import Ack, PoseWorker

STOP_KEYS = {
    "settle_window_s",
    "settle_position_m",
    "settle_angle_rad",
    "gripper_settle_rad",
}


def stop_limits(value):
    if (
        type(value) is not dict
        or set(value) != STOP_KEYS
        or any(not _number(v) or v <= 0 for v in value.values())
    ):
        raise ManualRefused("stop_limits_required")
    return dict(value)


@dataclass(frozen=True)
class PhysicalSettledEvidence:
    session_id: str
    instance_id: str
    generation: int
    observed_at_mono: float
    remote_end_proven: bool


class Transport:
    def __init__(self, url, session_id, limits, *, now=time.monotonic, request=None):
        parsed = urlsplit(url)
        try:
            local = (
                parsed.hostname == "localhost"
                or ipaddress.ip_address(parsed.hostname).is_loopback
            )
        except (ValueError, TypeError):
            local = False
        if (
            parsed.scheme != "http"
            or not local
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ManualRefused("manual_server_url_invalid")
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.session_id, self.limits, self.now = session_id, limits, now
        self.request = request or self._http
        self.instance_id = None
        self.cached = None
        self.lock = threading.Lock()
        self.dispatch_lock = threading.Lock()
        self.sequence = -1
        self.closed = False
        self.stop_started = False
        self.deadline = None
        self.error = None
        self.pending_requests = 0
        self.start_attempted = False
        self.end_proven = False

    def _http(self, method, path, body):
        # http.client ignores proxy environment and never follows redirects/retries.
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=self.limits.target_timeout_s
        )
        try:
            encoded = (
                None if body is None else json.dumps(body, allow_nan=False).encode()
            )
            connection.request(
                method,
                "/manual/v1/" + path,
                body=encoded,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ManualRefused("manual_remote_oversize")
            doc = json.loads(raw)
            if response.status != 200:
                code = doc.get("error") if type(doc) is dict else None
                if type(code) is str and re.fullmatch(r"[a-z0-9_]{1,96}", code):
                    raise ManualRefused("manual_remote_" + code)
                raise ManualRefused("manual_remote_refused")
            if type(doc) is not dict or doc.get("ok") is not True:
                raise ManualRefused("manual_remote_invalid")
            return doc
        finally:
            connection.close()

    def _call(self, method, path, body=None):
        with self.lock:
            self.pending_requests += 1
        try:
            doc = self.request(method, path, body)
            if type(doc) is not dict or doc.get("ok") is not True:
                raise ManualRefused("manual_remote_invalid")
            return doc
        finally:
            with self.lock:
                self.pending_requests -= 1

    def _bound(self, doc, *, ended=False):
        if doc.get("instance_id") != self.instance_id:
            raise ManualRefused("manual_instance_changed")
        if doc.get("session_id") != (None if ended else self.session_id):
            raise ManualRefused("manual_remote_session_changed")
        self.cached = doc
        return doc

    def start(self, limits_body, stop_body):
        capabilities = self._call("GET", "capabilities")
        if (
            capabilities.get("protocol_version") != 1
            or capabilities.get("clock_domain") != "same_host_monotonic"
            or capabilities.get("translation") is not True
        ):
            raise ManualRefused("manual_protocol_unsupported")
        instance = capabilities.get("instance_id")
        stamp = capabilities.get("monotonic_time")
        if (
            type(instance) is not str
            or not instance
            or not _number(stamp)
            or not 0 <= self.now() - stamp <= self.limits.max_reading_age_s
        ):
            raise ManualRefused("manual_clock_or_identity_invalid")
        self.instance_id = instance
        self.start_attempted = True
        return self._bound(
            self._call(
                "POST",
                "start",
                {
                    "session_id": self.session_id,
                    "limits": limits_body,
                    "stop_limits": stop_body,
                },
            )
        )

    def refresh(self):
        return self._bound(self._call("GET", "state"))

    def reading(self):
        doc = self.cached
        sample = {} if doc is None else doc.get("sample", {})
        pose, joints = sample.get("pose7"), sample.get("joints")
        stamp, joint_stamp = (
            sample.get("monotonic_time"),
            sample.get("joint_monotonic_time"),
        )
        if (
            not _vector(pose, 7)
            or not _vector(joints, 7)
            or not _number(stamp)
            or not _number(joint_stamp)
        ):
            raise ManualRefused("manual_feedback_invalid")
        if (
            not sample.get("frame")
            or type(sample.get("seq")) is not int
            or type(sample.get("joint_seq")) is not int
        ):
            raise ManualRefused("manual_feedback_invalid")
        if not 0 <= self.now() - joint_stamp <= self.limits.max_reading_age_s:
            raise ManualRefused("manual_joint_feedback_stale")
        return Reading(
            str(sample["seq"]) + ":" + str(sample["joint_seq"]),
            min(stamp, joint_stamp),
            tuple(pose[:3]),
            tuple(pose[3:]),
            tuple(joints),
            True,
            True,
            True,
        )

    def mutation(self, path, extra=None, deadline=None):
        # Serialize sequence allocation with HTTP dispatch; stop bypasses this lock.
        with self.dispatch_lock:
            with self.lock:
                if self.closed:
                    raise ManualRefused("admission_closed")
                self.sequence += 1
                expires = min(
                    self.now() + self.limits.target_timeout_s,
                    deadline
                    if deadline is not None
                    else self.now() + self.limits.target_timeout_s,
                )
                body = dict(
                    session_id=self.session_id,
                    sequence=self.sequence,
                    expires_at_mono=expires,
                    **(extra or {}),
                )
            return self._bound(self._call("POST", path, body))

    def send(self, pose):
        try:
            self.mutation(
                "pose",
                {"pose7": list(pose.position) + list(pose.quaternion)},
                self.deadline,
            )
            return Ack(True, "manual_remote_ack")
        except Exception:  # noqa: BLE001 - unknown remote delivery must close admission
            self.close_admission()
            return Ack(False, "manual_delivery_unknown")

    def close_admission(self):
        with self.lock:
            self.closed = True
            if self.stop_started:
                return
            self.stop_started = True

        def stop():
            try:
                self._bound(self._call("POST", "stop", {"session_id": self.session_id}))
            except Exception:  # noqa: BLE001 - unknown remote delivery must close admission
                self.error = "manual_stop_unproven"

        threading.Thread(target=stop, daemon=True, name="manual-remote-stop").start()

    def release(self):
        if self.end_proven:
            return self.cached
        doc = self.refresh()
        if (
            doc.get("admission_open") is not False
            or doc.get("stop_evidence") != "measured_settled"
        ):
            raise ManualRefused("physical_stop_unproven")
        with self.lock:
            if self.pending_requests:
                raise ManualRefused("manual_request_in_flight")
        answer = self._bound(
            self._call("POST", "end", {"session_id": self.session_id}), ended=True
        )
        if answer.get("stop_evidence") != "proven":
            raise ManualRefused("physical_stop_unproven")
        self.end_proven = True
        return answer


class RealEngine(Engine):
    def __init__(self, config, transport):
        self.transport = transport
        self._last_refresh = transport.now()
        self._real_gripper_seq = -1
        super().__init__(
            session_id=config["session_id"],
            generation=config["generation"],
            anchor=transport.reading(),
            limits=transport.limits,
            dependencies=Dependencies(
                transport.now,
                transport.reading,
                lambda reading, pose: True,
                lambda doc: config["audit"]("manual_engine", detail=doc),
            ),
            worker=PoseWorker(transport),
        )

    def tick(self):
        if (
            self._state != "ended"
            and self.transport.now() - self._last_refresh >= self.limits.period_s
        ):
            try:
                state = self.transport.refresh()
                self._last_refresh = self.transport.now()
                if (
                    state.get("admission_open") is not True
                    and not self.worker.snapshot()["closed"]
                ):
                    self._close("fault_locked", "remote_admission_closed")
            except Exception:  # noqa: BLE001 - unknown remote delivery must close admission
                self._close("fault_locked", "manual_feedback_unavailable")
        return super().tick()

    def pump(self):
        with self._lock:
            self.transport.deadline = (
                min(self._pending.expires_at, self._deadman_until)
                if self._pending is not None and self._deadman_until is not None
                else self.transport.now()
            )
        return super().pump()

    def deadman(self, **kwargs):
        super().deadman(**kwargs)
        if kwargs["pressed"]:
            try:
                remote = self.transport.mutation(
                    "heartbeat", deadline=self._deadman_until
                )
                if remote.get("admission_open") is not True:
                    raise ManualRefused("remote_admission_closed")
            except Exception:  # noqa: BLE001 - unknown remote delivery must close admission
                self._close("fault_locked", "manual_heartbeat_unknown")
                raise ManualRefused("manual_heartbeat_unknown") from None
        return self.view()

    def gripper(self, *, session_id, generation, sequence, opening_ratio):
        self._binding(session_id, generation)
        with self._lock:
            if (
                self.worker.snapshot()["closed"]
                or not self._deadman
                or self.transport.now() >= self._deadman_until
            ):
                raise ManualRefused("deadman_required")
            if type(sequence) is not int or sequence <= self._real_gripper_seq:
                raise ManualRefused("sequence_replay")
            if not _number(opening_ratio) or not 0 <= opening_ratio <= 1:
                raise ManualRefused("opening_invalid")
            self._real_gripper_seq = sequence
            deadline = self.transport.now() + self.limits.target_timeout_s
        self._emit("gripper_prepared", sequence=sequence, opening_ratio=opening_ratio)
        try:
            self.transport.mutation(
                "gripper", {"opening_ratio": opening_ratio}, deadline
            )
        except Exception:  # noqa: BLE001 - unknown remote delivery must close admission
            self._close("fault_locked", "manual_gripper_unknown")
            raise ManualRefused("manual_gripper_unknown") from None
        return self.view()

    def physical_evidence(self):
        if not self.worker.snapshot()["closed"] or self.worker.snapshot()["in_flight"]:
            raise ManualRefused("physical_stop_unproven")
        try:
            self.transport.release()
        except ManualRefused:
            raise
        except Exception:  # noqa: BLE001 - unknown remote delivery must close admission
            raise ManualRefused("physical_stop_unproven") from None
        return PhysicalSettledEvidence(
            self.session_id,
            self.transport.instance_id,
            self.generation,
            self.transport.now(),
            True,
        )

    def end(self, evidence):
        if self._state == "ended":
            return self.view()
        if not isinstance(evidence, PhysicalSettledEvidence) or not (
            evidence.session_id == self.session_id
            and evidence.instance_id == self.transport.instance_id
            and evidence.generation == self.generation
            and evidence.remote_end_proven is True
            and self.transport.end_proven
            and 0
            <= self.transport.now() - evidence.observed_at_mono
            <= self.limits.max_settle_age_s
            and self.worker.snapshot()["closed"]
            and not self.worker.snapshot()["in_flight"]
        ):
            raise ManualRefused("physical_stop_unproven")
        with self._lock:
            self._state, self._reason = "ended", "physical_settled"
            self._stop_evidence = "physical_settled"
            self._settled += 1
        self._emit("ended_physical")
        return self.view()

    def view(self):
        result = super().view()
        result["capabilities"] = {
            "real_motion": True,
            "synthetic_only": False,
            "gripper": True,
        }
        if self._state != "ended" and self.worker.snapshot()["closed"]:
            result["stop_evidence"] = (self.transport.cached or {}).get(
                "stop_evidence", "unproven"
            )
        result["measured"] = self.transport.cached
        return result


class LockedRealEngine:
    """A start may have reached the robot. Never return this arm lock to idle."""

    def __init__(self, config, transport, reason):
        self.config, self.transport, self.limits = config, transport, transport.limits
        self.reason = reason
        self.transport.close_admission()

    def view(self):
        return {
            "schema": "levi.marvel0.manual.v1",
            "session_id": self.config["session_id"],
            "generation": self.config["generation"],
            "anchor_id": None,
            "anchor": None,
            "state": "fault_locked",
            "reason_code": self.reason,
            "admission": "closed",
            "stop_evidence": "unproven",
            "closed_generation": 1,
            "counters": {"accepted": 0, "sent": 0, "acknowledged": 0, "settled": 0},
            "target": None,
            "deadman": {"pressed": False, "sequence": -1},
            "in_flight": False,
            "capabilities": {
                "real_motion": True,
                "synthetic_only": False,
                "gripper": False,
            },
        }

    def stop(self, reason="person"):
        self.transport.close_admission()
        return self.view()

    def tick(self):
        return self.view()

    def pump(self):
        return self.view()

    def physical_evidence(self):
        raise ManualRefused("physical_stop_unproven")

    def end(self, evidence):
        raise ManualRefused("physical_stop_unproven")

    def target(self, **kwargs):
        raise ManualRefused("admission_closed")

    def deadman(self, **kwargs):
        raise ManualRefused("admission_closed")

    def gripper(self, **kwargs):
        raise ManualRefused("admission_closed")


def create(config):
    limits = Limits(**config["limits"])
    stops = stop_limits(config.get("stop_limits"))
    transport = Transport(
        config["robot_server_url"],
        config["session_id"],
        limits,
        now=config.get("now", time.monotonic),
        request=config.get("request"),
    )
    try:
        transport.start(config["limits"], stops)
        return RealEngine(config, transport)
    except Exception as exc:
        if not transport.start_attempted:
            if isinstance(exc, ManualRefused):
                raise
            raise ManualRefused("manual_server_unavailable") from None
        # Even a refused/malformed/timeout reply can follow a delivered start.
        return LockedRealEngine(config, transport, "manual_start_unknown")
