"""Full client/server contract in memory, with a deterministic simulated robot."""

import threading

from test_manual_transport import LIMITS, STOPS

from integrations.fr3_manual.protocol import Protocol
from integrations.fr3_manual.test_protocol import Fake
from levi.marvel0 import manual_transport


class Robot(Fake):
    def __init__(self):
        super().__init__()
        self.pose = [0.4, 0, 0.3, 0, 0, 0, 1]
        self.stopped = threading.Event()

    def sample(self):
        return {
            **super().sample(),
            "pose7": list(self.pose),
            "joints": [0, -0.6, 0, -2.1, 0, 1.6, 0.8],
        }

    def publish(self, pose):
        self.pose = list(pose)
        super().publish(pose)

    def cancel_gripper(self):
        # This fake's cancel produces terminal action state. Real cancellation
        # acknowledgement alone is separately tested as insufficient.
        self.terminal = True
        self.stopped.set()


def create_pair():
    robot = Robot()
    protocol = Protocol(robot, lambda: robot.t)
    limits = {**LIMITS, "target_timeout_s": 2.0, "deadman_timeout_s": 0.4}
    calls = []

    def request(method, path, body):
        calls.append(path)
        result = (
            getattr(protocol, path)(body)
            if body is not None
            else getattr(protocol, path)()
        )
        return {**result, "ok": True}

    engine = manual_transport.create(
        {
            "session_id": "integration",
            "generation": 1,
            "limits": limits,
            "stop_limits": STOPS,
            "robot_server_url": "http://127.0.0.1:5000",
            "audit": lambda *a, **k: None,
            "now": lambda: robot.t,
            "request": request,
        }
    )
    return robot, protocol, engine, calls


def test_held_gripper_survives_several_heartbeats_then_measured_stop():
    robot, protocol, engine, calls = create_pair()
    identity = {"session_id": "integration", "generation": 1}
    # The person may review the anchor before beginning to hold a control.
    robot.advance(0.6)
    engine.tick()
    assert engine.view()["admission"] == "open"
    engine.deadman(**identity, sequence=1, pressed=True)
    engine.gripper(**identity, sequence=2, opening_ratio=0.5)
    for sequence in range(3, 11):
        robot.advance(0.1)
        engine.deadman(**identity, sequence=sequence, pressed=True)
        protocol.tick()
        assert protocol.open
    assert calls.count("gripper") == 1
    engine.stop()
    assert robot.stopped.wait(1)
    # New arm AND gripper samples across the entire explicit settle window.
    for _ in range(4):
        robot.advance(0.1)
        protocol.tick()
    engine.end(engine.physical_evidence())
    assert engine.view()["stop_evidence"] == "physical_settled"
    assert protocol.session_id is None


def test_translation_is_bounded_and_release_has_measured_proof():
    robot, protocol, engine, _calls = create_pair()
    identity = {"session_id": "integration", "generation": 1}
    engine.deadman(**identity, sequence=1, pressed=True)
    engine.target(
        **identity,
        anchor_id=engine.view()["anchor_id"],
        sequence=2,
        delta_position_m=[0.01, 0, 0],
    )
    engine.pump()
    assert len(robot.sent) == 1
    assert 0 < robot.pose[0] - 0.4 <= LIMITS["max_step_m"]
    assert robot.pose[3:] == [0, 0, 0, 1]
    engine.stop()
    assert robot.stopped.wait(1)
    for _ in range(4):
        robot.advance(0.1)
        protocol.tick()
    engine.end(engine.physical_evidence())
    assert protocol.session_id is None
