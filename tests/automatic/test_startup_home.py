"""Opt-in startup home: fake bridge/journal only; no socket or robot."""

import functools
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fr3_real_support import HOME
from fr3_real_support import build as bridge_rig
from subtask_rig import build, sub_config
from test_fr3_bridge import no_network  # noqa: F401

from levi.automatic import fr3_bridge as B
from levi.automatic import fr3_run as R
from levi.automatic.journal import Journal
from levi.automatic.orchestrator import ConfigError

CONFIRM = {"object_handled": True, "path_clear": True, "gripper_will_open": True}


def setup(tmp_path, **over):
    r = build(tmp_path, cfg=sub_config(start_home=True, **over))
    b = bridge_rig()
    r.orch.robot = b.robot
    r.orch.clock = b.clock
    r.orch.fence = b.fence
    r.orch.startup_home_ready = lambda: True
    b.robot.stop_probe = lambda: (
        r.orch.startup_home_active and r.orch.startup_stop_seen.is_set()
    )
    return r, b


def request(r, command="startup-1", **over):
    return r.orch.request_start_home(
        command,
        expected_seq=over.pop("expected_seq", r.orch.journal.next_seq),
        confirmation=over.pop("confirmation", dict(CONFIRM)),
        **over,
    )


def test_start_home_success_is_journalled_before_post_and_stays_closed(tmp_path):
    r, b = setup(tmp_path)
    post = b.env._post
    tokens = []

    def observed(route, *args, **kwargs):
        if route == "pose":
            events = Journal.read(r.directory).events
            prepared = [e for e in events if e.record == "prepared"][-1]
            assert prepared.reason == "startup_home_requested"
            assert prepared.episode_id is None and prepared.episode_role is None
            assert prepared.action.non_idempotent is True
            assert prepared.action.step == prepared.sequence_no
            tokens.append(b.fence.current)
        return post(route, *args, **kwargs)

    b.env._post = observed
    result = request(r)
    assert result.ok and r.orch.state == "PREFLIGHT"
    assert b.robot.admission_snapshot().closed
    assert b.fence.current is None
    assert tokens[0].episode_id.endswith(":startup-home")
    assert tokens[0].policy_epoch is None
    assert r.orch.ctx is None
    events = Journal.read(r.directory).events
    assert not any(e.episode_id or e.episode_result for e in events)
    assert [e.record for e in events if e.reason == "startup_home_requested"] == [
        "prepared",
        "committed",
    ]
    posts = b.env.routes.count("pose")
    assert not request(r).ok
    assert not request(r, "different-command").ok
    assert b.env.routes.count("pose") == posts


@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "missing",
        "partial",
        "enter",
        "stale",
        "not_ready",
        "red",
        "latch",
        "closed",
        "stop",
    ],
)
def test_start_home_refusals_send_nothing(tmp_path, case):
    r, b = setup(tmp_path)
    kwargs = {}
    if case == "disabled":
        r.orch.config = replace(r.orch.config, start_home=False)
    elif case == "missing":
        kwargs["confirmation"] = None
    elif case == "partial":
        kwargs["confirmation"] = {"object_handled": True}
    elif case == "enter":
        kwargs["confirmation"] = "\n"
    elif case == "stale":
        kwargs["expected_seq"] = r.orch.journal.next_seq - 1
    elif case == "not_ready":
        r.orch.startup_home_ready = None
    elif case == "red":
        b.guard.status = "red"
    elif case == "latch":
        b.robot.latched = "fault"
    elif case == "closed":
        b.robot.stop_admission()
    else:
        r.orch.stop("before-home")
    assert not request(r, **kwargs).ok
    assert "pose" not in b.env.routes and "clearerr" not in b.env.routes
    assert b.fence.current is None


@pytest.mark.parametrize(
    "executed,state", [("no", "WAIT_HUMAN"), ("unknown", "FAULT_LOCKED")]
)
def test_unsent_or_unknown_home_never_retries_in_same_run(tmp_path, executed, state):
    r, b = setup(tmp_path)
    b.robot.home_under_closed_gate = lambda **kw: B.HomeResult(executed, "refused")
    assert not request(r).ok
    assert r.orch.state == state
    assert b.robot.admission_snapshot().closed and b.fence.current is None
    assert not request(r, "another-command").ok
    assert not r.orch.resume(
        "resume",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    ).ok
    assert "pose" not in b.env.routes


@pytest.mark.parametrize(
    "point", ["after_prepared", "after_execute", "after_acknowledged"]
)
def test_crash_at_home_transaction_never_replays(tmp_path, point):
    from aeri_harness import fake

    from levi.automatic.journal import process_identity

    r, b = setup(tmp_path)

    def crash(where):
        if where == point:
            raise fake.SimulatedCrash(where)

    r.orch.crash_hook = crash
    with pytest.raises(fake.SimulatedCrash):
        request(r)
    r.orch.crash_hook = None
    count = b.env.routes.count("pose")
    authority = {
        "principal_kind": "recovery",
        "principal_id": "recovery",
        "session_id": "recovery",
        "process": process_identity(),
        "command_id": None,
    }
    r.orch.recovery = r.orch.journal.recover(authority=authority)
    assert r.orch.state == "FAULT_LOCKED"
    assert not request(r, "new-sequence").ok
    assert not r.orch.resume(
        "resume",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    ).ok
    assert b.env.routes.count("pose") == count
    assert all(e.episode_id is None for e in Journal.read(r.directory).events)


@pytest.mark.parametrize("when", ["before_first_post", "during_home", "at_target"])
def test_operator_stop_reaches_bridge_and_prevents_later_posts(tmp_path, when):
    r, b = setup(tmp_path)
    if when == "before_first_post":
        r.orch.crash_hook = lambda point: (
            r.orch.stop("stop-home") if point == "after_prepared" else None
        )
    elif when == "during_home":
        original = b.env._post

        def stop_after_pose(route, *args, **kwargs):
            value = original(route, *args, **kwargs)
            if route == "pose":
                r.orch.stop("stop-home")
            return value

        b.env._post = stop_after_pose

        def two_steps(env, args):
            env.step(np.array([*HOME, 1.0]))
            env.step(np.array([*HOME, 1.0]))

        b.robot.client._go_home = two_steps
    else:
        b.env.on_read = lambda n: (
            r.orch.stop("stop-home") if b.env.cmd_ns is not None else None
        )
    assert not request(r).ok
    assert r.orch.state == "FAULT_LOCKED"
    assert b.env.routes.count("pose") == (0 if when == "before_first_post" else 1)
    assert b.robot.admission_snapshot().closed and b.fence.current is None


def test_release_is_separate_and_consumes_owned_closure_once(tmp_path):
    r, b = setup(tmp_path)
    assert request(r).ok
    r.orch._subtask_inventory = lambda: True
    planned = []
    r.orch._plan_forward = lambda: planned.append(True)

    def release(*args):
        assert b.robot.admission_snapshot().closed
        return "released"

    r.gate.release_round = release
    r.orch._decide_subtask()
    assert planned == [True]
    assert not b.robot.admission_snapshot().closed
    assert r.orch._startup_gate_mark is None


@pytest.mark.parametrize("change", ["stop", "closure", "latch", "red", "moved"])
def test_release_cannot_refresh_a_changed_closure(tmp_path, change):
    r, b = setup(tmp_path)
    assert request(r).ok
    r.orch._subtask_inventory = lambda: True
    r.orch._plan_forward = lambda: pytest.fail("lost closure started policy")

    def release(*args):
        if change == "stop":
            r.orch.stop("late-stop")
        elif change == "closure":
            b.robot.reopen_admission()
            b.robot.stop_admission()
        elif change == "latch":
            b.robot.latched = "fault"
        elif change == "red":
            b.guard.status = "red"
        else:
            b.env.pose_fn = lambda since: np.array([HOME[0] + 0.1, *HOME[1:], 1.0])
        return "released"

    r.gate.release_round = release
    with pytest.raises((ValueError, B.AdmissionRefused)):
        r.orch._decide_subtask()
    assert b.robot.admission_snapshot().closed


def test_start_gate_optin_only_defers_readable_deviations():
    b = bridge_rig()
    b.env.grip_fn = lambda since: 0.2
    with pytest.raises(ValueError, match="start pose refused"):
        R.start_gate(b.robot)
    assert (
        "confirmation required"
        in R.start_gate(b.robot, allow_start_home=True)["start_pose_checked"]
    )
    b.env.grip_fn = lambda since: float("nan")
    with pytest.raises(ValueError):
        R.start_gate(b.robot, allow_start_home=True)
    assert "pose" not in b.env.routes


def test_other_strategies_cannot_enable_start_home():
    with pytest.raises(ConfigError, match="subtask_loop"):
        sub_config(reset_strategy="human_assisted", start_home=True)


def test_pending_note_cas_change_never_refreshes_sequence_or_sends(tmp_path):
    r, b = setup(tmp_path)
    sequence = r.orch.journal.next_seq
    r.orch.note("queued_before_confirmation", "a pending note changes prepare CAS")
    assert not request(r, expected_seq=sequence).ok
    assert "pose" not in b.env.routes
    assert b.fence.current is None


def test_preflight_without_live_readiness_never_prompts_or_advances(tmp_path):
    r, b = setup(tmp_path)
    r.orch.startup_home_ready = lambda: False
    r.gate.confirm_start_home = lambda: pytest.fail("confirmation before safety ready")
    r.orch._preflight()
    assert r.orch.state == "WAIT_HUMAN"
    assert "pose" not in b.env.routes


def test_preflight_requires_fresh_confirmation_before_home(tmp_path):
    r, b = setup(tmp_path)
    b.env.pose_fn = lambda since: np.array(
        [HOME[0] + (0.1 if since is None else 0), *HOME[1:], 1.0]
    )
    observed = []

    def confirm():
        observed.append(r.orch.state)
        assert "pose" not in b.env.routes
        return dict(CONFIRM)

    r.gate.confirm_start_home = confirm
    r.orch._preflight()
    assert observed == ["PREFLIGHT"]
    assert r.orch.state == "PREFLIGHT"
    assert b.robot.admission_snapshot().closed


@pytest.mark.parametrize("homed", [False, True])
def test_completed_startup_does_not_change_later_stop_resume(tmp_path, homed):
    r, b = setup(tmp_path)
    if homed:
        assert request(r).ok
        r.orch._subtask_inventory = lambda: True
        r.orch._plan_forward = lambda: None
        r.gate.release_round = lambda *args: "released"
        r.orch._decide_subtask()
    else:
        r.orch._preflight()
    assert r.orch.startup_home_active is False
    assert not b.robot.admission_snapshot().closed
    assert r.orch.stop("ordinary-round-stop").ok
    assert not b.robot.stopped()
    assert not r.orch.startup_stop_seen.is_set()
    assert r.orch._stop_before_start()
    assert r.orch.state == "WAIT_HUMAN"
    assert r.orch.resume(
        "ordinary-resume",
        expected_seq=r.orch.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    ).ok
    assert r.orch.state == "PREFLIGHT"
    assert not b.robot.stopped()
    assert not request(r, "must-not-reenter-startup").ok


def test_later_episode_recovery_keeps_ordinary_resume_semantics(tmp_path):
    from levi.automatic.orchestrator import Orchestrator

    r, b = setup(tmp_path)
    orch = r.orch
    orch._preflight()  # already home, no startup motion
    journal = orch.journal
    journal.prepare(
        "FORWARD_ACTIVE",
        "subtask_dispatched",
        kind="policy_steps",
        authority=orch._auth(),
        control_epoch=journal.control_epoch + 1,
        non_idempotent=True,
        step=0,
        episode_id=orch.config.run_id + ".forward.0001",
        episode_role="forward",
        policy_epoch=1,
    )
    journal.acknowledge("yes", "robot_server", "ok", authority=orch._auth())
    journal.commit(authority=orch._auth())
    parts = {
        "robot": b.robot,
        "policy": orch.policy,
        "recorder": orch.recorder,
        "events": orch.events,
        "verifier": orch.verifier,
        "scene": orch.scene,
        "clock": b.clock,
        "fence": b.fence,
        "subtask_vlm": orch.subvlm,
        "vlm_power": orch.power,
        "gate": orch.gate,
    }
    orch.close()
    restored = Orchestrator.restore(r.directory, orch.config, **parts)
    assert restored.state == "FAULT_LOCKED"
    assert restored.startup_home_active is False
    assert restored.resume(
        "recovered-round-resume",
        expected_seq=restored.journal.next_seq,
        environment_handled=True,
        health_rechecked=True,
    ).ok
    assert restored.state == "PREFLIGHT"
    assert not restored.request_start_home(
        "no-replay",
        expected_seq=restored.journal.next_seq,
        confirmation=dict(CONFIRM),
    ).ok
    assert "pose" not in b.env.routes


def test_stop_registration_does_not_wait_for_release_pose_read(tmp_path):
    import threading

    r, b = setup(tmp_path)
    assert request(r).ok
    r.orch._subtask_inventory = lambda: True
    r.orch._plan_forward = lambda: pytest.fail("stopped closure released policy")
    entered, unblock, stopped = threading.Event(), threading.Event(), threading.Event()
    errors = []
    reading_release = False

    def release(*args):
        nonlocal reading_release
        reading_release = True
        return "released"

    r.gate.release_round = release

    def block(n):
        if reading_release:
            entered.set()
            assert unblock.wait(2), "test did not release fake pose read"

    b.env.on_read = block

    def drive():
        try:
            r.orch._decide_subtask()
        except (ValueError, B.AdmissionRefused) as exc:
            errors.append(exc)

    def stop():
        r.orch.stop("stop-during-read")
        stopped.set()

    worker = threading.Thread(target=drive)
    stopper = threading.Thread(target=stop)
    worker.start()
    try:
        assert entered.wait(2)
        stopper.start()
        assert stopped.wait(1), "pose I/O blocked stop registration"
        assert r.orch.startup_stop_seen.is_set()
    finally:
        unblock.set()
        worker.join(2)
        if stopper.ident is not None:
            stopper.join(2)
    assert not worker.is_alive() and not stopper.is_alive()
    assert errors and isinstance(errors[0], ValueError)
    assert b.robot.admission_snapshot().closed


def test_execute_frozen_start_home_mismatch_refuses_before_robot_lock(monkeypatch):
    from types import SimpleNamespace as NS

    monkeypatch.setattr(R, "RobotLock", lambda: pytest.fail("mismatch took robot lock"))
    with pytest.raises(ValueError, match="frozen plan"):
        R.execute({"config": NS(start_home=True), "plan": {"run": {}}}, {})


@pytest.mark.parametrize("mode", ["not_ready", "dead_thread", "heartbeat_failure"])
def test_production_ready_binding_rejects_incomplete_heartbeat(
    tmp_path, monkeypatch, mode
):
    import inspect
    import threading

    from test_fr3_subtask_entry import subtask_rig

    from levi.automatic.journal import plan_digest
    from levi.automatic.orchestrator import Orchestrator

    world, _, _ = subtask_rig(tmp_path, monkeypatch)
    plan = {**world.job["plan"], "run": {"start_home": True}}
    digest = plan_digest(plan)
    world.job.update(
        plan=plan,
        plan_sha256=digest,
        config=replace(world.job["config"], start_home=True, plan_sha256=digest),
    )
    original_thread = threading.Thread
    made = []

    class Heartbeat:
        def __init__(self, target):
            self.target = target
            self.alive = mode == "not_ready"

        def start(self):
            ready = inspect.getclosurevars(self.target).nonlocals["heartbeat_ready"]
            monkeypatch.setattr(ready, "wait", lambda timeout=None: ready.is_set())
            if mode == "dead_thread":
                ready.set()  # A past ready indication cannot prove a live thread.
            elif mode == "heartbeat_failure":
                self.target()  # The production first-heartbeat failure path, synchronously.

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            pass

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") == "aeri-C2-heartbeat":
            fake = Heartbeat(kwargs["target"])
            made.append(fake)
            return fake
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(R.threading, "Thread", thread_factory)
    if mode == "heartbeat_failure":

        def fail(*args, **kwargs):
            raise OSError("injected session heartbeat failure")

        monkeypatch.setattr(R.SessionFiles, "heartbeat", fail)
    observed = []

    def run(orch, *args, **kwargs):
        observed.append(orch.startup_home_ready())
        assert orch._startup_ready() is False
        if mode == "heartbeat_failure":
            assert orch.startup_stop_seen.is_set()
            assert orch.robot.stopped()
        orch._preflight()
        return orch.state

    monkeypatch.setattr(Orchestrator, "run", run)
    assert (
        R.execute(world.job, world.binding, load_modules=lambda b: world.modules)
        == "WAIT_HUMAN"
    )
    assert made and observed == [False]
    motion = {"pose", "clearerr", "open_gripper", "close_gripper"}
    assert not any(c[0] == "route" and c[1] in motion for c in world.calls)


def startup_reverse_entry(tmp_path, monkeypatch, *, red, fault=None):
    """Production entry and one clock; only module I/O and operator inputs fake.

    Three-key flushing is tested separately by test_console_subtask. Here the
    operator returns the structured confirmation at the production prompt.
    """
    from fr3_real_support import offset_start
    from test_fr3_subtask_entry import Operator, subtask_rig

    from levi.automatic import console as C
    from levi.automatic.journal import plan_digest

    world, _, queries = subtask_rig(tmp_path, monkeypatch)
    plan = {
        **world.job["plan"],
        "run": {**world.job["plan"].get("run", {}), "start_home": True, "episodes": 2},
    }
    digest = plan_digest(plan)
    world.job.update(
        plan=plan,
        plan_sha256=digest,
        config=replace(
            world.job["config"],
            start_home=True,
            episodes=2,
            home_after_operator_stop=True,
            plan_sha256=digest,
        ),
    )
    offset_start(world, monkeypatch, 0.02)
    observed = {"confirm": [], "release": [], "wait": [], "reverse": [], "red": False}
    operators = []
    cleanup_fault = lambda: None
    if fault is not None:
        cleanup_fault = install_entry_fault(world, monkeypatch, observed, fault)
    guard_type = world.modules[1].Fr3Guard
    guard_check = guard_type.check

    def check_guard(guard, force=False):
        reading = guard_check(guard, force=force)
        if observed["red"]:
            reading.status = "red"
        return reading

    monkeypatch.setattr(guard_type, "check", check_guard)

    def motion_count():
        return world.calls.count(("route", "pose"))

    class ChainOperator(Operator):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            operators.append(self)

        def confirm_start_home(self):
            observed["confirm"].append((self.orch.state, motion_count(), len(queries)))
            return dict(CONFIRM)

        def release_round(self, round_no, total):
            observed["release"].append(
                {
                    "round": round_no,
                    "total": total,
                    "motions": motion_count(),
                    "queries": len(queries),
                    "closed": self.robot.admission_snapshot().closed,
                    "startup_commits": sum(
                        e.record == "committed" and e.reason == "startup_home_requested"
                        for e in self.orch.journal.events
                    ),
                }
            )
            return "released" if len(observed["release"]) == 1 else "refused"

        def on_state(self, state, info):
            super().on_state(state, info)
            if state != "RESET_VERIFY" or observed["reverse"]:
                return
            scope = self.orch.reverse_human_scope()
            observed["reverse"].append(
                {
                    "scope": scope,
                    "startup_active": self.orch.startup_home_active,
                    "startup_stop": self.orch.startup_stop_seen.is_set(),
                    "motions": motion_count(),
                }
            )
            if fault == "heartbeat":
                observed["inject_heartbeat"]()
                observed["heartbeat_stopped"] = self.robot.stopped()
                observed["heartbeat_stop"] = self.orch.stop_requested()
                return
            if scope is not None:
                observed["request"] = self.orch.reverse_human_request(
                    "chain-human", **scope
                )
                observed["red"] = red

        def wait_human(self):
            observed["wait"].append(
                {
                    "state": self.orch.state,
                    "scope": self.orch.reverse_human_scope(),
                    "motions": motion_count(),
                    "queries": len(queries),
                    "startup_active": self.orch.startup_home_active,
                    "startup_stop": self.orch.startup_stop_seen.is_set(),
                }
            )
            if fault is not None or red or len(observed["wait"]) != 1:
                return False
            observed["resume"] = self.orch.resume(
                "chain-resume",
                expected_seq=self.orch.journal.next_seq,
                environment_handled=True,
                health_rechecked=True,
            )
            scope = observed["reverse"][0]["scope"]
            observed["late"] = self.orch.reverse_human_request("chain-late", **scope)
            observed["restart_home"] = self.orch.request_start_home(
                "chain-no-startup-replay",
                expected_seq=self.orch.journal.next_seq,
                confirmation=dict(CONFIRM),
            )
            return observed["resume"].ok

    monkeypatch.setattr(C, "from_environment", lambda **kwargs: ChainOperator(**kwargs))
    try:
        outcome = R.execute(
            world.job, world.binding, load_modules=lambda _: world.modules
        )
    finally:
        cleanup_fault()
    directory = Path(world.job["rollout_root"]) / ".aeri/runs/real-test"
    events = Journal.read(directory).events
    # Listener exceptions are deliberately caught by production _tell; don't
    # let a failed test callback silently masquerade as a completed scenario.
    assert not any(
        e.record == "note" and e.note.code == "session_write_failed" for e in events
    )
    assert len(operators) == 1
    if fault != "identity":
        assert len(observed["reverse"]) == 1
        assert observed["reverse"][0]["startup_active"] is False
        assert observed["reverse"][0]["startup_stop"] is False
    if fault is None:
        assert observed["request"].ok
    assert len(observed["confirm"]) == 1
    assert observed["confirm"][0][0] == "PREFLIGHT"
    assert observed["confirm"][0][1] == 0
    assert observed["release"][0]["closed"] is True
    assert observed["release"][0]["startup_commits"] == 1
    startup = [
        e
        for e in events
        if e.record == "committed" and e.reason == "startup_home_requested"
    ]
    assert len(startup) == 1
    first_policy = next(
        e for e in events if e.record == "committed" and e.to_state == "FORWARD_ACTIVE"
    )
    assert startup[0].sequence_no < first_policy.sequence_no
    assert not (directory / "labels/round_label.jsonl").exists()
    assert not (directory / "labels/operator_label.jsonl").exists()
    return outcome, observed, events, world, operators[0]


def test_startup_then_reverse_human_resume_requires_new_release(tmp_path, monkeypatch):
    outcome, observed, events, world, operator = startup_reverse_entry(
        tmp_path, monkeypatch, red=False
    )
    assert outcome == "WAIT_HUMAN"
    assert observed["resume"].ok
    assert observed["late"].ok is False and observed["late"].code == "stale_reverse"
    assert observed["restart_home"].ok is False
    assert [r["round"] for r in observed["release"]] == [1, 2]
    assert len(observed["wait"]) == 2
    first_wait = observed["wait"][0]
    assert first_wait["scope"] is None
    assert first_wait["startup_active"] is False and first_wait["startup_stop"] is False
    second_release = observed["release"][1]
    assert second_release["motions"] == first_wait["motions"]
    assert second_release["queries"] == first_wait["queries"]
    assert world.calls.count(("route", "pose")) == first_wait["motions"]
    assert operator.orch.reverse_human_scope() is None
    assert (
        len(
            {
                e.episode_id
                for e in events
                if e.record == "committed" and e.to_state == "FORWARD_ACTIVE"
            }
        )
        == 1
    )
    assert any(
        e.record == "committed"
        and e.to_state == "WAIT_HUMAN"
        and e.reason == "operator_stop"
        for e in events
    )


def test_startup_then_reverse_human_red_guard_locks_without_recovery(
    tmp_path, monkeypatch
):
    outcome, observed, events, world, operator = startup_reverse_entry(
        tmp_path, monkeypatch, red=True
    )
    assert outcome == "FAULT_LOCKED"
    assert operator.orch.fault_reason == "safety_stop"
    assert observed["wait"] == [] and "resume" not in observed
    assert len(observed["release"]) == 1
    assert world.calls.count(("route", "pose")) == observed["reverse"][0]["motions"]
    assert operator.orch.reverse_human_scope() is None
    assert not any(
        e.record == "committed" and e.to_state == "WAIT_HUMAN" for e in events
    )


def install_entry_fault(world, monkeypatch, observed, fault):
    """Inject module I/O failures; never replace execute, acquire or stop outcomes."""
    if fault == "identity":
        from levi.automatic import subtask_vlm as V

        post = V._http_post
        client = world.modules[0].websocket_client_policy.WebsocketClientPolicy
        metadata = client.get_server_metadata
        acquire = B.Policy.acquire

        def response(url, body, timeout):
            answer = post(url, body, timeout)
            text = body["messages"][0]["content"][-1]["text"]
            if "Direction: reverse" in text:
                observed["identity_injected"] = world.calls.count(("route", "pose"))
            return answer

        def server_metadata(self):
            value = metadata(self)
            if "identity_injected" in observed:
                observed["bad_metadata"] = observed.get("bad_metadata", 0) + 1
                return {**value, "config": "wrong"}
            return value

        @functools.wraps(acquire)  # orchestrator inspects acquire for `prompt`
        def checked_acquire(self, *args, **kwargs):
            result = acquire(self, *args, **kwargs)
            if "identity_injected" in observed:
                observed["acquire_result"] = result
            return result

        monkeypatch.setattr(V, "_http_post", response)
        monkeypatch.setattr(client, "get_server_metadata", server_metadata)
        monkeypatch.setattr(B.Policy, "acquire", checked_acquire)
        return lambda: None

    assert fault == "heartbeat"
    import itertools
    import threading

    trigger = threading.Event()
    failed = threading.Event()
    threads = []
    heartbeat = R.SessionFiles.heartbeat
    real_time = R.time
    ticks = itertools.count(0, 3)

    class HeartbeatTime:
        def monotonic(self):
            return next(ticks)

        def __getattr__(self, name):
            return getattr(real_time, name)

    def session_heartbeat(self):
        observed["heartbeat_calls"] = observed.get("heartbeat_calls", 0) + 1
        if observed["heartbeat_calls"] == 1:
            thread = threading.current_thread()
            assert thread.name == "aeri-C2-heartbeat"
            threads.append(thread)
            return heartbeat(self)
        # Bounded even if the scenario never reaches RESET_VERIFY. This is
        # the production heartbeat thread, not an extra test worker.
        observed["heartbeat_triggered"] = trigger.wait(5)
        failed.set()
        raise OSError("injected running session heartbeat failure")

    def inject():
        trigger.set()
        assert failed.wait(5)
        assert len(threads) == 1
        # The listener can hold orch._lock; stop waits only STOP_LOCK_WAIT_S.
        threads[0].join(timeout=5)
        assert not threads[0].is_alive()

    def cleanup():
        trigger.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

    monkeypatch.setattr(R, "time", HeartbeatTime())
    monkeypatch.setattr(R.SessionFiles, "heartbeat", session_heartbeat)
    observed["inject_heartbeat"] = inject
    return cleanup


def test_startup_reverse_policy_identity_mismatch_waits_without_motion(
    tmp_path, monkeypatch
):
    outcome, observed, events, world, _ = startup_reverse_entry(
        tmp_path, monkeypatch, red=False, fault="identity"
    )
    assert outcome == "WAIT_HUMAN"
    assert observed["bad_metadata"] == 1
    assert observed["acquire_result"]["code"] == "deployment_mismatch"
    assert observed["acquire_result"]["kind"] == "unavailable"
    assert world.calls.count(("route", "pose")) == observed["identity_injected"]
    assert len(observed["release"]) == 1 and len(observed["wait"]) == 1
    assert observed["reverse"] == [] and "resume" not in observed
    assert any(
        e.record == "committed"
        and e.to_state == "WAIT_HUMAN"
        and e.reason == "policy_error"
        for e in events
    )
    assert not any(
        e.record == "committed"
        and (e.to_state == "RESET_ACTIVE" or e.reason == "operator_stop")
        for e in events
    )


def test_startup_reverse_session_heartbeat_failure_cannot_resume_or_home(
    tmp_path, monkeypatch
):
    outcome, observed, events, world, operator = startup_reverse_entry(
        tmp_path, monkeypatch, red=False, fault="heartbeat"
    )
    assert observed["heartbeat_calls"] == 2 and observed["heartbeat_triggered"]
    assert observed["heartbeat_stopped"] and observed["heartbeat_stop"]
    assert "session-heartbeat-failed" in operator.orch._stops
    assert outcome == "FAULT_LOCKED" and operator.orch.fault_reason == "home_failed"
    assert observed["wait"] == [] and "resume" not in observed
    assert len(observed["release"]) == 1
    assert world.calls.count(("route", "pose")) == observed["reverse"][0]["motions"]
    assert not any(
        e.record == "committed" and e.to_state == "WAIT_HUMAN" for e in events
    )
