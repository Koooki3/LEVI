"""UI-R1: the success condition of a job (recorded and shown only) and the
session requirements the run snapshot echoes from the frozen plan."""

import pytest
from test_aeri_cli import CONTRACT, job_text
from test_event_pause_judge import LEGACY_PLAN_SHA256

from levi.automatic import cli
from levi.domain import aeri

SUCCESS = "All plates of one colour are stacked; no colour is left alone."


def write(tmp_path, root="/pinned/rollouts", success=None, **over):
    (tmp_path / "initial-state.yaml").write_text(CONTRACT)
    text = job_text(root, **over)
    if success is not None:
        text = text.replace(
            "  initial_state_spec:",
            f"  success_condition: {success}\n  initial_state_spec:",
        )
    path = tmp_path / "job.yaml"
    path.write_text(text)
    return path


def test_without_a_success_condition_the_plan_is_byte_identical(tmp_path):
    job = cli.load_job(write(tmp_path))
    assert "success_condition" not in job["plan"]
    assert job["plan_sha256"] == LEGACY_PLAN_SHA256


def test_a_success_condition_enters_the_frozen_plan(tmp_path):
    plain = cli.load_job(write(tmp_path))
    job = cli.load_job(write(tmp_path, success=f'"{SUCCESS}"'))
    assert job["plan"]["success_condition"] == SUCCESS
    assert job["plan_sha256"] != plain["plan_sha256"]
    # The texts handed to the recorder and the policy are not touched.
    assert job["texts"] == plain["texts"]


def test_a_human_assisted_job_keeps_the_success_condition(tmp_path):
    job = cli.load_job(
        write(tmp_path, success=f'"{SUCCESS}"', strategy="human_assisted")
    )
    assert job["plan"]["success_condition"] == SUCCESS


@pytest.mark.parametrize(
    "value",
    ['""', '"' + "x" * 501 + '"', '"two\\nparagraphs"', '"a\\rb"'],
)
def test_a_success_condition_is_one_bounded_paragraph(tmp_path, value):
    with pytest.raises(cli.JobError):
        cli.load_job(write(tmp_path, success=value))


def test_the_limit_is_500_characters():
    assert aeri.MAX_SUCCESS_CONDITION == 500
    task = aeri.JobTask(success_condition="x" * 500)
    assert len(task.success_condition) == 500
