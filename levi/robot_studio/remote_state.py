"""Display-only measured FR3/2F85 telemetry from the fixed configured server.

Only GET /manual/v1/state. No session, motion, controller or arm-lock operation.
The existing product profile and explicit manual configuration gate this source.
Robot Server/Desk EE is labelled separately from the model's flange transform.
"""

import http.client
import ipaddress
import json
import math
import threading
import time
from urllib.parse import urlsplit

from . import state

TIMEOUT_S = 0.2
CACHE_S = 0.1
MAX_BYTES = 64 * 1024
END_FRAME = "robot_server_desk_EE"


def configured_url():
    """None selects the existing file reader; this does not contact a server."""
    from levi.marvel0 import control_site, site, stack

    if (
        stack.profile(stack._read_self_cgroup(), stack._in_main_checkout())["profile"]
        != "real"
    ):
        return None
    try:
        loaded = control_site.load()
    except Exception:  # noqa: BLE001 - unreadable enable switch is off
        return None
    if loaded is None or loaded.manual_errors or loaded.manual.enabled is not True:
        return None
    return site.require().server.robot_server_url


def _address(url):
    try:
        parsed = urlsplit(url)
        local = (
            parsed.hostname == "localhost"
            or ipaddress.ip_address(parsed.hostname).is_loopback
        )
        port = parsed.port or 80
    except (TypeError, ValueError):
        raise state.Invalid("remote_url_invalid") from None
    if (
        parsed.scheme != "http"
        or not local
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise state.Invalid("remote_url_invalid")
    return parsed.hostname, port


def fetch(url):
    host, port = _address(url)
    # http.client ignores proxy variables and does not follow redirects/retry.
    connection = http.client.HTTPConnection(host, port, timeout=TIMEOUT_S)
    try:
        connection.request(
            "GET", "/manual/v1/state", headers={"Accept": "application/json"}
        )
        response = connection.getresponse()
        raw = response.read(MAX_BYTES + 1)
        if response.status != 200:
            raise state.Invalid("remote_http_failed")
        if len(raw) > MAX_BYTES:
            raise state.Invalid("remote_too_large")
        return raw
    finally:
        connection.close()


def _decode(raw):
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise state.Invalid("remote_too_large")
    try:
        doc = json.loads(
            raw, object_pairs_hook=state._object, parse_constant=state._reject_constant
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise state.Invalid("remote_invalid_json") from None
    if (
        type(doc) is not dict
        or doc.get("ok") is not True
        or not state._text(doc.get("instance_id"), 64)
    ):
        raise state.Invalid("remote_identity_invalid")
    sample, grip = doc.get("sample"), doc.get("gripper")
    if type(sample) is not dict:
        raise state.Invalid("remote_feedback_missing")
    if not state._vector(sample.get("pose7"), 7) or not state._vector(
        sample.get("joints"), 7
    ):
        raise state.Invalid("remote_arm_invalid")
    if (
        not state._text(sample.get("frame"), 128)
        or abs(math.hypot(*sample["pose7"][3:]) - 1) > state.QUATERNION_NORM_TOLERANCE
    ):
        raise state.Invalid("remote_pose_invalid")
    # theta is actual measured knuckle angle, never command ratio/open default.
    grip_reason = None
    if (
        type(grip) is not dict
        or grip.get("supported") is not True
        or not state._number(grip.get("theta"))
    ):
        grip_reason = "remote_gripper_missing"
    channels = {
        "pose": (sample.get("seq"), sample.get("monotonic_time"), sample["pose7"]),
        "joint": (
            sample.get("joint_seq"),
            sample.get("joint_monotonic_time"),
            sample["joints"],
        ),
    }
    if grip_reason is None:
        channels["gripper"] = (
            grip.get("seq"),
            grip.get("monotonic_time"),
            grip["theta"],
        )
    for name, (seq, stamp, _) in list(channels.items()):
        if (
            type(seq) is not int
            or not 0 < seq <= state.MAX_SAFE_INTEGER
            or not state._number(stamp)
            or stamp < 0
        ):
            if name == "gripper":
                grip_reason = "remote_gripper_identity_invalid"
                del channels[name]
            else:
                raise state.Invalid("remote_sample_identity_invalid")
    return doc, channels, grip_reason


class Reader:
    """Single fixed-source cache, serialized across simultaneous browser tabs."""

    def __init__(self, *, request=fetch, monotonic=time.monotonic, epoch=time.time):
        self.request, self.monotonic, self.epoch = request, monotonic, epoch
        self.lock = threading.Lock()
        self.url = None
        self.cached = None
        self.fetched = None
        self.failure = None
        self.instance = None
        self.previous = {}

    def snapshot(self, url):
        with self.lock:
            now = self.monotonic()
            if not state._number(now) or now < 0:
                return state._document("unavailable", "clock_invalid")
            if self.url != url:
                self.url = url
                self.cached = None
                self.fetched = None
                self.instance = None
                self.previous = {}
            if (
                self.fetched is None
                or now - self.fetched >= CACHE_S
                or now < self.fetched
            ):
                try:
                    _address(url)
                    doc, channels, grip_reason = _decode(self.request(url))
                    if (
                        self.instance is not None
                        and self.instance != doc["instance_id"]
                    ):
                        self.instance = doc["instance_id"]
                        self.previous = {}
                        raise state.Invalid("remote_instance_changed")
                    self.instance = doc["instance_id"]
                    for name, (seq, stamp, value) in list(channels.items()):
                        old = self.previous.get(name)
                        if old and (seq < old[0] or stamp < old[1]):
                            if name == "gripper":
                                grip_reason = "remote_gripper_regressed"
                                del channels[name]
                                continue
                            raise state.Invalid("remote_sample_regressed")
                        if (
                            old
                            and seq == old[0]
                            and (stamp != old[1] or value != old[2])
                        ):
                            if name == "gripper":
                                grip_reason = "remote_gripper_identity_reused"
                                del channels[name]
                                continue
                            raise state.Invalid("remote_sample_identity_reused")
                    self.previous.update(channels)
                    self.cached = (doc, channels, grip_reason)
                    self.failure = None
                except state.Invalid as exc:
                    self.cached = None
                    self.failure = str(exc)
                except Exception:  # noqa: BLE001 - display failures must never invent telemetry
                    self.cached = None
                    self.failure = "remote_unavailable"
                self.fetched = self.monotonic()
            if self.cached is None:
                return state._document(
                    "unavailable", self.failure or "remote_unavailable"
                )
            doc, channels, grip_reason = self.cached
            now = self.monotonic()
            epoch = self.epoch()
            if (
                not state._number(now)
                or now < 0
                or not state._number(epoch)
                or epoch <= 0
            ):
                return state._document("unavailable", "clock_invalid")
            ages = {name: now - row[1] for name, row in channels.items()}
            age = max(ages["pose"], ages["joint"])
            identity = {
                "source_id": "fr3-manual:" + doc["instance_id"],
                "sequence": channels["joint"][0],
                "sampled_at_epoch": epoch - age,
            }
            if ages["pose"] < 0 or ages["joint"] < 0:
                return state._document(
                    "invalid", "remote_sample_in_future", identity, age
                )
            if age > state.MAX_AGE_S:
                return state._document("stale", "remote_sample_stale", identity, age)
            sample, grip = doc["sample"], doc.get("gripper")
            if "gripper" in ages:
                if ages["gripper"] < 0:
                    grip_reason = "remote_gripper_in_future"
                elif ages["gripper"] > state.MAX_AGE_S:
                    grip_reason = "remote_gripper_stale"
            measurement = {
                "joints_rad": sample["joints"],
                "gripper": {"knuckle_angle_rad": grip["theta"]}
                if grip_reason is None
                else None,
                "reported_ee": {
                    "position_m": sample["pose7"][:3],
                    "quaternion_xyzw": sample["pose7"][3:],
                    "frame_id": sample["frame"],
                    "end_frame": END_FRAME,
                },
            }
            result = state._document(
                "fresh", "sample_fresh", identity, age, measurement
            )
            result["source_detail"] = {
                "transport": "manual_v1_readonly",
                "timestamp_basis": "server_local_monotonic_receive",
                "end_frame_semantics": "Robot Server / Desk EE; not model flange",
                "gripper_state": "fresh" if grip_reason is None else "unknown",
                "gripper_reason_code": grip_reason,
                "channels": {
                    name: {
                        "sequence": row[0],
                        "sampled_at_mono": row[1],
                        "age_s": ages[name],
                    }
                    for name, row in channels.items()
                },
            }
            return result


_READER = Reader()


def snapshot():
    try:
        url = configured_url()
    except Exception:  # noqa: BLE001 - invalid configured source is unavailable, never a guessed pose
        return state._document("unavailable", "remote_configuration_invalid")
    return state.snapshot() if url is None else _READER.snapshot(url)
