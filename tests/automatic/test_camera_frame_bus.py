"""The LEVI side of the camera frame bus (CAMERA-FRAME-BUS.md): openpi's
optional ``image_meta`` through the bridge, the frozen-camera wait
(``camera_stale``), the recorder's frame columns, the VLM's JPEG cache and
image cap, and the optional binding keys. Fakes only: no camera, no robot,
no socket."""

import csv
import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
import subtask_rig
from aeri_harness import build, committed, config, fake, results
from test_aeri_orchestrator import no_network  # noqa: F401
from test_aeri_recorder import FOLDERS, GROUP, build_run

from levi.automatic import camera_meta as CM
from levi.automatic import fr3_bridge as B
from levi.automatic import fr3_media as M
from levi.automatic import fr3_run as R
from levi.automatic import state_machine as sm
from levi.automatic import subtask_loop as st
from levi.automatic import subtask_vlm as V
from levi.automatic.adapters import events as ev
from levi.domain import aeri

OLD_OBSERVE_KEYS = {
    "observed_ns",
    "pose",
    "frames",
    "state_fresh",
    "images_bgr",
    "robot_state",
    "gripper_measurement",
    "raw",
}


def raw_meta(seq_side=7, seq_wrist=9):
    return {
        "view1": {
            "seq": seq_side,
            "t_capture_ns": 123,
            "t_arrival_ns": 2000,
            "age_ms": 4.5,
        },
        "hand": {
            "seq": seq_wrist,
            "t_capture_ns": None,
            "t_arrival_ns": 2100,
            "age_ms": 6.0,
        },
    }


class StaleFrameError(RuntimeError):
    """Stands in for openpi's fr3_env.StaleFrameError (matched by name)."""


class Env:
    def __init__(self, meta=None, error=None):
        self.meta, self.error = meta, error
        self.history = {}

    def _post(self, route, payload=None):
        return {"ok": True}

    def get_observation(self):
        if self.error is not None:
            raise self.error
        image = np.zeros((4, 4, 3), dtype=np.uint8)
        image.flags.writeable = False  # openpi shares read-only frames now
        found = {
            "image": {"view1": image, "hand": image},
            "robot_state": {
                "cartesian_position": [0.4, 0.0, 0.2, 0.0, 0.0, 0.0, 1.0],
                "gripper_position": 1.0,
            },
        }
        if self.meta is not None:
            found["image_meta"] = self.meta
        return found


def bridge(env, **kw):
    guard = NS(tripped=None, check=lambda force=False: NS(status="ok", reasons=[]))
    return B.Robot(
        env, guard, NS(), NS(), fake.FakeClock(), sm.MotionFence(), tolerance_mm=5, **kw
    )


# --- the bridge ------------------------------------------------------------------------


def test_the_bridge_passes_image_meta_through_under_the_aeri_views():
    found = bridge(Env(meta=raw_meta())).observe()
    assert set(found) == OLD_OBSERVE_KEYS | {"image_meta"}
    assert found["image_meta"]["side"] == {
        "seq": 7,
        "t_capture_ns": 123,
        "t_arrival_ns": 2000,
        "age_ms": 4.5,
    }
    assert found["image_meta"]["wrist"]["seq"] == 9
    assert found["image_meta"]["wrist"]["t_capture_ns"] is None
    # The fingerprints stay as they were.
    assert set(found["frames"]) == {"side", "wrist"}


def test_without_image_meta_the_observation_is_the_old_one():
    found = bridge(Env()).observe()
    assert set(found) == OLD_OBSERVE_KEYS
    assert bridge(Env()).recent_frames("side", 2) == []  # no frame_history


def test_a_stale_frame_error_becomes_camera_stale():
    robot = bridge(Env(error=StaleFrameError("camera 'view1': seq=4 is 900 ms old")))
    with pytest.raises(CM.CameraStale, match="900 ms"):
        robot.observe()
    # Any other failure is unchanged.
    with pytest.raises(OSError):
        bridge(Env(error=OSError("usb"))).observe()


def test_a_frame_age_limit_needs_image_meta():
    with pytest.raises(ValueError, match="cannot enforce"):
        bridge(Env(), require_image_meta=True).observe()
    assert (
        "image_meta" in bridge(Env(meta=raw_meta()), require_image_meta=True).observe()
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"view1": raw_meta()["view1"]},  # a camera missing
        {**raw_meta(), "view1": {**raw_meta()["view1"], "seq": -1}},
        {**raw_meta(), "view1": {**raw_meta()["view1"], "age_ms": float("nan")}},
        {**raw_meta(), "hand": {**raw_meta()["hand"], "t_arrival_ns": None}},
    ],
)
def test_malformed_image_meta_is_refused(bad):
    with pytest.raises((ValueError, TypeError)):
        bridge(Env(meta=bad)).observe()


def test_recent_frames_map_the_view_to_the_openpi_camera():
    env = Env(meta=raw_meta())
    asked = []

    def frame_history(name, since_ns=None, n=None):
        asked.append((name, n))
        return [
            NS(seq=s, t_capture_ns=10 + s, t_arrival_ns=20 + s, image="px")
            for s in (5, 6)
        ]

    env.frame_history = frame_history
    found = bridge(env).recent_frames("wrist", 2)
    assert asked == [("hand", 2)]
    assert [f["seq"] for f in found] == [5, 6]
    assert found[0] == {"seq": 5, "t_capture_ns": 15, "t_arrival_ns": 25, "image": "px"}


def test_the_stale_step_limit_is_checked():
    assert bridge(Env()).camera_stale_steps == CM.DEFAULT_STALE_STEPS
    assert bridge(Env(), camera_stale_steps=5).camera_stale_steps == 5
    for bad in (1, 0, True, 2.5, 101):
        with pytest.raises(ValueError):
            bridge(Env(), camera_stale_steps=bad)


# --- the control loop: frozen cameras wait for a person, nothing moves ------------------


def install_meta(robot, get_ctx, *, role=None, after=2, raise_stale=False):
    """``robot.observe`` adds ``image_meta``; inside an episode of ``role``
    (any role: None) the seq stops advancing after ``after`` observations
    (or openpi's age check fails: ``raise_stale``)."""
    original = robot.observe
    state = {"seq": 0, "episode": None, "n": 0, "log": []}

    def homes():
        return sum(1 for m in robot.motions if m[0] == "home")

    def observe():
        ctx = get_ctx()
        episode = ctx.episode_id if ctx is not None else None
        if episode != state["episode"]:
            state["episode"], state["n"] = episode, 0
        state["n"] += 1
        hit = (
            ctx is not None
            and (role is None or ctx.role == role)
            and state["n"] > after
        )
        state["log"].append((episode, robot.step_calls, homes()))
        if hit and raise_stale:
            raise CM.CameraStale("camera 'view1': newest frame seq=9 is 900.0 ms old")
        value = original()
        if not hit:
            state["seq"] += 1
        value["image_meta"] = {
            view: {
                "seq": state["seq"],
                "t_capture_ns": 1000 + state["seq"],
                "t_arrival_ns": 2000 + state["seq"],
                "age_ms": 5.0,
            }
            for view in ("side", "wrist")
        }
        return value

    robot.observe = observe
    state["homes"] = homes
    return state


def nothing_moved_after_the_last_observation(robot, state):
    _, steps, homes = state["log"][-1]
    assert robot.step_calls == steps
    assert state["homes"]() == homes


def note_codes(orch):
    return {e.note.code for e in orch.journal.events if e.record == "note"}


def test_a_frame_age_error_ends_a_forward_episode_and_waits_without_a_home(
    tmp_path,
):
    """openpi's StaleFrameError (opt-in max_frame_age_ms) still stops."""
    raise_stale = True
    r = build(tmp_path, cfg=config(episodes=1))
    state = install_meta(r.robot, lambda: r.orch.ctx, raise_stale=raise_stale)
    assert r.orch.run() == "WAIT_HUMAN"
    path = committed(r.orch)
    assert ("FORWARD_ACTIVE", "FORWARD_STOPPING", "camera_stale") in path
    assert path[-1] == ("ROBOT_HOME", "WAIT_HUMAN", "camera_stale")
    (result,) = results(r.orch)
    assert result.stop_reason == "camera_stale"
    assert result.goal_verification == "unavailable"
    assert result.task_outcome != "success"
    nothing_moved_after_the_last_observation(r.robot, state)
    assert state["homes"]() == 0
    # The age error stops at the first such observation.
    assert r.robot.step_calls == 2
    assert "camera_stale" in note_codes(r.orch)
    assert sm.check_journal(r.orch.journal.events) == []
    motions = len(r.robot.motions)
    assert r.orch.run() == "WAIT_HUMAN" and len(r.robot.motions) == motions


def test_a_repeated_seq_is_recorded_once_and_never_stops_the_episode(tmp_path):
    """User 2026-10-18: a dropped frame is normal under CPU/GPU load; the
    same seq on K steps is a note (camera_seq_repeat), not a stop."""
    plain = build(tmp_path / "plain", cfg=config(episodes=1))
    assert plain.orch.run() == "COMPLETED"
    r = build(tmp_path / "frozen", cfg=config(episodes=1))
    install_meta(r.robot, lambda: r.orch.ctx)
    assert r.orch.run() == "COMPLETED"
    assert "camera_stale" not in {c[2] for c in committed(r.orch)}
    assert r.robot.step_calls == plain.robot.step_calls
    assert [c for c in r.orch.notes if c[0] == CM.SEQ_REPEAT] == [
        (CM.SEQ_REPEAT, "step 3: side camera seq 2 unchanged for 3 steps (age 5 ms)")
    ]
    assert "camera_stale" not in note_codes(r.orch)
    assert sm.check_journal(r.orch.journal.events) == []


def test_the_seq_repeat_limit_comes_from_the_robot(tmp_path):
    r = build(tmp_path, cfg=config(episodes=1))
    r.robot.camera_stale_steps = 5
    install_meta(r.robot, lambda: r.orch.ctx)
    assert r.orch.run() == "COMPLETED"
    # seq 2 on obs 2..6: the fifth such observation is the first repeat.
    (note,) = [c for c in r.orch.notes if c[0] == CM.SEQ_REPEAT]
    assert note[1].startswith("step 5: ") and "for 5 steps" in note[1]


def test_a_repeated_seq_in_a_subtask_is_recorded_and_the_loop_goes_on(tmp_path):
    plain = subtask_rig.build(tmp_path / "plain")
    expected = plain.orch.run()
    r = subtask_rig.build(tmp_path / "frozen")
    install_meta(r.robot, lambda: r.orch.ctx, role="forward")
    assert r.orch.run() == expected
    assert "camera_stale" not in {c[2] for c in subtask_rig.committed(r.orch)}
    assert any(c[0] == CM.SEQ_REPEAT for c in r.orch.notes)


def test_without_image_meta_a_still_frame_counter_does_not_stop_the_episode(tmp_path):
    """Old openpi: no seq, so nothing new is judged (the run completes)."""
    r = build(tmp_path, cfg=config(episodes=1))
    assert r.orch.run() == "COMPLETED"
    assert "camera_stale" not in {c[2] for c in committed(r.orch)}


def test_frozen_cameras_in_a_reset_episode_wait_without_the_resets_home(tmp_path):
    clock = fake.FakeClock()
    scene = ev.FakeSceneAssessor(
        [{"decision": "reset_required"}], clock, "r-sm", default={"decision": "ready"}
    )
    r = build(tmp_path, cfg=config(episodes=1), clock=clock, scene=scene)
    state = install_meta(r.robot, lambda: r.orch.ctx, role="reset", raise_stale=True)
    assert r.orch.run() == "WAIT_HUMAN"
    path = committed(r.orch)
    assert ("RESET_ACTIVE", "RESET_VERIFY", "camera_stale") in path
    assert ("RESET_VERIFY", "RESET_FINALIZE", "camera_stale") in path
    assert path[-1] == ("RESET_FINALIZE", "WAIT_HUMAN", "camera_stale")
    (result,) = results(r.orch)
    assert result.stop_reason == "camera_stale"
    assert result.robot_home == "not_attempted"
    nothing_moved_after_the_last_observation(r.robot, state)
    assert state["homes"]() == 0
    assert sm.check_journal(r.orch.journal.events) == []


def test_frozen_cameras_in_a_forward_subtask_wait_without_a_home(tmp_path):
    r = subtask_rig.build(tmp_path)
    state = install_meta(r.robot, lambda: r.orch.ctx, role="forward", raise_stale=True)
    assert r.orch.run() == "WAIT_HUMAN"
    path = subtask_rig.committed(r.orch)
    assert path[-1] == ("ROBOT_HOME", "WAIT_HUMAN", "camera_stale")
    assert ("home",) not in r.trace
    nothing_moved_after_the_last_observation(r.robot, state)
    # No VLM check was asked on the frozen frames.
    assert not subtask_rig.notes(r.orch, st.NOTE_CHECK)
    assert sm.check_journal(r.orch.journal.events) == []


def test_frozen_cameras_in_a_reverse_subtask_wait_without_a_home(tmp_path):
    r = subtask_rig.build(tmp_path)
    state = install_meta(r.robot, lambda: r.orch.ctx, role="reset", raise_stale=True)
    assert r.orch.run() == "WAIT_HUMAN"
    path = subtask_rig.committed(r.orch)
    assert path[-1] == ("RESET_FINALIZE", "WAIT_HUMAN", "camera_stale")
    # Only the forward loop's own home ran.
    assert r.trace.count(("home",)) == 1
    nothing_moved_after_the_last_observation(r.robot, state)
    result = [
        e.episode_result
        for e in r.orch.journal.events
        if e.record == "committed" and e.episode_result is not None
    ][-1]
    assert result.stop_reason == "camera_stale"
    assert result.robot_home == "not_attempted"
    assert sm.check_journal(r.orch.journal.events) == []


def test_camera_stale_is_registered_everywhere_a_reason_must_be():
    assert "camera_stale" in aeri.TRANSITION_REASONS
    assert "camera_stale" in aeri.STOP_REASONS
    assert "camera_stale" in sm.FORWARD_STOPS and "camera_stale" in sm.RESET_STOPS
    from levi.automatic import console as C
    from levi.automatic import console_text, modes

    happened, todo = C.WAIT_REASONS["camera_stale"]
    for lang in ("zh", "en"):
        assert console_text.TEXT[lang][happened] and console_text.TEXT[lang][todo]
    assert "camera_stale" in C.CONFIRM_REASONS
    assert "arbitration:after_reset:camera_stale" in modes.MODE_MATRIX
    # The reset's home rule never accepts it: a frozen camera never homes.
    with pytest.raises(sm.TransitionRefused):
        sm.check_transition(
            "RESET_FINALIZE",
            "WAIT_HUMAN",
            reason="camera_stale",
            principal_kind="orchestrator",
            command_id=None,
            action_kind="home",
            non_idempotent=True,
            step=1,
            episode_role="reset",
        )


# --- the recorder ------------------------------------------------------------------------


OLD_FRAMES_HEADER = [
    "timestamp_sec",
    "frame_index",
    "success_flag",
    "wrist_video",
    "side_video",
]


def test_frames_csv_gets_the_frame_columns_after_the_old_ones(tmp_path):
    class Video:
        def __init__(self, *a):
            pass

        def isOpened(self):
            return True

        def write(self, image):
            pass

        def release(self):
            pass

    cv2 = NS(VideoWriter=Video, VideoWriter_fourcc=lambda *a: 1)
    rotation = NS(from_euler=lambda *a: NS(as_quat=lambda: np.array([0, 0, 0, 1.0])))
    media = M.Media(
        cv2, rotation, None, NS(new_buffer=lambda hz: NS(add=lambda *a: None)), 10
    )
    folder = tmp_path / "demo_0001"
    folder.mkdir()
    media.open(folder)
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    obs = {
        "images_bgr": {"side": image, "wrist": image},
        "pose": [0.4, 0, 0.2, 0, 0, 0, 1.0],
        "robot_state": {"gripper_position": 1.0, "joint_positions": [0.0] * 7},
        "raw": {"image": {}},
    }
    meta = CM.view_meta(raw_meta())
    for value in ({**obs, "image_meta": meta}, obs):
        media.frame(folder, 0, value)
        media.commit(folder, "ep")
    media.finish(folder, 2)
    rows = list(csv.reader((folder / "frames.csv").open()))
    assert rows[0][:5] == OLD_FRAMES_HEADER
    assert rows[0][5:] == list(CM.CSV_COLUMNS)
    assert rows[1][3:5] == ["wrist_camera.mp4", "side_camera.mp4"]
    assert rows[1][5:] == ["7", "123", "4.5", "9", "", "6.0"]
    assert rows[2][5:] == [""] * 6  # no image_meta: empty cells


def test_aeri_steps_csv_gets_the_frame_columns_after_the_old_ones(tmp_path):
    r = build_run(tmp_path)
    install_meta(r.robot, lambda: r.orch.ctx, after=1000)  # never frozen
    r.orch.run()
    path = Path(tmp_path) / GROUP / FOLDERS["forward"] / "demo_0001" / "aeri_steps.csv"
    rows = list(csv.reader(path.open()))
    assert rows[0][:5] == ["step", "observed_ns", "executed", "detail", "pose"]
    assert rows[0][5:] == list(CM.CSV_COLUMNS)
    seqs = [int(row[5]) for row in rows[1:]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(row[5] == row[8] for row in rows[1:])  # side and wrist seq
    json.loads(rows[1][4])  # the pose column is where it was


# --- the VLM: one JPEG per frame, frame identity in the evidence, the image cap ------


def vlm(observe, *, encoded, **kw):
    sent = []

    def post(url, body, timeout_s):
        sent.append(body)
        return {"choices": [{"message": {"content": st.inventory_answer([])}}]}

    def encode(pixels):
        encoded.append(pixels)
        return b"jpeg-" + str(pixels).encode()

    client = V.VllmSubtaskVlm(
        "http://127.0.0.1:8100", "m", observe=observe, encode=encode, post=post, **kw
    )
    return client, sent


def images_of(body):
    return [c for c in body["messages"][0]["content"] if c["type"] == "image_url"]


def test_a_jpeg_is_encoded_once_per_camera_frame():
    meta = CM.view_meta(raw_meta())
    encoded = []
    client, _ = vlm(
        lambda: {"images_bgr": {"side": "s", "wrist": "w"}, "image_meta": meta},
        encoded=encoded,
    )
    first = client.capture("check")
    client.capture("check")
    assert len(encoded) == 2  # the second capture saw the same two frames
    assert first[0]["seq"] == 7 and first[0]["t_capture_ns"] == 123
    assert first[1]["seq"] == 9 and first[1]["t_capture_ns"] is None
    assert st.frames_brief(first)[0] == {
        "ref": first[0]["ref"],
        "sha256": first[0]["sha256"],
        "seq": 7,
        "t_capture_ns": 123,
    }


def test_without_image_meta_every_capture_encodes_and_carries_no_seq():
    encoded = []
    client, sent = vlm(
        lambda: {"images_bgr": {"side": "s", "wrist": "w"}}, encoded=encoded
    )
    client.capture("a")
    frames = client.capture("b")
    assert len(encoded) == 4
    assert all("seq" not in f for f in frames)
    assert st.frames_brief(frames) == [
        {"ref": f["ref"], "sha256": f["sha256"]} for f in frames
    ]
    reply = client.inventory(frames=frames)
    assert reply.image_meta is None
    assert len(images_of(sent[0])) == 2


def test_the_request_evidence_names_each_frame():
    meta = CM.view_meta(raw_meta())
    client, _ = vlm(
        lambda: {"images_bgr": {"side": "s", "wrist": "w"}, "image_meta": meta},
        encoded=[],
    )
    frames = client.capture("inventory")
    reply = client.inventory(frames=frames)
    assert reply.image_meta == [
        {"ref": frames[0]["ref"], "view": "side", "seq": 7, "t_capture_ns": 123},
        {"ref": frames[1]["ref"], "view": "wrist", "seq": 9, "t_capture_ns": None},
    ]


def history(view, n):
    return [
        {
            "seq": s,
            "t_capture_ns": 100 + s,
            "t_arrival_ns": 200 + s,
            "image": f"{view}{s}",
        }
        for s in (5, 6, 7)
    ][-n:]


def test_the_image_cap_keeps_the_current_frames_first():
    meta = CM.view_meta(raw_meta(seq_side=7, seq_wrist=7))
    observe = lambda: {"images_bgr": {"side": "s", "wrist": "w"}, "image_meta": meta}
    # Earlier frames asked for, but the default cap of 2 leaves no room.
    client, sent = vlm(observe, encoded=[], history_frames=1, recent=history)
    client.inventory(frames=client.capture("inventory"))
    assert len(images_of(sent[0])) == 2
    # Room for them: one earlier frame per view, older than the current one.
    client, sent = vlm(
        observe, encoded=[], max_images=4, history_frames=1, recent=history
    )
    frames = client.capture("inventory")
    reply = client.inventory(frames=frames)
    assert len(images_of(sent[0])) == 4
    assert reply.image_refs[:2] == [f["ref"] for f in frames]
    assert [m["seq"] for m in reply.image_meta] == [7, 7, 6, 6]
    text = sent[0]["messages"][0]["content"][-1]["text"]
    assert "Images 3 to 4 are earlier frames" in text
    assert reply.prompt_sha256 == st.prompt_sha256(text)


def test_image_settings_are_bounded():
    assert CM.image_settings({}) == {"max_images": 2, "history_frames": 0}
    for bad in (
        {"max_images": 0},
        {"max_images": 9},
        {"history_frames": 5},
        {"max_images": True},
    ):
        with pytest.raises(ValueError):
            CM.image_settings(bad)


def test_the_file_provider_cache_encodes_one_jpeg_per_frame():
    encoded = []
    cache = CM.JpegCache(lambda p: encoded.append(p) or b"x")
    item = {"seq": 3, "t_arrival_ns": 9}
    cache.get("side", "p", item)
    cache.get("side", "p", item)
    cache.get("wrist", "p", item)
    cache.get("side", "p", None)
    cache.get("side", "p", None)
    assert len(encoded) == 4 == cache.encoded
    assert CM.frame_fields({"side": {"seq": 3, "t_capture_ns": 5}}, "side") == {
        "seq": 3,
        "t_capture_ns": 5,
    }
    assert CM.frame_fields(None, "side") == {}


# --- the seq watch ---------------------------------------------------------------------


def test_the_seq_watch_counts_consecutive_steps_per_view():
    watch = CM.SeqWatch(3)
    meta = lambda s, w: {
        "side": {"seq": s, "age_ms": 1.0},
        "wrist": {"seq": w, "age_ms": 1.0},
    }
    assert watch.update(None) is None
    assert watch.update(meta(1, 1)) is None
    assert watch.update(meta(1, 2)) is None
    assert watch.update(meta(2, 3)) is None  # side advanced: its count restarts
    assert watch.update(meta(2, 4)) is None
    assert "side camera seq 2 unchanged for 3 steps" in watch.update(meta(2, 5))


# --- the binding -------------------------------------------------------------------------


def test_camera_kwargs_are_unchanged_without_a_frame_age_limit():
    found = {"view1": {"serial_number": "1"}, "hand": {"serial_number": "2"}}
    client = NS(build_camera_kwargs=lambda args: found)
    assert R.camera_kwargs(client, None, {}) is found
    limited = R.camera_kwargs(client, None, {"max_frame_age_ms": 150})
    assert limited == {
        "view1": {"serial_number": "1", "max_frame_age_ms": 150.0},
        "hand": {"serial_number": "2", "max_frame_age_ms": 150.0},
    }
    assert "max_frame_age_ms" not in found["view1"]  # openpi's dict untouched
    with pytest.raises(ValueError):
        R.camera_kwargs(
            NS(build_camera_kwargs=lambda a: {"view1": "1"}),
            None,
            {"max_frame_age_ms": 1},
        )


def test_the_binding_accepts_the_optional_camera_keys(tmp_path, monkeypatch):
    from test_fr3_bridge import rig

    r = rig(tmp_path, monkeypatch)
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    R.load_binding(path, r.job)  # without them, as before
    good = {**r.binding["safety"], "max_frame_age_ms": 150, "camera_stale_steps": 4}
    path.write_text(json.dumps({**r.binding, "safety": good}))
    value, _ = R.load_binding(path, r.job)
    assert value["safety"]["max_frame_age_ms"] == 150
    for key, bad in (
        ("max_frame_age_ms", 0),
        ("max_frame_age_ms", True),
        ("max_frame_age_ms", 20000),
        ("camera_stale_steps", 1),
        ("camera_stale_steps", 3.0),
    ):
        path.write_text(
            json.dumps({**r.binding, "safety": {**r.binding["safety"], key: bad}})
        )
        with pytest.raises(ValueError):
            R.load_binding(path, r.job)


def test_the_camera_keys_stay_off_the_openpi_client_args(tmp_path, monkeypatch):
    from test_fr3_bridge import rig

    r = rig(tmp_path, monkeypatch)
    r.binding["safety"].update(camera_stale_steps=4)
    seen = {}
    client = r.modules[0]
    client.Args = lambda: seen.setdefault("args", NS())
    R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    assert not hasattr(seen["args"], "camera_stale_steps")
    assert not hasattr(seen["args"], "max_frame_age_ms")
