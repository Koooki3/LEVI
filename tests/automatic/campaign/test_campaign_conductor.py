"""The campaign conductor (conductor.py, T-CP-03): the state machine, a
person's answers, stop rules, pause at a boundary, one campaign per robot,
never a second start of a child run."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from campaign_fixtures import FakePlanner, write_job
from campaign_world import FakeHost, FakeLauncher, Person, Session, World, deps

from levi.automatic.campaign import conductor as cd
from levi.automatic.campaign import spec
from levi.automatic.campaign.conductor import Conductor, ConductorError, HostResult
from levi.automatic.campaign.journal import CampaignJournal

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def planned(tmp_path, folder="job", **over):
    path = write_job(tmp_path / folder, **over)
    return spec.plan_campaign(path, job_root=tmp_path / "jobs", planner=FakePlanner())


def states(conductor) -> list:
    return [e.to_state for e in conductor.journal.events if e.record == "committed"]


def start(tmp_path, world=None, **over):
    found = planned(tmp_path)
    world = world or World(tmp_path / "world")
    given = deps(world, **over)
    return Conductor.create(found.directory, **given), found, world


def test_a_campaign_runs_every_segment_once_and_stops_for_the_report(tmp_path):
    conductor, found, world = start(tmp_path)
    outcome = conductor.run()
    assert outcome.kind == "waiting" and conductor.state == "ANALYZING"
    assert world.launches() == [c.run_id for c in found.children]
    seen = states(conductor)
    assert seen[:9] == [
        "PLANNED",
        "SEGMENT_PREPARE",
        "POLICY_STOP",
        "POLICY_START",
        "POLICY_READY",
        "ENV_CONFIRM",
        "ARM_RUNNING",
        "SEGMENT_SEALED",
        "SEGMENT_PREPARE",
    ]
    assert seen.count("ARM_RUNNING") == len(found.children)
    sealed = [
        e
        for e in conductor.journal.events
        if e.to_state == "SEGMENT_SEALED" and e.counts
    ]
    assert [e.counts.episodes_complete for e in sealed] == [2] * 4
    conductor.reporter = lambda plan, events: "report_written"
    assert conductor.run().kind == "done" and conductor.state == "REPORTED"
    conductor.close()


def test_idempotency_keys_name_the_segment_and_the_state(tmp_path):
    conductor, found, _ = start(tmp_path)
    conductor.run()
    keys = [e.action.idempotency_key for e in conductor.journal.events if e.action]
    assert "c1:PLANNED" in keys
    assert "c1:s01:ARM_RUNNING" in keys and "c1:s04:SEGMENT_SEALED" in keys
    launches = [
        e
        for e in conductor.journal.events
        if e.action and e.action.kind == "launch_run"
    ]
    assert all(e.action.non_idempotent for e in launches)
    assert [e.run_id for e in launches] == [c.run_id for c in found.children]
    conductor.close()


def test_the_same_arm_twice_in_a_row_keeps_its_policy(tmp_path):
    conductor, found, world = start(tmp_path)
    conductor.run()
    arms = [c.arm for c in sorted(found.children, key=lambda c: c.segment)]
    starts = [op["arm"] for op in world.host_ops() if op["op"] == "start"]
    expected = [a for i, a in enumerate(arms) if i == 0 or arms[i - 1] != a]
    assert starts == expected
    kept = [
        e
        for e in conductor.journal.events
        if e.reason == "same_arm_kept" and e.record == "committed"
    ]
    assert len(kept) == 2 * (len(arms) - len(expected))
    conductor.close()


def test_the_session_says_waiting_reset_while_the_policy_is_switched(tmp_path):
    session = Session()
    conductor, _, _ = start(tmp_path, session=session)
    conductor.run()
    first = session.calls.index(("waiting_reset", 1))
    assert ("release",) in session.calls[first:]
    conductor.close()


def test_nothing_starts_without_the_operators_scene_confirmation(tmp_path):
    person = Person(auto=False)
    conductor, _, world = start(tmp_path, confirmations=person)
    outcome = conductor.run()
    assert outcome.kind == "waiting" and conductor.state == "ENV_CONFIRM"
    assert world.launches() == []
    request = person.asked[-1]
    assert request.kind == "env_confirm" and request.arm_code in ("X1", "X2")
    assert request.slots and request.segment == 1
    # A pre-written or misdirected answer is dropped and noted.
    person.queue.append(
        lambda r: person.answer(
            r, "confirm", arm_still=True, layout_ready=True
        ).__class__(
            request_id=r.request_id,
            nonce="guessed",
            command_id="cmd-x",
            principal_id="op-1",
            decision="confirm",
            checks={"arm_still": True, "layout_ready": True},
        )
    )
    assert conductor.advance().kind == "waiting"
    assert any(
        e.note and e.note.code == "unsolicited_answer" for e in conductor.journal.events
    )
    # An incomplete confirmation does not start the run either.
    person.queue.append(lambda r: person.answer(r, "confirm", arm_still=True))
    assert conductor.advance().kind == "waiting"
    assert any(
        e.note and e.note.code == "confirmations_missing"
        for e in conductor.journal.events
    )
    assert world.launches() == []
    person.queue.append(
        lambda r: person.answer(r, "confirm", arm_still=True, layout_ready=True)
    )
    assert conductor.advance().detail == "launched"
    launched = [
        e
        for e in conductor.journal.events
        if e.action and e.action.kind == "launch_run"
    ]
    assert launched[0].authority.principal_kind == "operator"
    assert launched[0].authority.command_id
    conductor.close()


def test_a_used_command_id_is_not_taken_twice(tmp_path):
    person = Person(auto=False)
    conductor, _, world = start(tmp_path, confirmations=person)
    conductor.run()
    first = {}

    def once(r):
        first["c"] = person.answer(r, "confirm", arm_still=True, layout_ready=True)
        return first["c"]

    person.queue.append(once)
    conductor.run()  # launches segment 1, completes, waits at segment 2's scene
    assert conductor.state == "ENV_CONFIRM" and len(world.launches()) == 1

    def again(r):
        old = first["c"]
        return old.__class__(
            r.request_id, r.nonce, old.command_id, "op-1", "confirm", old.checks
        )

    person.queue.append(again)
    assert conductor.advance().kind == "waiting"
    assert any(
        e.note and e.note.code == "repeated_command" for e in conductor.journal.events
    )
    assert len(world.launches()) == 1
    conductor.close()


def test_an_operator_abort_ends_the_campaign(tmp_path):
    person = Person(auto=False)
    conductor, _, world = start(tmp_path, confirmations=person)
    conductor.run()
    person.queue.append(lambda r: person.answer(r, "abort"))
    conductor.run()
    assert conductor.state == "ABORTED" and world.launches() == []
    assert conductor.advance().kind == "done"
    conductor.close()


def test_a_pause_takes_effect_only_at_the_segment_boundary(tmp_path):
    world = World(tmp_path / "world")
    person = Person()
    conductor, found, _ = start(tmp_path, world=world, confirmations=person)
    first = found.children[0].run_id
    world.set_status(first, "running")
    conductor.run()
    assert conductor.state == "ARM_RUNNING"
    person.request_pause()
    assert conductor.run().kind == "waiting" and conductor.state == "ARM_RUNNING"
    world.set_status(first, "completed", episodes_complete=2)
    person.auto = False
    conductor.run()
    assert conductor.state == "PAUSED"
    assert len(world.launches()) == 1
    person.pause = None
    person.auto = True
    conductor.run()
    assert conductor.state == "ANALYZING" and len(world.launches()) == 4
    conductor.close()


def test_a_child_fault_waits_for_a_person_and_never_restarts_the_child(tmp_path):
    world = World(tmp_path / "world")
    person = Person(auto=True)
    conductor, found, _ = start(tmp_path, world=world, confirmations=person)
    first = found.children[0].run_id
    world.set_status(first, "fault_locked", faults=1)
    person.auto = False
    conductor.run()
    assert conductor.state == "ENV_CONFIRM"
    person.queue.append(
        lambda r: person.answer(r, "confirm", arm_still=True, layout_ready=True)
    )
    conductor.run()
    assert conductor.state == "WAIT_HUMAN"
    assert conductor.replay.wait_reason == "child_fault"
    # The child recovers by its own operator resume; the campaign watches it.
    world.set_status(first, "completed", episodes_complete=2, faults=1)
    person.queue.append(lambda r: person.answer(r, "resume"))
    conductor.run()
    assert world.launches().count(first) == 1
    assert conductor.replay.sealed[1]["faults"] == 1
    conductor.close()


def test_faults_in_a_row_stop_the_campaign_until_a_person_overrides(tmp_path):
    world = World(tmp_path / "world")
    person = Person(auto=False)
    conductor, found, _ = start(tmp_path, world=world, confirmations=person)
    one, two = found.children[0].run_id, found.children[1].run_id
    scene = lambda r: person.answer(r, "confirm", arm_still=True, layout_ready=True)
    world.set_status(one, "crashed")
    person.queue.append(scene)
    conductor.run()
    assert conductor.state == "WAIT_HUMAN"
    assert conductor.replay.wait_reason == "child_crashed"
    world.set_status(one, "completed", episodes_complete=1)
    world.set_status(two, "fault_locked", faults=1)
    person.queue += [lambda r: person.answer(r, "resume"), scene]
    conductor.run()
    assert conductor.state == "WAIT_HUMAN"
    assert conductor.replay.wait_reason == "stop_rule_faults"
    person.queue.append(lambda r: person.answer(r, "resume"))
    assert conductor.run().kind == "waiting" and conductor.state == "WAIT_HUMAN"
    assert any(
        e.note and e.note.code == "stop_rule_needs_override"
        for e in conductor.journal.events
    )
    world.set_status(two, "completed", episodes_complete=2)
    person.auto = True
    conductor.run()
    assert conductor.state == "ANALYZING"
    assert sorted(world.launches()) == sorted(c.run_id for c in found.children)
    conductor.close()


def test_unplanned_interventions_over_the_limit_wait_for_a_person(tmp_path):
    world = World(tmp_path / "world")
    person = Person(override=False)
    conductor, found, _ = start(tmp_path, world=world, confirmations=person)
    first = found.children[0]
    world.set_status(
        first.run_id, "completed", episodes_complete=2, unplanned_interventions=6
    )
    conductor.run()
    assert conductor.state == "WAIT_HUMAN"
    assert conductor.replay.wait_reason == "stop_rule_interventions"
    person.override = True
    conductor.run()
    assert conductor.state == "ANALYZING"
    conductor.close()


def test_a_foreign_listener_locks_the_campaign_and_a_resume_redoes_the_switch(tmp_path):
    world = World(tmp_path / "world")
    host = FakeHost(world, start_result=HostResult("yes", "port_taken", mismatch=True))
    person = Person(auto=False)
    conductor, _, _ = start(tmp_path, world=world, host=host, confirmations=person)
    conductor.run()
    assert conductor.state == "FAULT_LOCKED"
    assert conductor.replay.wait_reason == "listener_mismatch"
    host.start_result = None
    person.auto = True
    conductor.run()
    assert conductor.state == "ANALYZING"
    resumed = [
        e
        for e in conductor.journal.events
        if e.reason == "operator_resume" and e.record == "committed"
    ]
    assert resumed[0].to_state == "SEGMENT_PREPARE" and resumed[0].segment == 1
    conductor.close()


def test_a_policy_that_never_gets_ready_waits_for_a_person(tmp_path):
    world = World(tmp_path / "world")
    now = [0.0]
    host = FakeHost(world, ready="not_ready")
    found = planned(tmp_path)
    conductor = Conductor.create(
        found.directory,
        **deps(world, host=host),
        ready_timeout_s=60,
        clock=lambda: now[0],
    )
    assert conductor.run().kind == "waiting" and conductor.state == "POLICY_READY"
    now[0] = 61.0
    conductor.run(max_steps=1)
    assert conductor.state == "WAIT_HUMAN"
    assert conductor.replay.wait_reason == "policy_not_ready"
    conductor.close()


def test_a_listener_mismatch_at_the_readiness_check_locks_the_campaign(tmp_path):
    world = World(tmp_path / "world")
    conductor, _, _ = start(
        tmp_path,
        world=world,
        host=FakeHost(world, ready="mismatch"),
        confirmations=Person(auto=False),
    )
    conductor.run()
    assert conductor.state == "FAULT_LOCKED" and world.launches() == []
    conductor.close()


def test_a_launch_that_did_not_happen_is_tried_again_only_after_a_person(tmp_path):
    world = World(tmp_path / "world")
    launcher = FakeLauncher(world, result="no")
    person = Person(auto=False)
    conductor, found, _ = start(
        tmp_path, world=world, launcher=launcher, confirmations=person
    )
    conductor.run()
    person.queue.append(
        lambda r: person.answer(r, "confirm", arm_still=True, layout_ready=True)
    )
    conductor.run()
    assert (
        conductor.state == "WAIT_HUMAN"
        and conductor.replay.wait_reason == "launch_failed"
    )
    launcher.result = "yes"
    person.auto = True
    conductor.run()
    assert conductor.state == "ANALYZING"
    attempts = [
        e.action
        for e in conductor.journal.events
        if e.action and e.action.kind == "launch_run"
    ]
    first = found.children[0].run_id
    assert [a.idempotency_key for a in attempts[:2]] == [
        "c1:s01:ARM_RUNNING",
        "c1:s01:ARM_RUNNING:a2",
    ]
    assert world.launches().count(first) == 1
    conductor.close()


def test_a_run_already_there_before_the_launch_is_foreign(tmp_path):
    world = World(tmp_path / "world")
    found = planned(tmp_path)
    world.set_status(found.children[0].run_id, "running", present=True)
    conductor = Conductor.create(
        found.directory, **deps(world, confirmations=Person(auto=False))
    )
    conductor.run()
    assert conductor.state == "ENV_CONFIRM"
    person = conductor.confirmations
    person.queue.append(
        lambda r: person.answer(r, "confirm", arm_still=True, layout_ready=True)
    )
    conductor.run()
    assert (
        conductor.state == "FAULT_LOCKED"
        and conductor.replay.wait_reason == "foreign_run"
    )
    assert world.launches() == []
    conductor.close()


def test_a_busy_robot_holds_the_segment_back(tmp_path):
    world = World(tmp_path / "world")
    launcher = FakeLauncher(world, busy="robot-fr3.lock held by run r0")
    conductor, _, _ = start(tmp_path, world=world, launcher=launcher)
    outcome = conductor.run()
    assert outcome.kind == "waiting" and conductor.state == "SEGMENT_PREPARE"
    assert world.host_ops() == []
    conductor.close()


def test_one_campaign_per_robot(tmp_path, aeri_home):
    world = World(tmp_path / "world")
    first, _, _ = start(tmp_path, world=world)
    other = planned(tmp_path, folder="job2", campaign_id="c2")
    with pytest.raises(ConductorError) as caught:
        Conductor.create(other.directory, **deps(world))
    assert caught.value.code == "E_BUSY"
    assert caught.value.holder["campaign_id"] == "c1"
    elsewhere = planned(tmp_path, folder="job3", campaign_id="c3", robot="second")
    with Conductor.create(elsewhere.directory, **deps(World(tmp_path / "w3"))) as third:
        assert third.state == "DRAFT"
    first.close()
    with Conductor.create(other.directory, **deps(world)) as second:
        assert second.run().kind == "waiting"


def test_a_campaign_lock_held_by_another_process_refuses(tmp_path, aeri_home):
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,os,sys,time; p=sys.argv[1]; "
                "os.makedirs(os.path.dirname(p),exist_ok=True); "
                "f=open(p,'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
                "f.write('{\"pid\": 1}'); f.flush(); "
                "print('ready', flush=True); time.sleep(30)"
            ),
            str(aeri_home / "campaign-fr3.lock"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        found = planned(tmp_path)
        with pytest.raises(ConductorError) as caught:
            Conductor.create(found.directory, **deps(World(tmp_path / "w")))
        assert caught.value.code == "E_BUSY"
    finally:
        holder.kill()
        holder.wait()
    found = planned(tmp_path)
    with Conductor.create(found.directory, **deps(World(tmp_path / "w"))) as conductor:
        assert conductor.state == "DRAFT"


def test_a_conductor_stopped_mid_run_resumes_only_through_a_person(tmp_path):
    world = World(tmp_path / "world")
    found = planned(tmp_path)
    world.set_status(found.children[0].run_id, "running")
    conductor = Conductor.create(found.directory, **deps(world))
    conductor.run()
    assert conductor.state == "ARM_RUNNING"
    conductor.close()
    person = Person(auto=False)
    again = Conductor.attach("c1", found.directory, **deps(world, confirmations=person))
    assert again.state == "WAIT_HUMAN"
    assert again.replay.wait_reason == "conductor_restarted"
    assert again.run().kind == "waiting"
    world.set_status(found.children[0].run_id, "completed", episodes_complete=2)
    person.auto = True
    again.run()
    assert again.state == "ANALYZING"
    assert world.launches().count(found.children[0].run_id) == 1
    again.close()


def test_attach_refuses_a_corrupt_journal_and_writes_nothing(tmp_path, aeri_home):
    world = World(tmp_path / "world")
    found = planned(tmp_path)
    conductor = Conductor.create(found.directory, **deps(world))
    conductor.run()
    conductor.close()
    path = aeri_home / "campaigns" / "c1" / "journal.jsonl"
    lines = path.read_bytes().split(b"\n")
    lines[3] = lines[3].replace(b'"segment":1', b'"segment":2')
    path.write_bytes(b"\n".join(lines))
    before = path.read_bytes()
    with pytest.raises(ConductorError) as caught:
        Conductor.attach("c1", found.directory, **deps(world))
    assert caught.value.code == "E_CORRUPT"
    assert path.read_bytes() == before
    assert CampaignJournal.read(path.parent).effective_state == "FAULT_LOCKED"


def test_attach_refuses_a_changed_child_file(tmp_path):
    world = World(tmp_path / "world")
    found = planned(tmp_path)
    Conductor.create(found.directory, **deps(world)).close()
    child = found.directory / found.children[0].file
    child.write_text(child.read_text() + "\n")
    with pytest.raises(spec.CampaignError) as caught:
        Conductor.attach("c1", found.directory, **deps(world))
    assert caught.value.code == "E_CAMPAIGN_PLAN"


def test_the_robot_lock_is_released_when_attach_fails(tmp_path, aeri_home):
    world = World(tmp_path / "world")
    found = planned(tmp_path)
    Conductor.create(found.directory, **deps(world)).close()
    child = found.directory / found.children[0].file
    text = child.read_text()
    child.write_text(text + "\n")
    with pytest.raises(spec.CampaignError):
        Conductor.attach("c1", found.directory, **deps(world))
    child.write_text(text)
    with Conductor.attach("c1", found.directory, **deps(world)) as again:
        # Never started: the plan is frozen, then a person starts it.
        assert again.state == "WAIT_HUMAN"
        assert [
            e.to_state for e in again.journal.events if e.record == "committed"
        ] == [
            "PLANNED",
            "WAIT_HUMAN",
        ]


def test_the_operator_sees_arm_codes_unless_blinding_is_off(tmp_path):
    person = Person(auto=False)
    conductor, _, _ = start(tmp_path, confirmations=person)
    conductor.run()
    assert person.asked[-1].arm_code in ("X1", "X2")
    conductor.close()
    found = planned(
        tmp_path / "open",
        campaign_id="c9",
        extra_campaign="  blinding:\n    operator: none\n",
    )
    person = Person(auto=False)
    with Conductor.create(
        found.directory, **deps(World(tmp_path / "w9"), confirmations=person)
    ) as c:
        c.run()
        assert person.asked[-1].arm_code in ("A", "B")


def test_aeri_home_defaults_and_follows_the_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("LEVI_AERI_HOME", str(tmp_path / "h"))
    assert cd.aeri_home() == tmp_path / "h"
    monkeypatch.delenv("LEVI_AERI_HOME")
    assert cd.aeri_home() == Path.home() / ".levi-aeri"


def test_state_json_is_derived_from_the_journal(tmp_path, aeri_home):
    conductor, _, _ = start(tmp_path)
    conductor.run()
    found = json.loads((aeri_home / "campaigns" / "c1" / "state.json").read_text())
    assert found["state"] == "ANALYZING" and found["sealed"] == [1, 2, 3, 4]
    conductor.close()
