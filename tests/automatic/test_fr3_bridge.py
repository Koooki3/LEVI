"""The actual bridge on fake imported modules: no socket or robot SDK is used."""

import hashlib
import json
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from aeri_harness import fake

from levi.automatic import fr3_bridge as B
from levi.automatic import fr3_run as R
from levi.automatic import scene_assessment as sa
from levi.automatic import state_machine as sm
from levi.automatic.adapters import events as ev
from levi.automatic.journal import Journal
from levi.automatic.orchestrator import RunConfig
from levi.automatic.termination import TerminationConfig

Rotation = NS(from_euler=lambda *a: NS(as_quat=lambda: np.array([0.0, 0.0, 0.0, 1.0])))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("real bridge test tried network I/O")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def rig(tmp_path, monkeypatch, mode="human_assisted", *, faults=(), scenes=()):
    contract = sa.InitialStateContract(
        contract_id="initial",
        contract_version="1",
        required=("object_at_source",),
        min_evidence_refs=1,
        status="confirmed",
    )
    term = TerminationConfig(min_steps=1, settle_steps=0, cooldown_steps=0)
    cfg = RunConfig(
        run_id="real-test",
        plan_sha256="0" * 64,
        episodes=1,
        reset_strategy=mode,
        initial_state=contract,
        termination=term,
        forward_max_steps=6,
        reset_max_steps=6,
        control_period_ns=1_000_000,
    )
    deployments = {
        role: {
            "config": "cfg",
            "checkpoint_id": role,
            "checkpoint_dir": "/models/" + role,
        }
        for role in (("forward",) if mode == "human_assisted" else ("forward", "reset"))
    }
    plan = {"deployments": deployments, "fixture": "actual-bridge"}
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    cfg = replace(cfg, plan_sha256=digest)
    job = {
        "config": cfg,
        "contract": contract,
        "plan": plan,
        "plan_sha256": digest,
        "mode": "assisted",
        "texts": {"forward": "stack", "reset": "reset"},
        "group": "group",
        "rollout_root": str(tmp_path / "rollouts"),
    }
    provider_root = tmp_path / "providers"
    provider_root.mkdir()
    (provider_root / "READY.json").write_text(
        json.dumps(
            {
                "schema": "levi.aeri.provider_ready.v1",
                "contract_id": "initial",
                "contract_version": "1",
                "capabilities": ["scene", "events", "reset_judgement"],
                "updated_wall_ns": time.time_ns(),
            }
        )
    )
    binding = {
        "schema": R.SCHEMA,
        "openpi_root": "/fake/openpi",
        "robot_url": "http://127.0.0.1:5000/",
        "health_file": "/fake/health.json",
        "cameras": {"side": "123", "wrist": "456"},
        "safety": {
            "min_z": 0.05,
            "max_xy": 1.0,
            "max_step_m": 0.05,
            "max_step_rad": 0.2,
            "home_pose": [0.4, 0, 0.2, 0, 0, 0],
            "home_duration_s": 0.003,
            "home_tolerance_mm": 5,
        },
        "policies": {
            role: {"host": "127.0.0.1", "port": 8000 + i}
            for i, role in enumerate(deployments)
        },
        "providers": {
            "online_url": "http://127.0.0.1:7882",
            "file_root": str(provider_root),
        },
    }
    calls = []

    class Env:
        def __init__(self, **kw):
            assert kw["do_reset"] is False
            calls.append(("env", kw))
            self.pose = np.array([0.4, 0, 0.2, 0, 0, 0, 1.0])
            self.count = 0
            self.closed = False
            self.sent = 0
            self.guarded = False

        def _post(self, route, payload=None):
            calls.append(("route", route))
            return {"ok": True}

        def get_observation(self):
            self.count += 1
            image = np.full((4, 4, 3), self.count % 255, dtype=np.uint8)
            return {
                "image": {"view1": image, "hand": image},
                "robot_state": {
                    "cartesian_position": self.pose.copy(),
                    "joint_positions": np.zeros(7),
                    "gripper_position": 1.0,
                },
            }

        def step(self, action):
            assert self.guarded
            self._post("clearerr")
            self._post("pose")
            self.sent += 1
            if "no_ack" in faults:
                return None
            self.pose = np.asarray(action).copy()
            return {"ok": True}

        def close(self):
            self.closed = True
            calls.append(("closed", self.sent))

    class Guard:
        tripped = None

        def __init__(self, *a):
            self.env = None

        def check(self, force=False):
            status = "red" if "red" in faults and self.env and self.env.sent else "ok"
            return NS(status=status, reasons=[])

        def attach(self, env):
            self.env = env
            env.guarded = True
            return env

    class Client:
        def __init__(self, host, port):
            self.role = "forward" if port == 8000 else "reset"

        def get_server_metadata(self):
            calls.append(("metadata", self.role))
            return {
                "config": "wrong" if "identity" in faults else "cfg",
                "checkpoint_dir": "/models/" + self.role,
            }

    def clamp(action, current, args):
        values = np.asarray(action, dtype=float)
        notes = []
        if np.linalg.norm(values[:3] - current[:3]) > args.max_step_m:
            notes.append("STEP")
        if values[2] < args.min_z:
            notes.append("z")
        return values, notes

    def query(client, obs, state, prompt):
        pose = state.copy()
        pose[0] += 0.001
        return np.array([pose] * 4)

    def home(env, args):
        pose = np.array([*args.home_pose, 1.0])
        if "home_error" in faults:
            env.step(np.array([0.45, 0, 0.2, 0, 0, 0, 1.0]))
            return
        env.step(pose)

    client = NS(
        Args=lambda: NS(),
        websocket_client_policy=NS(WebsocketClientPolicy=Client),
        _check_server=lambda *a: None,
        _check_prompt=lambda *a: None,
        _query_policy=query,
        _clamp_action=clamp,
        _go_home=home,
        _resolve_home=lambda args: np.array([*args.home_pose, 1.0]),
        build_camera_kwargs=lambda args: {
            "side": args.view_camera_id,
            "wrist": args.wrist_camera_id,
        },
    )

    class Video:
        def __init__(self, path, *a):
            Path(path).write_bytes(b"fake video")

        def isOpened(self):
            return True

        def write(self, image):
            pass

        def release(self):
            pass

    cv2 = NS(
        VideoWriter=Video,
        VideoWriter_fourcc=lambda *a: 1,
        imencode=lambda *a: (True, np.array([255, 216, 255, 0], dtype=np.uint8)),
    )

    class Buffer:
        def __init__(self):
            self.rows = []

        def add(self, *a):
            self.rows.append(a)

        def __len__(self):
            return len(self.rows)

    online = NS(
        OnlineJudge=lambda *a, **kw: NS(new_buffer=lambda hz: Buffer()),
        fetch_spec=lambda url: NS(),
    )
    clock = fake.FakeClock()
    monkeypatch.setattr(R.bridge, "Clock", lambda: clock)

    def provider(root, kind, robot, clock, contract):
        if kind == "scene":
            return ev.FakeSceneAssessor(
                [{"decision": x} for x in scenes],
                clock,
                cfg.run_id,
                default={"decision": "ready", "evidence": 1},
                contract=contract.key,
                predicates=contract.required,
            )
        return NS(use_evidence=lambda store: None)

    monkeypatch.setattr(R.providers, "FileProvider", provider)
    monkeypatch.setattr(
        R.providers,
        "FileEvents",
        lambda root, rid, c: ev.FakeEventStream(
            {
                role: [{"step": 2, "event_type": "object_settled"}]
                for role in deployments
            },
            c,
            rid,
        ),
    )
    monkeypatch.setattr(
        R.providers,
        "OnlineVerifier",
        lambda module, judge, c, media, job, reset, **kw: ev.FakeGoalVerifier(
            [], c, cfg.run_id, default={"decision": "confirmed", "delay_ns": 0}
        ),
    )
    modules = (
        client,
        NS(Fr3Guard=Guard, GuardConfig=lambda **kw: NS(**kw)),
        NS(RobotEnv=Env),
        online,
        cv2,
        Rotation,
    )
    return NS(job=job, binding=binding, calls=calls, modules=modules, clock=clock)


@pytest.mark.parametrize("mode", ["human_assisted", "single_reset_policy"])
def test_actual_entry_completes_both_reset_modes_on_fake_io(
    tmp_path, monkeypatch, mode
):
    r = rig(
        tmp_path,
        monkeypatch,
        mode,
        scenes=("reset_required", "ready", "ready")
        if mode == "single_reset_policy"
        else (),
    )
    assert (
        R.execute(r.job, r.binding, load_modules=lambda binding: r.modules)
        == "COMPLETED"
    )
    directory = Path(r.job["rollout_root"]) / ".aeri/runs/real-test"
    events = Journal.read(directory).events
    assert sm.check_journal(events) == []
    results = [
        e.episode_result for e in events if e.record == "committed" and e.episode_result
    ]
    assert all(
        x.task_outcome != "success" or x.goal_verification == "verified"
        for x in results
    )
    assert any(x.rollout.task_folder == "reset" for x in results) == (
        mode == "single_reset_policy"
    )
    assert r.calls[-1][0] == "closed"
    assert r.calls.index(next(x for x in r.calls if x[0] == "env")) > r.calls.index(
        ("metadata", "forward")
    )
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["episodes"]
    for path in Path(r.job["rollout_root"]).glob("group/*/demo_*/*.mp4"):
        assert path.read_bytes() == b"fake video"


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("red", "FAULT_LOCKED"),
        ("no_ack", "FAULT_LOCKED"),
        ("home_error", "FAULT_LOCKED"),
    ],
)
def test_actual_entry_fault_gates_hold_and_close(
    tmp_path, monkeypatch, fault, expected
):
    r = rig(tmp_path, monkeypatch, faults=(fault,))
    assert (
        R.execute(r.job, r.binding, load_modules=lambda binding: r.modules) == expected
    )
    assert r.calls[-1][0] == "closed"
    if fault == "no_ack":
        assert r.calls[-1][1] == 1


def test_identity_refused_before_robot_construction(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch, faults=("identity",))
    with pytest.raises(ValueError, match="identity mismatch"):
        R.execute(r.job, r.binding, load_modules=lambda binding: r.modules)
    assert not any(c[0] == "env" for c in r.calls)


def test_binding_check_requires_identity_and_real_provider(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch, "single_reset_policy")
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(r.binding))
    value, digest = R.load_binding(path, r.job)
    assert value == r.binding and len(digest) == 64
    (Path(r.binding["providers"]["file_root"]) / "READY.json").unlink()
    with pytest.raises(ValueError, match="provider READY"):
        R.load_binding(path, r.job)
    assert r.calls == []


def test_old_run_refused_before_even_loading_hardware_modules(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    (Path(r.job["rollout_root"]) / ".aeri/runs/real-test").mkdir(parents=True)
    with pytest.raises(ValueError, match="already exists"):
        R.execute(r.job, r.binding, load_modules=lambda b: pytest.fail("SDK imported"))


def test_policy_timeout_cannot_acknowledge_quiesce_or_accept_late_query(
    tmp_path, monkeypatch
):
    r = rig(tmp_path, monkeypatch)
    client = r.modules[0]
    started, release = threading.Event(), threading.Event()

    def query(*a):
        started.set()
        release.wait(2)
        return np.ones((1, 7))

    client._query_policy = query
    robot = NS(raw={"robot_state": {"cartesian_position": np.ones(7)}})
    policy = B.Policy(
        {
            "forward": client.websocket_client_policy.WebsocketClientPolicy(
                "127.0.0.1", 8000
            )
        },
        r.job["plan"]["deployments"],
        r.job["texts"],
        robot,
        client,
        NS(),
        r.clock,
    )
    assert policy.acquire("forward", policy_epoch=1)["role"] == "forward"
    req = {
        "run_id": "real-test",
        "request_id": "q",
        "episode_id": "real-test.forward.0001",
        "policy_epoch": 1,
        "action_start_index": 0,
        "clock_domain": r.clock.domain,
    }
    ticket = policy.request(req)
    assert started.wait(1)
    assert policy.collect(ticket, timeout_ns=0) is None
    assert policy.quiesce(policy_epoch=1)["ok"] is False
    assert policy.request(req)["kind"] == "unavailable"
    release.set()
    assert ticket.done.wait(1)
    assert policy.quiesce(policy_epoch=1)["ok"] is True


def test_http_motion_route_checks_the_token_again_after_clearerr(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    client, health, env_module, *_ = r.modules
    env = env_module.RobotEnv(do_reset=False)
    guard = health.Fr3Guard()
    guard.attach(env)
    fence = sm.MotionFence()
    token = fence.issue(
        sm.MotionToken(
            run_id="real-test",
            transaction_id="tx",
            control_epoch=1,
            kind="policy_steps",
            episode_id="real-test.forward.0001",
            policy_epoch=1,
            expires_mono_ns=r.clock.now() + 1000000000,
        )
    )
    post = env._post

    def revoke_after_clearerr(route, payload=None):
        out = post(route, payload)
        if route == "clearerr":
            fence.revoke()
        return out

    env._post = revoke_after_clearerr
    args = NS(**r.binding["safety"])
    robot = B.Robot(env, guard, client, args, r.clock, fence, tolerance_mm=5)
    result = robot.step(env.pose, token=token)
    assert result.executed == "unknown"
    assert ("route", "pose") not in r.calls
    assert robot.latch_state()


def test_operator_stop_blocks_all_new_steps_and_homing(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    client, health, env_module, *_ = r.modules
    env = env_module.RobotEnv(do_reset=False)
    guard = health.Fr3Guard()
    guard.attach(env)
    args = NS(**r.binding["safety"])
    robot = B.Robot(env, guard, client, args, r.clock, sm.MotionFence(), tolerance_mm=5)
    robot.emergency_hold()
    assert robot.step(env.pose, token=None).executed == "no"
    assert robot.home(token=None).executed == "no"
    assert not any(c[0] == "route" for c in r.calls)


def test_check_and_nonterminal_refusal_never_import_sdk(tmp_path, monkeypatch, capsys):
    r = rig(tmp_path, monkeypatch)
    job_path = tmp_path / "job.yaml"
    binding_path = tmp_path / "binding.json"
    job_path.write_text("not used by fake parser")
    binding_path.write_text(json.dumps(r.binding))
    monkeypatch.setattr(R.cli, "load_job", lambda path: r.job)
    monkeypatch.setattr(R, "execute", lambda *a: pytest.fail("hardware entry reached"))
    assert (
        R.main(["--config", str(job_path), "--binding", str(binding_path), "--check"])
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["hardware_verified"] is False
    assert (
        R.main(
            [
                "--config",
                str(job_path),
                "--binding",
                str(binding_path),
                "--execute",
                "--expect-plan",
                out["plan_sha256"],
                "--expect-binding",
                out["binding_sha256"],
            ]
        )
        == 2
    )
    assert r.calls == []


def test_reset_failure_waits_for_human_without_a_forward_episode(tmp_path, monkeypatch):
    r = rig(
        tmp_path,
        monkeypatch,
        "single_reset_policy",
        scenes=("reset_required", "reset_required"),
    )
    assert R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "WAIT_HUMAN"
    scan = Journal.read(Path(r.job["rollout_root"]) / ".aeri/runs/real-test")
    assert not any(e.to_state == "FORWARD_ACTIVE" for e in scan.events)
    assert r.calls[-1][0] == "closed"


def test_provider_ready_heartbeat_expires_and_ignores_symlinks(tmp_path):
    from levi.automatic import fr3_providers as P

    contract = NS(contract_id="initial", contract_version="1")
    payload = {
        "schema": "levi.aeri.provider_ready.v1",
        "contract_id": "initial",
        "contract_version": "1",
        "capabilities": ["scene"],
        "updated_wall_ns": 100,
    }
    file = tmp_path / "READY.json"
    file.write_text(json.dumps(payload))
    assert P.ready(tmp_path, contract, ["scene"], now=100)
    assert not P.ready(tmp_path, contract, ["scene"], now=10000000101)
    assert not P.ready(tmp_path, contract, ["reset_judgement"], now=100)
    file.unlink()
    file.symlink_to(tmp_path / "absent")
    assert not P.ready(tmp_path, contract, ["scene"], now=100)


def test_recipe_quotes_actual_job_path_and_does_not_import_sdk(tmp_path, monkeypatch):
    import shlex

    r = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(R.cli, "load_job", lambda path: r.job)
    path = tmp_path / "job --max-timesteps 'quoted'.yaml"
    recipe = R.recipe(path)
    argv = shlex.split(recipe["check_command"])
    assert argv[argv.index("--config") + 1] == str(path.resolve())
    assert recipe["requires_new_plan"] is False
    assert recipe["hardware_verified"] is False
    assert r.calls == []


def test_stop_probe_blocks_motion_before_callback_thread(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    client, health, env_module, *_ = r.modules
    env = env_module.RobotEnv(do_reset=False)
    guard = health.Fr3Guard()
    guard.attach(env)
    robot = B.Robot(
        env,
        guard,
        client,
        NS(**r.binding["safety"]),
        r.clock,
        sm.MotionFence(),
        tolerance_mm=5,
    )
    robot.stop_probe = lambda: True
    assert robot.step(env.pose, token=None).executed == "no"
    assert not any(c[0] == "route" for c in r.calls)


def test_real_file_provider_keeps_current_frames_and_rejects_prebuilt_reply(
    tmp_path, monkeypatch
):
    import sys

    from levi.automatic import fr3_providers as P

    file_provider = P.FileProvider
    r = rig(tmp_path, monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        NS(imencode=lambda *a: (True, np.array([1, 2], dtype=np.uint8))),
    )
    robot = NS(
        observe=lambda: {
            "images_bgr": {"side": np.zeros((2, 2, 3))},
            "observed_ns": r.clock.now(),
        }
    )
    root = Path(r.binding["providers"]["file_root"])
    provider = file_provider(root, "scene", robot, r.clock, r.job["contract"])
    provider.use_evidence(
        NS(
            folder=tmp_path / "evidence",
            keep_frame=lambda ref, data: {
                "ref": ref,
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        )
    )
    request = {"request_id": "real-scene-1", "run_id": "real-test"}
    ticket = provider.submit(request)
    payload = json.loads(
        (
            root / "scene/requests" / (P.key(request["request_id"]) + ".json")
        ).read_bytes()
    )
    assert payload["clock_domain"] == r.clock.domain
    assert payload["frames"][0]["sha256"] == hashlib.sha256(bytes([1, 2])).hexdigest()
    assert payload["contract"]["required"] == ["object_at_source"]
    assert provider.collect(ticket, timeout_ns=0) is None
    ticket.path.write_bytes(b'{"actual": "provider response"}')
    assert provider.collect(ticket, timeout_ns=0) == b'{"actual": "provider response"}'
    with pytest.raises(ValueError, match="unsolicited"):
        provider.submit(request)
    cancelled = provider.submit({"request_id": "cancel-2", "run_id": "real-test"})
    provider.cancel(cancelled, "operator stop")
    assert provider.collect(cancelled, timeout_ns=0) is None
    assert (root / "scene/cancelled" / (P.key("cancel-2") + ".json")).is_file()


def test_actual_online_verifier_captures_post_quiesce_observation(
    tmp_path, monkeypatch
):
    from levi.automatic import fr3_providers as P

    verifier_class = P.OnlineVerifier
    r = rig(tmp_path, monkeypatch)
    captured = []
    buffer = NS(rows=[], snapshot=lambda: ["actual frame"], start_frame=lambda: 0)
    buffer.add = lambda *args: captured.append(args)

    class ActualBuffer:
        def __len__(self):
            return 1

        def add(self, *args):
            return buffer.add(*args)

        def snapshot(self):
            return buffer.snapshot()

        def start_frame(self):
            return buffer.start_frame()

    pending = NS(
        wait=lambda seconds: True,
        take=lambda: {
            "schema": "levi.online.judge.result.v1",
            "status": "unavailable",
            "reason": "provider_error",
        },
        cancel=lambda: captured.append("cancelled"),
    )
    judge = NS(submit=lambda *a, **kw: NS(pending=pending))
    verifier = verifier_class(
        None,
        judge,
        r.clock,
        NS(buffers={"ep": ActualBuffer()}),
        r.job,
        robot=NS(observe=lambda: {"raw": {"image": {"view1": "current pixels"}}}),
    )
    request = {
        "request_id": "ep:final",
        "run_id": "real-test",
        "episode_id": "ep",
        "episode_role": "forward",
        "target": "forward_goal",
        "observed_through_step": 3,
        "observed_through_ns": r.clock.now(),
    }
    ticket = verifier.submit(request)
    assert captured[0][0] == 4
    assert captured[0][2] == {"view1": "current pixels"}
    response = json.loads(verifier.collect(ticket, timeout_ns=1))
    assert response["kind"] == "unavailable"
    assert response["request_id"] == "ep:final"
    verifier.cancel(ticket, "stop")
    assert captured[-1] == "cancelled"
    reset_request = {**request, "episode_role": "reset", "target": "reset_goal"}
    assert json.loads(verifier.submit(reset_request))["kind"] == "unavailable"


@pytest.mark.parametrize("case", ["wrong_phrase", "changed_binding"])
def test_execute_requires_current_terminal_confirmation(tmp_path, monkeypatch, case):
    import sys

    r = rig(tmp_path, monkeypatch)
    job_path, binding_path = tmp_path / "job.yaml", tmp_path / "binding.json"
    binding_path.write_text(json.dumps(r.binding))
    monkeypatch.setattr(R.cli, "load_job", lambda path: r.job)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        R, "execute", lambda *a: pytest.fail("hardware execute bypassed confirmation")
    )

    def confirmation(prompt):
        if case == "changed_binding":
            changed = dict(r.binding)
            changed["cameras"] = {"side": "987", "wrist": "456"}
            binding_path.write_text(json.dumps(changed))
            return "execute real-test"
        return "yes"

    monkeypatch.setattr("builtins.input", confirmation)
    digest = hashlib.sha256(binding_path.read_bytes()).hexdigest()
    assert (
        R.main(
            [
                "--config",
                str(job_path),
                "--binding",
                str(binding_path),
                "--execute",
                "--expect-plan",
                r.job["plan_sha256"],
                "--expect-binding",
                digest,
            ]
        )
        == 2
    )
    assert r.calls == []


def test_explicit_stale_robot_read_locks_motion(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    client, health, env_module, *_ = r.modules
    env = env_module.RobotEnv(do_reset=False)
    env._post = lambda *a: {"state_fresh": False}
    guard = health.Fr3Guard()
    guard.attach(env)
    robot = B.Robot(
        env,
        guard,
        client,
        NS(**r.binding["safety"]),
        r.clock,
        sm.MotionFence(),
        tolerance_mm=5,
    )
    with pytest.raises(ValueError, match="stale"):
        env._post("getstate")
    assert robot.latch_state()
    assert robot.step(env.pose, token=None).executed == "no"
