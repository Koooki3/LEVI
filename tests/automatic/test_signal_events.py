"""Real observation wrapper and encoded evidence under adversarial identities."""

import hashlib
import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

from levi.automatic.adapters.signal_events import Binding, CachedObserver, SignalEvents
from levi.domain import aeri
from levi.events.signal_profiles import ChannelSpec, SignalProfile
from levi.events.stream import StreamConfig


class Clock:
    domain = "host-mono:test"
    ns = 0

    def now(self):
        return self.ns


class Robot:
    def __init__(self, clock):
        self.clock, self.calls, self.step = clock, 0, 0
        self.gripper, self.command = 0.0, 0.0
        self.proof = True
        self.state_fresh = True
        self.image = np.full((8, 8, 3), 10, dtype=np.uint8)
        self.generation = 0
        self.adjust = lambda value: value

    def observe(self):
        self.calls += 1
        started = self.clock.now()
        self.clock.ns += 1
        self.generation += 1
        observation = {
            "observed_ns": self.clock.now(),
            "pose": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, self.command],
            "robot_state": {"gripper_position": self.gripper},
            "state_fresh": self.state_fresh,
            "images_bgr": {"side": self.image},
        }
        if self.proof:
            observation["gripper_measurement"] = {
                "route": "getstate",
                "field": "gripper_pos",
                "generation": self.generation,
                "read_from_ns": started,
                "read_through_ns": self.clock.now(),
                "clock_domain": self.clock.domain,
                "value": self.gripper,
            }
        return self.adjust(observation)


def rig(tmp_path, *, kind="measured", keep=None, actor="arm_0", config=None):
    clock = Clock()
    robot = Robot(clock)
    observer = CachedObserver(robot, clock, step_of=lambda: robot.step)
    profile = SignalProfile(
        channels=[
            ChannelSpec(
                role="gripper",
                feature="observation.state" if kind == "measured" else "action",
                index=6,
                kind=kind,
                units="normalized",
                valid_range=(0, 1),
                open_level="high",
                actor_id=actor,
            )
        ]
    )
    binding = Binding(
        profile.channels[0].feature,
        6,
        ("robot_state", "gripper_position") if kind == "measured" else ("pose", 6),
        kind,
    )
    calls = []

    def save(ref, data):
        calls.append((ref, data))
        digest = hashlib.sha256(data).hexdigest()
        (tmp_path / (digest + ".jpg")).write_bytes(data)
        return {"ref": ref, "sha256": digest, "file": digest + ".jpg"}

    events = SignalEvents(
        observer,
        profile,
        [binding],
        run_id="run",
        clock=clock,
        evidence_root=tmp_path,
        keep_frame=keep or save,
        config=config,
    )
    sid = events.open("run.forward.0000", "forward")
    return SimpleNamespace(
        clock=clock, robot=robot, observer=observer, events=events, sid=sid, saves=calls
    )


def poll(r, step, *, observed=True, max_events=128):
    r.robot.step = step
    r.clock.ns += 100_000_000
    if observed:
        r.observer.observe()
    return r.events.poll(r.sid, through_step=step, max_events=max_events)


def crossing(r):
    assert poll(r, 0) == []
    r.robot.gripper = r.robot.command = 1.0
    return poll(r, 1)


def test_observe_once_poll_never_calls_hardware_and_retained_jpeg_is_real(tmp_path):
    r = rig(tmp_path)
    out = crossing(r)
    assert r.robot.calls == 2
    assert len(out) == 1
    event = aeri.parse(out[0], "event")
    assert event.status == "candidate"
    assert event.window_steps == [0, 1]
    assert event.signal_refs[0].kind == "measured"
    assert event.detector.version == r.events.stream[3].profile_sha256
    data = r.saves[0][1]
    assert event.evidence_refs[0].sha256 == hashlib.sha256(data).hexdigest()
    assert (
        event.evidence_refs[0].sha256
        != hashlib.sha256(r.robot.image.tobytes()).hexdigest()
    )
    assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")
    import cv2

    assert (
        cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR).shape
        == r.robot.image.shape
    )
    assert poll(r, 1, observed=False) == []
    assert r.robot.calls == 2 and len(r.saves) == 1


def test_command_source_cannot_imitate_measured_source(tmp_path):
    r = rig(tmp_path)
    poll(r, 0)
    r.robot.command = 1.0
    assert poll(r, 1) == []
    r.robot.proof = False
    r.robot.gripper = 1.0
    assert poll(r, 2) == []
    assert "measured_gripper_unproven" in r.events.status["reasons"]
    r.robot.proof = True
    assert poll(r, 3) == []
    commanded = rig(tmp_path, kind="commanded")
    event = json.loads(crossing(commanded)[0])
    assert event["event_type"] == "gripper_command_open"
    assert event["signal_refs"][0]["kind"] == "commanded"


@pytest.mark.parametrize(
    "fault", ["missing", "hash", "old", "symlink", "outside", "skip", "fifo"]
)
def test_corrupt_missing_old_or_unsafe_encoded_evidence_fails_closed(tmp_path, fault):
    r = rig(tmp_path)

    def bad(ref, data):
        digest = hashlib.sha256(data).hexdigest()
        path = tmp_path / "bad.jpg"
        if fault == "fifo":
            os.mkfifo(path)
        elif fault == "symlink":
            target = tmp_path / "target.jpg"
            target.write_bytes(data)
            path.symlink_to(target)
        elif fault != "missing":
            path.write_bytes(b"old" if fault == "old" else data)
        return {
            "ref": ref,
            "sha256": "0" * 64 if fault == "hash" else digest,
            "file": "../bad.jpg" if fault == "outside" else "bad.jpg",
            "skipped": "budget" if fault == "skip" else None,
        }

    r.events.keep_frame = bad
    assert crossing(r) == []
    assert r.events.status["state"] == "degraded"
    assert any(
        reason.startswith("evidence_unavailable")
        for reason in r.events.status["reasons"]
    )


@pytest.mark.parametrize(
    "fault",
    ["replay", "other_clock", "wrong_route", "unequal", "old_read", "future_read"],
)
def test_measured_gripper_proof_cannot_replay_or_lie_about_same_read(tmp_path, fault):
    r = rig(tmp_path)
    poll(r, 0)

    def adjust(obs):
        proof = obs["gripper_measurement"]
        if fault == "replay":
            proof["generation"] = 1
        if fault == "other_clock":
            proof["clock_domain"] = "host-mono:old"
        if fault == "wrong_route":
            proof["route"] = "command"
        if fault == "unequal":
            proof["value"] = 0.0
        if fault == "old_read":
            proof["read_from_ns"] = 0
        if fault == "future_read":
            proof["read_through_ns"] = obs["observed_ns"] + 1
        return obs

    r.robot.adjust = adjust
    r.robot.gripper = 1.0
    assert poll(r, 1) == []
    assert "measured_gripper_unproven" in r.events.status["reasons"]


@pytest.mark.parametrize(
    "fault", ["future_step", "past_step", "future_time", "past_time", "stale", "clock"]
)
def test_observation_clock_step_and_age_boundary_rejects(tmp_path, fault):
    r = rig(tmp_path)
    poll(r, 0)
    r.robot.step = 1
    r.clock.ns += 100_000_000
    r.observer.observe()
    through = 0 if fault == "future_step" else 2 if fault == "past_step" else 1
    if fault == "future_time":
        r.observer.latest[-1]["observed_ns"] += 1000
    if fault == "past_time":
        r.observer.latest[-1]["observed_ns"] = 0
    if fault == "stale":
        r.clock.ns += 600_000_000
    if fault == "clock":
        r.clock.domain = "host-mono:restart"
    with pytest.raises(ValueError):
        r.events.poll(r.sid, through_step=through, max_events=128)


def test_episode_restart_cannot_reuse_frame_candidate_or_stream_identity(tmp_path):
    r = rig(tmp_path)
    event = json.loads(crossing(r)[0])
    r.events.retract(r.sid, event["event_id"])
    retracted = json.loads(r.events.poll(r.sid, through_step=1, max_events=1)[0])
    assert retracted["status"] == "retracted" and retracted["seq"] == 1
    with pytest.raises(ValueError):
        r.events.retract(r.sid, event["event_id"])
    old = r.sid
    r.events.close(old)
    with pytest.raises(ValueError):
        r.events.open("run.forward.0000", "forward")
    r.sid = r.events.open("run.reset.0001", "reset")
    assert r.observer.latest is None
    assert r.events.poll(r.sid, through_step=0, max_events=1) == []
    with pytest.raises(ValueError):
        r.events.poll(old, through_step=1, max_events=1)
    with pytest.raises(ValueError):
        r.events.retract(r.sid, event["event_id"])
    assert r.robot.calls == 2


def test_profile_binding_cannot_claim_another_robot_or_measure_a_command(tmp_path):
    with pytest.raises(ValueError, match="actor"):
        rig(tmp_path, actor="arm_1")
    r = rig(tmp_path)
    channel = r.events._profile.channels[0]
    with pytest.raises(ValueError, match="binding"):
        SignalEvents(
            r.observer,
            r.events._profile,
            [Binding(channel.feature, 6, ("pose", 6), "measured")],
            run_id="run",
            clock=r.clock,
            evidence_root=tmp_path,
            keep_frame=lambda *a: {},
        )


def test_storage_and_budget_bounded_and_pending_old_events_expire(tmp_path):
    r = rig(tmp_path, config=StreamConfig(max_history=2))
    for step in range(140):
        r.robot.gripper = float(step % 2)
        poll(r, step)
    assert len(r.events.stream[3].history) == 2
    assert len(r.events._emitted) == 128 and not r.events.pending
    r.events.retract(r.sid, r.events._emitted[-1].event_id)
    r.clock.ns += 600_000_000
    assert r.events.poll(r.sid, through_step=139, max_events=1) == []
    assert r.events.status["dropped"] == 1
    assert len(list(tmp_path.glob("*.jpg"))) == 1


def test_unfresh_state_and_absent_camera_produce_unknown_not_success(tmp_path):
    r = rig(tmp_path)
    poll(r, 0)
    r.robot.state_fresh = False
    r.robot.gripper = 1.0
    assert poll(r, 1) == []
    assert "state_unavailable" in r.events.status["reasons"]
    r.robot.state_fresh = True
    poll(r, 2)
    r.robot.adjust = lambda obs: {**obs, "images_bgr": {}}
    r.robot.gripper = 0.0
    assert poll(r, 3) == []
    assert r.events.status["state"] == "degraded"


def test_real_bounded_evidence_store_and_file_event_wire_compatibility(tmp_path):
    from levi.automatic.fr3_providers import FileEvents
    from levi.automatic.recorder import EvidenceStore

    store = EvidenceStore(tmp_path / "run", max_frame_bytes=4096, max_total_bytes=8192)
    r = rig(tmp_path)
    r.events.evidence_root = store.folder
    r.events.keep_frame = store.keep_frame
    wire = crossing(r)[0]
    event = aeri.parse(wire, "event")
    saved = list(store.frames.glob("*.jpg"))
    assert len(saved) == 1
    assert (
        hashlib.sha256(saved[0].read_bytes()).hexdigest()
        == event.evidence_refs[0].sha256
    )
    files = FileEvents(tmp_path / "provider", "run", r.clock)
    sid = files.open("run.forward.0000", "forward")
    folder, _ = files.streams[sid]
    (folder / "event-0000.json").write_bytes(wire)
    assert files.poll(sid, through_step=1, max_events=1) == [wire]
    assert files.poll(sid, through_step=1, max_events=1) == []
    files.close(sid)


def test_observer_invalidates_cross_episode_and_cross_step_inflight_observations(
    tmp_path,
):
    r = rig(tmp_path)
    r.robot.adjust = lambda obs: setattr(r.robot, "step", 1) or obs
    r.observer.observe()
    assert r.observer.latest is None
    r.robot.adjust = lambda obs: r.observer.begin("different") or obs
    r.observer.observe()
    assert r.observer.latest is None
    assert r.robot.calls == 2


def test_observer_copy_is_bounded_and_does_not_alias_sdk_images(tmp_path):
    r = rig(tmp_path)
    r.observer.observe()
    retained = r.observer.latest[-1]["images_bgr"]["side"]
    r.robot.image[:] = 20
    assert retained[0, 0, 0] == 10
    r.robot.adjust = lambda obs: {
        **obs,
        "images_bgr": {name: r.robot.image for name in ("one", "two", "three")},
    }
    r.observer.observe()
    assert r.observer.latest is None


def test_new_process_cannot_accept_old_identity(tmp_path):
    r = rig(tmp_path)
    crossing(r)
    restarted = SignalEvents(
        r.observer,
        r.events._profile,
        r.events.bindings,
        run_id="run",
        clock=r.clock,
        evidence_root=tmp_path,
        keep_frame=r.events.keep_frame,
    )
    sid = restarted.open("run.forward.0000", "forward")
    assert sid != r.sid
    assert restarted.poll(sid, through_step=0, max_events=1) == []
    with pytest.raises(ValueError):
        restarted.poll(r.sid, through_step=1, max_events=1)


def test_invalid_episode_or_role_refused_before_opening_observer(tmp_path):
    r = rig(tmp_path)
    r.events.close(r.sid)
    for episode, role in (
        ("run.bad.0001", "bad"),
        ("other.forward.0001", "forward"),
        ("run.forward.0001", "reset"),
    ):
        with pytest.raises(ValueError):
            r.events.open(episode, role)
        assert r.observer.identity is None
    r.sid = r.events.open("run.forward.0001", "forward")
    assert r.sid


def test_retraction_rejects_same_domain_clock_rollback(tmp_path):
    r = rig(tmp_path)
    event = json.loads(crossing(r)[0])
    r.clock.ns = 0
    with pytest.raises(ValueError, match="backwards"):
        r.events.retract(r.sid, event["event_id"])


def test_numpy_sdk_pose_scalars_are_valid_measured_xyz(tmp_path):
    r = rig(tmp_path)
    r.events.close(r.sid)
    profile = SignalProfile(
        channels=[
            ChannelSpec(
                role="position",
                feature="pose",
                index=i,
                axis=axis,
                kind="measured",
                units="m",
                frame="base",
            )
            for i, axis in enumerate("xyz")
        ]
    )
    bindings = [Binding("pose", i, ("pose", i), "measured") for i in range(3)]
    r.events = SignalEvents(
        r.observer,
        profile,
        bindings,
        run_id="run",
        clock=r.clock,
        evidence_root=tmp_path,
        keep_frame=lambda *args: {},
        config=StreamConfig(still_ns=100_000_000),
    )
    r.sid = r.events.open("run.forward.0001", "forward")
    r.robot.adjust = lambda obs: {**obs, "pose": np.array(obs["pose"])}
    assert poll(r, 0) == []
    assert not r.events.status["reasons"]
    poll(r, 1)
    assert "missing_signal:pose:0" not in r.events.status["reasons"]


@pytest.mark.parametrize("fresh_observation", [False, True])
def test_queued_candidates_recheck_retained_frame_hash_before_delivery(
    tmp_path, fresh_observation
):
    r = rig(tmp_path)
    r.events.close(r.sid)
    channels = [
        ChannelSpec(
            role="gripper",
            feature=name,
            index=6,
            kind="measured",
            units="normalized",
            valid_range=(0, 1),
            open_level="high",
        )
        for name in ("state_one", "state_two")
    ]
    profile = SignalProfile(channels=channels)
    bindings = [
        Binding(c.feature, 6, ("robot_state", "gripper_position"), "measured")
        for c in channels
    ]
    r.events = SignalEvents(
        r.observer,
        profile,
        bindings,
        run_id="run",
        clock=r.clock,
        evidence_root=tmp_path,
        keep_frame=r.events.keep_frame,
    )
    r.sid = r.events.open("run.forward.0001", "forward")
    poll(r, 0)
    r.robot.gripper = 1.0
    assert len(poll(r, 1, max_events=1)) == 1
    assert len(r.events.pending) == 1
    next(tmp_path.glob("*.jpg")).write_bytes(b"changed")
    if fresh_observation:
        r.clock.ns += 100_000_000
        r.observer.observe()
    assert r.events.poll(r.sid, through_step=1, max_events=1) == []
    assert r.events.status["dropped"] == 1
    assert r.events.status["reasons"] == ["retained_evidence_changed"]


def test_same_step_fresh_observation_is_consumed_without_duplicate_candidate(tmp_path):
    r = rig(tmp_path)
    wire = crossing(r)[0]
    assert len(r.saves) == 1
    kernel = r.events.stream[3]
    last, history = kernel._last, tuple(kernel.history)
    serial = r.observer.serial
    r.robot.gripper = 0.0
    assert poll(r, 1) == []
    assert r.events._last_serial > serial
    assert kernel._last is last and tuple(kernel.history) == history
    assert len(r.saves) == 1
    assert r.events.poll(r.sid, through_step=1, max_events=128) == []
    r.robot.gripper = 0.0
    next_event = json.loads(poll(r, 2)[0])
    assert next_event["step"] == 2 and next_event["window_steps"] == [1, 2]
    assert next_event["event_id"] != json.loads(wire)["event_id"]


@pytest.mark.parametrize("fault", ["old_step", "old_time", "stale", "epoch", "clock"])
def test_same_step_refresh_keeps_step_time_age_epoch_and_clock_fences(tmp_path, fault):
    r = rig(tmp_path)
    crossing(r)
    r.robot.step = 1
    r.clock.ns += 100_000_000
    r.observer.observe()
    through = 1
    if fault == "old_step":
        through = 0
    elif fault == "old_time":
        identity, serial, _, _, step, domain, obs = r.observer.latest
        stamp = r.events._last_observed_ns
        obs["observed_ns"] = stamp
        r.observer.latest = identity, serial, stamp, stamp, step, domain, obs
    elif fault == "stale":
        r.clock.ns += r.events.config.max_age_ns + 1
    elif fault == "epoch":
        r.observer.latest = ("other-epoch", *r.observer.latest[1:])
    elif fault == "clock":
        r.clock.domain = "host-mono:changed"
    with pytest.raises(ValueError):
        r.events.poll(r.sid, through_step=through, max_events=128)


def test_same_step_lower_serial_with_newer_timestamp_is_not_a_new_source(tmp_path):
    r = rig(tmp_path)
    crossing(r)
    r.clock.ns += 100_000_000
    r.observer.observe()
    identity, _, started, ended, step, domain, obs = r.observer.latest
    assert obs["observed_ns"] > r.events._last_observed_ns
    r.observer.latest = (
        identity,
        r.events._last_serial - 1,
        started,
        ended,
        step,
        domain,
        obs,
    )
    with pytest.raises(ValueError, match="serial"):
        r.events.poll(r.sid, through_step=1, max_events=128)


def queued_two_camera_rig(tmp_path):
    r = rig(tmp_path)
    r.events.close(r.sid)
    channels = [
        ChannelSpec(
            role="gripper",
            feature=name,
            index=6,
            kind="measured",
            units="normalized",
            valid_range=(0, 1),
            open_level="high",
        )
        for name in ("state_one", "state_two")
    ]
    r.events = SignalEvents(
        r.observer,
        SignalProfile(channels=channels),
        [
            Binding(c.feature, 6, ("robot_state", "gripper_position"), "measured")
            for c in channels
        ],
        run_id="run",
        clock=r.clock,
        evidence_root=tmp_path,
        keep_frame=r.events.keep_frame,
    )
    r.sid = r.events.open("run.forward.0001", "forward")
    r.robot.adjust = lambda obs: {
        **obs,
        "images_bgr": {"side": r.robot.image, "wrist": r.robot.image + 1},
    }
    poll(r, 0)
    r.robot.gripper = 1.0
    first = aeri.parse(poll(r, 1, max_events=1)[0], "event")
    assert len(first.evidence_refs) == 2
    assert len(r.events.pending) == 1
    return r, first


@pytest.mark.parametrize("missing_frame", [False, True])
def test_same_step_two_camera_refresh_delivers_retained_pending_candidate_once(
    tmp_path, missing_frame
):
    r, first = queued_two_camera_rig(tmp_path)
    if missing_frame:
        r.robot.adjust = lambda obs: {**obs, "images_bgr": {"side": r.robot.image}}
    before = len(r.saves)
    second = aeri.parse(poll(r, 1, max_events=1)[0], "event")
    assert second.event_id != first.event_id
    assert second.evidence_refs == first.evidence_refs
    assert len(second.evidence_refs) == 2
    assert len(r.saves) == before
    assert not r.events.pending
    assert poll(r, 1) == []
    assert r.events.status["dropped"] == 0


def test_same_step_fresh_observation_cannot_refresh_queued_candidate_ttl(tmp_path):
    r, first = queued_two_camera_rig(tmp_path)
    r.clock.ns += r.events.config.max_age_ns + 1
    assert poll(r, 1, max_events=1) == []
    assert r.observer.latest[-1]["observed_ns"] > first.observed_ns
    assert r.events.status["dropped"] == 1
    assert not r.events.pending
    assert poll(r, 1) == []
    assert r.events.status["dropped"] == 1
