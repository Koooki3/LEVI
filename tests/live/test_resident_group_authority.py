"""Explicit fake observations only: no GPU, process, service or robot probes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from levi.domain import aeri
from levi.live.runtime_ownership import (
    GroupExitProof,
    GroupObservation,
    MemberExitProof,
    MemberObservation,
    OwnedGroup,
    OwnedMember,
    RequestStopProof,
    RoleWindow,
    RuntimeObservation,
    RuntimeOwnership,
)
from levi.live.workload_broker import (
    ExitProof,
    IdleProof,
    SafeWindow,
    WorkerIdentity,
    WorkloadBroker,
)

NOW = 1_000_000_000
MS = 1_000_000
CLOCK = "host-mono:fake"
PROFILE = "ab" * 32
ENV = {
    "schema": aeri.SCHEMAS["runtime"],
    "minor": aeri.MINOR,
    "run_id": "run",
    "emitted_wall_ns": 0,
}


def deadline(now=NOW, budget_ms=500):
    return aeri.Deadline(
        due_ns=now + budget_ms * MS,
        clock_domain=CLOCK,
        budget_ms=budget_ms,
        hardness="hard",
    )


def setup():
    broker = WorkloadBroker(run_id="run", clock_domain=CLOCK, profile_sha256=PROFILE)
    broker.prove_idle(
        IdleProof(broker.broker_id, NOW, CLOCK, True, True, True), now_ns=NOW
    )
    members = tuple(
        OwnedMember(role, WorkerIdentity(101 + i, 1, "fake"), role + ":cap")
        for i, role in enumerate(("forward", "judge", "reset"))
    )
    group = OwnedGroup("models", WorkerIdentity(100, 1, "fake"), members)
    declaration = aeri.ResidentGroupSpec(
        group_id=group.group_id,
        members=[{"role": m.role, "capability_id": m.capability_id} for m in members],
    )
    spec = aeri.WorkloadSpec(
        **ENV,
        kind="workload_spec",
        workload_id="models",
        workload_class="resident_group",
        requester="a",
        device_preference="gpu",
        memory_estimate_mib=1000,
        memory_source="declared",
        duration_p99_ms=10,
        duration_source="declared",
        preemptibility="request_boundary",
        resident_group=declaration,
    )
    pressure = aeri.ResourcePressure(
        **ENV,
        kind="pressure",
        observed_ns=NOW,
        clock_domain=CLOCK,
        source="fake",
        free_mib=5000,
        vllm_state="awake",
        gate_open=True,
        gate_code="open",
        policy_server_seen=False,
        age_ms=0,
    )
    window = SafeWindow(
        broker.broker_id, "run", None, PROFILE, NOW, NOW + 500 * MS, CLOCK
    )
    assert broker.submit(spec, deadline(), profile_sha256=PROFILE, now_ns=NOW) is None
    lease = broker.admit(pressure, window, now_ns=NOW)
    evidence = GroupObservation(
        group,
        tuple(MemberObservation(m, True, True, True, True) for m in members),
        "driver",
        1,
        "session",
        True,
        True,
        True,
    )
    obs = RuntimeObservation(
        lease.lease_id,
        lease.lease_epoch,
        group.supervisor,
        NOW,
        CLOCK,
        True,
        True,
        True,
        True,
        evidence,
    )
    owner = RuntimeOwnership(broker)
    token = owner.bind_group(lease, group, obs, now_ns=NOW)
    return owner, token, obs, pressure, window, spec


def role_window(
    owner,
    role="judge",
    *,
    epoch=1,
    authorization=1,
    episode="run.forward.0001",
    now=NOW,
):
    return RoleWindow(
        owner.broker.broker_id,
        "run",
        episode,
        PROFILE,
        now,
        now + 100 * MS,
        CLOCK,
        role,
        epoch,
        authorization,
    )


def begin(
    owner,
    token,
    obs,
    *,
    role="judge",
    epoch=1,
    authorization=1,
    episode="run.forward.0001",
    now=NOW,
    window=None,
):
    return owner.begin_role_request(
        token,
        role,
        deadline(now),
        obs,
        window
        or role_window(
            owner,
            role,
            epoch=epoch,
            authorization=authorization,
            episode=episode,
            now=now,
        ),
        episode_id=episode,
        policy_epoch=epoch,
        now_ns=now,
    )


def finish(owner, request, obs, now=NOW):
    return owner.finish_request(
        request, RequestStopProof(request, now, CLOCK, True, True), obs, now_ns=now
    )


def test_three_roles_have_one_slot_and_windows_cannot_be_replayed():
    owner, token, obs, *_ = setup()

    def compete(role):
        try:
            return begin(owner, token, obs, role=role)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(compete, ("forward", "judge", "reset")))
    request = next(r for r in results if r is not None)
    assert sum(r is not None for r in results) == 1
    assert finish(owner, request, obs)
    with pytest.raises(ValueError, match="role window"):
        begin(owner, token, obs, role=request.role)
    begin(owner, token, obs, authorization=2)


@pytest.mark.parametrize(
    "field",
    ["backend_idle", "request_workers_exited", "process_running", "capability_owned"],
)
@pytest.mark.parametrize("unproven", [False, None, 1])
def test_every_member_must_prove_identity_and_idle(field, unproven):
    owner, token, obs, *_ = setup()
    members = obs.group.members
    bad = replace(
        obs,
        group=replace(
            obs.group,
            members=(members[0], replace(members[1], **{field: unproven}), members[2]),
        ),
    )
    with pytest.raises(ValueError):
        begin(owner, token, bad)
    assert owner.snapshot(now_ns=NOW)["request"] is None
    assert owner.broker.snapshot(now_ns=NOW)["active"] is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"flock_held": None},
        {"driver_held": False},
        {"driver_generation": 2},
        {"driver_session": "other"},
        {"membership_complete": None},
        {"members": ()},
    ],
)
def test_unknown_driver_flock_or_membership_latches_closed(changes):
    owner, token, obs, *_ = setup()
    with pytest.raises(ValueError):
        begin(owner, token, replace(obs, group=replace(obs.group, **changes)))
    with pytest.raises(ValueError):
        begin(owner, token, obs)


def test_group_finish_needs_all_backend_and_local_workers_not_client_done():
    owner, token, obs, *_ = setup()
    request = begin(owner, token, obs)
    member = replace(obs.group.members[0], backend_idle=False)
    busy = replace(
        obs, group=replace(obs.group, members=(member,) + obs.group.members[1:])
    )
    with pytest.raises(ValueError, match="backend-stop"):
        finish(owner, request, busy)
    assert owner.snapshot(now_ns=NOW)["request"] == request
    assert finish(owner, request, obs)
    with pytest.raises(ValueError, match="new episode"):
        begin(owner, token, obs, episode="run.forward.0002", authorization=2)
    next_request = begin(
        owner, token, obs, episode="run.forward.0002", epoch=2, authorization=2
    )
    assert next_request.episode_id == "run.forward.0002"


def test_atomic_renew_updates_original_owner_and_fences_old_tokens():
    owner, token, obs, pressure, window, _ = setup()
    old_lease = owner.snapshot(now_ns=NOW)["lease"]
    later = NOW + MS
    fresh = replace(obs, observed_ns=later)
    new = owner.renew_resident(
        token,
        deadline(later, 1000),
        pressure.model_copy(update={"observed_ns": later}),
        replace(window, observed_ns=later, expires_ns=later + 1000 * MS),
        fresh,
        now_ns=later,
    )
    lease = owner.snapshot(now_ns=later)["lease"]
    assert new.worker == token.worker and new.runtime_epoch == token.runtime_epoch + 1
    assert (
        lease.lease_id == old_lease.lease_id
        and lease.lease_epoch == old_lease.lease_epoch + 1
    )
    assert owner.broker.snapshot(now_ns=later)["active"] == lease.model_dump(
        by_alias=True
    )
    for cancel_lease in (None, old_lease):
        with pytest.raises(ValueError):
            owner.broker.cancel("models", lease=cancel_lease, now_ns=later)
    with pytest.raises(ValueError):
        owner.renew_resident(
            token, deadline(later), pressure, window, fresh, now_ns=later
        )
    assert not owner.broker.snapshot(now_ns=later)["revoked"]
    new_obs = replace(fresh, lease_epoch=new.lease_epoch)
    begin(owner, new, new_obs, now=later)


@pytest.mark.parametrize(
    "case",
    [
        "request",
        "expired",
        "revoked",
        "unknown",
        "gate",
        "memory",
        "priority",
        "deadline",
    ],
)
def test_renew_refusal_never_mutates_or_resurrects_owner(case):
    owner, token, obs, pressure, window, spec = setup()
    lease = owner.snapshot(now_ns=NOW)["lease"]
    now = NOW
    due = deadline()
    if case == "request":
        begin(owner, token, obs)
    elif case == "expired":
        now = lease.expires_ns
        obs = replace(obs, observed_ns=now)
    elif case == "revoked":
        owner.broker.cancel("models", lease=lease, now_ns=now)
    elif case == "unknown":
        obs = replace(obs, group=replace(obs.group, flock_held=None))
    elif case == "gate":
        pressure = pressure.model_copy(update={"gate_open": False})
    elif case == "memory":
        pressure = pressure.model_copy(update={"free_mib": 0})
    elif case == "deadline":
        due = deadline(budget_ms=1)
    else:
        higher = spec.model_copy(
            update={
                "workload_id": "policy",
                "workload_class": "policy_realtime",
                "resident_group": None,
            }
        )
        owner.broker.submit(higher, due, profile_sha256=PROFILE, now_ns=now)
    with pytest.raises(ValueError):
        owner.renew_resident(token, due, pressure, window, obs, now_ns=now)
    assert owner.snapshot(now_ns=now)["runtime"] == token
    assert owner.snapshot(now_ns=now)["lease"] == lease
    assert owner.broker.snapshot(now_ns=now)["active"] == lease.model_dump(
        by_alias=True
    )


def test_group_exit_cannot_bypass_original_broker_or_omit_a_member():
    owner, token, obs, *_ = setup()
    lease = owner.snapshot(now_ns=NOW)["lease"]
    proof = ExitProof(
        owner.broker.broker_id,
        lease.lease_id,
        lease.lease_epoch,
        token.worker,
        NOW,
        CLOCK,
        True,
        True,
        True,
    )
    with pytest.raises(ValueError, match="group exit"):
        owner.broker.release_after_exit(lease, proof, now_ns=NOW)
    all_exit = GroupExitProof(
        **proof.__dict__,
        group=obs.group.group,
        members=tuple(
            MemberExitProof(m, True, True, True) for m in obs.group.group.members
        ),
        membership_complete=True,
    )
    with pytest.raises(ValueError):
        owner.release_after_exit(
            token, replace(all_exit, members=all_exit.members[:-1]), now_ns=NOW
        )
    owner.release_after_exit(token, all_exit, now_ns=NOW)
    assert owner.broker.snapshot(now_ns=NOW)["active"] is None


def test_resident_class_stays_separate_from_episode_policy_gate():
    owner, token, obs, pressure, window, _ = setup()
    owner.broker.admit(
        pressure.model_copy(
            update={"gate_open": False, "gate_code": "policy_inferring"}
        ),
        window,
        now_ns=NOW,
    )
    assert not owner.broker.snapshot(now_ns=NOW)["revoked"]
    assert owner.snapshot(now_ns=NOW)["runtime"] == token
    with pytest.raises(ValueError):
        owner.begin_request(token, deadline(), obs, now_ns=NOW)


def test_complete_group_exit_resolves_stranded_request_without_publishing():
    owner, token, obs, *_ = setup()
    request = begin(owner, token, obs)
    lease = owner.snapshot(now_ns=NOW)["lease"]
    proof = GroupExitProof(
        owner.broker.broker_id,
        lease.lease_id,
        lease.lease_epoch,
        token.worker,
        NOW,
        CLOCK,
        True,
        True,
        True,
        obs.group.group,
        tuple(MemberExitProof(m, True, True, True) for m in obs.group.group.members),
        True,
    )
    owner.release_after_exit(token, proof, now_ns=NOW)
    assert owner.snapshot(now_ns=NOW)["request"] is None
    assert not owner.accepts_result(request, obs, now_ns=NOW)


def test_two_renewals_cannot_both_change_the_same_epoch():
    owner, token, obs, pressure, window, _ = setup()

    def renew(_):
        try:
            return owner.renew_resident(
                token, deadline(), pressure, window, obs, now_ns=NOW
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(renew, range(2)))
    assert sum(t is not None for t in tokens) == 1
    assert owner.snapshot(now_ns=NOW)["lease"].lease_epoch == 2


@pytest.mark.parametrize(
    "change",
    [
        {"broker_id": "other"},
        {"run_id": "other"},
        {"profile_sha256": "cd" * 32},
        {"clock_domain": "host-mono:other"},
        {"observed_ns": NOW + 1},
        {"episode_id": "run.forward.0002"},
        {"policy_epoch": -1},
        {"authorization_epoch": True},
    ],
)
def test_foreign_future_or_epoch_mismatched_role_window_rejected(change):
    owner, token, obs, *_ = setup()
    with pytest.raises(ValueError):
        begin(owner, token, obs, window=replace(role_window(owner), **change))
    assert owner.snapshot(now_ns=NOW)["request"] is None


def test_model_construction_failure_keeps_both_renew_epochs(monkeypatch):
    import levi.live.runtime_ownership as module

    owner, token, obs, pressure, window, _ = setup()
    lease = owner.snapshot(now_ns=NOW)["lease"]

    def fail(*args):
        raise RuntimeError("token construction failed")

    monkeypatch.setattr(module, "RuntimeToken", fail)
    with pytest.raises(RuntimeError):
        owner.renew_resident(token, deadline(), pressure, window, obs, now_ns=NOW)
    assert owner.snapshot(now_ns=NOW)["lease"] == lease
    assert owner.snapshot(now_ns=NOW)["runtime"] == token
    assert owner.broker.snapshot(now_ns=NOW)["active"] == lease.model_dump(
        by_alias=True
    )


def test_resident_priority_and_declaration_are_explicit():
    owner, token, _obs, pressure, window, spec = setup()
    assert (
        aeri.WORKLOAD_PRIORITY["policy_realtime"]
        < aeri.WORKLOAD_PRIORITY["judge_critical"]
    )
    assert (
        aeri.WORKLOAD_PRIORITY["judge_critical"]
        < aeri.WORKLOAD_PRIORITY["resident_group"]
    )
    assert (
        aeri.WORKLOAD_PRIORITY["resident_group"]
        < aeri.WORKLOAD_PRIORITY["background_annotation"]
    )
    with pytest.raises(ValueError):
        aeri.WorkloadSpec.model_validate(
            spec.model_dump(by_alias=True) | {"resident_group": None}
        )
    with pytest.raises(ValueError):
        aeri.WorkloadSpec.model_validate(
            spec.model_dump(by_alias=True) | {"workload_class": "policy_realtime"}
        )
    critical = spec.model_copy(
        update={
            "workload_id": "critical",
            "workload_class": "judge_critical",
            "resident_group": None,
        }
    )
    assert (
        owner.broker.submit(critical, deadline(), profile_sha256=PROFILE, now_ns=NOW)
        is None
    )
    assert owner.broker.admit(pressure, window, now_ns=NOW).code == "busy"
    assert owner.broker.snapshot(now_ns=NOW)["revoked"]
    assert owner.snapshot(now_ns=NOW)["runtime"] == token


def test_begin_and_renew_race_has_exactly_one_winner():
    owner, token, obs, pressure, window, _ = setup()

    def operation(kind):
        try:
            if kind == "begin":
                return begin(owner, token, obs)
            return owner.renew_resident(
                token, deadline(), pressure, window, obs, now_ns=NOW
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(operation, ("begin", "renew")))
    assert sum(r is not None for r in results) == 1
    state = owner.snapshot(now_ns=NOW)
    assert (state["request"] is not None) != (state["lease"].lease_epoch == 2)


@pytest.mark.parametrize("unproven", [False, None, 1])
def test_live_or_unknown_member_descendant_cannot_release_group(unproven):
    owner, token, obs, *_ = setup()
    lease = owner.snapshot(now_ns=NOW)["lease"]
    members = tuple(
        MemberExitProof(m, True, True, True) for m in obs.group.group.members
    )
    members = (replace(members[0], descendants_exited=unproven),) + members[1:]
    proof = GroupExitProof(
        owner.broker.broker_id,
        lease.lease_id,
        lease.lease_epoch,
        token.worker,
        NOW,
        CLOCK,
        True,
        True,
        True,
        obs.group.group,
        members,
        True,
    )
    with pytest.raises(ValueError):
        owner.broker.release_after_exit(lease, proof, now_ns=NOW)
    assert owner.broker.snapshot(now_ns=NOW)["active"] is not None


def test_role_result_expires_and_late_complete_barrier_can_only_clear_slot():
    owner, token, obs, *_ = setup()
    request = begin(owner, token, obs)
    later = request.expires_ns
    fresh = replace(obs, observed_ns=later)
    assert not owner.accepts_result(request, fresh, now_ns=later)
    assert not finish(owner, request, fresh, now=later)
    assert owner.snapshot(now_ns=later)["runtime"] == token
    assert owner.broker.snapshot(now_ns=later)["active"] is not None


def test_changed_backend_identity_cannot_recover_by_restoring_old_observation():
    owner, token, obs, *_ = setup()
    request = begin(owner, token, obs)
    m = obs.group.members[0]
    changed = replace(
        m, member=replace(m.member, worker=replace(m.member.worker, start_ticks=2))
    )
    stale = replace(
        obs, group=replace(obs.group, members=(changed,) + obs.group.members[1:])
    )
    assert not owner.accepts_result(request, stale, now_ns=NOW)
    assert not owner.accepts_result(request, obs, now_ns=NOW)
    assert not finish(owner, request, obs)
    with pytest.raises(ValueError):
        begin(owner, token, obs, authorization=2)
