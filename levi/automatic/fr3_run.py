"""Explicit operator entry: python -m levi.automatic.fr3_run --help.

--check is read-only and imports no hardware modules. --execute needs a typed
confirmation at a terminal, immutable job/binding digests and real providers.
No deployment/service launch is performed. This is separate from the HTTP
launch runner, whose real-mode refusal remains in force.
"""

import argparse
import fcntl
import hashlib
import importlib
import json
import math
import os
import select
import shlex
import signal
import stat
import sys
import threading
import time
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from . import cli, control, launch
from . import fr3_bridge as bridge
from . import fr3_providers as providers
from . import state_machine as sm
from .adapters import human
from .orchestrator import Orchestrator
from .recorder import SessionFiles

SCHEMA = "levi.aeri.fr3.binding.v1"
ARM_LOCK_FILE = "/tmp/levi-aeri-fr3-arm.lock"


class RobotLock:
    """One conservative arm lock for both local motion ports; never unlink it."""

    def __enter__(self):
        self.fd = os.open(
            ARM_LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("physical arm lock has an unsafe owner/type/mode")
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "physical FR3 arm is already owned by another bridge run"
                ) from None
        except BaseException:
            os.close(self.fd)
            raise
        return self

    def __exit__(self, *args):
        os.close(self.fd)


def load_binding(path, job):
    data = Path(path).read_bytes()
    if len(data) > 64 * 1024:
        raise ValueError("binding exceeds 64 KiB")
    value = cli.sa.parse_document(data.decode("utf-8"))
    required = {
        "schema",
        "openpi_root",
        "robot_url",
        "health_file",
        "cameras",
        "safety",
        "policies",
        "providers",
    }
    if (
        not isinstance(value, dict)
        or set(value) not in (required, required | {"signal_events"})
        or value["schema"] != SCHEMA
    ):
        raise ValueError("binding requires exactly " + ", ".join(sorted(required)))
    for name in ("openpi_root", "health_file"):
        if not isinstance(value[name], str) or not Path(value[name]).is_absolute():
            raise ValueError(name + " must be an absolute path")
    url = urlparse(value["robot_url"])
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or url.port not in (5000, 5001)
        or url.username
        or url.password
    ):
        raise ValueError("robot_url must be the operator's loopback robot server")
    cameras = value["cameras"]
    if not isinstance(cameras, dict):
        raise TypeError("cameras must be a mapping")
    if (
        set(cameras) != {"side", "wrist"}
        or any(not isinstance(s, str) or not s.isdigit() for s in cameras.values())
        or cameras["side"] == cameras["wrist"]
    ):
        raise ValueError("two distinct camera serials are required")
    safety = value["safety"]
    if not isinstance(safety, dict):
        raise TypeError("safety must be a mapping")
    expected = {
        "min_z",
        "max_xy",
        "max_step_m",
        "max_step_rad",
        "home_pose",
        "home_duration_s",
        "home_tolerance_mm",
        "rx_range",
        "ry_range",
        "rz_range",
        "binarize_gripper",
    }
    if set(safety) != expected:
        raise ValueError("safety requires exactly " + ", ".join(sorted(expected)))
    if type(safety["binarize_gripper"]) is not bool:
        raise ValueError("binarize_gripper must be an explicit reviewed boolean")
    for name in ("rx_range", "ry_range", "rz_range"):
        limits = safety[name]
        if (
            not isinstance(limits, list)
            or len(limits) != 2
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in limits)
            or not 0 < limits[1] - limits[0] <= 2 * math.pi
        ):
            raise ValueError(
                name + " must be a finite increasing angular interval of at most 2pi"
            )
    for name in expected - {
        "home_pose",
        "rx_range",
        "ry_range",
        "rz_range",
        "binarize_gripper",
    }:
        x = safety[name]
        if (
            isinstance(x, bool)
            or not isinstance(x, int | float)
            or not math.isfinite(x)
            or x <= 0
        ):
            raise ValueError("safety." + name + " must be positive finite")
    if safety["home_duration_s"] * 1e9 >= job["config"].home_timeout_ns:
        raise ValueError("home duration must be shorter than the frozen home timeout")
    pose = safety["home_pose"]
    if (
        not isinstance(pose, list)
        or len(pose) != 6
        or any(
            isinstance(x, bool)
            or not isinstance(x, int | float)
            or not math.isfinite(x)
            for x in pose
        )
    ):
        raise ValueError("home_pose must contain six finite metres/radians")
    for angle, name in zip(
        pose[3:6], ("rx_range", "ry_range", "rz_range"), strict=True
    ):
        lo, hi = safety[name]
        if (angle - lo) % (2 * math.pi) > hi - lo + 1e-6:
            raise ValueError("home rotation is outside reviewed " + name)
    if pose[2] < safety["min_z"] or any(abs(x) > safety["max_xy"] for x in pose[:2]):
        raise ValueError("home pose is outside the configured workspace")
    roles = (
        {"forward"}
        if job["config"].reset_strategy == "human_assisted"
        else {"forward", "reset"}
    )
    if not isinstance(value["policies"], dict) or set(value["policies"]) != roles:
        raise ValueError("policy endpoint roles do not match reset strategy")
    deployments = job["plan"].get("deployments", {})
    for role in roles:
        if role not in deployments:
            raise ValueError(role + " requires a frozen deployment identity")
        bridge.checkpoint_path(deployments[role].get("checkpoint_dir"))
        endpoint = value["policies"][role]
        if not isinstance(endpoint, dict):
            raise TypeError("policy endpoint must be a mapping")
        if (
            set(endpoint) != {"host", "port"}
            or endpoint["host"] != "127.0.0.1"
            or type(endpoint["port"]) is not int
            or not 1024 <= endpoint["port"] <= 65535
        ):
            raise ValueError("policy endpoint must be an explicit loopback host/port")
        if endpoint["port"] in (5000, 5001, 5100, 7470):
            raise ValueError("policy endpoint names a motion/learner port")
    if not job.get("rollout_root") or not Path(job["rollout_root"]).is_absolute():
        raise ValueError("recording.rollout_root must be frozen as an absolute path")
    if job["contract"] is None or job["contract"].status != "confirmed":
        raise ValueError("real execution requires a confirmed Initial State Contract")
    if job["mode"] == "shadow":
        raise ValueError("shadow mode has no motion authority")
    provider = value["providers"]
    if not isinstance(provider, dict) or set(provider) not in (
        {"online_url", "file_root"},
        {"online_url", "file_root", "model_spec_file", "model_spec_sha256"},
    ):
        raise ValueError(
            "providers requires online_url/file_root and, together, model_spec_file/model_spec_sha256"
        )
    judge_url = urlparse(provider["online_url"])
    if (
        judge_url.scheme != "http"
        or judge_url.hostname != "127.0.0.1"
        or judge_url.port != 7882
        or judge_url.username
        or judge_url.password
    ):
        raise ValueError("online_url must be the existing loopback C5 :7882 endpoint")
    if (
        not isinstance(provider["file_root"], str)
        or not Path(provider["file_root"]).is_absolute()
    ):
        raise ValueError("provider file_root must be absolute")
    if (
        not Path(provider["file_root"]).is_dir()
        or Path(provider["file_root"]).is_symlink()
    ):
        raise ValueError("provider file_root must be an existing real directory")
    if "signal_events" in value and value["signal_events"] is None:
        raise ValueError("signal_events must be a complete explicit declaration")
    signals = signal_binding(value.get("signal_events"))
    model_spec = model_binding(provider, job, signals=signals)
    if job["config"].scene_check == "provider" or (
        model_spec is not None and "reset" in roles
    ):
        caps = {"scene"} if job["config"].scene_check == "provider" else set()
        if "reset" in roles:
            caps.add("reset_judgement")
        if job["config"].termination.allow_early_stop and signals is None:
            caps.add("events")
        if not providers.ready(
            provider["file_root"], job["contract"], caps, model_spec=model_spec
        ):
            raise ValueError(
                "real provider READY.json is missing, stale or does not match contract/capabilities"
            )
    return value, hashlib.sha256(data).hexdigest()


def signal_binding(value):
    """File-only opt-in validation; every semantic/threshold is reviewed inline.

    The full binding bytes, including this declaration, are covered by the
    operator's existing --expect-binding digest and reread after confirmation.
    No profile is inferred from task names or SDK defaults.
    """
    if value is None:
        return None
    from levi.events.signal_profiles import SignalProfile
    from levi.events.stream import StreamConfig

    from .adapters.signal_events import Binding, CachedObserver, SignalEvents

    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "profile", "bindings", "config", "actor_id"}
        or value["schema"] != "levi.aeri.signal_events.binding.v1"
    ):
        raise ValueError(
            "signal_events requires schema/profile/bindings/config/actor_id"
        )
    raw = value["profile"]
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "origin", "channels", "overridden"}
        or raw["origin"] != "declared"
        or raw["overridden"] != []
    ):
        raise ValueError("signal_events requires a complete declared profile")
    if not isinstance(raw["channels"], list) or not raw["channels"]:
        raise ValueError("signal_events requires explicit channels")
    for channel in raw["channels"]:
        required = {"role", "feature", "index", "actor_id", "kind", "units"}
        if not isinstance(channel, dict) or not required <= set(channel):
            raise ValueError("signal channel semantics must be explicit")
        if type(channel["index"]) is not int or any(
            not isinstance(channel[name], str)
            for name in ("feature", "actor_id", "kind", "units")
        ):
            raise ValueError("signal channel semantics must be explicitly typed")
        if channel["kind"] not in {
            "measured",
            "commanded",
        }:
            raise ValueError("signal channel index/kind must be explicit and typed")
        if channel["role"] == "gripper" and not {"open_level", "valid_range"} <= set(
            channel
        ):
            raise ValueError("gripper polarity and range must be explicit")
        if channel["role"] == "gripper":
            limits = channel.get("valid_range")
            if (
                not isinstance(limits, list)
                or len(limits) != 2
                or any(
                    type(v) not in (int, float) or not math.isfinite(v) for v in limits
                )
            ):
                raise ValueError("gripper range must be explicit finite numbers")
        if channel["role"] == "position" and (
            channel.get("frame") != "base" or "axis" not in channel
        ):
            raise ValueError(
                "FR3 measured position requires explicit base frame and axis"
            )
    profile = SignalProfile.model_validate(raw)
    config = value["config"]
    if not isinstance(config, dict) or set(config) != {
        f.name for f in fields(StreamConfig)
    }:
        raise ValueError("all StreamConfig thresholds and budgets must be explicit")
    config = StreamConfig(**config)
    raw_bindings = value["bindings"]
    if not isinstance(raw_bindings, list) or not 1 <= len(raw_bindings) <= 64:
        raise ValueError("signal bindings must be a bounded nonempty list")
    bindings = []
    for item in raw_bindings:
        if not isinstance(item, dict) or set(item) != {
            "feature",
            "index",
            "path",
            "kind",
        }:
            raise ValueError("signal binding requires feature/index/path/kind")
        if (
            not isinstance(item["feature"], str)
            or type(item["index"]) is not int
            or not isinstance(item["path"], list)
            or not 1 <= len(item["path"]) <= 4
            or any(type(part) not in (str, int) for part in item["path"])
            or item["kind"] not in {"measured", "commanded"}
        ):
            raise ValueError("signal binding fields must be explicitly typed")
        bindings.append(
            Binding(item["feature"], item["index"], tuple(item["path"]), item["kind"])
        )
    actor = value["actor_id"]
    if actor != "arm_0":
        raise ValueError("FR3 signal actor must be the explicitly declared arm_0")
    # Constructor performs complete profile/channel/path validation without I/O.
    clock = SimpleNamespace(domain="host-mono:binding")
    SignalEvents(
        CachedObserver(None, clock, step_of=lambda: 0),
        profile,
        bindings,
        run_id="binding-check",
        clock=clock,
        evidence_root=Path("/"),
        keep_frame=lambda *args: None,
        config=config,
        actor_id=actor,
    )
    return profile, tuple(bindings), config, actor


def model_binding(provider, job, *, signals=None):
    """Pure-file check: freeze must match the existing job, never rewrite it."""
    if "model_spec_file" not in provider:
        return None  # Existing external file protocol; not a model-worker claim.
    from .provider_worker import load_spec

    path = Path(provider["model_spec_file"])
    if not path.is_absolute():
        raise ValueError("model_spec_file must be absolute")
    spec = load_spec(path)
    value, cfg = spec.value, job["config"]
    if (
        provider["model_spec_sha256"] != spec.sha256
        or value["run_id"] != cfg.run_id
        or value["plan_sha256"] != job["plan_sha256"]
        or value["plan_sha256"] != cfg.plan_sha256
        or spec.contract.describe() != job["contract"].describe()
        or value["deadlines_ns"]
        != {
            "scene": cfg.scene_timeout_ns,
            "reset_final": cfg.final_judge_timeout_ns,
            "reset_active": cfg.judge_request_timeout_ns,
        }
    ):
        raise ValueError("model spec differs from frozen job run/plan/ISC/deadlines")
    if cfg.termination.allow_early_stop and signals is None:
        raise ValueError(
            "model worker has no frame-bearing events; freeze a new plan with allow_early_stop:false"
        )
    return spec


def modules(binding):
    root = Path(binding["openpi_root"])
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "packages" / "openpi-client" / "src"))
    client = importlib.import_module("examples.fr3_local.run_robotiq_client")
    health = importlib.import_module("examples.fr3_local.fr3_health")
    env = importlib.import_module("examples.fr3_local.fr3_env")
    online = importlib.import_module("examples.fr3_local.levi_online_judge")
    import cv2
    from scipy.spatial.transform import Rotation

    return client, health, env, online, cv2, Rotation


def execute(job, binding, *, load_modules=modules):
    # Stable across run ids, rollout roots, worktrees and ports 5000/5001.
    with RobotLock():
        return _execute_locked(job, binding, load_modules=load_modules)


def _execute_locked(job, binding, *, load_modules):
    cfg = replace(
        job["config"], specs={**job["config"].specs, **job["contract"].spec()}
    )
    directory = Path(job["rollout_root"]) / ".aeri" / "runs" / cfg.run_id
    if directory.exists():
        raise ValueError(
            "this run already exists; hardware replay is refused; use a new reviewed run id"
        )
    client, health, env_module, online, cv2, rotation = load_modules(binding)
    args = client.Args()
    for name, value in binding["safety"].items():
        if name != "home_tolerance_mm":
            setattr(args, name, value)
    args.control_hz = 1e9 / cfg.control_period_ns
    args.view_camera_id, args.wrist_camera_id = (
        binding["cameras"]["side"],
        binding["cameras"]["wrist"],
    )
    args.franka_server_url = binding["robot_url"]
    args.require_matching_server = True
    deployments = job["plan"]["deployments"]
    clients = {}
    # Every deployed role is checked before constructing cameras or any arm I/O.
    for role, endpoint in binding["policies"].items():
        clients[role] = client.websocket_client_policy.WebsocketClientPolicy(
            endpoint["host"], endpoint["port"]
        )
        metadata = clients[role].get_server_metadata()
        bridge.check_identity(deployments[role], metadata)
        client._check_server(metadata, args)
        client._check_prompt(job["texts"][role], args, metadata)
    judge = online.OnlineJudge(
        online.fetch_spec(binding["providers"]["online_url"]),
        timeout_s=cfg.final_judge_timeout_ns / 1e9,
    )
    clock, fence = bridge.Clock(), sm.MotionFence()
    guard = health.Fr3Guard(health.GuardConfig(health_file=binding["health_file"]))
    if guard.check(force=True).status != "ok":
        raise ValueError("fresh green health monitor required before RobotEnv")
    env = env_module.RobotEnv(
        action_space="pose_euler",
        gripper_action_space="position",
        camera_kwargs=client.build_camera_kwargs(args),
        do_reset=False,
        server_url=binding["robot_url"],
        post_step_verify_delay=0.0,
    )
    try:
        guard.attach(env)
        return assembled(
            job,
            binding,
            client,
            online,
            cv2,
            rotation,
            env,
            guard,
            args,
            clock,
            fence,
            cfg,
            clients,
            judge,
        )
    finally:
        env.close()


def assembled(
    job,
    binding,
    client,
    online,
    cv2,
    rotation,
    env,
    guard,
    args,
    clock,
    fence,
    cfg,
    clients,
    judge,
):
    deployments = job["plan"]["deployments"]
    signals = signal_binding(binding.get("signal_events"))
    base_robot = bridge.Robot(
        env,
        guard,
        client,
        args,
        clock,
        fence,
        tolerance_mm=binding["safety"]["home_tolerance_mm"],
    )
    orch = None
    robot = base_robot
    if signals is not None:
        from .event_window import EventObserver

        # ctx.last_step is the actual current control-loop step, not a guessed
        # pose/action counter (which also counts homing and reset motion).
        robot = EventObserver(
            base_robot,
            clock,
            step_of=lambda: (
                orch.ctx.last_step if orch is not None and orch.ctx is not None else -1
            ),
        )
    from .fr3_media import Media, Recorder

    media = Media(cv2, rotation, online, judge, args.control_hz)
    if signals is not None:
        media.use_event_source(
            robot,
            lambda: orch.ctx if orch is not None else None,
            signals[2],
            cfg.run_id,
        )
    directory = Path(job["rollout_root"]) / ".aeri" / "runs" / cfg.run_id
    recorder = Recorder(
        job["rollout_root"],
        run_id=cfg.run_id,
        run_dir=directory,
        group=job["group"],
        texts=job["texts"],
        media=media,
    )
    file_root = binding["providers"]["file_root"]
    model_spec = model_binding(binding["providers"], job, signals=signals)
    model_kwargs = {"model_spec": model_spec} if model_spec is not None else {}

    def capture():
        obs = robot.observe()
        out = {}
        for view, pixels in obs["images_bgr"].items():
            ok, data = cv2.imencode(".jpg", pixels)
            if ok:
                out[view] = data.tobytes()
        return out

    if cfg.scene_check == "operator_attested":
        scene = human.HumanSceneProvider(
            job["contract"],
            capture,
            human.FileTransport(directory / "scene"),
            clock,
            cfg.run_id,
        )
    else:
        scene = providers.FileProvider(
            file_root, "scene", robot, clock, job["contract"], **model_kwargs
        )
    reset_provider = (
        providers.FileProvider(
            file_root, "reset_judgement", robot, clock, job["contract"], **model_kwargs
        )
        if "reset" in clients
        else None
    )
    sessions = SessionFiles(
        job["rollout_root"],
        run_id=cfg.run_id,
        group=job["group"],
        folders=cli.session_folders(cfg),
        texts=job["texts"],
        policies=deployments,
    )
    policy = bridge.Policy(
        clients, deployments, job["texts"], robot, client, args, clock
    )
    events = (
        providers.FileEvents(file_root, cfg.run_id, clock) if signals is None else None
    )
    if signals is not None:
        from .adapters.signal_events import SignalEvents

        profile, bindings, config, actor = signals

        def unattached_evidence(*args):
            raise ValueError("evidence store not attached")

        events = SignalEvents(
            robot,
            profile,
            bindings,
            run_id=cfg.run_id,
            clock=clock,
            evidence_root=directory / "evidence",
            keep_frame=unattached_evidence,
            config=config,
            actor_id=actor,
        )
    finished = threading.Event()
    old_signals = {}
    thread = None
    tracking = None
    pump = None
    try:
        orch = Orchestrator.create(
            directory,
            cfg,
            plan=job["plan"],
            robot=robot,
            policy=policy,
            recorder=recorder,
            events=events,
            verifier=providers.OnlineVerifier(
                online, judge, clock, media, job, reset_provider, robot=robot
            ),
            scene=scene,
            clock=clock,
            fence=fence,
            listener=sessions,
        )
        if job.get("standalone_inputs") is not None:
            tracking = launch.register_standalone(job, job["standalone_inputs"])
            from .runner import Flag

            tracked_runner = SimpleNamespace(
                run_dir=directory,
                run_id=cfg.run_id,
                orch=orch,
                path=None,
                sigterm=Flag(),
                wake=threading.Event(),
                started=tracking["created_wall_ns"],
            )
            launch.standalone_status(tracking, "running", state_of_run=orch.state)
            pump = control.CommandPump(tracked_runner)
            pump.start()
        if signals is not None:
            if orch.evidence is None:
                raise ValueError("signal events require the real run evidence store")
            events.evidence_root = orch.evidence.folder
            events.keep_frame = orch.evidence.keep_frame
        if reset_provider is not None:
            reset_provider.use_evidence(orch.evidence)

        signal_stop = {"signum": None}
        base_robot.stop_probe = lambda: signal_stop["signum"] is not None

        def stop(signum, frame):
            # Python signal handlers may interrupt a non-reentrant lock.
            # Record a plain flag only; every motion route sees it immediately.
            signal_stop["signum"] = signum

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_signals[signum] = signal.signal(signum, stop)

        def heartbeat():
            last = time.monotonic()
            delivered = False
            while not finished.wait(0.05):
                if signal_stop["signum"] is not None and not delivered:
                    robot.emergency_hold()
                    orch.stop("signal-" + str(signal_stop["signum"]))
                    delivered = True
                if time.monotonic() - last < 2:
                    continue
                last = time.monotonic()
                try:
                    sessions.heartbeat()
                    if tracking is not None:
                        launch.standalone_status(
                            tracking, "running", state_of_run=orch.state
                        )
                except Exception as exc:  # noqa: BLE001 - no stale live session while moving
                    robot.emergency_hold()
                    orch.stop("session-heartbeat-failed")
                    print("Session heartbeat failed: " + str(exc), file=sys.stderr)
                    return

        thread = threading.Thread(
            target=heartbeat, daemon=True, name="aeri-C2-heartbeat"
        )
        thread.start()
        while True:
            state = orch.run()
            if state != "WAIT_HUMAN" or robot.stopped():
                return state
            print(
                "WAIT_HUMAN: scene/health require operator review. "
                "Type resume after handling the environment and rechecking health, or stop."
            )
            if not sys.stdin.isatty():
                return state
            if tracking is not None:
                # The terminal and page share the original command/state core.
                # Do not hold this driver inside input() after a page resumes.
                while orch.state == "WAIT_HUMAN" and not robot.stopped():
                    ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if ready:
                        break
                if orch.state != "WAIT_HUMAN":
                    continue
            answer = input("> ").strip()
            if robot.stopped() or answer != "resume":
                orch.stop("terminal-stop")
                return orch.state
            control.resume_checked(
                orch,
                "terminal-resume-" + str(time.time_ns()),
                expected_seq=orch.journal.next_seq,
                environment_handled=True,
                health_rechecked=robot.health()["state"] == "ok",
            )
    finally:
        finished.set()
        robot.emergency_hold()
        if thread is not None:
            thread.join(timeout=3)
        if pump is not None:
            pump.close()
        cleanup_error = None
        operations = ([fence.revoke, orch.close] if orch is not None else []) + [
            recorder.close
        ]
        if tracking is not None:
            operations.append(
                lambda: launch.standalone_status(
                    tracking, "exited", final_state=orch.state
                )
            )
        for operation in operations:
            try:
                operation()
            except BaseException as exc:  # noqa: BLE001 - attempt every owned resource
                cleanup_error = cleanup_error or exc
        for signum, previous in old_signals.items():
            signal.signal(signum, previous)
        if cleanup_error is not None:
            if sys.exc_info()[0] is None:
                raise cleanup_error
            print("Cleanup failed: " + str(cleanup_error), file=sys.stderr)


def recipe(job_path):
    """Read-only recipe for a retained wizard/campaign job; never arm it."""
    job = cli.load_job(job_path)
    blockers = [
        "reviewed_fr3_binding_required",
        "operator_terminal_confirmation_required",
        "physical_validation_not_performed",
        "online_C5_provider_required",
    ]
    missing = []
    if not job["rollout_root"]:
        missing.append("absolute_rollout_root_required")
    if job["contract"] is None or job["contract"].status != "confirmed":
        missing.append("confirmed_initial_state_contract_required")
    if job["mode"] == "shadow":
        missing.append("shadow_has_no_motion_authority")
    roles = (
        ("forward",)
        if job["config"].reset_strategy == "human_assisted"
        else ("forward", "reset")
    )
    for role in roles:
        deployment = job["plan"].get("deployments", {}).get(role, {})
        try:
            bridge.checkpoint_path(deployment.get("checkpoint_dir"))
        except ValueError:
            missing.append(role + "_deployment_identity_required")
    if job["config"].scene_check == "provider":
        blockers.append("real_scene_provider_required")
    if "reset" in roles:
        blockers += [
            "real_reset_goal_provider_required",
            "reset_policy_deployment_review_required",
        ]
        cfg = job["config"]
        if cfg.reset_horizon_completion == "verify_final":
            blockers.append("reviewed_pose_stability_capability_required")
        elif not cfg.termination.allow_early_stop:
            missing.append("automatic_bounded_reset_requires_new_verify_final_plan")
        else:
            blockers.append("real_event_provider_required")
    entrypoint = "python -B -m levi.automatic.fr3_run"
    source = (
        entrypoint
        + " --config "
        + shlex.quote(str(Path(job_path).resolve()))
        + " --binding '<reviewed-binding.json>'"
    )
    return {
        "entrypoint": entrypoint,
        "check_command": source + " --check",
        "execute_command_template": source
        + " --execute --expect-plan "
        + job["plan_sha256"]
        + " --expect-binding '<binding-sha256-from-check>'",
        "plan_sha256": job["plan_sha256"],
        "hardware_verified": False,
        "manual_review_required": True,
        "blockers": missing + blockers,
        "requires_new_plan": bool(missing),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--binding")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expect-plan")
    parser.add_argument("--expect-binding")
    parser.add_argument(
        "--scene-question", help="answer one real captured human scene question"
    )
    parser.add_argument(
        "--predicates",
        help="JSON mapping of required predicate names to true/false/null",
    )
    args = parser.parse_args(argv)
    try:
        if args.scene_question:
            if any(
                (
                    args.check,
                    args.execute,
                    args.config,
                    args.binding,
                    args.expect_plan,
                    args.expect_binding,
                )
            ):
                raise ValueError(
                    "scene-question cannot be combined with check/execute/config/binding"
                )
            question_path = Path(args.scene_question)
            question = json.loads(question_path.read_text())
            if question_path.parent.name != "questions" or not args.predicates:
                raise ValueError(
                    "scene-question must name a current scene/questions file and predicates"
                )
            predicates = json.loads(args.predicates)
            human.write_answer(question_path.parent.parent, question, predicates)
            return 0
        if not args.config or not args.binding or args.check == args.execute:
            raise ValueError(
                "choose exactly one of --check and --execute; supply --config and --binding"
            )
        job = cli.load_job(args.config)
        _binding, digest = load_binding(args.binding, job)
        if args.check:
            print(
                json.dumps(
                    {
                        "plan_sha256": job["plan_sha256"],
                        "binding_sha256": digest,
                        "reset_strategy": job["config"].reset_strategy,
                        "hardware_verified": False,
                        "event_source": "signals"
                        if _binding.get("signal_events") is not None
                        else "files",
                        "signal_events": _binding.get("signal_events"),
                    },
                    indent=2,
                )
            )
            return 0
        if args.expect_plan != job["plan_sha256"] or args.expect_binding != digest:
            raise ValueError(
                "job/binding digests must match the reviewed --check output"
            )
        if not sys.stdin.isatty():
            raise ValueError("real execution requires an operator terminal")
        phrase = "execute " + job["config"].run_id
        print(
            "This connects to the robot and may move it. Confirm the reviewed scene, limits, "
            "checkpoints, active providers and hardware readiness."
        )
        if input("Type " + phrase + ": ").strip() != phrase:
            raise ValueError("operator did not authorize motion")
        # Re-read after confirmation: operator waiting cannot hide a changed plan.
        checked_job_fact = launch.job_fact(str(Path(args.config).resolve()))
        checked = cli.load_job(args.config)
        checked_binding, checked_digest = load_binding(args.binding, checked)
        if (
            launch.job_fact(str(Path(args.config).resolve())) != checked_job_fact
            or checked["plan_sha256"] != args.expect_plan
            or checked_digest != args.expect_binding
        ):
            raise ValueError("configuration changed during operator confirmation")
        if launch.index_path(checked["config"].run_id).exists():
            raise ValueError(
                "run id is already registered; real runs cannot be replayed or attached"
            )
        checked["standalone_inputs"] = {
            "job": checked_job_fact,
            "binding_sha256": checked_digest,
        }
        return 0 if execute(checked, checked_binding) == "COMPLETED" else 1
    except (
        ValueError,
        TypeError,
        OSError,
        cli.JobError,
        cli.sa.ContractError,
        launch.LaunchRefused,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
