"""The automatic grasp source (``levi/automatic/auto_grasp.py``) on fakes: no network, no GPU, no robot.

``AutoGraspGate`` decides the takeover's keys from a locator; the session under it is the real
``grasp_assist.AssistSession`` over the bridge ``Robot`` over the env fake, so every limit and every holding fact
is the production one. The locator is a simulated wrist camera that follows the env's pose (the pixel of the object
and the angle of its long axis are a function of the tool position and yaw), so the loop is closed: a wrong sign or
a wrong step shows up as an object that does not centre. ``QwenLocator`` is tested over a fake transport and over a
real HTTP server on the loopback."""

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest
from assist_rig import EMPTY, HELD, START, build, session

from levi.automatic import auto_grasp as ag
from levi.automatic import grasp_assist as ga
from levi.domain import aeri

OBJECT = "purple eggplant"
Z_GRASP = 0.12
HEIGHTS = (f"{OBJECT}|{Z_GRASP}",)
HW = (480, 640)
K_PX = 472.0  # u = cx + K * b / z: 20 px per cm at z = 0.236

# The wrist image of an upright mount: how the body frame (x forward, y left, metres) lands in
# pixels. A mirror (det < 0) and a turn (det > 0) both occur on real mounts; the sign of the
# yaw key follows the calibration, so both must converge.
MOUNTS = {
    "flip": ((0.0, -1.0), (-1.0, 0.0)),
    "turn": ((0.0, 1.0), (-1.0, 0.0)),
}


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    """handoff holding_fact (until V2) and the auto source (until its own acceptance) are
    planned levels the job refuses: the tests enable both, apart."""
    monkeypatch.setattr(aeri, "UNENABLED_HANDOFFS", ())
    monkeypatch.setattr(aeri, "UNENABLED_GRASP_SOURCES", ())


def assist_cfg(**over):
    kw = {
        "enabled": True,
        "grasp_source": "auto",
        "handoff": "holding_fact",
        "holding_band": (f"{OBJECT}|0.15|0.4",),
        "grasp_height": HEIGHTS,
        "step_m": 0.02,
        "max_steps": 80,
        "yaw_enabled": True,
        **over,
    }
    return ga.GraspAssistConfig(**kw)


class SimCamera:
    """A locator that looks at the env: the object is on the table at ``obj_xy`` with its long axis
    along ``heading_deg`` (world), the camera turns with the tool."""

    def __init__(
        self,
        env,
        obj_xy,
        *,
        heading_deg=30.0,
        mount="flip",
        elongated=True,
        visible_from_z=0.0,
        shift_scale=1.0,
        yaw_sign=1.0,
        error=None,
        offset=(0.0, 0.0),
    ):
        self.env, self.obj = env, obj_xy
        self.heading = math.radians(heading_deg)
        self.m0 = MOUNTS[mount] if isinstance(mount, str) else mount
        self.elongated, self.visible_from_z = elongated, visible_from_z
        self.shift_scale, self.yaw_sign, self.error = shift_scale, yaw_sign, error
        # Where the camera sits from the tool axis, in the tool's frame (metres). A real
        # wrist camera is some centimetres off: the fingertips are not at the image centre
        # and the point between them moves with the height and turns with the yaw.
        self.offset = offset
        self.rz0 = ga.quat_to_euler(env._quat_now())[2]
        self.calls: list = []
        self.on_call = None

    def tool(self):
        xyz = self.env._xyz()
        yaw = ga.quat_to_euler(self.env._quat_now())[2] - self.rz0
        return xyz, yaw * self.yaw_sign

    def pixel(self):
        (x, y, z), yaw = self.tool()
        wx, wy = self.obj[0] - x, self.obj[1] - y
        c, s = math.cos(-yaw), math.sin(-yaw)
        bx, by = c * wx - s * wy - self.offset[0], s * wx + c * wy - self.offset[1]
        (a, b), (cc, d) = self.m0
        k = K_PX / z * self.shift_scale
        return (320 + k * (a * bx + b * by), 240 + k * (cc * bx + d * by))

    def fingertip_pixel(self, z):
        """Where the point between the fingertips (the tool axis) is in the image with
        the tool at height ``z``: what ``grasp_pixel`` is measured to be offline."""
        (a, b), (cc, d) = self.m0
        k = K_PX / z * self.shift_scale
        bx, by = -self.offset[0], -self.offset[1]
        return (320 + k * (a * bx + b * by), 240 + k * (cc * bx + d * by))

    def axis_deg(self):
        _, yaw = self.tool()
        psi = self.heading - yaw
        (a, b), (cc, d) = self.m0
        dx, dy = math.cos(psi), math.sin(psi)
        return ag.fold_axis_deg(a * dx + b * dy, cc * dx + d * dy)

    def locate(self, image, name):
        self.calls.append(name)
        if self.on_call is not None:
            self.on_call(len(self.calls))
        if self.error is not None:
            return ag.Detection(False, error=self.error)
        if self.env._xyz()[2] < self.visible_from_z:
            return ag.Detection(False)
        u, v = self.pixel()
        if not (0 <= u <= HW[1] and 0 <= v <= HW[0]):
            return ag.Detection(False)
        return ag.Detection(
            True,
            center=(u, v),
            axis_deg=self.axis_deg() if self.elongated else None,
            confidence=0.9,
        )


class Base:
    """What terminal D would be for the rest of the orchestrator's gate calls."""

    def __init__(self):
        self.reports: list = []
        self.interrupt = None

    def assist_report(self, kind, data):
        self.reports.append((kind, data))

    def assist_interrupt(self):
        return self.interrupt

    def confirm_inventory(self, value):
        return ("confirmed", value)


def rig(*, obj_xy=(START[0] + 0.04, START[1] - 0.03), env_kw=None, stop=None, **cam):
    r = build(
        env_kw={"meta_age_ms": 20.0, **(env_kw or {})},  # a frame bus: auto needs it
        stop=stop,
        config_kw={
            "grasp_source": "auto",
            "handoff": "holding_fact",
            "grasp_height": HEIGHTS,
            "step_m": 0.02,
            "yaw_enabled": True,
            "max_steps": cam.pop("max_steps", 80),
            **cam.pop("config_kw", {}),
        },
    )
    r.env.image_hw = HW
    r.cam = SimCamera(r.env, obj_xy, **cam)
    return r


def gate_for(r, **over):
    cfg = over.pop("config", None)
    gate = ag.AutoGraspGate(
        Base(), r.port, r.cam, r.cfg, config=cfg, limits=r.limits, **over
    )
    return gate


def drive(r, gate, *, limit=300, words=OBJECT):
    """The orchestrator's ``_assist_loop`` over a real session: ``("handback", record)``,
    ``("abort", reason)`` or ``("stop", None)``. Returns the outcome and the session."""
    s = session(r, object_words=words)
    gate.assist_report("enter", {**s.view(), "episode": "e"})
    for _ in range(limit):
        key = gate.assist_key(s.view())
        if key == "stop":
            return ("stop", None), s
        try:
            proposal = s.propose(key)
            gate.assist_report("proposal", proposal.describe())
            if proposal.kind == "abort":
                return ("abort", last_why(gate)), s
            if proposal.kind == "handback":
                return ("handback", s.finalize_handback(proposal)), s
            result = s.execute(proposal)
        except ga.AssistRefused as exc:
            gate.assist_report(
                "refused", {"key": key, "code": exc.code, "detail": exc.detail}
            )
            continue
        except ga.AssistStopped:
            return ("stop", None), s
        except ga.AssistHeld as exc:
            return ("abort", exc.code), s
        gate.assist_report("result", {**result, "key": key, **s.view()})
    raise AssertionError("the gate never ended the takeover")


def last_why(gate):
    return [x for x in gate.records if x["kind"] == "decision"][-1]["why"]


def keys_of(gate):
    return [x["key"] for x in gate.records if x["kind"] == "decision"]


def pose_posts(r):
    return len([p for p in r.env.posts if p[0] == "pose"])


def routes(r, name):
    return len([p for p in r.env.posts if p[0] == name])


def tool_xyz(r):
    return r.env._xyz()


def settle(r):
    r.clock.advance(int(3e9))


# --- the happy path ------------------------------------------------------------------------------


@pytest.mark.parametrize("mount", ["flip", "turn"])
def test_the_object_is_centred_turned_vertical_grasped_and_handed_back(mount):
    r = rig(mount=mount, heading_deg=40.0, env_kw={"meta_age_ms": 20.0})
    gate = gate_for(r)
    (kind, record), s = drive(r, gate)
    assert kind == "handback", gate.records[-3:]
    assert record["by"] == "auto" and record["handoff"] == "holding_fact"
    # The hand-back re-measures the closed gripper right before it: that is the record.
    assert record["fact_status"] in ("holding_candidate", "holding_verified")
    keys = keys_of(gate)
    # The calibration is the first two keys; the descent, the close and the lift follow.
    assert keys[:2] == ["x+", "y+"]
    assert keys.index("close") < keys.index("lift") < keys.index("handback")
    assert "r+" in keys or "r-" in keys
    assert keys[-1] == "handback" and keys.count("close") == 1
    # The tool ended above the object at the grasp height, the long axis vertical.
    settle(r)
    x, y, z = tool_xyz(r)
    assert math.dist((x, y), r.cam.obj) < 0.02
    assert z == pytest.approx(Z_GRASP + 0.02, abs=0.011)  # lifted 2 cm after the close
    assert abs(90.0 - (r.cam.axis_deg() % 180.0)) <= 10.0 + 1e-6
    # All of it went through the takeover's keys and limits.
    assert set(keys) <= set(ga.KEYS)
    assert s.steps <= s.config.max_steps


@pytest.mark.parametrize("mount", ["flip", "turn"])
@pytest.mark.parametrize("heading, key", [(60.0, "r+"), (-50.0, "r-")])
def test_the_tool_turns_the_way_the_object_lies_whatever_the_mount_of_the_camera(
    mount, heading, key
):
    # Vertical in the image is the body x axis on both mounts, so a long axis at +60 degrees
    # in the world needs the tool turned +60 (r+), one at -50 degrees needs r-: the same
    # physical turn although the two mounts are a mirror image of each other (the key's sign
    # comes from the calibration, not from a table).
    r = rig(mount=mount, heading_deg=heading)
    gate = gate_for(r)
    (kind, _), _ = drive(r, gate)
    assert kind == "handback"
    turned = {k for k in keys_of(gate) if k in ("r+", "r-")}
    assert turned == {key}
    settle(r)
    yaw = math.degrees(r.cam.tool()[1])
    assert abs(((yaw - heading + 90) % 180) - 90) <= 10.0 + 1e-6


def test_a_round_object_is_not_turned():
    r = rig(elongated=False)
    gate = gate_for(r)
    (kind, _), _ = drive(r, gate)
    assert kind == "handback"
    assert not {"r+", "r-"} & set(keys_of(gate))
    assert any(x["kind"] == "yaw_skipped" for x in gate.records)


def test_an_object_already_vertical_is_not_turned():
    r = rig(
        heading_deg=0.0, mount="turn"
    )  # the "turn" mount maps heading 0 to vertical
    assert abs(90.0 - (r.cam.axis_deg() % 180.0)) <= 10.0
    gate = gate_for(r)
    (kind, _), _ = drive(r, gate)
    assert kind == "handback"
    assert not {"r+", "r-"} & set(keys_of(gate))


def test_every_decision_is_logged_with_its_frame_and_the_locators_answer(tmp_path):
    r = rig(env_kw={"meta_age_ms": 15.0})
    path = tmp_path / "run" / "auto_grasp.jsonl"
    gate = gate_for(
        r, log_path=path, encode=lambda image: b"jpeg" + str(image.shape).encode()
    )
    (kind, _), _ = drive(r, gate)
    assert kind == "handback"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == json.loads(json.dumps(gate.records, default=str))
    kinds = {x["kind"] for x in lines}
    assert {"enter", "locate", "decision", "calibration", "centered"} <= kinds
    locates = [x for x in lines if x["kind"] == "locate"]
    assert locates and all(
        x["frame"]["seq"] is not None and x["frame"]["age_ms"] == 15.0 for x in locates
    )
    assert all(x["detection"]["found"] for x in locates)
    first = next(x for x in lines if x["kind"] == "decision")
    assert first["key"] == "x+" and first["why"] == "calibrate_image_axes"
    # Frames are kept next to the log, never anywhere else.
    kept = sorted(p.name for p in (tmp_path / "run" / "auto_grasp").iterdir())
    assert kept[0] == "0001.jpg" and len(kept) == len(locates)
    assert locates[0]["frame"]["file"] == "auto_grasp/0001.jpg"


def test_the_rest_of_the_gate_is_the_terminals():
    r = rig()
    gate = gate_for(r)
    assert gate.confirm_inventory(3) == ("confirmed", 3)
    with pytest.raises(AttributeError):
        gate.nothing_like_this  # noqa: B018
    # The display gets every report as well.
    gate.assist_report("refused", {"key": "x+", "code": "c", "detail": "d"})
    assert gate._base.reports == [
        ("refused", {"key": "x+", "code": "c", "detail": "d"})
    ]


# --- the target is not there ---------------------------------------------------------------------


def test_a_target_that_is_not_visible_is_looked_for_from_higher_then_aborted():
    r = rig(visible_from_z=1.0)  # never visible
    gate = gate_for(r)
    before = START[2]
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "target_not_found")
    assert keys_of(gate) == ["z+"] * ag.FIND_UP_MAX + ["abort"]
    settle(r)
    assert tool_xyz(r)[2] == pytest.approx(
        before + 0.02 * ag.FIND_UP_MAX, abs=0.005 * ag.FIND_UP_MAX
    )  # moves start from the measured pose (each raise lags a few mm in the rig)


def test_a_target_seen_after_one_step_up_goes_on():
    r = rig(visible_from_z=START[2] + 0.015)
    gate = gate_for(r)
    (kind, _), _ = drive(r, gate)
    assert kind == "handback"
    assert keys_of(gate)[:3] == ["z+", "x+", "y+"]


def test_a_locator_that_cannot_answer_aborts_at_once_without_moving():
    r = rig(error="request_failed: TimeoutError")
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "locator_unavailable")
    assert keys_of(gate) == ["abort"] and pose_posts(r) == 0


def test_a_locator_that_raises_never_ends_the_takeover_by_itself():
    r = rig()

    def boom(image, name):
        raise RuntimeError("model exploded")

    r.cam.locate = boom
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "locator_unavailable") and pose_posts(r) == 0


def test_a_target_lost_while_centring_is_an_abort():
    r = rig()
    gate = gate_for(r)
    real, n = r.cam.locate, {"calls": 0}

    def flaky(image, name):
        # Seen for the find and the two calibration looks, then gone.
        n["calls"] += 1
        return real(image, name) if n["calls"] <= 3 else ag.Detection(False)

    r.cam.locate = flaky
    (kind, why), _ = drive(r, gate)
    assert kind == "abort" and why.startswith("target_lost_")


def test_a_low_confidence_answer_counts_as_not_seen():
    r = rig()
    gate = gate_for(r)
    real = r.cam.locate

    def timid(image, name):
        found = real(image, name)
        return ag.Detection(
            True, center=found.center, axis_deg=found.axis_deg, confidence=0.1
        )

    r.cam.locate = timid
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "target_not_found")


# --- calibration ---------------------------------------------------------------------------------


def test_a_target_that_does_not_move_in_the_image_is_a_degenerate_calibration():
    r = rig(shift_scale=0.0)
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "calibration_degenerate")
    assert keys_of(gate)[:2] == ["x+", "y+"] and "z-" not in keys_of(gate)


def test_parallel_image_axes_are_a_degenerate_calibration():
    r = rig(mount=((1.0, 1.0), (1.0, 1.0)))
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "calibration_degenerate")


def test_a_tool_that_did_not_move_is_a_degenerate_calibration():
    r = rig()
    gate = gate_for(r)
    # The env answers every pose command with the old pose: the arm never moves, so the
    # measured shift per metre cannot be computed (and the takeover reports a miss).
    r.env.stuck = True
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "key_did_not_arrive")


# --- grasp height --------------------------------------------------------------------------------


def test_a_missing_grasp_height_aborts_before_anything_moves():
    r = rig(config_kw={"grasp_height": ("pink plate|0.1",)})
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "grasp_height_missing")
    assert keys_of(gate) == ["abort"] and pose_posts(r) == 0 and r.cam.calls == []


def test_a_grasp_height_below_the_floor_aborts_before_anything_moves():
    r = rig(config_kw={"grasp_height": (f"{OBJECT}|0.04",)})
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "grasp_height_below_floor") and pose_posts(r) == 0


def test_no_object_name_aborts_before_anything_moves():
    r = rig()
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate, words="")
    assert (kind, why) == ("abort", "no_object_name") and pose_posts(r) == 0


# --- an empty grasp ------------------------------------------------------------------------------


def flip_after_first_close(r, to):
    real = r.env._post
    state = {"closes": 0}

    def post(route, payload=None):
        if route == "close_gripper":
            state["closes"] += 1
            if state["closes"] == 2:
                r.env.object_opening = to
        return real(route, payload)

    r.env._post = post


def test_an_empty_grasp_opens_backs_up_and_tries_again_then_hands_back():
    r = rig(env_kw={"object_opening": EMPTY}, max_steps=200)
    flip_after_first_close(r, HELD)
    gate = gate_for(r)
    (kind, record), _ = drive(r, gate)
    assert kind == "handback" and record["by"] == "auto"
    keys = keys_of(gate)
    assert keys.count("close") == 2 and keys.count("open") == 1
    first_open = keys.index("open")
    assert keys[first_open + 1] == "z+"  # back up for the retry
    assert any(
        x["why"] == "grasp_did_not_hold"
        for x in gate.records
        if x["kind"] == "decision"
    )
    # The second attempt did not calibrate again.
    assert keys.count("x+") == 1 or keys.count("x+") >= 1
    first_open = next(i for i, x in enumerate(gate.records) if x.get("key") == "open")
    assert not any(x["kind"] == "calibration" for x in gate.records[first_open:])


def test_grasps_that_never_hold_are_retried_then_the_takeover_is_abandoned():
    r = rig(
        env_kw={"object_opening": EMPTY},
        max_steps=200,
        config_kw={"auto_max_retries": 2},
    )
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "grasp_failed")
    keys = keys_of(gate)
    assert keys.count("close") == 3 and keys.count("open") == 2
    assert "handback" not in keys and keys[-1] == "abort" and "lift" not in keys


def test_no_retry_when_the_job_allows_none():
    r = rig(
        env_kw={"object_opening": EMPTY},
        max_steps=200,
        config_kw={"auto_max_retries": 0},
    )
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "grasp_failed")
    assert keys_of(gate).count("close") == 1 and "open" not in keys_of(gate)


def test_a_refused_hand_back_holds_and_aborts_it_does_not_drop_the_object():
    r = rig()
    gate = gate_for(r)
    s = session(r, object_words=OBJECT)
    gate.assist_report("enter", {**s.view(), "episode": "e"})
    # Drive by hand up to the hand-back, then report it refused.
    for _ in range(200):
        key = gate.assist_key(s.view())
        if key == "handback":
            break
        p = s.propose(key)
        gate.assist_report("proposal", p.describe())
        gate.assist_report("result", {**s.execute(p), "key": key, **s.view()})
    gate.assist_report(
        "refused", {"key": "handback", "code": "holding_not_verified", "detail": ""}
    )
    # The object is lifted: no "open" (it would fall), no second hand-back, a person looks.
    assert gate.assist_key(s.view()) == "abort"
    last = [x for x in gate.records if x["kind"] == "decision"][-1]
    assert last["why"] == "handback_refused"
    assert last["detail"]["code"] == "holding_not_verified"


def test_a_grasp_that_slips_before_the_lift_is_a_failed_grasp_not_an_abort():
    r = rig()
    gate = gate_for(r)
    s = session(r, object_words=OBJECT)
    gate.assist_report("enter", {**s.view(), "episode": "e"})
    for _ in range(200):
        key = gate.assist_key(s.view())
        if key == "lift":
            break
        p = s.propose(key)
        gate.assist_report("proposal", p.describe())
        gate.assist_report("result", {**s.execute(p), "key": key, **s.view()})
    gate.assist_report(
        "refused",
        {"key": "lift", "code": "lift_needs_holding_candidate", "detail": "slipped"},
    )
    assert gate.assist_key(s.view()) == "open"


# --- the stop ------------------------------------------------------------------------------------


def test_a_registered_stop_is_the_next_key_and_nothing_is_decided():
    stop = {"flag": True}
    r = rig(stop=stop)
    gate = gate_for(r)
    s = session(r, object_words=OBJECT)
    gate.assist_report("enter", {**s.view(), "episode": "e"})
    assert gate.assist_key(s.view()) == "stop"
    assert r.cam.calls == [] and pose_posts(r) == 0


def test_a_stop_during_the_locator_call_drops_the_key():
    stop = {"flag": False}
    r = rig(stop=stop)
    r.cam.on_call = lambda n: stop.update(flag=n >= 3)  # during the first centring look
    gate = gate_for(r)
    (kind, _), _ = drive(r, gate)
    assert kind == "stop"
    posted = pose_posts(r)
    assert keys_of(gate)[-1] in (
        "x+",
        "y+",
        "x-",
        "y-",
    )  # the keys sent before the stop
    assert any(x["kind"] == "interrupted" and x["dropped"] for x in gate.records)
    # Nothing is sent once the stop was seen.
    assert pose_posts(r) == posted


def test_a_stop_typed_at_the_terminal_ends_the_takeover():
    r = rig()
    gate = gate_for(r)
    gate._base.interrupt = "stop"
    s = session(r, object_words=OBJECT)
    gate.assist_report("enter", {**s.view(), "episode": "e"})
    assert gate.assist_key(s.view()) == "stop" and pose_posts(r) == 0
    gate._base.interrupt = "abort"
    assert gate.assist_key(s.view()) == "abort"


# --- limits are the takeover's, not argued with --------------------------------------------------


def test_keys_run_out_with_the_step_budget_and_the_takeover_is_abandoned():
    r = rig(max_steps=5)
    gate = gate_for(r)
    (kind, why), s = drive(r, gate)
    assert (kind, why) == ("abort", "max_steps_reached")
    assert s.steps == 5 and "handback" not in keys_of(gate)


def test_a_refusal_by_the_takeover_ends_in_the_abort_path():
    r = rig(config_kw={"max_travel_m": 0.03})
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert kind == "abort" and why.startswith("refused_travel_exceeded")


def test_a_yaw_the_takeover_refuses_ends_in_the_abort_path():
    r = rig(heading_deg=80.0, config_kw={"max_yaw_rad": 0.1})
    gate = gate_for(r)
    (kind, why), s = drive(r, gate)
    assert (kind, why) == ("abort", "refused_yaw_exceeded")
    assert s.refused == 1


def test_a_yaw_that_makes_the_error_grow_is_not_followed():
    # The camera turns the other way from what the calibration says: the first yaw key
    # makes the angle worse and the plan stops after it, it does not spin on.
    r = rig(heading_deg=45.0, yaw_sign=-1.0)
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "yaw_direction_unexpected")
    assert len([k for k in keys_of(gate) if k in ("r+", "r-")]) == 1


def test_centring_that_does_not_converge_is_given_up():
    r = rig()
    gate = gate_for(r, config=ag.AutoGraspConfig(max_center_keys=1, center_tol_px=1.0))
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "centering_did_not_converge")


def test_a_gate_that_breaks_aborts_instead_of_moving():
    r = rig()
    gate = gate_for(r)
    gate._plan = lambda: (_ for _ in ()).throw(KeyError("bug"))
    s = session(r, object_words=OBJECT)
    gate.assist_report("enter", {**s.view(), "episode": "e"})
    assert gate.assist_key(s.view()) == "abort"
    assert any(x["kind"] == "error" for x in gate.records) and pose_posts(r) == 0


def test_a_comms_fault_is_not_swallowed():
    r = rig()
    gate = gate_for(r)
    s = session(r, object_words=OBJECT)
    gate.assist_report("enter", {**s.view(), "episode": "e"})

    def broken():
        raise ga.AssistFault("observe_failed", "camera gone")

    r.port.observe = broken
    with pytest.raises(ga.AssistFault):
        gate.assist_key(s.view())


# --- frames --------------------------------------------------------------------------------------


def test_a_frame_that_is_too_old_is_never_used():
    r = rig(env_kw={"meta_age_ms": 2000.0})
    gate = gate_for(r)
    began = r.clock.now()
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "stale_frame")
    assert r.cam.calls == [] and pose_posts(r) == 0
    assert (r.clock.now() - began) / 1e9 >= ag.FRAME_WAIT_S  # it waited for a newer one
    assert any(x["kind"] == "frame_stale" for x in gate.records)


def test_a_frame_older_than_the_last_move_is_waited_for_and_the_wait_is_in_the_log():
    r = rig(env_kw={"meta_age_ms": 300.0})  # fresh enough alone, but older than a move
    gate = gate_for(r)
    (kind, _), _ = drive(r, gate)
    assert kind == "handback"
    # Every locate that came after a move used a frame captured after that move ended.
    ended = None
    waited = 0
    for rec in gate.records:
        if rec["kind"] == "outcome" and not rec.get("refused"):
            ended = rec["t_ns"]
        elif rec["kind"] == "locate" and ended is not None:
            captured = rec["t_ns"] - int(rec["frame"]["age_ms"] * 1e6)
            assert captured >= ended
            assert rec["t_ns"] - ended >= int(0.3e9)  # it had to wait the frame's age
            waited += 1
    assert waited >= 3


def test_without_a_frame_bus_auto_aborts_before_anything_moves():
    r = rig()
    r.env.meta_age_ms = None  # an openpi without image_meta
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "no_frame_bus")
    assert r.cam.calls == [] and pose_posts(r) == 0
    assert any(x["kind"] == "no_frame_bus" for x in gate.records)


def test_a_camera_whose_frames_stop_is_found_out_after_the_first_move():
    r = rig()
    # The first look is fine; from then on the camera repeats its last frame (a frozen
    # USB camera): the seq does not advance after the calibration move.
    r.cam.on_call = lambda n: (
        setattr(r.env, "seq_cap", r.env.obs_count) if n == 1 else None
    )
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "stale_frame")
    assert keys_of(gate) == ["x+", "abort"]
    stale = [x for x in gate.records if x["kind"] == "frame_stale"]
    assert stale and stale[0]["why"] == "seq_not_advanced"


def test_a_camera_frozen_at_seq_zero_is_found_out_too():
    # Review N3: ``last_seq or -1`` read a last seq of 0 as "none", so a camera stuck
    # at frame 0 passed the freshness check after a move.
    r = rig()
    r.env.seq_cap = 0
    gate = gate_for(r)
    (kind, why), _ = drive(r, gate)
    assert (kind, why) == ("abort", "stale_frame")
    assert keys_of(gate) == ["x+", "abort"]


# --- a typed key during the locator call, descent, request ----------------------------------------


@pytest.mark.parametrize("typed", ["abort", "stop"])
def test_a_key_typed_at_the_terminal_during_the_locator_call_wins_over_the_decided_key(
    typed,
):
    r = rig()
    gate = gate_for(r)
    r.cam.on_call = lambda n: (
        setattr(gate._base, "interrupt", typed) if n == 3 else None
    )
    (kind, _), _ = drive(r, gate)
    assert kind == typed
    # Calls 1-3 were the find and the two calibration looks: x+ and y+ went out, and the
    # key decided after the third look (a centring step) was dropped, not sent.
    assert keys_of(gate) == ["x+", "y+"]
    dropped = [
        x for x in gate.records if x["kind"] == "interrupted" and x.get("dropped")
    ]
    assert dropped and dropped[0]["key"] == typed
    assert pose_posts(r) == 2 * ga.SUBSTEPS


@pytest.mark.parametrize("grasp_z", [0.100, 0.112, 0.120, 0.125, 0.131, 0.150])
def test_the_descent_never_goes_more_than_5_mm_below_the_registered_height(grasp_z):
    r = rig(config_kw={"grasp_height": (f"{OBJECT}|{grasp_z}",)})
    gate = gate_for(r)
    (kind, _), _ = drive(r, gate)
    assert kind == "handback"
    close_at = next(
        x for x in gate.records if x["kind"] == "decision" and x["key"] == "close"
    )
    settle(r)
    lowest = min(z for (_, _, z) in r.env.pose_targets)
    assert lowest >= grasp_z - ag.DESCENT_TOL_M - 1e-9
    assert lowest < grasp_z + 0.02  # and no further than one step above it
    assert close_at["detail"]["z"] == grasp_z


def test_the_sites_extra_body_cannot_replace_the_schema_the_model_or_the_sampling():
    loc = ag.QwenLocator(
        "http://127.0.0.1:8100",
        "qwen-test",
        encode=lambda image: b"jpeg",
        extra_body={
            "response_format": {"type": "text"},
            "temperature": 1.5,
            "model": "other",
            "messages": [],
            "max_tokens": 5000,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    body = loc.request(np.zeros((480, 640, 3), dtype=np.uint8), "x")
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["temperature"] == 0 and body["model"] == "qwen-test"
    assert len(body["messages"]) == 1 and body["max_tokens"] == loc.max_tokens
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


# --- the run's entry -----------------------------------------------------------------------------


def test_the_entry_wraps_terminal_d_in_the_auto_gate_only_for_the_auto_source(tmp_path):
    from types import SimpleNamespace as NS

    from levi.automatic import fr3_run

    r = rig()
    parts = {"grasp_assist": ga.AssistRig(r.port, r.limits)}
    vlm = {
        "base_url": "http://127.0.0.1:8100",
        "model": "qwen-test",
        "timeout_s": 60,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    terminal = object()
    gate = fr3_run.auto_grasp_gate(
        NS(subtask_grasp_assist=r.cfg), terminal, parts, vlm, tmp_path, lambda i: b""
    )
    assert isinstance(gate, ag.AutoGraspGate) and gate._base is terminal
    assert gate.log_path == tmp_path / "auto_grasp.jsonl" and gate.limits == r.limits
    assert isinstance(gate.locator, ag.QwenLocator)
    assert gate.locator.base_url == "http://127.0.0.1:8100"
    assert gate.locator.model == "qwen-test" and gate.locator.timeout_s == 20.0
    assert gate.locator.extra_body == vlm["extra_body"]
    operator_cfg = ga.GraspAssistConfig(
        enabled=True, holding_band=(f"{OBJECT}|0.15|0.4",)
    )
    for cfg in (
        NS(subtask_grasp_assist=operator_cfg),
        NS(subtask_grasp_assist=None),
        NS(subtask_grasp_assist=assist_cfg(enabled=False)),
    ):
        assert (
            fr3_run.auto_grasp_gate(cfg, terminal, parts, vlm, tmp_path, None)
            is terminal
        )


# --- config and job validation -------------------------------------------------------------------


def test_the_auto_source_needs_the_holding_fact_hand_over():
    with pytest.raises(ValueError, match="auto_needs_holding_fact"):
        ga.GraspAssistConfig(
            grasp_source="auto",
            handoff="operator",
            holding_band=(f"{OBJECT}|0.15|0.4",),
            grasp_height=HEIGHTS,
        )
    assert assist_cfg().grasp_source == "auto"


def test_the_auto_source_needs_a_grasp_height():
    with pytest.raises(ValueError, match="grasp_height"):
        assist_cfg(grasp_height=())
    with pytest.raises(ValueError, match="one '<words>\\|<z>' entry per object"):
        assist_cfg(grasp_height=(f"{OBJECT}|0.1", f"{OBJECT}|0.2"))
    for bad in ("x y|0", "x y|1", "x y|nan", "x y|two"):
        with pytest.raises(ValueError, match="grasp_height"):
            assist_cfg(grasp_height=(bad,))
    assert assist_cfg().grasp_heights() == {OBJECT: Z_GRASP}


def test_the_auto_retry_count_is_checked_and_stays_out_of_the_plan_at_its_default():
    assert (
        assist_cfg().auto_max_retries == 2
        and "auto_max_retries" not in assist_cfg().describe()
    )
    assert assist_cfg(auto_max_retries=0).describe()["auto_max_retries"] == 0
    assert assist_cfg(auto_max_retries=ga.MAX_AUTO_RETRIES_CAP).auto_max_retries == (
        ga.MAX_AUTO_RETRIES_CAP
    )
    for bad in (-1, ga.MAX_AUTO_RETRIES_CAP + 1, 1.5, True):
        with pytest.raises(ValueError, match="auto_max_retries"):
            assist_cfg(auto_max_retries=bad)
    assert assist_cfg().describe()["grasp_height"] == list(HEIGHTS)


def test_operator_jobs_describe_themselves_as_before():
    cfg = ga.GraspAssistConfig(enabled=True, holding_band=(f"{OBJECT}|0.15|0.4",))
    assert set(cfg.describe()) == {
        "enabled",
        "grasp_source",
        "handoff",
        "step_m",
        "max_steps",
        "max_travel_m",
        "follow_error_m",
        "max_follow_misses",
        "holding_band",
    }


def test_the_job_model_knows_the_auto_source():
    part = aeri.JobSubtaskGraspAssist
    assert aeri.GRASP_ASSIST_SOURCES == ("operator", "auto")
    ok = part(
        grasp_source="auto", handoff="holding_fact", grasp_height=[f"{OBJECT}|0.12"]
    )
    assert ok.grasp_source == "auto"
    with pytest.raises(ValueError, match="auto_needs_holding_fact"):
        part(grasp_source="auto", handoff="operator", grasp_height=[f"{OBJECT}|0.12"])
    with pytest.raises(ValueError, match="auto_needs_holding_fact"):
        part(grasp_source="auto", grasp_height=[f"{OBJECT}|0.12"])
    with pytest.raises(ValueError, match="grasp_height"):
        part(grasp_source="auto", handoff="holding_fact")
    for bad in ("purple eggplant|1.5", "Purple|0.1", "purple eggplant|"):
        with pytest.raises(ValueError):
            part(grasp_source="auto", handoff="holding_fact", grasp_height=[bad])


def test_the_job_refuses_the_auto_source_until_the_holding_fact_handoff_is_enabled(
    monkeypatch,
):
    monkeypatch.setattr(aeri, "UNENABLED_HANDOFFS", ("holding_fact",))
    with pytest.raises(ValueError, match="handoff_not_enabled"):
        aeri.JobSubtaskGraspAssist(
            grasp_source="auto",
            handoff="holding_fact",
            grasp_height=[f"{OBJECT}|0.12"],
        )


def test_the_hand_back_record_says_who_decided():
    proposal = ga.Proposal("handback", "handback")
    assert session(rig()).handback_record(proposal)["by"] == "auto"
    assert session(build()).handback_record(proposal)["by"] == "operator"
    holding = build(config_kw={"handoff": "holding_fact"})
    assert session(holding).handback_record(proposal)["by"] == "holding_fact"


# --- the locator: parsing --------------------------------------------------------------------------


def reply(**over):
    body = {"found": True, "center": [300, 200], "endpoints": [[300, 100], [300, 300]]}
    body.update(over)
    return json.dumps(body)


def test_a_valid_reply_gives_the_centre_and_the_axis_folded_to_90():
    det = ag.parse_locate_reply(reply(), 640, 480)
    assert det.found and det.center == (300.0, 200.0)
    assert det.axis_deg == pytest.approx(90.0)  # a vertical axis
    det = ag.parse_locate_reply(
        reply(endpoints=[[100, 100], [300, 100]], confidence=0.8), 640, 480
    )
    assert det.axis_deg == pytest.approx(0.0) and det.confidence == 0.8
    # A line has no direction: the endpoints' order does not matter.
    flipped = ag.parse_locate_reply(reply(endpoints=[[300, 100], [100, 100]]), 640, 480)
    assert flipped.axis_deg == pytest.approx(0.0)
    diagonal = ag.parse_locate_reply(
        reply(endpoints=[[100, 100], [200, 200]]), 640, 480
    )
    assert diagonal.axis_deg == pytest.approx(45.0)
    assert ag.fold_axis_deg(1, -1) == pytest.approx(-45.0)
    assert ag.fold_axis_deg(0, -1) == pytest.approx(90.0)


def test_endpoints_missing_or_too_close_mean_no_axis():
    assert ag.parse_locate_reply(reply(endpoints=None), 640, 480).axis_deg is None
    short = ag.parse_locate_reply(reply(endpoints=[[300, 200], [310, 205]]), 640, 480)
    assert short.found and short.axis_deg is None


def test_not_found_is_a_clean_answer_not_an_error():
    det = ag.parse_locate_reply(
        json.dumps({"found": False, "center": None, "endpoints": None}), 640, 480
    )
    assert det.found is False and det.error is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[1, 2]",
        "null",
        "```json\n{}\n```",
        json.dumps({"found": True}),
        json.dumps({"found": "yes", "center": [1, 1], "endpoints": None}),
        json.dumps({"found": True, "center": [1, 1], "endpoints": None, "extra": 1}),
        json.dumps({"found": True, "center": [1], "endpoints": None}),
        json.dumps({"found": True, "center": ["1", "2"], "endpoints": None}),
        json.dumps({"found": True, "center": [True, 2], "endpoints": None}),
        json.dumps({"found": True, "center": [700, 100], "endpoints": None}),
        json.dumps({"found": True, "center": [-1, 100], "endpoints": None}),
        json.dumps({"found": True, "center": [1, 480.5], "endpoints": None}),
        json.dumps({"found": True, "center": [1, 1], "endpoints": [[1, 1]]}),
        json.dumps({"found": True, "center": [1, 1], "endpoints": [[1, 1], [900, 1]]}),
        json.dumps(
            {"found": True, "center": [1, 1], "endpoints": None, "confidence": 2}
        ),
        json.dumps({"found": False, "center": [1, 1], "endpoints": None}),
        '{"found": true, "center": [NaN, 1], "endpoints": null}',
        None,
        5,
    ],
)
def test_anything_but_a_whole_valid_reply_is_not_found_with_a_reason(text):
    det = ag.parse_locate_reply(text, 640, 480)
    assert det.found is False and det.error and det.error.startswith("bad_reply")


def test_a_normalised_grid_is_scaled_to_the_image():
    det = ag.parse_locate_reply(
        reply(center=[500, 500], endpoints=[[500, 0], [500, 1000]]),
        640,
        480,
        coords="norm1000",
    )
    assert det.center == (320.0, 240.0) and det.axis_deg == pytest.approx(90.0)


# --- the locator: the request and the transport ----------------------------------------------------


def chat(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


def locator(post, **kw):
    return ag.QwenLocator(
        "http://127.0.0.1:8100",
        "qwen-test",
        encode=lambda image: b"\xff\xd8jpeg",
        post=post,
        **kw,
    )


def test_the_request_is_one_strict_chat_completion_with_the_image_and_the_object():
    seen = []

    def post(url, body, timeout):
        seen.append((url, body, timeout))
        return chat(reply())

    image = np.zeros((480, 640, 3), dtype=np.uint8)
    det = locator(
        post, extra_body={"chat_template_kwargs": {"enable_thinking": False}}
    ).locate(image, "purple eggplant")
    assert det.found and det.center == (300.0, 200.0)
    ((url, body, timeout),) = seen
    assert (
        url == "http://127.0.0.1:8100/v1/chat/completions"
        and timeout <= ag.LOCATE_TIMEOUT_S
    )
    assert body["model"] == "qwen-test" and body["temperature"] == 0
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    parts = body["messages"][0]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    text = parts[1]["text"]
    assert (
        "purple eggplant" in text
        and "640 pixels wide" in text
        and "480 pixels high" in text
    )
    assert (
        body["response_format"]["json_schema"]["schema"]["additionalProperties"]
        is False
    )


def test_the_grid_locator_asks_for_the_grid_and_reads_it():
    # Asked for pixels, Qwen3-VL answered on the 0..1000 grid anyway (11-18 replay):
    # the question must say the scale the answer is read in.
    seen = []

    def post(url, body, timeout):
        seen.append(body)
        return chat(reply(center=[500, 500], endpoints=[[500, 100], [500, 900]]))

    image = np.zeros((480, 640, 3), dtype=np.uint8)
    det = locator(post, coords="norm1000").locate(image, "eggplant")
    text = seen[0]["messages"][0]["content"][1]["text"]
    assert "0 to 1000 grid" in text and "pixels wide" not in text
    assert det.found and det.center == (320.0, 240.0) and det.axis_deg == 90.0


@pytest.mark.parametrize(
    "answer",
    [
        chat("garbage"),
        chat(reply(), finish="length"),
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"nothing": 1},
        "text",
        None,
    ],
)
def test_a_bad_http_answer_is_not_found(answer):
    det = locator(lambda url, body, timeout: answer).locate(
        np.zeros((480, 640, 3), dtype=np.uint8), "x"
    )
    assert det.found is False and det.error


@pytest.mark.parametrize(
    "exc", [TimeoutError("t"), OSError("refused"), ValueError("v")]
)
def test_a_transport_failure_is_not_found_and_never_raises(exc):
    def post(url, body, timeout):
        raise exc

    det = locator(post).locate(np.zeros((480, 640, 3), dtype=np.uint8), "x")
    assert det.found is False and det.error.startswith("request_failed")


def test_an_encoder_that_fails_is_not_found():
    def encode(image):
        raise RuntimeError("no jpeg")

    det = ag.QwenLocator(
        "http://127.0.0.1:8100", "m", encode=encode, post=lambda *a: chat(reply())
    ).locate(np.zeros((480, 640, 3), dtype=np.uint8), "x")
    assert det.found is False and det.error.startswith("request_failed")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8100",
        "https://127.0.0.1:8100",
        "http://127.0.0.1",
        "http://127.0.0.1:5000",  # a robot port: never
        "http://127.0.0.1:8000",
        "http://user@127.0.0.1:8100",
    ],
)
def test_the_endpoint_is_a_loopback_url_that_is_not_a_robot_port(url):
    with pytest.raises(ValueError):
        ag.QwenLocator(url, "m", encode=lambda image: b"")


def test_the_timeout_is_capped():
    assert locator(None, timeout_s=600).timeout_s == ag.LOCATE_TIMEOUT_S
    with pytest.raises(ValueError):
        locator(None, coords="metres")


def test_a_real_http_exchange_over_the_loopback():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, body))
            data = json.dumps(chat(reply())).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        found = ag.QwenLocator(
            f"http://127.0.0.1:{port}", "m", encode=lambda image: b"jpeg", timeout_s=5
        ).locate(np.zeros((480, 640, 3), dtype=np.uint8), "eggplant")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert found.found and found.center == (300.0, 200.0)
    assert received[0][0] == "/v1/chat/completions"


# --- the whole thing through the orchestrator ------------------------------------------------------


def test_the_orchestrator_hands_the_arm_over_on_the_auto_gates_keys(tmp_path):
    from subtask_rig import (
        FORWARD,
        REVERSE,
        committed,
        notes,
        sub_config,
    )
    from subtask_rig import (
        build as build_run,
    )
    from test_subtask_grasp_assist import (
        AssistGate,
        AssistPolicy,
        share_gate,
    )
    from test_subtask_reset_role import ENTRIES

    from levi.automatic import state_machine as sm
    from levi.automatic import subtask_loop as st

    pink = "pink plate"
    fence = sm.MotionFence()
    rig_ = build(
        fence=fence,
        band=f"{pink}|0.15|0.4",
        config_kw={
            "grasp_source": "auto",
            "handoff": "holding_fact",
            "grasp_height": (f"{pink}|{Z_GRASP}",),
        },
    )
    rig_.env.image_hw = HW
    rig_.env.meta_age_ms = 20.0
    cam = SimCamera(rig_.env, (START[0] + 0.03, START[1] + 0.02), heading_deg=20.0)
    cfg = sub_config(
        episodes=1,
        subtask_reverse_policy_role="reset",
        subtask_reset_prompts=ENTRIES,
        subtask_grasp_assist={
            "enabled": True,
            "grasp_source": "auto",
            "handoff": "holding_fact",
            "holding_band": [f"{pink}|0.15|0.4"],
            "grasp_height": [f"{pink}|{Z_GRASP}"],
            "step_m": 0.02,
            "max_steps": 80,
            "yaw_enabled": True,
        },
    )
    terminal = AssistGate([])
    gate = ag.AutoGraspGate(
        terminal, rig_.port, cam, cfg.subtask_grasp_assist, limits=rig_.limits
    )
    r = build_run(
        tmp_path,
        cfg=cfg,
        fence=fence,
        gate=gate,
        grasp_assist=ga.AssistRig(rig_.port, rig_.limits),
        plans=[st.plan_answer(FORWARD), st.plan_answer(REVERSE)],
        checks=st.checks(True, True, True, True),
        policy_factory=lambda clock, trace, power: AssistPolicy(
            clock, trace=trace, power=power
        ),
    )
    r.policy.robot = r.robot
    r.rig = rig_
    share_gate(r, rig_)
    rig_.port.stop_probe = lambda: r.orch.stop_requested()
    awake_when_looking = []
    look = cam.locate
    cam.locate = lambda image, name: (
        awake_when_looking.append(r.power.awake),
        look(image, name),
    )[1]
    assert r.orch.run() == "COMPLETED"
    # The VLM the locator asks was woken for the takeover (it sleeps for the reset
    # policy, R7) and was asleep again whenever a policy started.
    assert awake_when_looking and all(awake_when_looking)
    assert r.policy.awake_at_acquire and not any(r.policy.awake_at_acquire)
    # The terminal never asked a person for a key: the auto gate decided every one.
    assert ("assist_key",) not in r.trace
    handoff = notes(r.orch, "grasp_assist_handoff")[0]
    assert handoff["by"] == "auto" and handoff["fact_status"] in (
        "holding_candidate",
        "holding_verified",
    )
    moves = committed(r.orch)
    assert ("SCENE_ASSESS", "GRASP_ASSIST", "grasp_assist_started") in moves
    assert ("GRASP_ASSIST", "RESET_ACTIVE", "subtask_dispatched") in moves
    # The object words the plan used are the subtask's pick.
    assert set(cam.calls) == {pink}
    assert gate.records[0]["kind"] == "enter" and gate.records[0]["object"] == pink
    # The terminal still got every report to display.
    assert terminal.reports[0][0] == "enter"
    assert terminal.reports[-1][0] == "handback"


def test_the_orchestrator_waits_for_a_person_when_the_auto_grasp_gives_up(tmp_path):
    from subtask_rig import FORWARD, REVERSE, committed, sub_config
    from subtask_rig import build as build_run
    from test_subtask_grasp_assist import AssistGate, AssistPolicy, share_gate
    from test_subtask_reset_role import ENTRIES

    from levi.automatic import state_machine as sm
    from levi.automatic import subtask_loop as st

    pink = "pink plate"
    fence = sm.MotionFence()
    rig_ = build(
        fence=fence,
        band=f"{pink}|0.15|0.4",
        config_kw={
            "grasp_source": "auto",
            "handoff": "holding_fact",
            "grasp_height": (f"{pink}|{Z_GRASP}",),
        },
    )
    rig_.env.image_hw = HW
    rig_.env.meta_age_ms = 20.0
    cam = SimCamera(rig_.env, (START[0], START[1]), visible_from_z=9.0)  # never seen
    cfg = sub_config(
        episodes=1,
        subtask_reverse_policy_role="reset",
        subtask_reset_prompts=ENTRIES,
        subtask_grasp_assist={
            "enabled": True,
            "grasp_source": "auto",
            "handoff": "holding_fact",
            "holding_band": [f"{pink}|0.15|0.4"],
            "grasp_height": [f"{pink}|{Z_GRASP}"],
        },
    )
    gate = ag.AutoGraspGate(
        AssistGate([]), rig_.port, cam, cfg.subtask_grasp_assist, limits=rig_.limits
    )
    r = build_run(
        tmp_path,
        cfg=cfg,
        fence=fence,
        gate=gate,
        grasp_assist=ga.AssistRig(rig_.port, rig_.limits),
        plans=[st.plan_answer(FORWARD), st.plan_answer(REVERSE)],
        checks=st.checks(True, True, True, True),
        policy_factory=lambda clock, trace, power: AssistPolicy(
            clock, trace=trace, power=power
        ),
    )
    r.policy.robot = r.robot
    share_gate(r, rig_)
    rig_.port.stop_probe = lambda: r.orch.stop_requested()
    r.orch.run()
    moves = committed(r.orch)
    assert any(m[:2] == ("GRASP_ASSIST", "WAIT_HUMAN") for m in moves)
    assert not any(m[1] == "RESET_ACTIVE" and m[0] == "GRASP_ASSIST" for m in moves)
    # The arm was held, never homed, and the raise keys were the only motion.
    assert not any(route in ("jointreset", "startimp") for route, _ in rig_.env.posts)
    assert [k for k in keys_of(gate)] == ["z+"] * ag.FIND_UP_MAX + ["abort"]


def test_without_yaw_enabled_the_gate_grasps_as_the_gripper_stands():
    r = rig(heading_deg=60.0, config_kw={"yaw_enabled": False})
    gate = gate_for(r)
    (kind, _), _ = drive(r, gate)
    assert kind == "handback"
    assert not {"r+", "r-"} & set(keys_of(gate))
    assert any(x.get("why") == "yaw_not_enabled" for x in gate.records)


# --- the auto source has its own switch -------------------------------------------------------------


def test_enabling_the_holding_fact_handoff_does_not_enable_the_auto_source(monkeypatch):
    monkeypatch.setattr(aeri, "UNENABLED_GRASP_SOURCES", ("auto",))
    monkeypatch.setattr(aeri, "UNENABLED_HANDOFFS", ())
    # A person's takeover may hand over on the measured fact...
    ga.GraspAssistConfig(
        enabled=True, handoff="holding_fact", holding_band=(f"{OBJECT}|0.15|0.4",)
    )
    # ... the automatic grasp is still refused, in the config and in the job.
    with pytest.raises(ValueError, match="grasp_source_not_enabled"):
        assist_cfg()
    with pytest.raises(ValueError, match="grasp_source_not_enabled"):
        aeri.JobSubtaskGraspAssist(
            grasp_source="auto",
            handoff="holding_fact",
            grasp_height=[f"{OBJECT}|0.12"],
        )
    assert aeri.UNENABLED_GRASP_SOURCES == ("auto",)


def test_the_committed_defaults_enable_auto_and_holding_fact():
    # RELAXED[2026-11-19] (user: "auto 和 holding_fact 要你自动推进和解锁"; was:
    # ('auto',) ('holding_fact',)). The switches stay: the monkeypatched tests above
    # show each still refuses on its own when listed.
    # A fresh interpreter: the committed constants, not this module's fixtures.
    import subprocess
    import sys

    code = (
        "from levi.domain import aeri; "
        "print(aeri.UNENABLED_GRASP_SOURCES, aeri.UNENABLED_HANDOFFS)"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "() ()"


def test_the_job_checks_grasp_heights_with_the_same_parser_as_the_takeover():
    part = aeri.JobSubtaskGraspAssist
    for bad in ([f"{OBJECT}|0.1", f"{OBJECT}|0.2"], [f"{OBJECT}|0"], ["pink plate|0"]):
        with pytest.raises(ValueError, match="grasp_height"):
            part(grasp_source="auto", handoff="holding_fact", grasp_height=bad)
    assert aeri.parse_grasp_heights([f"{OBJECT}|0.12"]) == {OBJECT: 0.12}
    with pytest.raises(ValueError):
        aeri.parse_grasp_heights(["a b|1"])


def test_a_grasp_height_below_the_floor_is_refused_when_the_run_is_built(tmp_path):
    from types import SimpleNamespace as NS

    from levi.automatic import fr3_run

    r = rig(config_kw={"grasp_height": (f"{OBJECT}|0.04",)})
    parts = {"grasp_assist": ga.AssistRig(r.port, r.limits)}
    vlm = {
        "base_url": "http://127.0.0.1:8100",
        "model": "m",
        "timeout_s": 60,
        "extra_body": {},
    }
    with pytest.raises(ValueError, match="below the binding's min_z"):
        fr3_run.auto_grasp_gate(
            NS(subtask_grasp_assist=r.cfg),
            object(),
            parts,
            vlm,
            tmp_path,
            lambda i: b"",
        )


def test_the_locator_coordinates_are_a_job_setting_with_the_grid_as_default(tmp_path):
    from types import SimpleNamespace as NS

    from levi.automatic import fr3_run

    # The 11-18 offline replay: Qwen3-VL answered on the 0..1000 grid in 11 of 11 real
    # wrist frames even when asked for pixels; a pixel default made every reply "bad".
    assert assist_cfg().locator_coords == "norm1000"
    assert "locator_coords" not in assist_cfg().describe()
    px = assist_cfg(locator_coords="pixel")
    assert px.describe()["locator_coords"] == "pixel"
    with pytest.raises(ValueError, match="locator_coords"):
        assist_cfg(locator_coords="metres")
    with pytest.raises(ValueError):
        aeri.JobSubtaskGraspAssist(locator_coords="metres")
    assert (
        aeri.JobSubtaskGraspAssist(locator_coords="norm1000").locator_coords
        == "norm1000"
    )
    r = rig(config_kw={"locator_coords": "norm1000"})
    parts = {"grasp_assist": ga.AssistRig(r.port, r.limits)}
    vlm = {
        "base_url": "http://127.0.0.1:8100",
        "model": "m",
        "timeout_s": 60,
        "extra_body": {},
    }
    gate = fr3_run.auto_grasp_gate(
        NS(subtask_grasp_assist=r.cfg), object(), parts, vlm, tmp_path, lambda i: b""
    )
    assert gate.locator.coords == "norm1000"


# --- a camera that is not on the tool axis ---------------------------------------------------------


@pytest.mark.parametrize("mount", ["flip", "turn"])
@pytest.mark.parametrize("heading", [40.0, 80.0])
def test_the_object_is_grasped_in_place_with_the_camera_off_the_tool_axis(
    mount, heading
):
    # The camera is 4 cm ahead of and 1 cm beside the tool axis; the matrix turns with
    # every yaw key (it is measured again after the turn) and the target pixel is the
    # fingertip midpoint measured at the grasp height, scaled to the height of each look.
    r = rig(mount=mount, heading_deg=heading, offset=(0.04, 0.01), max_steps=200)
    aim = r.cam.fingertip_pixel(Z_GRASP)
    gate = gate_for(r, config=ag.AutoGraspConfig(aim_px=aim))
    (kind, _), _ = drive(r, gate)
    assert kind == "handback", gate.records[-3:]
    settle(r)
    x, y, _ = tool_xyz(r)
    assert math.dist((x, y), r.cam.obj) < 0.02
    # The matrix was measured again after the yaw (two more calibration keys).
    assert sum(1 for x in gate.records if x["kind"] == "calibration") == 2
    assert abs(90.0 - (r.cam.axis_deg() % 180.0)) <= 10.0 + 1e-6


def test_the_image_centre_as_aim_misses_a_camera_that_is_off_the_axis():
    # The default aim (the image centre) is right only for a camera on the tool axis: with
    # this one the gripper closes where the camera looks, 4 cm from the object. This is why
    # the real aim is a measured job setting.
    r = rig(heading_deg=40.0, offset=(0.04, 0.0), max_steps=200)
    gate = gate_for(r)
    (kind, _), _ = drive(r, gate)
    settle(r)
    assert kind != "handback" or math.dist(tool_xyz(r)[:2], r.cam.obj) > 0.02


def test_the_aim_pixel_follows_the_height_of_the_look():
    r = rig(offset=(0.04, 0.0))
    gate = gate_for(r, config=ag.AutoGraspConfig(aim_px=(320.0, 400.0)))
    gate._z_grasp = 0.12
    look = ag.Look(
        ag.Detection(True, center=(0, 0)), (0.5, 0.0, 0.24), {"shape": (480, 640)}
    )
    # 160 px below the centre at the grasp height is 80 px at twice that height.
    assert gate._aim(look) == (320.0, 240.0 + 160.0 * 0.5)
    look.xyz = (0.5, 0.0, 0.12)
    assert gate._aim(look) == (320.0, 400.0)
    assert gate_for(r)._aim(look) == (320.0, 240.0)  # no grasp_pixel: the centre


def test_the_grasp_pixel_is_a_checked_job_setting_given_to_the_gate(tmp_path):
    from types import SimpleNamespace as NS

    from levi.automatic import fr3_run

    assert (
        assist_cfg().grasp_pixel is None
        and "grasp_pixel" not in assist_cfg().describe()
    )
    cfg = assist_cfg(grasp_pixel=[330.0, 380.0])
    assert cfg.grasp_pixel == (330.0, 380.0) and cfg.describe()["grasp_pixel"] == [
        330.0,
        380.0,
    ]
    for bad in (
        [1.0],
        [1.0, 2.0, 3.0],
        [-1.0, 2.0],
        [float("nan"), 2.0],
        "ab",
        [True, 1],
    ):
        with pytest.raises(ValueError, match="grasp_pixel"):
            assist_cfg(grasp_pixel=bad)
    part = aeri.JobSubtaskGraspAssist
    assert part(grasp_pixel=[330, 380]).grasp_pixel == [330.0, 380.0]
    for bad in ([1], [1, 2, 3], [-1, 2], [20000, 1]):
        with pytest.raises(ValueError):
            part(grasp_pixel=bad)
    r = rig(config_kw={"grasp_pixel": (330.0, 380.0)})
    parts = {"grasp_assist": ga.AssistRig(r.port, r.limits)}
    vlm = {
        "base_url": "http://127.0.0.1:8100",
        "model": "m",
        "timeout_s": 60,
        "extra_body": {},
    }
    gate = fr3_run.auto_grasp_gate(
        NS(subtask_grasp_assist=r.cfg), object(), parts, vlm, tmp_path, lambda i: b""
    )
    assert gate.config.aim_px == (330.0, 380.0)
