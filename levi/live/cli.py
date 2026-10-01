"""``levi live``: start, stop and inspect the background annotation service.

    levi live start [--daemon] [--auto-approve] [--config F] [--workspace W]
    levi live stop
    levi live status [--json]
    levi live doctor [--json]
    levi live once [--fake-vlm]
    levi live init

Standard library only until the service needs more: the supervisor imports no
numerical library (see ``controller.py``).
"""

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import auto, controller, gpumgr, jsonio, mirror, resources
from . import config as live_config

ENV_WORKSPACE = "LEVI_LIVE_WORKSPACE"
ENV_HOME = "LEVI_LIVE_HOME"
ENV_CONFIG = "LEVI_LIVE_CONFIG"


# --- configuration from flags ------------------------------------------------------------


def add_config_options(parser):
    parser.add_argument("--config", help="live.toml (default: <workspace>/live.toml)")
    parser.add_argument(
        "--workspace",
        help=f"service workspace (default {live_config.DEFAULT_WORKSPACE})",
    )
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help="rollout root to watch (repeatable)",
    )
    parser.add_argument("--gpu-mode", choices=live_config.GPU_MODES)
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="enable the audited automatic approver",
    )
    parser.add_argument(
        "--process-backlog",
        action="store_true",
        help="also annotate rollouts finished before the service began",
    )
    parser.add_argument(
        "--since", help="ISO time: rollouts finished before it are backlog"
    )
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help="start vLLM as soon as no evaluation is running and keep it resident "
        "(idle only sleeps it): no cold start while the robot evaluates",
    )
    parser.add_argument("--ui-port", type=int)
    parser.add_argument("--core-port", type=int)
    parser.add_argument(
        "--adopt-workspace",
        action="store_true",
        help="turn an existing LEVI workspace that is not a live one into the live "
        "workspace (refused otherwise)",
    )
    parser.add_argument(
        "--home", help="where status.json and the pid file live (default ~/.levi-live)"
    )


def resolve_config(args):
    workspace = args.workspace or os.environ.get(ENV_WORKSPACE)
    path = args.config or os.environ.get(ENV_CONFIG)
    config = live_config.load(path, workspace)
    if args.roots:
        config.watch.roots = list(args.roots)
    if args.gpu_mode:
        config.gpu.mode = args.gpu_mode
    if args.auto_approve:
        config.pipeline.auto_approve = True
    if args.process_backlog:
        config.watch.backlog = "process"
    if args.since:
        config.watch.since = args.since
    if getattr(args, "prewarm", False):
        config.vllm.prewarm = True
    if args.ui_port:
        config.service.ui_port = args.ui_port
    if args.core_port:
        config.service.core_port = args.core_port
    home = args.home or os.environ.get(ENV_HOME)
    if home:
        config.service.home = home
    return config.validate()


LOOP_STALE_S = 300.0  # the doctor warns when the loop is this far behind

SOCKET_LIMIT = 103  # bytes in a Unix socket path (sun_path, with the NUL)


def socket_path(config) -> Path:
    return config.workspace / "outputs/LEVI/workbench/agent/core/api.sock"


def check_socket_path(config):
    """The core listens on a Unix socket inside the workspace; a long
    workspace path makes it too long and the core refuses to start. Say so
    up front, not in ``ui.log`` after the service claims to be running."""
    path = socket_path(config)
    if len(os.fsencode(path)) > SOCKET_LIMIT:
        raise ValueError(
            f"the workspace path is too long for the core's Unix socket "
            f"({len(os.fsencode(path))} > {SOCKET_LIMIT} bytes: {path}); "
            "choose a shorter --workspace"
        )


def check_workspace(config, adopt=False):
    """Refuse a workspace that is not a live one.

    ``levi live`` writes a marker that switches on the automatic approver; it
    must never land in the workspace of the product LEVI or any other LEVI
    that people use. A workspace that already holds LEVI's own state
    (``outputs/LEVI``, a catalog) without the live marker needs
    ``--adopt-workspace``; the product checkout's ``.state`` is refused
    always."""
    root = config.workspace.expanduser().resolve()
    if root == (controller.project_root() / ".state").resolve():
        raise ValueError(
            f"{root} is the checkout's own LEVI workspace (the product LEVI's): "
            "the live service needs a workspace of its own"
        )
    live = (root / "live" / auto.MARKER).is_file()
    holds = (root / "outputs/LEVI").exists() or (root / "datasets.json").exists()
    if holds and not live and not adopt:
        raise ValueError(
            f"{root} already holds a LEVI workspace that is not a live one "
            "(someone may be using it). Choose another --workspace, or pass "
            "--adopt-workspace to turn it into the live workspace"
        )


def prepare(config, adopt=False, core=False):
    """Create the workspace, the live.toml (if missing) and the marker.
    ``core``: a page/core will be started, so its socket path must fit."""
    if core:
        check_socket_path(config)
    check_workspace(config, adopt)
    config.live_dir.mkdir(parents=True, exist_ok=True)
    config.home.mkdir(parents=True, exist_ok=True)
    target = config.workspace / "live.toml"
    if not target.exists() and config.path is None:
        target.write_text(live_config.render(config))
    auto.write_marker(config.live_dir, {"config": str(target)})


service_env = resources.service_env


# --- the UI and core -------------------------------------------------------------------------


class Frontend:
    """``levi serve`` (UI + core) for the live workspace, or the core alone
    when there is no production build. A child of the supervisor CLI; the
    core it starts outlives it and is stopped through its own API."""

    def __init__(self, config, log):
        self.config = config
        self.log = log
        self.process = None
        self.ui = False

    def start(self, ui=True):
        c = self.config
        project = controller.project_root()
        self.ui = bool(ui and (project / ".next/BUILD_ID").is_file())
        if ui and not self.ui:
            self.log(
                "no production build (.next): starting the core API only; run `levi build` for the page"
            )
        c.logs_dir.mkdir(parents=True, exist_ok=True)
        log = c.logs_dir / "ui.log"
        resources.rotate_file(log, c.resources.log_max_mb, c.resources.log_backups)
        argv = [
            sys.executable,
            "-m",
            "levi.cli",
            "serve" if self.ui else "backend",
            "--host",
            c.service.host,
            "--port",
            str(c.service.ui_port),
            "--backend-port",
            str(c.service.core_port),
        ]
        with log.open("ab") as sink:
            self.process = subprocess.Popen(
                argv,
                cwd=project,
                env=service_env(c),
                stdout=sink,
                stderr=sink,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        resources.apply(c, pid=self.process.pid)
        self.started = time.time()
        self.state = "starting"

    def core_pid(self):
        record = jsonio.read(
            self.config.workspace / "outputs/LEVI/workbench/agent/core/instance.json"
        )
        pid = (record or {}).get("pid")
        return pid if isinstance(pid, int) and gpumgr.identity(pid) else None

    # --- is it up? ----------------------------------------------------------

    state = "starting"  # starting | ok | failed
    error = ""
    attempts = 0
    started = 0.0

    def public(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "attempts": self.attempts,
            "ui": self.ui,
            "core_port": self.config.service.core_port,
        }

    def _tail_error(self) -> str:
        """The last error line of ui.log (what the core said when it died)."""
        try:
            lines = (
                (self.config.logs_dir / "ui.log")
                .read_text(errors="replace")
                .splitlines()
            )
        except OSError:
            return ""
        for line in reversed(lines[-80:]):
            if "Error" in line or "error" in line or "Traceback" in line:
                return line.strip()[:300]
        return lines[-1].strip()[:300] if lines else ""

    def _core_answers(self) -> bool:
        url = f"http://127.0.0.1:{self.config.service.core_port}/api/levi/health"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url, timeout=1) as response:
                return response.status == 200
        except (OSError, ValueError):
            return False

    def _alive(self) -> bool:
        if self.ui:
            return self.process is not None and self.process.poll() is None
        return self.core_pid() is not None

    def check(self, now) -> str | None:
        """The page/core watchdog: decide whether it came up, and restart it
        if it dies (three attempts, 30 s then 60 s then 120 s apart); then
        stop trying and say so once. Returns a message when something
        happened."""
        if self.state == "failed" or now < getattr(self, "_next", 0):
            return None
        if self.state == "starting":
            up = self._alive() and (not self.ui or self._core_answers())
            if up:
                self.state, self.error = "ok", ""
                return None
            exited = self.process is not None and self.process.poll() is not None
            core_only_done = not self.ui and exited and self.process.returncode == 0
            if (exited and not core_only_done) or now - self.started > 120:
                return self._failed(now, "did not come up")
            if core_only_done and self.core_pid() is None:
                return self._failed(now, "did not come up")
            return None
        if self._alive():
            return None
        return self._failed(now, "stopped")

    def _failed(self, now, what) -> str:
        what_is = "page" if self.ui else "core"
        reason = self._tail_error() or "no reason in ui.log"
        if self.attempts >= 3:
            self.state, self.error = "failed", reason
            return (
                f"the {what_is} {what} and was restarted {self.attempts} times: giving "
                f"up (labelling is not affected): {reason}"
            )
        self.attempts += 1
        self.error = reason
        self._next = now + 30.0 * 2 ** (self.attempts - 1)
        self.start(ui=self.ui)
        return f"the {what_is} {what}: {reason}; restarted (attempt {self.attempts})"

    def stop(self):
        if self.process and self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.process.pid, signal.SIGKILL)
        self.process = None
        self.stop_core()

    def stop_core(self):
        """Stop this workspace's core through its API (it refuses while jobs run)."""
        env = service_env(self.config)
        try:
            done = subprocess.run(
                [sys.executable, "-m", "levi.cli", "stop"],
                cwd=controller.project_root(),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if done.returncode:
                self.log("core did not stop: " + (done.stderr or done.stdout)[:200])
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"core stop failed: {type(exc).__name__}")


# --- start ------------------------------------------------------------------------------------------


def cmd_start(args) -> int:
    config = resolve_config(args)
    if args.daemon:
        return daemonize(args, config)
    prepare(config, args.adopt_workspace, core=not args.no_core)
    instance = controller.Instance(config.home, config.workspace)
    if not instance.acquire():
        holder = instance.holder() or {}
        print(
            f"The live service is already running (pid {holder.get('pid', '?')}). `levi live status` shows it."
        )
        return 1
    os.environ.update(service_env(config))
    os.environ.pop(ENV_CONFIG, None)
    resources.apply(config)
    logger = resources.rotating_logger(
        "levi.live",
        config.logs_dir / "live.log",
        config.resources.log_max_mb,
        config.resources.log_backups,
    )

    def log(*parts):
        text = " ".join(str(p) for p in parts)
        logger.info(text)
        if sys.stdout.isatty() or not args.daemon_child:
            print(time.strftime("%H:%M:%S"), text, flush=True)

    frontend = None
    ctl = controller.Controller(config, log=log)
    try:
        if not args.no_core:
            frontend = Frontend(config, log)
            frontend.start(ui=not args.no_ui)
        signal.signal(
            signal.SIGTERM, lambda *_: setattr(ctl, "running", False) or ctl.wake.set()
        )
        signal.signal(
            signal.SIGINT, lambda *_: setattr(ctl, "running", False) or ctl.wake.set()
        )
        log(
            f"live service up: workspace {config.workspace}, gpu {config.effective_gpu_mode()}, "
            f"auto-approve {'ON' if config.pipeline.auto_approve else 'off'}, "
            f"UI :{config.service.ui_port} core :{config.service.core_port}"
        )
        if frontend:
            ctl.hooks.append(frontend.check)
            ctl.frontend = frontend.public
        ctl.run()
    finally:
        log("shutting down")
        ctl.shutdown()
        if frontend:
            frontend.stop()
        instance.release()
    return 0


def daemonize(args, config) -> int:
    if not args.no_core:
        check_socket_path(config)
    check_workspace(config, args.adopt_workspace)
    instance = controller.Instance(config.home, config.workspace)
    if instance.holder():
        print(f"The live service is already running (pid {instance.holder()['pid']}).")
        return 1
    argv = [sys.executable, "-m", "levi.live", "start", "--daemon-child"]
    argv += _forward(args)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    with (config.logs_dir / "daemon.log").open("ab") as sink:
        subprocess.Popen(
            argv,
            cwd=controller.project_root(),
            stdout=sink,
            stderr=sink,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    wants_frontend = not args.no_core
    deadline = time.time() + 150
    while time.time() < deadline:
        holder = instance.holder()
        status = jsonio.read(config.status_file) or {}
        up = bool(
            holder
            and status.get("pid") == holder["pid"]
            and status.get("state") not in (None, "starting")
        )
        front = status.get("frontend") or {}
        settled = (
            not wants_frontend
            or front.get("state") in ("ok", "failed")
            or (front.get("attempts") or 0) >= 1  # it failed once: say why now
        )
        if up and settled:
            log = config.logs_dir / "live.log"
            if front.get("state") != "ok" and wants_frontend:
                print(
                    f"live service running in the background (pid {holder['pid']}) and "
                    f"labelling works, but the {'page' if front.get('ui') else 'core API'} "
                    f"did not come up ({'giving up' if front.get('state') == 'failed' else 'still retrying'}): "
                    f"{front.get('error')}\nsee {config.logs_dir / 'ui.log'}"
                )
                return 2
            where = (
                f"page http://{config.service.host}:{config.service.ui_port}"
                if front.get("ui")
                else f"core API :{config.service.core_port}"
                + (
                    " (no production build: run `levi build` for the page)"
                    if wants_frontend and not args.no_ui
                    else ""
                )
            )
            print(
                f"live service running in the background (pid {holder['pid']}); "
                f"{where if wants_frontend else 'no page or API'}; log {log}"
            )
            return 0
        time.sleep(0.5)
    print(
        "The live service did not finish starting in 150 s; see",
        config.logs_dir / "daemon.log",
    )
    return 1


def _forward(args) -> list:
    out = []
    for flag, value in (
        ("--config", args.config),
        ("--workspace", args.workspace),
        ("--gpu-mode", args.gpu_mode),
        ("--since", args.since),
        ("--ui-port", args.ui_port),
        ("--core-port", args.core_port),
        ("--home", args.home),
    ):
        if value:
            out += [flag, str(value)]
    for root in args.roots or []:
        out += ["--root", root]
    for flag, on in (
        ("--auto-approve", args.auto_approve),
        ("--prewarm", getattr(args, "prewarm", False)),
        ("--process-backlog", args.process_backlog),
        ("--adopt-workspace", args.adopt_workspace),
        ("--no-ui", args.no_ui),
        ("--no-core", args.no_core),
    ):
        if on:
            out.append(flag)
    return out


# --- stop / status -------------------------------------------------------------------------------------


def cmd_stop(args) -> int:
    config = resolve_config(args)
    instance = controller.Instance(config.home, config.workspace)
    holder = instance.holder()
    if not holder:
        print("The live service is not running.")
        return 0
    pid = holder["pid"]
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 150
    while time.time() < deadline:
        if gpumgr.identity(pid) != holder["identity"]:
            print(f"stopped (pid {pid})")
            return 0
        time.sleep(0.5)
    print(
        f"pid {pid} did not stop within 150 s; it is still shutting down (worker or vLLM stopping)"
    )
    return 1


def read_status(config):
    value = jsonio.read(config.status_file)
    holder = controller.Instance(config.home, config.workspace).holder()
    alive = bool(holder and value and value.get("pid") == holder["pid"])
    return value, alive, holder


def format_status(value, alive) -> str:
    if not value:
        return "No status file: the live service has not run here."
    now = time.time()
    age = now - float(value.get("updated_at") or 0)
    lines = [
        f"state      {value.get('state')}{'' if alive else f'  (NOT RUNNING: last seen {age:.0f} s ago)'}",
        f"workspace  {value.get('workspace')}   UI {value.get('ui_url')}   core :{value.get('core_port')}",
        f"auto       {'automatic approver ON' if value.get('auto_approve') else 'approvals wait for a person'}",
    ]
    gpu = value.get("gpu") or {}
    lines.append(
        f"gpu        mode {gpu.get('mode')}, vLLM {gpu.get('vllm_state')}, policy server "
        f"{'seen' if gpu.get('policy_server_seen') else 'not seen'}; {((gpu.get('decision') or {}).get('reason')) or ''}"
    )
    res = value.get("resources") or {}
    lines.append(
        f"resources  rss {res.get('rss_mb')} MiB, {res.get('threads')} threads, cpu {res.get('cpu_percent')}%"
    )
    fr3 = value.get("fr3") or {}
    lines.append(f"fr3        {fr3.get('state')} {fr3.get('detail') or ''}")
    for session in value.get("sessions") or []:
        lines.append(
            f"session    {session['group']}/{session['task_folder']}: {session['state']}"
            + (f" ({session['reason']})" if session.get("reason") else "")
        )
    worker = value.get("worker")
    if worker:
        lines.append(f"worker     {worker['dataset']}: {worker.get('phase')}")
    lines.append("datasets   name: pending / annotating / done / failed / waiting")
    for name, row in (value.get("datasets") or {}).items():
        flag = "  FR3 FAULT" if row.get("fault") else ""
        lines.append(
            f"  {name}: {row['pending']} / {row['annotating']} / {row['done']} / {row['failed']} / {row['waiting']}"
            f"  [{row.get('state')}]{flag}"
        )
    for event in (value.get("events") or [])[:5]:
        lines.append(
            f"event      {time.strftime('%H:%M:%S', time.localtime(event['time']))} {event['text']}"
        )
    if value.get("last_error"):
        lines.append(f"last error {value['last_error']}")
    return "\n".join(lines)


def cmd_status(args) -> int:
    config = resolve_config(args)
    value, alive, _ = read_status(config)
    if args.json:
        print(
            json.dumps({"alive": alive, "status": value}, ensure_ascii=False, indent=1)
        )
    else:
        print(format_status(value, alive))
    return 0 if alive else 3


# --- doctor --------------------------------------------------------------------------------------------------


def nice_of(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[16])
    except (OSError, ValueError, IndexError):
        return None


def diagnose(config) -> dict:
    """What the service is using right now, and what looks wrong."""
    value, alive, holder = read_status(config)
    warnings = []
    report = {"alive": alive, "processes": [], "warnings": warnings}
    now = time.time()
    if not alive:
        warnings.append("the live service is not running (levi live start)")
    if holder:
        for pid in resources.tree(holder["pid"]):
            if resources.rss_mb(pid) is None:
                continue  # exited while we looked
            report["processes"].append(
                {
                    "pid": pid,
                    "cmd": _cmd(pid),
                    "rss_mb": resources.rss_mb(pid),
                    "threads": resources.thread_count(pid),
                    "nice": nice_of(pid),
                    "cpu_seconds": resources.cpu_seconds(pid),
                }
            )
        sup = report["processes"][0]
        if sup["rss_mb"] and sup["rss_mb"] > 150:
            warnings.append(
                f"the supervisor holds {sup['rss_mb']} MiB (expected < 60 MiB when idle)"
            )
        if sup["threads"] and sup["threads"] > 12:
            warnings.append(f"the supervisor has {sup['threads']} threads")
        for row in report["processes"]:
            if row["nice"] is not None and row["nice"] < config.resources.nice:
                warnings.append(
                    f"pid {row['pid']} runs at nice {row['nice']}, not {config.resources.nice}"
                )
    loop_at = (value or {}).get("loop_at")
    if alive and loop_at and now - float(loop_at) > LOOP_STALE_S:
        warnings.append(
            f"the service's main loop has not ticked for {now - float(loop_at):.0f} s "
            "although the process is alive (stuck?); see the service log, "
            "`levi live stop` and start again"
        )
    record = jsonio.read(config.live_dir / "vllm.json")
    if (
        isinstance(record, dict)
        and record.get("pid")
        and gpumgr.identity(record["pid"]) == record.get("identity")
        and not holder
    ):
        report["orphan_vllm"] = record["pid"]
        warnings.append(
            f"an orphan vLLM (pid {record['pid']}, port {record.get('port')}) is still "
            "running with no live service: it holds the GPU. `levi live start` takes it "
            f"back, or stop it with `{config.vllm.stop_script} --stop {record.get('port')}`"
        )
    gpu = {"vram": gpumgr.vram(), "processes": gpumgr.gpu_holders()}
    report["gpu"] = gpu
    vllm = ((value or {}).get("gpu") or {}).get("vllm") or {}
    report["vllm"] = vllm
    if vllm.get("state") == "error" or (value or {}).get("attention"):
        warnings.append(
            "vLLM is failing to start"
            + (f": {vllm.get('error')}" if vllm.get("error") else "")
            + "; labelling is paused"
            + (" until `levi live resume`" if (value or {}).get("attention") else "")
        )
    if vllm.get("state") in ("ready", "starting"):
        working = bool((value or {}).get("worker")) or bool(
            (value or {}).get("queue_depth")
        )
        if (
            not working
            and vllm.get("started_at")
            and now - (vllm.get("started_at") or now) > config.vllm.idle_timeout_s + 120
        ):
            warnings.append(
                "vLLM is up with nothing to do: it should have been released"
            )
    ports = gpumgr.listening_ports()
    policy = [int(p) for p in config.gpu.policy_ports if int(p) in ports]
    report["policy_ports_listening"] = policy
    gate = ((value or {}).get("gpu") or {}).get("gate") or {}
    report["gate"] = gate
    report["notes"] = [
        (
            "A cold start of vLLM takes 45-70 s (weights, engine, CUDA graphs); "
            "a sleeping one wakes in under a second."
        ),
        (
            "With the policy server at XLA_PYTHON_CLIENT_MEM_FRACTION=.22 both stay "
            "resident (about 31.3 of 32.6 GB); a larger policy fraction leaves no room."
        ),
    ]
    free_mib = (gpu["vram"] or {}).get("free_mib")
    if (
        free_mib is not None
        and free_mib < config.gpu.min_free_mib
        and vllm.get("state") == "ready"
    ):
        warnings.append(
            f"only {free_mib} MiB of VRAM free with vLLM awake "
            f"(it sleeps below {config.gpu.min_free_mib})"
        )
    free = resources.disk_free_gib(config.workspace)
    cache = resources.run_files(config.workspace / "outputs/LEVI/workbench")
    cache_gib = round(sum(r[2] for r in cache) / 1024**3, 2)
    report["disk"] = {
        "free_gib": free,
        "captures_gib": round(resources.dir_size(config.captures_dir) / 1024**3, 2),
        "run_cache_gib": cache_gib,
        "run_cache_cap_gib": config.resources.cache_max_gib,
    }
    if free is not None and free < 50:
        warnings.append(f"only {free} GiB of disk free under the workspace")
    if cache_gib > config.resources.cache_max_gib:
        warnings.append(
            f"run cache {cache_gib} GiB is over its {config.resources.cache_max_gib} GiB cap"
        )
    logs = (
        sum(p.stat().st_size for p in config.logs_dir.glob("*") if p.is_file())
        if config.logs_dir.is_dir()
        else 0
    )
    report["logs_mib"] = round(logs / 1024**2, 1)
    copied = sum(
        (row.get("copied") or 0)
        for state in mirror.list_states(config).values()
        for row in (state.get("demos") or {}).values()
    )
    if copied:
        warnings.append(
            f"{copied} file(s) were copied, not hard-linked: the rollout root is on another file system"
        )
    report["queue"] = {
        "depth": (value or {}).get("queue_depth"),
        "datasets": {
            n: r.get("state") for n, r in ((value or {}).get("datasets") or {}).items()
        },
    }
    for name, row in ((value or {}).get("datasets") or {}).items():
        if row.get("stuck"):
            warnings.append(
                f"{name}: {row['stuck']} demo(s) never finished (a leftover raw capture "
                "or a client that died mid-write); they are left alone"
            )
        if row.get("source_changed"):
            warnings.append(
                f"{name}: the source of {row['source_changed']} mirrored demo(s) was "
                "replaced after it was mirrored (annotations belong to the old content)"
            )
    for root in config.watch.roots:
        if not Path(root).expanduser().is_dir():
            warnings.append(f"watch root {root} does not exist")
    fr3 = ((value or {}).get("fr3") or {}).get("state")
    report["fr3"] = fr3
    if fr3 == "red":
        warnings.append("FR3 health monitor reports a red light")
    elif fr3 in ("offline", "missing"):
        warnings.append(
            f"FR3 health monitor is {fr3} (the client falls back to its own signals)"
        )
    if alive and value and now - float(value.get("updated_at") or 0) > 15:
        warnings.append("status.json is stale: the service is not heartbeating")
    return report


def _cmd(pid):
    try:
        return (
            Path(f"/proc/{pid}/cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode(errors="replace")[:120]
        )
    except OSError:
        return ""


def cmd_doctor(args) -> int:
    config = resolve_config(args)
    report = diagnose(config)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"service    {'running' if report['alive'] else 'NOT running'}")
        for p in report["processes"]:
            print(
                f"  pid {p['pid']}: rss {p['rss_mb']} MiB, {p['threads']} threads, nice {p['nice']}, cpu {p['cpu_seconds']} s  {p['cmd'][:70]}"
            )
        vram = (report["gpu"] or {}).get("vram") or {}
        print(
            f"gpu        free {vram.get('free_mib')} / {vram.get('total_mib')} MiB; on it: "
            + (
                ", ".join(
                    f"{h['name']}({h['memory_mib']})"
                    for h in report["gpu"]["processes"]
                )
                or "nothing"
            )
        )
        print(
            f"vllm       {report['vllm'].get('state')} profile {report['vllm'].get('profile')}; policy ports listening: {report['policy_ports_listening']}; gate {'open' if (report['gate'] or {}).get('open', True) else 'CLOSED'} ({(report['gate'] or {}).get('reason', '')})"
        )
        for note in report["notes"]:
            print(f"note       {note}")
        d = report["disk"]
        print(
            f"disk       free {d['free_gib']} GiB; captures {d['captures_gib']} GiB; run cache {d['run_cache_gib']}/{d['run_cache_cap_gib']} GiB; logs {report['logs_mib']} MiB"
        )
        print(
            f"queue      depth {report['queue']['depth']}; {report['queue']['datasets']}"
        )
        print(f"fr3        {report['fr3']}")
        for w in report["warnings"]:
            print(f"WARNING    {w}")
        if not report["warnings"]:
            print("no warnings")
    return 1 if report["warnings"] else 0


# --- once ------------------------------------------------------------------------------------------------------


def cmd_once(args) -> int:
    config = resolve_config(args)
    fake = None
    if args.fake_vlm:
        if (
            not args.workspace
            or Path(args.workspace).expanduser().resolve()
            == Path(live_config.DEFAULT_WORKSPACE).resolve()
        ):
            raise ValueError(
                "--fake-vlm labels with a stand-in model and (with --auto-approve) "
                "commits its output as automatic annotations: give it a scratch "
                "--workspace, never the live workspace"
            )
        from . import fakevlm

        server, fake, port = fakevlm.serve(0)
        config.gpu.mode = "manual"
        config.vllm.port = port
        print(f"fake vLLM on 127.0.0.1:{port}")
    prepare(config, args.adopt_workspace)
    instance = controller.Instance(config.home, config.workspace)
    if not instance.acquire():
        print(
            "The live service is running; stop it first (levi live stop) or let it work."
        )
        return 1
    os.environ.update(service_env(config))
    resources.apply(config)
    ctl = controller.Controller(config)
    try:
        ctl.run(once=True, max_seconds=args.max_seconds)
    finally:
        ctl.shutdown()
        instance.release()
        if fake:
            server.shutdown()
    status = ctl.status()
    print(format_status(status, False))
    bad = [
        n for n, r in status["datasets"].items() if r["failed"] or r["state"] == "error"
    ]
    return 1 if bad else 0


def cmd_resume(args) -> int:
    """Tell the running service to forget vLLM start failures (it gave up
    after ``vllm.max_start_failures``) and try again."""
    config = resolve_config(args)
    value, alive, _ = read_status(config)
    config.live_dir.mkdir(parents=True, exist_ok=True)
    jsonio.write(config.live_dir / "resume.json", {"requested_at": time.time()})
    attention = (value or {}).get("attention")
    if attention:
        print(f"was waiting for a person: {attention.get('reason')}")
    print(
        "resume requested; the service picks it up within seconds"
        if alive
        else "resume recorded; the service is not running, the next start sees it"
    )
    return 0


def cmd_init(args) -> int:
    config = resolve_config(args)
    check_workspace(config, args.adopt_workspace)
    target = config.workspace / "live.toml"
    config.workspace.mkdir(parents=True, exist_ok=True)
    if target.exists():
        print(f"{target} exists; not overwritten")
        return 0
    target.write_text(live_config.render(config))
    print(f"wrote {target}")
    return 0


# --- entry --------------------------------------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="levi live", description="Background live annotation service"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="run the service (foreground, or --daemon)")
    add_config_options(start)
    start.add_argument(
        "--daemon", action="store_true", help="run in the background and return"
    )
    start.add_argument("--daemon-child", action="store_true", help=argparse.SUPPRESS)
    start.add_argument(
        "--no-ui", action="store_true", help="start the core API without the web page"
    )
    start.add_argument(
        "--no-core", action="store_true", help="start neither the page nor the core API"
    )
    stop = sub.add_parser("stop", help="stop the running service")
    add_config_options(stop)
    status = sub.add_parser("status", help="what the service is doing")
    add_config_options(status)
    status.add_argument("--json", action="store_true")
    doctor = sub.add_parser("doctor", help="resource use and warnings")
    add_config_options(doctor)
    doctor.add_argument("--json", action="store_true")
    once = sub.add_parser("once", help="label everything finished now, then exit")
    add_config_options(once)
    once.add_argument(
        "--fake-vlm", action="store_true", help="use a fake model server (no GPU)"
    )
    once.add_argument("--max-seconds", type=float, default=None)
    resume = sub.add_parser(
        "resume", help="clear a vLLM start failure the service gave up on"
    )
    add_config_options(resume)
    init = sub.add_parser("init", help="write a live.toml with every default")
    add_config_options(init)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "once": cmd_once,
        "resume": cmd_resume,
        "init": cmd_init,
    }
    try:
        return handlers[args.command](args)
    except ValueError as exc:
        print(f"levi live: {exc}", file=sys.stderr)
        return 2
