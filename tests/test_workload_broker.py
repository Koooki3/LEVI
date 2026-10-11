"""GPU admission under concurrency, stale epochs and physical-owner uncertainty."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from levi.domain import aeri
from levi.live.workload_broker import (
    BrokerConfig,
    ExitProof,
    IdleProof,
    SafeWindow,
    WorkerIdentity,
    WorkloadBroker,
)

RUN = "run"
CLOCK = "host-mono:test-boot"
PROFILE = "ab" * 32
NOW = 1_000_000_000
EP = "run.forward.0001"


def broker(**options):
    return WorkloadBroker(
        run_id=RUN, clock_domain=CLOCK, profile_sha256=PROFILE, **options
    )


def envelope():
    return {
        "schema": aeri.SCHEMAS["runtime"],
        "minor": aeri.MINOR,
        "run_id": RUN,
        "emitted_wall_ns": 0,
    }


def spec(wid="w", workload_class="judge_critical", **over):
    values = dict(
        **envelope(),
        kind="workload_spec",
        workload_id=wid,
        workload_class=workload_class,
        requester="c",
        episode_id=EP,
        device_preference="gpu",
        memory_estimate_mib=1000,
        memory_source="measured",
        duration_p99_ms=100,
        duration_source="measured",
        preemptibility="request_boundary",
    )
    return aeri.WorkloadSpec(**{**values, **over})


def deadline(**over):
    return aeri.Deadline(
        **{
            "due_ns": NOW + 500 * 1_000_000,
            "clock_domain": CLOCK,
            "budget_ms": 500,
            "hardness": "hard",
            **over,
        }
    )


def pressure(**over):
    values = dict(
        **envelope(),
        kind="pressure",
        observed_ns=NOW,
        clock_domain=CLOCK,
        source="fake",
        free_mib=5000,
        policy_mib=2000,
        vllm_state="awake",
        gate_open=True,
        gate_code="open",
        policy_server_seen=True,
        age_ms=0,
    )
    return aeri.ResourcePressure(**{**values, **over})


def window(b, **over):
    return SafeWindow(
        **{
            "broker_id": b.broker_id,
            "run_id": RUN,
            "episode_id": EP,
            "profile_sha256": PROFILE,
            "observed_ns": NOW,
            "expires_ns": NOW + 500 * 1_000_000,
            "clock_domain": CLOCK,
            **over,
        }
    )


def idle(b, **over):
    return IdleProof(
        **{
            "broker_id": b.broker_id,
            "lease_epoch": b.snapshot(now_ns=NOW)["lease_epoch"],
            "observed_ns": NOW,
            "clock_domain": CLOCK,
            "workers_exited": True,
            "descendants_exited": True,
            "owned_gpu_released": True,
            **over,
        }
    )


def ready(**options):
    b = broker(**options)
    b.prove_idle(idle(b), now_ns=NOW)
    return b


def submit(b, workload=None, due=None, *, now_ns=NOW):
    return b.submit(
        workload or spec(), due or deadline(), profile_sha256=PROFILE, now_ns=now_ns
    )


def grant(b, workload=None):
    assert submit(b, workload) is None
    lease = b.admit(pressure(), window(b), now_ns=NOW)
    assert isinstance(lease, aeri.ResourceLease)
    return lease


def worker(pid=100):
    return WorkerIdentity(pid, 123, "test-boot")


def exited(b, lease, identity, **over):
    return ExitProof(
        **{
            "broker_id": b.broker_id,
            "worker": identity,
            "lease_id": lease.lease_id,
            "lease_epoch": lease.lease_epoch,
            "observed_ns": NOW,
            "clock_domain": CLOCK,
            "process_exited": True,
            "descendants_exited": True,
            "owned_gpu_released": True,
            **over,
        }
    )


def test_restart_unknown_ownership_is_not_inferred_from_empty_queue_or_free_vram():
    b = broker()
    submit(b)
    denied = b.admit(pressure(), window(b), now_ns=NOW)
    assert denied.code == "broker_unavailable"
    assert b.snapshot(now_ns=NOW)["active"] is None
    with pytest.raises(ValueError):
        b.prove_idle(idle(b, owned_gpu_released=None), now_ns=NOW)
    b.prove_idle(idle(b), now_ns=NOW)
    lease = b.admit(pressure(), window(b), now_ns=NOW)
    assert isinstance(lease, aeri.ResourceLease)
    replacement = broker()
    submit(replacement)
    with pytest.raises(ValueError):
        replacement.prove_idle(idle(b), now_ns=NOW)
    assert not replacement.accepts_result(lease, now_ns=NOW)
    assert (
        replacement.admit(pressure(), window(replacement), now_ns=NOW).code
        == "broker_unavailable"
    )


def test_contract_priority_policy_then_critical_judge_then_other_work():
    b = ready()
    for kind in reversed(aeri.WORKLOAD_CLASSES):
        assert submit(b, spec(kind, kind)) is None
    granted = []
    for number, expected in enumerate(aeri.WORKLOAD_CLASSES):
        lease = b.admit(pressure(), window(b), now_ns=NOW)
        assert isinstance(lease, aeri.ResourceLease)
        assert lease.workload_class == expected
        assert aeri.parse(lease.model_dump_json(by_alias=True), "runtime") == lease
        identity = worker(100 + number)
        b.bind_worker(lease, identity, now_ns=NOW)
        assert b.accepts_result(lease, now_ns=NOW)
        granted.append(lease)
        b.release_after_exit(lease, exited(b, lease, identity), now_ns=NOW)
    assert [l.lease_epoch for l in granted] == [1, 2, 3, 4, 5]
    assert not b.accepts_result(granted[0], now_ns=NOW)


def test_concurrent_submit_and_admit_has_exactly_one_physical_owner():
    b = ready()
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(lambda i: submit(b, spec(f"w{i}")), range(32)))
    assert replies == [None] * 32
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(
            pool.map(lambda _: b.admit(pressure(), window(b), now_ns=NOW), range(32))
        )
    leases = [r for r in replies if isinstance(r, aeri.ResourceLease)]
    assert len(leases) == 1
    assert all(
        isinstance(r, (aeri.ResourceLease, aeri.AdmissionRejected)) for r in replies
    )
    assert b.snapshot(now_ns=NOW)["queued"] == 31
    assert not b.accepts_result(leases[0], now_ns=NOW)  # no physical worker receipt


@pytest.mark.parametrize("mode", ["request_boundary", "cooperative", "none"])
def test_preemption_requests_exit_not_immediate_gpu_reassignment(mode):
    b = ready()
    lease = grant(b, spec("background", "background_annotation", preemptibility=mode))
    identity = worker()
    b.bind_worker(lease, identity, now_ns=NOW)
    assert submit(b, spec("policy", "policy_realtime")) is None
    denied = b.admit(pressure(), window(b), now_ns=NOW)
    assert denied.code == "policy_priority"
    preemption = b.preemption_request()
    if mode == "none":
        assert preemption is None and b.accepts_result(lease, now_ns=NOW)
    else:
        assert preemption["mode"] == mode and preemption["worker"] == identity
        assert not b.accepts_result(lease, now_ns=NOW)
    assert b.snapshot(now_ns=NOW)["active"]["lease_id"] == lease.lease_id
    b.release_after_exit(lease, exited(b, lease, identity), now_ns=NOW)
    higher = b.admit(pressure(), window(b), now_ns=NOW)
    assert higher.workload_class == "policy_realtime"


@pytest.mark.parametrize("cause", ["cancel", "expiry"])
def test_cancel_or_expiry_keeps_worker_slot_until_exact_exit_proof(cause):
    b = ready()
    lease = grant(b)
    identity = worker()
    b.bind_worker(lease, identity, now_ns=NOW)
    now = NOW
    if cause == "cancel":
        assert b.cancel("w", now_ns=now)
    else:
        now = lease.expires_ns
    assert not b.accepts_result(lease, now_ns=now)
    due = deadline(due_ns=now + 500 * 1_000_000)
    assert submit(b, spec("next"), due, now_ns=now) is None
    current_pressure = pressure(observed_ns=now)
    current_window = window(b, observed_ns=now, expires_ns=now + 500 * 1_000_000)
    assert b.admit(current_pressure, current_window, now_ns=now).code == "busy"
    for changes in (
        {"worker": worker(200)},
        {"process_exited": False},
        {"descendants_exited": None},
        {"owned_gpu_released": False},
        {"broker_id": "broker:old"},
        {"clock_domain": "host-mono:old"},
    ):
        with pytest.raises(ValueError):
            b.release_after_exit(
                lease,
                exited(b, lease, identity, observed_ns=now, **changes),
                now_ns=now,
            )
    assert b.snapshot(now_ns=now)["active"] is not None
    b.release_after_exit(lease, exited(b, lease, identity, observed_ns=now), now_ns=now)
    assert isinstance(
        b.admit(current_pressure, current_window, now_ns=now), aeri.ResourceLease
    )


def test_unstarted_reservation_stays_owned_until_explicit_idle_proof():
    b = ready()
    lease = grant(b)
    b.cancel("w", now_ns=NOW)
    with pytest.raises(ValueError):
        b.release_unstarted(lease, idle(b, workers_exited=None), now_ns=NOW)
    assert b.snapshot(now_ns=NOW)["active"] is not None
    b.release_unstarted(lease, idle(b), now_ns=NOW)
    lease = grant(b, spec("next"))
    b.bind_worker(lease, worker(), now_ns=NOW)
    with pytest.raises(ValueError):
        b.release_unstarted(lease, idle(b), now_ns=NOW)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"free_mib": None}, "broker_unavailable"),
        ({"policy_server_seen": None}, "broker_unavailable"),
        ({"policy_mib": None}, "broker_unavailable"),
        ({"vllm_state": "unknown"}, "broker_unavailable"),
        ({"vllm_state": "starting"}, "broker_unavailable"),
        ({"gate_open": None}, "evaluation_active"),
        ({"gate_open": False, "gate_code": "policy_inferring"}, "policy_priority"),
        ({"gate_open": True, "gate_code": "policy_inferring"}, "policy_priority"),
        ({"gate_open": False, "gate_code": "episode_imminent"}, "episode_imminent"),
        ({"gate_open": False, "gate_code": "unknown_client"}, "unknown_client"),
        ({"clock_domain": "host-mono:other"}, "broker_unavailable"),
        ({"observed_ns": NOW + 1}, "broker_unavailable"),
        ({"observed_ns": 0, "age_ms": 1}, "broker_unavailable"),
        ({"age_ms": 1001}, "broker_unavailable"),
        ({"free_mib": 1255}, "no_memory"),
        ({"vllm_state": "absent"}, "cold_start_forbidden"),
        ({"policy_server_seen": False, "policy_mib": 2000}, "unknown_client"),
    ],
)
def test_unknown_stale_contradictory_or_closed_pressure_never_grants(changes, expected):
    b = ready()
    submit(b)
    refusal = b.admit(pressure(**changes), window(b), now_ns=NOW)
    assert refusal.code == expected
    assert b.snapshot(now_ns=NOW)["active"] is None
    assert aeri.parse(refusal.model_dump_json(by_alias=True), "runtime") == refusal


@pytest.mark.parametrize(
    "changes",
    [
        {"broker_id": "broker:old"},
        {"run_id": "other"},
        {"episode_id": "run.reset.0001"},
        {"profile_sha256": "cd" * 32},
        {"clock_domain": "host-mono:old"},
        {"observed_ns": NOW + 1},
        {"observed_ns": -1},
        {"expires_ns": NOW},
    ],
)
def test_window_requires_current_run_profile_episode_clock_and_freshness(changes):
    b = ready()
    submit(b)
    assert (
        b.admit(pressure(), window(b, **changes), now_ns=NOW).code
        == "evaluation_active"
    )
    assert b.admit(pressure(), None, now_ns=NOW).code == "evaluation_active"


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"memory_estimate_mib": None, "memory_source": "unknown"}, "no_memory"),
        (
            {"duration_p99_ms": None, "duration_source": "unknown"},
            "deadline_infeasible",
        ),
        ({"memory_estimate_mib": 0}, "no_memory"),
        ({"duration_p99_ms": 0}, "deadline_infeasible"),
        ({"device_preference": "cpu"}, "broker_unavailable"),
    ],
)
def test_unknown_workload_memory_duration_or_cpu_policy_refused(fields, expected):
    b = ready()
    assert submit(b, spec(**fields)).code == expected
    assert b.snapshot(now_ns=NOW)["queued"] == 0


@pytest.mark.parametrize("bound", ["due", "window", "budget", "waited"])
def test_complete_p99_plus_margin_must_fit_every_deadline_bound(bound):
    b = ready()
    due = (
        deadline(due_ns=NOW + 109 * 1_000_000)
        if bound == "due"
        else deadline(budget_ms=109)
        if bound == "budget"
        else deadline()
    )
    assert submit(b, due=due) is None
    win = (
        window(b, expires_ns=NOW + 109 * 1_000_000) if bound == "window" else window(b)
    )
    now = NOW + 400 * 1_000_000 if bound == "waited" else NOW
    refusal = b.admit(pressure(), win, now_ns=now)
    assert refusal.code == "deadline_infeasible"
    assert b.snapshot(now_ns=now)["active"] is None


def test_frozen_profile_and_clock_are_checked_before_queueing():
    b = ready()
    assert (
        b.submit(spec(), deadline(), profile_sha256="cd" * 32, now_ns=NOW).code
        == "unknown_client"
    )
    assert (
        submit(b, due=deadline(clock_domain="host-mono:old")).code == "unknown_client"
    )
    assert (
        submit(b, spec(run_id="other", episode_id="other.forward.0001")).code
        == "unknown_client"
    )
    assert b.snapshot(now_ns=NOW)["queued"] == 0


def test_old_lease_epoch_or_tampered_contract_cannot_release_or_authorize_result():
    b = ready()
    lease = grant(b)
    identity = worker()
    b.bind_worker(lease, identity, now_ns=NOW)
    old = lease.model_copy(update={"lease_epoch": lease.lease_epoch + 1})
    assert not b.accepts_result(old, now_ns=NOW)
    with pytest.raises(ValueError):
        b.release_after_exit(old, exited(b, lease, identity), now_ns=NOW)
    with pytest.raises(ValueError):
        b.bind_worker(lease, worker(200), now_ns=NOW)
    assert b.accepts_result(lease, now_ns=NOW)
    invalid = spec("bad").model_copy(update={"duration_p99_ms": None})
    with pytest.raises(ValueError):
        submit(b, invalid)


def test_forward_reset_rotate_same_run_profile_without_parallel_workers():
    b = ready()
    forward = grant(b)
    identity = worker()
    b.bind_worker(forward, identity, now_ns=NOW)
    reset_spec = spec("reset", episode_id="run.reset.0001")
    assert submit(b, reset_spec) is None
    reset_window = window(b, episode_id="run.reset.0001")
    assert b.admit(pressure(), reset_window, now_ns=NOW).code == "busy"
    b.release_after_exit(forward, exited(b, forward, identity), now_ns=NOW)
    reset = b.admit(pressure(), reset_window, now_ns=NOW)
    assert isinstance(reset, aeri.ResourceLease)
    assert (
        reset.lease_id != forward.lease_id and reset.lease_epoch > forward.lease_epoch
    )
    assert not b.accepts_result(forward, now_ns=NOW)


def test_queue_capacity_duplicate_and_expired_notices_are_bounded():
    b = ready(config=BrokerConfig(max_queue=2))
    assert submit(b, spec("one")) is None
    assert submit(b, spec("one")).code == "busy"
    assert submit(b, spec("two")) is None
    assert submit(b, spec("three")).code == "busy"
    assert b.admit(pressure(), window(b), now_ns=NOW + 600 * 1_000_000) is None
    notices = b.drain_rejections(limit=2)
    assert len(notices) == 2 and {n.workload_id for n in notices} == {"one", "two"}
    assert all(n.code == "deadline_infeasible" for n in notices)
    assert b.snapshot(now_ns=NOW + 600 * 1_000_000)["queued"] == 0


def test_receiver_clock_cannot_move_backwards():
    b = ready()
    lease = grant(b)
    with pytest.raises(ValueError):
        b.accepts_result(lease, now_ns=NOW - 1)
    with pytest.raises(ValueError):
        b.admit(pressure(), window(b), now_ns=True)


def test_kernel_does_not_probe_run_start_daemon_or_issue_robot_commands(monkeypatch):
    import builtins
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("I/O or process start from pure broker")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(socket.socket, "connect", forbidden)
        patch.setattr(subprocess, "run", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        b = ready()
        lease = grant(b)
        b.bind_worker(lease, worker(), now_ns=NOW)
        assert b.accepts_result(lease, now_ns=NOW)
    assert "policy_realtime" in aeri.WORKLOAD_PRIORITY
    assert not any("watchdog" in k or "stop" in k for k in aeri.WORKLOAD_PRIORITY)


def test_pressure_failure_revokes_model_results_even_without_waiting_requests():
    b = ready()
    lease = grant(b)
    identity = worker()
    b.bind_worker(lease, identity, now_ns=NOW)
    assert (
        b.admit(
            pressure(gate_open=False, gate_code="policy_inferring"), None, now_ns=NOW
        )
        is None
    )
    assert not b.accepts_result(lease, now_ns=NOW)
    assert b.preemption_request()["mode"] == "request_boundary"
    assert b.snapshot(now_ns=NOW)["active"]["lease_id"] == lease.lease_id
    with pytest.raises(ValueError):
        b.prove_idle(idle(b), now_ns=NOW)


def test_cancelled_reservation_can_bind_a_late_started_worker_but_never_accept_results():
    b = ready()
    lease = grant(b)
    assert b.cancel("w", now_ns=NOW)
    b.bind_worker(lease, worker(), now_ns=NOW)
    assert not b.accepts_result(lease, now_ns=NOW)
    with pytest.raises(ValueError):
        b.release_unstarted(lease, idle(b), now_ns=NOW)
    b.release_after_exit(lease, exited(b, lease, worker()), now_ns=NOW)


def test_live_policy_owner_is_never_cooperatively_preempted_by_model_gate():
    b = ready()
    lease = grant(b, spec("policy", "policy_realtime"))
    b.bind_worker(lease, worker(), now_ns=NOW)
    assert submit(b, spec("judge")) is None
    assert (
        b.admit(
            pressure(gate_open=False, gate_code="policy_inferring"),
            window(b),
            now_ns=NOW,
        ).code
        == "policy_priority"
    )
    assert b.preemption_request() is None
    assert b.accepts_result(lease, now_ns=NOW)


def test_old_idle_proof_cannot_release_new_reservation_even_at_same_clock_tick():
    b = ready()
    old_idle = idle(b)
    lease = grant(b)
    with pytest.raises(ValueError):
        b.release_unstarted(lease, old_idle, now_ns=NOW)
    b.release_unstarted(lease, idle(b), now_ns=NOW)
    previous_idle = idle(b)
    next_lease = grant(b, spec("next"))
    with pytest.raises(ValueError):
        b.release_unstarted(next_lease, previous_idle, now_ns=NOW)
