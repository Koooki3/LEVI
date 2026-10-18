"""Admission gate, command counters and AdmissionClosedProof on fake env/clock.

No socket, SDK or robot: the env is an in-process fake and the clock is manual.
"""

from types import SimpleNamespace as NS

import numpy as np
import pytest

from levi.automatic import fr3_bridge as B
from levi.automatic import state_machine as sm
from levi.automatic.adapters.physical_hold import (
    AdmissionClosedProof,
    RobotAdmissionWitness,
    assemble_measured_hold,
)
from levi.automatic.intervention import InterventionUnproven
from levi.automatic.reset_manager import SettleConfig

RUN, EP, PLAN = "adm", "adm.forward.0001", "cd" * 32
CFG = SettleConfig(1_000_000, 2_000_000, 10_000_000, 0.001, 0.01, 0.01)


class Clock:
    domain = "host-mono:adm-fake"

    def __init__(self):
        self.ns = 1_000_000_000

    def now(self):
        return self.ns

    def __call__(self):
        return self.ns

    def advance(self, ns):
        self.ns += max(1, ns)


class Env:
    def __init__(self, clock):
        self.clock = clock
        self.http = []
        self.pose = np.array([0.4, 0, 0.2, 0, 0, 0, 1.0])

    def _post(self, route, payload=None):
        self.http.append(route)
        return {"ok": True}

    def get_observation(self):
        image = np.zeros((2, 2, 3), dtype=np.uint8)
        return {
            "image": {"view1": image, "hand": image},
            "robot_state": {
                "cartesian_position": self.pose.copy(),
                "gripper_position": 1.0,
            },
        }

    def step(self, action):
        clearerr = self._post("clearerr")
        pose = self._post("pose")
        return {
            "clearerr_response": clearerr,
            "pose_response": pose,
            "gripper_response": None,
        }


class Guard:
    tripped = None

    def check(self, force=False):
        return NS(status="ok", reasons=[])


def client():
    return NS(_clamp_action=lambda a, c, args: (np.asarray(a, dtype=float), []))


class Rig:
    def __init__(self):
        self.clock = Clock()
        self.env = Env(self.clock)
        self.fence = sm.MotionFence()
        self.token = self.fence.issue(
            sm.MotionToken(
                RUN,
                "tx",
                1,
                "policy_steps",
                EP,
                1,
                self.clock.now() + 10**12,
            )
        )
        self.robot = B.Robot(
            self.env,
            Guard(),
            client(),
            NS(binarize_gripper=False),
            self.clock,
            self.fence,
            tolerance_mm=5,
        )

    def action(self):
        return [0.4, 0, 0.2, 0, 0, 0, 1.0]

    def posts(self, route):
        return self.env.http.count(route)


@pytest.fixture
def rig():
    return Rig()


# --- gate and counters ---------------------------------------------------------


def test_default_gate_is_open_and_step_unchanged(rig):
    result = rig.robot.step(rig.action(), token=rig.token)
    assert (result.executed, result.detail) == ("yes", "guarded_step")
    assert rig.env.http == ["clearerr", "pose"]
    snap = rig.robot.admission_snapshot()
    assert snap.closed is False and snap.generation == 0
    assert snap.pose_released == 2  # clearerr + pose belong to the pose family
    assert snap.gripper_released == 0


def test_closed_gate_rejects_every_route_locally_without_http(rig):
    rig.robot.stop_admission()
    for route in (
        "pose",
        "clearerr",
        "open_gripper",
        "close_gripper",
        "close_gripper_slow",
        "move_gripper",
    ):
        with pytest.raises(B.AdmissionRefused):
            rig.env._post(route, {})
    assert rig.env.http == []
    snap = rig.robot.admission_snapshot()
    assert (snap.pose_attempts, snap.pose_gate_rejected) == (2, 2)
    assert (snap.gripper_attempts, snap.gripper_gate_rejected) == (4, 4)
    assert snap.pose_released == snap.gripper_released == 0


def test_rejection_counts_are_monotonic_and_separate(rig):
    rig.env._post("getstate")  # reads are not commands
    rig.robot.stop_admission()
    seen = []
    for route in ("pose", "open_gripper", "pose"):
        with pytest.raises(B.AdmissionRefused):
            rig.env._post(route)
        s = rig.robot.admission_snapshot()
        seen.append((s.pose_attempts, s.gripper_attempts))
    assert seen == [(1, 0), (1, 1), (2, 1)]


def test_step_after_stop_is_no_without_http_or_latch(rig):
    rig.robot.stop_admission()
    result = rig.robot.step(rig.action(), token=rig.token)
    assert result.executed == "no" and "admission" in result.detail
    assert rig.env.http == []
    assert rig.robot.latch_state() is None


def test_home_after_stop_is_no_without_http(rig):
    rig.robot.client._go_home = lambda env, args: env.step(np.array(rig.action()))
    rig.robot.client._resolve_home = lambda args: np.array(rig.action())
    rig.robot.args = NS(binarize_gripper=False)
    rig.robot.stop_admission()
    result = rig.robot.home(token=rig.token)
    assert result.executed == "no" and rig.env.http == []


def test_stop_is_idempotent_and_keeps_original_closure(rig):
    first = rig.robot.stop_admission()
    rig.clock.advance(5)
    second = rig.robot.stop_admission()
    assert (second.generation, second.closed_from_ns) == (
        first.generation,
        first.closed_from_ns,
    )


def test_reopen_restores_old_behavior_and_changes_generation(rig):
    mark = rig.robot.stop_admission()
    rig.robot.reopen_admission()
    assert rig.robot.step(rig.action(), token=rig.token).executed == "yes"
    assert rig.robot.admission_snapshot().generation > mark.generation


# --- proof ---------------------------------------------------------------------


def witness(rig, mark, **kw):
    return RobotAdmissionWitness(
        rig.robot, rig.fence, rig.clock, mark, run_id=RUN, plan_sha256=PLAN, **kw
    )


def closed(rig):
    rig.fence.revoke()
    return rig.robot.stop_admission()


def test_proof_covers_closure_interval_and_matches_observer_contract(rig):
    rig.robot.step(rig.action(), token=rig.token)
    mark = closed(rig)
    w = witness(rig, mark, validity_ns=50_000_000)
    rig.clock.advance(1000)
    deadline = rig.clock.now() + 100_000_000
    proof = w(EP, 3, deadline)
    assert isinstance(proof, AdmissionClosedProof)
    assert (proof.run_id, proof.episode_id, proof.policy_epoch) == (RUN, EP, 3)
    assert proof.clock_domain == rig.clock.domain
    assert proof.closed_from_ns == mark.closed_from_ns
    assert proof.observed_through_ns == rig.clock.now()
    assert proof.observed_through_ns < proof.expires_ns <= deadline
    assert proof.admission_closed is True and proof.no_new_commands is True


def test_proof_refused_while_gate_never_closed(rig):
    mark = rig.robot.admission_snapshot()  # open snapshot is not a closure mark
    with pytest.raises(InterventionUnproven):
        witness(rig, mark, validity_ns=1)
    rig.robot.stop_admission()
    with pytest.raises(InterventionUnproven):
        witness(rig, mark, validity_ns=1)


def test_proof_refused_if_fence_token_still_live(rig):
    mark = rig.robot.stop_admission()  # fence deliberately NOT revoked
    w = witness(rig, mark, validity_ns=50_000_000)
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now() + 10**8)


def test_counter_change_inside_interval_yields_no_proof(rig):
    mark = closed(rig)
    w = witness(rig, mark, validity_ns=50_000_000)
    w(EP, 3, rig.clock.now() + 10**8)
    # A command slipped through (simulated by tampering with the released count).
    rig.robot._admission_counts["pose"]["attempts"] += 1
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now() + 10**8)


def test_gripper_counter_change_yields_no_proof(rig):
    mark = closed(rig)
    rig.robot._admission_counts["gripper"]["attempts"] += 1
    w = witness(rig, mark, validity_ns=50_000_000)
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now() + 10**8)


def test_rejected_attempts_do_not_break_proof_but_are_visible(rig):
    mark = closed(rig)
    with pytest.raises(B.AdmissionRefused):
        rig.env._post("pose")
    w = witness(rig, mark, validity_ns=50_000_000)
    assert w(EP, 3, rig.clock.now() + 10**8).no_new_commands is True


def test_gate_reopened_yields_no_proof_and_stays_failed(rig):
    mark = closed(rig)
    w = witness(rig, mark, validity_ns=50_000_000)
    w(EP, 3, rig.clock.now() + 10**8)
    rig.robot.reopen_admission()
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now() + 10**8)
    rig.robot.stop_admission()  # re-closing does not repair the old interval
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now() + 10**8)


def test_reopen_and_reclose_between_samples_is_a_discontinuity(rig):
    mark = closed(rig)
    w = witness(rig, mark, validity_ns=50_000_000)
    rig.robot.reopen_admission()
    rig.clock.advance(10)
    rig.robot.stop_admission()
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now() + 10**8)


def test_clock_going_backwards_yields_no_proof(rig):
    mark = closed(rig)
    w = witness(rig, mark, validity_ns=50_000_000)
    rig.clock.advance(100)
    w(EP, 3, rig.clock.now() + 10**8)
    rig.clock.ns -= 50
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now() + 10**8)


def test_binding_to_episode_and_epoch_cannot_change(rig):
    mark = closed(rig)
    w = witness(rig, mark, validity_ns=50_000_000)
    w(EP, 3, rig.clock.now() + 10**8)
    with pytest.raises(InterventionUnproven):
        w(EP, 4, rig.clock.now() + 10**8)


def test_validity_beyond_deadline_is_clipped_and_expired_deadline_refused(rig):
    mark = closed(rig)
    w = witness(rig, mark, validity_ns=10**12)
    deadline = rig.clock.now() + 1000
    assert w(EP, 3, deadline).expires_ns == deadline
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now())  # nothing left to prove


def test_no_defaults_for_injected_parameters(rig):
    mark = closed(rig)
    with pytest.raises(TypeError):
        RobotAdmissionWitness(  # type: ignore[call-arg]
            rig.robot, rig.fence, rig.clock, mark, run_id=RUN, plan_sha256=PLAN
        )
    with pytest.raises(InterventionUnproven):
        witness(rig, mark, validity_ns=0)


def test_wrong_clock_domain_refused(rig):
    mark = closed(rig)
    other = Clock()
    other.domain = "host-mono:other"
    with pytest.raises(InterventionUnproven):
        RobotAdmissionWitness(
            rig.robot,
            rig.fence,
            other,
            mark,
            run_id=RUN,
            plan_sha256=PLAN,
            validity_ns=1,
        )


# --- assembly into MeasuredPhysicalHold ----------------------------------------


class Observing:
    """Robot facade whose observe() reads a fake measured stream; the gate and
    counters come from the real bridge Robot."""

    def __init__(self, rig):
        self.rig, self.generation = rig, 0

    def observe(self):
        clock = self.rig.clock
        began = clock.now()
        clock.advance(1)
        self.generation += 1
        pose = [0.0] * 6 + [0.9]
        return {
            "observed_ns": clock.now(),
            "state_fresh": True,
            "pose": pose,
            "robot_state": {"cartesian_position": list(pose), "gripper_position": 0.1},
            "gripper_measurement": {
                "route": "getstate",
                "field": "gripper_pos",
                "generation": self.generation,
                "read_from_ns": began,
                "read_through_ns": clock.now(),
                "clock_domain": clock.domain,
                "value": 0.1,
            },
        }


def assembled(rig, mark, **kw):
    return assemble_measured_hold(
        Observing(rig),
        rig.robot,
        rig.fence,
        rig.clock,
        CFG,
        mark,
        run_id=RUN,
        plan_sha256=PLAN,
        validity_ns=50_000_000,
        interrupted=lambda: False,
        **kw,
    )


def test_assembled_hold_returns_physical_proof_through_real_gate(rig):
    mark = closed(rig)
    hold = assembled(rig, mark)
    proof = hold(EP, 3, rig.clock.now() + 100_000_000)
    assert proof.admission_closed and proof.no_new_commands
    assert proof.physically_stable
    assert rig.env.http == []


def test_assembled_hold_fails_when_gate_reopened_midway(rig):
    mark = closed(rig)
    hold = assembled(rig, mark)
    obs = hold.robot.observe

    def reopen_then_observe():
        rig.robot.reopen_admission()
        return obs()

    hold.robot.observe = reopen_then_observe
    with pytest.raises(InterventionUnproven):
        hold(EP, 3, rig.clock.now() + 100_000_000)
    assert hold.failed


def test_assembled_hold_fails_when_command_released_midway(rig):
    mark = closed(rig)
    hold = assembled(rig, mark)
    obs = hold.robot.observe

    def tamper():
        rig.robot._admission_counts["gripper"]["attempts"] += 1
        return obs()

    hold.robot.observe = tamper
    with pytest.raises(InterventionUnproven):
        hold(EP, 3, rig.clock.now() + 100_000_000)
