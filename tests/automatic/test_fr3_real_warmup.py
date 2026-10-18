"""A bounded warm-up before the first episode (L2-REAL-WARMUP).

Fake env and fake clock; no socket, robot or GPU. Thresholds are the reviewed ones.
"""

import json
import threading
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest
from aeri_harness import fake
from fr3_real_support import notes, run_dir
from test_fr3_bridge import no_network, rig  # noqa: F401

from levi.automatic import fr3_bridge as B
from levi.automatic import fr3_run as R


def policy_with(query, *, raw=None):
    client = NS(
        _query_policy=query,
        _check_server=lambda *a: None,
        _check_prompt=lambda *a: None,
    )
    robot = NS(raw=raw)
    return B.Policy(
        {
            "forward": NS(
                get_server_metadata=lambda: {
                    "config": "cfg",
                    "checkpoint_dir": "/models/forward",
                }
            )
        },
        {"forward": {"config": "cfg", "checkpoint_dir": "/models/forward"}},
        {"forward": "stack"},
        robot,
        client,
        NS(),
        fake.FakeClock(),
    )


RAW = {"robot_state": {"cartesian_position": np.zeros(7)}}


def test_warmup_waits_out_a_cold_first_inference_and_discards_the_actions():
    seen = []

    def query(client, raw, state, prompt):
        seen.append((raw, prompt))
        if len(seen) == 1:
            time.sleep(0.4)  # JIT
        return np.ones((4, 7))

    policy = policy_with(query)
    out = policy.warmup("forward", raw=RAW, timeout_s=5.0)
    assert out["ok"] is True and out["duration_s"] >= 0.4
    assert "actions" not in out
    assert seen[0][0] is RAW and seen[0][1] == "stack"
    # the slot is free again and the server is warm now
    assert policy.acquire("forward", policy_epoch=1)["role"] == "forward"


def test_warmup_timeout_is_a_refusal_not_a_hang():
    release = threading.Event()
    policy = policy_with(lambda *a: release.wait(5) and np.ones((1, 7)))
    began = time.monotonic()
    out = policy.warmup("forward", raw=RAW, timeout_s=0.2)
    release.set()
    assert out["ok"] is False and "timed out" in out["detail"]
    assert time.monotonic() - began < 2


def test_warmup_failure_carries_the_exception_text():
    def query(*a):
        raise RuntimeError("model not loaded")

    out = policy_with(query).warmup("forward", raw=RAW, timeout_s=2.0)
    assert out["ok"] is False and "model not loaded" in out["detail"]


def test_warmup_refuses_while_another_query_is_running():
    release = threading.Event()
    policy = policy_with(lambda *a: release.wait(5) and np.ones((1, 7)))
    policy.warmup("forward", raw=RAW, timeout_s=0.1)
    out = policy.warmup("forward", raw=RAW, timeout_s=0.1)
    release.set()
    assert out["ok"] is False and "query_still_running" in out["detail"]


def test_cold_server_is_warmed_before_the_first_episode_and_no_motion_is_sent(
    tmp_path, monkeypatch
):
    r = rig(tmp_path, monkeypatch)
    r.binding["policy_warmup_timeout_s"] = 10
    client = r.modules[0]
    first = client._query_policy
    motion = ("pose", "clearerr", "open_gripper", "close_gripper")
    count = []

    def sent():
        return sum(1 for c in r.calls if c[0] == "route" and c[1] in motion)

    def query(c, obs, state, prompt):
        count.append(sent())
        if len(count) == 1:
            time.sleep(0.8)  # longer than the 500 ms chunk deadline
        return first(c, obs, state, prompt)

    client._query_policy = query
    assert R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "COMPLETED"
    assert count[0] == 0  # the warm-up ran with no motion command sent
    assert "policy_warmup" in notes(r)


def test_warmup_timeout_refuses_to_start_without_any_motion(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    r.binding["policy_warmup_timeout_s"] = 0.3
    release = threading.Event()
    r.modules[0]._query_policy = lambda *a: release.wait(5) and np.ones((1, 7))
    try:
        with pytest.raises(ValueError, match="warm-up.*timed out"):
            R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    finally:
        release.set()
    routes = [c[1] for c in r.calls if c[0] == "route"]
    assert not {"pose", "clearerr", "open_gripper", "close_gripper"} & set(routes)
    assert not run_dir(r).exists()
    assert r.calls[-1][0] == "closed"


def test_timed_out_warmup_retains_the_query_slot_until_actual_completion():
    release = threading.Event()
    policy = policy_with(lambda *a: release.wait(5) and np.ones((1, 7)), raw=RAW)
    try:
        assert policy.warmup("forward", raw=RAW, timeout_s=0.05)["ok"] is False
        assert (
            policy.acquire("forward", policy_epoch=1)["code"] == "query_still_running"
        )
        assert policy.quiesce(policy_epoch=1)["ok"] is False
        assert policy.warmup("forward", raw=RAW, timeout_s=0.05)["ok"] is False
    finally:
        release.set()
        if policy.ticket is not None:
            assert policy.ticket.done.wait(1)
    assert policy.acquire("forward", policy_epoch=2)["role"] == "forward"


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), "30"])
def test_binding_rejects_invalid_warmup_timeout_without_connections(
    tmp_path, monkeypatch, value
):
    r = rig(tmp_path, monkeypatch)
    r.binding["policy_warmup_timeout_s"] = value
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    with pytest.raises((ValueError, TypeError), match="positive finite|not allowed"):
        R.load_binding(path, r.job)
    assert not r.calls


def test_binding_accepts_explicit_warmup_budget(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    r.binding["policy_warmup_timeout_s"] = 10.0
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    binding, _ = R.load_binding(path, r.job)
    assert binding["policy_warmup_timeout_s"] == 10.0 and not r.calls


def test_start_pose_is_rechecked_after_warmup_before_any_motion(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    env_class = r.modules[2].RobotEnv
    original_init = env_class.__init__
    current = {}

    def init(self, **kwargs):
        original_init(self, **kwargs)
        current["env"] = self

    query = r.modules[0]._query_policy

    def changed_pose(*args):
        actions = query(*args)
        current["env"].pose[0] += 0.05
        return actions

    monkeypatch.setattr(env_class, "__init__", init)
    r.modules[0]._query_policy = changed_pose
    with pytest.raises(ValueError, match="start pose refused"):
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules)
    assert not any(c[0] == "route" for c in r.calls)
    assert not run_dir(r).exists()
