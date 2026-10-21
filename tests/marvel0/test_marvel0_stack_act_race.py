"""A stop or a cancel that is answered between a step's look at the machine and
its start must win: the guided sequence reads the machine without the
machine-wide lock, then starts something under it, and "stop whole stack" and
"cancel" are answered under that same lock. So every start, touch and check the
sequence makes looks again, inside the lock, whether it is still the running
sequence (``Act._seq_locked``), and does nothing when it is not.

The review of the stop-all package found it for A (``_seq_arm``); these tests
fire the stop or the cancel at the instant before the locked call of each
action of the sequence, on the fake machine of ``stack_act_machine.py`` (L1).
Nothing here starts a process or opens a socket."""

import pytest
from stack_act_machine import ActMachine
from test_marvel0_deploy import forbid_real_machine  # noqa: F401 - the guard
from test_marvel0_stack_act import start_sequence
from test_marvel0_stack_act_chain import press, to_step

from levi.marvel0 import stack_act


@pytest.fixture
def m(tmp_path):
    return ActMachine(tmp_path)


def interrupt_stop_all(m):
    return m.act.sequence_stop(m.stop_all_body("stop-all-race1"))


def interrupt_cancel(m):
    return m.act.sequence_cancel({"request_id": "seq-cancel-race1"})


INTERRUPTS = {"stop whole stack": interrupt_stop_all, "cancel": interrupt_cancel}


def snapshot(m, action):
    """Everything a step of the sequence can start, touch or check. (The marker
    only counts for the step that sets it: a real stop of A releases it.)"""
    return {
        "systemd_run": len(m.systemd_runs()),
        "oneshots": len(m.oneshots),
        "fci": len(m.fci_probes),
        "marker": m.marker_path.exists() if action == "sequence_marker" else None,
    }


# action -> the step the sequence is at when it locks for it
ACTIONS = {
    "sequence_monitor": "monitor",
    "sequence_probe": "probe",
    "sequence_marker": "marker",
    "sequence_arm": "arm",
    "sequence_probe_b": "b_probe",
    "sequence_b": "b",
    "sequence_preflight_c": "c_preflight",
    "sequence_c": "c",
}


def fire_before(m, action, interrupt):
    """The interrupt is answered at the instant before the first locked call of
    ``action``; returns what it answered."""
    orig = m.act._locked
    fired = {}

    def locked(name, call):
        if name == action and not fired:
            fired["answer"] = interrupt(m)
        return orig(name, call)

    m.act._locked = locked
    return fired


@pytest.mark.parametrize("interrupt", sorted(INTERRUPTS))
@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_an_interrupt_answered_before_a_locked_call_wins(m, action, interrupt):
    step = ACTIONS[action]
    if step == "monitor":
        press(m)  # (M is not up: the first step starts it)
    else:
        to_step(m, step)
    if step == "b_probe":
        m.t += stack_act.B_PROBE_REFRESH_S + 1  # (the old answer is due again)
        m.refresh_health()
    before = snapshot(m, action)
    fired = fire_before(m, action, INTERRUPTS[interrupt])
    old = m.act._seq
    m.act.advance()
    assert fired, f"{action} was not reached at step {step}"
    assert snapshot(m, action) == before, f"{action} went ahead after {interrupt}"
    assert old["state"] == "cancelled"
    assert old["reason"] == ("cancelled" if interrupt == "cancel" else "superseded")
    # and the sequence goes on doing nothing
    for _ in range(4):
        m.chain_world(set())
        m.act.advance()
    assert snapshot(m, action) == before


def test_the_reviewers_probe_for_a_stop_all_before_the_start_of_a(m):
    start_sequence(m)
    for _ in range(40):
        seq = m.act.advance()
        if seq.get("step") == "wait":
            m.t += 40
            m.no_manager()
        if seq.get("step") == "arm":
            break
    assert m.act._seq["step"] == "arm"
    fired = fire_before(m, "sequence_arm", interrupt_stop_all)
    m.act.advance()
    assert fired["answer"] == {"done": "already_stopped"}
    for _ in range(5):
        m.act.advance()
    assert m.systemd_runs("a") == [], "A was started after stop whole stack answered"


def test_the_reviewers_probe_for_a_cancel_before_the_start_of_a(m):
    start_sequence(m)
    for _ in range(40):
        seq = m.act.advance()
        if seq.get("step") == "wait":
            m.t += 40
            m.no_manager()
        if seq.get("step") == "arm":
            break
    fired = fire_before(m, "sequence_arm", interrupt_cancel)
    m.act.advance()
    assert fired["answer"] == {"done": "sequence_cancelled"}
    for _ in range(5):
        m.act.advance()
    assert m.systemd_runs("a") == [], "A was started after the cancel answered"


def test_the_optional_check_of_b_while_waiting_is_not_made_after_an_interrupt(m):
    start_sequence(m)
    seq = {}
    up = False
    for _ in range(40):
        seq = m.act.advance()
        if seq["state"] != "running":
            break
        if seq.get("step") == "wait":
            m.t += 40
            m.no_manager()
        if "a" in m.units and not up:
            m.a_up()
            up = True
    assert seq["state"] == "waiting_confirm"
    m.t += stack_act.B_PROBE_TTL_S + 1
    m.a_up(lines=False)
    before = len(m.oneshots)
    fired = fire_before(m, "sequence_probe_b", interrupt_cancel)
    m.act.advance()
    assert fired and len(m.oneshots) == before


def test_a_start_the_interrupt_did_not_beat_is_made_as_always(m):
    """Control: with no interrupt the same step starts what it is for."""
    to_step(m, "arm")
    assert m.systemd_runs("a") == []
    m.act.advance()
    assert len(m.systemd_runs("a")) == 1


def test_a_gate_that_breaks_between_the_look_and_the_lock_still_refuses(m):
    """The look is not the decision: ``_do_start`` reads the machine again."""
    to_step(m, "b")
    orig = m.act._locked

    def locked(name, call):
        if name == "sequence_b":
            m.serial.unlink()  # (gone between the look and the lock)
        return orig(name, call)

    m.act._locked = locked
    seq = m.act.advance()
    assert (seq["state"], seq["reason"]) == ("failed", "serial_missing")
    assert m.systemd_runs("b") == []
