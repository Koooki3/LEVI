"""``object_dropped``: the MEASURED opening falls below the holding band while
the command is closed. Never a command value, never motion."""

import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from levi.automatic.adapters.signal_events import Binding, CachedObserver, SignalEvents
from levi.automatic.holding import DropConfig
from levi.domain import aeri
from levi.events.signal_profiles import ChannelSpec, SignalProfile

BAND = (0.15, 0.45)  # example band, not a calibrated one


class Clock:
    domain = "host-mono:test"
    ns = 0

    def now(self):
        return self.ns


class Robot:
    def __init__(self, clock):
        self.clock, self.step = clock, 0
        self.gripper, self.command = 0.30, 0.0
        self.proof = True
        self.generation = 0
        self.image = np.full((8, 8, 3), 10, dtype=np.uint8)

    def observe(self):
        started = self.clock.now()
        self.clock.ns += 1
        self.generation += 1
        value = {
            "observed_ns": self.clock.now(),
            "pose": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, self.command],
            "robot_state": {"gripper_position": self.gripper},
            "state_fresh": True,
            "images_bgr": {"side": self.image},
        }
        if self.proof:
            value["gripper_measurement"] = {
                "route": "getstate",
                "field": "gripper_pos",
                "generation": self.generation,
                "read_from_ns": started,
                "read_through_ns": self.clock.now(),
                "clock_domain": self.clock.domain,
                "value": self.gripper,
            }
        return value


def channel(kind):
    return ChannelSpec(
        role="gripper",
        feature="observation.state" if kind == "measured" else "action",
        index=6,
        kind=kind,
        units="normalized",
        valid_range=(0, 1),
        open_level="high",
        actor_id="arm_0",
    )


def rig(tmp_path, *, drop="default", kinds=("measured", "commanded"), role="reset"):
    clock = Clock()
    robot = Robot(clock)
    observer = CachedObserver(robot, clock, step_of=lambda: robot.step)
    profile = SignalProfile(channels=[channel(k) for k in kinds])
    bindings = [
        Binding(
            channel(k).feature,
            6,
            ("robot_state", "gripper_position") if k == "measured" else ("pose", 6),
            k,
        )
        for k in kinds
    ]

    def save(ref, data):
        digest = hashlib.sha256(data).hexdigest()
        (tmp_path / (digest + ".jpg")).write_bytes(data)
        return {"ref": ref, "sha256": digest, "file": digest + ".jpg"}

    if drop == "default":
        drop = DropConfig(*BAND, samples=3, arm_samples=2)
    events = SignalEvents(
        observer,
        profile,
        bindings,
        run_id="run",
        clock=clock,
        evidence_root=tmp_path,
        keep_frame=save,
        drop=drop,
    )
    sid = events.open(f"run.{role}.0000", role)
    return SimpleNamespace(
        clock=clock, robot=robot, observer=observer, events=events, sid=sid
    )


def poll(r, step, *, gripper=None, command=None):
    r.robot.step = step
    if gripper is not None:
        r.robot.gripper = gripper
    if command is not None:
        r.robot.command = command
    r.clock.ns += 100_000_000
    r.observer.observe()
    out = r.events.poll(r.sid, through_step=step, max_events=128)
    return [aeri.parse(x, "event") for x in out]


def drops(events):
    return [e for e in events if e.event_type == "object_dropped"]


def play(r, rows):
    found = []
    for step, (command, gripper) in enumerate(rows):
        found += drops(poll(r, step, gripper=gripper, command=command))
    return found


def test_command_closed_and_measured_falls_below_band_makes_one_event(tmp_path):
    r = rig(tmp_path)
    found = play(r, [(0.0, 0.30)] * 3 + [(0.0, 0.01)] * 5)
    assert len(found) == 1
    event = found[0]
    assert event.status == "candidate"
    assert event.priority == "routine"  # never a goal candidate of the arbiter
    assert event.step == 5 and event.window_steps == [3, 5]
    assert [s.kind for s in event.signal_refs] == ["measured", "commanded"]
    assert event.evidence_refs and event.evidence_refs[0].sha256
    assert event.detector.version == r.events.stream[3].profile_sha256


@pytest.mark.parametrize(
    "rows",
    [
        [(0.0, 0.30)] * 10,  # stays inside the band
        [(1.0, 0.30)] * 3 + [(1.0, 0.01)] * 5,  # command open, measured low
        [(0.0, 0.30)] * 3 + [(1.0, 0.01)] * 5,  # released on purpose
        [(0.0, 0.01)] * 10,  # never held
    ],
)
def test_no_event(tmp_path, rows):
    assert play(rig(tmp_path), rows) == []


def test_the_command_is_not_the_event_source(tmp_path):
    # command value 0.0 is below the band lo; mistaken for the opening it would fire
    r = rig(tmp_path)
    assert play(r, [(0.0, 0.30)] * 12) == []


def test_unproven_measurement_never_fires_and_clears_the_run(tmp_path):
    r = rig(tmp_path)
    found = play(r, [(0.0, 0.30)] * 3 + [(0.0, 0.01)] * 2)
    assert found == []
    r.robot.proof = False
    assert drops(poll(r, 5, gripper=0.01, command=0.0)) == []
    r.robot.proof = True
    assert drops(poll(r, 6, gripper=0.01, command=0.0)) == []  # run restarted


def test_drop_is_opt_in_and_needs_both_bindings(tmp_path):
    r = rig(tmp_path, drop=None)
    assert play(r, [(0.0, 0.30)] * 3 + [(0.0, 0.01)] * 5) == []
    with pytest.raises(ValueError):
        rig(tmp_path, kinds=("measured",))
    with pytest.raises(ValueError):
        rig(tmp_path, kinds=("commanded",))
    with pytest.raises(TypeError):
        rig(tmp_path, drop={"lo": 0.1})


def test_roles_limit_the_detector(tmp_path):
    only_reset = DropConfig(*BAND, samples=2, arm_samples=1, roles=("reset",))
    rows = [(0.0, 0.30)] * 2 + [(0.0, 0.01)] * 4
    assert play(rig(tmp_path, drop=only_reset, role="forward"), rows) == []
    assert len(play(rig(tmp_path, drop=only_reset, role="reset"), rows)) == 1


def test_the_event_type_is_registered_and_listable_by_early_check():
    from levi.automatic import subtask_loop as st

    assert "object_dropped" in aeri.EVENT_TYPES
    early = st.EarlyCheck(
        on_events=True, event_types=("gripper_open", "object_dropped")
    )
    assert early.event_types == ("gripper_open", "object_dropped")
    assert st.EARLY_EVENT_TYPES == ("gripper_open",)  # the default is unchanged
