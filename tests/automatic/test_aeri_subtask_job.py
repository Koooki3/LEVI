"""The subtask_loop job file (aeri.JobSubtask, cli.load_job): its settings
reach RunConfig, its plan keeps the task name and the template (never a
subtask text), other modes keep their plan digests, and bad values are
refused with clear messages."""

import pytest

from levi.automatic import cli
from levi.automatic import subtask_loop as st

JOB = """schema_version: levi.aeri.job.v1
experiment:
  name: r20261017-sub
  episodes: 3
task:
  instruction: stack the plates of same color together
reset:
  strategy: subtask_loop
{extra}"""


def load(tmp_path, extra=""):
    path = tmp_path / "job.yaml"
    path.write_text(JOB.format(extra=extra))
    return cli.load_job(path)


def test_the_defaults(tmp_path):
    job = load(tmp_path)
    cfg = job["config"]
    assert cfg.reset_strategy == "subtask_loop"
    assert cfg.subtask_task == "stack the plates of same color together"
    assert (cfg.subtask_max_steps, cfg.subtask_max_retries) == (200, 5)
    assert cfg.max_subtasks_per_loop is None
    assert cfg.vlm_min_free_mem_gib == 28.0 and cfg.vlm_sleep_fallback == "wait_human"
    assert cfg.vlm_alternation == "sleep" and cfg.home_after_loop is True
    # The VLM sleeps while the policy runs: early stop off unless asked.
    assert cfg.termination.allow_early_stop is False
    assert cfg.subtask_forward_folder == "forward__r20261017-sub__subtask"
    assert cfg.subtask_reverse_folder == "forward__r20261017-sub__reverse"
    assert job["warnings"] == []
    plan = job["plan"]
    assert plan["subtask"]["templates"] == st.INSTRUCTION_TEMPLATES
    vlm = plan["subtask"]["vlm"]
    assert (vlm["port"], vlm["gpu_memory_utilization"], vlm["max_model_len"]) == (
        8100,
        0.70,
        16384,
    )
    assert vlm["min_model_len"] == 16384 and vlm["sleep_mode"] is True
    assert plan["texts"]["forward"] == cfg.subtask_task
    assert plan["run"]["subtask_task"] == cfg.subtask_task
    assert cli.session_folders(cfg) == {
        "forward": cfg.subtask_forward_folder,
        "reset": cfg.subtask_reverse_folder,
    }


def test_the_settings_reach_the_run(tmp_path):
    job = load(
        tmp_path,
        """subtask:
  max_steps: 150
  max_retries: 2
  max_subtasks_per_loop: 4
  vlm_min_free_mem_gib: 30
  vlm_sleep_fallback: level2
  vlm_alternation: co_resident
  forward_folder: plates__sub
  reverse_folder: plates__rev
  vlm:
    model: /models/small-vlm
    served_name: small
    port: 8111
    gpu_memory_utilization: 0.3
""",
    )
    cfg = job["config"]
    assert (
        cfg.subtask_max_steps,
        cfg.subtask_max_retries,
        cfg.max_subtasks_per_loop,
    ) == (
        150,
        2,
        4,
    )
    assert cfg.vlm_sleep_fallback == "level2" and cfg.vlm_alternation == "co_resident"
    assert (cfg.subtask_forward_folder, cfg.subtask_reverse_folder) == (
        "plates__sub",
        "plates__rev",
    )
    vlm = job["plan"]["subtask"]["vlm"]
    assert (vlm["model"], vlm["served_name"], vlm["port"]) == (
        "/models/small-vlm",
        "small",
        8111,
    )
    text = cli.subtask_summary(cfg)
    assert "150 steps" in text and "co_resident" in text and "plates__rev" in text


@pytest.mark.parametrize(
    "extra, words",
    [
        ("subtask:\n  max_steps: 0\n", "max_steps"),
        ("subtask:\n  max_retries: 0\n", "max_retries"),
        ("subtask:\n  max_subtasks_per_loop: 0\n", "max_subtasks_per_loop"),
        ("subtask:\n  vlm_sleep_fallback: pray\n", "vlm_sleep_fallback"),
        ("subtask:\n  home_after_loop: false\n", "home_after_loop"),
        ("subtask:\n  vlm:\n    port: 8000\n", "robot or policy port"),
        ("termination:\n  allow_early_stop: true\n", "allow_early_stop"),
        ("subtask:\n  forward_folder: a\n  reverse_folder: a\n", "own folders"),
    ],
)
def test_bad_values_are_refused(tmp_path, extra, words):
    with pytest.raises(cli.JobError, match=words):
        load(tmp_path, extra)


def test_the_task_name_is_required(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text(
        JOB.format(extra="").replace(
            "task:\n  instruction: stack the plates of same color together\n", ""
        )
    )
    with pytest.raises(cli.JobError, match="task.instruction"):
        cli.load_job(path)


def test_reset_policy_keys_are_ignored_and_subtask_keys_elsewhere_too(tmp_path):
    job = load(tmp_path, "policies:\n  reset:\n    max_steps: 80\n")
    assert job["ignored"] == {"policies.reset": "strategy subtask_loop"}
    other = tmp_path / "other.yaml"
    other.write_text(
        "schema_version: levi.aeri.job.v1\nexperiment:\n  name: r-h\nreset:\n"
        "  strategy: human_assisted\nsubtask:\n  max_steps: 10\n"
    )
    found = cli.load_job(other)
    assert found["ignored"] == {"subtask.max_steps": "strategy is not subtask_loop"}
    assert "subtask_task" not in found["plan"]["run"]
    assert "subtask" not in found["plan"]


def test_a_dry_run_completes_on_the_fakes(tmp_path):
    job = load(tmp_path, "subtask:\n  max_steps: 6\n")
    found = cli.DryRun(job, tmp_path / "ws").run()
    assert found["state"] == "COMPLETED"
    reasons = [t[2] for t in found["transitions"]]
    assert reasons.count("subtask_reverse_done") == 3
    assert reasons.count("subtask_forward_done") == 3
    assert found["metrics"]["subtasks"]["rounds"] == 3
    rollouts = tmp_path / "ws" / "aeri"
    assert {p.name for p in rollouts.iterdir()} == {
        job["config"].subtask_forward_folder,
        job["config"].subtask_reverse_folder,
    }


def test_subtask_rollouts_carry_their_subtask_in_the_metadata(tmp_path):
    import json

    job = load(tmp_path, "subtask:\n  max_steps: 6\n")
    cli.DryRun(job, tmp_path / "ws", episodes=1).run()
    root = tmp_path / "ws" / "aeri"
    forward = root / job["config"].subtask_forward_folder / "demo_0001"
    meta = json.loads((forward / "metadata.json").read_text())
    sub = meta["eval"]["aeri"]["subtask"]
    assert sub["loop"] == "forward" and sub["round"] == 1
    assert (sub["index"], sub["attempt"]) == (0, 1)
    assert (
        meta["task_description"]
        == sub["instruction"]
        == st.render("pink_plate_2", "pink_plate_1")
    )
    reverse = root / job["config"].subtask_reverse_folder / "demo_0001"
    meta = json.loads((reverse / "metadata.json").read_text())
    assert meta["eval"]["aeri"]["subtask"]["loop"] == "reverse"
    assert meta["eval"]["aeri"]["episode_role"] == "reset"


def test_export_and_the_pool_never_take_a_subtask_rollout_as_success(tmp_path):
    """Even a subtask rollout whose metadata somehow said success has no
    episode outcome for export and the pool (raw.demo_outcome, the robot flag
    they read): the outcome belongs to the round."""
    import json

    from levi.conversion import raw

    job = load(tmp_path, "subtask:\n  max_steps: 6\n")
    cli.DryRun(job, tmp_path / "ws", episodes=1).run()
    root = tmp_path / "ws" / "aeri"
    demos = sorted(root.glob("*/demo_*"))
    assert demos
    for demo in demos:
        assert raw.demo_outcome(demo) is None
        meta = json.loads((demo / "metadata.json").read_text())
        meta["eval"]["outcome"] = "success"
        (demo / "metadata.json").write_text(json.dumps(meta))
        assert raw.demo_outcome(demo) is None


@pytest.mark.parametrize(
    "setting", ["", "subtask:\n  vlm:\n    inventory_max_tokens: null\n"]
)
def test_unset_inventory_budget_preserves_the_exact_legacy_plan(tmp_path, setting):
    from levi.automatic.journal import plan_digest

    job = load(tmp_path, setting)
    # The complete VLM profile before inventory_max_tokens was introduced.
    legacy_vlm = {
        "model": None,
        "served_name": None,
        "port": 8100,
        "gpu_memory_utilization": 0.70,
        "max_model_len": 16384,
        "min_model_len": 16384,
        "sleep_mode": True,
        "prewarm": True,
        "fallback_gpu_memory_utilization": 0.72,
        "request_timeout_s": 60,
        "wake_timeout_s": 60,
    }
    legacy_plan = {
        **job["plan"],
        "subtask": {"templates": st.INSTRUCTION_TEMPLATES, "vlm": legacy_vlm},
    }
    assert job["plan"] == legacy_plan
    assert job["plan_sha256"] == plan_digest(legacy_plan)
    assert job["config"].plan_sha256 == plan_digest(legacy_plan)
    assert "inventory output budget" not in cli.subtask_summary(
        job["config"], plan=job["plan"]
    )


@pytest.mark.parametrize("budget", [1, 400, 1024, 4096])
def test_inventory_budget_is_frozen_and_shown_for_review(tmp_path, budget):
    baseline = load(tmp_path)
    job = load(tmp_path, f"subtask:\n  vlm:\n    inventory_max_tokens: {budget}\n")
    assert job["plan"]["subtask"]["vlm"]["inventory_max_tokens"] == budget
    assert job["plan_sha256"] != baseline["plan_sha256"]
    assert job["config"].plan_sha256 == job["plan_sha256"]
    assert f"inventory output budget {budget} tokens (explicit override)" in (
        cli.subtask_summary(job["config"], plan=job["plan"])
    )
    before = baseline["plan"]["subtask"]["vlm"]
    after = dict(job["plan"]["subtask"]["vlm"])
    after.pop("inventory_max_tokens")
    assert after == before


@pytest.mark.parametrize("value", ["0", "-1", "4097", "true", "400.0", "'1024'"])
def test_inventory_budget_rejects_invalid_job_values(tmp_path, value):
    with pytest.raises(cli.JobError, match="inventory_max_tokens"):
        load(tmp_path, f"subtask:\n  vlm:\n    inventory_max_tokens: {value}\n")


def test_inventory_budget_leaves_room_in_the_smallest_model_context(tmp_path):
    with pytest.raises(cli.JobError, match="below min_model_len"):
        load(
            tmp_path,
            "subtask:\n  vlm:\n    min_model_len: 1024\n"
            "    inventory_max_tokens: 1024\n",
        )


def test_start_home_is_frozen_only_when_enabled(tmp_path):
    default = load(tmp_path)
    disabled = load(tmp_path, "subtask:\n  start_home: false\n")
    omitted = load(tmp_path, "subtask:\n  start_home: null\n")
    enabled = load(tmp_path, "subtask:\n  start_home: true\n")
    assert default["plan_sha256"] == disabled["plan_sha256"] == omitted["plan_sha256"]
    assert "start_home" not in default["plan"]["run"]
    assert enabled["config"].start_home is True
    assert enabled["plan"]["run"]["start_home"] is True
    assert enabled["plan_sha256"] != default["plan_sha256"]


def test_start_home_setting_cannot_be_ignored_in_another_strategy(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text(
        JOB.format(extra="subtask:\n  start_home: true\n").replace(
            "strategy: subtask_loop", "strategy: human_assisted"
        )
    )
    with pytest.raises(cli.JobError, match="start_home"):
        cli.load_job(path)


@pytest.mark.parametrize("mode", ["local", "external"])
def test_provider_mode_is_a_frozen_declaration_only(tmp_path, mode):
    from dataclasses import asdict

    baseline = load(tmp_path)
    job = load(tmp_path, f"subtask:\n  vlm:\n    provider_mode: {mode}\n")
    assert job["plan"]["subtask"]["vlm"]["provider_mode"] == mode
    assert job["plan_sha256"] != baseline["plan_sha256"]
    after = dict(job["plan"]["subtask"]["vlm"])
    after.pop("provider_mode")
    assert after == baseline["plan"]["subtask"]["vlm"]
    before_config, after_config = asdict(baseline["config"]), asdict(job["config"])
    before_config.pop("plan_sha256")
    after_config.pop("plan_sha256")
    assert before_config == after_config


def test_null_provider_mode_keeps_the_previous_plan_and_hash(tmp_path):
    baseline = load(tmp_path, "subtask:\n  vlm:\n    inventory_max_tokens: 1024\n")
    job = load(
        tmp_path,
        "subtask:\n  vlm:\n    inventory_max_tokens: 1024\n    provider_mode: null\n",
    )
    assert "provider_mode" not in job["plan"]["subtask"]["vlm"]
    assert job["plan"] == baseline["plan"]
    assert job["plan_sha256"] == baseline["plan_sha256"]


@pytest.mark.parametrize("value", ["unknown", "LOCAL", "true", "1", "[]", "{}"])
def test_invalid_provider_mode_is_refused(tmp_path, value):
    with pytest.raises(cli.JobError, match="provider_mode"):
        load(tmp_path, f"subtask:\n  vlm:\n    provider_mode: {value}\n")


def test_early_check_is_frozen_only_when_given(tmp_path):
    default = load(tmp_path)
    enabled = load(
        tmp_path,
        "subtask:\n  early_check:\n    on_events: true\n    max_checks: 2\n",
    )
    # Off (absent) keeps every existing plan digest.
    assert "subtask_early_check" not in default["plan"]["run"]
    assert default["config"].subtask_early_check is None
    early = enabled["config"].subtask_early_check
    assert isinstance(early, st.EarlyCheck)
    assert early.on_events is True and early.max_checks == 2
    assert early.event_types == st.EARLY_EVENT_TYPES
    assert enabled["plan"]["run"]["subtask_early_check"] == early.describe()
    assert enabled["plan_sha256"] != default["plan_sha256"]
    interval = load(tmp_path, "subtask:\n  early_check:\n    every_steps: 60\n")
    assert interval["config"].subtask_early_check.every_steps == 60
    assert interval["plan_sha256"] not in (
        default["plan_sha256"],
        enabled["plan_sha256"],
    )


@pytest.mark.parametrize(
    "block, words",
    [
        ("    every_steps: 20\n", "every_steps"),
        ("    max_checks: 0\n", "max_checks"),
        ("    on_events: false\n", "on_events"),
        ("    on_events: true\n    event_types: [nope]\n", "event_types"),
        ("    on_events: true\n    surprise: 1\n", "surprise"),
    ],
)
def test_bad_early_check_values_are_refused(tmp_path, block, words):
    with pytest.raises(cli.JobError, match=words):
        load(tmp_path, "subtask:\n  early_check:\n" + block)


def test_early_check_cannot_be_ignored_in_another_strategy(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text(
        JOB.format(extra="subtask:\n  early_check:\n    on_events: true\n").replace(
            "strategy: subtask_loop", "strategy: human_assisted"
        )
    )
    with pytest.raises(cli.JobError, match="early_check"):
        cli.load_job(path)
