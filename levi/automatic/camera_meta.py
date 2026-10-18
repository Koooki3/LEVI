"""Camera frame metadata from openpi's frame bus (brief CAMERA-FRAME-BUS.md).

openpi's ``RobotEnv.get_observation()`` may return ``obs["image_meta"]``:
per camera ``{"seq", "t_capture_ns", "t_arrival_ns", "age_ms"}`` under the
camera names of ``obs["image"]`` (``view1``, ``hand``). ``seq`` counts frames
from 0 per camera; ``t_capture_ns`` is the camera's timestamp in the host's
wall-clock domain (or None) and may only be compared with other
``t_capture_ns``; ``t_arrival_ns`` is the reader thread's
``time.monotonic_ns()``. With ``max_frame_age_ms`` set for a camera, openpi
raises ``StaleFrameError`` instead of returning an old image.

The metadata is optional: an openpi without it behaves exactly as before (no
``image_meta`` key in the bridge's observation, no check, empty CSV cells).

Nothing here sends anything to the robot. A repeated seq (``SeqWatch``) is
only recorded (note ``camera_seq_repeat``; user 2026-10-18). Only openpi's
``StaleFrameError`` with the opt-in ``max_frame_age_ms`` stops motion
(``CameraStale`` -> the episode ends, the run waits for a person, reason
``camera_stale``); no AERI job sets it.
"""

from __future__ import annotations

import math
from collections import OrderedDict

# openpi camera name -> AERI view (the same mapping as obs["image"]).
VIEW_OF = {"view1": "side", "hand": "wrist"}
VIEWS = ("side", "wrist")
# The journal reason (levi.domain.aeri TRANSITION_REASONS / STOP_REASONS):
# openpi's StaleFrameError, only with the opt-in max_frame_age_ms.
REASON = "camera_stale"
# The note of a repeated seq (SeqWatch): recorded only, never a stop.
SEQ_REPEAT = "camera_seq_repeat"
# The same seq seen on this many consecutive control steps: the camera has
# stopped (at 15 fps and ~110 ms per step every step should see a new frame).
DEFAULT_STALE_STEPS = 3
MIN_STALE_STEPS = 2
MAX_STALE_STEPS = 100
# binding.safety.max_frame_age_ms bounds (a tightening threshold, optional).
MAX_FRAME_AGE_MS = 10_000
# frames.csv / aeri_steps.csv: appended after every old column.
CSV_COLUMNS = tuple(
    f"{view}_{field}" for view in VIEWS for field in ("seq", "t_capture_ns", "age_ms")
)
# VLM requests: images per request (default: the current side + wrist pair),
# and optional earlier frames per view (default none).
DEFAULT_MAX_IMAGES = 2
MAX_IMAGES_LIMIT = 8
MAX_HISTORY_FRAMES = 4


class CameraStale(Exception):
    """The observation's camera frames are not current: no action may be
    taken on it. Raised by the bridge for openpi's ``StaleFrameError``."""

    code = REASON


def is_stale_frame_error(exc: BaseException) -> bool:
    """openpi's ``fr3_env.StaleFrameError`` (matched by class name: LEVI does
    not import openpi)."""
    return any(c.__name__ == "StaleFrameError" for c in type(exc).__mro__)


def _int(value, name, *, allow_none=False):
    if value is None and allow_none:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"image_meta {name} must be a whole number >= 0")
    return value


def view_meta(raw) -> dict | None:
    """``obs["image_meta"]`` mapped to AERI views and checked, or None when
    openpi gave none. Malformed metadata is an error (never guessed)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("image_meta must be a mapping")
    out = {}
    for name, view in VIEW_OF.items():
        item = raw.get(name)
        if not isinstance(item, dict):
            raise TypeError(f"image_meta has no {name} camera")
        age = item.get("age_ms")
        if isinstance(age, bool) or not isinstance(age, int | float):
            raise TypeError("image_meta age_ms must be a number")
        if not math.isfinite(age):
            raise ValueError("image_meta age_ms must be finite")
        out[view] = {
            "seq": _int(item.get("seq"), "seq"),
            "t_capture_ns": _int(
                item.get("t_capture_ns"), "t_capture_ns", allow_none=True
            ),
            "t_arrival_ns": _int(item.get("t_arrival_ns"), "t_arrival_ns"),
            "age_ms": float(age),
        }
    return out


def stale_steps(value) -> int:
    if value is None:
        return DEFAULT_STALE_STEPS
    if type(value) is not int or not MIN_STALE_STEPS <= value <= MAX_STALE_STEPS:
        raise ValueError(
            f"camera_stale_steps must be a whole number from {MIN_STALE_STEPS} "
            f"to {MAX_STALE_STEPS}"
        )
    return value


class SeqWatch:
    """Per view, how many consecutive observations carried the same ``seq``.
    ``update`` returns why the observation is stale (the same seq on
    ``limit`` consecutive steps), else None. Observations without metadata
    are not judged (old openpi: the old behaviour)."""

    def __init__(self, limit: int = DEFAULT_STALE_STEPS):
        self.limit = stale_steps(limit)
        self._last: dict = {}

    def update(self, meta) -> str | None:
        if not meta:
            return None
        found = None
        for view in VIEWS:
            item = meta.get(view)
            if not isinstance(item, dict):
                continue
            seq = item.get("seq")
            last, run = self._last.get(view, (None, 0))
            run = run + 1 if seq == last else 1
            self._last[view] = (seq, run)
            if run >= self.limit and found is None:
                found = (
                    f"{view} camera seq {seq} unchanged for {run} steps "
                    f"(age {item.get('age_ms', float('nan')):.0f} ms)"
                )
        return found


def csv_cells(meta) -> list:
    """The ``CSV_COLUMNS`` cells of one row ("" without metadata)."""
    cells = []
    for view in VIEWS:
        item = (meta or {}).get(view) or {}
        for field in ("seq", "t_capture_ns", "age_ms"):
            value = item.get(field)
            if field == "age_ms" and isinstance(value, float):
                value = round(value, 3)
            cells.append("" if value is None else value)
    return cells


def frame_fields(meta, view) -> dict:
    """``{"seq", "t_capture_ns"}`` of one view for request evidence (empty
    without metadata), so a VLM frame can be matched with the recorded frame
    and the policy request of the same camera frame."""
    item = (meta or {}).get(view)
    if not isinstance(item, dict):
        return {}
    return {"seq": item.get("seq"), "t_capture_ns": item.get("t_capture_ns")}


class JpegCache:
    """JPEG bytes per camera frame: ``(view, seq, t_arrival_ns)`` is encoded
    once and reused (a frame without metadata is encoded every time, as
    before). ``encode(pixels) -> bytes``. Keeps the newest ``capacity``."""

    def __init__(self, encode, capacity: int = 16):
        self.encode = encode
        self.capacity = capacity
        self.encoded = 0  # how many times encode ran (tests, diagnostics)
        self._items: OrderedDict = OrderedDict()

    def get(self, view, pixels, item=None) -> bytes:
        key = None
        if isinstance(item, dict) and type(item.get("seq")) is int:
            key = (view, item["seq"], item.get("t_arrival_ns"))
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        data = self.encode(pixels)
        self.encoded += 1
        if key is not None:
            self._items[key] = data
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
        return data


def image_settings(value) -> dict:
    """Optional ``max_images`` / ``history_frames`` of a VLM binding block."""
    value = value or {}
    images = value.get("max_images", DEFAULT_MAX_IMAGES)
    history = value.get("history_frames", 0)
    if type(images) is not int or not 1 <= images <= MAX_IMAGES_LIMIT:
        raise ValueError(
            f"max_images must be a whole number from 1 to {MAX_IMAGES_LIMIT}"
        )
    if type(history) is not int or not 0 <= history <= MAX_HISTORY_FRAMES:
        raise ValueError(
            f"history_frames must be a whole number from 0 to {MAX_HISTORY_FRAMES}"
        )
    return {"max_images": images, "history_frames": history}
