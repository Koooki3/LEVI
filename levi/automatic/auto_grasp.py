"""The automatic grasp source of GRASP_ASSIST (``subtask.grasp_assist.grasp_source: auto``).

Design: AUTO-GRASP-DESIGN-20261118 (user decisions of 2026-11-18: Qwen3-VL finds the
object, no depth, yaw included, ``step_m`` 0.02 and ``max_steps`` 80 allowed). The
takeover (``grasp_assist.AssistSession``) is unchanged: it takes one key at a time from
``gate.assist_key(view)`` and checks every limit (step, floor, workspace, travel, step
budget, follow error, orientation, yaw) before anything is sent. ``AutoGraspGate`` is a
gate whose keys are decided from the wrist camera instead of typed, so every limit, the
holding facts, the hand-back, the abort (WAIT_HUMAN, the arm held) and the stop are the
operator path's own. Nothing here opens a robot connection or sends a command: the
only I/O are an observation (``port.observe()``), the locator's HTTP request to the VLM
and the JSONL decision log.

What it does, one key per call (a generator below: ``AutoGraspGate._plan``):

1. find the target (the object words of the subtask, ``view["object"]``): the locator
   says where it is in the wrist image; not visible -> ``z+`` (up to twice), then abort;
   a locator that cannot be reached is not "not visible": abort at once;
2. calibrate the image axes with one ``x+`` and one ``y+`` step: the target's pixel shift
   per metre of measured tool motion is a 2x2 matrix; a shift too small or two columns
   that are parallel is a degenerate mapping: abort;
3. translate: one key at a time, the axis with the largest metric error, until the
   target is within ``center_tol_px`` of the aim point (the image centre) or within half
   a step;
4. yaw: the camera turns with the gripper, so the target's long axis in the image is its
   direction relative to the gripper; the fingers close along the image horizontal, so the
   long axis is turned vertical, within ``yaw_tol_deg`` (skipped when the locator gives no
   axis: not elongated). The sign comes from the calibration (the determinant of the
   matrix says whether the image turns with or against the robot's yaw) and the first turn
   is checked against it (the error must not grow);
5. re-centre, then descend ``z-`` to the registered grasp height of the object
   (``grasp_height``; missing: abort before anything moves);
6. ``close``, then ``lift`` when the closed gripper reads ``holding_candidate``; the lift
   fact ``holding_verified`` -> ``handback``; anything else -> ``open``, ``z+`` back up
   to where the attempt began, and again (``auto_max_retries``), then abort.

Any refusal, any key that did not arrive, a lost target, a frame that is too old and
every unexpected error end in the abort path (the arm stays where it is). A stop is
looked for before each decision and again after the (slow) locator call, like the
operator gate's. Every decision, with the frame it used (camera seq, age, hash) and the
locator's answer, is one line of the JSONL decision log.
"""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import grasp_assist as ga
from . import subtask_vlm

# --- tuning (uncalibrated: offline replay on real wrist frames sets them) ----------------

CENTER_TOL_PX = 25.0
YAW_TOL_DEG = 10.0
# A frame older than this when it is read is not used (the wrist camera is on USB 2.1
# and drops frames; with a frame bus every observation says how old its frame is).
MAX_FRAME_AGE_MS = 400.0
# The most a frame newer than the last move is waited for before the key is given up.
FRAME_WAIT_S = 1.0
FRAME_POLL_S = 0.1
# Without ``image_meta`` the frame's age is unknown: wait this long after a move first.
DESCENT_TOL_M = 0.005
FIND_UP_MAX = 2
MAX_CENTER_KEYS = 8
MAX_YAW_KEYS = 14
# A calibration step must move the target at least this many pixels, and the two
# columns must not be (nearly) parallel.
MIN_CALIBRATION_SHIFT_PX = 4.0
MIN_CALIBRATION_ORTHOGONALITY = 0.2
# The first yaw key must not make the angle error larger by more than this (noise).
YAW_DIRECTION_SLACK_DEG = 3.0
MIN_CONFIDENCE = 0.3
LOCATE_TIMEOUT_S = 20.0
MIN_AXIS_PX = 40.0


class Abort(Exception):
    """The plan cannot go on: the abort key is sent (the arm holds, a person)."""

    def __init__(self, reason: str, **detail):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


# --- perception ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    """What a locator says about one frame. ``found`` False with ``error`` set is a
    locator that did not answer (unreachable, a bad reply): not the same as an object
    that is not in the image. ``center`` is the object's centre in pixels (x right, y
    down); ``axis_deg`` the angle of its long axis in the same pixel coordinates, folded
    to (-90, 90], or None when no axis was given (not elongated)."""

    found: bool
    center: tuple[float, float] | None = None
    axis_deg: float | None = None
    confidence: float | None = None
    error: str | None = None
    raw: Any = None

    def describe(self) -> dict:
        return {
            "found": self.found,
            "center": None
            if self.center is None
            else [round(v, 1) for v in self.center],
            "axis_deg": None if self.axis_deg is None else round(self.axis_deg, 1),
            "confidence": self.confidence,
            "error": self.error,
        }


class Locator(Protocol):
    def locate(self, image_bgr, object_name: str) -> Detection: ...


def fold_axis_deg(dx: float, dy: float) -> float:
    """The angle of a line through the vector ``(dx, dy)`` in (-90, 90] (a line has
    no direction)."""
    angle = math.degrees(math.atan2(dy, dx))
    while angle > 90:
        angle -= 180
    while angle <= -90:
        angle += 180
    return angle


LOCATE_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "center": {
            "anyOf": [
                {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 2,
                },
                {"type": "null"},
            ]
        },
        "endpoints": {
            "anyOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "minItems": 2,
                    "maxItems": 2,
                },
                {"type": "null"},
            ]
        },
        "confidence": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    },
    "required": ["found", "center", "endpoints"],
    "additionalProperties": False,
}

LOCATE_PROMPT = (
    "This is the image of a camera on a robot gripper, {w} pixels wide and {h} pixels "
    "high (x to the right, y down, origin at the top left). Find the {name}. Answer "
    "with one JSON object and nothing else: "
    '{{"found": true or false, "center": [x, y] the centre of the {name} in pixels, '
    '"endpoints": [[x1, y1], [x2, y2]] the two ends of the {name}\'s long axis in '
    "pixels, or null when it is about as wide as it is long, "
    '"confidence": a number from 0 to 1}}. If the {name} is not visible answer '
    '{{"found": false, "center": null, "endpoints": null}}.'
)
# The same question on the 0..1000 grid Qwen3-VL answers in: offline replay of the
# 11-18 wrist frames (Qwen3-VL-4B-FP8) answered on that grid in 11 of 11 frames even
# when asked for pixels, so the prompt says the grid when ``coords`` is ``norm1000``.
LOCATE_PROMPT_NORM1000 = (
    "This is the image of a camera on a robot gripper. Coordinates are on a 0 to 1000 "
    "grid over the image (x to the right, y down, origin at the top left, 1000 at the "
    "right and bottom edges). Find the {name}. Answer with one JSON object and nothing "
    'else: {{"found": true or false, "center": [x, y] the centre of the {name}, '
    '"endpoints": [[x1, y1], [x2, y2]] the two ends of the {name}\'s long axis, or '
    "null when it is about as wide as it is long, "
    '"confidence": a number from 0 to 1}}. If the {name} is not visible answer '
    '{{"found": false, "center": null, "endpoints": null}}.'
)


def _pair(value, w: float, h: float, scale: tuple[float, float]):
    """``(x, y)`` in pixels from a two-number list inside the image, else None."""
    if not isinstance(value, list) or len(value) != 2:
        return None
    if any(isinstance(v, bool) or not isinstance(v, int | float) for v in value):
        return None
    x, y = float(value[0]) * scale[0], float(value[1]) * scale[1]
    if not (math.isfinite(x) and math.isfinite(y)) or not (0 <= x <= w and 0 <= y <= h):
        return None
    return (x, y)


def parse_locate_reply(
    text,
    width: int,
    height: int,
    *,
    coords: str = "pixel",
    min_axis_px: float = MIN_AXIS_PX,
) -> Detection:
    """The locator's JSON reply as a ``Detection``; anything else (not JSON, extra or
    missing keys, a wrong type, a point outside the image) is ``found=False`` with the
    reason in ``error``: a reply is trusted whole or not at all. ``coords`` is
    ``"pixel"`` or ``"norm1000"`` (a 0..1000 grid over the image)."""
    scale = (1.0, 1.0) if coords == "pixel" else (width / 1000.0, height / 1000.0)

    def bad(why):
        return Detection(False, error=f"bad_reply: {why}", raw=str(text)[:300])

    if not isinstance(text, str):
        return bad("not text")
    try:
        data = json.loads(text.strip())
    except ValueError:
        return bad("not JSON")
    if not isinstance(data, dict):
        return bad("not an object")
    keys = set(data)
    if not {"found", "center", "endpoints"} <= keys or keys - {
        "found",
        "center",
        "endpoints",
        "confidence",
    }:
        return bad("keys")
    if type(data["found"]) is not bool:
        return bad("found is not a boolean")
    confidence = data.get("confidence")
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        return bad("confidence")
    if not data["found"]:
        if data["center"] is not None or data["endpoints"] is not None:
            return bad("found false with a position")
        return Detection(False, confidence=confidence, raw=text[:300])
    center = _pair(data["center"], width, height, scale)
    if center is None:
        return bad("center")
    axis = None
    ends = data["endpoints"]
    if ends is not None:
        if not isinstance(ends, list) or len(ends) != 2:
            return bad("endpoints")
        a = _pair(ends[0], width, height, scale)
        b = _pair(ends[1], width, height, scale)
        if a is None or b is None:
            return bad("endpoints")
        length = math.dist(a, b)
        if length >= min_axis_px:
            axis = fold_axis_deg(b[0] - a[0], b[1] - a[1])
    return Detection(
        True,
        center=center,
        axis_deg=axis,
        confidence=None if confidence is None else float(confidence),
        raw=text[:300],
    )


class QwenLocator:
    """``locate`` over an OpenAI-compatible ``/v1/chat/completions`` (the subtask VLM,
    Qwen3-VL, or the harness proxy): the image and a question, strict JSON back (a
    ``json_schema`` response format, ``temperature`` 0). The endpoint is an explicit
    loopback URL that is not a robot port (``subtask_vlm.loopback_url``). Any transport
    or parse failure is ``Detection(found=False, error=...)``: it never raises and
    never guesses. ``encode(pixels)`` makes JPEG bytes; ``post(url, body, timeout_s)``
    is the transport (a fake in tests)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        encode,
        timeout_s: float = LOCATE_TIMEOUT_S,
        extra_body: dict | None = None,
        post=None,
        coords: str = "pixel",
        min_axis_px: float = MIN_AXIS_PX,
        max_tokens: int = 200,
    ):
        if coords not in ("pixel", "norm1000"):
            raise ValueError("coords is 'pixel' or 'norm1000'")
        self.base_url = subtask_vlm.loopback_url(base_url)
        self.model, self.encode = model, encode
        self.timeout_s = min(float(timeout_s), LOCATE_TIMEOUT_S)
        self.extra_body = dict(extra_body or {})
        self.post = post or subtask_vlm._http_post
        self.coords, self.min_axis_px, self.max_tokens = coords, min_axis_px, max_tokens

    def request(self, image_bgr, object_name: str) -> dict:
        height, width = int(image_bgr.shape[0]), int(image_bgr.shape[1])
        jpeg = self.encode(image_bgr)
        prompt = LOCATE_PROMPT if self.coords == "pixel" else LOCATE_PROMPT_NORM1000
        text = prompt.format(w=width, h=height, name=object_name)
        # The site's extra_body first: it may add (chat_template_kwargs, ...) but never
        # replaces the model, the messages, the strict schema or the sampling below.
        return {
            **self.extra_body,
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64,"
                                + base64.b64encode(jpeg).decode()
                            },
                        },
                        {"type": "text", "text": text},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "locate",
                    "strict": True,
                    "schema": LOCATE_SCHEMA,
                },
            },
        }

    def locate(self, image_bgr, object_name: str) -> Detection:
        try:
            body = self.request(image_bgr, object_name)
            found = self.post(
                self.base_url + "/v1/chat/completions", body, self.timeout_s
            )
        except Exception as exc:  # noqa: BLE001 - unreachable, timeout, bad image
            return Detection(False, error=f"request_failed: {type(exc).__name__}"[:120])
        try:
            choice = found["choices"][0]
            if choice.get("finish_reason") == "length":
                return Detection(False, error="bad_reply: truncated")
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError, AttributeError):
            return Detection(False, error="bad_reply: no choices[0].message.content")
        height, width = int(image_bgr.shape[0]), int(image_bgr.shape[1])
        return parse_locate_reply(
            content,
            width,
            height,
            coords=self.coords,
            min_axis_px=self.min_axis_px,
        )


# --- the gate ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoGraspConfig:
    center_tol_px: float = CENTER_TOL_PX
    yaw_tol_deg: float = YAW_TOL_DEG
    max_frame_age_ms: float = MAX_FRAME_AGE_MS
    frame_wait_s: float = FRAME_WAIT_S
    find_up_max: int = FIND_UP_MAX
    max_center_keys: int = MAX_CENTER_KEYS
    max_yaw_keys: int = MAX_YAW_KEYS
    min_calibration_shift_px: float = MIN_CALIBRATION_SHIFT_PX
    min_confidence: float = MIN_CONFIDENCE
    # Where in the wrist image the object should be before the descent (pixels);
    # None: the image centre. The finger gap may not be at the centre.
    aim_px: tuple | None = None

    def __post_init__(self):
        for name in (
            "center_tol_px",
            "yaw_tol_deg",
            "max_frame_age_ms",
            "frame_wait_s",
            "min_calibration_shift_px",
        ):
            value = getattr(self, name)
            if not ga._number(value) or value <= 0:
                raise ValueError(f"auto grasp: {name} is a positive number")
        if not ga._number(self.min_confidence) or not 0 <= self.min_confidence <= 1:
            raise ValueError("auto grasp: min_confidence is from 0 to 1")
        for name in ("find_up_max", "max_center_keys", "max_yaw_keys"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"auto grasp: {name} is a whole number >= 0")


@dataclass
class Look:
    """One locate call: where the tool was, the frame it used and the answer."""

    detection: Detection
    xyz: tuple
    frame: dict = field(default_factory=dict)


class AutoGraspGate:
    """A gate (the orchestrator's ``gate``) whose takeover keys come from perception.
    Everything else the orchestrator asks of its gate (``confirm_inventory``,
    ``release_round``, the verdict prompts, ...) is the wrapped terminal's:
    attributes not defined here are looked up on ``base``.

    ``port`` is the takeover's ``AssistPort`` (its ``observe()`` gives the wrist frame
    and the pose, its ``clock`` the time, its ``stopped()`` the stop). ``assist``
    is the job's ``GraspAssistConfig`` (``step_m``, ``yaw_step_rad``, the grasp heights,
    ``auto_max_retries``). ``log_path`` is the JSONL decision log (a file in the run
    directory); ``encode`` (JPEG bytes) additionally keeps the frame each decision used
    next to it. ``records`` holds the log in memory too."""

    def __init__(
        self,
        base,
        port,
        locator: Locator,
        assist: ga.GraspAssistConfig,
        *,
        config: AutoGraspConfig | None = None,
        limits: ga.Limits | None = None,
        log_path: Path | str | None = None,
        encode=None,
    ):
        self._base = base
        self.port, self.locator, self.assist = port, locator, assist
        self.config = config or AutoGraspConfig(aim_px=assist.grasp_pixel)
        self.limits = limits
        self.log_path = None if log_path is None else Path(log_path)
        self.frames_dir = None
        if self.log_path is not None and encode is not None:
            self.frames_dir = self.log_path.with_suffix("")
        self.encode = encode
        self.heights = assist.grasp_heights()
        self.step_m = assist.step_m
        self.records: list[dict] = []
        self._gen = None
        self._outcome: dict | None = None
        self._outcome_for_plan: dict | None = None
        self._view: dict = {}
        self._moved_ns: int | None = None
        self._moved_since_look = False
        self._last_seq: int | None = None
        self._z_grasp: float | None = None
        self._why = ""
        self._detail: dict = {}
        self._frame_count = 0
        self._xyz: tuple = (0.0, 0.0, 0.0)

    def __getattr__(self, name):
        # Only reached for what this class does not define: the terminal's methods.
        base = self.__dict__.get("_base")
        if base is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(base, name)

    # ------------------------------------------------------------------ the gate contract

    def assist_report(self, kind: str, data: dict) -> None:
        """What the takeover reports: remembered (the outcome of the key just sent,
        a refusal) and passed on to the terminal's display."""
        if kind == "enter":
            self._gen, self._outcome, self._moved_ns = None, None, None
            self._view = dict(data)
            self._log("enter", object=data.get("object"), xyz=data.get("xyz"))
        elif kind == "result":
            self._outcome = {"kind": "result", **data}
            self._view = dict(data)
            self._moved_ns = self.port.clock.now()
            self._moved_since_look = True
            self._log(
                "outcome", key=data.get("key"), ok=data.get("ok"), code=data.get("code")
            )
        elif kind == "refused":
            self._outcome = {"kind": "refused", **data}
            self._log("outcome", key=data.get("key"), refused=data.get("code"))
        report = getattr(self._base, "assist_report", None)
        if callable(report):
            report(kind, data)

    def assist_key(self, view: dict) -> str:
        """The next key: a ``grasp_assist.KEYS`` member or ``"stop"``."""
        try:
            return self._next(view)
        except ga.AssistFault:
            raise  # a comms anomaly locks the run: the orchestrator's path
        except Exception as exc:  # noqa: BLE001 - a bug here must not move the arm
            self._log("error", error=f"{type(exc).__name__}: {exc}"[:300])
            return "abort"

    # ----------------------------------------------------------------------- stepping

    def _interrupted(self) -> str | None:
        """``"stop"`` when a stop is registered; ``"abort"`` / ``"stop"`` when a person
        typed one at the terminal (``Console.assist_interrupt``)."""
        if self.port.stopped():
            return "stop"
        interrupt = getattr(self._base, "assist_interrupt", None)
        return interrupt() if callable(interrupt) else None

    def _next(self, view: dict) -> str:
        found = self._interrupted()
        if found is not None:
            self._log("interrupted", key=found)
            return found
        self._view = {**self._view, **view}
        if self._gen is None:
            self._gen = self._run()
        self._outcome_for_plan = self._outcome
        try:
            key = next(self._gen)
        except StopIteration:
            self._decision("plan_ended")
            key = "abort"
        self._outcome = None
        # The locator takes seconds: a stop registered meanwhile, and an ``a`` or ``q``
        # typed at the terminal meanwhile, win over the key (the same checks as before
        # the decision, ``_interrupted``, not only the robot's stop flag).
        if key not in ("abort", "stop"):
            found = self._interrupted()
            if found is not None:
                self._log("interrupted", key=found, dropped=key)
                return found
        self._log("decision", key=key, why=self._why, detail=self._detail)
        return key

    def _decision(self, why: str, **detail) -> None:
        self._why, self._detail = why, detail

    # ------------------------------------------------------------------------ the plan

    def _run(self):
        try:
            yield from self._plan()
        except Abort as stop:
            self._decision(stop.reason, **stop.detail)
            yield "abort"

    def _act(self, key: str, why: str, **detail):
        """Send ``key`` and return its result; a refusal or a key that did not arrive
        is an abort (the takeover's own limits are not argued with)."""
        if self._view.get("steps", 0) >= self._view.get("max_steps", 10**9):
            raise Abort("max_steps_reached", steps=self._view.get("steps"))
        self._decision(why, **detail)
        yield key
        out = self._outcome_for_plan
        if out is None:
            raise Abort("no_outcome", which=key)
        if out["kind"] == "refused":
            raise Abort(f"refused_{out.get('code')}", which=key, info=out.get("detail"))
        if out.get("ok") is not True:
            raise Abort(
                "key_did_not_arrive",
                which=key,
                code=out.get("code"),
                info=out.get("detail"),
            )
        return out

    def _plan(self):
        obj = self._view.get("object") or ""
        if not obj:
            raise Abort("no_object_name")
        z_grasp = self.heights.get(obj)
        if z_grasp is None:
            raise Abort("grasp_height_missing", object=obj)
        if self.limits is not None and z_grasp < self.limits.min_z:
            raise Abort("grasp_height_below_floor", z=z_grasp, floor=self.limits.min_z)
        self._z_grasp = z_grasp
        retries = 0
        matrix = None
        while True:
            start_z = self._pose_xyz()[2]
            look = yield from self._find(obj)
            if matrix is None:
                matrix, look = yield from self._calibrate(obj, look)
            look = yield from self._center(obj, matrix, look)
            turned, look = yield from self._align_yaw(obj, matrix, look)
            if turned:
                # The camera turned with the gripper: the pixel/metre matrix turned
                # with it. Measure it again (one x+ and one y+) before centring.
                matrix, look = yield from self._calibrate(obj, look)
                look = yield from self._center(obj, matrix, look)
            yield from self._descend(z_grasp)
            closed = yield from self._act("close", "grasp_height_reached", z=z_grasp)
            if closed.get("fact") == "holding_candidate":
                try:
                    lifted = yield from self._act("lift", "closed_gripper_holds")
                except Abort as refused:
                    # The fresh reading before the lift no longer says "holding":
                    # the grasp slipped, a failed grasp like any other.
                    if refused.reason != "refused_lift_needs_holding_candidate":
                        raise
                    lifted = {}
                if lifted.get("fact") == "holding_verified":
                    self._decision("holding_verified")
                    yield "handback"
                    # Resumed: the hand-back was refused (the takeover says why). The
                    # object is lifted: opening the gripper would drop it. Hold, and a
                    # person decides.
                    refused = self._outcome_for_plan or {}
                    raise Abort("handback_refused", code=refused.get("code"))
            retries += 1
            if retries > self.assist.auto_max_retries:
                raise Abort("grasp_failed", retries=retries - 1)
            yield from self._act("open", "grasp_did_not_hold", retry=retries)
            yield from self._rise(start_z)

    def _find(self, obj: str):
        raised = 0
        while True:
            look = self._look(obj, "find")
            if look.detection.found:
                return look
            if look.detection.error is not None:
                raise Abort("locator_unavailable", error=look.detection.error)
            if raised >= self.config.find_up_max:
                raise Abort("target_not_found", raised=raised)
            raised += 1
            yield from self._act(
                "z+", "target_not_found_look_from_higher", raised=raised
            )

    def _must_find(self, obj: str, phase: str) -> Look:
        look = self._look(obj, phase)
        if not look.detection.found:
            raise Abort(f"target_lost_{phase}", error=look.detection.error)
        return look

    def _calibrate(self, obj: str, look0: Look):
        """One ``x+`` and one ``y+`` step: the target's pixel shift per metre of tool
        motion (the measured motion, not the commanded one). Returns ``(matrix,
        look)``: ``matrix[row][col]`` is the pixel shift (row 0 u, row 1 v) per metre
        along robot x (col 0) and y (col 1)."""
        columns = []
        look = look0
        for axis, key in ((0, "x+"), (1, "y+")):
            yield from self._act(key, "calibrate_image_axes")
            after = self._must_find(obj, "calibration")
            moved = after.xyz[axis] - look.xyz[axis]
            if abs(moved) < 0.5 * self.step_m:
                raise Abort(
                    "calibration_degenerate", info="tool_did_not_move", which=key
                )
            (u0, v0), (u1, v1) = look.detection.center, after.detection.center
            columns.append(((u1 - u0) / moved, (v1 - v0) / moved))
            look = after
        (a, c), (b, d) = columns  # a, c: shift per metre of x; b, d: per metre of y
        matrix = ((a, b), (c, d))
        norm_x, norm_y = math.hypot(a, c), math.hypot(b, d)
        det = a * d - b * c
        if (
            norm_x * self.step_m < self.config.min_calibration_shift_px
            or norm_y * self.step_m < self.config.min_calibration_shift_px
            or abs(det) < MIN_CALIBRATION_ORTHOGONALITY * norm_x * norm_y
        ):
            raise Abort(
                "calibration_degenerate",
                matrix=[[round(v, 1) for v in row] for row in matrix],
            )
        self._log("calibration", matrix=[[round(v, 1) for v in row] for row in matrix])
        return matrix, look

    def _aim(self, look: Look) -> tuple[float, float]:
        """The pixel the target is brought to: the midpoint between the fingertips in
        the wrist image (``grasp_pixel``, measured offline with the tool at the
        registered grasp height; default the image centre, right only when the camera
        axis passes between the fingertips). A camera that is not on the tool axis sees
        that point move with the height (parallax): its offset from the image centre
        scales with 1 / z, taking the tool z as the camera's height above the table
        plane, so the pixel measured at the grasp height is scaled to the height of
        this look."""
        h, w = look.frame["shape"]
        centre = (w / 2.0, h / 2.0)
        if self.config.aim_px is None:
            return centre
        scale = self._z_grasp / look.xyz[2] if self._z_grasp else 1.0
        return (
            centre[0] + (float(self.config.aim_px[0]) - centre[0]) * scale,
            centre[1] + (float(self.config.aim_px[1]) - centre[1]) * scale,
        )

    def _center(self, obj: str, matrix, look: Look):
        """Translate until the target is at the aim point: one key per look."""
        (a, b), (c, d) = matrix
        det = a * d - b * c
        for n in range(self.config.max_center_keys + 1):
            aim = self._aim(look)
            eu = aim[0] - look.detection.center[0]
            ev = aim[1] - look.detection.center[1]
            # M @ move = (eu, ev)  ->  move = M^-1 @ error  (metres along x and y)
            move = ((d * eu - b * ev) / det, (-c * eu + a * ev) / det)
            if (
                math.hypot(eu, ev) <= self.config.center_tol_px
                or max(abs(move[0]), abs(move[1])) < 0.5 * self.step_m
            ):
                self._log("centered", error_px=[round(eu, 1), round(ev, 1)])
                return look
            axis = 0 if abs(move[0]) >= abs(move[1]) else 1
            key = "xy"[axis] + ("+" if move[axis] > 0 else "-")
            if n == self.config.max_center_keys:
                raise Abort("centering_did_not_converge", error_px=[eu, ev])
            yield from self._act(
                key, "center_on_target", error_px=[round(eu, 1), round(ev, 1)]
            )
            look = self._must_find(obj, "centering")
        raise Abort("centering_did_not_converge")  # pragma: no cover - the loop returns

    def _align_yaw(self, obj: str, matrix, look: Look):
        """Turn the gripper until the target's long axis is vertical in the image.
        Returns ``(turned, look)``."""
        if not self.assist.yaw_enabled:
            # The job did not turn the yaw keys on: grasp as the gripper stands.
            self._log("yaw_skipped", why="yaw_not_enabled")
            return False, look
        if look.detection.axis_deg is None:
            self._log("yaw_skipped", why="no_axis_not_elongated")
            return False, look
        (a, b), (c, d) = matrix
        sign = 1.0 if a * d - b * c > 0 else -1.0

        def error(det: Detection) -> float:
            # Degrees still to turn the image so that the axis is vertical, (-90, 90].
            return 90.0 - (det.axis_deg % 180.0)

        delta = error(look.detection)
        if abs(delta) <= self.config.yaw_tol_deg:
            self._log("yaw_ok", error_deg=round(delta, 1))
            return False, look
        for n in range(self.config.max_yaw_keys):
            # The scene turns in the image against the tool's yaw (about the camera
            # axis, along the tool's): the image turns by -yaw when the calibration
            # matrix keeps the handedness (det > 0), by +yaw when it flips it.
            turn = -delta * sign
            key = "r+" if turn > 0 else "r-"
            before = abs(delta)
            yield from self._act(
                key, "align_long_axis_vertical", error_deg=round(delta, 1)
            )
            look = self._must_find(obj, "yaw")
            if look.detection.axis_deg is None:
                raise Abort("axis_lost_during_yaw")
            delta = error(look.detection)
            if n == 0 and before < 80 and abs(delta) > before + YAW_DIRECTION_SLACK_DEG:
                raise Abort(
                    "yaw_direction_unexpected",
                    before=round(before, 1),
                    after=round(abs(delta), 1),
                )
            if abs(delta) <= self.config.yaw_tol_deg:
                self._log("yaw_ok", error_deg=round(delta, 1), keys=n + 1)
                return True, look
        raise Abort("yaw_did_not_converge", error_deg=round(delta, 1))

    def _descend(self, z_grasp: float):
        # Never more than ``DESCENT_TOL_M`` below the registered height (the keys are
        # whole steps, so the tool may stop up to a step above it).
        while self._pose_xyz()[2] - self.step_m >= z_grasp - DESCENT_TOL_M:
            yield from self._act("z-", "descend_to_grasp_height", z_grasp=z_grasp)

    def _rise(self, z_start: float):
        while self._pose_xyz()[2] + self.step_m / 2 < z_start:
            yield from self._act("z+", "back_up_for_retry", z_start=round(z_start, 4))

    # ------------------------------------------------------------------------ perception

    def _pose_xyz(self) -> tuple:
        obs = self.port.observe()
        self._xyz = tuple(float(v) for v in obs["pose"][:3])
        return self._xyz

    def _fresh_frame(self, phase: str) -> tuple[dict, Any, dict]:
        """An observation whose wrist frame is newer than the last move and not older
        than ``max_frame_age_ms``; waits (bounded) for one, else the key is given up."""
        waited = 0.0
        while True:
            obs = self.port.observe()
            meta = (obs.get("image_meta") or {}).get("wrist")
            now = self.port.clock.now()
            moved = self._moved_ns
            stale = None
            if meta is None:
                # No frame timestamps (an openpi without the frame bus): a frozen
                # camera cannot be told from a still scene, so no blind waiting.
                self._log("no_frame_bus", phase=phase)
                raise Abort("no_frame_bus")
            age = meta["age_ms"]
            captured = obs.get("observed_ns", now) - int(age * 1e6)
            if age > self.config.max_frame_age_ms:
                stale = f"age_{age:.0f}_ms"
            elif moved is not None and captured < moved:
                stale = "older_than_the_last_move"
            elif self._moved_since_look and meta["seq"] <= (
                -1 if self._last_seq is None else self._last_seq
            ):
                stale = "seq_not_advanced"
            if stale is None:
                frames = obs.get("frames") or {}
                frame = {
                    "view": "wrist",
                    "seq": meta.get("seq"),
                    "age_ms": meta.get("age_ms"),
                    "sha256": (frames.get("wrist") or "")[:16] or None,
                }
                self._last_seq, self._moved_since_look = meta["seq"], False
                return obs, obs["images_bgr"]["wrist"], frame
            if waited >= self.config.frame_wait_s:
                self._log("frame_stale", phase=phase, why=stale)
                raise Abort("stale_frame", stale=stale)
            self.port.clock.advance(int(FRAME_POLL_S * 1e9))
            waited += FRAME_POLL_S

    def _look(self, obj: str, phase: str) -> Look:
        obs, image, frame = self._fresh_frame(phase)
        xyz = tuple(float(v) for v in obs["pose"][:3])
        self._xyz = xyz
        frame["shape"] = (int(image.shape[0]), int(image.shape[1]))
        try:
            det = self.locator.locate(image, obj)
            if not isinstance(det, Detection):
                det = Detection(False, error="locator_returned_no_detection")
        except Exception as exc:  # noqa: BLE001 - a locator never ends the takeover
            det = Detection(False, error=f"locator_raised: {type(exc).__name__}"[:120])
        if det.found and (
            det.center is None
            or not all(math.isfinite(v) for v in det.center)
            or (
                det.confidence is not None
                and det.confidence < self.config.min_confidence
            )
        ):
            det = Detection(
                False,
                confidence=det.confidence,
                error=None if det.center is not None else "no_center",
                raw=det.raw,
            )
        self._frame_count += 1
        name = self._keep_frame(image)
        self._log(
            "locate",
            phase=phase,
            object=obj,
            frame={**{k: v for k, v in frame.items() if k != "shape"}, "file": name},
            xyz=[round(v, 4) for v in xyz],
            detection=det.describe(),
        )
        return Look(det, xyz, frame)

    # ------------------------------------------------------------------------ the log

    def _keep_frame(self, image) -> str | None:
        if self.frames_dir is None or self.encode is None:
            return None
        name = f"{self._frame_count:04d}.jpg"
        try:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            (self.frames_dir / name).write_bytes(self.encode(image))
        except Exception:  # noqa: BLE001 - a frame that cannot be kept is no reason to stop
            return None
        return f"{self.frames_dir.name}/{name}"

    def _log(self, kind: str, **fields) -> None:
        record = {
            "t_ns": self.port.clock.now(),
            "kind": kind,
            "steps": self._view.get("steps"),
            **fields,
        }
        if len(self.records) < 5000:
            self.records.append(record)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, sort_keys=True, default=str)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
