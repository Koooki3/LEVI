"""AERI integration contracts v1 (levi/domain/aeri.py): strict parsing,
control keys, consistency rules, schema snapshots and compatibility."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

import aeri_factory as f
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from levi.domain import aeri

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixtures(group):
    return sorted(FIXTURES.glob(f"*/{group}/*.json"))


def _expect(path):
    return path.name.rsplit(".", 2)[1]


# --- pydantic behaviour the design relies on (X1 marked it unverified) ---------------


class _Lax(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int
    k: Literal["a", "b"]


class _Strict(_Lax):
    model_config = ConfigDict(strict=True)


def test_lax_pydantic_coerces_bool_and_text_to_int_strict_refuses():
    assert _Lax.model_validate({"n": True, "k": "a"}).n == 1
    assert _Lax.model_validate({"n": "1", "k": "a"}).n == 1
    for value in (True, "1", 1.0):
        with pytest.raises(ValidationError):
            _Strict.model_validate({"n": value, "k": "a"})


def test_model_validate_json_keeps_the_last_duplicate_key_silently():
    # The reason parse() decodes with its own duplicate check.
    assert _Strict.model_validate_json('{"n": 1, "n": 2, "k": "a"}').n == 2
    with pytest.raises(aeri.AeriError) as caught:
        aeri.parse('{"schema": "x", "schema": "y"}', "event")
    assert caught.value.code == "E_DUPLICATE_KEY"


def test_aeri_contracts_are_strict_and_closed():
    for model in (aeri.EventProposal, aeri.Judgement, aeri.RunEvent, aeri.Deadline):
        config = model.model_config
        assert config["strict"] and config["extra"] == "forbid"
        assert config["allow_inf_nan"] is False and config["frozen"]
    # The shared base contract is unchanged (strict only on AERI models).
    from levi.domain.contracts import Contract

    assert "strict" not in Contract.model_config


# --- fixtures ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", _fixtures("valid"), ids=lambda p: p.parent.parent.name + "/" + p.stem
)
def test_valid_fixture_parses_and_round_trips(path):
    contract = path.parent.parent.name
    message = aeri.parse(path.read_bytes(), contract)
    again = aeri.parse(aeri.dump(message), contract)
    assert again == message
    assert aeri.dump(again) == aeri.dump(message)


@pytest.mark.parametrize(
    "path", _fixtures("invalid"), ids=lambda p: p.parent.parent.name + "/" + p.stem
)
def test_invalid_fixture_is_refused_with_its_code(path):
    contract = path.parent.parent.name
    with pytest.raises(aeri.AeriError) as caught:
        aeri.parse(path.read_bytes(), contract)
    assert caught.value.code == _expect(path), str(caught.value)


def test_every_contract_has_enough_fixtures():
    for contract in aeri.SCHEMAS:
        valid = list((FIXTURES / contract / "valid").glob("*.json"))
        invalid = list((FIXTURES / contract / "invalid").glob("*.json"))
        assert len(valid) >= 2 and len(invalid) >= 12, contract
        codes = {_expect(p) for p in invalid}
        assert {
            "E_SCHEMA",
            "E_UNKNOWN_FIELD",
            "E_DUPLICATE_KEY",
            "E_NONFINITE",
        } <= codes
        if contract in aeri.CONTROL_SCANNED:
            assert "E_CONTROL_FIELD" in codes, contract


def test_committed_fixtures_match_the_factory(tmp_path):
    f.write_fixtures(tmp_path)
    made = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.json")}
    committed = {
        p.relative_to(FIXTURES): p.read_bytes() for p in FIXTURES.rglob("*.json")
    }
    assert made == committed


# --- parse ------------------------------------------------------------------------


def test_oversized_message_is_refused_before_decoding():
    raw = json.dumps({**f.event(), "padding": "y" * aeri.MAX_BYTES}).encode()
    with pytest.raises(aeri.AeriError) as caught:
        aeri.parse(raw, "event")
    assert caught.value.code == "E_TOO_LARGE"


def test_parse_accepts_the_schema_id_and_refuses_an_unknown_contract():
    raw = json.dumps(f.event())
    assert aeri.parse(raw, "levi.aeri.event.v1").event_id == "ev-17"
    with pytest.raises(ValueError):
        aeri.parse(raw, "levi.aeri.nothing.v1")
    # The caller says what it expects: an event is not a judgement.
    with pytest.raises(aeri.AeriError) as caught:
        aeri.parse(raw, "judgement")
    assert caught.value.code == "E_SCHEMA"


@pytest.mark.parametrize(
    "key",
    [
        "robot_stop",
        "ROBOT_STOP",
        "Robot-Stop",
        "robot stop",
        "robotStop",
        "ｒｏｂｏｔ＿ｓｔｏｐ",
        "Execute_Reset",
        "eStop",
        "e_stop",
        "go_home",
        "jointreset",
        "clearErr",
        "resume",
        "command",
        "cmd_pose",
        "force_open",
        "execute_anything",
    ],
)
def test_control_keys_are_refused_anywhere(key):
    assert aeri.is_control_key(key)
    nested = f.judgement(
        predicate_results=[
            {"name": "stable", "value": True, "required": True, key: True}
        ]
    )
    for message in ({**f.judgement(), key: True}, nested):
        with pytest.raises(aeri.AeriError) as caught:
            aeri.validate(message, "judgement")
        assert caught.value.code == "E_CONTROL_FIELD"


def test_no_declared_field_of_an_a_or_b_contract_looks_like_a_control_key():
    def names(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties":
                    yield from value
                yield from names(value)
        elif isinstance(node, list):
            for value in node:
                yield from names(value)

    documents = aeri.schema_documents()
    for contract in aeri.CONTROL_SCANNED:
        flagged = [n for n in names(documents[contract]) if aeri.is_control_key(n)]
        assert flagged == [], contract
    # The run event is C's own record: its ``robot_home`` result field would
    # trip the prefix rule, which is why it is not scanned.
    assert "robot_home" in set(names(documents["run_event"]))


def test_operator_keys_are_found_for_requests_to_a():
    request = {"episode": {"Operator_Outcome": "success"}, "images": [{"label": 1}]}
    assert aeri.operator_keys(request) == [
        "$.episode.Operator_Outcome",
        "$.images[0].label",
    ]
    assert aeri.operator_keys({"episode_id": "x", "images": []}) == []


def test_python_infinity_in_nested_actions_is_refused():
    message = f.runtime("chunk_response")
    message["actions"][1][2] = float("inf")
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(message, "runtime")
    assert caught.value.code == "E_NONFINITE"


def test_int_beyond_int64_is_refused():
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(f.event(observed_ns=2**63), "event")
    assert caught.value.code == "E_SCHEMA"


# --- consistency rules ----------------------------------------------------------------


def test_unknown_and_unavailable_are_distinct_and_never_success():
    unknown = aeri.validate(f.unknown_judgement(), "judgement")
    missing = aeri.validate(f.unavailable(), "judgement")
    assert isinstance(unknown, aeri.Judgement) and unknown.decision == "unknown"
    assert isinstance(missing, aeri.JudgementUnavailable)
    assert not hasattr(missing, "decision")


def test_confirmed_needs_a_required_predicate():
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(f.judgement(predicate_results=[]), "judgement")
    assert caught.value.code == "E_INCONSISTENT"
    optional = [{"name": "stable", "value": True, "required": False}]
    with pytest.raises(aeri.AeriError):
        aeri.validate(f.judgement(predicate_results=optional), "judgement")


def test_rejected_by_a_confirmed_veto_alone():
    message = f.judgement(
        decision="rejected",
        vetoes=[{"id": "human_hand", "state": "confirmed"}],
        legacy_c5=None,
    )
    assert aeri.validate(message, "judgement").decision == "rejected"


def test_unknown_stands_with_any_predicates():
    message = f.unknown_judgement(
        unknown_reason="conflicting_predicates",
        predicate_results=f.predicates(object_state=True, stable=False),
        legacy_c5=None,
    )
    assert aeri.validate(message, "judgement").decision == "unknown"


def test_calibrated_confidence_carries_probability_and_reference():
    message = f.judgement(
        confidence_kind="calibrated",
        calibrated_probability=0.9,
        calibration_ref="cal-1",
    )
    assert aeri.validate(message, "judgement").calibrated_probability == 0.9
    with pytest.raises(aeri.AeriError):
        aeri.validate(f.judgement(calibrated_probability=0.9), "judgement")


def test_admission_rejected_unavailable_retryable_only_with_retry_after():
    bad = f.unavailable(code="admission_rejected", retryable=True)
    with pytest.raises(aeri.AeriError):
        aeri.validate(bad, "judgement")
    good = f.unavailable(code="admission_rejected", retryable=True, retry_after_ms=50)
    assert aeri.validate(good, "judgement").retry_after_ms == 50
    with pytest.raises(aeri.AeriError):
        aeri.validate(f.unavailable(retryable=False, retry_after_ms=50), "judgement")


def test_runtime_unavailable_has_its_own_codes():
    assert aeri.validate(f.runtime("unavailable"), "runtime").code == "epoch_fenced"
    with pytest.raises(aeri.AeriError):
        aeri.validate(f.unavailable(code="epoch_fenced", retryable=False), "judgement")


def test_scene_unknown_allowed_even_when_predicates_pass():
    message = f.scene(decision="unknown", unknown_reason="stale_inputs")
    assert aeri.validate(message, "scene").decision == "unknown"


def test_workload_estimates_follow_their_source():
    with pytest.raises(aeri.AeriError):
        aeri.validate(f.runtime("workload_spec", memory_estimate_mib=100), "runtime")
    with pytest.raises(aeri.AeriError):
        aeri.validate(f.runtime("workload_spec", duration_source="unknown"), "runtime")


def test_workload_priority_follows_the_class_and_has_no_p0():
    assert aeri.WORKLOAD_PRIORITY["policy_realtime"] == 1
    assert min(aeri.WORKLOAD_PRIORITY.values()) == 1


def test_run_event_rules():
    for record in ("run_header", "prepared", "acknowledged", "committed", "note"):
        aeri.validate(f.run_event(record), "run_event")
    bad = [
        f.run_event("acknowledged", ack=None),
        f.run_event("note", note=None),
        f.run_event("committed", reason=None),
        f.run_event("prepared", episode_role=None),
        f.run_event("prepared", transaction_id="other-run:tx41"),
        f.run_event("run_header", prev_sha256=f.SHA),
        f.run_event("acknowledged", action=f.run_event()["action"]),
    ]
    for message in bad:
        with pytest.raises(aeri.AeriError) as caught:
            aeri.validate(message, "run_event")
        assert caught.value.code == "E_INCONSISTENT"
    reset_result = f.run_event(
        "committed",
        from_state="RESET_FINALIZE",
        to_state="VERIFY_INITIAL",
        reason="reset_verified",
        episode_id=f.RESET_EPISODE,
        episode_role="reset",
    )
    assert aeri.validate(reset_result, "run_event").episode_result is not None


def test_specs_check_predicate_names():
    message = aeri.validate(f.judgement(), "judgement", specs=aeri.BUILTIN_SPECS)
    assert message.spec_id == "generic-final"
    renamed = f.judgement(
        predicate_results=f.predicates(object_state=True, colour=True)
    )
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(renamed, "judgement", specs=aeri.BUILTIN_SPECS)
    assert caught.value.code == "E_SPEC_MISMATCH"
    # No initial-state contract is registered in v1.
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(f.scene(), "scene", specs=aeri.BUILTIN_SPECS)
    assert caught.value.code == "E_SPEC_MISMATCH"
    # Unavailable results carry no predicates and pass.
    aeri.validate(f.unavailable(), "judgement", specs=aeri.BUILTIN_SPECS)


def test_freshness_across_clock_domains_counts_as_expired():
    message = aeri.validate(f.judgement(), "judgement")
    now = message.produced_ns
    aeri.check_fresh(message, now_ns=now, local=f.CLOCK)
    with pytest.raises(aeri.AeriError) as caught:
        aeri.check_fresh(message, now_ns=now, local=f.OTHER_CLOCK)
    assert caught.value.code == "E_CLOCK_DOMAIN"
    with pytest.raises(ValueError):
        aeri.check_fresh(message, now_ns=now)  # both or neither
    assert aeri.host_clock_domain().startswith("host-mono:")


def test_episode_ids_parse_from_the_right():
    assert aeri.episode_parts("r.1.forward.0003") == ("r.1", "forward", 3)


# --- schema snapshots ---------------------------------------------------------------


def test_committed_schemas_match_the_models():
    assert aeri.check_snapshots(ROOT) == []


def test_schema_rendering_is_stable_across_processes():
    code = (
        "from levi.domain import aeri; import sys; "
        "sys.stdout.write(''.join(aeri.snapshot_texts().values()))"
    )
    outputs = set()
    for seed in ("0", "1", "12345"):
        path = os.pathsep.join(filter(None, [os.environ.get("PYTHONPATH"), str(ROOT)]))
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": path}
        outputs.add(
            subprocess.run(
                [sys.executable, "-c", code],
                env=env,
                capture_output=True,
                check=True,
                text=True,
            ).stdout
        )
    assert len(outputs) == 1


def _copy_snapshots(tmp_path):
    for relative, text in aeri.snapshot_texts().items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path / aeri.SNAPSHOT_DIR


def test_drift_and_breaking_changes_are_detected(tmp_path):
    folder = _copy_snapshots(tmp_path)
    path = folder / "judgement.schema.json"
    document = json.loads(path.read_text())
    judgement = document["$defs"]["Judgement"]
    judgement["properties"]["decision"]["enum"].append("probably")
    judgement["properties"]["old_field"] = {"type": "string"}
    judgement["required"].remove("spec_id")
    path.write_text(aeri.render_schema(document))
    problems = aeri.check_snapshots(tmp_path)
    assert problems[0] == f"{aeri.SNAPSHOT_DIR}/judgement.schema.json is out of date"
    text = "\n".join(problems)
    assert "Judgement.properties.decision.enum" in text
    assert "Judgement.properties.old_field: removed" in text
    assert "Judgement.required" in text
    # The models are the source: rewriting from them would be a breaking
    # change for this snapshot, so it is refused without the explicit flag.
    assert aeri.write_snapshots(tmp_path)
    assert path.read_text() == aeri.render_schema(document)
    assert aeri.write_snapshots(tmp_path, accept_breaking=True) == []
    assert aeri.check_snapshots(tmp_path) == []


def test_missing_and_stray_snapshots_are_reported(tmp_path):
    folder = _copy_snapshots(tmp_path)
    (folder / "scene.schema.json").unlink()
    (folder / "old.schema.json").write_text("{}")
    problems = aeri.check_snapshots(tmp_path)
    assert f"{aeri.SNAPSHOT_DIR}/scene.schema.json is missing" in problems
    assert (
        f"{aeri.SNAPSHOT_DIR}/old.schema.json is not a contract of this version"
        in problems
    )
    # A missing file is written; nothing breaking stops it.
    assert aeri.write_snapshots(tmp_path) == []
    assert (folder / "scene.schema.json").is_file()


def test_breaking_change_rules():
    base = {
        "type": "object",
        "properties": {"a": {"type": "string", "maxLength": 10}},
        "required": ["a"],
        "x-levi-control-keys": ["halt", "estop"],
    }

    def changed(**edit):
        new = json.loads(json.dumps(base))
        for key, value in edit.items():
            new[key] = value
        return aeri.breaking_changes(base, new)

    # Compatible: a new optional property, a looser bound, a longer
    # control-key list, a new description.
    assert (
        changed(properties={"a": base["properties"]["a"], "b": {"type": "integer"}})
        == []
    )
    assert changed(properties={"a": {"type": "string", "maxLength": 20}}) == []
    assert changed(**{"x-levi-control-keys": ["halt", "estop", "go_home"]}) == []
    assert changed(description="now documented") == []
    # Breaking: required grows, a bound tightens, a type changes, a control
    # key is dropped, a property disappears.
    assert changed(required=["a", "b"])
    assert changed(properties={"a": {"type": "string", "maxLength": 5}})
    assert changed(properties={"a": {"type": "integer", "maxLength": 10}})
    assert changed(**{"x-levi-control-keys": ["halt"]})
    assert changed(properties={})


def test_check_contracts_command_passes_and_leaves_the_old_snapshot(capsys):
    from levi.domain.schema_catalog import main

    before = (ROOT / "docs/architecture/contracts.json").read_text()
    assert main([]) == 0
    assert "matches" in capsys.readouterr().out
    assert (ROOT / "docs/architecture/contracts.json").read_text() == before


# --- review fixes (each test failed before its fix) -------------------------------------


def _result(**over):
    base = f.run_event("committed")["episode_result"]
    return f.run_event("committed", episode_result={**base, **over})


@pytest.mark.parametrize(
    ("outcome", "verification", "ok"),
    [
        ("success", "verified", True),
        ("failure", "contradicted", True),
        ("unknown", "undecided", True),
        ("unknown", "unavailable", True),
        ("failure", "undecided", False),
        ("failure", "unavailable", False),
        ("unknown", "contradicted", False),
        ("unknown", "verified", False),
        ("success", "undecided", False),
    ],
)
def test_episode_result_never_counts_not_known_as_failure(outcome, verification, ok):
    message = _result(task_outcome=outcome, goal_verification=verification)
    if ok:
        aeri.validate(message, "run_event")
    else:
        with pytest.raises(aeri.AeriError) as caught:
            aeri.validate(message, "run_event")
        assert caught.value.code == "E_INCONSISTENT"


def _c5(reading, outcome, undecided):
    return {"reading": reading, "outcome": outcome, "undecided": undecided}


@pytest.mark.parametrize(
    ("legacy", "fields", "ok"),
    [
        (_c5("supported", "success", False), {}, True),
        (
            _c5("contradicted", "failure", False),
            {
                "decision": "rejected",
                "predicate_results": f.predicates(object_state=False, stable=True),
            },
            True,
        ),
        (
            _c5("unknown", "failure", True),
            {"decision": "unknown", "unknown_reason": "model_undecided"},
            True,
        ),
        # Contradictions between the online judgement's values and the decision.
        (_c5("unknown", "failure", True), {}, False),
        (
            _c5("unknown", "failure", True),
            {"decision": "unknown", "unknown_reason": "occluded"},
            False,
        ),
        (_c5("contradicted", "failure", False), {}, False),
        (
            _c5("supported", "success", False),
            {"decision": "unknown", "unknown_reason": "model_undecided"},
            False,
        ),
        (_c5("supported", "success", True), {}, False),
        (_c5("supported", "success", False), {"provider": "fake"}, False),
    ],
)
def test_legacy_c5_values_must_agree_with_the_decision(legacy, fields, ok):
    message = f.judgement(legacy_c5=legacy, **fields)
    if ok:
        aeri.validate(message, "judgement")
    else:
        with pytest.raises(aeri.AeriError) as caught:
            aeri.validate(message, "judgement")
        assert caught.value.code == "E_INCONSISTENT"


def test_validity_and_lease_spans_are_bounded():
    produced = f.judgement()["produced_ns"]
    limit = aeri.MAX_RESULT_VALIDITY_MS * 1_000_000
    aeri.validate(f.judgement(valid_until_ns=produced + limit), "judgement")
    for message, contract in (
        (f.judgement(valid_until_ns=produced + limit + 1), "judgement"),
        (f.judgement(valid_until_ns=aeri.INT64_MAX), "judgement"),
        (f.scene(valid_until_ns=aeri.INT64_MAX), "scene"),
        (f.runtime("lease", expires_ns=aeri.INT64_MAX), "runtime"),
        (
            f.runtime(
                "lease",
                expires_ns=8_000_000_000 + aeri.MAX_LEASE_MS * 1_000_000 + 1,
            ),
            "runtime",
        ),
    ):
        with pytest.raises(aeri.AeriError) as caught:
            aeri.validate(message, contract)
        assert caught.value.code == "E_INCONSISTENT"
    documents = aeri.schema_documents()
    assert documents["judgement"]["x-levi-max-result-validity-ms"] == 30_000
    assert documents["runtime"]["x-levi-max-lease-ms"] == aeri.MAX_LEASE_MS


def test_check_fresh_refuses_results_from_the_future():
    message = aeri.validate(f.judgement(), "judgement")
    now = message.produced_ns
    aeri.check_fresh(message, now_ns=now, local=f.CLOCK)
    aeri.check_fresh(message, now_ns=now - aeri.FUTURE_TOLERANCE_NS, local=f.CLOCK)
    with pytest.raises(aeri.AeriError) as caught:
        aeri.check_fresh(
            message, now_ns=now - aeri.FUTURE_TOLERANCE_NS - 1, local=f.CLOCK
        )
    assert caught.value.code == "E_FUTURE"
    with pytest.raises(aeri.AeriError) as caught:
        aeri.check_fresh(message, now_ns=message.valid_until_ns, local=f.CLOCK)
    assert caught.value.code == "E_EXPIRED"
    lease = aeri.validate(f.runtime("lease"), "runtime")
    aeri.check_fresh(lease, now_ns=lease.granted_ns, local=f.CLOCK)
    with pytest.raises(aeri.AeriError):
        aeri.check_fresh(lease, now_ns=lease.expires_ns, local=f.CLOCK)


def test_check_fresh_reads_this_hosts_clock_by_default():
    import time as _time

    now = _time.monotonic_ns()
    message = aeri.validate(
        f.judgement(
            clock_domain=aeri.host_clock_domain(),
            observed_through_ns=now - 1000,
            produced_ns=now,
            valid_until_ns=now + 10_000_000_000,
        ),
        "judgement",
    )
    aeri.check_fresh(message)
    # Another domain (here the fixtures') counts as expired.
    with pytest.raises(aeri.AeriError) as caught:
        aeri.check_fresh(aeri.validate(f.judgement(), "judgement"))
    assert caught.value.code == "E_CLOCK_DOMAIN"


def test_chunk_deadline_is_judged_by_the_receivers_clock():
    response = aeri.validate(f.runtime("chunk_response"), "runtime")
    request = aeri.validate(f.runtime("chunk_request"), "runtime")
    due = request.deadline.due_ns
    # received_ns says it arrived in time; C's own clock says it did not.
    assert response.received_ns <= due
    with pytest.raises(aeri.AeriError) as caught:
        aeri.check_deadline(request.deadline, now_ns=due + 1, local=f.CLOCK)
    assert caught.value.code == "E_EXPIRED"
    aeri.check_deadline(request.deadline, now_ns=due, local=f.CLOCK)
    schema = aeri.schema_documents()["runtime"]["$defs"]["ChunkResponse"]
    assert "receiving side" in schema["properties"]["received_ns"]["description"]


@pytest.mark.parametrize("port", [8000, 5000, 5001, 5100, 7470])
def test_robot_and_policy_server_ports_are_refused(port):
    message = f.runtime("policy_handle", endpoint={"host": "127.0.0.1", "port": port})
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(message, "runtime")
    assert caught.value.code == "E_INCONSISTENT"


@pytest.mark.parametrize(
    "key",
    [
        "robot.stop",
        "robot/stop",
        "robot:stop",
        "robotMove",
        "executeAnything",
        "cmdHome",
        "forceStop",
        "robot\u200bstop",
        "r̵obot_stop",
        "ROBOT­STOP",
    ],
)
def test_control_key_spellings_report_the_control_code(key):
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate({**f.event(), key: True}, "event")
    assert caught.value.code == "E_CONTROL_FIELD", key


def test_no_contract_has_a_free_form_object():
    def walk(node, path="$"):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                assert node.get("additionalProperties") is False, path
            assert "patternProperties" not in node, path
            for key, value in node.items():
                yield from walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from walk(value, f"{path}[{index}]")
        yield path

    for document in aeri.schema_documents().values():
        list(walk(document))


# --- snapshots against the base branch ------------------------------------------------


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path, texts):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for relative, text in texts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-q",
        "-m",
        "base",
    )
    _git(root, "checkout", "-q", "-b", "feature")
    return root


def _shrunk_judgement():
    """The judgement snapshot as if the base had one more decision value:
    the current models then drop it (a breaking change against the base)."""
    texts = aeri.snapshot_texts()
    relative = f"{aeri.SNAPSHOT_DIR}/judgement.schema.json"
    document = json.loads(texts[relative])
    document["$defs"]["Judgement"]["properties"]["decision"]["enum"].append("probably")
    return {**texts, relative: aeri.render_schema(document)}


def test_breaking_change_against_the_base_branch_is_reported(tmp_path, monkeypatch):
    root = _repo(tmp_path, _shrunk_judgement())
    # The feature branch commits snapshots that match its models (as
    # --write --accept-breaking would): the same-tree check passes...
    for relative, text in aeri.snapshot_texts().items():
        (root / relative).write_text(text)
    assert aeri.check_snapshots(root) == []
    # ...the comparison with the base does not, once v1 is released.
    monkeypatch.setattr(aeri, "RELEASED", True)
    problems, notes = aeri.check_against_base(root, "main")
    assert any("judgement.schema.json" in p and "probably" in p for p in problems)
    monkeypatch.setattr(aeri, "RELEASED", False)
    problems, notes = aeri.check_against_base(root, "main")
    assert problems == [] and any("probably" in n for n in notes)


def test_compatible_or_new_snapshots_pass_against_the_base(tmp_path, monkeypatch):
    monkeypatch.setattr(aeri, "RELEASED", True)
    root = _repo(tmp_path, aeri.snapshot_texts())
    assert aeri.check_against_base(root, "main") == ([], [])
    empty = _repo(tmp_path / "x", {"README": "no contracts yet\n"})
    problems, notes = aeri.check_against_base(empty, "main")
    assert problems == [] and all("new" in n for n in notes)
    # Pinned: the five contracts and the job file, one note each.
    expected = {"event", "judgement", "scene", "runtime", "run_event", "job"}
    assert set(aeri.schema_documents()) == expected
    named = {
        n.split(":")[0].rsplit("/", 1)[-1].removesuffix(".schema.json") for n in notes
    }
    assert named == expected and len(notes) == 6


def test_an_unreadable_base_fails_instead_of_passing(tmp_path):
    root = _repo(tmp_path, aeri.snapshot_texts())
    problems, _ = aeri.check_against_base(root, "no-such-branch")
    assert problems and "no-such-branch" in problems[0]
    plain = tmp_path / "plain"
    plain.mkdir()
    problems, _ = aeri.check_against_base(plain, "main")
    assert problems


def test_accept_breaking_needs_write():
    from levi.domain.schema_catalog import main

    with pytest.raises(SystemExit) as caught:
        main(["--accept-breaking"])
    assert caught.value.code == 2


# --- second review (each test failed before its fix) -----------------------------------


def test_an_undecided_online_success_maps_to_unknown():
    # anchored.undecided() turns a success with an undecided veto (or a
    # disputed exemption, or a missing input) into undecided: the online
    # judgement can say outcome=success, undecided=true (X1 §2.2: unknown).
    message = f.judgement(
        decision="unknown",
        unknown_reason="model_undecided",
        legacy_c5=_c5("supported", "success", True),
    )
    assert aeri.validate(message, "judgement").decision == "unknown"


def _prepared(**action):
    base = f.run_event()
    merged = {**base["action"], **action}
    merged["idempotency_key"] = aeri.action_key(
        f.RUN, base["episode_id"], merged["kind"], merged.get("step")
    )
    return {**base, "action": merged}


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "home", "non_idempotent": True},
        {"kind": "policy_steps", "non_idempotent": True, "step": None},
        {"kind": "home", "non_idempotent": False, "step": 3},
        {"kind": "hold", "non_idempotent": True},
    ],
)
def test_physical_actions_need_a_step_and_are_never_idempotent(action):
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(_prepared(**action), "run_event")
    assert caught.value.code == "E_INCONSISTENT"


def test_the_idempotency_key_is_derived_from_run_episode_action_and_step():
    good = _prepared(kind="home", non_idempotent=True, step=3)
    assert aeri.validate(good, "run_event").action.step == 3
    forged = {**good, "action": {**good["action"], "idempotency_key": f.SHA}}
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(forged, "run_event")
    assert caught.value.code == "E_INCONSISTENT"
    retry = _prepared(kind="home", non_idempotent=True, step=4, retry_of=f"{f.RUN}:tx2")
    assert aeri.validate(retry, "run_event").action.retry_of == f"{f.RUN}:tx2"
    # Only a physical action is retried.
    with pytest.raises(aeri.AeriError):
        aeri.validate(_prepared(kind="none", retry_of=f"{f.RUN}:tx2"), "run_event")


def _clone_detached(tmp_path, source):
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(source), str(clone)], check=True, capture_output=True
    )
    _git(clone, "checkout", "-q", "--detach")
    for branch in _git(
        clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    ).split():
        _git(clone, "branch", "-q", "-D", branch)
    return clone


def test_base_is_local_main_when_there_is_one(tmp_path, monkeypatch):
    monkeypatch.delenv("LEVI_CONTRACT_BASE", raising=False)
    root = _repo(tmp_path, aeri.snapshot_texts())
    assert aeri.resolve_base(root) == "main"
    assert aeri.check_against_base(root) == ([], [])


def test_base_falls_back_to_origin_main_in_a_detached_checkout(tmp_path, monkeypatch):
    # GitHub Actions: pull request, tag or branch push -- no local main.
    monkeypatch.delenv("LEVI_CONTRACT_BASE", raising=False)
    clone = _clone_detached(tmp_path, _repo(tmp_path / "src", aeri.snapshot_texts()))
    assert _git(clone, "for-each-ref", "refs/heads/") == ""
    assert aeri.resolve_base(clone) == "origin/main"
    assert aeri.check_against_base(clone) == ([], [])


def test_base_in_a_source_tree_without_git_fails_with_a_fix(tmp_path, monkeypatch):
    monkeypatch.delenv("LEVI_CONTRACT_BASE", raising=False)
    tree = tmp_path / "tree"
    tree.mkdir()
    problems, _ = aeri.check_against_base(tree)
    assert problems and "--base" in problems[0] and "LEVI_CONTRACT_BASE" in problems[0]


def test_base_order_is_flag_then_environment_then_main_then_origin(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path, aeri.snapshot_texts())
    _git(root, "branch", "-q", "trunk", "main")
    monkeypatch.setenv("LEVI_CONTRACT_BASE", "trunk")
    assert aeri.resolve_base(root) == "trunk"
    assert aeri.resolve_base(root, "main") == "main"
    # A named base that does not resolve fails; it never falls back.
    monkeypatch.setenv("LEVI_CONTRACT_BASE", "nope")
    problems, _ = aeri.check_against_base(root)
    assert problems and "nope" in problems[0]
    problems, _ = aeri.check_against_base(root, "also-nope")
    assert problems and "also-nope" in problems[0]
    monkeypatch.delenv("LEVI_CONTRACT_BASE")
    _git(root, "branch", "-q", "-m", "main", "old-main")
    problems, _ = aeri.check_against_base(root)
    assert problems and "main" in problems[0] and "origin/main" in problems[0]


def test_unreadable_base_is_not_reported_as_a_contract_difference(capsys, monkeypatch):
    from levi.domain.schema_catalog import main

    assert main(["--base", "no-such-branch-anywhere"]) == 1
    out = capsys.readouterr().out
    assert "cannot read the base" in out
    assert "in a way v1 does not allow" not in out


def test_ci_compares_with_origin_main():
    workflow = (ROOT / ".github/workflows/test.yml").read_text()
    assert "levi dev check-contracts --base origin/main" in workflow
