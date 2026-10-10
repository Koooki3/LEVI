"""The campaign journal (campaign/journal.py, T-CP-03): the transaction
rules its replay enforces, torn and corrupt lines, one writer."""

import pytest
from campaign_fixtures import FakePlanner, write_job
from campaign_guard import aeri_home_fixture, guard_fixture  # noqa: F401

from levi.automatic.campaign import spec
from levi.automatic.campaign.journal import (
    CampaignJournal,
    CampaignJournalBusy,
    CampaignJournalError,
)
from levi.automatic.journal import process_identity


def who(kind="conductor", command=None):
    return {
        "principal_kind": kind,
        "principal_id": "p-1" if kind != "operator" else "op-1",
        "session_id": "s-1",
        "process": process_identity(),
        "command_id": command,
    }


OPERATOR = 0


def operator():
    global OPERATOR
    OPERATOR += 1
    return who("operator", f"cmd-{OPERATOR}")


@pytest.fixture
def plan(tmp_path):
    path = write_job(tmp_path / "job", trials=2, segment_trials=2)
    found = spec.plan_campaign(path, job_root=tmp_path / "jobs", planner=FakePlanner())
    return found.to_json()


@pytest.fixture
def journal(tmp_path, plan):
    found = CampaignJournal.create(tmp_path / "c", plan=plan, authority=who())
    yield found
    found.close()


def act(journal, to, reason, kind, executed="yes", **extra):
    journal.prepare(to, reason, kind=kind, authority=who(), **extra)
    journal.acknowledge(executed, "policy_host", "ok", authority=who())
    if executed == "yes":
        return journal.commit(authority=who())
    return journal.abort(authority=who())


def to_env_confirm(journal, segment=1):
    if journal.state == "DRAFT":
        journal.move("PLANNED", "plan_frozen", authority=who())
        journal.move("SEGMENT_PREPARE", "segment_started", authority=who(), segment=1)
    journal.move("POLICY_STOP", "robot_free", authority=who(), segment=segment)
    act(journal, "POLICY_START", "policy_stopped", "policy_stop", segment=segment)
    act(journal, "POLICY_READY", "policy_started", "policy_start", segment=segment)
    journal.move("ENV_CONFIRM", "policy_ready", authority=who(), segment=segment)


def launch(journal, executed="yes", attempt=1, segment=1, authority=None):
    run_id = f"c1__X__s{segment:02d}"
    journal.prepare(
        "ARM_RUNNING",
        "run_launched",
        kind="launch_run",
        authority=authority or operator(),
        segment=segment,
        run_id=run_id,
        attempt=attempt,
    )
    journal.acknowledge(executed, "run_launcher", "ok", authority=who())
    if executed == "yes":
        return journal.commit(authority=who())
    return journal.abort(authority=who())


def refused(call, *args, **kwargs):
    with pytest.raises(CampaignJournalError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == "E_PROTOCOL", caught.value
    return caught.value


def test_a_header_names_the_plan_and_the_first_state_is_draft(journal, plan):
    header = journal.events[0].header
    assert header.campaign_sha256 == plan["campaign_sha256"]
    assert header.segments == 2 and header.arms == ["A", "B"]
    assert journal.state == "DRAFT"


def test_the_launch_carries_a_launch_action_and_is_never_repeated(journal):
    to_env_confirm(journal)
    refused(journal.prepare, "ARM_RUNNING", "run_launched", authority=who(), segment=1)
    launch(journal)
    journal.move("WAIT_HUMAN", "child_fault", authority=who(), segment=1)
    # A resume watches the run again; it never starts it.
    journal.move("ARM_RUNNING", "operator_resume", authority=operator(), segment=1)
    journal.move("WAIT_HUMAN", "child_fault", authority=who(), segment=1)
    journal.move("SEGMENT_PREPARE", "operator_resume", authority=operator(), segment=1)
    to_env_confirm(journal)
    error = refused(launch, journal, attempt=2)
    assert "never started again" in error.detail


def test_a_launch_acknowledged_yes_and_then_aborted_is_never_retried(journal):
    to_env_confirm(journal)
    journal.prepare(
        "ARM_RUNNING",
        "run_launched",
        kind="launch_run",
        authority=operator(),
        segment=1,
        run_id="c1__A__s01",
    )
    journal.acknowledge("yes", "run_launcher", "ok", authority=who())
    journal.abort(authority=who())  # e.g. a recovery after a crash
    journal.move("WAIT_HUMAN", "launch_failed", authority=who(), segment=1)
    journal.move("SEGMENT_PREPARE", "operator_resume", authority=operator(), segment=1)
    to_env_confirm(journal)
    refused(launch, journal, attempt=2)


def test_a_launch_that_did_not_happen_may_be_tried_again_with_a_new_attempt(journal):
    to_env_confirm(journal)
    launch(journal, executed="no")
    journal.move("WAIT_HUMAN", "launch_failed", authority=who(), segment=1)
    journal.move("SEGMENT_PREPARE", "operator_resume", authority=operator(), segment=1)
    to_env_confirm(journal)
    refused(launch, journal, attempt=1)  # the key was used
    journal_state = journal.state
    assert journal_state == "ENV_CONFIRM"
    refused(launch, journal, attempt=3)
    launch(journal, attempt=2)
    assert journal.state == "ARM_RUNNING"


def test_only_an_operator_leaves_a_wait_or_aborts(journal):
    to_env_confirm(journal)
    journal.move("WAIT_HUMAN", "policy_not_ready", authority=who(), segment=1)
    refused(
        journal.move, "SEGMENT_PREPARE", "operator_resume", authority=who(), segment=1
    )
    refused(journal.move, "ABORTED", "operator_abort", authority=who())
    refused(
        journal.move,
        "SEGMENT_PREPARE",
        "operator_resume",
        authority=who("operator"),
        segment=1,
    )  # an operator without a command
    journal.move("FAULT_LOCKED", "listener_mismatch", authority=who(), segment=1)
    refused(
        journal.move, "WAIT_HUMAN", "conductor_restarted", authority=who("recovery")
    )
    journal.move("FAULT_LOCKED", "recovery_ambiguous", authority=who("recovery"))
    journal.move("ABORTED", "operator_abort", authority=operator())
    refused(journal.move, "WAIT_HUMAN", "child_fault", authority=who())


def test_a_recovery_only_waits(journal):
    journal.move("PLANNED", "plan_frozen", authority=who())
    refused(
        journal.move,
        "SEGMENT_PREPARE",
        "segment_started",
        authority=who("recovery"),
        segment=1,
    )
    journal.move("WAIT_HUMAN", "conductor_restarted", authority=who("recovery"))


def test_segments_go_in_order_and_pause_only_at_a_boundary(journal):
    journal.move("PLANNED", "plan_frozen", authority=who())
    refused(
        journal.move, "SEGMENT_PREPARE", "segment_started", authority=who(), segment=2
    )
    journal.move("SEGMENT_PREPARE", "segment_started", authority=who(), segment=1)
    refused(journal.move, "PAUSED", "pause_at_boundary", authority=who())
    to_env_confirm(journal)
    launch(journal)
    refused(journal.move, "PAUSED", "pause_at_boundary", authority=who())
    refused(journal.move, "SEGMENT_SEALED", "run_completed", authority=who(), segment=2)
    journal.move("SEGMENT_SEALED", "run_completed", authority=who(), segment=1)
    refused(journal.move, "ANALYZING", "campaign_complete", authority=who())
    journal.move("PAUSED", "pause_at_boundary", authority=who())
    journal.move("SEGMENT_PREPARE", "operator_resume", authority=operator(), segment=2)
    to_env_confirm(journal, segment=2)
    launch(journal, segment=2)
    journal.move("SEGMENT_SEALED", "run_completed", authority=who(), segment=2)
    journal.move("ANALYZING", "campaign_complete", authority=who())
    assert sorted(journal.replay.sealed) == [1, 2]


def test_a_commit_keeps_what_was_prepared_and_needs_the_acknowledgement(journal):
    journal.move("PLANNED", "plan_frozen", authority=who())
    journal.move("SEGMENT_PREPARE", "segment_started", authority=who(), segment=1)
    journal.move("POLICY_STOP", "robot_free", authority=who(), segment=1)
    journal.prepare(
        "POLICY_START", "policy_stopped", kind="policy_stop", authority=who(), segment=1
    )
    refused(journal.commit, authority=who())
    journal.acknowledge("unknown", "policy_host", "timeout", authority=who())
    refused(journal.commit, authority=who())
    journal.abort(authority=who())
    assert journal.state == "POLICY_STOP"


def test_a_second_writer_is_refused(journal, tmp_path):
    with pytest.raises(CampaignJournalBusy):
        CampaignJournal.open(tmp_path / "c")


def test_a_torn_last_line_is_set_aside_and_a_bad_middle_line_is_corrupt(tmp_path, plan):
    folder = tmp_path / "c"
    with CampaignJournal.create(folder, plan=plan, authority=who()) as journal:
        journal.move("PLANNED", "plan_frozen", authority=who())
    path = folder / "journal.jsonl"
    whole = path.read_bytes()
    path.write_bytes(whole + b'{"schema": "levi.aeri.campaign_ev')
    with CampaignJournal.open(folder) as journal:
        assert journal.state == "PLANNED" and journal.corrupt is None
    assert path.read_bytes() == whole
    assert len(list((folder / "torn").iterdir())) == 1
    lines = whole.split(b"\n")
    lines[1] = lines[1].replace(b'"plan_frozen"', b'"segment_started"')
    path.write_bytes(b"\n".join(lines))
    with CampaignJournal.open(folder) as journal:
        assert journal.corrupt and journal.state == "FAULT_LOCKED"
        with pytest.raises(CampaignJournalError) as caught:
            journal.note("x", authority=who())
        assert caught.value.code == "E_CORRUPT"


def test_the_kept_plan_must_be_the_headers(tmp_path, plan):
    folder = tmp_path / "c"
    CampaignJournal.create(folder, plan=plan, authority=who()).close()
    (folder / "plan.json").write_text("{}")
    with pytest.raises(CampaignJournalError) as caught:
        CampaignJournal.open(folder)
    assert caught.value.code == "E_PLAN"
    with pytest.raises(CampaignJournalError) as caught:
        CampaignJournal.create(
            tmp_path / "d", plan={**plan, "robot": "x"}, authority=who()
        )
    assert caught.value.code == "E_PLAN"


def test_a_pause_request_is_kept_until_taken(journal):
    journal.note("pause_requested", "", authority=who("operator", "pause-1"))
    assert journal.replay.pending_pause == "pause-1"
    journal.move("PLANNED", "plan_frozen", authority=who())
    journal.move("PAUSED", "pause_at_boundary", authority=who())
    assert journal.replay.pending_pause is None
