# ruff: noqa: F401, F811
"""UI-R1: the success condition of a job (recorded and shown only) and the
session requirements the run snapshot echoes from the frozen plan."""

import pytest
from test_aeri_cli import CONTRACT, job_text
from test_api_common import client, finished_run, home, job_id_of, roots, write_job
from test_event_pause_judge import LEGACY_PLAN_SHA256

from levi.automatic import api, cli, launch
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


# --- what the plan card and the session window echo --------------------------------------------

BASE = "/api/levi/automatic"
REQUIREMENT_KEYS = {
    "task", "episodes", "max_steps", "allow_early_stop", "reset_mode",
    "policies", "inference",
}  # fmt: skip


def with_success(path, text=SUCCESS):
    path.write_text(
        path.read_text().replace(
            "  initial_state_spec:",
            f'  success_condition: "{text}"\n  initial_state_spec:',
        )
    )


def test_the_plan_view_echoes_the_requirements(client, roots, tmp_path):
    with_success(write_job(roots, tmp_path, name="r-req"))
    body = client.post(
        f"{BASE}/plan",
        json={"job_id": job_id_of(client, "r-req"), "execution_mode": "dry_run"},
    ).json()
    need = body["requirements"]
    assert set(need) == REQUIREMENT_KEYS
    assert need["task"] == {
        "instruction": "stack the plates",
        "reset_instruction": "Reset: stack the plates",
        "success_condition": SUCCESS,
    }
    assert need["episodes"] == 2
    assert need["max_steps"] == {"forward": 30, "reset": 20}
    assert need["allow_early_stop"] is True
    assert need["reset_mode"] == "single_reset_policy"
    assert need["inference"] == {"mode": "legacy_async", "physical_hold": False}
    # A job file without checkpoints in it names none; no path is echoed.
    assert need["policies"] == {"forward": None, "reset": None}
    assert "/pinned" not in str(need) and str(tmp_path) not in str(need)


def test_the_snapshot_echoes_the_requirements_of_the_frozen_plan(
    client, roots, tmp_path
):
    path = write_job(roots, tmp_path, name="r-snap")
    with_success(path)
    request = launch.LaunchRequest(
        job_path=str(path), execution_mode="dry_run", entry="api", backend="inprocess"
    )
    found = launch.plan(request)
    launch.launch(
        request,
        plan_sha256=found.plan_sha256,
        launch_token=found.launch_token,
        deadline_s=20,
    )
    need = client.get(f"{BASE}/runs/r-snap").json()["requirements"]
    assert set(need) == REQUIREMENT_KEYS
    assert need["task"]["success_condition"] == SUCCESS
    assert need["task"]["instruction"] == "stack the plates"
    assert need["episodes"] == 2 and need["max_steps"]["forward"] == 30


def test_a_job_without_a_success_condition_says_none(client, roots, tmp_path):
    run_id = finished_run(client, tmp_path, roots, name="r-none")
    need = client.get(f"{BASE}/runs/{run_id}").json()["requirements"]
    assert need["task"]["success_condition"] is None


def test_a_human_assisted_plan_has_no_reset_requirements(client, roots, tmp_path):
    write_job(roots, tmp_path, name="r-human", strategy="human_assisted")
    body = client.post(
        f"{BASE}/plan",
        json={"job_id": job_id_of(client, "r-human"), "execution_mode": "dry_run"},
    ).json()
    need = body["requirements"]
    assert need["reset_mode"] == "human_assisted"
    assert need["max_steps"]["reset"] is None
    assert need["task"]["reset_instruction"] is None
    assert need["policies"] == {"forward": None, "reset": None}


def test_an_old_run_without_a_plan_has_no_requirements():
    assert api.requirements_of({}) is None
    assert api.requirements_of(None) is None


# --- the current step of the open episode (a lower bound, never invented) -----------------------


def open_episode(tmp_path, rows, *, state="open"):
    steps = tmp_path / "root" / "grp" / "fold" / "demo_0001"
    steps.mkdir(parents=True)
    text = "step,observed_ns,executed,detail,pose\n" + "".join(
        f"{i},1,yes,ok,[]\n" for i in range(rows)
    )
    (steps / "aeri_steps.csv").write_text(text + "7,1,yes,ok")  # torn last line
    manifest = {
        "rollout_root": str(tmp_path / "root"),
        "group": "grp",
        "episodes": [
            {
                "episode_id": "r.forward.0001",
                "role": "forward",
                "state": state,
                "task_folder": "fold",
                "demo": "demo_0001",
            }
        ],
    }
    return manifest, {"run": {"forward_max_steps": 300}}


def test_the_step_is_the_committed_rows_of_the_open_forward_episode(tmp_path):
    manifest, plan = open_episode(tmp_path, 42)
    got = api.progress_of(manifest, plan, alive=True)
    assert got == {
        "episode_id": "r.forward.0001",
        "step": 42,  # the torn last line is not a row
        "max_steps": 300,
        "source": "steps_csv",
        "lower_bound": True,
        "reason": None,
    }


@pytest.mark.parametrize(
    "case, reason",
    [("sealed", "no_open_episode"), ("dead", "runner_not_alive")],
)
def test_the_step_is_unknown_with_a_reason_not_a_guess(tmp_path, case, reason):
    manifest, plan = open_episode(
        tmp_path, 5, state="complete" if case == "sealed" else "open"
    )
    got = api.progress_of(manifest, plan, alive=case != "dead")
    assert got["step"] is None and got["reason"] == reason
    assert got["max_steps"] == 300


def test_a_missing_or_escaping_steps_file_is_unknown(tmp_path):
    manifest, plan = open_episode(tmp_path, 5)
    manifest["episodes"][0]["demo"] = "../demo_0001"
    got = api.progress_of(manifest, plan, alive=True)
    assert got["step"] is None and got["reason"] == "unreadable"
    assert api.progress_of({}, {}, alive=True)["step"] is None
    assert api.progress_of(None, None, alive=True)["reason"] == "no_open_episode"
