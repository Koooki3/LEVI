"""Home-only admission (``Robot.home_under_closed_gate``) on a fake env and clock.

No socket, SDK or robot. The env imitates the routes the reviewed client's
``_go_home`` reaches through ``RobotEnv.step`` (clearerr, pose, and open_gripper
when the gripper is not open); it can also be told to send a route the home must
never use. The tests state what the software gate lets through, not what a real
arm does.
"""

import dataclasses
from types import SimpleNamespace as NS

import numpy as np
import pytest

from levi.automatic import fr3_bridge as B
from levi.automatic import state_machine as sm
from levi.automatic.adapters.physical_hold import RobotAdmissionWitness
from levi.automatic.intervention import InterventionUnproven

RUN, EP, PLAN = "hg", "hg.forward.0001", "ef" * 32
HOME = [0.48, -0.01, 0.14, 3.1, 0.0, -0.88, 1.0]
WHITELIST = {"pose", "clearerr", "open_gripper"}


class Clock:
    domain = "host-mono:hg-fake"

    def __init__(self):
        self.ns = 1_000_000_000

    def now(self):
        return self.ns

    def __call__(self):
        return self.ns

    def advance(self, ns):
        self.ns += max(1, ns)


class Env:
    """Imitates RobotEnv.step: clearerr, pose, then a gripper POST only when the
    commanded gripper differs from the measured one."""

    def __init__(self, gripper=0.2):
        self.http = []
        self.pose = np.array([0.40, 0.0, 0.30, 3.0, 0.1, -0.5, 1.0])
        self.gripper = gripper
        self.reply = {}
        self.on_post = None  # called inside the original POST, i.e. past the gate

    def _post(self, route, payload=None):
        self.http.append(route)
        if self.on_post is not None:
            self.on_post(route)
        return self.reply.get(route, {"ok": True})

    def get_observation(self):
        image = np.zeros((2, 2, 3), dtype=np.uint8)
        return {
            "image": {"view1": image, "hand": image},
            "robot_state": {
                "cartesian_position": self.pose.copy(),
                "gripper_position": self.gripper,
            },
        }

    def step(self, action):
        action = np.asarray(action, dtype=float)
        clearerr = self._post("clearerr")
        pose = self._post("pose", {"arr": list(action[:6])})
        gripper = None
        if action[6] >= 0.5 and self.gripper < 0.9:
            gripper = {
                "command": "open_gripper",
                "response": self._post("open_gripper"),
            }
            self.gripper = 1.0
        elif action[6] < 0.5 and self.gripper > 0.2:
            gripper = {
                "command": "close_gripper",
                "response": self._post("close_gripper"),
            }
        self.pose = np.array([*action[:6], 1.0])
        return {
            "clearerr_response": clearerr,
            "pose_response": pose,
            "gripper_response": gripper,
        }


class Guard:
    tripped = None
    status = "ok"

    def check(self, force=False):
        return NS(status=self.status, reasons=[])


class Rig:
    def __init__(self, *, gripper=0.2, go_home=None):
        self.clock = Clock()
        self.env = Env(gripper)
        self.guard = Guard()
        self.fence = sm.MotionFence()
        self.steps_seen = []

        def clamp(action, current, args):
            return np.asarray(action, dtype=float), []

        def default_go_home(env, args):
            for fraction in (0.5, 1.0):
                self.steps_seen.append(fraction)
                env.step(np.array(HOME))

        client = NS(
            _clamp_action=clamp,
            _resolve_home=lambda args: np.array(HOME),
            _go_home=go_home(self) if go_home else default_go_home,
        )
        self.robot = B.Robot(
            self.env,
            self.guard,
            client,
            NS(binarize_gripper=False),
            self.clock,
            self.fence,
            tolerance_mm=5,
        )

    def token(self, kind="home", *, expires=10**12, transaction="tx-home"):
        return self.fence.issue(
            sm.MotionToken(RUN, transaction, 2, kind, EP, 1, self.clock.now() + expires)
        )

    def close(self):
        self.fence.revoke()
        return self.robot.stop_admission()

    def snap(self):
        return self.robot.admission_snapshot()

    def action(self):
        return list(HOME)


@pytest.fixture
def rig():
    return Rig()


# --- entry conditions: nothing is sent, nothing is latched ----------------------------


def test_open_gate_refuses_the_home_only_entry(rig):
    token = rig.token()
    result = rig.robot.home_under_closed_gate(token=token)
    assert result.executed == "no" and "closed" in result.detail
    assert rig.env.http == [] and rig.robot.latch_state() is None
    assert rig.snap().mode == "open" and rig.snap().generation == 0


@pytest.mark.parametrize(
    "case",
    ["policy_kind", "expired", "revoked", "superseded", "no_token"],
)
def test_bad_tokens_never_enter_the_home_only_window(rig, case):
    rig.close()
    before = rig.snap()
    if case == "policy_kind":
        token = rig.token("policy_steps")
    elif case == "expired":
        token = rig.token(expires=-1)
    elif case == "revoked":
        token = rig.token()
        rig.fence.revoke()
    elif case == "superseded":
        token = rig.token(transaction="tx-old")
        rig.token(transaction="tx-new")
    else:
        token = None
    result = rig.robot.home_under_closed_gate(token=token)
    assert result.executed == "no"
    assert rig.env.http == []
    after = rig.snap()
    assert (after.mode, after.generation, after.closed_from_ns) == (
        "closed",
        before.generation,
        before.closed_from_ns,
    )
    assert rig.robot.latch_state() is None
    assert (after.home_pose_released, after.home_gripper_released) == (0, 0)


@pytest.mark.parametrize("case", ["stopped", "latched", "health_red", "guard_tripped"])
def test_stop_latch_or_red_health_never_enter_the_window(rig, case):
    rig.close()
    token = rig.token()
    if case == "stopped":
        rig.robot.stop_event.set()
    elif case == "latched":
        rig.robot.latched = "earlier fault"
    elif case == "health_red":
        rig.guard.status = "red"
    else:
        rig.guard.tripped = "tripped"
    try:
        result = rig.robot.home_under_closed_gate(token=token)
    finally:
        rig.guard.tripped = None
    assert result.executed == "no" and rig.env.http == []
    assert rig.snap().mode == "closed"
    assert rig.snap().generation == 1


def test_the_old_home_is_still_refused_by_a_closed_gate(rig):
    rig.close()
    result = rig.robot.home(token=rig.token())
    assert result.executed == "no" and rig.env.http == []
    assert rig.snap().mode == "closed" and rig.snap().home_pose_released == 0


# --- the window: whitelist, counters, no policy ----------------------------------------


def test_home_passes_only_the_whitelisted_routes_and_counts_them_apart(rig):
    rig.close()
    mark = rig.snap()
    result = rig.robot.home_under_closed_gate(token=rig.token())
    assert (result.executed, result.detail) == ("yes", "home_verified")
    assert set(rig.env.http) <= WHITELIST
    assert rig.env.http.count("open_gripper") == 1  # the gripper began closed
    snap = rig.snap()
    # clearerr + pose for each of two interpolation steps, one open_gripper
    assert snap.home_pose_released == 4 and snap.home_gripper_released == 1
    assert snap.home_gate_rejected == 0
    # the ordinary counters do not move: a witness of the old closure sees nothing
    assert (snap.pose_attempts, snap.pose_gate_rejected) == (
        mark.pose_attempts,
        mark.pose_gate_rejected,
    )
    assert (snap.gripper_attempts, snap.gripper_gate_rejected) == (
        mark.gripper_attempts,
        mark.gripper_gate_rejected,
    )
    assert rig.robot.held is True


def test_inside_the_window_the_snapshot_is_home_only_and_not_closed():
    seen = {}

    def go_home(r):
        def run(env, args):
            seen["snap"] = r.snap()
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    assert r.robot.home_under_closed_gate(token=r.token()).executed == "yes"
    snap = seen["snap"]
    assert snap.mode == "home_only" and snap.closed is False
    assert snap.home_only_from_ns is not None
    assert snap.generation == 2  # closed (1) -> home_only (2)


def test_policy_steps_and_other_gripper_routes_are_refused_inside_the_window():
    seen, holder = {}, {}

    def go_home(r):
        def run(env, args):
            policy = sm.MotionToken(
                RUN, "tx-pol", 3, "policy_steps", EP, 1, r.clock.now() + 10**12
            )
            seen["policy_step"] = r.robot.step(r.action(), token=policy)
            for route in ("close_gripper", "close_gripper_slow", "move_gripper"):
                try:
                    r.env._post(route, {})
                except Exception as exc:  # noqa: BLE001
                    seen[route] = type(exc).__name__
            for route in ("jointreset", "startimp", "activate_gripper"):
                try:
                    r.env._post(route, {})
                except Exception as exc:  # noqa: BLE001
                    seen[route] = type(exc).__name__
            try:  # a pose POST outside the robot's own send path
                r.env._post("pose", {})
            except Exception as exc:  # noqa: BLE001
                seen["bare_pose"] = type(exc).__name__
            try:  # the old, ungated send path may not use the window
                r.robot._send(r.action(), holder["token"], "home")
            except B.AdmissionRefused as exc:
                seen["ungated"] = str(exc)
            seen["reopen"] = None
            try:
                r.robot.reopen_admission()
            except B.AdmissionRefused as exc:
                seen["reopen"] = str(exc)
            seen["mode_after_attempts"] = r.snap().mode
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    generation = r.snap().generation
    holder["token"] = r.token()
    result = r.robot.home_under_closed_gate(token=holder["token"])
    assert result.executed == "yes"
    assert seen["policy_step"].executed == "no"
    for route in ("close_gripper", "close_gripper_slow", "move_gripper", "bare_pose"):
        assert seen[route] == "AdmissionRefused", route
    for route in ("jointreset", "startimp", "activate_gripper"):
        assert seen[route] == "ValueError"
    assert "home-only" in seen["ungated"]  # the old home() path is not the gate's
    assert seen["reopen"] is not None and seen["mode_after_attempts"] == "home_only"
    assert set(r.env.http) <= WHITELIST
    snap = r.snap()
    assert snap.home_gate_rejected >= 4  # counted apart from the ordinary counters
    assert snap.generation == generation + 2  # entered and left
    assert (snap.pose_attempts, snap.gripper_attempts) == (0, 0)


def test_a_client_that_closes_the_gripper_is_stopped_at_the_gate():
    def go_home(r):
        def run(env, args):
            env.step(np.array([*HOME[:6], 0.0]))  # asks to close: not a home route

        return run

    r = Rig(go_home=go_home, gripper=1.0)
    r.close()
    # gripper measured open: the env sends close_gripper only if gripper > 0.2
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "unknown"  # a pose was already sent
    assert "close_gripper" not in r.env.http
    assert r.snap().home_gate_rejected == 1
    assert r.robot.latch_state() is not None
    assert r.snap().mode == "closed"


# --- the window always ends closed -----------------------------------------------------


def test_success_ends_closed_with_a_new_generation_and_closure_time(rig):
    mark = rig.close()
    rig.clock.advance(10_000)
    result = rig.robot.home_under_closed_gate(token=rig.token())
    assert result.executed == "yes"
    posts = list(rig.env.http)
    snap = rig.snap()
    assert snap.mode == "closed" and snap.closed is True
    assert snap.generation == mark.generation + 2
    assert snap.closed_from_ns >= mark.closed_from_ns + 10_000
    assert snap.home_only_from_ns is not None
    assert snap.home_only_from_ns <= snap.closed_from_ns
    # the policy still cannot move
    policy = rig.token("policy_steps", transaction="tx-after")
    after = rig.robot.step(rig.action(), token=policy)
    assert after.executed == "no" and "admission" in after.detail
    assert rig.env.http == posts


def test_a_resume_after_the_home_reopens_with_a_newer_generation(rig):
    rig.close()
    rig.robot.home_under_closed_gate(token=rig.token())
    closed = rig.snap().generation
    rig.fence.revoke()
    rig.robot.reopen_admission()
    assert rig.snap().mode == "open" and rig.snap().generation == closed + 1
    rig.robot.latched = None
    policy = rig.token("policy_steps", transaction="tx-resume")
    assert rig.robot.step(rig.action(), token=policy).executed == "yes"


def test_an_exception_from_the_client_ends_closed_and_latched():
    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            raise RuntimeError("client blew up after a pose")

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "unknown" and "blew up" in result.detail
    assert r.robot.latch_state() is not None
    assert r.snap().mode == "closed"


def test_an_exception_before_any_pose_is_a_plain_no_and_still_closes():
    def go_home(r):
        def run(env, args):
            raise RuntimeError("client failed before sending")

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "no" and r.env.http == []
    assert r.snap().mode == "closed"


def test_a_base_exception_still_closes_the_gate():
    class Crash(BaseException):
        pass

    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            raise Crash

        return run

    r = Rig(go_home=go_home)
    r.close()
    generation = r.snap().generation
    with pytest.raises(Crash):
        r.robot.home_under_closed_gate(token=r.token())
    snap = r.snap()
    assert snap.mode == "closed" and snap.generation == generation + 2
    assert r.robot.held is True


def test_unverified_home_is_unknown_latched_and_closed():
    def go_home(r):
        def run(env, args):
            env.step(np.array([*HOME[:2], 0.60, *HOME[3:]]))  # ends 460 mm high

        return run

    r = Rig(go_home=go_home)
    r.robot.home_settle_timeout_s = 0.3
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "unknown" and r.robot.latch_state() is not None
    assert r.snap().mode == "closed"


def test_a_stop_inside_the_window_revokes_the_token_and_closes():
    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            r.fence.revoke()  # what a stop does first
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "unknown"
    # the first step went out; the revoked token stopped the second before any POST
    assert r.env.http == ["clearerr", "pose", "open_gripper"]
    assert r.snap().mode == "closed" and r.robot.latch_state() is not None


def test_stop_admission_inside_the_window_closes_it_at_once():
    seen = {}

    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            seen["mark"] = r.robot.stop_admission()
            try:
                r.env._post("pose", {})
            except B.AdmissionRefused as exc:
                seen["after"] = str(exc)
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "unknown"
    assert seen["mark"].mode == "closed" and "closed" in seen["after"]
    snap = r.snap()
    assert snap.mode == "closed" and snap.generation == seen["mark"].generation


# --- snapshot compatibility and the old proofs -----------------------------------------


def test_snapshot_keeps_its_positional_shape_and_defaults_the_new_fields():
    old = B.AdmissionSnapshot(True, 3, 5, 7, "d", 1, 0, 1, 0)
    assert (old.mode, old.home_only_from_ns) == ("open", None)
    assert (old.home_pose_released, old.home_gripper_released) == (0, 0)
    assert old.home_gate_rejected == 0
    assert old.pose_released == 1 and old.gripper_released == 1


def witness(rig, mark):
    return RobotAdmissionWitness(
        rig.robot,
        rig.fence,
        rig.clock,
        mark,
        run_id=RUN,
        plan_sha256=PLAN,
        validity_ns=50_000_000,
    )


def test_an_old_witness_fails_forever_after_a_home_window(rig):
    mark = rig.close()
    old = witness(rig, mark)
    assert old(EP, 3, rig.clock.now() + 10**8).no_new_commands is True
    rig.robot.home_under_closed_gate(token=rig.token())
    rig.fence.revoke()
    with pytest.raises(InterventionUnproven):
        old(EP, 3, rig.clock.now() + 10**8)
    # the gate is closed again, but the old interval is gone for good
    assert rig.snap().mode == "closed"
    with pytest.raises(InterventionUnproven):
        old(EP, 3, rig.clock.now() + 10**8)


def test_a_witness_inside_the_window_fails_even_when_counters_are_unchanged():
    seen = {}

    def go_home(r):
        def run(env, args):
            w = witness(r, mark)
            try:
                w(EP, 3, r.clock.now() + 10**8)
            except InterventionUnproven as exc:
                seen["inside"] = str(exc)
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    mark = r.close()
    r.robot.home_under_closed_gate(token=r.token())
    assert "open" in seen["inside"] or "reopened" in seen["inside"]


def test_a_new_closure_mark_after_the_home_gives_a_working_witness(rig):
    first = rig.close()
    rig.robot.home_under_closed_gate(token=rig.token())
    rig.fence.revoke()
    fresh = rig.robot.stop_admission()  # idempotent: the current closed interval
    assert fresh.generation == first.generation + 2
    assert fresh.closed_from_ns >= first.closed_from_ns
    proof = witness(rig, fresh)(EP, 4, rig.clock.now() + 10**8)
    assert proof.admission_closed and proof.no_new_commands
    assert proof.closed_from_ns == fresh.closed_from_ns


def test_the_witness_refuses_a_snapshot_whose_mode_is_not_closed(rig):
    mark = rig.close()
    w = witness(rig, mark)
    real = rig.robot.admission_snapshot
    # An inconsistent snapshot (closed flag set, mode home_only) is not a closure.
    rig.robot.admission_snapshot = lambda: dataclasses.replace(real(), mode="home_only")
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now() + 10**8)
    assert w.failed


def test_a_snapshot_taken_in_the_window_is_not_a_closure_mark():
    seen = {}

    def go_home(r):
        def run(env, args):
            seen["mark"] = r.snap()
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    r.robot.home_under_closed_gate(token=r.token())
    with pytest.raises(InterventionUnproven):
        witness(r, seen["mark"])


@pytest.mark.parametrize("family", ["pose", "gripper"])
def test_a_home_command_counted_inside_the_interval_yields_no_proof(rig, family):
    mark = rig.close()
    w = witness(rig, mark)
    w(EP, 3, rig.clock.now() + 10**8)
    # Tampering: a home command slipped through the closed gate.
    rig.robot._home_counts[family] += 1
    with pytest.raises(InterventionUnproven):
        w(EP, 3, rig.clock.now() + 10**8)


# --- review fixes: threads, reopening inside the window, zero-POST windows ----------


def test_another_thread_cannot_use_the_window_even_during_a_home_step():
    import threading

    seen = {}

    def foreign(r):
        outcome = {}

        def attempt(route):
            try:
                r.env._post(route, {})
                outcome[route] = "sent"
            except B.AdmissionRefused:
                outcome[route] = "refused"
            except Exception as exc:  # noqa: BLE001
                outcome[route] = type(exc).__name__

        def during_step(route):
            if route != "pose" or "done" in seen:
                return
            seen["done"] = True
            marks = len(r.env.http)
            threads = [
                threading.Thread(target=attempt, args=(name,))
                for name in (
                    "pose",
                    "clearerr",
                    "open_gripper",
                    "close_gripper",
                    "close_gripper_slow",
                    "move_gripper",
                )
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            seen["outcome"] = outcome
            seen["foreign_posts"] = r.env.http[marks:]

        return during_step

    r = Rig()
    r.env.on_post = foreign(r)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "yes"
    assert set(seen["outcome"].values()) == {"refused"}, seen["outcome"]
    assert seen["foreign_posts"] == []
    assert r.snap().home_gate_rejected >= 6


def test_reopen_inside_the_window_is_refused_even_after_stop_admission():
    seen = {}

    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            r.robot.stop_admission()
            try:
                r.robot.reopen_admission()
            except B.AdmissionRefused as exc:
                seen["reopen"] = str(exc)
            seen["mode"] = r.snap().mode
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert "home" in seen["reopen"] and seen["mode"] == "closed"
    assert result.executed == "unknown"  # the second step met a closed gate
    assert r.env.http == ["clearerr", "pose", "open_gripper"]
    assert r.snap().mode == "closed"


def test_the_window_forces_the_gate_closed_whatever_its_mode_at_the_end():
    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            r.robot._admission_mode = B.GATE_OPEN  # a bug elsewhere reopened it

        return run

    r = Rig(go_home=go_home)
    r.close()
    generation = r.snap().generation
    r.robot.home_under_closed_gate(token=r.token())
    snap = r.snap()
    assert snap.mode == "closed" and snap.generation > generation + 1
    assert (
        r.robot.step(
            r.action(), token=r.token("policy_steps", transaction="p")
        ).executed
        == "no"
    )


def test_a_window_with_no_post_and_a_frozen_clock_still_kills_old_witnesses():
    def go_home(r):
        return lambda env, args: None  # the arm is already home: nothing is sent

    r = Rig(go_home=go_home, gripper=1.0)
    r.env.pose = np.array(HOME)
    mark = r.close()
    old = witness(r, mark)
    old(EP, 3, r.clock.now() + 10**8)
    now = r.clock.now()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "yes" and r.env.http == []
    # the clock did not move, so only the generation separates the intervals
    assert r.snap().generation == mark.generation + 2
    assert r.robot.admission_snapshot().closed_from_ns >= now
    r.fence.revoke()
    with pytest.raises(InterventionUnproven):
        old(EP, 3, r.clock.now() + 10**8)


# --- second review: stop before the first POST, re-entry, clock, threads ---------------


def test_a_revoked_token_before_the_first_post_is_a_no_and_does_not_latch():
    def go_home(r):
        def run(env, args):
            r.fence.revoke()  # what Orchestrator.stop does first
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "no" and r.env.http == []
    assert r.robot.latch_state() is None
    assert r.snap().mode == "closed"
    # the next person-approved run is not locked by this stop
    assert r.robot.preflight()["ok"] is True


def test_a_revoked_token_after_a_post_still_locks_and_is_unknown():
    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            r.fence.revoke()
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert result.executed == "unknown" and r.robot.latch_state() is not None


def test_the_old_home_still_latches_on_a_revoked_token():
    def go_home(r):
        def run(env, args):
            r.fence.revoke()
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    result = r.robot.home(token=r.token())
    assert result.executed == "no" and r.robot.latch_state() is not None


def test_a_nested_home_is_refused_and_does_not_end_the_outer_window():
    seen = {}

    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            r.robot.stop_admission()
            seen["inner"] = r.robot.home_under_closed_gate(token=r.token())
            try:
                r.robot.reopen_admission()
            except B.AdmissionRefused:
                seen["reopen"] = "refused"
            seen["gen"] = r.snap().generation

        return run

    r = Rig(go_home=go_home)
    r.close()
    r.robot.home_under_closed_gate(token=r.token())
    assert seen["inner"].executed == "no" and "already" in seen["inner"].detail
    assert seen["reopen"] == "refused"
    assert r.snap().generation == seen["gen"]  # the inner call moved nothing
    assert r.snap().mode == "closed"
    r.fence.revoke()
    r.robot.reopen_admission()  # after the outer call has ended
    assert r.snap().mode == "open"


def test_a_clock_failure_on_entry_leaves_the_gate_as_it_was():
    r = Rig()
    r.close()
    before = r.snap()
    token = r.token()
    real, calls = r.clock.now, []

    def flaky():
        calls.append(1)
        if len(calls) == 2:  # the read after the fence check
            raise RuntimeError("clock unreadable")
        return real()

    r.clock.now = flaky
    result = r.robot.home_under_closed_gate(token=token)
    r.clock.now = real
    assert result.executed == "no" and r.env.http == []
    after = r.snap()
    assert (after.mode, after.generation) == ("closed", before.generation)
    r.fence.revoke()
    r.robot.reopen_admission()  # no stuck _home_active
    assert r.snap().mode == "open"


def test_an_async_exception_right_after_entry_still_closes_the_gate():
    class Interrupt(BaseException):
        pass

    r = Rig()
    r.close()
    token = r.token()
    real = r.robot._run_home

    def interrupted(*a, **k):
        raise Interrupt

    r.robot._run_home = interrupted
    with pytest.raises(Interrupt):
        r.robot.home_under_closed_gate(token=token)
    r.robot._run_home = real
    snap = r.snap()
    assert snap.mode == "closed" and snap.generation == 3
    r.fence.revoke()
    r.robot.reopen_admission()
    assert r.snap().mode == "open"


def test_another_thread_cannot_send_through_the_gated_path_or_disturb_the_home():
    import threading

    seen = {}

    def hook(r):
        def during(route):
            if route != "pose" or "done" in seen:
                return
            seen["done"] = True
            motion = r.robot._motion
            out = {}

            def attempt():
                for name, call in (
                    (
                        "send",
                        lambda: r.robot._send(r.action(), seen["token"], "home", True),
                    ),
                    (
                        "step",
                        lambda: r.robot.step(r.action(), token=seen["token"]),
                    ),
                ):
                    try:
                        out[name] = call()
                    except B.AdmissionRefused:
                        out[name] = "refused"

            t = threading.Thread(target=attempt)
            t.start()
            t.join()
            seen["out"] = out
            seen["motion_same"] = r.robot._motion == motion

        return during

    r = Rig()
    r.env.on_post = hook(r)
    r.close()
    seen["token"] = r.token()
    result = r.robot.home_under_closed_gate(token=seen["token"])
    assert result.executed == "yes"
    assert seen["out"]["send"] == "refused"
    assert seen["out"]["step"].executed == "no"
    assert seen["motion_same"] is True
    assert set(r.env.http) <= WHITELIST


# --- third review: late step() thread, clock on leave, entry order, nested purity ------


def _late_step_then(r, action):
    """After the home's first command (clearerr) went out, another thread calls
    step() (which clears the shared _sending flag), then ``action`` runs."""
    import threading

    seen = {}

    def during(route):
        if route != "clearerr" or seen:
            return
        seen["done"] = True
        t = threading.Thread(
            target=lambda: r.robot.step(
                r.action(),
                token=sm.MotionToken(
                    RUN, "tx-late", 3, "policy_steps", EP, 1, r.clock.now() + 10**12
                ),
            )
        )
        t.start()
        t.join()
        action()

    r.env.on_post = during


def test_a_late_step_thread_does_not_hide_a_sent_command_from_the_revoke_check():
    r = Rig()
    _late_step_then(r, r.fence.revoke)  # the stop, before the pose of this step
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert r.env.http == ["clearerr"]  # a command went out
    assert result.executed == "unknown"
    assert r.robot.latch_state() is not None
    assert r.robot.preflight()["ok"] is False


def test_a_late_step_thread_does_not_hide_a_sent_command_from_the_gate_branch():
    r = Rig()
    _late_step_then(r, r.robot.stop_admission)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert r.env.http == ["clearerr"]
    assert result.executed == "unknown" and r.robot.latch_state() is not None


def test_a_failed_leave_clock_closes_the_gate_with_no_closure_time(rig):
    mark = rig.close()
    old = witness(rig, mark)
    token = rig.token()
    real = rig.clock.now
    state = {"fail": False}

    def flaky():
        if state["fail"]:
            raise RuntimeError("clock gone")
        return real()

    rig.clock.now = flaky
    go = rig.robot.client._go_home

    def go_then_break_clock(env, args):
        go(env, args)
        state["fail"] = True

    rig.robot.client._go_home = go_then_break_clock
    try:
        # _settle_home reads the clock too: it fails and the call ends in unknown
        rig.robot.home_under_closed_gate(token=token)
    except RuntimeError:
        pass
    state["fail"] = False
    rig.clock.now = real
    snap = rig.snap()
    assert snap.mode == "closed" and snap.closed_from_ns is None
    rig.fence.revoke()
    fresh = rig.robot.stop_admission()  # idempotent: the same closure, no time
    assert fresh.closed_from_ns is None
    with pytest.raises(InterventionUnproven):
        witness(rig, fresh)
    with pytest.raises(InterventionUnproven):
        old(EP, 3, rig.clock.now() + 10**8)


def test_an_interrupt_while_the_window_opens_still_ends_closed(monkeypatch):
    r = Rig()
    r.close()
    token = r.token()
    real = B.threading.get_ident
    box = {"armed": True}

    def get_ident():
        if box["armed"]:
            box["armed"] = False
            raise KeyboardInterrupt
        return real()

    monkeypatch.setattr(B.threading, "get_ident", get_ident)
    with pytest.raises(KeyboardInterrupt):
        r.robot.home_under_closed_gate(token=token)
    monkeypatch.setattr(B.threading, "get_ident", real)
    snap = r.snap()
    assert snap.mode == "closed" and snap.generation >= 3
    r.fence.revoke()
    r.robot.reopen_admission()  # not refused for good
    assert r.snap().mode == "open"


def test_a_refused_nested_call_changes_nothing_at_all():
    seen = {}

    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            before = (r.fence.refused, r.robot.latched, r.snap())
            r.guard.status = "red"  # a precheck that ran would latch or count
            r.fence.revoke()  # and a fence check would count a refusal
            seen["inner"] = r.robot.home_under_closed_gate(token=r.token())
            r.guard.status = "ok"
            seen["same"] = before == (r.fence.refused, r.robot.latched, r.snap())

        return run

    r = Rig(go_home=go_home)
    r.close()
    r.robot.home_under_closed_gate(token=r.token())
    assert seen["inner"].executed == "no" and "already" in seen["inner"].detail
    assert seen["same"] is True


def _one_shot_interrupt_after_run_home(r, error=KeyboardInterrupt):
    """Make the first clock read after the home ran (the one `_leave_home_only`
    makes) raise `error`."""
    real_now = r.clock.now
    real_run = r.robot._run_home
    state = {"armed": False}

    def now():
        if state["armed"]:
            state["armed"] = False
            raise error
        return real_now()

    def run(*a, **k):
        result = real_run(*a, **k)
        state["armed"] = True
        return result

    r.clock.now = now
    r.robot._run_home = run
    return real_now


def test_an_interrupt_in_the_leave_clock_read_still_closes_the_gate_and_holds():
    r = Rig()
    r.close()
    token = r.token()
    _one_shot_interrupt_after_run_home(r)
    with pytest.raises(KeyboardInterrupt):
        r.robot.home_under_closed_gate(token=token)
    snap = r.snap()
    assert snap.mode == "closed" and snap.closed_from_ns is None
    assert r.robot._home_active is False
    assert r.robot._home_gate_token is None
    assert r.robot._home_thread is None and r.robot._home_marker is None
    assert r.robot.held is True
    r.fence.revoke()
    r.robot.reopen_admission()
    assert r.snap().mode == "open"


def test_an_interrupt_after_the_mode_closes_still_finishes_the_cleanup(monkeypatch):
    r = Rig()
    r.close()
    token = r.token()
    state = {"armed": False}
    stored = {}

    def get(self):
        return stored.get("v")

    def put(self, value):
        if value is None and state["armed"]:
            state["armed"] = False
            raise KeyboardInterrupt  # after mode=CLOSED, before _home_active=False
        stored["v"] = value

    monkeypatch.setattr(B.Robot, "_home_gate_token", property(get, put), raising=False)
    real_run = r.robot._run_home

    def run(*a, **k):
        result = real_run(*a, **k)
        state["armed"] = True
        return result

    r.robot._run_home = run
    with pytest.raises(KeyboardInterrupt):
        r.robot.home_under_closed_gate(token=token)
    assert r.snap().mode == "closed"
    assert r.robot._home_active is False
    assert r.robot._home_thread is None and r.robot._home_marker is None
    assert r.robot.held is True
    r.fence.revoke()
    r.robot.reopen_admission()
    assert r.snap().mode == "open"


def test_after_a_failed_leave_clock_a_new_interval_needs_reopen_then_stop():
    """Current behaviour, pinned: the gate is already closed with closed_from=None,
    so stop_admission() is a no-op; only reopen_admission() + stop_admission()
    builds an interval a witness accepts."""
    r = Rig()
    r.close()
    _one_shot_interrupt_after_run_home(r, RuntimeError)
    r.robot.home_under_closed_gate(token=r.token())
    snap = r.snap()
    assert snap.mode == "closed" and snap.closed_from_ns is None
    r.fence.revoke()
    same = r.robot.stop_admission()  # no-op: still no closure time
    assert same.closed_from_ns is None and same.generation == snap.generation
    with pytest.raises(InterventionUnproven):
        witness(r, same)
    r.robot.reopen_admission()
    r.clock.advance(10_000)
    fresh = r.robot.stop_admission()
    assert fresh.closed_from_ns is not None
    assert fresh.generation > snap.generation
    witness(r, fresh)  # builds


# --- the count of fenced motion POSTs: where it sits in fenced_post ---------------------


def test_a_post_that_raises_after_the_fence_counts_as_sent_and_locks():
    def go_home(r):
        def run(env, args):
            def boom(route):
                raise TimeoutError("no reply")  # the command may have arrived

            r.env.on_post = boom
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert r.env.http == ["clearerr"]
    assert result.executed == "unknown"
    assert r.robot.latch_state() is not None
    assert r.robot._motion_posts == 1


def test_a_post_the_gate_refuses_is_not_counted_as_sent():
    seen = {}

    def go_home(r):
        def run(env, args):
            before = r.robot._motion_posts
            try:
                r.env._post("close_gripper")  # not a home route
            finally:
                seen["delta"] = r.robot._motion_posts - before

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert seen["delta"] == 0 and r.env.http == []
    assert result.executed == "no"
    assert r.robot.latch_state() is None  # nothing was sent, nothing is locked
    assert r.snap().home_gate_rejected == 1


def test_a_post_the_fence_refuses_is_not_counted_as_sent():
    def go_home(r):
        real_step = r.env.step

        def revoking_step(action):
            r.fence.revoke()  # after _send's own checks, before the first POST
            r.robot._sending = False  # as if another thread's step() cleared it
            return real_step(action)

        def run(env, args):
            r.env.step = revoking_step
            env.step(np.array(HOME))

        return run

    r = Rig(go_home=go_home)
    r.close()
    result = r.robot.home_under_closed_gate(token=r.token())
    assert r.env.http == []  # the fence in fenced_post stopped the first POST
    assert r.robot._motion_posts == 0
    assert result.executed == "no"


def test_a_persistent_cleanup_error_does_not_spin_and_still_holds(monkeypatch):
    """A cleanup step that fails every time must not loop for ever: the call
    returns or raises in bounded time and the caller's finally still holds."""
    import threading

    r = Rig()
    r.close()
    token = r.token()
    stored = {}

    def get(self):
        return stored.get("v")

    def put(self, value):
        if value is None:
            raise RuntimeError("cleanup always fails")
        stored["v"] = value

    monkeypatch.setattr(B.Robot, "_home_gate_token", property(get, put), raising=False)
    out = {}

    def call():
        try:
            out["result"] = r.robot.home_under_closed_gate(token=token)
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "cleanup loop does not end"
    assert isinstance(out.get("error"), RuntimeError)
    assert r.robot.held is True


def test_an_interrupt_that_happens_once_is_retried_and_raised_after():
    r = Rig()
    r.close()
    token = r.token()
    inner = r.robot._admission_lock
    state = {"armed": False}
    real_run = r.robot._run_home

    def run(*a, **k):
        result = real_run(*a, **k)
        state["armed"] = True
        return result

    class OnceLock:
        """The first lock entry after the home raises KeyboardInterrupt."""

        def __enter__(self):
            if state["armed"]:
                state["armed"] = False
                raise KeyboardInterrupt
            return inner.__enter__()

        def __exit__(self, *a):
            return inner.__exit__(*a)

    r.robot._run_home = run
    r.robot._admission_lock = OnceLock()
    with pytest.raises(KeyboardInterrupt):
        r.robot.home_under_closed_gate(token=token)
    r.robot._admission_lock = inner
    assert r.snap().mode == "closed"
    assert r.robot._home_active is False and r.robot.held is True


def test_a_stop_inside_the_window_with_a_failed_clock_keeps_no_stale_closure_time():
    """stop_admission() inside the window changes the mode first and reads the
    clock last. If the read fails, closed_from must not be the timestamp of the
    closure before the window: home commands were released since then."""

    def go_home(r):
        def run(env, args):
            env.step(np.array(HOME))
            real = r.clock.now

            def broken():
                raise RuntimeError("clock gone")

            r.clock.now = broken
            try:
                r.robot.stop_admission()
            except RuntimeError:
                pass
            finally:
                r.clock.now = real

        return run

    r = Rig(go_home=go_home)
    old = r.close()
    r.clock.advance(10_000)
    r.robot.home_under_closed_gate(token=r.token())
    snap = r.snap()
    assert snap.mode == "closed" and snap.home_pose_released >= 1
    assert snap.closed_from_ns is None or snap.closed_from_ns >= snap.home_only_from_ns
    assert snap.closed_from_ns != old.closed_from_ns


def _run_in_thread(call):
    import threading

    out = {}

    def target():
        try:
            out["result"] = call()
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "the leave does not end"
    return out


def _fail_on_set(monkeypatch, name, make_error, *, only_none=False, once=False):
    """Replace attribute `name` of Robot by a property whose setter raises."""
    state = {"left": 1 if once else None, "armed": False, "value": None}

    def put(self, value):
        fires = state["armed"] and (value is None or not only_none)
        if fires and (state["left"] is None or state["left"] > 0):
            if state["left"] is not None:
                state["left"] -= 1
            raise make_error()
        state["value"] = value

    monkeypatch.setattr(
        B.Robot,
        name,
        property(lambda self: state["value"], put),
        raising=False,
    )
    return state


def _arm_after_run_home(r, state, extra=None):
    real_run = r.robot._run_home

    def run(*a, **k):
        result = real_run(*a, **k)
        state["armed"] = True
        if extra is not None:
            extra()
        return result

    r.robot._run_home = run


def test_a_persistent_keyboard_interrupt_is_retried_three_times_then_raised(
    monkeypatch,
):
    r = Rig()
    r.close()
    token = r.token()
    state = _fail_on_set(
        monkeypatch, "_home_gate_token", KeyboardInterrupt, only_none=True
    )
    inner = r.robot._admission_lock
    entries = {"n": 0}

    class Counting:
        def __enter__(self):
            if state["armed"]:
                entries["n"] += 1
            return inner.__enter__()

        def __exit__(self, *a):
            return inner.__exit__(*a)

    _arm_after_run_home(r, state)
    r.robot._admission_lock = Counting()
    out = _run_in_thread(lambda: r.robot.home_under_closed_gate(token=token))
    assert isinstance(out.get("error"), KeyboardInterrupt)
    assert entries["n"] == 3
    assert r.robot.held is True


def test_an_interrupt_at_the_generation_write_still_writes_time_and_generation(
    monkeypatch,
):
    r = Rig()
    r.close()
    token = r.token()
    initial = r.robot.__dict__["_admission_generation"]
    state = _fail_on_set(
        monkeypatch, "_admission_generation", KeyboardInterrupt, once=True
    )
    state["value"] = initial
    _arm_after_run_home(r, state)
    before = initial
    with pytest.raises(KeyboardInterrupt):
        r.robot.home_under_closed_gate(token=token)
    snap = r.snap()
    assert snap.mode == "closed"
    assert snap.closed_from_ns is not None
    assert snap.generation > before + 1  # entry and leave each changed it
    assert r.robot._home_active is False and r.robot.held is True


def test_the_first_failure_is_the_one_raised(monkeypatch):
    r = Rig()
    r.close()
    token = r.token()
    state = _fail_on_set(monkeypatch, "_home_gate_token", RuntimeError, only_none=True)
    # a KeyboardInterrupt from the leave clock read comes first
    real_now = r.clock.now
    box = {"armed": False}

    def now():
        if box["armed"]:
            box["armed"] = False
            raise KeyboardInterrupt
        return real_now()

    r.clock.now = now
    _arm_after_run_home(r, state, extra=lambda: box.update(armed=True))
    with pytest.raises(KeyboardInterrupt):
        r.robot.home_under_closed_gate(token=token)
    assert r.robot.held is True


def test_an_ordinary_exception_in_the_cleanup_is_not_retried(monkeypatch):
    r = Rig()
    r.close()
    token = r.token()
    state = _fail_on_set(monkeypatch, "_home_gate_token", RuntimeError, only_none=True)
    inner = r.robot._admission_lock
    entries = {"n": 0}

    class Counting:
        def __enter__(self):
            if state["armed"]:
                entries["n"] += 1
            return inner.__enter__()

        def __exit__(self, *a):
            return inner.__exit__(*a)

    _arm_after_run_home(r, state)
    r.robot._admission_lock = Counting()
    out = _run_in_thread(lambda: r.robot.home_under_closed_gate(token=token))
    assert isinstance(out.get("error"), RuntimeError)
    assert entries["n"] == 1
    assert r.robot.held is True
