"""Small in-process fake regressions; no GPU, socket, subprocess or robot."""

from types import SimpleNamespace

import pytest
from aeri_harness import config, results, rig

from levi.automatic import state_machine as sm
from levi.automatic.intervention import (
    InferenceCycle,
    InterventionHooks,
    PhysicalHoldProof,
    hold_contract_sha256,
)
from levi.automatic.orchestrator import ConfigError, Orchestrator
from levi.automatic.reset_manager import SettleConfig
from levi.automatic.termination import TerminationConfig

HOLD = SettleConfig(1_000_000, 2_000_000, 100_000_000, 0.001, 0.01, 0.01)


class FakeAuthority:
    """Explicit fake for coordinator tests, not an owned process/SDK adapter."""

    def __init__(self):
        self.active = None
        self.calls = []
        self.revoked = False
        self.last_authorization = 0
        self.bad_barrier = False

    def begin_role_request(self, runtime, role, deadline, observation, window, **kw):
        assert self.active is None
        assert window.authorization_epoch > self.last_authorization
        self.last_authorization = window.authorization_epoch
        self.active = SimpleNamespace(role=role, policy_epoch=kw["policy_epoch"])
        self.calls.append(("begin", role, kw["policy_epoch"]))
        return self.active

    def finish_request(self, request, proof, observation, **kw):
        assert request is self.active
        if (
            self.bad_barrier
            or not proof.backend_request_stopped
            or not proof.request_workers_exited
        ):
            raise ValueError("fake backend remained active")
        self.calls.append(("finish", request.role, request.policy_epoch))
        self.active = None
        return not self.revoked

    def revoke_request(self, request, **kw):
        if request is not self.active:
            raise ValueError("stale request")
        self.revoked = True


def build(
    tmp_path,
    *,
    script=None,
    max_steps=40,
    event_step=8,
    bad_hold=False,
    bad_barrier=False,
    policy_fault=None,
):
    from aeri_harness import fake

    from levi.automatic.adapters import events as ev

    cfg = config(
        episodes=1,
        forward_max_steps=max_steps,
        reset_strategy="human_assisted",
        termination=TerminationConfig(
            inference_mode="event_pause_judge",
            physical_hold=HOLD,
            min_steps=5,
            settle_steps=3,
            cooldown_steps=5,
        ),
    )
    clock = fake.FakeClock()
    verifier = ev.FakeGoalVerifier(
        script or [], clock, cfg.run_id, default={"decision": "confirmed"}
    )
    event_items = (
        []
        if event_step is None
        else [{"step": event_step, "event_type": "object_settled"}]
    )
    r = rig(
        tmp_path / "run",
        cfg=cfg,
        clock=clock,
        verifier=verifier,
        create=False,
        events=ev.FakeEventStream({"forward": event_items}, clock, cfg.run_id),
        policy={"chunk_faults": policy_fault or {}},
    )
    authority = FakeAuthority()
    hooks_log = []

    def hold(episode, epoch, deadline):
        before = clock.now()
        clock.advance(HOLD.stable_window_ns)
        return PhysicalHoldProof(
            cfg.run_id,
            episode,
            epoch,
            cfg.plan_sha256,
            hold_contract_sha256(HOLD),
            clock.domain,
            before,
            clock.now(),
            deadline,
            True,
            True,
            not bad_hold,
        )

    def pause(episode, old, new):
        hooks_log.append(("pause", old, new, r.robot.step_calls))
        r.policy.quiesce(policy_epoch=old)
        return True

    def resume(episode, epoch, obs):
        hooks_log.append(("resume", epoch, obs["observed_ns"], clock.now()))
        return True

    def proof(request, deadline):
        return SimpleNamespace(
            backend_request_stopped=True, request_workers_exited=True
        )

    cycle = InferenceCycle(
        authority=authority,
        runtime=object(),
        clock=clock,
        run_id=cfg.run_id,
        plan_sha256=cfg.plan_sha256,
        condition_sha256=hold_contract_sha256(HOLD),
        hooks=InterventionHooks(
            pause,
            resume,
            hold,
            lambda: object(),
            lambda role, episode, epoch, authorization, deadline: SimpleNamespace(
                authorization_epoch=authorization
            ),
            proof,
            lambda request, obs: request["observed_through_ns"] == obs["observed_ns"],
        ),
    )
    r.orch = Orchestrator.create(r.directory, cfg, intervention=cycle, **r.parts())
    authority.bad_barrier = bad_barrier
    r.authority, r.hooks_log, r.cycle = authority, hooks_log, cycle
    return r


def test_mode_needs_explicit_physical_conditions():
    with pytest.raises(ValueError, match="physical_hold"):
        TerminationConfig(inference_mode="event_pause_judge")


def test_missing_owned_hooks_refused_before_creating_journal(tmp_path):
    r = rig(
        tmp_path / "missing",
        cfg=config(
            termination=TerminationConfig(
                inference_mode="event_pause_judge", physical_hold=HOLD
            )
        ),
        create=False,
    )
    with pytest.raises(ConfigError, match="injected owned"):
        Orchestrator.create(r.directory, r.config, **r.parts())
    assert not r.directory.exists()


def test_confirmed_pauses_then_seals_without_more_actions_or_home(tmp_path):
    r = build(tmp_path)
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.step_calls == 8 and r.robot.home_calls == 0
    assert results(r.orch)[0].task_outcome == "success"
    transitions = [e for e in r.orch.journal.events if e.record == "committed"]
    assert any(e.to_state == "FORWARD_JUDGING" for e in transitions)
    assert sm.check_journal(r.orch.journal.events) == []
    assert [c[1] for c in r.authority.calls if c[0] == "begin"].count("judge") == 2


@pytest.mark.parametrize("event_step", [1, 8])
def test_candidate_stops_new_actions_at_once_despite_legacy_delays(
    tmp_path, event_step
):
    # build() freezes min_steps=5, settle_steps=3, cooldown_steps=5: none may delay a pause
    r = build(tmp_path, event_step=event_step)
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.step_calls == event_step
    assert next(h[3] for h in r.hooks_log if h[0] == "pause") == event_step


def test_rejected_candidate_is_spent_and_cannot_loop_without_motion(tmp_path):
    r = build(tmp_path, script=[{"decision": "rejected"}, {"decision": "confirmed"}])
    assert r.orch.run() == "WAIT_HUMAN"
    assert next(h[3] for h in r.hooks_log if h[0] == "pause") == 8
    assert r.robot.step_calls == 40  # no second pause without a newer event


def test_rejected_uses_fresh_observation_new_transaction_epoch_and_chunk(tmp_path):
    r = build(tmp_path, script=[{"decision": "rejected"}, {"decision": "confirmed"}])
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.policy.requests >= 3  # old chunk at pause is discarded
    resumes = [
        e
        for e in r.orch.journal.events
        if e.record == "committed" and e.reason == "inference_resumed"
    ]
    assert len(resumes) == 1
    entry = resumes[0]
    motion_tokens = [
        motion[2] for motion in r.robot.motions if motion[0] == "policy_steps"
    ]
    assert entry.transaction_id in motion_tokens
    assert r.policy.acquired[-1]["policy_epoch"] == entry.policy_epoch
    assert any(item[0] == "resume" and item[2] == item[3] for item in r.hooks_log)
    assert sm.check_journal(r.orch.journal.events) == []


@pytest.mark.parametrize("decision", ["unknown", "timeout"])
def test_unknown_and_timeout_never_resume_motion(tmp_path, decision):
    item = {"decision": "unknown"} if decision == "unknown" else {"delay_ns": 10**12}
    r = build(tmp_path, script=[item])
    assert r.orch.run() == "FAULT_LOCKED"
    assert r.robot.step_calls == 8
    assert (
        sum(item[0] == "resume" for item in r.hooks_log) == 1
    )  # initial admission only
    assert results(r.orch)[0].task_outcome == "unknown"


def test_unproven_physical_hold_prevents_any_initial_model_or_action(tmp_path):
    r = build(tmp_path, bad_hold=True)
    assert r.orch.run() == "FAULT_LOCKED"
    assert r.robot.step_calls == 0 and r.authority.calls == []


def test_unproven_backend_retains_request_and_prevents_motion(tmp_path):
    r = build(tmp_path, bad_barrier=True)
    assert r.orch.run() == "FAULT_LOCKED"
    assert r.authority.active is not None and r.cycle.failed
    assert r.robot.step_calls == 0


def test_400_counts_executed_actions_and_horizon_judges_while_held(tmp_path):
    r = build(
        tmp_path, max_steps=400, event_step=None, script=[{"decision": "rejected"}]
    )
    assert r.orch.run() == "WAIT_HUMAN"
    assert r.robot.step_calls == 400 and r.robot.home_calls == 0
    assert results(r.orch)[0].stop_reason == "horizon_exhausted"
    assert results(r.orch)[0].task_outcome == "failure"
    assert sm.check_journal(r.orch.journal.events) == []


def test_old_chunk_epoch_is_rejected_after_pause(tmp_path):
    r = build(tmp_path, script=[{"decision": "rejected"}, {"decision": "confirmed"}])
    assert r.orch.run() == "WAIT_HUMAN"
    epoch_values = [c[2] for c in r.authority.calls if c[0] == "begin"]
    assert epoch_values == sorted(epoch_values)
    assert r.authority.last_authorization == len(epoch_values)


def test_stop_during_judge_revokes_without_waiting_for_backend(tmp_path):
    r = build(tmp_path)
    collect = r.verifier.collect
    stops = []

    def interrupt(ticket, **kw):
        if not stops:
            stops.append(r.orch.stop("stop-during-judge"))
        return collect(ticket, **kw)

    r.verifier.collect = interrupt
    assert r.orch.run() == "FAULT_LOCKED"
    assert stops[0].ok and r.authority.revoked
    assert r.robot.step_calls == 8 and r.robot.home_calls == 0


def test_operator_stop_during_judge_keeps_operator_stop_reason_and_slot(tmp_path):
    r = build(tmp_path)
    collect = r.verifier.collect
    stops = []

    def interrupt(ticket, **kw):
        if not stops:
            stops.append(r.orch.stop("stop-reason"))
        return collect(ticket, **kw)

    r.verifier.collect = interrupt
    assert r.orch.run() == "FAULT_LOCKED"
    assert stops[0].ok and r.authority.revoked
    assert r.authority.active is not None  # the request slot stays latched
    result = results(r.orch)[0]
    assert result.stop_reason == "operator_stop"
    assert result.task_outcome == "unknown"
    assert sm.check_journal(r.orch.journal.events) == []


@pytest.mark.parametrize("decision", ["unknown", "timeout"])
def test_unproven_judgement_is_not_reported_as_operator_stop(tmp_path, decision):
    item = {"decision": "unknown"} if decision == "unknown" else {"delay_ns": 10**12}
    r = build(tmp_path, script=[item])
    assert r.orch.run() == "FAULT_LOCKED"
    assert results(r.orch)[0].stop_reason == "watchdog_timeout"


def test_new_mode_and_hold_conditions_change_frozen_plan_hash(tmp_path):
    from test_aeri_cli import CONTRACT, job_text

    from levi.automatic.cli import load_job

    path = tmp_path / "job.yaml"
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    text = job_text(tmp_path / "rollouts")
    path.write_text(text)
    legacy = load_job(path)
    mode_text = text.replace(
        "termination:\n",
        "termination:\n  inference_mode: event_pause_judge\n  physical_hold:\n    sample_interval_ns: 1000000\n    stable_window_ns: 2000000\n    max_wait_ns: 100000000\n    position_tolerance_m: 0.001\n    rotation_tolerance_rad: 0.01\n    gripper_tolerance: 0.01\n",
    )
    path.write_text(mode_text)
    new = load_job(path)
    assert new["config"].termination.inference_mode == "event_pause_judge"
    assert new["plan_sha256"] != legacy["plan_sha256"]
    path.write_text(
        mode_text.replace("position_tolerance_m: 0.001", "position_tolerance_m: 0.002")
    )
    assert load_job(path)["plan_sha256"] != new["plan_sha256"]


# plan_sha256 of test_aeri_cli.job_text("/pinned/rollouts") computed with the
# code of fe0e1fd, the last commit before inference_mode/physical_hold existed.
LEGACY_PLAN_SHA256 = "9910f8c6c486f43a15b096d0b9e437e7adda423f152c591c895c221ab9f026fe"


def test_legacy_plan_digest_is_unchanged_by_the_new_mode_fields(tmp_path):
    from test_aeri_cli import CONTRACT, job_text

    from levi.automatic.cli import load_job

    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    (tmp_path / "job.yaml").write_text(job_text("/pinned/rollouts"))
    job = load_job(tmp_path / "job.yaml")
    assert "inference_mode" not in job["plan"]["run"]["termination"]
    assert "physical_hold" not in job["plan"]["run"]["termination"]
    assert job["plan_sha256"] == LEGACY_PLAN_SHA256


def test_resume_cannot_reissue_motion_after_registered_stop(tmp_path):
    r = build(tmp_path, script=[{"decision": "rejected"}])
    original = r.orch._issue_intervention_motion

    def issue(ctx, tx):
        if tx.prepared.reason == "inference_resumed":
            r.orch.stop("stop-before-new-token")
        return original(ctx, tx)

    r.orch._issue_intervention_motion = issue
    assert r.orch.run() == "FAULT_LOCKED"
    assert r.orch.fence.current is None
    assert r.robot.step_calls == 8


def test_real_binding_refuses_mode_before_reading_binding_or_robot(tmp_path):
    from levi.automatic.fr3_run import load_binding

    cfg = config(
        termination=TerminationConfig(
            inference_mode="event_pause_judge", physical_hold=HOLD
        )
    )
    with pytest.raises(ValueError, match="real execution refused"):
        load_binding(tmp_path / "does-not-exist.json", {"config": cfg})
