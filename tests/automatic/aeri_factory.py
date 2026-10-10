"""Valid AERI messages to build tests and fixtures from (plain dicts; every
call returns a fresh copy). ``fixture_cases`` writes the committed fixtures
under ``tests/automatic/fixtures`` (``python tests/automatic/aeri_factory.py``
regenerates them)."""

import copy
import json
from pathlib import Path

RUN = "r20261010-a"
CLOCK = "host-mono:0f0e0d0c-0b0a-4908-8706-050403020100"
OTHER_CLOCK = "robot:fr3-0"
EPISODE = f"{RUN}.forward.0003"
RESET_EPISODE = f"{RUN}.reset.0003"
ZEROS = "0" * 64
SHA = "ab" * 32
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def envelope(schema, **fields):
    return {
        "schema": f"levi.aeri.{schema}.v1",
        "minor": 0,
        "run_id": RUN,
        "emitted_wall_ns": 1_791_600_000_000_000_000,
        **fields,
    }


def predicates(**values):
    return [
        {"name": name, "value": value, "required": True, "evidence_refs": []}
        for name, value in values.items()
    ]


def event(**over):
    message = envelope(
        "event",
        event_id="ev-17",
        episode_id=EPISODE,
        episode_role="forward",
        seq=4,
        event_type="gripper_open",
        status="candidate",
        retracts=None,
        priority="goal_candidate",
        actor_id="arm_0",
        step=120,
        window_steps=[115, 125],
        observed_ns=5_000_000_000,
        clock_domain=CLOCK,
        signal_refs=[
            {
                "feature": "gripper_state",
                "dimension": 0,
                "kind": "measured",
                "units": "m",
            }
        ],
        evidence_refs=[{"kind": "frame", "ref": "side@120", "step": 120}],
        salience=0.8,
        detector={"name": "gripper-crossing", "version": "1"},
    )
    message.update(over)
    return message


def retraction(**over):
    fields = {"event_id": "ev-18", "seq": 5, "status": "retracted", "retracts": "ev-17"}
    return event(**{**fields, **over})


def judgement(**over):
    message = envelope(
        "judgement",
        kind="judgement",
        judgement_id="j-8",
        request_id="req-8",
        episode_id=EPISODE,
        episode_role="forward",
        target="forward_goal",
        subgoal=None,
        event_ids=["ev-17"],
        decision="confirmed",
        unknown_reason=None,
        predicate_results=predicates(object_state=True, stable=True),
        vetoes=[],
        observed_from_step=100,
        observed_through_step=130,
        observed_through_ns=5_100_000_000,
        produced_ns=5_900_000_000,
        valid_until_ns=6_900_000_000,
        clock_domain=CLOCK,
        spec_id="generic-final",
        spec_version="1",
        provider="vlm",
        model_name="qwen3.8-27b-int4",
        model_digest=None,
        confidence_kind="none",
        calibrated_probability=None,
        calibration_ref=None,
        cost={"elapsed_ms": 800, "prompt_tokens": 1200, "completion_tokens": 40},
        legacy_c5={"reading": "supported", "outcome": "success", "undecided": False},
    )
    message.update(over)
    return message


def unknown_judgement(**over):
    fields = {
        "judgement_id": "j-9",
        "request_id": "req-9",
        "decision": "unknown",
        "unknown_reason": "model_undecided",
        "predicate_results": predicates(object_state=None, stable=True),
        "legacy_c5": {"reading": "unknown", "outcome": "failure", "undecided": True},
    }
    return judgement(**{**fields, **over})


def unavailable(schema="judgement", **over):
    message = envelope(
        schema,
        kind="unavailable",
        request_id="req-10",
        code="gate_closed",
        retryable=True,
        retry_after_ms=None,
        detail="policy inferring",
        produced_ns=6_000_000_000,
        clock_domain=CLOCK,
    )
    message.update(over)
    return message


def scene(**over):
    message = envelope(
        "scene",
        kind="scene",
        assessment_id="sa-2",
        request_id="req-11",
        episode_id=f"{RUN}.forward.0004",
        target="initial_state",
        decision="ready",
        contract_id="plates-initial",
        contract_version="1",
        predicate_results=predicates(plates_separated=True, gripper_empty=True),
        failed_predicates=[],
        unknown_predicates=[],
        unknown_reason=None,
        evidence_refs=[{"kind": "frame", "ref": "top@0"}],
        observed_ns=7_000_000_000,
        produced_ns=7_500_000_000,
        valid_until_ns=9_500_000_000,
        clock_domain=CLOCK,
        provider="fake",
        model_name=None,
        model_digest=None,
        confidence_kind="none",
        calibrated_probability=None,
        calibration_ref=None,
        cost=None,
    )
    message.update(over)
    return message


def reset_required(**over):
    fields = {
        "assessment_id": "sa-3",
        "request_id": "req-12",
        "decision": "reset_required",
        "predicate_results": predicates(plates_separated=False, gripper_empty=None),
        "failed_predicates": ["plates_separated"],
        "unknown_predicates": ["gripper_empty"],
    }
    return scene(**{**fields, **over})


def deadline(**over):
    value = {
        "due_ns": 8_100_000_000,
        "clock_domain": CLOCK,
        "budget_ms": 100,
        "hardness": "hard",
    }
    value.update(over)
    return value


def runtime(kind, **over):
    bodies = {
        "workload_spec": {
            "workload_id": "w-1",
            "workload_class": "judge_critical",
            "requester": "c",
            "episode_id": EPISODE,
            "device_preference": "gpu",
            "memory_estimate_mib": None,
            "memory_source": "unknown",
            "duration_p99_ms": 1500,
            "duration_source": "measured",
            "preemptibility": "request_boundary",
        },
        "lease": {
            "lease_id": "l-1",
            "lease_epoch": 3,
            "workload_id": "w-1",
            "workload_class": "judge_critical",
            "device": "gpu",
            "memory_reserved_mib": None,
            "granted_ns": 8_000_000_000,
            "expires_ns": 9_000_000_000,
            "clock_domain": CLOCK,
            "revocable": True,
        },
        "admission_rejected": {
            "workload_id": "w-2",
            "code": "policy_priority",
            "retryable": True,
            "retry_after_ms": 250,
            "legacy_code": "policy_inferring",
            "detail": "the policy is inferring",
        },
        "pressure": {
            "observed_ns": 8_000_000_000,
            "clock_domain": CLOCK,
            "source": "fake",
            "free_mib": 9000,
            "policy_mib": None,
            "vllm_state": "asleep",
            "gate_open": True,
            "gate_code": None,
            "policy_server_seen": False,
            "age_ms": 120,
        },
        "policy_handle": {
            "handle_id": "h-1",
            "role": "forward",
            "checkpoint": {
                "config": "pi05_fr3_all_state",
                "dir_name": "pi05_fr3_all_step49999",
                "sha256": None,
            },
            "action_contract": "fr3-robotiq@1",
            "action_dims": 7,
            "action_horizon": 10,
            "endpoint": {"host": "127.0.0.1", "port": 8000},
            "policy_epoch": 2,
        },
        "chunk_request": {
            "request_id": "c-40",
            "episode_id": EPISODE,
            "handle_id": "h-1",
            "policy_epoch": 2,
            "obs_step": 40,
            "obs_ns": 8_000_000_000,
            "clock_domain": CLOCK,
            "action_start_index": 40,
            "committed_prefix_steps": 2,
            "previous_chunk_ref": "c-32",
            "prefix_conditioning": "soft_guidance",
            "deadline": deadline(),
        },
        "chunk_response": {
            "request_id": "c-40",
            "episode_id": EPISODE,
            "policy_epoch": 2,
            "action_contract": "fr3-robotiq@1",
            "actions": [[0.1, 0.0, -0.02, 0.0, 0.0, 0.01, 1.0] for _ in range(3)],
            "valid_from_action_index": 42,
            "prefix_conditioning_applied": "soft_guidance",
            "model_version": {
                "config": "pi05_fr3_all_state",
                "checkpoint_dir_name": "pi05_fr3_all_step49999",
            },
            "timing": {"queued_ms": 2, "infer_ms": 61, "total_ms": 70},
            "received_ns": 8_070_000_000,
            "clock_domain": CLOCK,
        },
        "quiesce_ack": {
            "handle_id": "h-1",
            "fenced_epoch": 2,
            "inflight_cancelled": 1,
            "ok": True,
        },
    }
    if kind == "unavailable":
        return unavailable(
            "runtime", **{"code": "epoch_fenced", "retryable": False, **over}
        )
    message = envelope("runtime", kind=kind, **copy.deepcopy(bodies[kind]))
    message.update(over)
    return message


RUNTIME_KINDS = (
    "workload_spec",
    "lease",
    "admission_rejected",
    "pressure",
    "policy_handle",
    "chunk_request",
    "chunk_response",
    "quiesce_ack",
    "unavailable",
)


def authority(kind="orchestrator", **over):
    value = {
        "principal_kind": kind,
        "principal_id": "orch-1",
        "session_id": "s-1",
        "process": {"pid": 4242, "start_ticks": 123456, "boot_id": "0f0e0d0c"},
        "command_id": None,
    }
    value.update(over)
    return value


def run_event(record="prepared", **over):
    base = envelope(
        "run_event",
        sequence_no=41,
        record=record,
        transaction_id=f"{RUN}:tx41",
        authority=authority(),
        control_epoch=12,
        mono_ns=9_000_000_000,
        clock_domain=CLOCK,
        prev_sha256=SHA,
    )
    if record == "run_header":
        base.update(
            sequence_no=0,
            transaction_id=None,
            prev_sha256=ZEROS,
            header={
                "plan_sha256": SHA,
                "contracts": [{"schema": "levi.aeri.run_event.v1", "minor": 0}],
                "levi_commit": "d68f656",
            },
        )
    elif record == "prepared":
        base.update(
            from_state="FORWARD_ACTIVE",
            to_state="FORWARD_STOPPING",
            reason="goal_verified",
            episode_id=EPISODE,
            episode_role="forward",
            action={
                "kind": "hold",
                "idempotency_key": SHA,
                "non_idempotent": False,
                "params_sha256": SHA,
            },
            evidence_ids=["j-8"],
        )
    elif record == "acknowledged":
        base.update(
            sequence_no=42,
            ack={"executed": "yes", "source": "robot_server", "detail_code": "ok"},
        )
    elif record == "committed":
        base.update(
            sequence_no=43,
            from_state="FORWARD_FINALIZE",
            to_state="ROBOT_HOME",
            reason="goal_verified",
            episode_id=EPISODE,
            episode_role="forward",
            episode_result={
                "task_outcome": "success",
                "stop_reason": "goal_verified",
                "goal_verification": "verified",
                "robot_home": "not_attempted",
                "scene_reset": "unknown",
                "label_kind": "autonomous_verdict",
                "judgement_ids": ["j-8"],
                "rollout": {
                    "task_folder": "stack_the_plates",
                    "demo": "demo_0003",
                    "sealed": "complete",
                },
            },
        )
    elif record == "note":
        base.update(
            sequence_no=44,
            transaction_id=None,
            note={"code": "judgement_dropped_stale", "detail": "j-7 after expiry"},
        )
    base.update(over)
    return base


# --- committed fixtures ---------------------------------------------------------------


def _text(message) -> bytes:
    return json.dumps(message, indent=1, ensure_ascii=False).encode("utf-8")


def _replace(message, old: bytes, new: bytes) -> bytes:
    raw = _text(message)
    assert old in raw, old
    return raw.replace(old, new, 1)


def _without(message, key):
    message = dict(message)
    message.pop(key)
    return _text(message)


def _common_invalid(name, valid, int_field, text_field, list_field, list_item):
    """Invalid variants every contract has: (file stem, expected code, bytes)."""
    big = dict(valid)
    big[text_field] = "x" * 2001
    long_list = dict(valid)
    long_list[list_field] = [list_item] * 65
    first_key = next(iter(valid))
    cases = [
        ("missing-required", "E_SCHEMA", _without(valid, int_field)),
        ("unknown-top-key", "E_UNKNOWN_FIELD", _text({**valid, "extra_field": 1})),
        (
            "duplicate-key",
            "E_DUPLICATE_KEY",
            _replace(
                valid,
                f'"{first_key}"'.encode(),
                f'"{first_key}": "x",\n "{first_key}"'.encode(),
            ),
        ),
        (
            "nan",
            "E_NONFINITE",
            _replace(
                valid,
                f'"{int_field}": '.encode(),
                f'"{int_field}": NaN, "z": '.encode(),
            ),
        ),
        (
            "infinity",
            "E_NONFINITE",
            _replace(
                valid,
                f'"{int_field}": '.encode(),
                f'"{int_field}": Infinity, "z": '.encode(),
            ),
        ),
        ("overflow-1e400", "E_NONFINITE", _text_number(valid, int_field, "1e400")),
        ("bool-for-int", "E_SCHEMA", _text({**valid, int_field: True})),
        ("string-for-int", "E_SCHEMA", _text({**valid, int_field: "1"})),
        (
            "wrong-schema",
            "E_SCHEMA",
            _text({**valid, "schema": f"levi.aeri.{name}.v2"}),
        ),
        ("minor-too-new", "E_SCHEMA_TOO_NEW", _text({**valid, "minor": 1})),
        ("string-too-long", "E_SCHEMA", _text(big)),
        ("list-too-long", "E_SCHEMA", _text(long_list)),
        ("not-json", "E_JSON", _text(valid)[:-3]),
    ]
    return cases


def _text_number(message, field, literal):
    raw = _text(message)
    value = json.dumps(message[field]).encode()
    old = f'"{field}": {value.decode()}'.encode()
    assert old in raw, old
    return raw.replace(old, f'"{field}": {literal}'.encode(), 1)


def fixture_cases() -> dict[str, dict[str, list]]:
    """``{contract: {"valid": [(stem, bytes)], "invalid": [(stem, code, bytes)]}}``."""
    evidence = {"kind": "frame", "ref": "side@1"}
    control = "E_CONTROL_FIELD"
    cases = {}

    valid = event()
    cases["event"] = {
        "valid": [("candidate", _text(valid)), ("retracted", _text(retraction()))],
        "invalid": _common_invalid(
            "event", valid, "seq", "actor_id", "evidence_refs", evidence
        )
        + [
            ("control-robot-stop", control, _text({**valid, "robot_stop": True})),
            (
                "control-fullwidth",
                control,
                _text({**valid, "ｒｏｂｏｔ＿ｓｔｏｐ": True}),
            ),
            (
                "control-nested",
                control,
                _text(
                    event(detector={"name": "d", "version": "1", "Execute_Reset": 1})
                ),
            ),
            (
                "unknown-nested-key",
                "E_UNKNOWN_FIELD",
                _text(event(detector={"name": "d", "version": "1", "extra": 1})),
            ),
            (
                "retracted-without-retracts",
                "E_INCONSISTENT",
                _text(event(status="retracted")),
            ),
            ("window-misses-step", "E_INCONSISTENT", _text(event(window_steps=[1, 2]))),
            (
                "episode-of-other-run",
                "E_INCONSISTENT",
                _text(event(episode_id="r-other.forward.0003")),
            ),
            ("bad-clock-domain", "E_SCHEMA", _text(event(clock_domain="monotonic"))),
            ("salience-over-one", "E_SCHEMA", _text(event(salience=1.5))),
            ("status-verified", "E_SCHEMA", _text(event(status="verified"))),
        ],
    }

    valid = judgement()
    cases["judgement"] = {
        "valid": [
            ("confirmed", _text(valid)),
            ("unknown", _text(unknown_judgement())),
            ("unavailable", _text(unavailable())),
        ],
        "invalid": _common_invalid(
            "judgement", valid, "observed_from_step", "spec_id", "event_ids", "ev-1"
        )
        + [
            (
                "control-in-predicate",
                control,
                _text(
                    judgement(
                        predicate_results=[
                            {
                                "name": "stable",
                                "value": True,
                                "required": True,
                                "Execute_Reset": True,
                            }
                        ]
                    )
                ),
            ),
            (
                "confirmed-with-false-predicate",
                "E_INCONSISTENT",
                _text(
                    judgement(
                        predicate_results=predicates(object_state=False, stable=True)
                    )
                ),
            ),
            (
                "confirmed-with-undecided-veto",
                "E_INCONSISTENT",
                _text(judgement(vetoes=[{"id": "human_hand", "state": "undecided"}])),
            ),
            (
                "rejected-without-reason",
                "E_INCONSISTENT",
                _text(judgement(decision="rejected")),
            ),
            (
                "unknown-without-reason",
                "E_INCONSISTENT",
                _text(judgement(decision="unknown")),
            ),
            (
                "valid-until-not-after-produced",
                "E_INCONSISTENT",
                _text(judgement(valid_until_ns=5_900_000_000)),
            ),
            (
                "calibrated-without-probability",
                "E_INCONSISTENT",
                _text(judgement(confidence_kind="calibrated")),
            ),
            ("vlm-without-model", "E_INCONSISTENT", _text(judgement(model_name=None))),
            (
                "unavailable-timeout-retryable",
                "E_INCONSISTENT",
                _text(unavailable(code="timeout", retryable=True)),
            ),
            ("unavailable-unknown-code", "E_SCHEMA", _text(unavailable(code="sleepy"))),
            ("kind-missing", "E_SCHEMA", _without(valid, "kind")),
        ],
    }

    valid = scene()
    cases["scene"] = {
        "valid": [
            ("ready", _text(valid)),
            ("reset-required", _text(reset_required())),
            ("unavailable", _text(unavailable("scene", code="busy"))),
        ],
        "invalid": _common_invalid(
            "scene", valid, "observed_ns", "contract_id", "evidence_refs", evidence
        )
        + [
            ("control-key", control, _text({**valid, "go_home": True})),
            ("control-dashed", control, _text({**valid, "Robot-Stop": True})),
            (
                "ready-but-failed",
                "E_INCONSISTENT",
                _text(
                    scene(
                        predicate_results=predicates(plates_separated=False),
                        failed_predicates=["plates_separated"],
                    )
                ),
            ),
            (
                "failed-list-mismatch",
                "E_INCONSISTENT",
                _text(reset_required(failed_predicates=["gripper_empty"])),
            ),
            (
                "reset-required-nothing-failed",
                "E_INCONSISTENT",
                _text(scene(decision="reset_required")),
            ),
            ("uppercase-decision", "E_SCHEMA", _text(scene(decision="READY"))),
            (
                "valid-until-before-produced",
                "E_INCONSISTENT",
                _text(scene(valid_until_ns=7_000_000_000)),
            ),
            (
                "recommended-action",
                "E_UNKNOWN_FIELD",
                _text({**valid, "recommended_action": "reset"}),
            ),
        ],
    }

    valid = runtime("chunk_response")
    cases["runtime"] = {
        "valid": [
            (kind.replace("_", "-"), _text(runtime(kind))) for kind in RUNTIME_KINDS
        ],
        "invalid": _common_invalid(
            "runtime", valid, "policy_epoch", "request_id", "actions", [0.0] * 7
        )
        + [
            ("control-cmd-prefix", control, _text({**valid, "cmd_move": [0.1]})),
            (
                "control-in-deadline",
                control,
                _text(runtime("chunk_request", deadline={**deadline(), "override": 1})),
            ),
            (
                "deadline-other-clock",
                "E_INCONSISTENT",
                _text(
                    runtime(
                        "chunk_request", deadline=deadline(clock_domain=OTHER_CLOCK)
                    )
                ),
            ),
            (
                "hard-prefix",
                "E_SCHEMA",
                _text(runtime("chunk_request", prefix_conditioning="hard")),
            ),
            (
                "ragged-actions",
                "E_INCONSISTENT",
                _text(runtime("chunk_response", actions=[[0.0] * 7, [0.0] * 6])),
            ),
            (
                "actions-nan",
                "E_NONFINITE",
                _replace(runtime("chunk_response"), b"[\n   0.1,", b"[\n   NaN,"),
            ),
            (
                "robot-port",
                "E_INCONSISTENT",
                _text(
                    runtime(
                        "policy_handle", endpoint={"host": "127.0.0.1", "port": 5000}
                    )
                ),
            ),
            (
                "remote-host",
                "E_SCHEMA",
                _text(
                    runtime(
                        "policy_handle", endpoint={"host": "10.0.0.2", "port": 8000}
                    )
                ),
            ),
            (
                "free-priority",
                "E_UNKNOWN_FIELD",
                _text({**runtime("workload_spec"), "priority": 0}),
            ),
            (
                "p0-class",
                "E_SCHEMA",
                _text(runtime("workload_spec", workload_class="safety")),
            ),
            (
                "lease-expired-at-grant",
                "E_INCONSISTENT",
                _text(runtime("lease", expires_ns=8_000_000_000)),
            ),
            (
                "unknown-kind",
                "E_SCHEMA",
                _text({**runtime("quiesce_ack"), "kind": "stop"}),
            ),
        ],
    }

    valid = run_event("prepared")
    cases["run_event"] = {
        "valid": [
            (record.replace("_", "-"), _text(run_event(record)))
            for record in (
                "run_header",
                "prepared",
                "acknowledged",
                "committed",
                "note",
            )
        ],
        "invalid": _common_invalid(
            "run_event", valid, "control_epoch", "transaction_id", "evidence_ids", "j-1"
        )
        + [
            # The run event is C's own record: not scanned for control keys,
            # but an undeclared key is still refused.
            ("robot-stop-key", "E_UNKNOWN_FIELD", _text({**valid, "robot_stop": True})),
            (
                "prepared-without-action",
                "E_INCONSISTENT",
                _text(run_event(action=None)),
            ),
            (
                "tx-named-after-other-line",
                "E_INCONSISTENT",
                _text(run_event(transaction_id=f"{RUN}:tx40")),
            ),
            (
                "header-not-line-0",
                "E_INCONSISTENT",
                _text(run_event("run_header", sequence_no=3)),
            ),
            ("new-state-name", "E_SCHEMA", _text(run_event(to_state="RESET_RUNNING"))),
            (
                "unknown-counted-as-success",
                "E_INCONSISTENT",
                _text(
                    run_event(
                        "committed",
                        episode_result={
                            **run_event("committed")["episode_result"],
                            "goal_verification": "unavailable",
                        },
                    )
                ),
            ),
            (
                "result-on-wrong-transition",
                "E_INCONSISTENT",
                _text(run_event("committed", from_state="FORWARD_ACTIVE")),
            ),
            (
                "operator-label-in-result",
                "E_UNKNOWN_FIELD",
                _text(
                    run_event(
                        "committed",
                        episode_result={
                            **run_event("committed")["episode_result"],
                            "operator_outcome": "success",
                        },
                    )
                ),
            ),
        ],
    }
    return cases


def write_fixtures(root: Path = FIXTURES) -> int:
    count = 0
    for contract, groups in fixture_cases().items():
        for group in ("valid", "invalid"):
            folder = root / contract / group
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob("*.json"):
                old.unlink()
            for case in groups[group]:
                stem, raw = case[0], case[-1]
                name = (
                    f"{stem}.{case[1]}.json" if group == "invalid" else f"{stem}.json"
                )
                (folder / name).write_bytes(raw)
                count += 1
    return count


if __name__ == "__main__":
    print(f"wrote {write_fixtures()} fixtures under {FIXTURES}")
