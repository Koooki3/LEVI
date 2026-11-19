"""subtask.reverse_policy_role / subtask.reset_prompts (design STEP5-AERI-RESET-DESIGN, R1 / P1b): a job that
leaves them out keeps its plan digest byte for byte; one that sets them freezes the role and the reset policy's
training sentences in the plan, and every inconsistent combination is refused at load."""

import pytest
from subtask_rig import sub_config

from levi.automatic import cli
from levi.automatic import subtask_loop as st
from levi.automatic.journal import plan_digest
from levi.automatic.orchestrator import ConfigError, RunConfig
from levi.domain import aeri

JOB = """schema_version: levi.aeri.job.v1
experiment:
  name: r20261114-reset
  episodes: 2
policies:
  forward:
    deployment:
      checkpoint_id: pi05_fr3_all_step49999
      config: pi05_fr3_all_state
      checkpoint_dir: /models/pi05_fr3_all_step49999
{reset_policy}task:
  instruction: pick the eggplant into the white plate
reset:
  strategy: {strategy}
{subtask}"""
RESET_POLICY = """  reset:
    deployment:
      checkpoint_id: reset_r1_step20000
      config: reset_r1_step20000
      checkpoint_dir: /models/reset_r1_step20000
"""
ENTRIES = (
    "purple eggplant|white plate|pick the eggplant into the blue plate",
    "pink plate|table|pick peach on green plate",
)
TABLE = {
    "purple eggplant|white plate": "Reset: pick the eggplant into the blue plate",
    "pink plate|table": "Reset: pick peach on green plate",
}
PROMPTS = "  reset_prompts:\n" + "".join(f"    - {e}\n" for e in ENTRIES)
RESET = "subtask:\n  reverse_policy_role: reset\n" + PROMPTS


def load(tmp_path, subtask="", *, reset_policy="", strategy="subtask_loop"):
    path = tmp_path / "job.yaml"
    path.write_text(
        JOB.format(reset_policy=reset_policy, strategy=strategy, subtask=subtask)
    )
    return cli.load_job(path)


def test_a_job_without_the_keys_keeps_its_plan_and_digest(tmp_path):
    job = load(tmp_path)
    run = job["plan"]["run"]
    assert "subtask_reverse_policy_role" not in run
    assert "subtask_reset_prompts" not in run
    assert job["config"].subtask_reverse_policy_role == "forward"
    assert job["config"].subtask_reset_prompts is None
    assert list(job["plan"]["deployments"]) == ["forward"]
    assert job["plan_sha256"] == plan_digest(job["plan"])
    # An explicit forward role is the default, not a new plan.
    explicit = load(tmp_path, "subtask:\n  reverse_policy_role: forward\n")
    assert explicit["plan_sha256"] == job["plan_sha256"]


def test_the_reset_role_and_its_sentences_are_frozen_in_the_plan(tmp_path):
    default = load(tmp_path)
    job = load(tmp_path, RESET, reset_policy=RESET_POLICY)
    cfg = job["config"]
    assert cfg.subtask_reverse_policy_role == "reset"
    assert st.reset_prompt_table(cfg.subtask_reset_prompts) == TABLE
    run = job["plan"]["run"]
    assert run["subtask_reverse_policy_role"] == "reset"
    assert run["subtask_reset_prompts"] == sorted(ENTRIES)
    assert job["plan"]["deployments"]["reset"]["config"] == "reset_r1_step20000"
    assert job["plan_sha256"] != default["plan_sha256"]
    # Another sentence is another plan.
    other = load(
        tmp_path,
        RESET.replace("pick peach on green plate", "pick peach on blue plate"),
        reset_policy=RESET_POLICY,
    )
    assert other["plan_sha256"] != job["plan_sha256"]


def test_reset_policy_keys_are_not_called_ignored_when_the_reverse_loop_uses_them(
    tmp_path,
):
    job = load(tmp_path, RESET, reset_policy=RESET_POLICY)
    assert "policies.reset" not in job["ignored"]
    plain = load(tmp_path, reset_policy=RESET_POLICY)
    assert "policies.reset" in plain["ignored"]
    assert list(plain["plan"]["deployments"]) == ["forward"]
    steps = RESET_POLICY.replace("  reset:\n", "  reset:\n    max_steps: 50\n")
    with_steps = load(tmp_path, RESET, reset_policy=steps)
    assert set(with_steps["ignored"]) == {"policies.reset.max_steps"}


def swap(old, new):
    return RESET.replace(old, new)


@pytest.mark.parametrize(
    "subtask, reset_policy, words",
    [
        (
            "subtask:\n  reverse_policy_role: reset\n",
            RESET_POLICY,
            "needs subtask.reset_prompts",
        ),
        (RESET, "", "policies.reset.deployment"),
        (
            RESET,
            "  reset:\n    deployment:\n      checkpoint_id: x\n      config: y\n",
            "checkpoint_dir",
        ),
        ("subtask:\n" + PROMPTS, RESET_POLICY, "belongs to reverse_policy_role reset"),
        (
            "subtask:\n  reverse_policy_role: forward\n" + PROMPTS,
            RESET_POLICY,
            "belongs to reverse_policy_role reset",
        ),
        (swap("role: reset", "role: reset2"), RESET_POLICY, "reverse_policy_role"),
        (
            swap("|pick the eggplant", "pick the eggplant"),
            RESET_POLICY,
            "reset_prompts",
        ),
        (swap("purple eggplant|", "Purple_eggplant|"), RESET_POLICY, "reset_prompts"),
        (swap("|pick peach on", "|pick peach! on"), RESET_POLICY, "reset_prompts"),
    ],
)
def test_inconsistent_reset_role_jobs_are_refused(
    tmp_path, subtask, reset_policy, words
):
    with pytest.raises(cli.JobError, match=words):
        load(tmp_path, subtask, reset_policy=reset_policy)


@pytest.mark.parametrize(
    "block", ["subtask:\n  reverse_policy_role: reset\n", "subtask:\n" + PROMPTS]
)
def test_the_keys_are_refused_outside_subtask_loop(tmp_path, block):
    with pytest.raises(cli.JobError, match="only allowed with subtask_loop"):
        load(tmp_path, block, reset_policy=RESET_POLICY, strategy="human_assisted")


def test_the_job_schema_snapshot_names_the_new_keys():
    document = aeri.schema_documents()["job"]
    props = document["$defs"]["JobSubtask"]["properties"]
    assert "reverse_policy_role" in props and "reset_prompts" in props
    assert "grasp_assist" in props  # minor 6
    assist = document["$defs"]["JobSubtaskGraspAssist"]["properties"]
    assert {  # minor 7: yaw and the automatic grasp source
        "yaw_enabled",
        "yaw_step_rad",
        "max_yaw_rad",
        "grasp_height",
        "auto_max_retries",
        "locator_coords",
        "grasp_pixel",
    } <= set(assist)
    assert document["x-levi-minor"] == aeri.JOB_MINOR == 7


# --- RunConfig and the table helpers ---------------------------------------------------------


def test_runconfig_checks_the_role_and_the_table():
    cfg = sub_config(
        subtask_reverse_policy_role="reset", subtask_reset_prompts=ENTRIES[::-1]
    )
    assert cfg.subtask_reset_prompts == tuple(sorted(ENTRIES))
    reset = {"subtask_reverse_policy_role": "reset"}
    for bad in (
        reset,  # no table
        {**reset, "subtask_reset_prompts": ()},
        {**reset, "subtask_reset_prompts": ("a b|c|Reset: x",)},
        {**reset, "subtask_reset_prompts": (ENTRIES[0], ENTRIES[0])},  # a key twice
        {**reset, "subtask_reset_prompts": ENTRIES[0]},  # a string, not a list
        {"subtask_reverse_policy_role": "both", "subtask_reset_prompts": ENTRIES},
        {"subtask_reset_prompts": ENTRIES},  # a table without the role
    ):
        with pytest.raises(ConfigError):
            sub_config(**bad)
    with pytest.raises(ConfigError, match="requires subtask_loop"):
        RunConfig(
            run_id="r",
            plan_sha256="0" * 64,
            episodes=1,
            subtask_reverse_policy_role="reset",
        )


def test_the_prompt_lookup_is_by_words_and_never_falls_back_to_the_template():
    hit = st.Subtask(
        0,
        "purple_eggplant_1",
        "white_plate_1",
        (),
        "put the purple eggplant into the white plate",
    )
    miss = st.Subtask(
        1, "purple_eggplant_1", "table", (), "put the purple eggplant on the table"
    )
    assert st.reset_key(hit.pick, hit.place) == "purple eggplant|white plate"
    assert st.reset_prompt(TABLE, hit) == "Reset: pick the eggplant into the blue plate"
    assert st.reset_prompt(TABLE, miss) is None
    assert st.reset_prompt(None, hit) is None and st.reset_prompt({}, hit) is None
    assert st.reset_prompt(TABLE, st.Subtask(2, "NotAnId", "table", (), "x")) is None


def test_the_job_schema_and_the_runtime_read_one_entry_pattern():
    assert st._RESET_ENTRY.pattern == aeri.RESET_PROMPT_ENTRY
    prop = aeri.schema_documents()["job"]["$defs"]["JobSubtask"]["properties"]
    items = prop["reset_prompts"]["anyOf"][0]["items"]
    assert items["pattern"] == aeri.RESET_PROMPT_ENTRY
