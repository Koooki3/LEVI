"""Resume sequence CAS must precede any inference-cycle rearm."""

import threading

import pytest
from test_event_pause_judge import build

from levi.automatic import state_machine as sm


def stopped(tmp_path):
    r = build(tmp_path, event_step=None, script=[{"decision": "confirmed"}])
    step = r.robot.step

    def stop_after_action(action, *, token):
        result = step(action, token=token)
        if r.robot.step_calls == 3:
            r.orch.stop("initial-stop")
        return result

    r.robot.step = stop_after_action
    assert r.orch.run() == "FAULT_LOCKED"
    assert r.authority.active is None and r.cycle._request is None
    assert r.cycle.failed and r.cycle.operator_stopped
    assert not r.cycle.proof_failed
    return r


def resume(r, seq):
    return r.orch.resume(
        "resume-sequence",
        expected_seq=seq,
        environment_handled=True,
        health_rechecked=True,
    )


@pytest.mark.parametrize("change", ["stale", "pending_note", "writer"])
def test_sequence_refusal_does_not_rearm_cycle(tmp_path, change):
    r = stopped(tmp_path)
    before = dict(vars(r.cycle))
    stops = dict(r.orch._stops)
    steps = r.robot.step_calls
    sequence = r.orch.journal.next_seq
    if change == "stale":
        sequence -= 1
    elif change == "pending_note":
        # _tx flushes notes before journal.prepare performs the actual CAS.
        r.orch._note("operator_stop", "pending note makes the supplied CAS stale")
    else:
        prepare = r.orch.journal.prepare

        def writer_before_prepare(*args, **kwargs):
            # A journal writer after any early next_seq check must still win
            # the journal mutex CAS, without changing cycle bookkeeping.
            writer = threading.Thread(
                target=lambda: r.orch.journal.note(
                    "operator_stop", "concurrent writer", authority=r.orch._auth()
                )
            )
            writer.start()
            writer.join(timeout=2)
            assert not writer.is_alive()
            return prepare(*args, **kwargs)

        r.orch.journal.prepare = writer_before_prepare
    found = resume(r, sequence)
    assert not found.ok and found.code == "stale_sequence"
    assert vars(r.cycle) == before
    assert r.orch._stops == stops
    assert r.orch.state == "FAULT_LOCKED"
    assert r.robot.step_calls == steps and r.orch.fence.current is None
    assert sm.check_journal(r.orch.journal.events) == []


def test_stop_after_resume_commit_is_not_rearmed(tmp_path):
    r = stopped(tmp_path)
    raced = []

    def stop_after_commit(point):
        if point == "after_committed" and not raced:
            worker = threading.Thread(
                target=lambda: raced.append(r.orch.stop("later-stop"))
            )
            worker.start()
            worker.join(timeout=2)
            assert not worker.is_alive()

    r.orch.crash_hook = stop_after_commit
    found = resume(r, r.orch.journal.next_seq)
    assert found.ok
    assert raced and raced[0].ok
    assert "later-stop" in r.orch._stops
    assert r.cycle.failed and r.cycle.operator_stopped
    steps = r.robot.step_calls
    assert r.orch.run() in ("WAIT_HUMAN", "FAULT_LOCKED")
    assert r.cycle.failed and r.cycle.operator_stopped
    assert r.robot.step_calls == steps


def test_proof_failure_during_resume_transaction_relocks_without_motion(tmp_path):
    r = stopped(tmp_path)

    def fail_after_prepare(point):
        if point == "after_prepared":
            r.cycle.interrupt(proof_failed=True)

    r.orch.crash_hook = fail_after_prepare
    steps = r.robot.step_calls
    found = resume(r, r.orch.journal.next_seq)
    assert not found.ok and found.code == "intervention_latched"
    assert r.orch.state == "FAULT_LOCKED"
    assert r.cycle.failed and r.cycle.proof_failed
    assert r.robot.step_calls == steps and r.orch.fence.current is None
    assert sm.check_journal(r.orch.journal.events) == []


def test_failed_durable_commit_never_rearms_cycle(tmp_path, monkeypatch):
    r = stopped(tmp_path)
    rearm = r.cycle.rearm
    commit = r.orch.journal.commit
    rearms = []
    commits = []

    def record_rearm():
        rearms.append(1)
        return rearm()

    def fail_first_commit(**kwargs):
        commits.append(1)
        if len(commits) == 1:
            raise OSError("injected resume commit failure")
        return commit(**kwargs)

    monkeypatch.setattr(r.cycle, "rearm", record_rearm)
    monkeypatch.setattr(r.orch.journal, "commit", fail_first_commit)
    steps = r.robot.step_calls
    with pytest.raises(OSError, match="resume commit failure"):
        resume(r, r.orch.journal.next_seq)
    assert rearms == []
    assert r.cycle.failed and r.cycle.proof_failed
    assert r.orch.state == "FAULT_LOCKED"
    assert r.robot.step_calls == steps and r.orch.fence.current is None
    assert sm.check_journal(r.orch.journal.events) == []


def test_valid_resume_rearms_only_after_commit_and_repeated_command_is_unchanged(
    tmp_path, monkeypatch
):
    r = stopped(tmp_path)
    rearm = r.cycle.rearm
    rearms = []

    def committed_rearm():
        assert r.orch.journal.open_transaction is None
        assert r.orch.journal.events[-1].reason == "human_resumed"
        assert r.orch.journal.events[-1].record == "committed"
        rearms.append(1)
        return rearm()

    monkeypatch.setattr(r.cycle, "rearm", committed_rearm)
    found = resume(r, r.orch.journal.next_seq)
    assert found.ok and found.code == "resumed"
    assert rearms == [1] and not r.cycle.failed and not r.cycle.operator_stopped
    before = dict(vars(r.cycle))
    repeated = resume(r, -1)
    assert repeated.ok and repeated.repeated and repeated.code == "repeated"
    assert vars(r.cycle) == before and rearms == [1]
