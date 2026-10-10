"""The campaign block, layout cards, expansion into child job files,
``settings_sha256`` and ``campaign_sha256`` (spec.py, T-CP-02)."""

import copy
import json
import threading

import pytest
from campaign_fixtures import ARM_A, ARM_B, ARM_C, ARM_D, FakePlanner, write_job

from levi.automatic import scene_assessment as sa
from levi.automatic.campaign import spec


def plan(tmp_path, folder="job", planner=None, **over):
    path = write_job(tmp_path / folder, **over)
    return spec.plan_campaign(
        path, job_root=tmp_path / "jobs", planner=planner or FakePlanner()
    )


def refused(tmp_path, code, **over):
    path = write_job(tmp_path / "job", **over)
    with pytest.raises(spec.CampaignError) as caught:
        spec.plan_campaign(path, job_root=tmp_path / "jobs", planner=FakePlanner())
    assert caught.value.code == code, str(caught.value)
    return caught.value


# --- expansion ----------------------------------------------------------------------------


def test_a_campaign_expands_into_one_valid_job_file_per_segment(tmp_path):
    found = plan(tmp_path, planner=spec.LoadJobPlanner())
    folder = tmp_path / "jobs" / "campaigns" / "c1"
    names = sorted(p.name for p in folder.glob("*.yaml"))
    assert names == sorted(
        f"c1__{c.arm}__s{c.segment:02d}.yaml" for c in found.children
    )
    assert len(found.children) == 4  # 2 arms x 4 trials / 2 per segment
    from levi.automatic.cli import load_job

    groups = set()
    for child in found.children:
        job = load_job(folder / child.file)
        assert job["plan_sha256"] == child.plan_sha256
        assert job["config"].run_id == child.run_id
        assert job["config"].episodes == child.trials == 2
        assert job["plan"]["recording"]["forward_folder"] == "forward__c1"
        assert job["rollout_root"] == str((tmp_path / "job" / "rollouts").resolve())
        groups.add((child.arm, job["group"]))
    assert groups == {
        ("A", "pi05_fr3_all_step49999"),
        ("B", "recap_cfg_r2_best_step14300_jax"),
    }
    saved = spec.read_plan(folder / spec.PLAN_FILE)
    assert saved["campaign_sha256"] == found.campaign_sha256
    assert saved["settings_sha256"] == found.settings_sha256
    assert saved["schedule"]["conclusion_level"] == "confirmatory_eligible"


def test_planning_twice_is_a_no_op_and_a_change_under_the_same_id_is_refused(tmp_path):
    first = plan(tmp_path)
    again = plan(tmp_path)
    assert again.campaign_sha256 == first.campaign_sha256
    path = write_job(tmp_path / "job", seed=8)
    with pytest.raises(spec.CampaignError) as caught:
        spec.plan_campaign(path, job_root=tmp_path / "jobs", planner=FakePlanner())
    assert caught.value.code == "E_CAMPAIGN_EXISTS"


def test_the_campaign_sha256_is_deterministic_and_covers_the_schedule(tmp_path):
    one = plan(tmp_path, folder="job")
    two = spec.plan_campaign(
        tmp_path / "job" / "campaign.yaml",
        job_root=tmp_path / "other-root",
        planner=FakePlanner(),
    )
    assert one.campaign_sha256 == two.campaign_sha256
    seeded = plan(tmp_path, folder="job8", seed=8, campaign_id="c8")
    renamed = plan(tmp_path, folder="job9", seed=7, campaign_id="c8b")
    assert (
        len({one.campaign_sha256, seeded.campaign_sha256, renamed.campaign_sha256}) == 3
    )


def test_a_tampered_plan_or_child_is_detected(tmp_path):
    found = plan(tmp_path)
    folder = found.directory
    path = folder / spec.PLAN_FILE
    value = json.loads(path.read_text())
    value["schedule"]["seed"] = 99
    path.chmod(0o644)
    path.write_text(json.dumps(value))
    with pytest.raises(spec.CampaignError) as caught:
        spec.read_plan(path)
    assert caught.value.code == "E_CAMPAIGN_PLAN"
    child = folder / found.children[0].file
    child.write_text(child.read_text().replace("min_steps: 5", "min_steps: 6"))
    with pytest.raises(spec.CampaignError):
        spec.verify_children(found.to_json(), folder)


def test_concurrent_planners_of_one_campaign_agree(tmp_path):
    path = write_job(tmp_path / "job")
    results, errors = [], []

    def run():
        try:
            results.append(
                spec.plan_campaign(
                    path, job_root=tmp_path / "jobs", planner=FakePlanner()
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors and len({r.campaign_sha256 for r in results}) == 1


# --- settings_sha256 ----------------------------------------------------------------------


def documents(tmp_path):
    loaded = spec.load(write_job(tmp_path / "job"))
    _, children, settings = spec.draft(loaded)
    return {name: doc for name, (_, doc) in children.items()}, settings


@pytest.mark.parametrize(
    "path",
    [
        ("termination", "min_steps"),
        ("task", "instruction"),
        ("reset", "strategy"),
        ("reset", "scene_check"),
        ("recording", "forward_folder"),
        ("recording", "rollout_root"),
        ("experiment", "random_seed"),
        ("experiment", "execution_mode"),
        ("schema_version",),
    ],
)
def test_changing_any_shared_setting_of_one_child_is_refused(tmp_path, path):
    docs, _ = documents(tmp_path)
    name = sorted(docs)[1]
    changed = copy.deepcopy(docs)
    part = changed[name]
    for key in path[:-1]:
        part = part.setdefault(key, {})
    part[path[-1]] = "something-else-7"
    with pytest.raises(spec.CampaignError) as caught:
        spec.settings_sha256(changed)
    assert caught.value.code == "E_CAMPAIGN_SETTINGS_DIFFER"
    assert ".".join(path) in caught.value.detail


@pytest.mark.parametrize(
    "path, value",
    [
        (("policies", "forward", "max_steps"), 999),
        (("recording", "group"), "another-group"),
        (("experiment", "name"), "renamed"),
        (("experiment", "episodes"), 17),
    ],
)
def test_the_masked_keys_may_differ(tmp_path, path, value):
    docs, settings = documents(tmp_path)
    name = min(docs)
    part = docs[name]
    for key in path[:-1]:
        part = part.setdefault(key, {})
    part[path[-1]] = value
    assert spec.settings_sha256(docs) == settings


def test_a_contract_file_that_differs_between_children_is_refused(tmp_path):
    docs, _ = documents(tmp_path)
    other = tmp_path / "other-contract.yaml"
    other.write_text((tmp_path / "job" / "initial-state.yaml").read_text() + "# v2\n")
    name = sorted(docs)[1]
    docs[name]["task"]["initial_state_spec"] = str(other)
    with pytest.raises(spec.CampaignError) as caught:
        spec.settings_sha256(docs)
    assert caught.value.code == "E_CAMPAIGN_SETTINGS_DIFFER"


def test_a_contract_edited_after_planning_changes_the_child_plan(tmp_path):
    found = plan(tmp_path, planner=spec.LoadJobPlanner())
    (tmp_path / "job" / "initial-state.yaml").write_text(
        (tmp_path / "job" / "initial-state.yaml").read_text() + "# edited\n"
    )
    with pytest.raises(spec.CampaignError) as caught:
        spec.verify_children(found.to_json(), found.directory, spec.LoadJobPlanner())
    assert "changed" in caught.value.detail


# --- refusals -----------------------------------------------------------------------------


def test_two_reference_arms_are_refused(tmp_path):
    second = ARM_B.replace("    B:\n", "    B:\n      role: reference\n")
    refused(tmp_path, "E_CAMPAIGN_ARMS", arms=(ARM_A, second))


def test_one_arm_or_an_arm_id_outside_a_to_h_is_refused(tmp_path):
    refused(tmp_path, "E_CAMPAIGN_SCHEMA", arms=(ARM_A,))
    refused(tmp_path, "E_CAMPAIGN_SCHEMA", arms=(ARM_A, ARM_B.replace("B:", "I:")))


def test_up_to_eight_arms(tmp_path):
    arms = [ARM_A, ARM_B, ARM_C, ARM_D]
    for letter in "EFGH":
        arms.append(
            ARM_D.replace("D:", f"{letter}:").replace(
                "recap_cfg_r2_step15000_jax", f"recap_cfg_x{letter}"
            )
        )
    found = plan(tmp_path, arms=tuple(arms), trials=2, segment_trials=1)
    assert len({c.arm for c in found.children}) == 8


@pytest.mark.parametrize(
    "checkpoint, code",
    [
        ("        sha256_status: hashed\n", "E_CAMPAIGN_SCHEMA"),
        ("        sha256_status: recorded\n", "E_CAMPAIGN_CHECKPOINT"),
        (
            "        sha256_status: none\n        manifest_sha256: " + "cd" * 32 + "\n",
            "E_CAMPAIGN_CHECKPOINT",
        ),
    ],
)
def test_checkpoint_hash_status_rules(tmp_path, checkpoint, code):
    arm = ARM_B + "      checkpoint:\n" + checkpoint.replace("        ", "        ")
    refused(tmp_path, code, arms=(ARM_A, arm))


def test_verified_and_recorded_statuses_carry_a_manifest(tmp_path):
    arm = (
        ARM_B
        + "      checkpoint:\n        sha256_status: verified\n"
        + ("        manifest_sha256: " + "ef" * 32 + "\n")
    )
    assert plan(tmp_path, arms=(ARM_A, arm)).children


def test_a_cfg_checkpoint_must_be_served_with_the_cfg_config(tmp_path):
    wrong = ARM_B.replace(
        "config: pi05_fr3_all_state_cfg", "config: pi05_fr3_all_state"
    )
    error = refused(tmp_path, "E_CAMPAIGN_PAIRING", arms=(ARM_A, wrong))
    assert "pi05_fr3_all_state_cfg" in error.detail
    plain_with_cfg = ARM_A.replace(
        "config: pi05_fr3_all_state\n", "config: pi05_fr3_all_state_cfg\n"
    )
    refused(tmp_path, "E_CAMPAIGN_PAIRING", arms=(plain_with_cfg, ARM_B))


def test_the_pairing_table_is_required_and_must_cover_every_checkpoint(tmp_path):
    refused(tmp_path, "E_CAMPAIGN_PAIRING", pairing="")
    unknown = ARM_B.replace("recap_cfg_r2_best_step14300_jax", "mystery_ckpt")
    refused(tmp_path, "E_CAMPAIGN_PAIRING", arms=(ARM_A, unknown))
    ambiguous = (
        "  pairing:\n    recap_cfg_*: pi05_fr3_all_state_cfg\n"
        "    recap_*: pi05_fr3_all_state\n    pi05_fr3_all_step*: pi05_fr3_all_state\n"
    )
    refused(tmp_path, "E_CAMPAIGN_PAIRING", pairing=ambiguous)


def test_the_pairing_table_comes_from_the_configuration(tmp_path):
    other = (
        "  pairing:\n    recap_cfg_*: my_cfg_config\n    pi05_*: pi05_fr3_all_state\n"
    )
    arm = ARM_B.replace("config: pi05_fr3_all_state_cfg", "config: my_cfg_config")
    assert plan(tmp_path, pairing=other, arms=(ARM_A, arm)).children


def test_a_reset_policy_per_arm_needs_treatment_includes_reset(tmp_path):
    arm = ARM_B + (
        "      policy_reset:\n        checkpoint_dir: /ckpt/recap_cfg_reset\n"
        "        config: pi05_fr3_all_state_cfg\n        port: 8000\n"
    )
    none = "  layouts:\n    source: none\n"
    refused(
        tmp_path,
        "E_CAMPAIGN_ARMS",
        arms=(ARM_A, arm),
        strategy="single_policy",
        layouts=none,
    )
    found = plan(
        tmp_path,
        folder="job2",
        arms=(ARM_A, arm),
        strategy="single_policy",
        layouts=none,
        extra_campaign="  treatment_includes_reset: true\n",
    )
    assert found.children
    refused(tmp_path / "x", "E_CAMPAIGN_ARMS", arms=(ARM_A, arm))  # human_assisted


def test_layout_source_none_is_bound_to_the_reset_policy_mode(tmp_path):
    none = "  layouts:\n    source: none\n"
    refused(tmp_path / "a", "E_CAMPAIGN_LAYOUTS", layouts=none)  # human_assisted
    refused(tmp_path / "b", "E_CAMPAIGN_LAYOUTS", strategy="single_policy")
    found = plan(tmp_path / "c", strategy="single_policy", layouts=none)
    assert all(
        slot.startswith("slot") for t in found.schedule.trials() for slot in [t.slot]
    )
    refused(
        tmp_path / "d",
        "E_CAMPAIGN_LAYOUTS",
        layouts="  layouts:\n    source: card_set\n",
    )
    refused(
        tmp_path / "e",
        "E_CAMPAIGN_LAYOUTS",
        layouts="  layouts:\n    source: card_set\n    file: layouts.yaml\n    per_round: 3\n",
    )


def test_too_few_cards_or_a_bad_card_file_is_refused(tmp_path):
    refused(tmp_path / "a", "E_CAMPAIGN_LAYOUTS", cards=1)
    path = write_job(tmp_path / "b")
    (tmp_path / "b" / "layouts.yaml").write_text(
        "schema_version: levi.aeri.layouts.v1\ncards:\n  c01:\n    colour: red\n"
    )
    with pytest.raises(spec.CampaignError) as caught:
        spec.plan_campaign(path, job_root=tmp_path / "jobs", planner=FakePlanner())
    assert caught.value.code == "E_CAMPAIGN_LAYOUTS"


def test_randomized_blocks_need_one_trial_segments_and_step_is_reserved(tmp_path):
    refused(
        tmp_path / "a",
        "E_CAMPAIGN_SCHEDULE",
        kind="randomized_blocks",
        segment_trials=3,
    )
    found = plan(tmp_path / "b", kind="randomized_blocks", segment_trials=None)
    assert {c.trials for c in found.children} == {1}
    primary = "  primary:\n    comparison: [B, A]\n    sequential: step\n"
    refused(tmp_path / "c", "E_CAMPAIGN_SCHEDULE", extra_campaign=primary)
    unknown = "  primary:\n    comparison: [B, Z]\n"
    refused(tmp_path / "d", "E_CAMPAIGN_SCHEMA", extra_campaign=unknown)
    same = "  primary:\n    comparison: [B, B]\n"
    refused(tmp_path / "e", "E_CAMPAIGN_ARMS", extra_campaign=same)


def test_an_invalid_shared_job_is_refused(tmp_path):
    refused(tmp_path, "E_CAMPAIGN_JOB", termination="  unknown_key: 1\n")
    refused(tmp_path / "x", "E_CAMPAIGN_SCHEMA", extra_campaign="  colour: blue\n")


def test_two_arms_cannot_share_a_recording_group(tmp_path):
    same = ARM_B + "      group: pi05_fr3_all_step49999\n"
    refused(tmp_path, "E_CAMPAIGN_ARMS", arms=(ARM_A, same))


# --- the YAML subset, written -----------------------------------------------------------


def test_written_job_files_read_back_as_themselves():
    document = {
        "schema_version": "levi.aeri.job.v1",
        "task": {"instruction": "it's a 'test' # not a comment: no, \"really\" é"},
        "termination": {"goal_event_types": ["gripper_open", "a, b"], "min_steps": 3},
        "reset": {"enabled": False, "x": None, "f": 0.25, "big": 1e-07},
        "empty": {},
    }
    text = spec.to_yaml(document)
    back = sa.parse_document(text)
    assert back["task"] == document["task"]
    assert back["termination"] == document["termination"]
    assert back["reset"] == document["reset"]
    assert back["empty"] is None
    with pytest.raises(spec.CampaignError):
        spec.to_yaml({"task": {"instruction": "two\nlines"}})
    with pytest.raises(spec.CampaignError):
        spec.to_yaml({"x": [{"a": 1}]})
