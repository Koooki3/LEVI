"""``levi automatic``: check, validate, dry-run, inspect and report AERI
runs (T-C-16). There is no real robot run in this version: ``run`` refuses
without ``--dry-run``, and a dry run drives the in-process fakes only
(``integrations/fr3_automatic/fake.py``), in a temporary folder, with no
motion authority over anything real.

    levi automatic doctor   [--config F] [--json]
    levi automatic validate --config F [--json]
    levi automatic run      --config F --dry-run [--episodes N] [--scenes S] [--keep DIR] [--json]
    levi automatic status   --run-dir D [--json]
    levi automatic report   --run-dir D [--config F] [--truth T] [--format json|md]

Until ``levi automatic`` is wired into ``levi.cli``, run it as
``python -m levi.automatic.cli``.

The job file (``levi.aeri.job.v1``) uses the same strict YAML subset as the
Initial State Contract (``scene_assessment.parse_document``); it is a draft
like the contract (HA-23)::

    schema_version: levi.aeri.job.v1
    experiment:
      name: r20261010-a          # the run id
      episodes: 30
      random_seed: 42
      execution_mode: shadow     # shadow | assisted | autonomous (checked only)
    policies:
      forward: {max_steps: 120}  # flow mappings are NOT supported: write blocks
    task:
      instruction: stack the plates
      reset_instruction: "Reset: stack the plates"
      initial_state_spec: initial-state.yaml   # relative to this file
    termination: {...}           # TerminationConfig fields
    reset:
      strategy: single_reset_policy   # or human_assisted
      max_attempts: 1
      on_unknown: reset               # or wait_human
    recording:
      rollout_root: /data/rollouts
      group: aeri
      forward_folder: stack_plates__r20261010-a
      reset_folder: reset_stack_plates__r20261010-a
"""

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from levi.domain import aeri

from . import metrics
from . import scene_assessment as sa
from .journal import Journal
from .orchestrator import ConfigError, Orchestrator, RunConfig
from .recorder import MANIFEST, RolloutRecorder, SessionFiles
from .termination import TerminationConfig

JOB_SCHEMA = "levi.aeri.job.v1"
MODES = ("shadow", "assisted", "autonomous")
STRATEGY_ALIASES = {"single_policy": "single_reset_policy"}
FAKE = (
    Path(__file__).resolve().parents[2] / "integrations" / "fr3_automatic" / "fake.py"
)
CONTRACTS = (
    Path(__file__).resolve().parents[2] / "docs" / "architecture" / "aeri" / "v1"
)
EXIT_OK, EXIT_PROBLEM, EXIT_REFUSED = 0, 1, 2

_TOP = {
    "schema_version",
    "experiment",
    "policies",
    "task",
    "termination",
    "reset",
    "recording",
}
_SECTIONS = {
    "experiment": {"name", "episodes", "random_seed", "execution_mode"},
    "policies": {"forward", "reset"},
    "task": {"instruction", "reset_instruction", "initial_state_spec"},
    "reset": {"strategy", "max_attempts", "on_unknown", "enabled"},
    "recording": {"rollout_root", "group", "forward_folder", "reset_folder"},
    "termination": set(TerminationConfig.__dataclass_fields__),
}


class JobError(ValueError):
    pass


# --- the job file ---------------------------------------------------------------------------


def _section(job, name):
    value = job.get(name) or {}
    if not isinstance(value, dict):
        raise JobError(f"{name}: a mapping")
    unknown = sorted(set(value) - _SECTIONS[name])
    if unknown:
        raise JobError(f"{name}: unknown key(s) {', '.join(unknown)}")
    return value


def load_job(path) -> dict:
    """The job as ``{"config": RunConfig, "contract", "seed", "mode",
    "recording", "texts", "plan_sha256"}``; ``JobError`` says what is
    wrong."""
    path = Path(path)
    try:
        job = sa.parse_document(path.read_text())
    except OSError as exc:
        raise JobError(f"{path}: {exc.strerror or exc}") from None
    except sa.ContractError as exc:
        raise JobError(f"{path}: {exc}") from None
    if job.get("schema_version") != JOB_SCHEMA:
        raise JobError(f"schema_version must be {JOB_SCHEMA}")
    unknown = sorted(set(job) - _TOP)
    if unknown:
        raise JobError(f"unknown section(s) {', '.join(unknown)}")
    experiment = _section(job, "experiment")
    task = _section(job, "task")
    reset = _section(job, "reset")
    recording = _section(job, "recording")
    termination = _section(job, "termination")
    policies = _section(job, "policies")
    run_id = experiment.get("name")
    if not isinstance(run_id, str) or not sa.LABEL.match(run_id) or "." in run_id:
        raise JobError("experiment.name: an id (letters, digits, _ : -), no dots")
    mode = experiment.get("execution_mode", "shadow")
    if mode not in MODES:
        raise JobError(f"experiment.execution_mode: one of {', '.join(MODES)}")
    seed = experiment.get("random_seed", 0)
    if type(seed) is not int:
        raise JobError("experiment.random_seed: a whole number")
    contract = None
    if task.get("initial_state_spec") is not None:
        spec = Path(str(task["initial_state_spec"]))
        spec = spec if spec.is_absolute() else path.parent / spec
        try:
            contract = sa.load_contract(spec.read_text())
        except OSError as exc:
            raise JobError(f"task.initial_state_spec: {exc.strerror or exc}") from None
        except sa.ContractError as exc:
            raise JobError(f"task.initial_state_spec: {exc}") from None
    steps = {}
    for role in ("forward", "reset"):
        value = policies.get(role) or {}
        if not isinstance(value, dict) or set(value) - {"max_steps"}:
            raise JobError(f"policies.{role}: only max_steps is read in v1")
        if "max_steps" in value:
            steps[f"{role}_max_steps"] = value["max_steps"]
    group = recording.get("group", "aeri")
    folders = {
        "forward_folder": recording.get("forward_folder", f"forward__{run_id}"),
        "reset_folder": recording.get("reset_folder", f"reset__{run_id}"),
    }
    for key, value in [("group", group), *folders.items()]:
        if not isinstance(value, str) or not sa.LABEL.match(value):
            raise JobError(f"recording.{key}: a folder name")
    if folders["forward_folder"] == folders["reset_folder"]:
        raise JobError("recording: forward and reset need their own folders")
    strategy = reset.get("strategy", "single_reset_policy")
    try:
        term = TerminationConfig(**termination)
        config = RunConfig(
            run_id=run_id,
            plan_sha256="0" * 64,
            episodes=experiment.get("episodes", 1),
            reset_strategy=STRATEGY_ALIASES.get(strategy, strategy),
            reset_enabled=reset.get("enabled", True),
            max_reset_attempts=reset.get("max_attempts", 1),
            on_scene_unknown=reset.get("on_unknown", "reset"),
            initial_state=contract,
            termination=term,
            **folders,
            **steps,
        )
    except (ConfigError, TypeError, ValueError) as exc:
        raise JobError(str(exc)) from None
    texts = {
        "forward": str(task.get("instruction") or folders["forward_folder"]),
        "reset": str(task.get("reset_instruction") or folders["reset_folder"]),
    }
    plan = {
        "schema_version": JOB_SCHEMA,
        "run": _plain(config),
        "contract": contract.describe() if contract else None,
        "mode": mode,
        "seed": seed,
        "recording": {"group": group, **folders},
        "texts": texts,
    }
    digest = hashlib.sha256(
        json.dumps(plan, sort_keys=True, default=str).encode()
    ).hexdigest()
    config = RunConfig(**{**_fields(config), "plan_sha256": digest})
    root = recording.get("rollout_root")
    return {
        "config": config,
        "contract": contract,
        "seed": seed,
        "mode": mode,
        "group": group,
        "rollout_root": str(root) if root is not None else None,
        "texts": texts,
        "plan": plan,
        "plan_sha256": digest,
    }


def _fields(config) -> dict:
    return {name: getattr(config, name) for name in config.__dataclass_fields__}


def _plain(config) -> dict:
    out = {}
    for name, value in _fields(config).items():
        if name == "specs":
            continue
        if name == "termination":
            value = asdict(value)
        elif name == "initial_state":
            value = value.key if value else None
        out[name] = value
    return out


# --- the fakes ----------------------------------------------------------------------------------


def load_fakes():
    """``integrations/fr3_automatic/fake.py`` (not part of an installed
    package: a source checkout only)."""
    name = "fr3_automatic_fake"
    if name in sys.modules:
        return sys.modules[name]
    if not FAKE.is_file():
        raise JobError(
            f"the fakes are not here ({FAKE}): a dry run needs a source checkout"
        )
    spec = importlib.util.spec_from_file_location(name, FAKE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DryRun:
    """One dry run on the fakes in ``folder``. ``robot`` is a ``FakeRobot``:
    nothing real ever receives a command."""

    def __init__(self, job: dict, folder: Path, *, episodes=None, scenes=()):
        import random

        from . import state_machine as sm
        from .adapters import events as ev

        fake = load_fakes()
        cfg = job["config"]
        if episodes is not None:
            cfg = RunConfig(**{**_fields(cfg), "episodes": episodes})
        self.config = cfg
        self.folder = Path(folder)
        self.directory = self.folder / ".aeri" / "runs" / cfg.run_id
        clock = fake.FakeClock()
        fence = sm.MotionFence()
        contract = job["contract"]
        evidence = contract.min_evidence_refs if contract else 0
        ready = {"decision": "ready", "evidence": evidence}
        scene_kw = (
            {"contract": contract.key, "predicates": contract.required}
            if contract
            else {}
        )
        settle = max(1, min(cfg.forward_max_steps, cfg.reset_max_steps) - 1)
        step = min(cfg.termination.min_steps, settle)
        self.robot = fake.FakeRobot(clock, fence.check)
        self.recorder = RolloutRecorder(
            self.folder,
            run_id=cfg.run_id,
            run_dir=self.directory,
            group=job["group"],
            texts=job["texts"],
        )
        self.sessions = SessionFiles(
            self.folder,
            run_id=cfg.run_id,
            group=job["group"],
            folders={"forward": cfg.forward_folder, "reset": cfg.reset_folder},
            texts=job["texts"],
        )
        self.orch = Orchestrator.create(
            self.directory,
            cfg,
            robot=self.robot,
            policy=fake.FakePolicy(clock, rng=random.Random(job["seed"])),
            recorder=self.recorder,
            events=ev.FakeEventStream(
                {
                    "forward": [{"step": step, "event_type": "object_settled"}],
                    "reset": [{"step": step, "event_type": "object_settled"}],
                },
                clock,
                cfg.run_id,
            ),
            verifier=ev.FakeGoalVerifier(
                [], clock, cfg.run_id, default={"decision": "confirmed"}
            ),
            scene=ev.FakeSceneAssessor(
                [{**ready, "decision": d} for d in scenes],
                clock,
                cfg.run_id,
                default=ready,
                **scene_kw,
            ),
            clock=clock,
            fence=fence,
            listener=self.sessions,
        )

    def run(self) -> dict:
        try:
            state = self.orch.run()
            events = self.orch.journal.events
            manifest = json.loads((self.directory / MANIFEST).read_text())
        finally:
            self.orch.close()
            self.recorder.close()
        return {
            "state": state,
            "lines": len(events),
            "transitions": [
                [e.from_state, e.to_state, e.reason]
                for e in events
                if e.record == "committed"
            ],
            "robot": {
                "kind": type(self.robot).__name__,
                "motions": len(self.robot.motions),
                "refused": len(self.robot.refused),
            },
            "metrics": metrics.report(
                events,
                manifest=manifest,
                termination=self.config.termination,
                max_steps=self.config.forward_max_steps,
            ),
        }


# --- commands -----------------------------------------------------------------------------------


class NetworkRefused(RuntimeError):
    pass


@contextlib.contextmanager
def no_network():
    """While a dry run lasts, any attempt to connect a socket raises: the
    fakes need none, and nothing real may be reached."""

    def refuse(*args, **kwargs):
        raise NetworkRefused("a dry run opens no connection")

    saved = socket.socket.connect, socket.socket.connect_ex, socket.create_connection
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection = (
            saved
        )


def _print(value, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        print(text)


def cmd_doctor(args) -> int:
    checks = []

    def check(name, ok, detail="", required=True):
        checks.append(
            {"check": name, "ok": bool(ok), "detail": detail, "required": required}
        )

    check("python", sys.version_info >= (3, 11), sys.version.split()[0])
    missing = [
        name
        for name in aeri.SCHEMAS
        if not (CONTRACTS / f"{name}.schema.json").is_file()
    ]
    check(
        "contract snapshots",
        not missing,
        f"missing: {', '.join(missing)}" if missing else str(CONTRACTS),
    )
    check("fakes (dry run)", FAKE.is_file(), str(FAKE), required=False)
    check(
        "real robot adapter",
        False,
        "not in this version: a real run is refused, use run --dry-run",
        required=False,
    )
    if args.config:
        try:
            job = load_job(args.config)
            check("job file", True, f"plan {job['plan_sha256'][:12]}")
            root = job["rollout_root"]
            if root is None:
                check(
                    "rollout root",
                    False,
                    "recording.rollout_root not set",
                    required=False,
                )
            else:
                exists = Path(root).is_dir()
                writable = exists and os.access(root, os.W_OK | os.X_OK)
                check(
                    "rollout root",
                    writable,
                    f"{root}: " + ("writable" if writable else "missing or read-only"),
                    required=False,
                )
        except JobError as exc:
            check("job file", False, str(exc))
    ok = all(c["ok"] for c in checks if c["required"])
    lines = [
        f"{'ok ' if c['ok'] else ('-- ' if not c['required'] else 'NO ')} {c['check']}: {c['detail']}"
        for c in checks
    ]
    _print({"ok": ok, "checks": checks}, args.json, "\n".join(lines))
    return EXIT_OK if ok else EXIT_PROBLEM


def cmd_validate(args) -> int:
    try:
        job = load_job(args.config)
    except JobError as exc:
        _print({"ok": False, "error": str(exc)}, args.json, f"invalid: {exc}")
        return EXIT_REFUSED
    plan = {**job["plan"], "plan_sha256": job["plan_sha256"]}
    text = (
        f"valid: run {job['config'].run_id}, {job['config'].episodes} episodes, "
        f"reset strategy {job['config'].reset_strategy}, contract "
        f"{job['contract'].key if job['contract'] else 'none (provider decides)'}, "
        f"plan {job['plan_sha256'][:12]}"
    )
    _print({"ok": True, "plan": plan}, args.json, text)
    return EXIT_OK


def cmd_run(args) -> int:
    if not args.dry_run:
        message = (
            "a real run is not available in this version (no FR3 adapter); "
            "use --dry-run to drive the fakes"
        )
        _print({"ok": False, "error": message}, args.json, message)
        return EXIT_REFUSED
    try:
        job = load_job(args.config)
    except JobError as exc:
        _print({"ok": False, "error": str(exc)}, args.json, f"invalid: {exc}")
        return EXIT_REFUSED
    if args.keep:
        folder = Path(args.keep)
        if folder.exists() and any(folder.iterdir()):
            message = f"{folder} is not empty"
            _print({"ok": False, "error": message}, args.json, message)
            return EXIT_REFUSED
        folder.mkdir(parents=True, exist_ok=True)
    else:
        folder = Path(tempfile.mkdtemp(prefix="aeri-dry-run-"))
    scenes = [s for s in (args.scenes or "").split(",") if s]
    bad = [s for s in scenes if s not in ("ready", "reset_required", "unknown")]
    if bad:
        message = f"--scenes: unknown decision(s) {', '.join(bad)}"
        _print({"ok": False, "error": message}, args.json, message)
        return EXIT_REFUSED
    try:
        with no_network():
            result = DryRun(job, folder, episodes=args.episodes, scenes=scenes).run()
    except (JobError, ConfigError) as exc:
        _print({"ok": False, "error": str(exc)}, args.json, str(exc))
        return EXIT_REFUSED
    finally:
        if not args.keep:
            shutil.rmtree(folder, ignore_errors=True)
    result["dry_run"] = True
    result["kept"] = str(folder) if args.keep else None
    auto = result["metrics"]["autonomous"]
    text = (
        f"dry run (fakes only, no motion authority): {result['state']}, "
        f"{len(result['transitions'])} transitions, "
        f"{auto['forward_episodes']} forward episodes, "
        f"{result['metrics']['reset']['resets']} resets, "
        f"fake robot motions {result['robot']['motions']}"
        + (f"; kept in {folder}" if args.keep else "")
    )
    _print(result, args.json, text)
    return EXIT_OK if result["state"] in ("COMPLETED", "WAIT_HUMAN") else EXIT_PROBLEM


def _scan(run_dir: Path):
    scan = Journal.read(run_dir)
    if not scan.events and scan.corrupt is None:
        raise JobError(f"{run_dir} has no journal")
    return scan


def cmd_status(args) -> int:
    run_dir = Path(args.run_dir)
    try:
        scan = _scan(run_dir)
    except JobError as exc:
        _print({"ok": False, "error": str(exc)}, args.json, str(exc))
        return EXIT_REFUSED
    events = scan.events
    last = next((e for e in reversed(events) if e.record == "committed"), None)
    open_tx = scan.replay.open_tx
    found = {
        "run_id": events[0].run_id if events else None,
        "state": scan.effective_state,
        "corrupt": scan.corrupt,
        "torn_bytes": len(scan.torn),
        "lines": len(events),
        "next_seq": len(events),
        "last": [last.from_state, last.to_state, last.reason] if last else None,
        "open_transaction": open_tx.transaction_id if open_tx else None,
        "episode_results": sum(
            1 for e in events if e.record == "committed" and e.episode_result
        ),
    }
    try:
        manifest = json.loads((run_dir / MANIFEST).read_text())
        found["rollouts"] = {
            state: sum(1 for e in manifest["episodes"] if e["state"] == state)
            for state in ("opening", "open", "complete", "incomplete")
        }
    except (OSError, ValueError, KeyError, TypeError):
        found["rollouts"] = None
    text = (
        f"{found['run_id']}: {found['state']}"
        + (f" (CORRUPT: {scan.corrupt})" if scan.corrupt else "")
        + f", {found['lines']} lines, last {found['last']}"
        + (f", open {found['open_transaction']}" if open_tx else "")
    )
    _print(found, args.json, text)
    return EXIT_OK


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    try:
        scan = _scan(run_dir)
        job = load_job(args.config) if args.config else None
    except JobError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    try:
        manifest = json.loads((run_dir / MANIFEST).read_text())
    except (OSError, ValueError):
        manifest = None
    try:
        found = metrics.report(
            scan.events,
            labels=metrics.LabelStore(run_dir),
            manifest=manifest,
            termination=job["config"].termination if job else None,
            max_steps=job["config"].forward_max_steps if job else None,
            truth=args.truth,
        )
    except metrics.LabelRefused as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    found["state"] = scan.effective_state
    if args.format == "json":
        print(json.dumps(found, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        print(markdown(found))
    return EXIT_OK


def _rate(value) -> str:
    if not value or value.get("rate") is None:
        return f"- ({value.get('n', 0) if value else 0}/{value.get('of', 0) if value else 0})"
    low, high = value["wilson95"]
    return (
        f"{value['rate']:.3f} ({value['n']}/{value['of']}; 95% CI {low:.3f}-{high:.3f})"
    )


def markdown(found: dict) -> str:
    auto = found["autonomous"]
    et = found["early_termination"]
    rs = found["reset"]
    au = found["automation"]
    return "\n".join(
        [
            f"# AERI run report ({found['state']})",
            "",
            (
                f"Truth: {found['truth']} ({found['truth_labels']['task_outcome']} "
                f"outcome labels, {found['truth_labels']['initial_state']} scene "
                "labels). autonomous_* rates are the run's own verdicts, not "
                "ground truth."
            ),
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| forward episodes | {auto['forward_episodes']} |",
            f"| autonomous success rate | {_rate(auto['autonomous_success_rate'])} |",
            f"| early stops | {et['early_stops']} |",
            f"| precision | {_rate(et['precision'])} |",
            f"| recall | {_rate(et['recall'])} |",
            f"| false early stop rate | {_rate(et['false_early_stop_rate'])} |",
            f"| saved steps | {et['saved_steps']['total']} |",
            f"| resets | {rs['resets']} |",
            f"| autonomous reset success rate | {_rate(rs['autonomous_reset_success_rate'])} |",
            f"| skip accuracy | {_rate(rs['skip_accuracy'])} |",
            f"| wrong-skip rate | {_rate(rs['wrong_skip_rate'])} |",
            f"| interventions | {au['interventions']} |",
            f"| longest run without intervention | {au['longest_run_without_intervention']} |",
            "",
        ]
    )


# --- the parser ------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="levi automatic",
        description=(
            "Automatic evaluation pipeline (AERI): check, validate, dry-run on "
            "fakes, inspect and report runs. No real robot run in this version. / "
            "自动测评流水线（AERI）：检查、校验、用 Fake 试运行、查看和报告运行。"
            "本版本不能在真机上运行。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    json_help = "machine-readable output / 输出 JSON"
    config_help = "job file (levi.aeri.job.v1) / 作业配置文件"

    doctor = sub.add_parser(
        "doctor",
        help="read-only readiness checks / 只读的就绪检查",
        description="Read-only checks: contracts, fakes, the job file, the rollout "
        "root. Nothing is written, nothing is connected. / 只读检查，不写入、不连接。",
    )
    doctor.add_argument("--config", help=config_help)
    doctor.add_argument("--json", action="store_true", help=json_help)
    doctor.set_defaults(func=cmd_doctor)

    validate = sub.add_parser(
        "validate",
        help="check a job file and print its plan / 校验作业文件并打印计划",
    )
    validate.add_argument("--config", required=True, help=config_help)
    validate.add_argument("--json", action="store_true", help=json_help)
    validate.set_defaults(func=cmd_validate)

    run = sub.add_parser(
        "run",
        help="dry-run a job on the fakes (a real run is refused) / 用 Fake 试运行（拒绝真机运行）",
        description="--dry-run drives the in-process fakes in a temporary folder: no "
        "robot, no policy server, no model, no port, no motion authority over "
        "anything real. / --dry-run 只驱动进程内的 Fake，在临时目录里运行，"
        "没有任何真实运动权限。",
    )
    run.add_argument("--config", required=True, help=config_help)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="required: run on the fakes only / 必填：只在 Fake 上运行",
    )
    run.add_argument(
        "--episodes",
        type=int,
        default=None,
        help="override the job's episode count / 覆盖片段数",
    )
    run.add_argument(
        "--scenes",
        default="",
        help="scene decisions the fake answers first, comma separated "
        "(ready, reset_required, unknown) / Fake 依次给出的场景结论，逗号分隔",
    )
    run.add_argument(
        "--keep",
        metavar="DIR",
        help="keep the dry run's files in this new or empty folder / 把试运行文件保留在此空目录",
    )
    run.add_argument("--json", action="store_true", help=json_help)
    run.set_defaults(func=cmd_run)

    status = sub.add_parser(
        "status",
        help="the state of a run, read-only / 只读查看运行状态",
    )
    status.add_argument(
        "--run-dir", required=True, help="<root>/.aeri/runs/<run id> / 运行目录"
    )
    status.add_argument("--json", action="store_true", help=json_help)
    status.set_defaults(func=cmd_status)

    report = sub.add_parser(
        "report",
        help="metrics of a run with Wilson intervals / 带 Wilson 区间的运行指标",
    )
    report.add_argument(
        "--run-dir", required=True, help="<root>/.aeri/runs/<run id> / 运行目录"
    )
    report.add_argument(
        "--config", help="job file, for steps and control episodes / 作业配置文件"
    )
    report.add_argument(
        "--truth",
        choices=metrics.TRUTH,
        default=metrics.TRUTH[0],
        help="which labels count as truth / 用哪类标签作真值",
    )
    report.add_argument(
        "--format",
        choices=("md", "json"),
        default="md",
        help="output format / 输出格式",
    )
    report.set_defaults(func=cmd_report)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
