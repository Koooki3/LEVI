"""Raw pose components must be finite before home-error aggregation (P1 review).

All tests use existing fake modules; no SDK, device or model is contacted.
"""

import numpy as np
import pytest
from fr3_real_support import HOME, build, run_dir, token
from test_fr3_bridge import no_network, rig  # noqa: F401

from levi.automatic import fr3_run as R

NONFINITE = [float("nan"), float("inf"), -float("inf")]


def pose_with(axis, value):
    pose = np.array([*HOME, 1.0])
    pose[axis] = value
    return pose


@pytest.mark.parametrize("axis", range(7))
@pytest.mark.parametrize("value", NONFINITE)
def test_initial_raw_pose_rejects_every_nonfinite_component_before_warmup(
    tmp_path, monkeypatch, axis, value
):
    r = rig(tmp_path, monkeypatch)
    env_class = r.modules[2].RobotEnv
    observe = env_class.get_observation
    queries = []

    def invalid(self):
        found = observe(self)
        found["robot_state"]["cartesian_position"] = pose_with(axis, value)
        return found

    def warmup(*args, **kwargs):
        queries.append(1)
        pytest.fail("warmup reached with an invalid raw initial pose")

    monkeypatch.setattr(env_class, "get_observation", invalid)
    monkeypatch.setattr(R.bridge.Policy, "warmup", warmup)
    with pytest.raises(ValueError, match="start pose refused"):
        R.execute(r.job, r.binding, load_modules=lambda binding: r.modules)
    assert queries == []
    assert not any(call[0] == "route" for call in r.calls)
    assert not run_dir(r).exists()
    assert r.calls[-1][0] == "closed"


@pytest.mark.parametrize("axis", range(7))
@pytest.mark.parametrize("value", NONFINITE)
def test_home_terminal_measurement_rejects_every_nonfinite_component(axis, value):
    r = build()
    r.env.pose_fn = lambda since: (
        pose_with(axis, value) if since is not None else np.array([*HOME, 1.0])
    )
    result = r.robot.home(token=token(r, "home"))
    assert result.executed != "yes"
    assert result.detail != "home_verified"
    assert r.robot.latched is not None


@pytest.mark.parametrize("axis", range(7))
@pytest.mark.parametrize("value", NONFINITE)
def test_resolved_home_rejects_every_nonfinite_component(axis, value):
    r = build()
    with pytest.raises(ValueError, match="pose"):
        r.robot._home_reading(pose_with(axis, value))


@pytest.mark.parametrize("axis", range(6))
@pytest.mark.parametrize("value", NONFINITE)
def test_bound_home_rejects_every_nonfinite_component(axis, value):
    r = build()
    home = list(HOME)
    home[axis] = value
    r.robot.args.home_pose = home
    assert r.robot.start_pose()["ok"] is False


@pytest.mark.parametrize("length", [0, 3, 5, 8, 9])
@pytest.mark.parametrize("source", ["observed", "expected"])
def test_incomplete_or_extra_pose_dimensions_are_refused(length, source):
    r = build()
    values = np.array([*HOME, 1.0, 0.0, 0.0])[:length]
    expected = np.array([*HOME, 1.0])
    if source == "observed":
        r.env.pose_fn = lambda since: values
    else:
        expected = values
    with pytest.raises(ValueError, match="pose"):
        r.robot._home_reading(expected)


@pytest.mark.parametrize("observed_length", [6, 7])
@pytest.mark.parametrize("expected_length", [6, 7])
def test_existing_six_and_seven_component_pose_representations_stay_valid(
    observed_length, expected_length
):
    r = build()
    pose = np.array([*HOME, 1.0])
    r.env.pose_fn = lambda since: pose[:observed_length]
    assert r.robot._home_reading(pose[:expected_length]) == (0.0, 0.0, 1.0)


@pytest.mark.parametrize("axis", range(7))
@pytest.mark.parametrize("value", NONFINITE)
def test_bad_pose_after_warmup_refuses_journal_creation(
    tmp_path, monkeypatch, axis, value
):
    r = rig(tmp_path, monkeypatch)
    env_class = r.modules[2].RobotEnv
    init = env_class.__init__
    active = {}
    queries = []
    query = r.modules[0]._query_policy

    def created(self, **kwargs):
        init(self, **kwargs)
        active["env"] = self

    def warm_query(*args):
        queries.append(1)
        actions = query(*args)
        active["env"].pose = pose_with(axis, value)
        return actions

    def journal(*args, **kwargs):
        pytest.fail("journal reached with a bad pose after warmup")

    monkeypatch.setattr(env_class, "__init__", created)
    monkeypatch.setattr(R.Orchestrator, "create", journal)
    r.modules[0]._query_policy = warm_query
    with pytest.raises(ValueError, match="start pose refused"):
        R.execute(r.job, r.binding, load_modules=lambda binding: r.modules)
    assert queries == [1]
    assert not any(call[0] == "route" for call in r.calls)
    assert not run_dir(r).exists()
    assert r.calls[-1][0] == "closed"
