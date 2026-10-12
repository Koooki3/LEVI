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


@pytest.mark.parametrize("reopen", [False, True])
def test_rearm_refusal_is_a_durable_repeated_command_result(tmp_path, reopen):
    from levi.automatic.journal import Journal

    r = stopped(tmp_path)

    def fail_after_prepare(point):
        if point == "after_prepared":
            r.cycle.interrupt(proof_failed=True)

    r.orch.crash_hook = fail_after_prepare
    first = resume(r, r.orch.journal.next_seq)
    assert not first.ok and first.code == "intervention_latched"
    if reopen:
        r.orch.journal.close()
        r.orch.journal = Journal.open(
            r.directory,
            plan_sha256=r.orch.config.plan_sha256,
            clock=r.orch.clock,
            clock_domain=r.orch.clock.domain,
        )
    before = dict(vars(r.cycle))
    sequence = r.orch.journal.next_seq
    repeated = resume(r, -1)
    assert not repeated.ok and repeated.code == first.code and repeated.repeated
    assert repeated.sequence_no == first.sequence_no
    assert vars(r.cycle) == before and r.orch.journal.next_seq == sequence
    # A later healthy cycle cannot turn this historical refusal into success.
    r.cycle.failed = r.cycle.operator_stopped = r.cycle.proof_failed = False
    repeated = resume(r, -1)
    assert not repeated.ok and repeated.code == first.code and repeated.repeated
    assert sm.check_journal(r.orch.journal.events) == []


def test_later_cycle_failure_cannot_reclassify_a_successful_resume(tmp_path):
    r = stopped(tmp_path)
    first = resume(r, r.orch.journal.next_seq)
    assert first.ok
    r.cycle.interrupt(proof_failed=True)
    sequence = r.orch.journal.next_seq
    repeated = resume(r, -1)
    assert repeated.ok and repeated.code == "repeated" and repeated.repeated
    assert r.orch.journal.next_seq == sequence and r.cycle.proof_failed


def test_stale_resume_and_existing_stop_repetitions_leave_cycle_unchanged(tmp_path):
    r = stopped(tmp_path)
    before = dict(vars(r.cycle))
    sequence = r.orch.journal.next_seq
    for _ in range(2):
        stale = resume(r, sequence - 1)
        assert not stale.ok and stale.code == "stale_sequence"
        stop = r.orch.stop("initial-stop")
        assert stop.ok and stop.repeated
        assert vars(r.cycle) == before and r.orch.journal.next_seq == sequence


def test_refusal_marker_write_failure_does_not_return_an_unrecorded_result(
    tmp_path, monkeypatch
):
    r = stopped(tmp_path)
    note = r.orch.journal.note

    def fail_after_prepare(point):
        if point == "after_prepared":
            r.cycle.interrupt(proof_failed=True)

    def fail_refusal_marker(code, *args, **kwargs):
        if code == "resume_rearm_refused":
            raise OSError("injected refusal marker write failure")
        return note(code, *args, **kwargs)

    r.orch.crash_hook = fail_after_prepare
    monkeypatch.setattr(r.orch.journal, "note", fail_refusal_marker)
    steps = r.robot.step_calls
    with pytest.raises(OSError, match="refusal marker write failure"):
        resume(r, r.orch.journal.next_seq)
    assert r.cycle.failed and r.cycle.proof_failed
    assert r.orch.state == "FAULT_LOCKED" and r.robot.step_calls == steps
    assert r.orch.fence.current is None


def test_other_transaction_refusal_marker_cannot_override_success(tmp_path):
    r = stopped(tmp_path)
    assert resume(r, r.orch.journal.next_seq).ok
    other = next(
        event
        for event in r.orch.journal.events
        if event.record == "prepared" and event.reason != "human_resumed"
    )
    r.orch.journal.note(
        "resume_rearm_refused",
        "intervention_latched",
        authority=r.orch._auth("operator", "resume-sequence"),
        transaction_id=other.transaction_id,
    )
    repeated = resume(r, -1)
    assert repeated.ok and repeated.repeated and repeated.code == "repeated"
    assert sm.check_journal(r.orch.journal.events) == []


@pytest.mark.parametrize("outcome", ["accepted", "refused"])
@pytest.mark.parametrize("reopen", [False, True])
def test_outcome_write_failure_retry_never_reports_success(
    tmp_path, monkeypatch, outcome, reopen
):
    from levi.automatic.journal import Journal

    r = stopped(tmp_path)
    note = r.orch.journal.note
    marker = "resume_rearm_" + outcome
    if outcome == "refused":

        def fail_after_prepare(point):
            if point == "after_prepared":
                r.cycle.interrupt(proof_failed=True)

        r.orch.crash_hook = fail_after_prepare

    def fail_outcome_marker(code, *args, **kwargs):
        if code == marker:
            raise OSError("injected outcome marker write failure")
        return note(code, *args, **kwargs)

    monkeypatch.setattr(r.orch.journal, "note", fail_outcome_marker)
    steps = r.robot.step_calls
    with pytest.raises(OSError, match="outcome marker write failure"):
        resume(r, r.orch.journal.next_seq)
    assert r.orch.state == "FAULT_LOCKED" and r.cycle.failed
    assert r.orch.fence.current is None and r.robot.step_calls == steps
    if reopen:
        r.orch.journal.close()
        r.orch.journal = Journal.open(
            r.directory,
            plan_sha256=r.orch.config.plan_sha256,
            clock=r.orch.clock,
            clock_domain=r.orch.clock.domain,
        )
    before = dict(vars(r.cycle))
    sequence = r.orch.journal.next_seq
    repeated = resume(r, -1)
    assert not repeated.ok and repeated.code == "command_used" and repeated.repeated
    assert vars(r.cycle) == before and r.orch.journal.next_seq == sequence
    assert r.robot.step_calls == steps and sm.check_journal(r.orch.journal.events) == []


@pytest.mark.parametrize("reopen", [False, True])
def test_old_unmarked_event_resume_is_readable_but_not_reported_success(
    tmp_path, reopen
):
    from levi.automatic.journal import Journal

    r = stopped(tmp_path)
    # Reproduce a retained human_resumed record from before outcome markers,
    # without executing or granting any policy/model action.
    r.orch._tx(
        "PREFLIGHT",
        "human_resumed",
        authority=r.orch._auth("operator", "resume-sequence"),
        expected_seq=r.orch.journal.next_seq,
    )
    if reopen:
        r.orch.journal.close()
        r.orch.journal = Journal.open(
            r.directory,
            plan_sha256=r.orch.config.plan_sha256,
            clock=r.orch.clock,
            clock_domain=r.orch.clock.domain,
        )
    sequence = r.orch.journal.next_seq
    before = dict(vars(r.cycle))
    repeated = resume(r, -1)
    assert not repeated.ok and repeated.code == "command_used" and repeated.repeated
    assert r.orch.journal.next_seq == sequence and vars(r.cycle) == before
    assert sm.check_journal(r.orch.journal.events) == []


def test_legacy_resume_repetition_keeps_old_unmarked_contract(tmp_path):
    from test_aeri_stop_registry import waiting

    r = waiting(tmp_path)
    assert r.orch.config.termination.inference_mode == "legacy_async"
    assert resume(r, r.orch.journal.next_seq).ok
    assert not any(
        event.record == "note" and event.note.code.startswith("resume_rearm_")
        for event in r.orch.journal.by_command("resume-sequence")
    )
    repeated = resume(r, -1)
    assert repeated.ok and repeated.code == "repeated" and repeated.repeated


def test_foreign_transaction_acceptance_cannot_resolve_unmarked_resume(tmp_path):
    r = stopped(tmp_path)
    other = next(event for event in r.orch.journal.events if event.record == "prepared")
    r.orch._tx(
        "PREFLIGHT",
        "human_resumed",
        authority=r.orch._auth("operator", "resume-sequence"),
        expected_seq=r.orch.journal.next_seq,
    )
    r.orch.journal.note(
        "resume_rearm_accepted",
        "resumed",
        authority=r.orch._auth("operator", "resume-sequence"),
        transaction_id=other.transaction_id,
    )
    repeated = resume(r, -1)
    assert not repeated.ok and repeated.code == "command_used" and repeated.repeated


@pytest.mark.parametrize("reopen", [False, True])
def test_storage_failure_during_outcome_and_containment_remains_unknown(
    tmp_path, monkeypatch, reopen
):
    from levi.automatic.journal import Journal

    r = stopped(tmp_path)
    note = r.orch.journal.note

    def fail_append(*args, **kwargs):
        raise OSError("injected storage unavailable")

    def lose_storage_at_outcome(code, *args, **kwargs):
        if code == "resume_rearm_accepted":
            monkeypatch.setattr(r.orch.journal, "append", fail_append)
        return note(code, *args, **kwargs)

    monkeypatch.setattr(r.orch.journal, "note", lose_storage_at_outcome)
    steps = r.robot.step_calls
    with pytest.raises(OSError, match="storage unavailable"):
        resume(r, r.orch.journal.next_seq)
    assert r.orch.halted and r.orch.state == "FAULT_LOCKED"
    assert r.cycle.failed and r.cycle.proof_failed and r.orch.fence.current is None
    if reopen:
        r.orch.journal.close()
        r.orch.journal = Journal.open(
            r.directory,
            plan_sha256=r.orch.config.plan_sha256,
            clock=r.orch.clock,
            clock_domain=r.orch.clock.domain,
        )
    sequence = r.orch.journal.next_seq
    repeated = resume(r, -1)
    assert not repeated.ok and repeated.code == "command_used" and repeated.repeated
    assert r.orch.journal.next_seq == sequence and r.robot.step_calls == steps
