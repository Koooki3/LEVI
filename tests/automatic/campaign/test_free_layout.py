"""Free placement plans actual trials, never cards or confirmatory facts."""

# ruff: noqa: F401, F811 - imported pytest fixtures
import pytest
from campaign_fixtures import ARM_A, ARM_C, ARM_D, write_job
from campaign_guard import aeri_home_fixture, guard_fixture
from test_api_campaign_common import URL, world

from levi.automatic import api as base
from levi.automatic.campaign import controller, spec


def test_three_policies_ten_trials_four_hundred_steps_are_really_expanded(tmp_path):
    arms = (ARM_A.replace("      role: reference\n", ""), ARM_C, ARM_D)
    path = write_job(
        tmp_path,
        arms=arms,
        kind="blocked",
        segment_trials=10,
        trials=10,
        layouts="  layouts:\n    source: none\n",
        termination="  control_fraction: 0.0\n",
        extra_campaign="  control:\n    fraction: 0.0\n  blinding:\n    operator: none\n",
    )
    path.write_text(path.read_text().replace("max_steps: 120", "max_steps: 400"))
    loaded = spec.load(path)
    schedule, children, _ = spec.draft(loaded)
    assert (
        loaded.cards is None
        and loaded.layouts_sha256 is None
        and loaded.block.primary is None
    )
    assert schedule.conclusion_level == "exploratory"
    assert [s.trials for s in schedule.segments] == [10, 10, 10]
    assert sum(s.trials for s in schedule.segments) == 30
    assert len(children) == 3
    assert all(
        doc["policies"]["forward"]["max_steps"] == 400
        and "reset" not in doc["policies"]
        for _, doc in children.values()
    )


def test_free_layout_rejects_reference_and_nonzero_control(tmp_path):
    path = write_job(tmp_path, layouts="  layouts:\n    source: none\n")
    with pytest.raises(spec.CampaignError, match="free human placement"):
        spec.load(path)


def test_wizard_api_generates_actual_none_zero_control_and_no_primary(world):
    job = world.make_job(
        strategy="human_assisted", run={"episodes": 10, "max_steps": 400}
    )
    body = world.plan_body(
        job,
        layouts={"source": "none"},
        preregistered=False,
        trials_per_arm=10,
        schedule={"kind": "blocked", "segment_trials": 10, "seed": 7},
        arms=[
            {"id": letter, "checkpoint_id": name, "role": "candidate"}
            for letter, name in zip("ABC", ("pi05_a", "recap_b", "recap_c"))
        ],
    )
    response = world.client.post(f"{URL}/plan", json=body)
    assert response.status_code == 200, response.text
    view = response.json()
    assert (
        len(view["segments"]) == 3
        and view["power"]["design"] == "exploratory"
        and view["power"]["rows"] == []
    )
    prepared = controller.prepare(
        body,
        job_path=str(world.jobs / "base.yaml"),
        forward=base.checkpoint_ids("forward"),
        final=False,
    )
    block = prepared.plan.spec.block
    assert (
        block.layouts.source == "none"
        and block.control.fraction == 0
        and block.blinding.operator == "none"
    )
    assert block.primary is None and all(
        a.role == "candidate" for a in block.arms.values()
    )
    assert prepared.plan.spec.job.termination.control_fraction == 0


def test_layout_request_is_strict(world):
    job = world.make_job(strategy="human_assisted")
    for layouts in ({"source": "unknown"}, {"source": "none", "file": "invented"}):
        response = world.client.post(
            f"{URL}/plan", json=world.plan_body(job, layouts=layouts)
        )
        assert response.status_code == 422


def test_ordinary_explicit_none_remains_simple_with_a_reset_policy(world):
    job = world.make_job(strategy="single_reset_policy")
    body = world.plan_body(
        job,
        layouts={"source": "none"},
        preregistered=False,
        schedule={"kind": "blocked", "segment_trials": 4, "seed": 7},
        arms=[
            {"id": "A", "checkpoint_id": "pi05_a", "role": "candidate"},
            {"id": "B", "checkpoint_id": "recap_b", "role": "candidate"},
        ],
    )
    prepared = controller.prepare(
        body,
        job_path=str(world.jobs / "base.yaml"),
        forward=base.checkpoint_ids("forward"),
        final=False,
    )
    assert prepared.plan.spec.block.primary is None
    assert prepared.plan.spec.block.control.fraction == 0
    assert prepared.plan.spec.block.blinding.operator == "none"
    assert prepared.plan.spec.job.termination.control_fraction == 0
