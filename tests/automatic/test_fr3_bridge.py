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
def no_network(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "ARM_LOCK_FILE", str(tmp_path / "physical-arm.lock"))

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
            "rx_range": [-3.141592653589793, 3.141592653589793],
            "ry_range": [-3.141592653589793, 3.141592653589793],
            "rz_range": [-3.141592653589793, 3.141592653589793],
            "binarize_gripper": True,
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
            clearerr = self._post("clearerr")
            pose = self._post("pose")
            self.sent += 1
            if "no_ack" in faults:
                return None
            self.pose = np.asarray(action).copy()
            return {
                "clearerr_response": clearerr,
                "pose_response": pose,
                "gripper_response": None,
            }

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
    from levi.automatic.recorder import EvidenceStore

    provider.use_evidence(EvidenceStore(tmp_path / "retained"))
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
    assert json.loads(provider.collect(ticket, timeout_ns=0))["kind"] == "unavailable"
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


@pytest.mark.parametrize(
    "kind,attack",
    [
        ("scene", "top_old"),
        ("scene", "predicate_old"),
        ("scene", "missing"),
        ("scene", "timestamp"),
        ("scene", "saved_deleted"),
        ("scene", "none"),
        ("reset_judgement", "predicate_old"),
        ("reset_judgement", "missing"),
        ("reset_judgement", "timestamp"),
        ("reset_judgement", "saved_deleted"),
        ("reset_judgement", "none"),
    ],
)
def test_actual_execute_binds_provider_evidence_to_current_ticket(
    tmp_path, monkeypatch, kind, attack
):
    import sys

    from levi.automatic import fr3_providers as P

    real_provider = P.FileProvider
    mode = "human_assisted" if kind == "scene" else "single_reset_policy"
    r = rig(tmp_path, monkeypatch, mode)
    monkeypatch.setitem(sys.modules, "cv2", r.modules[4])
    reset = {}
    replies = []

    class AnsweringProvider(real_provider):
        def collect(self, ticket, *, timeout_ns):
            if self.kind == "scene":
                decision = (
                    "reset_required"
                    if mode == "single_reset_policy" and not reset.get("initial_seen")
                    else "ready"
                )
                reset["initial_seen"] = True
                builder = ev.FakeSceneAssessor(
                    [],
                    r.clock,
                    "real-test",
                    default={"decision": decision, "delay_ns": 0},
                    contract=r.job["contract"].key,
                    predicates=r.job["contract"].required,
                )
            else:
                builder = ev.FakeGoalVerifier(
                    [],
                    r.clock,
                    "real-test",
                    default={"decision": "confirmed", "delay_ns": 0},
                )
            answer = json.loads(
                builder.collect(builder.submit(ticket.request), timeout_ns=0)
            )
            references = [
                {"kind": "frame", "ref": ref, "sha256": digest, "step": None}
                for ref, digest, file in ticket.frames
            ]
            for predicate in answer["predicate_results"]:
                predicate["evidence_refs"] = references
            observed_key = (
                "observed_ns" if self.kind == "scene" else "observed_through_ns"
            )
            answer[observed_key] = ticket.observed_ns
            answer["provider"], answer["model_name"] = "vlm", "test-model"
            if self.kind == "scene":
                answer["evidence_refs"] = references
            if self.kind == kind:
                old = [
                    {
                        "kind": "frame",
                        "ref": "side:OLD-NONEXISTENT",
                        "sha256": "0" * 64,
                        "step": None,
                    }
                ]
                if attack == "top_old":
                    answer["evidence_refs"] = old
                if attack == "predicate_old":
                    answer["predicate_results"][0]["evidence_refs"] = old
                if attack == "missing":
                    answer["predicate_results"][0]["evidence_refs"] = []
                if attack == "timestamp":
                    answer[observed_key] += 1
                if attack == "saved_deleted":
                    (self.store.folder / ticket.frames[0][2]).unlink()
            ticket.path.write_text(json.dumps(answer))
            result = super().collect(ticket, timeout_ns=timeout_ns)
            decoded = json.loads(result)
            replies.append(
                {k: decoded.get(k) for k in ("request_id", "kind", "code", "detail")}
            )
            return result

    def provider(root, which, robot, clock, contract):
        value = AnsweringProvider(root, which, robot, clock, contract)
        if which == "reset_judgement":
            reset["provider"] = value
        return value

    monkeypatch.setattr(P, "FileProvider", provider)
    forward = ev.FakeGoalVerifier(
        [], r.clock, "real-test", default={"decision": "confirmed", "delay_ns": 0}
    )

    class Verifier:
        def submit(self, request):
            return (
                reset["provider"].submit(request)
                if request["episode_role"] == "reset"
                else forward.submit(request)
            )

        def collect(self, ticket, *, timeout_ns):
            return (
                reset["provider"].collect(ticket, timeout_ns=timeout_ns)
                if ticket.request["episode_role"] == "reset"
                else forward.collect(ticket, timeout_ns=timeout_ns)
            )

        def cancel(self, ticket, reason):
            return (
                reset["provider"].cancel(ticket, reason)
                if ticket.request["episode_role"] == "reset"
                else forward.cancel(ticket, reason)
            )

    monkeypatch.setattr(P, "OnlineVerifier", lambda *a, **kw: Verifier())
    state = R.execute(r.job, r.binding, load_modules=lambda binding: r.modules)
    scan = Journal.read(Path(r.job["rollout_root"]) / ".aeri/runs/real-test")
    if attack == "none":
        assert state == "COMPLETED", replies
    else:
        assert state == "WAIT_HUMAN"
        assert not any(e.to_state == "FORWARD_ACTIVE" for e in scan.events)
        if kind == "scene":
            assert not any(e.episode_result for e in scan.events)
        else:
            assert all(
                not e.episode_result.judgement_ids
                for e in scan.events
                if e.episode_result
            )
    assert r.calls[-1][0] == "closed"


def test_file_events_deliver_after_more_than_thousand_seen_files(tmp_path):
    from levi.automatic.fr3_providers import FileEvents

    events = FileEvents(tmp_path, "run", fake.FakeClock())
    stream = events.open("ep", "forward")
    folder = events.streams[stream][0]
    for n in range(1101):
        (folder / f"event-{n:05d}.json").write_text(json.dumps({"number": n}))
    found = []
    for _ in range(20):
        found.extend(
            json.loads(item)["number"]
            for item in events.poll(stream, through_step=99, max_events=64)
        )
    assert found == list(range(1101))
    assert events.poll(stream, through_step=99, max_events=64) == []


@pytest.mark.parametrize(
    "route,response",
    [
        ("clearerr", None),
        ("clearerr", {"ok": False}),
        ("pose", None),
        ("pose", {"ok": False}),
        ("close_gripper", None),
        ("close_gripper", {"ok": False}),
    ],
)
def test_actual_execute_refuses_unknown_or_negative_nested_http_ack(
    tmp_path, monkeypatch, route, response
):
    r = rig(tmp_path, monkeypatch)
    env_class = r.modules[2].RobotEnv
    post, step = env_class._post, env_class.step

    def failing_post(self, name, payload=None):
        if name == route:
            r.calls.append(("route", name))
            return response
        return post(self, name, payload)

    def with_gripper(self, action):
        value = step(self, action)
        if route == "close_gripper":
            value["gripper_response"] = {
                "command": route,
                "response": self._post(route),
            }
        return value

    monkeypatch.setattr(env_class, "_post", failing_post)
    monkeypatch.setattr(env_class, "step", with_gripper)
    assert (
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "FAULT_LOCKED"
    )
    routes = [c[1] for c in r.calls if c[0] == "route"]
    assert routes.count(route) == 1
    if route == "clearerr":
        assert "pose" not in routes
    assert r.calls[-1][0] == "closed"


def test_nested_pose_field_must_match_an_actual_ack(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    env_class = r.modules[2].RobotEnv
    step = env_class.step

    def missing_nested(self, action):
        value = step(self, action)
        value["pose_response"] = None
        return value

    monkeypatch.setattr(env_class, "step", missing_nested)
    assert (
        R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "FAULT_LOCKED"
    )
    assert sum(c == ("route", "pose") for c in r.calls) == 1


def test_different_run_roots_and_both_ports_share_the_physical_arm_lock(
    tmp_path, monkeypatch
):
    r = rig(tmp_path, monkeypatch)
    held, release = threading.Event(), threading.Event()
    result = []
    monkeypatch.setattr(R, "assembled", lambda *a: "COMPLETED")

    def importing(binding):
        held.set()
        assert release.wait(5)
        return r.modules

    def first():
        result.append(R.execute(r.job, r.binding, load_modules=importing))

    thread = threading.Thread(target=first)
    thread.start()
    assert held.wait(5)
    second = {
        **r.job,
        "config": replace(r.job["config"], run_id="different-run"),
        "rollout_root": str(tmp_path / "other-root"),
    }
    try:
        for port in (5000, 5001):
            binding = {**r.binding, "robot_url": f"http://127.0.0.1:{port}/"}
            with pytest.raises(ValueError, match="already owned"):
                R.execute(
                    second,
                    binding,
                    load_modules=lambda b: pytest.fail("second run imported SDK"),
                )
        assert r.calls == []
    finally:
        release.set()
        thread.join(5)
    assert result == ["COMPLETED"]

    # A failed import also releases the same kernel lock.
    def failed(binding):
        raise RuntimeError("import failed")

    with pytest.raises(RuntimeError, match="import failed"):
        R.execute(second, r.binding, load_modules=failed)
    assert R.execute(second, r.binding, load_modules=lambda b: r.modules) == "COMPLETED"


@pytest.mark.parametrize("option", ["--check", "--execute", "--config", "--binding"])
def test_check_and_question_modes_refuse_mixing_before_any_write(
    tmp_path, monkeypatch, option
):
    monkeypatch.setattr(
        R.human, "write_answer", lambda *a: pytest.fail("mixed mode wrote an answer")
    )
    monkeypatch.setattr(
        R.cli, "load_job", lambda *a: pytest.fail("mixed mode read a job")
    )
    argv = [
        "--scene-question",
        str(tmp_path / "absent/questions/q.json"),
        "--predicates",
        "{}",
        option,
    ]
    if option in ("--config", "--binding"):
        argv.append("absent")
    assert R.main(argv) == 2
    assert not Path(R.ARM_LOCK_FILE).exists()


@pytest.mark.parametrize("fault", ["release", "fsync"])
def test_media_close_attempts_every_resource_even_after_first_error(
    tmp_path, monkeypatch, fault
):
    from levi.automatic import fr3_media as M

    seen = []

    def fail(*args):
        raise OSError("first close error")

    class Handle:
        def __init__(self, name):
            self.name = name

        def flush(self):
            seen.append("flush-" + self.name)

        def fileno(self):
            return 7

        def close(self):
            seen.append("close-" + self.name)

    media = M.Media(None, None, None, None, 10)
    media.active[tmp_path] = {
        "video": {
            "side": NS(
                release=fail
                if fault == "release"
                else lambda: seen.append("release-side")
            ),
            "wrist": NS(release=lambda: seen.append("release-wrist")),
        },
        "files": {"one": Handle("one"), "two": Handle("two")},
        "episode": "ep",
    }
    media.buffers["ep"] = "buffer"
    monkeypatch.setattr(M.os, "fsync", fail if fault == "fsync" else lambda fd: None)
    with pytest.raises(OSError, match="first close error"):
        media.close()
    assert "release-wrist" in seen and "close-one" in seen and "close-two" in seen
    assert media.active == {} and media.buffers == {}


def test_reviewed_binding_matches_actual_sdk_pure_clamp(tmp_path, monkeypatch):
    import ast
    import os

    source_path = os.environ.get("LEVI_TEST_FR3_CLIENT_SOURCE")
    if not source_path:
        pytest.skip(
            "set LEVI_TEST_FR3_CLIENT_SOURCE to check the deployed SDK pure functions"
        )
    module = ast.parse(Path(source_path).read_text())
    names = {"wrap_to_pi", "_clamp_angle_to", "_clamp_action"}
    functions = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in functions} == names
    namespace = {"np": np, "Args": NS}
    exec(  # noqa: S102 - trusted deployed source, only three pure functions
        compile(
            ast.Module(body=functions, type_ignores=[]),
            "deployed-sdk-pure-clamp",
            "exec",
        ),
        namespace,
    )
    r = rig(tmp_path, monkeypatch)
    pure_clamp = namespace["_clamp_action"]
    pose = np.array([*r.binding["safety"]["home_pose"], 1.0])
    values, notes = pure_clamp(pose, pose, NS(**r.binding["safety"]))
    assert notes == [] and np.allclose(values, pose)
    fractional = pose.copy()
    fractional[6] = 0.7
    values, notes = pure_clamp(fractional, pose, NS(**r.binding["safety"]))
    assert notes == [] and values[6] == 1.0  # explicitly frozen normalization
    # The real client's default rx interval cannot home to zero rotation.
    invalid = json.loads(json.dumps(r.binding))
    invalid["safety"]["rx_range"] = [1.50, 4.03]
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(invalid))
    with pytest.raises(ValueError, match="home rotation"):
        R.load_binding(path, r.job)
    for parameter in ("rx_range", "ry_range", "rz_range", "binarize_gripper"):
        invalid = json.loads(json.dumps(r.binding))
        del invalid["safety"][parameter]
        path.write_text(json.dumps(invalid))
        with pytest.raises(ValueError, match="safety requires"):
            R.load_binding(path, r.job)


@pytest.mark.parametrize("fault", ["skipped", "error"])
def test_actual_execute_cannot_start_when_current_evidence_is_not_saved(
    tmp_path, monkeypatch, fault
):
    import sys

    from levi.automatic import fr3_providers as P
    from levi.automatic.recorder import EvidenceStore

    real_provider = P.FileProvider
    r = rig(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "cv2", r.modules[4])
    monkeypatch.setattr(P, "FileProvider", real_provider)

    def failed_save(*args):
        if fault == "error":
            raise OSError("disk full")
        return {"file": None, "skipped": "evidence_budget_exhausted"}

    monkeypatch.setattr(EvidenceStore, "keep_frame", failed_save)
    assert R.execute(r.job, r.binding, load_modules=lambda b: r.modules) == "WAIT_HUMAN"
    scan = Journal.read(Path(r.job["rollout_root"]) / ".aeri/runs/real-test")
    assert not any(e.to_state == "FORWARD_ACTIVE" for e in scan.events)


def test_recorder_close_preserves_first_error_and_still_closes_media(monkeypatch):
    from levi.automatic import fr3_media as M

    seen = []

    def recorder_close(self):
        seen.append("recorder")
        raise OSError("first recorder error")

    def media_close():
        seen.append("media")
        raise OSError("second media error")

    monkeypatch.setattr(M.RolloutRecorder, "close", recorder_close)
    recorder = object.__new__(M.Recorder)
    recorder.media = NS(close=media_close)
    with pytest.raises(OSError, match="first recorder error"):
        recorder.close()
    assert seen == ["recorder", "media"]
