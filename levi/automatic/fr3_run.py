"""Explicit operator entry: python -m levi.automatic.fr3_run --help.

--check is read-only and imports no hardware modules. --execute needs a typed
confirmation at a terminal, immutable job/binding digests and real providers.
No deployment/service launch is performed, except that a binding declaring
``policy_swap`` lets this run start and stop its own reviewed policy units
(``fr3_policy_swap``), one at a time. This is separate from the HTTP launch
runner, whose real-mode refusal remains in force.
"""

import argparse
import contextlib
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

from . import camera_meta, cli, control, launch
from . import console as aeri_console
from . import fr3_bridge as bridge
from . import fr3_policy_swap as swap
from . import fr3_providers as providers
from . import state_machine as sm
from .adapters import human
from .orchestrator import Orchestrator
from .recorder import SessionFiles

SCHEMA = "levi.aeri.fr3.binding.v1"
# Optional binding.safety keys: bounded waits only, never thresholds. Absent
# keys use the documented defaults in fr3_bridge.
OPTIONAL_SAFETY = (
    "home_settle_timeout_s",
    "settle_poll_s",
    "gripper_confirm_timeout_s",
)
# Optional binding.safety keys of the camera frame bus (CAMERA-FRAME-BUS.md).
# Absent: the old behaviour (no frame age check; the same-seq check uses
# camera_meta.DEFAULT_STALE_STEPS and only runs when openpi gives image_meta).
# max_frame_age_ms only tightens: openpi refuses a frame older than it.
CAMERA_SAFETY = ("max_frame_age_ms", "camera_stale_steps")
MAX_SETTLE_POLL_S = 1.0
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


def camera_kwargs(client, args, safety):
    """openpi's camera_kwargs; with ``safety.max_frame_age_ms`` set it is
    copied into every camera's own kwargs (openpi reads it per camera).
    Absent: exactly what ``build_camera_kwargs`` returns, as before."""
    found = client.build_camera_kwargs(args)
    if "max_frame_age_ms" not in safety:
        return found
    if not isinstance(found, dict) or not all(
        isinstance(v, dict) for v in found.values()
    ):
        raise ValueError("camera_kwargs must map each camera to its kwargs")
    limit = float(safety["max_frame_age_ms"])
    return {name: {**cfg, "max_frame_age_ms": limit} for name, cfg in found.items()}


def load_binding(path, job):
    if job["config"].termination.inference_mode == "event_pause_judge":
        raise ValueError(
            "event_pause_judge has no owned FR3 policy/barrier/physical-hold/evidence binding; real execution refused"
        )
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
    optional = {"signal_events", "policy_swap", "policy_warmup_timeout_s"}
    # subtask_loop: no scene provider, goal judge or event files; the local
    # VLM (``subtask_vlm``) plans and checks the subtasks instead.
    subtask = job["config"].reset_strategy == "subtask_loop"
    if subtask:
        required = (required - {"providers"}) | {"subtask_vlm"}
    if (
        not isinstance(value, dict)
        or not required <= set(value) <= required | optional
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
    warmup = value.get(
        "policy_warmup_timeout_s", bridge.DEFAULT_POLICY_WARMUP_TIMEOUT_S
    )
    if type(warmup) not in (int, float) or not math.isfinite(warmup) or warmup <= 0:
        raise ValueError("policy_warmup_timeout_s must be positive finite")
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
    if (
        not expected
        <= set(safety)
        <= expected | set(OPTIONAL_SAFETY) | set(CAMERA_SAFETY)
    ):
        raise ValueError(
            "safety requires exactly "
            + ", ".join(sorted(expected))
            + "; optional: "
            + ", ".join((*OPTIONAL_SAFETY, *CAMERA_SAFETY))
        )
    if "max_frame_age_ms" in safety:
        x = safety["max_frame_age_ms"]
        if (
            isinstance(x, bool)
            or not isinstance(x, int | float)
            or not math.isfinite(x)
            or not 0 < x <= camera_meta.MAX_FRAME_AGE_MS
        ):
            raise ValueError(
                "safety.max_frame_age_ms must be positive and at most "
                f"{camera_meta.MAX_FRAME_AGE_MS} ms"
            )
    if "camera_stale_steps" in safety:
        camera_meta.stale_steps(safety["camera_stale_steps"])
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
    for name in OPTIONAL_SAFETY:
        if name in safety:
            x = safety[name]
            if (
                isinstance(x, bool)
                or not isinstance(x, int | float)
                or not math.isfinite(x)
                or x <= 0
            ):
                raise ValueError("safety." + name + " must be positive finite")
    if safety.get("settle_poll_s", 0) > MAX_SETTLE_POLL_S:
        raise ValueError(f"safety.settle_poll_s must be at most {MAX_SETTLE_POLL_S} s")
    if safety["home_duration_s"] * 1e9 >= job["config"].home_timeout_ns:
        raise ValueError("home duration must be shorter than the frozen home timeout")
    settle = safety.get("home_settle_timeout_s", bridge.DEFAULT_HOME_SETTLE_TIMEOUT_S)
    if (safety["home_duration_s"] + settle) * 1e9 >= job["config"].home_timeout_ns:
        raise ValueError(
            "home_settle_timeout_s plus home_duration_s must be shorter than the "
            "frozen home timeout"
        )
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
        # subtask_loop: both loops run the evaluation policy (one service).
        if job["config"].reset_strategy in ("human_assisted", "subtask_loop")
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
    if "policy_swap" in value:
        if "policy_warmup_timeout_s" in value:
            raise ValueError(
                "policy_warmup_timeout_s does not apply with policy_swap: the "
                "switcher warms its own service; set policy_swap.warmup_timeout_s"
            )
        swap_config(value, job)
    if not job.get("rollout_root") or not Path(job["rollout_root"]).is_absolute():
        raise ValueError("recording.rollout_root must be frozen as an absolute path")
    if job["mode"] == "shadow":
        raise ValueError("shadow mode has no motion authority")
    if subtask:
        from . import subtask_vlm

        early = job["config"].subtask_early_check
        on_events = early is not None and early.on_events
        # The event stream feeds only subtask.early_check's triggers: it may
        # end an episode early for a check, never move the arm itself.
        if "signal_events" in value and not on_events:
            raise ValueError(
                "subtask_loop reads signal_events only for subtask.early_check "
                "on_events: true"
            )
        if on_events and "signal_events" not in value:
            raise ValueError(
                "subtask.early_check on_events: true needs the binding's "
                "signal_events (gripper/motion events); without them use "
                "every_steps only"
            )
        if on_events:
            signal_binding(value["signal_events"])
        subtask_vlm.binding(value["subtask_vlm"], job)
        return value, hashlib.sha256(data).hexdigest()
    if job["contract"] is None or job["contract"].status != "confirmed":
        raise ValueError("real execution requires a confirmed Initial State Contract")
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


def swap_config(binding, job):
    """The binding's reviewed policy switch, or None (the original contract:
    every role's server already runs). Pure file check; never runs a tool."""
    if "policy_swap" not in binding:
        return None
    roles = set(binding["policies"])
    services = (
        binding["policy_swap"].get("services")
        if isinstance(binding["policy_swap"], dict)
        else None
    )
    if roles not in ({"forward", "reset"}, {"forward"}) or (
        isinstance(services, dict) and set(services) != roles
    ):
        raise ValueError(
            "policy_swap needs the forward and reset roles of single_reset_policy, "
            "or the forward role alone of human_assisted; policy_swap.services must "
            "name exactly the binding's policy roles (policies: "
            + ", ".join(sorted(roles))
            + ")"
        )
    return swap.parse(
        binding["policy_swap"],
        policies=binding["policies"],
        deployments=job["plan"]["deployments"],
        safety=binding["safety"],
    )


def swap_unclean(binding, job):
    """Earlier switch logs of this rollout root that need the operator's
    check (empty without ``policy_swap``: the original contract is untouched)."""
    if "policy_swap" not in binding:
        return []
    return swap.pending(job["rollout_root"])


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


SCENE_TERMINAL_REQUIRED = (
    "a real run needs the operator terminal (stdin and stdout a terminal, "
    "{switch} not 0) whatever its scene check: answers on the page do not "
    "release motion (decision UI-2) and neither does a provider's answer "
    "alone (L2-UI2-PROVIDER-RELEASE), so the run is refused before any "
    "connection / 真机运行无论场景核对方式都必须有操作员终端（标准输入输出都是终端，"
    "且 {switch} 不为 0）：页面上的回答不算放行（取舍 UI-2），provider 的答案也不单独放行"
    "（L2-UI2-PROVIDER-RELEASE），运行在连接任何设备前被拒绝"
)
_FROM_ENVIRONMENT = object()


def _scene_terminal_required(cfg, operator) -> None:
    # Every scene check of a real run: operator_attested (UI-2) and provider
    # (L2-UI2-PROVIDER-RELEASE, decision 2026-10-15) alike.
    if operator is None:
        raise ValueError(SCENE_TERMINAL_REQUIRED.format(switch=aeri_console.ENV_SWITCH))


def execute(job, binding, *, load_modules=modules, swap_io=None):
    # Motion authorization must be frozen in the reviewed plan, not supplied
    # as an unfrozen binding flag or a changed in-memory config.
    enabled = job["config"].start_home
    frozen = job["plan"].get("run", {}).get("start_home", False)
    if type(frozen) is not bool or enabled is not frozen:
        raise ValueError("startup home differs from the frozen plan")
    # Stable across run ids, rollout roots, worktrees and ports 5000/5001.
    with RobotLock():
        return _execute_locked(job, binding, load_modules=load_modules, swap_io=swap_io)


def _switcher(switch, job, cfg, handshake, swap_io):
    """The policy switcher of a ``policy_swap`` binding. ``swap_io`` replaces
    systemd, ``/proc``, ``nvidia-smi`` and the clock in tests only."""
    io = swap_io or SimpleNamespace(
        systemd=None,
        proc_root=None,
        gpu=None,
        clock=time.monotonic,
        sleep=time.sleep,
        poll_s=0.5,
    )
    log = swap.SwapLog.open_for_run(
        swap.log_path(job["rollout_root"], cfg.run_id),
        run_id=cfg.run_id,
        units={role: s.unit for role, s in switch.services.items()},
    )
    return swap.PolicySwitcher(
        switch,
        log=log,
        handshake=handshake,
        run_id=cfg.run_id,
        systemd=io.systemd,
        proc_root=io.proc_root,
        cgroup_root=getattr(io, "cgroup_root", swap.CGROUP_ROOT),
        gpu=io.gpu,
        clock=io.clock,
        sleep=io.sleep,
        poll_s=io.poll_s,
        service_log_dir=Path(job["rollout_root"]) / swap.LOG_DIR,
    )


def _execute_locked(job, binding, *, load_modules, swap_io=None):
    contract = job["contract"]
    cfg = replace(
        job["config"],
        specs={**job["config"].specs, **(contract.spec() if contract else {})},
    )
    subtask = cfg.reset_strategy == "subtask_loop"
    directory = Path(job["rollout_root"]) / ".aeri" / "runs" / cfg.run_id
    if directory.exists():
        raise ValueError(
            "this run already exists; hardware replay is refused; use a new reviewed run id"
        )
    switch = swap_config(binding, job)
    seen = sorted(job.get("swap_acknowledged", []))
    if switch is not None and sorted(p for p, _ in swap_unclean(binding, job)) != seen:
        raise ValueError(
            "policy switch logs changed since the operator's confirmation; "
            "run --execute again"
        )
    # The console before any connection: without a terminal a real run's
    # scene would be answered on the page (UI-2) or by the provider alone.
    guard_box = []
    operator = aeri_console.from_environment(
        run_dir=directory, health_probe=lambda: guard_box[0].check().status == "ok"
    )
    _scene_terminal_required(cfg, operator)
    client, health, env_module, online, cv2, rotation = load_modules(binding)
    args = client.Args()
    for name, value in binding["safety"].items():
        if (
            name != "home_tolerance_mm"
            and name not in OPTIONAL_SAFETY
            and name
            not in CAMERA_SAFETY  # LEVI's own; reaches openpi only via camera_kwargs
        ):
            setattr(args, name, value)
    args.control_hz = 1e9 / cfg.control_period_ns
    args.view_camera_id, args.wrist_camera_id = (
        binding["cameras"]["side"],
        binding["cameras"]["wrist"],
    )
    args.franka_server_url = binding["robot_url"]
    args.require_matching_server = True
    deployments = job["plan"]["deployments"]

    def handshake(role):
        endpoint = binding["policies"][role]
        policy_client = client.websocket_client_policy.WebsocketClientPolicy(
            endpoint["host"], endpoint["port"]
        )
        metadata = policy_client.get_server_metadata()
        bridge.check_identity(deployments[role], metadata)
        client._check_server(metadata, args)
        client._check_prompt(job["texts"][role], args, metadata)
        return policy_client

    clients = {}
    switcher = None
    if switch is None:
        # Every deployed role is checked before constructing cameras or any arm I/O.
        for role in binding["policies"]:
            clients[role] = handshake(role)
    else:
        if seen:
            swap.acknowledge(seen, by_run=cfg.run_id)
        switcher = _switcher(switch, job, cfg, handshake, swap_io)
    services = None
    try:
        if subtask:
            services = _subtask_services(binding, job, cfg, operator, switch)
        if services is not None:
            # 1/7 preflight, 2/7 the VLM up, 3/7 the VLM asleep: before the
            # policy loads (the lower peak), and before any arm I/O.
            services.preflight()
            services.start_vlm()
            services.sleep_vlm()
        if switcher is not None:
            # AERI starts and checks the forward service before any arm I/O.
            clients["forward"] = switcher.startup("forward")
        if services is not None:
            services.policy_ready()
            services.rounds()
        judge = (
            None
            if subtask
            else online.OnlineJudge(
                online.fetch_spec(binding["providers"]["online_url"]),
                timeout_s=cfg.final_judge_timeout_ns / 1e9,
            )
        )
        clock, fence = bridge.Clock(), sm.MotionFence()
        guard = health.Fr3Guard(health.GuardConfig(health_file=binding["health_file"]))
        guard_box.append(guard)
        if guard.check(force=True).status != "ok":
            raise ValueError("fresh green health monitor required before RobotEnv")
        env = env_module.RobotEnv(
            action_space="pose_euler",
            gripper_action_space="position",
            camera_kwargs=camera_kwargs(client, args, binding["safety"]),
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
                # The original call is unchanged without a policy_swap binding.
                **({} if switcher is None else {"switcher": switcher}),
                operator=operator,
                **({} if services is None else {"services": services}),
            )
        finally:
            env.close()
    finally:
        policy_stopped = True
        if switcher is not None:
            try:
                _stop_policy_services(switcher)
            except BaseException:
                policy_stopped = False
                if services is None:
                    raise
        if services is not None:
            # 7/7: our VLM (identity checked), the release check, the lock.
            with contextlib.suppress(Exception):
                services.shutdown(policy_stopped=policy_stopped)


def _subtask_services(binding, job, cfg, operator, switch):
    """Terminal D's own VLM unit and the numbered guide, when the binding's
    ``subtask_vlm.service`` asks for it (else None: the VLM is served by
    someone else on the bound port)."""
    from . import subtask_units, subtask_vlm

    vlm_binding = subtask_vlm.binding(binding["subtask_vlm"], job)
    say = operator.say if operator is not None else print
    texts = (
        operator.t if operator is not None else aeri_console.console_text.Texts("en")
    )
    service = switch.services.get("forward") if switch is not None else None
    root = Path(job["rollout_root"]) / ".aeri" / "units"
    return subtask_units.for_run(
        vlm_binding,
        policy_unit=getattr(service, "unit", None),
        policy_port=binding["policies"]["forward"]["port"],
        say=say,
        t=texts,
        record_dir=root,
        log_path=root / f"{cfg.run_id}-steps.jsonl",
        profile=(job["plan"].get("subtask") or {}).get("vlm") or {},
        min_mem_gib=cfg.vlm_min_free_mem_gib,
    )


def _stop_policy_services(switcher):
    """Stop this run's policy unit and confirm it released port, processes and
    GPU memory. A failure never hides the error already propagating."""
    inflight = sys.exc_info()[0] is not None
    try:
        switcher.shutdown()
    except BaseException as exc:
        if not inflight:
            raise
        print("Policy service shutdown not confirmed: " + str(exc), file=sys.stderr)


def operator_text(key, **fields):
    """A sentence for the person at the terminal, in the console language
    (``console_text``; ``LEVI_AERI_CONSOLE_LANG``). Errors raised for logs and
    tests stay as they are; what is printed to the operator comes from here."""
    return aeri_console.console_text.Texts(
        aeri_console.console_text.language(os.environ)
    )(key, **fields)


def _flush_terminal_input():
    """Drop what was typed on stdin before a prompt (a terminal only): the
    old WAIT_HUMAN path reads lines, and a line typed ahead must not answer.

    Relies on the terminal's canonical mode (ICANON): there the kernel hands
    over one line per read, so the lines typed ahead are still in the kernel
    queue that ``TCIFLUSH`` empties. Without ICANON Python's own buffer may
    already hold them; ``_warn_if_not_canonical`` says so on the terminal."""
    with contextlib.suppress(Exception):
        if sys.stdin.isatty():
            aeri_console.StdinKeys(sys.stdin.fileno()).flush()


def _warn_if_not_canonical():
    """A terminal stdin without ICANON: the flush may miss lines typed ahead
    (see ``_flush_terminal_input``); warn once per wait. Unknown: no warning."""
    with contextlib.suppress(Exception):
        import termios

        if not termios.tcgetattr(sys.stdin.fileno())[3] & termios.ICANON:
            print(operator_text("legacy_not_canonical"), file=sys.stderr)


# What ``_legacy_line`` returns when no line answers the wait.
LEGACY_MOVED = object()  # the page resumed, the state or the journal moved on
LEGACY_STOPPED = object()  # a signal or the stop probe stopped the robot


def _legacy_line(orch, robot, expected_seq, tracking):
    """One typed line for the old WAIT_HUMAN path, read only while the wait
    it answers is still the one shown: ``orch`` in WAIT_HUMAN at
    ``expected_seq`` and the robot not stopped. In tracking mode (always, from
    main()) the wait polls ``select`` so a page resume, a state change, a new
    journal line, the stop probe or a signal end it without a blocking
    input(). End-of-input is the empty line (a stop, never a resume)."""

    def current():
        return orch.state == "WAIT_HUMAN" and orch.journal.next_seq == expected_seq

    if tracking:
        while current() and not robot.stopped():
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
            if ready:
                break
    if robot.stopped():
        return LEGACY_STOPPED
    if not current():
        return LEGACY_MOVED
    try:
        answer = input("> ").strip()
    except EOFError:
        answer = ""
    if robot.stopped():
        return LEGACY_STOPPED
    return answer


def _legacy_answer(orch, robot, tracking):
    """The old WAIT_HUMAN path: ``"resume"`` (resume at the returned
    sequence), ``"stop"`` or ``"moved"`` (the wait is over without this
    terminal: run the driver again). ``resume`` resumes; any other line,
    an empty one or end-of-input stops. The waits of
    ``console.CONFIRM_REASONS`` (user decision 2026-10-14) need one more
    line in ``console.CONFIRM_KEYS``, typed after a second flush."""
    _warn_if_not_canonical()
    # Lines typed while the run moved (a "resume" typed ahead) must not
    # answer this wait: drop them, then fix the wait being answered.
    _flush_terminal_input()
    expected_seq = orch.journal.next_seq
    answer = _legacy_line(orch, robot, expected_seq, tracking)
    if answer is LEGACY_MOVED:
        return "moved", expected_seq
    if answer is LEGACY_STOPPED or answer != "resume":
        return "stop", expected_seq
    reason = aeri_console.wait_reason(orch)
    if reason in aeri_console.CONFIRM_REASONS:
        print(operator_text("legacy_confirm_resume", reason=reason))
        _flush_terminal_input()
        answer = _legacy_line(orch, robot, expected_seq, tracking)
        if answer is LEGACY_MOVED:
            return "moved", expected_seq
        if answer is LEGACY_STOPPED or answer not in aeri_console.CONFIRM_KEYS:
            return "stop", expected_seq
    if orch.state != "WAIT_HUMAN":
        return "moved", expected_seq
    return "resume", expected_seq


def start_gate(base_robot, emit=None, *, allow_start_home=False):
    """Refuse to start unless the arm already stands at the bound home pose.

    Read-only: the arm is never moved here; a person decides how to bring it
    home. Runs before any session, recorder or journal exists, so a refusal
    leaves nothing behind and the run id stays unused."""
    emit = emit or (lambda text: print(text, file=sys.stderr, flush=True))
    start = base_robot.start_pose()
    if not start["ok"]:
        if (
            allow_start_home is True
            and start.get("unreadable_reason") is None
            and start.get("unmet")
            and set(start["unmet"])
            <= {"position_tolerance", "rotation_tolerance", "gripper_not_open"}
            and not base_robot.stopped()
            and not base_robot.latch_state()
            and base_robot.health()["state"] == "ok"
        ):
            return {
                "start_pose_checked": "explicit startup home confirmation required: "
                + start["text"]
            }
        raise ValueError(
            "start pose refused: the arm is not at the bound home_pose within the "
            "home tolerance with the gripper open ("
            + start["text"]
            + "). No motion command was sent; no run ID was consumed. "
            "Homing opens the gripper: first check and safely handle any held object "
            "and confirm the return path is clear. This check does not home the arm "
            "or clear a fault latch. Follow the operator recovery procedure before "
            "bringing the arm home with the gripper open, then retry the start."
        )
    emit("Start pose verified: " + start["text"])
    return {"start_pose_checked": start["text"]}


def print_fault(orch, directory):
    """FAULT_LOCKED must say why on the terminal: reason code, the latest
    note details (raw exception and gripper reply text) and the journal path."""
    reason = str(getattr(orch, "fault_reason", None) or orch.halted or "unknown")
    lines = [operator_text("plain_fault_head", reason=reason)]
    # Only operational notes and the policy error text (cut to 300
    # characters): a judgement note carries the final verdict.
    for code, detail in aeri_console.shown_notes(orch.notes)[-8:]:
        if code in aeri_console.POLICY_ERROR_NOTES:
            lines.append(operator_text("policy_error_detail", code=code, detail=detail))
        else:
            lines.append(f"  {code}: {detail}")
    lines.append(
        operator_text("plain_fault_log", path=Path(directory) / "state_journal.jsonl")
    )
    print("\n".join(lines), file=sys.stderr, flush=True)


def warm_forward_at_start(policy, switcher, env, binding):
    """The one warm-up of the forward service before any session exists.

    A service somebody else runs is warmed through ``Policy.warmup`` (identity
    and prompt re-check, bounded by ``policy_warmup_timeout_s``). A service this
    run started itself (``policy_swap``) is warmed by the switcher, whose
    ``ensure`` also checks the actions, the motion count and the motion fence;
    ``Policy.warmup`` is not run on top of it, so the service is queried once,
    not twice. Either way a refusal comes back as ``{"ok": False, "detail"}``.
    Services the switcher starts later are warmed inside the switch."""
    if switcher is None:
        return policy.warmup(
            "forward",
            raw=env.get_observation(),
            timeout_s=binding.get(
                "policy_warmup_timeout_s", bridge.DEFAULT_POLICY_WARMUP_TIMEOUT_S
            ),
        )
    began = time.monotonic()
    try:
        switcher.ensure("forward")
    except swap.SwapError as exc:
        return {"ok": False, "detail": str(exc), "duration_s": time.monotonic() - began}
    return {"ok": True, "duration_s": time.monotonic() - began}


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
    *,
    switcher=None,
    operator=_FROM_ENVIRONMENT,
    services=None,
):
    deployments = job["plan"]["deployments"]
    signals = signal_binding(binding.get("signal_events"))
    subtask = cfg.reset_strategy == "subtask_loop"
    base_robot = bridge.Robot(
        env,
        guard,
        client,
        args,
        clock,
        fence,
        tolerance_mm=binding["safety"]["home_tolerance_mm"],
        **{k: binding["safety"][k] for k in OPTIONAL_SAFETY if k in binding["safety"]},
        **(
            {"camera_stale_steps": binding["safety"]["camera_stale_steps"]}
            if "camera_stale_steps" in binding["safety"]
            else {}
        ),
        # With a frame age limit the observation must say how old its frames
        # are; an openpi without image_meta would ignore the limit silently.
        require_image_meta="max_frame_age_ms" in binding["safety"],
    )
    gate_notes = start_gate(base_robot, allow_start_home=cfg.start_home)
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

    def prompt_allowed(text):
        # LEVI's own check of an episode prompt (subtask_loop): a template
        # text of the run's frozen whitelist, nothing else.
        from . import subtask_loop as st

        sub = getattr(orch, "sub", None)
        return sub is not None and st.instruction_allowed(text, sub.inventory)

    allow = {"prompt_allowed": prompt_allowed} if subtask else {}
    if switcher is None:
        policy = bridge.Policy(
            clients, deployments, job["texts"], robot, client, args, clock, **allow
        )
    else:

        def pose():
            state = env.get_observation()["robot_state"]
            return [float(x) for x in state["cartesian_position"]]

        def switch_warmup(role, policy_client):
            # One inference on the current observation; the actions are dropped.
            raw = env.get_observation()
            return client._query_policy(
                policy_client,
                raw,
                raw["robot_state"]["cartesian_position"],
                job["texts"][role],
            )

        switcher.bind_robot(
            swap.RobotSide(
                pose=pose,
                home_pose=binding["safety"]["home_pose"],
                home_tolerance_mm=binding["safety"]["home_tolerance_mm"],
                fence=fence,
                hold=base_robot.hold,
                motion_count=lambda: base_robot.steps,
                warmup=switch_warmup,
                cancelled=lambda: (
                    base_robot.stopped() or (orch is not None and orch.stop_requested())
                ),
            )
        )
        policy = swap.SwitchingPolicy(
            switcher,
            clients,
            deployments,
            job["texts"],
            robot,
            client,
            args,
            clock,
            **allow,
        )
    warmup = warm_forward_at_start(policy, switcher, env, binding)
    if not warmup["ok"]:
        raise ValueError("policy warm-up refused: " + warmup["detail"])
    gate_notes.update(
        start_gate(base_robot, allow_start_home=cfg.start_home)
    )  # The pose can change during warm-up.
    gate_notes["policy_warmup"] = (
        f"forward warm-up completed; duration_s={warmup['duration_s']:.3f}; actions discarded"
    )
    from .fr3_media import Media, Recorder

    if subtask:
        from . import subtask_vlm

    media = Media(
        cv2,
        rotation,
        online,
        subtask_vlm.NoJudge() if subtask else judge,
        args.control_hz,
    )
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
    file_root = None if subtask else binding["providers"]["file_root"]
    model_spec = (
        None if subtask else model_binding(binding["providers"], job, signals=signals)
    )
    model_kwargs = {"model_spec": model_spec} if model_spec is not None else {}

    def capture():
        obs = robot.observe()
        out = {}
        for view, pixels in obs["images_bgr"].items():
            ok, data = cv2.imencode(".jpg", pixels)
            if ok:
                out[view] = data.tobytes()
        return out

    # None unless stdin and stdout are terminals: then the old paths below run.
    if operator is _FROM_ENVIRONMENT:
        operator = aeri_console.from_environment(
            run_dir=directory, health_probe=lambda: guard.check().status == "ok"
        )
    # Again here (``execute`` refused already): never a page-only person check.
    _scene_terminal_required(cfg, operator)
    subtask_parts = {}
    if subtask and services is None:
        raise ValueError("subtask_loop runs only with terminal D's own VLM unit")
    if subtask:
        # No scene check: terminal D releases each round (operator.release_round).
        scene = subtask_vlm.Refused()

        def jpeg(pixels):
            ok, data = cv2.imencode(".jpg", pixels)
            if not ok:
                raise ValueError("frame could not be encoded")
            return data.tobytes()

        vlm_binding = subtask_vlm.binding(binding["subtask_vlm"], job)
        subtask_parts = {
            "subtask_vlm": subtask_vlm.VllmSubtaskVlm(
                vlm_binding["base_url"],
                vlm_binding["model"],
                observe=robot.observe,
                encode=jpeg,
                timeout_s=vlm_binding["timeout_s"],
                extra_body=vlm_binding["extra_body"],
                inventory_max_tokens=vlm_binding["inventory_max_tokens"],
                # Image cap and optional earlier frames (default: 2, none).
                max_images=vlm_binding["max_images"],
                history_frames=vlm_binding["history_frames"],
                recent=getattr(robot, "recent_frames", None),
            ),
            # Terminal D's own unit's power (ownership-checked, R15).
            "vlm_power": services.power,
            "gate": operator,
        }
    elif cfg.scene_check == "operator_attested":
        scene = aeri_console.human_scene(
            operator,
            job["contract"],
            capture,
            human.FileTransport(directory / "scene"),
            clock,
            cfg.run_id,
        )
    else:
        # The provider answers for reference; the terminal's y/n/u releases
        # each round (L2-UI2-PROVIDER-RELEASE).
        scene = aeri_console.confirmed_scene(
            operator,
            providers.FileProvider(
                file_root, "scene", robot, clock, job["contract"], **model_kwargs
            ),
            job["contract"],
            capture,
            human.FileTransport(directory / "scene"),
            clock,
            cfg.run_id,
            specs={**cfg.specs, **job["contract"].spec()},
            model_timeout_ns=cfg.scene_timeout_ns,
        )
    reset_provider = (
        providers.FileProvider(
            file_root, "reset_judgement", robot, clock, job["contract"], **model_kwargs
        )
        if "reset" in binding["policies"] and not subtask
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
    if subtask:
        events = subtask_vlm.NoEvents()
    else:
        events = (
            providers.FileEvents(file_root, cfg.run_id, clock)
            if signals is None
            else None
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
    signal_stop = {"signum": None}
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
            verifier=subtask_vlm.Refused()
            if subtask
            else providers.OnlineVerifier(
                online, judge, clock, media, job, reset_provider, robot=robot
            ),
            scene=scene,
            clock=clock,
            fence=fence,
            listener=aeri_console.tee(operator, sessions),
            **subtask_parts,
        )
        if subtask:
            subtask_parts["subtask_vlm"].use_evidence(orch.evidence)
            # The service steps so far (1/7-6/7) go into the journal.
            for record in services.records if services is not None else ():
                orch.journal.note(
                    "subtask_service_step",
                    json.dumps(record, default=str)[:300],
                    authority=orch._auth(),
                )
        for code, text in gate_notes.items():
            orch.note(code, text)
        if operator is not None:
            operator.bind(orch, robot, directory, lambda: signal_stop["signum"])
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

        base_robot.stop_probe = lambda: (
            signal_stop["signum"] is not None
            or (orch.startup_home_active and orch.startup_stop_seen.is_set())
        )

        def stop(signum, frame):
            # Python signal handlers may interrupt a non-reentrant lock.
            # Record a plain flag only; every motion route sees it immediately.
            signal_stop["signum"] = signum

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_signals[signum] = signal.signal(signum, stop)

        heartbeat_ready = threading.Event()

        def heartbeat():
            if cfg.start_home:
                try:
                    sessions.heartbeat()
                except Exception:  # noqa: BLE001 - no startup motion without heartbeat
                    robot.emergency_hold()
                    orch.stop("startup-heartbeat-failed")
                    return
            heartbeat_ready.set()
            last = time.monotonic()
            delivered = False
            while not finished.wait(0.05):
                if signal_stop["signum"] is not None and not delivered:
                    robot.emergency_hold()
                    orch.stop("signal-" + str(signal_stop["signum"]))
                    delivered = True
                    if operator is not None:
                        operator.interrupted(signal_stop["signum"])
                poll = getattr(operator, "poll_verdict", None)
                if callable(poll) and signal_stop["signum"] is None:
                    # Terminal D's s/f/d while a forward round runs: one
                    # non-blocking key read. A failure here only loses keys.
                    with contextlib.suppress(Exception):
                        poll()
                if time.monotonic() - last < 2:
                    continue
                last = time.monotonic()
                try:
                    if operator is not None:
                        operator.tick()
                    sessions.heartbeat()
                    if tracking is not None:
                        launch.standalone_status(
                            tracking, "running", state_of_run=orch.state
                        )
                except Exception as exc:  # noqa: BLE001 - no stale live session while moving
                    robot.emergency_hold()
                    orch.stop("session-heartbeat-failed")
                    print(operator_text("heartbeat_failed", why=exc), file=sys.stderr)
                    return

        thread = threading.Thread(
            target=heartbeat, daemon=True, name="aeri-C2-heartbeat"
        )
        thread.start()
        if cfg.start_home:
            # The callback is live evidence, not a persisted readiness flag.
            orch.startup_home_ready = lambda: (
                operator is not None
                and len(old_signals) == 2
                and heartbeat_ready.is_set()
                and thread.is_alive()
                and not finished.is_set()
                and signal_stop["signum"] is None
            )
            heartbeat_ready.wait(timeout=1)
        while True:
            state = orch.run()
            if state != "WAIT_HUMAN" or robot.stopped():
                return state
            if operator is not None:
                if operator.wait_human():
                    continue
                return orch.state
            print(operator_text("legacy_wait_human"))
            if not sys.stdin.isatty():
                return state
            # The terminal and page share the original command/state core: in
            # tracking mode this driver is never held inside input() after a
            # page resume, a stop or a fault.
            found, expected_seq = _legacy_answer(orch, robot, tracking is not None)
            if found == "moved":
                continue
            if found == "stop":
                orch.stop("terminal-stop")
                return orch.state
            control.resume_checked(
                orch,
                "terminal-resume-" + str(time.time_ns()),
                expected_seq=expected_seq,
                environment_handled=True,
                health_rechecked=robot.health()["state"] == "ok",
            )
    finally:
        finished.set()
        robot.emergency_hold()
        if orch is not None and orch.state == "FAULT_LOCKED":
            # After the hold, and never raising: a dead stderr (EIO) or a text
            # lookup error must not skip the releases below.
            with contextlib.suppress(Exception):
                print_fault(orch, directory)
        if signal_stop["signum"] is not None:
            # For main(): the run ended by Ctrl+C / SIGTERM (exit code 128+n).
            job["interrupted_by"] = signal_stop["signum"]
        if thread is not None:
            thread.join(timeout=3)
        if pump is not None:
            pump.close()
        cleanup_error = None

        def close_console():
            # A console that cannot close (a dead terminal) does not change
            # the result of the run: leave a note, warn, keep the state. The
            # note comes first and the warning cannot raise: stderr may have
            # gone away together with the terminal.
            try:
                operator.close()
            except Exception as exc:  # noqa: BLE001 - result must survive
                if orch is not None:
                    with contextlib.suppress(Exception):
                        orch.journal.note(
                            "console_close_failed",
                            str(exc)[:300],
                            authority=orch._auth("operator"),
                        )
                with contextlib.suppress(Exception):
                    print(
                        operator_text("console_close_failed", why=exc), file=sys.stderr
                    )

        operations = (
            ([close_console] if operator is not None else [])
            # Derived rollout manifest, best effort (never raises, never moves).
            + (
                [lambda: keep_rollouts(orch, directory, job["rollout_root"])]
                if orch is not None and subtask
                else []
            )
            + ([fence.revoke, orch.close] if orch is not None else [])
        )
        operations.append(recorder.close)
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
            print(operator_text("cleanup_failed", why=cleanup_error), file=sys.stderr)


def keep_rollouts(orch, run_dir, rollout_root) -> None:
    """At run close (subtask_loop): write the derived ``rollouts.json``
    (``levi.automatic.rollouts``). Best effort: it only reads the journal and
    the demos' metadata and writes that one file; a failure leaves a
    ``rollouts_write_failed`` journal note and changes no motion, state or
    exit code. Never raises."""
    try:
        from . import rollouts

        rollouts.write_rollouts(Path(run_dir), Path(rollout_root))
    except Exception as exc:  # noqa: BLE001 - a derived file never decides a run
        with contextlib.suppress(Exception):
            orch.journal.note(
                "rollouts_write_failed", str(exc)[:300], authority=orch._auth()
            )


def hooks_summary(job, binding_path):
    """The offline hook report of ``--check --hooks``. Reads the job and, when it
    parses, the binding file as a plain mapping; opens no connection and enables
    nothing (load_binding still refuses event_pause_judge)."""
    from .fr3_intervention import hooks_report

    try:
        data = Path(binding_path).read_bytes()
        value = (
            cli.sa.parse_document(data.decode("utf-8")) if len(data) <= 65536 else None
        )
    except (OSError, ValueError, UnicodeDecodeError, cli.sa.ContractError):
        value = None
    return hooks_report(job["config"], value if isinstance(value, dict) else None)


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
    parser.add_argument(
        "--hooks",
        action="store_true",
        help="with --check: first print which of the seven event-pause hooks exist "
        "and what each is bound to (offline report; the event mode stays refused)",
    )
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
        if args.hooks and not args.check:
            raise ValueError("--hooks is only valid together with --check")
        job = cli.load_job(args.config)
        if args.hooks:
            print(json.dumps(hooks_summary(job, args.binding), indent=2))
        _binding, digest = load_binding(args.binding, job)
        if args.check:
            checked_out = {
                "plan_sha256": job["plan_sha256"],
                "binding_sha256": digest,
                "reset_strategy": job["config"].reset_strategy,
                "hardware_verified": False,
                "event_source": "signals"
                if _binding.get("signal_events") is not None
                else "files",
                "signal_events": _binding.get("signal_events"),
            }
            switch = swap_config(_binding, job)
            if switch is not None:
                # Only a binding that declares the switch gets the extra key.
                checked_out["policy_swap"] = swap.summary(switch)
            print(json.dumps(checked_out, indent=2))
            return 0
        if args.expect_plan != job["plan_sha256"] or args.expect_binding != digest:
            raise ValueError(
                "job/binding digests must match the reviewed --check output"
            )
        if not sys.stdin.isatty():
            raise ValueError("real execution requires an operator terminal")
        print(aeri_console.summary_text(job))
        phrase = "execute " + job["config"].run_id
        unclean = swap_unclean(_binding, job)
        if unclean:
            print(operator_text("swap_unclean_head"))
            for path, why in unclean:
                print("  " + path + ": " + why)
            phrase += " after policy switch check"
        print(operator_text("run_confirm_warning"))
        try:
            typed = input(operator_text("run_type_phrase", phrase=phrase)).strip()
        except (KeyboardInterrupt, EOFError):
            # Nothing is connected yet: a clean cancel, not a traceback.
            print("\n" + operator_text("run_cancelled"), file=sys.stderr)
            return 130
        if typed != phrase:
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
        if "policy_swap" in checked_binding:
            # Only these logs were shown and confirmed; re-checked under the arm lock.
            checked["swap_acknowledged"] = [path for path, _ in unclean]
        state = execute(checked, checked_binding)
        if checked.get("interrupted_by") is not None:
            # The run was stopped safely by a signal: say it ended that way.
            print(
                operator_text(
                    "run_stopped_by_signal",
                    signum=checked["interrupted_by"],
                    state=state,
                ),
                file=sys.stderr,
            )
            return 128 + int(checked["interrupted_by"])
        return 0 if state == "COMPLETED" else 1
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
