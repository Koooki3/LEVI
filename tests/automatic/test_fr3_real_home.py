"""Home verification settles instead of reading once (L2-REAL-HOME).

Fake env and fake clock; no socket, robot or GPU. Thresholds are the reviewed ones.
"""

import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from fr3_real_support import HOME, TOLERANCE_MM, build, lagging, notes, token
from test_fr3_bridge import no_network, rig  # noqa: F401

from levi.automatic import fr3_bridge as B
from levi.automatic import fr3_run as R


def test_home_polls_until_the_lagging_arm_enters_the_tolerance():
    r = build(home_settle_timeout_s=3.0, settle_poll_s=0.1)
    r.env.pose_fn = lagging(0.020, 0.6)  # 20 mm off right after the last step
    found = r.robot.home(token=token(r, "home"))
    assert (found.executed, found.detail) == ("yes", "home_verified")
    assert found.error_mm <= TOLERANCE_MM
    assert r.robot.latched is None
    assert r.env.reads > 3  # it really polled
    assert any(code == "home_measured" for code, _ in found.notes)


def test_home_that_never_converges_latches_with_the_measured_numbers():
    r = build(home_settle_timeout_s=1.0, settle_poll_s=0.1)
    r.env.pose_fn = lambda since: np.array([HOME[0] + 0.012, *HOME[1:], 1.0])
    r.env.grip_fn = lambda since: 0.95
    found = r.robot.home(token=token(r, "home"))
    assert found.executed == "unknown"
    assert r.robot.latched == found.detail
    for text in ("error_mm=12.0", "tolerance_mm=5", "gripper=0.95", "waited_s=1."):
        assert text in found.detail, found.detail
    assert "rotation_rad=" in found.detail
    assert dict(found.notes)["home_measured"] in found.detail
    assert found.error_mm > TOLERANCE_MM
    # bounded: the fake clock advanced by about the budget, not more
    waited = (r.clock.now() - r.env.cmd_ns) / 1e9
    assert 1.0 <= waited <= 1.3


@pytest.mark.parametrize(
    "dx,rz,grip,ok",
    [
        (0.0049, 0.0, 1.0, True),
        (0.0051, 0.0, 1.0, False),  # position tolerance unchanged: 5 mm
        (0.0, 0.049, 1.0, True),
        (0.0, 0.051, 1.0, False),  # rotation tolerance unchanged: 0.05 rad
        (0.0, 0.0, 0.9, True),
        (0.0, 0.0, 0.899, False),  # open-gripper threshold unchanged: 0.9
    ],
)
def test_home_thresholds_are_not_relaxed_at_the_boundary(dx, rz, grip, ok):
    r = build(home_settle_timeout_s=0.3, settle_poll_s=0.1)
    r.env.pose_fn = lambda since: np.array(
        [HOME[0] + dx, HOME[1], HOME[2], 0.0, 0.0, rz, 1.0]
    )
    r.env.grip_fn = lambda since: grip
    found = r.robot.home(token=token(r, "home"))
    assert (found.executed == "yes") is ok, found.detail
    assert (r.robot.latched is None) is ok


def test_home_polling_stops_at_once_on_operator_stop():
    r = build(home_settle_timeout_s=30.0, settle_poll_s=0.1)
    r.env.pose_fn = lambda since: np.array([HOME[0] + 0.05, *HOME[1:], 1.0])
    r.env.on_read = lambda n: r.robot.stop_event.set() if n >= 3 else None
    began = r.clock.now()
    found = r.robot.home(token=token(r, "home"))
    assert found.executed == "unknown" and r.robot.latched
    assert "stop" in found.detail and "error_mm=50.0" in found.detail
    assert r.env.reads == 3
    assert (r.clock.now() - began) / 1e9 < 1.0


def test_home_polling_stops_at_once_when_health_turns_red():
    r = build(home_settle_timeout_s=30.0, settle_poll_s=0.1)
    r.env.pose_fn = lambda since: np.array([HOME[0] + 0.05, *HOME[1:], 1.0])

    def red(n):
        if n >= 3:
            r.guard.status = "red"

    r.env.on_read = red
    found = r.robot.home(token=token(r, "home"))
    assert found.executed == "unknown" and r.robot.latched
    assert "health" in found.detail and "error_mm=50.0" in found.detail
    assert r.env.reads == 3


def test_home_settle_defaults_are_documented_constants():
    assert B.DEFAULT_HOME_SETTLE_TIMEOUT_S == 5.0
    assert B.DEFAULT_SETTLE_POLL_S == 0.1
    assert B.HOME_ROTATION_RAD == 0.05 and B.HOME_GRIPPER_MIN == 0.9


@pytest.mark.parametrize(
    "key,value",
    [
        ("home_settle_timeout_s", 0),
        ("home_settle_timeout_s", -1),
        ("home_settle_timeout_s", True),
        ("settle_poll_s", 0),
        ("settle_poll_s", 9),  # polling slower than the budget is meaningless
    ],
)
def test_binding_refuses_bad_optional_settle_keys(tmp_path, monkeypatch, key, value):
    r = rig(tmp_path, monkeypatch)
    r.binding["safety"][key] = value
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    with pytest.raises(ValueError, match=key):
        R.load_binding(path, r.job)


def test_binding_accepts_optional_keys_and_keeps_them_off_the_client_args(
    tmp_path, monkeypatch
):
    r = rig(tmp_path, monkeypatch)
    r.binding["safety"].update(home_settle_timeout_s=4, settle_poll_s=0.2)
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    value, _ = R.load_binding(path, r.job)
    assert value["safety"]["home_settle_timeout_s"] == 4
    seen = {}
    client = r.modules[0]
    client.Args = lambda: seen.setdefault("args", NS())
    R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    assert not hasattr(seen["args"], "home_settle_timeout_s")
    assert not hasattr(seen["args"], "settle_poll_s")


def test_binding_accepts_a_15_mm_home_tolerance_and_an_8_s_settle(
    tmp_path, monkeypatch
):
    """The site binding relaxes the home tolerance to 15 mm and the settle wait
    to 8 s (RELAXED[2026-11-17]): no code bound refuses them."""
    r = rig(tmp_path, monkeypatch)
    r.binding["safety"].update(home_tolerance_mm=15, home_settle_timeout_s=8)
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    value, _ = R.load_binding(path, r.job)
    assert value["safety"]["home_tolerance_mm"] == 15
    assert value["safety"]["home_settle_timeout_s"] == 8


# --- RELAXED[2026-11-17]: the optional safety.home_rotation_rad ----------------------


@pytest.mark.parametrize("value", [0.05, 0.08, 0.1, 0.01])
def test_binding_accepts_a_bounded_home_rotation_key(tmp_path, monkeypatch, value):
    r = rig(tmp_path, monkeypatch)
    r.binding["safety"]["home_rotation_rad"] = value
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    assert R.load_binding(path, r.job)[0]["safety"]["home_rotation_rad"] == value


@pytest.mark.parametrize("value", [0, -0.01, 0.1001, 0.2, 1, True, "0.08"])
def test_binding_refuses_a_home_rotation_key_outside_the_bound(
    tmp_path, monkeypatch, value
):
    r = rig(tmp_path, monkeypatch)
    r.binding["safety"]["home_rotation_rad"] = value
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    with pytest.raises(ValueError, match="home_rotation_rad"):
        R.load_binding(path, r.job)


@pytest.mark.parametrize("key,expected", [(None, 0.05), (0.08, 0.08)])
def test_the_key_reaches_the_robot_and_an_old_binding_keeps_0_05(
    tmp_path, monkeypatch, key, expected
):
    seen = []
    original = B.Robot.__init__

    def spy(self, *a, **kw):
        original(self, *a, **kw)
        seen.append(self.rotation_tolerance_rad)

    monkeypatch.setattr(B.Robot, "__init__", spy)
    r = rig(tmp_path, monkeypatch)
    if key is not None:
        r.binding["safety"]["home_rotation_rad"] = key
    R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    assert seen == [expected]  # no key: today's behaviour


def test_the_key_is_not_a_client_arg(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    r.binding["safety"]["home_rotation_rad"] = 0.08
    seen = {}
    r.modules[0].Args = lambda: seen.setdefault("args", NS())
    R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    assert not hasattr(seen["args"], "home_rotation_rad")


@pytest.mark.parametrize(
    "tolerance,rotation,within",
    [
        (None, 0.049, True),
        (None, 0.051, False),
        (0.08, 0.079, True),
        (0.08, 0.081, False),
        (0.1, 0.099, True),
    ],
)
def test_home_and_start_pose_use_the_configured_rotation_tolerance(
    tolerance, rotation, within
):
    kw = {} if tolerance is None else {"home_rotation_rad": tolerance}
    r = build(**kw)
    r.env.pose_fn = lambda since: np.array([*HOME[:5], rotation, 1.0])
    reading = r.robot._home_reading(r.robot.client._resolve_home(r.robot.args))
    assert r.robot._home_within(reading) is within
    result = r.robot.start_pose()
    assert result["ok"] is within
    assert result["rotation_tol_rad"] == (0.05 if tolerance is None else tolerance)
    assert ("rotation_tolerance" in result["unmet"]) is (not within)
    assert f"rotation_tol_rad={result['rotation_tol_rad']} " in r.robot._pose_text(
        reading
    )


@pytest.mark.parametrize("bad", [0, 0.11, True, float("inf"), None, "0.08"])
def test_robot_refuses_a_rotation_tolerance_outside_the_bound(bad):
    with pytest.raises(ValueError, match="home_rotation_rad"):
        build(home_rotation_rad=bad)


def test_binding_refuses_a_settle_budget_that_cannot_fit_the_home_timeout(
    tmp_path, monkeypatch
):
    r = rig(tmp_path, monkeypatch)
    timeout_s = r.job["config"].home_timeout_ns / 1e9
    r.binding["safety"]["home_settle_timeout_s"] = timeout_s
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    with pytest.raises(ValueError, match="home_settle_timeout_s"):
        R.load_binding(path, r.job)


def test_home_failure_detail_reaches_the_journal_and_the_terminal(
    tmp_path, monkeypatch, capsys
):
    r = rig(tmp_path, monkeypatch, faults=("home_error",))
    assert (
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "FAULT_LOCKED"
    )
    detail = notes(r)["home_failed_detail"]
    assert "error_mm=" in detail and "tolerance_mm=5" in detail
    assert "error_mm=" in capsys.readouterr().err


@pytest.mark.parametrize("interrupt", ["stop", "latch", "red", "revoked", "expired"])
def test_home_at_target_cannot_succeed_with_concurrent_safety_failure(interrupt):
    r = build()
    home_token = token(r, "home")

    def interrupt_on_settle(n):
        if r.env.cmd_ns is None:
            return
        if interrupt == "stop":
            r.robot.stop_event.set()
        elif interrupt == "latch":
            r.robot.latched = "injected fault latch"
        elif interrupt == "red":
            r.guard.status = "red"
        elif interrupt == "revoked":
            r.fence.revoke()
        else:
            r.clock.advance(home_token.expires_mono_ns - r.clock.now())

    r.env.on_read = interrupt_on_settle
    result = r.robot.home(token=home_token)
    assert result.executed == "unknown" and r.robot.latched
    assert interrupt in result.detail
    assert r.env.routes.count("pose") == 1
    assert r.env.routes.count("clearerr") == 1
    assert not any(route.endswith("gripper") for route in r.env.routes)
