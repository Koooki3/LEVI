"""Pilot protocol and authority tests. No provider, GPU or checkpoint execution."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from levi.agent import grants
from levi.agent.capabilities import invoke
from levi.agent.pilot_contracts import GrantRequest
from levi.agent.runtime import Workbench
from levi.agent.schema import TaskContext
from levi.agent.security import Principal


@pytest.fixture(autouse=True)
def no_inference(monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "jax", "sam3", "pydantic_ai", "tensorflow"}:
            raise AssertionError("Real inference/accelerator imports forbidden")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


@pytest.fixture
def prepared(client, dataset):
    from levi import catalog, service

    entry = catalog.register(str(dataset))
    wb = Workbench(service.STATE)
    context = TaskContext(
        repo_id=entry["id"],
        episodes=[0],
        instruction="Inspect only",
        provider="external",
        pilot_runtime="codex",
    )
    run = invoke(wb, Principal("human", human=True), "runs.plan", context.model_dump())
    return wb, run


def test_grant_revocation_scope_and_budget(prepared):
    wb, run = prepared
    item = grants.create(
        wb.store,
        GrantRequest(
            client="codex",
            datasets=[run["context"]["repo_id"]],
            run_id=run["id"],
            max_tool_calls=2,
        ),
    )
    who = grants.authenticate(wb.store, item["token"])
    assert "token" not in json.dumps(wb.store.list("grants"))
    invoke(wb, who, "capabilities.list", {})
    with pytest.raises(PermissionError):
        invoke(wb, who, "plans.approve", {"run_id": run["id"], "revision": 1})
    invoke(wb, who, "runs.get", {"run_id": run["id"]})
    invoke(wb, who, "runs.get", {"run_id": run["id"]})
    with pytest.raises(PermissionError, match="budget"):
        invoke(wb, who, "runs.get", {"run_id": run["id"]})
    wb.store.mutate("grants", item["id"], lambda g: g.update(enabled=False))
    with pytest.raises(PermissionError):
        grants.authenticate(wb.store, item["token"])


def test_scoped_agent_cannot_access_control_api(client, prepared):
    wb, run = prepared
    item = grants.create(
        wb.store, GrantRequest(client="claude", datasets=[run["context"]["repo_id"]])
    )
    headers = {"Authorization": "Bearer " + item["token"]}
    for path in ["grants", "pilot/sessions", "runtimes"]:
        assert (
            client.get("/api/levi/agent/v1/" + path, headers=headers).status_code == 403
        )
    assert (
        client.post(
            "/api/levi/agent/v1/grants",
            headers=headers,
            json={"client": "codex", "datasets": ["local/fixture"]},
        ).status_code
        == 403
    )


def test_action_pair_and_artifact_manifest(prepared):
    wb, run = prepared
    who = Principal("human", human=True)
    invoke(wb, who, "plans.approve", {"run_id": run["id"], "revision": 1})
    invoke(wb, who, "runs.prepare", {"run_id": run["id"]})
    events = wb.store.events(run["id"])
    starts = {e["call_id"] for e in events if e["type"] == "action.started"}
    ends = {e["call_id"] for e in events if e["type"] == "action.completed"}
    assert starts == ends
    from levi.agent.tracking import manifest

    result = manifest(wb, run["id"])
    assert result["dataset"] == run["context"]["repo_id"]
    assert (wb.store.run_dir(run["id"]) / "result.json").exists()
    assert all(not Path(a["path"]).is_absolute() for a in result["artifacts"])


def test_project_configuration_preserves_other_entries(tmp_path):
    from levi.agent.control import configuration

    file = tmp_path / ".mcp.json"
    file.write_text('{"mcpServers":{"other":{"command":"existing"}}}')
    _, before, after = configuration("claude", tmp_path, tmp_path / "credential")
    assert json.loads(after)["mcpServers"]["other"]["command"] == "existing"
    assert file.read_text() == before
    directory = tmp_path / ".codex"
    directory.mkdir()
    (directory / "config.toml").write_text('model = "keep-me"\n')
    _, _, after = configuration("codex", tmp_path, tmp_path / "credential")
    import tomllib

    assert tomllib.loads(after)["model"] == "keep-me"
    assert "credential" in after and "Bearer" not in after


def test_acp_interleaved_events_permissions_and_failure(tmp_path):
    from levi.agent.pilot import ACP

    script = tmp_path / "fixture.py"
    script.write_text("""import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize':
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'fixture'}}}}),flush=True)
  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':1}}),flush=True)
 elif m.get('method')=='permission':
  print(json.dumps({'jsonrpc':'2.0','id':99,'method':'session/request_permission','params':{}}),flush=True)
  answer=json.loads(sys.stdin.readline())
  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':answer['result']}),flush=True)
 else:
  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'error':{'message':'fixture failure'}}),flush=True)
""")
    seen = []
    acp = ACP(
        [sys.executable, str(script)],
        tmp_path,
        dict(os.environ),
        lambda *v: seen.append(v),
        lambda _: {"outcome": {"outcome": "cancelled"}},
    )
    try:
        assert acp.call("initialize", {})["protocolVersion"] == 1
        assert seen
        assert acp.call("permission", {})["outcome"]["outcome"] == "cancelled"
        with pytest.raises(ValueError, match="fixture failure"):
            acp.call("fail", {})
    finally:
        acp.close()


def test_unapproved_pilot_does_not_spawn(prepared, monkeypatch):
    from levi.agent.pilot import start
    from levi.agent.pilot_contracts import PilotStart

    wb, run = prepared
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **k: pytest.fail("No process before approval")
    )
    with pytest.raises(ValueError):
        start(wb, PilotStart(run_id=run["id"], runtime="codex"))


def test_core_shared_no_ui(tmp_path, monkeypatch):
    # Bind only local sockets. Use a short path to satisfy AF_UNIX's system limit.
    from levi import paths
    from levi.agent import core

    root = Path("/tmp") / ("levi-pilot-" + str(os.getpid()))
    root.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("LEVI_WORKSPACE", str(root))
    monkeypatch.setenv("LEVI_SAM3_CHECKPOINT_DIR", str(root / "checkpoints/sam3"))
    monkeypatch.setattr(paths, "STATE", root / "outputs/LEVI/workbench")
    monkeypatch.delenv("LEVI_PILOT_CHILD", raising=False)
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    try:
        try:
            first = core.ensure(port)
        except RuntimeError:
            print((core.directory() / "core.log").read_text())
            raise
        assert core.ensure(port)["instance"] == first["instance"]
        assert core.request("/api/levi/agent/v1/runs", human=True) == []
        assert (core.directory() / "human.key").stat().st_mode & 0o077 == 0
        core.stop()
        for _ in range(50):
            if core.status() is None:
                break
            time.sleep(0.1)
        assert core.status() is None
    finally:
        if core.status():
            core.stop()
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_managed_session_reuses_harness_and_revokes_on_pause(prepared, monkeypatch):
    from levi.agent import pilot
    from levi.agent.pilot_contracts import PilotStart

    wb, run = prepared
    invoke(
        wb,
        Principal("human", human=True),
        "plans.approve",
        {"run_id": run["id"], "revision": 1},
    )
    seen = []

    class FixtureACP:
        def __init__(self, argv, cwd, env, notify, permission):
            assert env["LEVI_PILOT_CHILD"] == "1"
            assert "LEVI_UI_TOKEN" not in env
            self.notify, self.permission = notify, permission

        def call(self, method, params, **kwargs):
            seen.append((method, params))
            if method == "initialize":
                return {"agentCapabilities": {"promptCapabilities": {"image": True}}}
            if method == "session/new":
                return {"sessionId": "fixture-session"}
            if method == "session/prompt":
                assert "Never approve" in params["prompt"][0]["text"]
                assert (
                    self.permission({"toolCall": {"kind": "execute"}})["outcome"][
                        "outcome"
                    ]
                    == "cancelled"
                )
                self.notify(
                    "session/update",
                    {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Fixture draft ready"},
                        }
                    },
                )
                return {"stopReason": "end_turn"}

        def send(self, value):
            seen.append(("notify", value))

        def close(self):
            pass

    monkeypatch.setattr(pilot, "ACP", FixtureACP)
    monkeypatch.setattr(pilot, "command", lambda _: Path(sys.executable))
    monkeypatch.setattr(pilot.shutil, "which", lambda _: sys.executable)

    class Result:
        stdout = "v22.0.0"

    monkeypatch.setattr(pilot.subprocess, "run", lambda *a, **k: Result())
    value = pilot.start(wb, PilotStart(run_id=run["id"], runtime="codex"))
    try:
        for _ in range(100):
            current = wb.store.get("pilot_sessions", value["id"])
            if current.get("stop_reason") == "end_turn":
                break
            time.sleep(0.02)
        assert current["state"] == "idle"
        assert wb.store.get("runs", run["id"])["pilot_turns"] == 1
        assert any(e["type"] == "pilot.activity" for e in wb.store.events(run["id"]))
        assert (
            wb.store.run_dir(run["id"]) / f"runtime-events-{value['id']}.jsonl"
        ).exists()
        pilot.control(wb, value["id"], "pause")
        for _ in range(100):
            if (
                wb.store.get("pilot_sessions", value["id"])["connection"]
                == "disconnected"
            ):
                break
            time.sleep(0.02)
        assert not wb.store.get("grants", value["grant_id"])["enabled"]
    finally:
        pilot.shutdown()


def _idle_runtime(monkeypatch, closed):
    from levi.agent import pilot

    class FixtureACP:
        def __init__(self, argv, cwd, env, notify, permission):
            pass

        def call(self, method, params, **kwargs):
            if method == "initialize":
                return {"agentCapabilities": {"promptCapabilities": {"image": True}}}
            if method == "session/new":
                return {"sessionId": "fixture-session"}
            return {"stopReason": "end_turn"}

        def send(self, value):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr(pilot, "ACP", FixtureACP)
    monkeypatch.setattr(pilot, "command", lambda _: Path(sys.executable))
    monkeypatch.setattr(pilot.shutil, "which", lambda _: sys.executable)

    class Result:
        stdout = "v22.0.0"

    monkeypatch.setattr(pilot.subprocess, "run", lambda *a, **k: Result())


def _wait_disconnected(wb, id, seconds):
    for _ in range(int(seconds / 0.05)):
        value = wb.store.get("pilot_sessions", id)
        if value["connection"] == "disconnected":
            return value
        time.sleep(0.05)
    raise AssertionError(f"session still connected: {value}")


def test_an_idle_session_releases_its_runtime(prepared, monkeypatch):
    from levi.agent import pilot
    from levi.agent.pilot_contracts import PilotStart

    wb, run = prepared
    invoke(
        wb,
        Principal("human", human=True),
        "plans.approve",
        {"run_id": run["id"], "revision": 1},
    )
    closed = []
    _idle_runtime(monkeypatch, closed)
    monkeypatch.setenv("LEVI_PILOT_IDLE_SECONDS", "0.5")
    value = pilot.start(wb, PilotStart(run_id=run["id"], runtime="codex"))
    try:
        session = _wait_disconnected(wb, value["id"], 10)
        assert session["state"] == "idle_closed" and closed
        assert not wb.store.get("grants", value["grant_id"])["enabled"]
    finally:
        pilot.shutdown()


def test_a_finished_run_releases_its_runtime(prepared, monkeypatch):
    from levi.agent import pilot
    from levi.agent.pilot_contracts import PilotStart

    wb, run = prepared
    invoke(
        wb,
        Principal("human", human=True),
        "plans.approve",
        {"run_id": run["id"], "revision": 1},
    )
    closed = []
    _idle_runtime(monkeypatch, closed)
    value = pilot.start(wb, PilotStart(run_id=run["id"], runtime="codex"))
    try:
        for _ in range(100):
            if wb.store.get("pilot_sessions", value["id"]).get("stop_reason"):
                break
            time.sleep(0.02)
        wb.store.mutate("runs", run["id"], lambda r: r.update(status="succeeded"))
        session = _wait_disconnected(wb, value["id"], 10)
        assert session["state"] == "finished" and closed
    finally:
        pilot.shutdown()


def test_runtime_binding_change_requires_reapproval(prepared, monkeypatch):
    wb, run = prepared
    from levi.agent import runtime_binding

    invoke(
        wb,
        Principal("human", human=True),
        "plans.approve",
        {"run_id": run["id"], "revision": 1},
    )
    monkeypatch.setattr(runtime_binding, "binding", lambda _: {"runtime": "different"})
    from levi.agent.planning import require

    with pytest.raises(ValueError, match="account changed"):
        require(wb, wb.store.get("runs", run["id"]))


def test_scoped_artifacts_reject_other_run(prepared):
    wb, run = prepared
    item = grants.create(
        wb.store,
        GrantRequest(
            client="codex", datasets=[run["context"]["repo_id"]], run_id=run["id"]
        ),
    )
    who = grants.authenticate(wb.store, item["token"])
    with pytest.raises(PermissionError):
        grants.require_run(wb.store, who, "different-run")


def test_human_creates_catalog_grant(client, prepared):
    _wb, run = prepared
    response = client.post(
        "/api/levi/agent/v1/grants",
        json={"client": "codex", "datasets": [run["context"]["repo_id"]]},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    headers = {"Authorization": "Bearer " + token}
    assert (
        client.get("/api/levi/agent/v1/capabilities", headers=headers).status_code
        == 200
    )
    result = client.get("/api/levi/agent/v1/grants")
    assert token not in result.text


def test_a_local_connection_lasts_until_it_is_disconnected(client, monkeypatch):
    """No clock and no call cap unless one was asked for.

    A connection the operator opened on their own machine should not stop
    working mid-task because a default expired.
    """
    import time

    from levi import service
    from levi.agent import grants
    from levi.agent.pilot_contracts import GrantRequest
    from levi.agent.store import Store

    store = Store(service.STATE)
    unlimited = grants.create(
        store, GrantRequest(client="claude", datasets=["local/fixture"])
    )
    record = store.get("grants", unlimited["id"])
    assert record["expires"] is None
    assert record["max_tool_calls"] is None

    who = grants.authenticate(store, unlimited["token"])
    assert who is not None
    for _ in range(500):  # far past the old 300-call cap
        grants.check_call(store, who, None)
    assert store.get("grants", unlimited["id"])["calls"] == 500
    # Each call records when the client last spoke, so the panel can show a
    # live connection instead of guessing.
    assert (
        store.get("grants", unlimited["id"])["last_seen"] >= record["at"]
        if "at" in record
        else True
    )

    bounded = grants.create(
        store,
        GrantRequest(
            client="codex", datasets=["local/fixture"], hours=1, max_tool_calls=2
        ),
    )
    limited = grants.authenticate(store, bounded["token"])
    assert store.get("grants", bounded["id"])["expires"] > time.time()
    grants.check_call(store, limited, None)
    grants.check_call(store, limited, None)
    with pytest.raises(PermissionError, match="budget"):
        grants.check_call(store, limited, None)


def test_disconnecting_still_stops_an_unlimited_connection(client):
    from levi import service
    from levi.agent import grants
    from levi.agent.pilot_contracts import GrantRequest
    from levi.agent.store import Store

    store = Store(service.STATE)
    grant = grants.create(
        store, GrantRequest(client="claude", datasets=["local/fixture"])
    )
    who = grants.authenticate(store, grant["token"])
    grants.check_call(store, who, None)  # works while connected
    store.mutate("grants", grant["id"], lambda value: value.update(enabled=False))
    with pytest.raises(PermissionError, match="disconnected"):
        grants.check_call(store, who, None)
    # The credential itself stops authenticating too, so a new session cannot
    # revive a connection the operator closed.
    with pytest.raises(PermissionError, match="disconnected"):
        grants.authenticate(store, grant["token"])
