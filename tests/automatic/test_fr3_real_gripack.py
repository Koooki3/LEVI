"""Queued gripper replies are confirmed by measurement (L2-REAL-GRIPACK).

Fake env and fake clock; no socket, robot or GPU. Thresholds are the reviewed ones.
"""

import numpy as np
import pytest
from fr3_real_support import HOME, build, step_with, token
from test_fr3_bridge import no_network, rig  # noqa: F401

from levi.automatic import fr3_bridge as B


@pytest.mark.parametrize(
    "route,reply",
    [
        ("open_gripper", "Opened"),
        ("open_gripper", "open gripper target already satisfied"),
        ("close_gripper", "Closed"),
        ("close_gripper", "close gripper target already satisfied"),
    ],
)
def test_confirmed_gripper_replies_continue_without_extra_reads(route, reply):
    r = build()
    out = step_with(r, route, reply)
    assert out.executed == "yes" and r.robot.latched is None
    reads = r.env.reads
    r.env.gripper_command = None
    assert (
        r.robot.step(np.array([*HOME, 1.0]), token=token(r, "policy_steps")).executed
        == "yes"
    )
    assert r.env.reads - reads == 1  # only _send's own pose read


@pytest.mark.parametrize(
    "route,reply,pos",
    [
        ("open_gripper", "open gripper goal already in flight", 1.0),
        ("open_gripper", "open gripper request debounced", 0.95),
        ("close_gripper", "close gripper goal already in flight", 0.4),
        ("close_gripper", "close gripper request debounced", 0.75),
    ],
)
def test_queued_gripper_reply_is_confirmed_by_the_measured_position(route, reply, pos):
    r = build(gripper_confirm_timeout_s=2.0)
    out = step_with(r, route, reply, grip=lambda since: pos)
    assert out.executed == "yes" and r.robot.latched is None
    assert dict(out.notes)["gripper_reply_queued"].count(reply) == 1  # verbatim
    nxt = r.robot.step(np.array([*HOME, 1.0]), token=token(r, "policy_steps"))
    assert nxt.executed == "yes" and r.robot.latched is None
    assert "gripper_reply_confirmed" in dict(nxt.notes)
    assert reply in dict(nxt.notes)["gripper_reply_confirmed"]


def test_retained_close_is_confirmed_when_the_gripper_was_open_and_is_now_closed():
    r = build(gripper_confirm_timeout_s=2.0)
    reply = "close gripper target retained while busy"
    out = step_with(
        r, "close_gripper", reply, grip=lambda since: 1.0 if since is None else 0.3
    )
    assert out.executed == "yes"
    r.env.gripper_command = None
    nxt = r.robot.step(np.array([*HOME, 1.0]), token=token(r, "policy_steps"))
    assert nxt.executed == "yes", nxt.detail


def test_queued_gripper_reply_not_confirmed_by_measurement_locks_before_next_motion():
    r = build(gripper_confirm_timeout_s=1.0, settle_poll_s=0.1)
    reply = "open gripper goal already in flight"
    out = step_with(r, "open_gripper", reply, grip=lambda since: 0.1)  # stays closed
    assert out.executed == "yes"  # accepted, pending
    poses = r.env.routes.count("pose")
    began = r.clock.now()
    nxt = r.robot.step(np.array([*HOME, 1.0]), token=token(r, "policy_steps"))
    assert nxt.executed == "no" and r.robot.latched
    assert reply in nxt.detail and "gripper=0.1" in nxt.detail
    assert r.env.routes.count("pose") == poses  # the next motion was never sent
    assert 1.0 <= (r.clock.now() - began) / 1e9 <= 1.3  # bounded


def test_retained_close_with_gripper_already_closed_and_still_is_not_proof():
    r = build(gripper_confirm_timeout_s=0.5, settle_poll_s=0.1)
    reply = "close gripper target retained while busy"
    # closed (0.3) before and after: an opposite goal may still be running and
    # the retained close not yet dispatched -> not delivered as far as we know.
    out = step_with(r, "close_gripper", reply, grip=lambda since: 0.3)
    assert out.executed == "yes"
    nxt = r.robot.step(np.array([*HOME, 1.0]), token=token(r, "policy_steps"))
    assert nxt.executed == "no" and reply in nxt.detail


def test_retained_close_that_opens_and_closes_again_is_confirmed():
    r = build(gripper_confirm_timeout_s=3.0, settle_poll_s=0.1)
    reply = "close gripper target retained while busy"

    def wave(since):  # closed, opened by the in-flight goal, closed again
        if since is None or since < 0.3:
            return 0.3
        return 0.95 if since < 0.6 else 0.3

    out = step_with(r, "close_gripper", reply, grip=wave)
    assert out.executed == "yes"
    r.env.gripper_command = None
    nxt = r.robot.step(np.array([*HOME, 1.0]), token=token(r, "policy_steps"))
    assert nxt.executed == "yes", nxt.detail


@pytest.mark.parametrize(
    "reply",
    [
        "open gripper action server is not available",
        {"ok": False, "error": "no"},
        "something the server never said",
        None,
    ],
)
def test_negative_or_unknown_gripper_reply_still_locks_with_the_raw_text(reply):
    r = build()
    out = step_with(r, "open_gripper", reply)
    assert out.executed == "unknown" and r.robot.latched
    assert "open_gripper" in out.detail
    assert str(reply) in out.detail or repr(reply) in out.detail


def test_a_queue_text_for_the_wrong_route_is_not_accepted():
    r = build()
    out = step_with(r, "open_gripper", "close gripper goal already in flight")
    assert out.executed == "unknown" and r.robot.latched


def test_queued_reply_text_must_match_exactly():
    r = build()
    out = step_with(
        r, "open_gripper", "open gripper goal already in flight; ignoring failure"
    )
    assert out.executed == "unknown"


def test_stop_during_gripper_confirmation_locks_at_once():
    r = build(gripper_confirm_timeout_s=30.0, settle_poll_s=0.1)
    out = step_with(
        r, "open_gripper", "open gripper goal already in flight", grip=lambda s: 0.1
    )
    assert out.executed == "yes"
    r.env.on_read = lambda n: r.robot.stop_event.set() if n >= 3 else None
    began = r.clock.now()
    nxt = r.robot.step(np.array([*HOME, 1.0]), token=token(r, "policy_steps"))
    assert nxt.executed == "no" and r.robot.latched
    assert (r.clock.now() - began) / 1e9 < 1.0


def test_classifier_accepts_only_the_three_reviewed_queue_texts():
    queued = [
        "open gripper goal already in flight",
        "open gripper target retained while busy",
        "open gripper request debounced",
    ]
    assert [B.reply_status("open_gripper", t) for t in queued] == ["pending"] * 3
    assert (
        B.reply_status("close_gripper_slow", "close gripper request debounced")
        == "pending"
    )
    assert B.reply_status("open_gripper", "Opened") == "ack"
    assert B.reply_status("pose", "open gripper request debounced") == "bad"
    assert B.reply_status("move_gripper", "open gripper request debounced") == "bad"
    assert B.acknowledged("open_gripper", queued[0]) is False  # strict check unchanged


@pytest.mark.parametrize("position,ok", [(0.95, True), (0.1, False)])
def test_last_queued_gripper_reply_is_checked_without_a_next_motion(position, ok):
    r = build(gripper_confirm_timeout_s=0.3, settle_poll_s=0.1)
    reply = "open gripper goal already in flight"
    assert (
        step_with(r, "open_gripper", reply, grip=lambda since: position).executed
        == "yes"
    )
    motions = len(r.env.routes)
    r.robot.hold()
    if ok:
        notes = r.robot.confirm_pending_gripper()
        assert reply in dict(notes)["gripper_reply_confirmed"]
        assert r.robot._grip_pending is None
    else:
        with pytest.raises(
            ValueError, match="not confirmed.*open gripper goal already in flight"
        ):
            r.robot.confirm_pending_gripper()
        assert r.robot.latched is not None
    assert "pose" not in r.env.routes[motions:]


@pytest.mark.parametrize("position", [float("nan"), float("inf"), -1.0, 1.1])
def test_final_gripper_confirmation_refuses_invalid_measurements(position):
    r = build()
    step_with(
        r,
        "open_gripper",
        "open gripper goal already in flight",
        grip=lambda since: position,
    )
    r.robot.hold()
    with pytest.raises(ValueError, match="invalid measured gripper"):
        r.robot.confirm_pending_gripper()


@pytest.mark.parametrize("interrupt", ["stop", "red"])
def test_final_confirmation_never_accepts_a_target_after_stop_or_red(interrupt):
    r = build()
    step_with(
        r,
        "open_gripper",
        "open gripper goal already in flight",
        grip=lambda since: 0.95,
    )
    r.robot.hold()

    def stop_at_second_read(n):
        if n >= 3:
            if interrupt == "stop":
                r.robot.stop_event.set()
            else:
                r.guard.status = "red"

    r.env.on_read = stop_at_second_read
    with pytest.raises(ValueError, match="stop|red"):
        r.robot.confirm_pending_gripper()


def test_terminal_confirmation_requires_hold():
    r = build()
    assert (
        step_with(r, "open_gripper", "open gripper goal already in flight").executed
        == "yes"
    )
    with pytest.raises(ValueError, match="requires hold"):
        r.robot.confirm_pending_gripper()


@pytest.mark.parametrize(
    "position,expected", [(0.95, "COMPLETED"), (0.1, "FAULT_LOCKED")]
)
def test_real_entry_checks_last_ack_after_quiesce_before_seal_or_home(
    tmp_path, monkeypatch, position, expected
):
    from fr3_real_support import notes

    from levi.automatic import fr3_run as R

    r = rig(tmp_path, monkeypatch)
    r.binding["safety"]["gripper_confirm_timeout_s"] = 0.3
    from levi.automatic.adapters import events as ev

    monkeypatch.setattr(
        R.providers, "FileEvents", lambda root, rid, c: ev.FakeEventStream({}, c, rid)
    )
    env_class = r.modules[2].RobotEnv
    step, observe, post = env_class.step, env_class.get_observation, env_class._post
    reply = "open gripper goal already in flight"
    lifecycle = []
    confirm = B.Robot.confirm_pending_gripper
    from levi.automatic.fr3_media import Recorder

    seal = Recorder.seal

    def confirmed(self):
        lifecycle.append("terminal_confirm")
        return confirm(self)

    def sealed(self, *args, **kwargs):
        lifecycle.append("seal")
        return seal(self, *args, **kwargs)

    monkeypatch.setattr(B.Robot, "confirm_pending_gripper", confirmed)
    monkeypatch.setattr(Recorder, "seal", sealed)

    def queued_post(self, name, payload=None):
        if name == "open_gripper":
            r.calls.append(("route", name))
            return reply
        return post(self, name, payload)

    def last_step(self, action):
        if self.sent >= r.job["config"].forward_max_steps:
            lifecycle.append("home")
        value = step(self, action)
        if self.sent == r.job["config"].forward_max_steps:
            value["gripper_response"] = {
                "command": "open_gripper",
                "response": self._post("open_gripper"),
            }
        return value

    def measured(self):
        value = observe(self)
        if self.sent >= r.job["config"].forward_max_steps:
            value["robot_state"]["gripper_position"] = position
        return value

    monkeypatch.setattr(env_class, "_post", queued_post)
    monkeypatch.setattr(env_class, "step", last_step)
    monkeypatch.setattr(env_class, "get_observation", measured)
    assert R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == expected
    if expected == "FAULT_LOCKED":
        assert (
            sum(c == ("route", "pose") for c in r.calls)
            == r.job["config"].forward_max_steps
        )
        assert reply in notes(r)["motion_unacknowledged"]
        assert "seal" not in lifecycle and "home" not in lifecycle
    else:
        assert reply in notes(r)["gripper_reply_confirmed"]
        assert (
            lifecycle.index("terminal_confirm")
            < lifecycle.index("seal")
            < lifecycle.index("home")
        )


@pytest.mark.parametrize(
    "completed_ns,ok", [(99_999_999, True), (100_000_000, True), (100_000_001, False)]
)
@pytest.mark.parametrize("interrupt", [None, "stop", "latch", "red", "invalid"])
def test_confirmation_uses_sample_completion_deadline(completed_ns, ok, interrupt):
    r = build(gripper_confirm_timeout_s=0.1, settle_poll_s=0.05)
    step_with(r, "open_gripper", "open gripper goal already in flight")
    r.robot.hold()
    reads = r.env.reads
    began = r.clock.now()

    def finish_read(n):
        if n == reads + 2:
            # Read starts before the deadline but can finish after it.
            assert r.clock.now() == began + 50_000_000
            r.clock.advance(began + completed_ns - r.clock.now())
            if interrupt == "stop":
                r.robot.stop_event.set()
            elif interrupt == "latch":
                r.robot.latched = "injected fault"
            elif interrupt == "red":
                r.guard.status = "red"
            elif interrupt == "invalid":
                r.env.grip_fn = lambda since: float("nan")

    r.env.on_read = finish_read
    if ok and interrupt is None:
        assert "gripper_reply_confirmed" in dict(r.robot.confirm_pending_gripper())
        assert r.robot._grip_pending is None
    else:
        reason = {
            None: "not confirmed within the budget",
            "stop": "operator stop or signal",
            "latch": "fault latch",
            "red": "health red",
            "invalid": "invalid measured gripper",
        }[interrupt]
        with pytest.raises(ValueError, match=reason):
            r.robot.confirm_pending_gripper()
        assert r.robot.latched
        assert r.robot._grip_pending is not None
    assert r.env.routes.count("pose") == 1


def test_unsteady_sample_at_deadline_is_refused_without_another_poll():
    r = build(gripper_confirm_timeout_s=0.1, settle_poll_s=0.1)
    step_with(r, "open_gripper", "open gripper goal already in flight")
    r.robot.hold()
    began = r.clock.now()
    r.env.grip_fn = lambda since: 0.2 if r.clock.now() == began else 1.0
    with pytest.raises(ValueError, match="not confirmed within the budget"):
        r.robot.confirm_pending_gripper()
    assert r.clock.now() == began + 100_000_000
