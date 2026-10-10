"""``levi.aeri.campaign_event.v1`` (levi/domain/aeri.py, T-CP-03): strict
parsing, cross-field rules, its snapshot, and the comparison with the base
branch; the five messages' registry is unchanged."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from campaign_guard import aeri_home_fixture, guard_fixture  # noqa: F401

from levi.domain import aeri

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = f"{aeri.SNAPSHOT_DIR}/campaign_event.schema.json"
AUTHORITY = {
    "principal_kind": "conductor",
    "principal_id": "conductor",
    "session_id": "s-1",
    "process": {"pid": 42, "start_ticks": 7, "boot_id": "boot-1"},
    "command_id": None,
}
HEADER = {
    "schema": "levi.aeri.campaign_event.v1",
    "minor": 0,
    "campaign_id": "c1",
    "emitted_wall_ns": 1,
    "sequence_no": 0,
    "record": "campaign_header",
    "authority": AUTHORITY,
    "control_epoch": 0,
    "mono_ns": 5,
    "clock_domain": "host-mono:boot-1",
    "prev_sha256": "0" * 64,
    "header": {
        "campaign_sha256": "ab" * 32,
        "settings_sha256": "cd" * 32,
        "robot": "fr3",
        "arms": ["A", "B"],
        "segments": 4,
        "schedule_kind": "counterbalanced_segments",
        "conclusion_level": "confirmatory_eligible",
        "levi_commit": None,
    },
}
LAUNCH = {
    **{k: v for k, v in HEADER.items() if k != "header"},
    "sequence_no": 9,
    "record": "prepared",
    "transaction_id": "c1:tx9",
    "segment": 1,
    "from_state": "ENV_CONFIRM",
    "to_state": "ARM_RUNNING",
    "reason": "run_launched",
    "run_id": "c1__A__s01",
    "authority": {**AUTHORITY, "principal_kind": "operator", "command_id": "cmd-1"},
    "prev_sha256": "ef" * 32,
    "action": {
        "kind": "launch_run",
        "idempotency_key": "c1:s01:ARM_RUNNING",
        "non_idempotent": True,
        "attempt": 1,
        "params_sha256": "12" * 32,
    },
}


def text(value) -> bytes:
    return json.dumps(value).encode()


def code_of(raw) -> str:
    with pytest.raises(aeri.AeriError) as caught:
        aeri.parse_campaign_event(raw)
    return caught.value.code


def changed(base, path, value):
    found = copy.deepcopy(base)
    part = found
    for key in path[:-1]:
        part = part[key]
    if value is KeyError:
        del part[path[-1]]
    else:
        part[path[-1]] = value
    return found


def test_valid_lines_parse_and_dump_canonically():
    for value in (HEADER, LAUNCH):
        event = aeri.parse_campaign_event(text(value))
        again = aeri.parse_campaign_event(aeri.dump(event))
        assert again == event


@pytest.mark.parametrize(
    "value, code",
    [
        (changed(LAUNCH, ["colour"], "red"), "E_UNKNOWN_FIELD"),
        (changed(LAUNCH, ["minor"], 1), "E_SCHEMA_TOO_NEW"),
        (changed(LAUNCH, ["schema"], "levi.aeri.campaign_event.v2"), "E_SCHEMA"),
        (changed(LAUNCH, ["segment"], True), "E_SCHEMA"),
        (changed(LAUNCH, ["segment"], "1"), "E_SCHEMA"),
        (changed(LAUNCH, ["to_state"], "RUNNING"), "E_SCHEMA"),
        (changed(LAUNCH, ["authority", "principal_kind"], "orchestrator"), "E_SCHEMA"),
        (
            changed(LAUNCH, ["action", "idempotency_key"], "c1:s02:ARM_RUNNING"),
            "E_INCONSISTENT",
        ),
        (changed(LAUNCH, ["action", "non_idempotent"], False), "E_INCONSISTENT"),
        (changed(LAUNCH, ["run_id"], None), "E_INCONSISTENT"),
        (changed(LAUNCH, ["run_id"], "other__A__s01"), "E_INCONSISTENT"),
        (changed(LAUNCH, ["transaction_id"], "c2:tx9"), "E_INCONSISTENT"),
        (changed(LAUNCH, ["transaction_id"], "c1:tx8"), "E_INCONSISTENT"),
        (changed(LAUNCH, ["segment"], KeyError), "E_INCONSISTENT"),
        (changed(LAUNCH, ["action"], KeyError), "E_INCONSISTENT"),
        (
            changed(
                LAUNCH,
                ["counts"],
                {"episodes_complete": 1, "faults": 0, "unplanned_interventions": 0},
            ),
            "E_INCONSISTENT",
        ),
        (changed(HEADER, ["sequence_no"], 3), "E_INCONSISTENT"),
        (changed(HEADER, ["prev_sha256"], "ab" * 32), "E_INCONSISTENT"),
        (changed(HEADER, ["header", "arms"], ["A"]), "E_SCHEMA"),
        (changed(HEADER, ["header", "arms"], ["A", "Z"]), "E_SCHEMA"),
    ],
)
def test_invalid_lines_are_refused_with_their_code(value, code):
    assert code_of(text(value)) == code


def test_duplicate_keys_infinities_and_size_are_refused():
    raw = text(LAUNCH)
    assert code_of(raw[:-1] + b', "segment": 1}') == "E_DUPLICATE_KEY"
    assert code_of(raw.replace(b'"mono_ns": 5', b'"mono_ns": NaN')) == "E_NONFINITE"
    assert code_of(b"x" * (aeri.MAX_BYTES + 1)) == "E_TOO_LARGE"
    assert code_of(b"[1]") == "E_SCHEMA"


def test_a_second_attempt_carries_its_number_in_the_key():
    second = changed(LAUNCH, ["action", "attempt"], 2)
    assert code_of(text(second)) == "E_INCONSISTENT"
    second["action"]["idempotency_key"] = "c1:s01:ARM_RUNNING:a2"
    assert aeri.parse_campaign_event(text(second)).action.attempt == 2
    assert aeri.campaign_key("c1", None, "PLANNED") == "c1:PLANNED"


def test_the_committed_snapshot_matches_and_the_message_registry_is_unchanged():
    assert aeri.check_snapshots(ROOT) == []
    assert (ROOT / SNAPSHOT).read_text() == aeri.snapshot_texts()[SNAPSHOT]
    # A run header lists the five messages only; the campaign line has its
    # own registry and minor.
    assert "campaign_event" not in aeri.SCHEMAS
    assert "campaign_event" not in aeri.MINORS
    assert aeri.CAMPAIGN_MINORS == {"campaign_event": 0}


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(root: Path, texts: dict) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for relative, content in texts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "base",
    )
    return root


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_campaign_schema_is_compared_with_the_base_branch(tmp_path, monkeypatch):
    texts = aeri.snapshot_texts()
    same = _repo(tmp_path / "same", texts)
    assert aeri.check_campaign_against_base(same, "main") == ([], [])
    empty = _repo(tmp_path / "empty", {"README": "x\n"})
    problems, notes = aeri.check_campaign_against_base(empty, "main")
    assert problems == [] and len(notes) == 1 and "new since" in notes[0]
    document = json.loads(texts[SNAPSHOT])
    document["$defs"]["CampaignAck"]["properties"]["executed"]["enum"].append("maybe")
    wider = _repo(tmp_path / "wider", {**texts, SNAPSHOT: aeri.render_schema(document)})
    monkeypatch.setattr(aeri, "RELEASED", True)
    problems, _ = aeri.check_campaign_against_base(wider, "main")
    assert any("maybe" in p for p in problems)
    monkeypatch.setattr(aeri, "RELEASED", False)
    problems, notes = aeri.check_campaign_against_base(wider, "main")
    assert problems == [] and any("maybe" in n for n in notes)
