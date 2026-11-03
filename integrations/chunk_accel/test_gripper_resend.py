"""The v3 client's gripper re-send wrapper (run_collect_phase.install_gripper_no_resend) on the user's REAL
fr3_env.RobotEnv._update_gripper, loaded read-only from the openpi checkout with stand-ins for pyrealsense2, requests
and scipy (never edited, never run against a robot). Skips without the openpi checkout."""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest

CA = Path(__file__).resolve().parent
_ROOT = os.environ.get("CHUNK_ACCEL_OPENPI_ROOT")
OPENPI = Path(_ROOT) if _ROOT else None
FR3_ENV = OPENPI / "examples" / "fr3_local" / "fr3_env.py" if OPENPI else None

pytestmark = pytest.mark.skipif(
    FR3_ENV is None or not FR3_ENV.is_file(),
    reason="set CHUNK_ACCEL_OPENPI_ROOT to an openpi checkout",
)


def _stub(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


@pytest.fixture
def loaded(monkeypatch):
    """(entry module, fr3_env module, clock list) with the stand-ins in place."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    _stub(monkeypatch, "pyrealsense2")
    _stub(
        monkeypatch,
        "requests",
        RequestException=type("RequestException", (Exception,), {}),
    )
    _stub(monkeypatch, "scipy")
    _stub(monkeypatch, "scipy.spatial")
    _stub(monkeypatch, "scipy.spatial.transform", Rotation=object)
    rc_stub = types.ModuleType("rc_stub")

    @dataclasses.dataclass
    class StubArgs:
        pass

    rc_stub.Args = StubArgs
    monkeypatch.setitem(sys.modules, "run_robotiq_client_collect_data", rc_stub)
    monkeypatch.setitem(
        sys.modules, "tyro", types.SimpleNamespace(cli=lambda cls: cls())
    )
    monkeypatch.setenv("CHUNK_ACCEL_OPENPI_ROOT", str(OPENPI))
    monkeypatch.syspath_prepend(str(CA))
    for name, path in (
        ("fr3_env_ro", FR3_ENV),
        ("run_collect_phase_ro", CA / "run_collect_phase.py"),
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("fr3_env_ro", FR3_ENV)
    env_mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "fr3_env_ro", env_mod)
    spec.loader.exec_module(env_mod)
    clock = [0.0]
    monkeypatch.setattr(
        env_mod,
        "time",
        types.SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]),
    )
    spec = importlib.util.spec_from_file_location(
        "run_collect_phase_ro", CA / "run_collect_phase.py"
    )
    entry = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "run_collect_phase_ro", entry)
    spec.loader.exec_module(entry)
    return entry, env_mod, clock


def _env(env_mod, cls=None):
    """A RobotEnv with the gripper fields of its __init__ defaults and recording senders (no server)."""
    env = object.__new__(cls or env_mod.RobotEnv)
    env.gripper_sleep = 0.75
    env.gripper_open_threshold = 0.9
    env.gripper_closed_threshold = 0.18
    env.gripper_move_deadband = 0.08
    env._last_gripper_act = 0.0
    env._last_gripper_cmd = 1.0
    env.sent = []
    env._close_gripper = lambda: env.sent.append(("close", round(clock_now[0], 2)))
    env._open_gripper = lambda: env.sent.append(("open", round(clock_now[0], 2)))
    return env


clock_now = [0.0]


def _drive(env, clock, plan):
    """plan: [(t, command, aperture)]; returns the _update_gripper results."""
    out = []
    for t, cmd, ap in plan:
        clock[0] = clock_now[0] = t
        out.append(env._update_gripper(cmd, ap))
    return out


def _hold_then_release():
    """Close at 1.0 s (fingers 1.0 -> 0.55 on a ball, then still), open commanded at 3.0 s."""
    plan = []
    aps = {1.0: 1.0, 1.1: 1.0, 1.2: 0.95, 1.3: 0.80, 1.4: 0.66, 1.5: 0.56, 1.6: 0.55}
    t = 1.0
    while t < 3.0 - 1e-9:
        plan.append((round(t, 1), 0.0, aps.get(round(t, 1), 0.55)))
        t += 0.1
    plan.append((3.0, 1.0, 0.55))
    return plan


def test_the_users_client_re_sends_and_delays_the_release(loaded):
    """The motivating behaviour, on the unwrapped method: re-sent closes keep restarting the throttle."""
    _entry, env_mod, clock = loaded
    env = _env(env_mod)
    _drive(env, clock, _hold_then_release())
    closes = [s for s in env.sent if s[0] == "close"]
    assert len(closes) >= 2, env.sent
    assert ("open", 3.0) not in env.sent, (
        "the release went out at once; the premise of the wrapper is gone"
    )


def test_no_re_send_while_the_fingers_rest_on_an_object_and_the_release_goes_at_once(
    loaded,
):
    entry, env_mod, clock = loaded
    cls = type("RobotEnvCopy", (env_mod.RobotEnv,), {})
    assert entry.install_gripper_no_resend(cls) is True
    assert entry.install_gripper_no_resend(cls) is False, "wrapped once"
    env = _env(env_mod, cls)
    _drive(env, clock, _hold_then_release())
    assert env.sent == [("close", 1.0), ("open", 3.0)]
    assert env._last_gripper_cmd == 1.0


def test_a_close_whose_fingers_never_moved_is_re_sent(loaded):
    entry, env_mod, clock = loaded
    cls = type("RobotEnvCopy", (env_mod.RobotEnv,), {})
    entry.install_gripper_no_resend(cls)
    env = _env(env_mod, cls)
    plan = [
        (round(1.0 + 0.1 * i, 1), 0.0, 1.0) for i in range(12)
    ]  # the command was lost: still fully open
    _drive(env, clock, plan)
    assert [s[0] for s in env.sent] == ["close", "close"], env.sent
    assert env.sent[1][1] >= 1.75 - 1e-9


def test_after_a_reset_opened_the_gripper_a_close_goes_at_once(loaded):
    """fr3_env.reset opens the gripper directly and latches open; the wrapper's memory of the old close must not skip."""
    entry, env_mod, clock = loaded
    cls = type("RobotEnvCopy", (env_mod.RobotEnv,), {})
    entry.install_gripper_no_resend(cls)
    env = _env(env_mod, cls)
    _drive(env, clock, _hold_then_release()[:12])  # closed on the ball, still at 0.55
    env._last_gripper_cmd = (
        1.0  # reset(): _open_gripper() then the latch, outside _update_gripper
    )
    _drive(env, clock, [(5.0, 1.0, 1.0), (5.1, 0.0, 1.0)])
    assert env.sent[-1] == ("close", 5.1), env.sent


def test_a_close_at_full_open_is_never_skipped(loaded):
    entry, env_mod, clock = loaded
    cls = type("RobotEnvCopy", (env_mod.RobotEnv,), {})
    entry.install_gripper_no_resend(cls)
    env = _env(env_mod, cls)
    _drive(env, clock, _hold_then_release()[:12])
    env._last_gripper_cmd = 0.0  # a stale latch while the fingers are back at full open
    _drive(env, clock, [(6.0, 0.0, 1.0), (6.1, 0.0, 1.0)])
    assert env.sent[-1][0] == "close" and env.sent[-1][1] >= 6.0, env.sent


def _wrapped(entry, env_mod):
    cls = type("RobotEnvCopy", (env_mod.RobotEnv,), {})
    entry.install_gripper_no_resend(cls)
    return _env(env_mod, cls)


def test_moved_counts_only_toward_the_command(loaded):
    """Review 2026-11-03: an open blocked at 0.70, a close parked behind it, the re-open dropped by the server, then the
    stale close grips at 0.55. The fingers moved, but against the latched open: the open must be re-sent."""
    entry, env_mod, clock = loaded
    plan = [(1.0, 0.0, 1.0), (1.1, 0.0, 0.9), (1.2, 0.0, 0.7), (1.3, 0.0, 0.56)]
    plan += [(round(1.4 + 0.1 * i, 1), 0.0, 0.55) for i in range(6)]
    plan += [(2.0, 1.0, 0.55), (2.1, 1.0, 0.55), (2.2, 1.0, 0.55), (2.3, 1.0, 0.62)]
    plan += [(2.4, 1.0, 0.70), (2.5, 1.0, 0.70), (2.6, 1.0, 0.70)]
    plan += [(2.8, 0.0, 0.70)] + [
        (round(2.9 + 0.1 * i, 1), 0.0, 0.70) for i in range(6)
    ]
    plan += [(3.6, 1.0, 0.70), (3.7, 1.0, 0.65), (3.8, 1.0, 0.56)]
    plan += [(round(3.9 + 0.1 * i, 1), 1.0, 0.55) for i in range(25)]
    env = _wrapped(entry, env_mod)
    _drive(env, clock, plan)
    assert [s for s in env.sent if s[0] == "open" and s[1] > 3.6], env.sent


def test_a_close_that_never_moved_from_mid_stroke_is_re_sent(loaded):
    """The moved guard alone: the fingers sit at 0.70 (between the stops) and never answer the close."""
    entry, env_mod, clock = loaded
    env = _wrapped(entry, env_mod)
    _drive(env, clock, [(round(1.0 + 0.1 * i, 1), 0.0, 0.70) for i in range(12)])
    assert [s[0] for s in env.sent] == ["close", "close"], env.sent


def test_fingers_still_moving_are_not_treated_as_settled(loaded):
    """The still guard alone: a slow close that keeps moving past the throttle is re-sent as before."""
    entry, env_mod, clock = loaded
    env = _wrapped(entry, env_mod)
    plan = [
        (round(1.0 + 0.1 * i, 1), 0.0, round(1.0 - 0.04 * i, 2)) for i in range(12)
    ]  # 1.0 -> 0.56, never still
    _drive(env, clock, plan)
    assert [s[0] for s in env.sent] == ["close", "close"], env.sent


def test_a_close_at_the_open_stop_is_re_sent_even_when_still(loaded):
    """The between guard alone: answered once (moved 0.08 toward closed), then back at the open stop and still."""
    entry, env_mod, clock = loaded
    env = _wrapped(entry, env_mod)
    plan = [(1.0, 0.0, 0.98), (1.1, 0.0, 0.90)]
    plan += [
        (round(1.2 + 0.1 * i, 1), 0.0, 0.93) for i in range(9)
    ]  # back at the open stop (>= 0.9), still
    _drive(env, clock, plan)
    assert [s[0] for s in env.sent] == ["close", "close"], env.sent


def test_a_latch_changed_elsewhere_forgets_the_wrappers_issue(loaded):
    """The latch-clear guard alone: a close issued at 0.55, reset() then latches open outside the wrapper inside the
    throttle while the fingers rest at 0.60; the policy's open must still go out once the throttle allows it."""
    entry, env_mod, clock = loaded
    env = _wrapped(entry, env_mod)
    _drive(env, clock, [(1.0, 0.0, 0.55), (1.1, 0.0, 0.60), (1.2, 0.0, 0.60)])
    env._last_gripper_cmd = (
        1.0  # reset(): open sent directly and latched, outside _update_gripper
    )
    _drive(env, clock, [(round(1.3 + 0.1 * i, 1), 1.0, 0.60) for i in range(13)])
    assert [s for s in env.sent if s[0] == "open"], env.sent


def test_an_open_after_a_reset_with_the_fingers_mid_stroke_goes_out(loaded):
    """A reset latched open outside the wrapper while the fingers still rest at 0.55: the policy's open must go out."""
    entry, env_mod, clock = loaded
    env = _wrapped(entry, env_mod)
    _drive(env, clock, _hold_then_release()[:12])  # closed on the ball, still at 0.55
    env._last_gripper_cmd = (
        1.0  # reset(): open sent directly and latched, outside _update_gripper
    )
    _drive(env, clock, [(round(5.0 + 0.1 * i, 1), 1.0, 0.55) for i in range(10)])
    assert [s for s in env.sent if s[0] == "open"], env.sent


def test_the_entry_wraps_only_when_not_golden():
    """main() wraps the class only for an accelerated run (golden must stay the user's client, bit for bit)."""
    src = (CA / "run_collect_phase.py").read_text()
    assert "    if not params.golden:\n        _wrap_client_gripper()" in src
    assert np.isclose(0.02, 0.02)
