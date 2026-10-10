"""T-CL-06: run_event minor 1 (``RunHeader.reset_mode/scene_check``) and the
job file contract (``levi.aeri.job.v1``, ``JobSpec``): the reset policy's
keys are optional and ignored in the human-assisted ("policy evaluation
only") mode, out of the plan and its digest (design X2 §1.3, G1)."""

import dataclasses
import json
import os
from pathlib import Path

import aeri_factory as f
import pytest

from levi.automatic import cli
from levi.automatic.journal import Journal
from levi.automatic.termination import TerminationConfig
from levi.domain import aeri

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "run_event"
CONTRACT = """initial_state:
  id: stack-plates-initial
  version: "1"
  predicates:
    required: [object_at_source, gripper_open]
  observations:
    require_visible_evidence: true
    min_evidence_refs: 1
"""

HUMAN = """schema_version: levi.aeri.job.v1
experiment:
  name: r-ha
  episodes: 3
policies:
  forward:
    max_steps: 30
task:
  instruction: stack the plates
  initial_state_spec: initial-state.yaml
reset:
  strategy: human_assisted
recording:
  rollout_root: rollouts
  forward_folder: stack__r-ha
"""

RESET_KEYS = """policies:
  forward:
    max_steps: 30
  reset:
    max_steps: {steps}
task:
  instruction: stack the plates
  reset_instruction: "Reset: {text}"
  initial_state_spec: initial-state.yaml
reset:
  strategy: human_assisted
  enabled: {enabled}
  max_attempts: {attempts}
  on_unknown: {unknown}
recording:
  rollout_root: rollouts
  forward_folder: stack__r-ha
  reset_folder: {folder}
"""


def write(tmp_path, text, name="job.yaml"):
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    path = tmp_path / name
    path.write_text(text)
    return path


def with_reset_keys(**over):
    values = {
        "steps": 20,
        "text": "put back",
        "enabled": "true",
        "attempts": 2,
        "unknown": "reset",
        "folder": "reset_stack__r-ha",
    }
    values.update(over)
    head = HUMAN.split("policies:")[0]
    return head + RESET_KEYS.format(**values)


# --- run_event minor 1 -----------------------------------------------------------------


def test_the_run_event_is_minor_1_and_the_messages_stay_minor_0():
    assert aeri.MINORS == {
        "event": 0,
        "judgement": 0,
        "scene": 0,
        "runtime": 0,
        "run_event": 1,
    }
    assert aeri.MINOR == 0
    docs = aeri.schema_documents()
    assert docs["run_event"]["x-levi-minor"] == 1
    assert {docs[name]["x-levi-minor"] for name in ("event", "scene")} == {0}


def test_an_old_minor_0_header_is_still_read_and_the_modes_come_from_the_plan():
    line = aeri.parse(
        (FIXTURES / "valid" / "run-header.json").read_bytes(), "run_event"
    )
    assert line.minor == 0
    assert (line.header.reset_mode, line.header.scene_check) == (None, None)
    assert aeri.header_modes(line.header) == {"reset_mode": None, "scene_check": None}
    plan = {"run": {"reset_strategy": "human_assisted"}}
    assert aeri.header_modes(line.header, plan) == {
        "reset_mode": "human_assisted",
        "scene_check": "provider",
    }
    plan = {"reset_mode": "human_assisted", "scene_check": "operator_attested"}
    assert aeri.header_modes(line.header, plan)["scene_check"] == "operator_attested"


def test_a_minor_1_header_names_the_modes_and_minor_2_is_refused():
    line = aeri.parse(
        (FIXTURES / "valid" / "run-header-minor-1.json").read_bytes(), "run_event"
    )
    assert (line.minor, line.header.reset_mode, line.header.scene_check) == (
        1,
        "human_assisted",
        "operator_attested",
    )
    # The header wins over the plan.
    assert aeri.header_modes(line.header, {"reset_mode": "single_reset_policy"}) == {
        "reset_mode": "human_assisted",
        "scene_check": "operator_attested",
    }
    newer = {**f.run_header_minor_1(), "minor": 2}
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(newer, "run_event")
    assert caught.value.code == "E_SCHEMA_TOO_NEW"


@pytest.mark.parametrize(
    "header",
    [{"reset_mode": "single_policy"}, {"scene_check": "human"}],
)
def test_the_header_carries_code_names_only(header):
    with pytest.raises(aeri.AeriError) as caught:
        aeri.validate(f.run_header_minor_1(**header), "run_event")
    assert caught.value.code == "E_SCHEMA"


def test_a_journal_written_today_is_at_the_run_event_minor_and_reads_back(tmp_path):
    """The journal writer writes the run event's own minor (T-JNL-1; minor 0
    before it); a header without modes still takes them from the plan."""
    journal = Journal.create(
        tmp_path,
        run_id="r-old",
        plan_sha256="ab" * 32,
        authority={
            "principal_kind": "orchestrator",
            "principal_id": "aeri-orchestrator",
            "session_id": "s-aeri",
            "process": {"pid": os.getpid(), "start_ticks": 1, "boot_id": "b"},
            "command_id": None,
        },
        clock=lambda: 1,
        clock_domain="host-mono:test",
    )
    journal.close()
    scan = Journal.read(tmp_path)
    assert scan.corrupt is None and scan.events[0].minor == aeri.MINORS["run_event"]
    header = scan.events[0].header
    assert aeri.header_modes(header, {"reset_mode": "single_reset_policy"}) == {
        "reset_mode": "single_reset_policy",
        "scene_check": "provider",
    }


# --- levi.aeri.job.v1 ----------------------------------------------------------------------------


def test_the_job_termination_keys_are_the_termination_config_fields():
    assert set(aeri.JobTermination.model_fields) == {
        field.name for field in dataclasses.fields(TerminationConfig)
    }


def test_the_job_schema_snapshot_is_committed_and_names_the_aliases():
    root = Path(__file__).resolve().parents[2]
    document = json.loads((root / aeri.SNAPSHOT_DIR / "job.schema.json").read_text())
    assert document["$id"] == "levi.aeri.job.v1"
    assert document["x-levi-reset-strategy-aliases"] == aeri.RESET_STRATEGY_ALIASES
    assert document["x-levi-ignored-by-human-assisted"] == list(aeri.HUMAN_IGNORED_KEYS)


def test_a_human_assisted_job_needs_no_reset_keys(tmp_path):
    job = cli.load_job(write(tmp_path, HUMAN))
    assert job["config"].reset_strategy == "human_assisted"
    assert job["ignored"] == {}
    plan = job["plan"]
    assert plan["reset_mode"] == "human_assisted" and plan["scene_check"] == "provider"
    for name in cli.RESET_POLICY_FIELDS:
        assert name not in plan["run"]
    assert "reset_folder" not in plan["recording"] and "reset" not in plan["texts"]
    assert "human_scene_timeout_ns" not in plan["run"]


def test_the_reset_keys_of_a_human_assisted_job_are_ignored_and_out_of_the_digest(
    tmp_path,
):
    plain = cli.load_job(write(tmp_path, HUMAN))
    one = cli.load_job(write(tmp_path, with_reset_keys(), "one.yaml"))
    other = cli.load_job(
        write(
            tmp_path,
            with_reset_keys(
                steps=5,
                text="other",
                enabled="false",
                attempts=0,
                unknown="wait_human",
                folder="stack__r-ha",  # even the forward folder: never used
            ),
            "other.yaml",
        )
    )
    assert plain["plan_sha256"] == one["plan_sha256"] == other["plan_sha256"]
    assert plain["plan"] == one["plan"] == other["plan"]
    assert set(one["ignored"]) == set(aeri.HUMAN_IGNORED_KEYS)
    assert set(one["ignored"].values()) == {"strategy human_assisted"}


def test_validate_shows_the_ignored_keys_beside_the_plan(tmp_path, capsys):
    path = write(tmp_path, with_reset_keys())
    code = cli.main(["validate", "--config", str(path), "--dry-run", "--json"])
    found = json.loads(capsys.readouterr().out)
    assert code == 0 and found["ok"]
    assert found["plan"]["ignored"]["policies.reset"] == "strategy human_assisted"
    assert found["plan"]["plan_sha256"] == cli.load_job(path)["plan_sha256"]


@pytest.mark.parametrize(
    ("text", "words"),
    [
        # single_reset_policy needs its reset policy
        (HUMAN.replace("human_assisted", "single_reset_policy"), "policies.reset"),
        (HUMAN.replace("human_assisted", "single_policy"), "policies.reset"),
        # a person attests the scene they put back: human_assisted only
        (
            with_reset_keys().replace(
                "strategy: human_assisted", "strategy: single_policy"
            )
            + "",
            None,
        ),
    ],
)
def test_the_reset_policy_is_required_by_single_reset_policy(tmp_path, text, words):
    path = write(tmp_path, text)
    if words is None:
        job = cli.load_job(path)  # with policies.reset: valid
        assert job["config"].reset_strategy == "single_reset_policy"
        assert job["plan"]["reset_mode"] == "single_reset_policy"
        assert job["ignored"] == {}
        return
    with pytest.raises(cli.JobError) as caught:
        cli.load_job(path)
    assert words in str(caught.value)


def test_a_disabled_reset_policy_needs_no_policy_block(tmp_path):
    text = HUMAN.replace(
        "strategy: human_assisted", "strategy: single_reset_policy\n  enabled: false"
    )
    job = cli.load_job(write(tmp_path, text))
    assert job["config"].reset_enabled is False


def test_operator_attested_belongs_to_human_assisted(tmp_path):
    attested = HUMAN.replace(
        "strategy: human_assisted",
        "strategy: human_assisted\n  scene_check: operator_attested\n"
        "  human_scene_timeout_s: 120",
    )
    job = cli.load_job(write(tmp_path, attested))
    assert job["config"].scene_check == "operator_attested"
    assert job["config"].human_scene_timeout_ns == 120 * 10**9
    assert job["plan"]["run"]["human_scene_timeout_ns"] == 120 * 10**9
    single = with_reset_keys().replace(
        "strategy: human_assisted",
        "strategy: single_reset_policy\n  scene_check: operator_attested",
    )
    with pytest.raises(cli.JobError) as caught:
        cli.load_job(write(tmp_path, single, "single.yaml"))
    assert "operator_attested" in str(caught.value)


def test_a_human_timeout_without_a_person_checking_is_ignored(tmp_path):
    text = HUMAN.replace(
        "strategy: human_assisted",
        "strategy: human_assisted\n  human_scene_timeout_s: 30",
    )
    job = cli.load_job(write(tmp_path, text))
    assert job["ignored"] == {"reset.human_scene_timeout_s": "scene_check provider"}
    assert (
        job["plan_sha256"]
        == cli.load_job(write(tmp_path, HUMAN, "b.yaml"))["plan_sha256"]
    )


def test_aliases_stay_in_the_configuration(tmp_path):
    code = with_reset_keys().replace(
        "strategy: human_assisted", "strategy: single_reset_policy"
    )
    alias = code.replace("strategy: single_reset_policy", "strategy: single_policy")
    one = cli.load_job(write(tmp_path, code, "code.yaml"))
    two = cli.load_job(write(tmp_path, alias, "alias.yaml"))
    assert one["plan_sha256"] == two["plan_sha256"]
    assert two["plan"]["run"]["reset_strategy"] == "single_reset_policy"
    for later in ("scripted_safe", "atomic_skills"):
        text = code.replace("strategy: single_reset_policy", f"strategy: {later}")
        with pytest.raises(cli.JobError) as caught:
            cli.load_job(write(tmp_path, text, f"{later}.yaml"))
        assert "planned" in str(caught.value)


def test_the_plan_holds_the_resolved_rollout_root_and_the_contract_bytes(tmp_path):
    path = write(tmp_path, HUMAN)
    job = cli.load_job(path)
    assert job["plan"]["recording"]["rollout_root"] == os.path.realpath(
        tmp_path / "rollouts"
    )
    assert job["rollout_root"] == job["plan"]["recording"]["rollout_root"]
    first = job["plan"]["contract"]["sha256"]
    # A change to the contract file's bytes alone (a comment) changes the plan.
    (tmp_path / "initial-state.yaml").write_text(CONTRACT + "# reviewed\n")
    again = cli.load_job(path)
    assert again["plan"]["contract"]["sha256"] != first
    assert again["plan_sha256"] != job["plan_sha256"]
    # Another root, another plan.
    path.write_text(HUMAN.replace("rollout_root: rollouts", "rollout_root: elsewhere"))
    assert cli.load_job(path)["plan_sha256"] != again["plan_sha256"]


@pytest.mark.parametrize(
    "edit",
    [
        ("episodes: 3", "episodes: true"),  # no coercion
        ("max_steps: 30", "max_steps: 0"),
        ("strategy: human_assisted", "strategy: by_hand"),
        ("strategy: human_assisted", "strategy: human_assisted\n  scene_check: eyes"),
        ("  forward_folder: stack__r-ha", "  forward_folder: a/b"),
        ("instruction: stack the plates", "instruction: 42"),
    ],
)
def test_the_job_schema_is_strict(tmp_path, edit):
    with pytest.raises(cli.JobError):
        cli.load_job(write(tmp_path, HUMAN.replace(*edit)))


def test_parse_job_codes():
    with pytest.raises(aeri.AeriError) as caught:
        aeri.parse_job({"schema_version": "levi.aeri.job.v2"})
    assert caught.value.code == "E_SCHEMA"
    with pytest.raises(aeri.AeriError) as caught:
        aeri.parse_job(
            {
                "schema_version": "levi.aeri.job.v1",
                "experiment": {"name": "x"},
                "robot": {},
            }
        )
    assert caught.value.code == "E_UNKNOWN_FIELD"
