"""Read only <LEVI_WORKSPACE>/robot-studio/telemetry.json for display.

This module does not import a robot client, probe ports, spawn a process, write
files or affect admission. A producer is deliberately outside this adapter.
Freshness uses the producer's sample timestamp, never the file's mtime. Valid
old samples retain only their identity and age; no stale pose is applied.
"""

import errno
import json
import math
import os
import stat
import time
from pathlib import Path

TELEMETRY_SCHEMA = "levi.robot_studio.telemetry.v1"
STATE_SCHEMA = "levi.robot_studio.state.v1"
MAX_BYTES = 16 * 1024
MAX_AGE_S = 1.0
FUTURE_SKEW_S = 0.05
QUATERNION_NORM_TOLERANCE = 0.001
MAX_SAFE_INTEGER = (1 << 53) - 1


class Invalid(ValueError):
    """A stable public reason code, without file contents or local paths."""


def _directory(parent_fd, name):
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise Invalid("unsafe_path")
    fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise Invalid("unsafe_path")
    return fd


def _read(root):
    # Walk every parent through pinned directory descriptors. Checking only
    # telemetry.json with O_NOFOLLOW would still allow robot-studio -> outside.
    # Do not resolve links before this walk: every component must be a directory.
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise Invalid("unsafe_path")
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in (*root.parts[1:], "robot-studio"):
            next_fd = _directory(parent, part)
            os.close(parent)
            parent = next_fd
        before = os.stat("telemetry.json", dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise Invalid("not_regular")
        fd = os.open(
            "telemetry.json",
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (
                before.st_dev,
                before.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise Invalid("changed_during_read")
            if opened.st_size > MAX_BYTES:
                raise Invalid("too_large")
            chunks, remaining = [], MAX_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_BYTES:
                raise Invalid("too_large")
            after = os.fstat(fd)
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise Invalid("changed_during_read")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _vector(value, length):
    return (
        isinstance(value, list)
        and len(value) == length
        and all(_number(number) for number in value)
    )


def _text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _keys(value, expected):
    return isinstance(value, dict) and value.keys() == expected


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise Invalid("invalid_json")
        out[key] = value
    return out


def _reject_constant(value):
    raise Invalid("invalid_json")


def _parse(raw):
    try:
        data = json.loads(
            raw, object_pairs_hook=_object, parse_constant=_reject_constant
        )
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise Invalid("invalid_json") from exc
    if (
        not _keys(
            data,
            {
                "schema",
                "robot",
                "source_id",
                "sequence",
                "sampled_at_epoch",
                "joints_rad",
                "gripper",
                "reported_ee",
            },
        )
        or data["schema"] != TELEMETRY_SCHEMA
        or data["robot"] != "fr3"
    ):
        raise Invalid("schema_invalid")
    if (
        not _text(data["source_id"], 80)
        or type(data["sequence"]) is not int
        or not 0 <= data["sequence"] <= MAX_SAFE_INTEGER
    ):
        raise Invalid("schema_invalid")
    if not _number(data["sampled_at_epoch"]) or data["sampled_at_epoch"] <= 0:
        raise Invalid("invalid_timestamp")
    if not _vector(data["joints_rad"], 7):
        raise Invalid("invalid_joints")
    gripper = data["gripper"]
    if gripper is not None:
        if _keys(gripper, {"knuckle_angle_rad"}):
            valid = _number(gripper["knuckle_angle_rad"])
        elif _keys(gripper, {"opening_width_m"}):
            width = gripper["opening_width_m"]
            # Source-defined measured opening, not a predicted command or an
            # invented conversion to knuckle angle. No model-specific clamp.
            valid = _number(width) and width >= 0
        else:
            valid = False
        if not valid:
            raise Invalid("invalid_gripper")
    ee = data["reported_ee"]
    if ee is not None:
        if not _keys(
            ee, {"position_m", "quaternion_xyzw", "frame_id", "end_frame"}
        ) or not (
            _vector(ee["position_m"], 3)
            and _vector(ee["quaternion_xyzw"], 4)
            and _text(ee["frame_id"], 128)
            and _text(ee["end_frame"], 128)
        ):
            raise Invalid("invalid_reported_ee")
        if abs(math.hypot(*ee["quaternion_xyzw"]) - 1.0) > QUATERNION_NORM_TOLERANCE:
            raise Invalid("invalid_reported_ee")
    return data


def _document(state, reason, data=None, age=None, measurement=None):
    return {
        "schema": STATE_SCHEMA,
        "state": state,
        "reason_code": reason,
        "age_s": age,
        "sampled_at_epoch": data["sampled_at_epoch"] if data else None,
        "sequence": data["sequence"] if data else None,
        "source_id": data["source_id"] if data else None,
        "measurement": measurement,
        "capabilities": {"read_only": True, "motion": False},
    }


def snapshot(*, now=None):
    """Read the single fixed file. No request-controlled path or URL exists."""
    from levi.paths import ROOT

    supplied_now = now
    now = time.time() if supplied_now is None else supplied_now
    if not _number(now) or now <= 0:
        return _document("unavailable", "clock_invalid")
    try:
        data = _parse(_read(ROOT))
    except FileNotFoundError:
        return _document("missing", "file_missing")
    except Invalid as exc:
        return _document("invalid", str(exc))
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return _document("invalid", "unsafe_path")
        return _document("unavailable", "read_failed")
    # Include bounded-file read/parse latency rather than dating the result at request start.
    now = time.time() if supplied_now is None else supplied_now
    if not _number(now) or now <= 0:
        return _document("unavailable", "clock_invalid")
    age = now - data["sampled_at_epoch"]
    if age < -FUTURE_SKEW_S:
        return _document("invalid", "sample_in_future", data, age)
    if age > MAX_AGE_S:
        return _document("stale", "sample_stale", data, age)
    measurement = {key: data[key] for key in ("joints_rad", "gripper", "reported_ee")}
    return _document("fresh", "sample_fresh", data, max(0.0, age), measurement)
