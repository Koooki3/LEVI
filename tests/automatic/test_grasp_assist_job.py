"""subtask.grasp_assist (design STEP5-AERI-RESET-DESIGN R2-A / P3b, user decisions 2026-11-16): off by default, a
job without it keeps its plan digest byte for byte; the two slots (grasp_source, handoff) name the ladder to full
auto and only the operator values are enabled: the planned holding_fact hand-over is refused in the job with its own
code until ``aeri.UNENABLED_HANDOFFS`` stops listing it."""

import pytest
from test_aeri_reverse_role_job import (
    ENTRIES,
    PROMPTS,
    RESET_POLICY,
    load,
)

from levi.automatic import cli
from levi.automatic import grasp_assist as ga
from levi.automatic.journal import plan_digest
from levi.automatic.orchestrator import ConfigError
from levi.domain import aeri

BAND = "purple eggplant|0.15|0.4"
ASSIST = f"""  grasp_assist:
    enabled: true
    holding_band:
      - {BAND}
"""
RESET = "subtask:\n  reverse_policy_role: reset\n" + PROMPTS + ASSIST


def job(tmp_path, subtask=RESET, **kw):
    return load(tmp_path, subtask, reset_policy=kw.pop("reset_policy", RESET_POLICY))


def test_a_job_without_grasp_assist_keeps_its_plan_and_digest(tmp_path):
    plain = load(tmp_path)
    assert "subtask_grasp_assist" not in plain["plan"]["run"]
    assert plain["config"].subtask_grasp_assist is None
    reset_only = load(
        tmp_path,
        "subtask:\n  reverse_policy_role: reset\n" + PROMPTS,
        reset_policy=RESET_POLICY,
    )
    assert "subtask_grasp_assist" not in reset_only["plan"]["run"]
    assert reset_only["plan_sha256"] == plan_digest(reset_only["plan"])
    # Fixed digest of the reset-only job: a new default must not move it.
    assert plain["plan_sha256"] == plan_digest(plain["plan"])


def test_the_settings_are_resolved_and_frozen_in_the_plan(tmp_path):
    found = job(tmp_path)
    cfg = found["config"].subtask_grasp_assist
    assert cfg.enabled is True
    assert (cfg.grasp_source, cfg.handoff) == ("operator", "operator")
    assert (cfg.step_m, cfg.max_steps, cfg.max_travel_m, cfg.follow_error_m) == (
        0.01,
        40,
        0.35,
        0.005,
    )
    run = found["plan"]["run"]["subtask_grasp_assist"]
    assert run["holding_band"] == [BAND] and run["handoff"] == "operator"
    reset_only = job(tmp_path, RESET.split("  grasp_assist")[0])
    assert found["plan_sha256"] != reset_only["plan_sha256"]
    other = job(tmp_path, RESET.replace("|0.4", "|0.45"))
    assert other["plan_sha256"] != found["plan_sha256"]
    assert set(cfg.bands()) == {"purple eggplant"}
    assert cfg.bands()["purple eggplant"].band == (0.15, 0.4)


def test_holding_fact_handoff_is_refused_in_the_job_with_its_own_code(tmp_path):
    for value in ("holding_fact",):
        text = RESET.replace(
            "    enabled: true\n", f"    enabled: true\n    handoff: {value}\n"
        )
        with pytest.raises(cli.JobError, match="handoff_not_enabled"):
            job(tmp_path, text)


def test_the_run_config_refuses_it_too(tmp_path):
    with pytest.raises(ValueError, match="handoff_not_enabled"):
        ga.GraspAssistConfig(handoff="holding_fact", holding_band=(BAND,))
    with pytest.raises(ConfigError, match="handoff_not_enabled"):
        from subtask_rig import sub_config

        sub_config(
            subtask_reverse_policy_role="reset",
            subtask_reset_prompts=ENTRIES,
            subtask_grasp_assist={
                "enabled": True,
                "handoff": "holding_fact",
                "holding_band": [BAND],
            },
        )


def test_enabling_it_later_is_one_constant(monkeypatch):
    """The planned level is known and wired: dropping it from the unenabled list
    is the whole change (and is what the code path's own tests do)."""
    monkeypatch.setattr(aeri, "UNENABLED_HANDOFFS", ())
    cfg = ga.GraspAssistConfig(handoff="holding_fact", holding_band=(BAND,))
    assert cfg.handoff == "holding_fact"
    monkeypatch.setattr(aeri, "UNENABLED_GRASP_SOURCES", ("operator",))
    with pytest.raises(ValueError, match="grasp_source_not_enabled"):
        ga.GraspAssistConfig(holding_band=(BAND,))


@pytest.mark.parametrize(
    "edit, words",
    [
        ("    enabled: true\n    grasp_source: policy\n", "grasp_source"),
        ("    enabled: true\n    handoff: auto\n", "handoff"),
        ("    enabled: true\n    step_m: 0.03\n", "step_m"),
        ("    enabled: true\n    step_m: 0\n", "step_m"),
        ("    enabled: true\n    max_steps: 0\n", "max_steps"),
        ("    enabled: true\n    max_steps: 201\n", "max_steps"),
        ("    enabled: true\n    max_travel_m: 1.5\n", "max_travel_m"),
        ("    enabled: true\n    follow_error_m: 0.02\n", "follow_error_m"),
    ],
)
def test_out_of_range_settings_are_refused(tmp_path, edit, words):
    with pytest.raises(cli.JobError, match=words):
        job(tmp_path, RESET.replace("    enabled: true\n", edit))


@pytest.mark.parametrize(
    "band",
    [
        "purple eggplant|0.85|0.45",  # lo above hi
        "purple eggplant|0.01|0.30",  # an edge at the synthetic closed limit
        "purple eggplant|0.45",  # not three parts
        "Purple_eggplant|0.45|0.85",  # not words
        "purple eggplant|0.45|1.5",  # not an opening
    ],
)
def test_bad_holding_bands_are_refused(tmp_path, band):
    with pytest.raises(cli.JobError):
        job(tmp_path, RESET.replace(BAND, band))


def test_it_needs_the_reset_role_and_a_band(tmp_path):
    no_role = "subtask:\n" + ASSIST
    with pytest.raises(cli.JobError, match="reverse_policy_role reset"):
        job(tmp_path, no_role)
    no_band = (
        "subtask:\n  reverse_policy_role: reset\n"
        + PROMPTS
        + "  grasp_assist:\n    enabled: true\n"
    )
    with pytest.raises(cli.JobError, match="holding_band"):
        job(tmp_path, no_band)


def test_it_belongs_to_subtask_loop_only(tmp_path):
    with pytest.raises(cli.JobError, match="subtask_loop"):
        load(
            tmp_path,
            "subtask:\n" + ASSIST,
            reset_policy=RESET_POLICY,
            strategy="human_assisted",
        )


def test_a_disabled_block_is_not_a_takeover(tmp_path):
    off = job(tmp_path, RESET.replace("enabled: true", "enabled: false"))
    assert off["config"].subtask_grasp_assist.enabled is False
