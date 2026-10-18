"""Failure texts reach the journal and the terminal (L2-REAL-ERRLOG).

Fake env and fake clock; no socket, robot or GPU. Thresholds are the reviewed ones.
"""

from fr3_real_support import notes, run_dir
from test_fr3_bridge import no_network, rig  # noqa: F401

from levi.automatic import fr3_run as R


def test_policy_error_text_reaches_the_journal_and_the_terminal(
    tmp_path, monkeypatch, capsys
):
    r = rig(tmp_path, monkeypatch)
    client = r.modules[0]
    first = client._query_policy
    calls = []

    def query(c, obs, state, prompt):
        calls.append(1)
        if len(calls) == 1:  # the warm-up passes
            return first(c, obs, state, prompt)
        raise RuntimeError("cold-server: websocket closed (1011)")

    client._query_policy = query
    lifecycle = []
    hold, quiesce = R.bridge.Robot.hold, R.bridge.Policy.quiesce

    def held(self):
        lifecycle.append("hold")
        return hold(self)

    def quiet(self, **kw):
        lifecycle.append("quiesce")
        return quiesce(self, **kw)

    monkeypatch.setattr(R.bridge.Robot, "hold", held)
    monkeypatch.setattr(R.bridge.Policy, "quiesce", quiet)
    assert (
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "FAULT_LOCKED"
    )
    assert "websocket closed (1011)" in notes(r)["policy_unavailable"]
    assert len(calls) == 2  # warm-up, then one failing execution; no retries
    assert "hold" in lifecycle and "quiesce" in lifecycle
    assert lifecycle.index("hold") < lifecycle.index("quiesce")
    assert not any(c[0] == "route" for c in r.calls)  # zero policy/home motion
    err = capsys.readouterr().err
    assert "FAULT_LOCKED" in err and "policy_error" in err
    assert "websocket closed (1011)" in err
    assert str(run_dir(r) / "state_journal.jsonl") in err


def test_gripper_reply_text_reaches_the_journal_and_the_terminal(
    tmp_path, monkeypatch, capsys
):
    r = rig(tmp_path, monkeypatch)
    env_class = r.modules[2].RobotEnv
    step = env_class.step
    reply = "open gripper action server is not available"

    def with_gripper(self, action):
        value = step(self, action)
        value["gripper_response"] = {
            "command": "open_gripper",
            "response": self._post("open_gripper"),
        }
        return value

    post = env_class._post

    def bad_post(self, name, payload=None):
        if name == "open_gripper":
            r.calls.append(("route", name))
            return reply
        return post(self, name, payload)

    monkeypatch.setattr(env_class, "_post", bad_post)
    monkeypatch.setattr(env_class, "step", with_gripper)
    assert (
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "FAULT_LOCKED"
    )
    assert reply in notes(r)["motion_unacknowledged"]
    err = capsys.readouterr().err
    assert "FAULT_LOCKED" in err and reply in err


def test_operator_stop_during_first_policy_error_keeps_existing_human_path(
    tmp_path, monkeypatch
):
    r = rig(tmp_path, monkeypatch)
    live = {}
    create = R.Orchestrator.create

    def created(*args, **kwargs):
        orch = create(*args, **kwargs)
        live["orch"] = orch
        return orch

    monkeypatch.setattr(R.Orchestrator, "create", created)
    query = r.modules[0]._query_policy
    calls = []

    def stopped_query(*args):
        calls.append(1)
        if len(calls) == 1:
            return query(*args)
        live["orch"].stop("operator-during-error")
        raise RuntimeError("closed while operator stopped")

    r.modules[0]._query_policy = stopped_query
    assert R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "WAIT_HUMAN"
    assert not any(c[0] == "route" for c in r.calls)
    assert "closed while operator stopped" in notes(r)["policy_unavailable"]
