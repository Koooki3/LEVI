"""``levi automatic``: check, validate, dry-run, inspect and report AERI
runs (T-C-16). There is no real robot run in this version: ``run`` refuses
without ``--dry-run``, and a dry run drives the in-process fakes only
(``integrations/fr3_automatic/fake.py``), in a temporary folder, with no
motion authority over anything real.

    levi automatic doctor   [--config F] [--json]
    levi automatic validate --config F [--json]
    levi automatic run      --config F --dry-run [--episodes N] [--scenes S] [--keep DIR] [--json]
    levi automatic plan     --config F [--mode M] [--episodes N] [--scenes S] [--keep DIR] [--json]
    levi automatic run      --config F --mode dry_run --detach|--foreground [--expect-plan SHA]
    levi automatic runs     [--json]
    levi automatic status   --run-dir D [--json]
    levi automatic stop     --run R | --run-dir D           (typed confirmation at a terminal)
    levi automatic resume   --run R | --run-dir D           (typed confirmation at a terminal)
    levi automatic scene-answer --run R --request-id Q --predicates a=true,b=null
    levi automatic attach   --run R | --run-dir D --detach|--foreground
    levi automatic report   --run-dir D [--config F] [--truth T] [--format json|md]
    levi automatic label    --run-dir D --episode ID --value V [--principal P] [--json]

``plan``, ``run --mode``, ``runs``, ``stop``, ``resume``, ``scene-answer``
and ``attach`` go through the launch core (``launch.py``), the runner
(``runner.py``) and the command channel (``control.py``), T-CL-07..09.

Until ``levi automatic`` is wired into ``levi.cli``, run it as
``python -m levi.automatic.cli``.

The job file (``levi.aeri.job.v1``, checked by ``aeri.JobSpec``) uses the
same strict YAML subset as the Initial State Contract
(``scene_assessment.parse_document``); it is a draft like the contract
(HA-23). With ``human_assisted`` the reset policy's keys may be left out
(written anyway they are ignored and kept out of the plan)::

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
      strategy: single_reset_policy   # or human_assisted (aliases: single_policy)
      max_attempts: 1
      on_unknown: reset               # or wait_human
      scene_check: provider           # or operator_attested (human_assisted only)
      human_scene_timeout_s: 600      # operator_attested: then unavailable
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
import time
from dataclasses import asdict
from pathlib import Path

from levi.domain import aeri

from . import metrics
from . import scene_assessment as sa
from .journal import Journal
from .orchestrator import ConfigError, Orchestrator, RunConfig
from .recorder import MANIFEST, RolloutRecorder, SessionFiles
from .termination import TerminationConfig

JOB_SCHEMA = aeri.JOB_SCHEMA
MODES = aeri.EXECUTION_MODES
# What ``--mode`` takes: the launch core's dry run and the job's modes.
MODES_ALL = ("dry_run", *MODES)
# Configuration aliases of the reset strategies (design X2 §1.1); the plan,
# the journal and the contracts carry the code names only.
STRATEGY_ALIASES = aeri.RESET_STRATEGY_ALIASES
FAKE = (
    Path(__file__).resolve().parents[2] / "integrations" / "fr3_automatic" / "fake.py"
)
CONTRACTS = (
    Path(__file__).resolve().parents[2] / "docs" / "architecture" / "aeri" / "v1"
)
EXIT_OK, EXIT_PROBLEM, EXIT_REFUSED = 0, 1, 2


class JobError(ValueError):
    pass


# --- the job file ---------------------------------------------------------------------------


# RunConfig fields a human_assisted run never reads: left out of its plan, so
# the digest does not change with them (design X2 §1.3, G1).
RESET_POLICY_FIELDS = (
    "reset_folder",
    "reset_max_steps",
    "reset_enabled",
    "max_reset_attempts",
    "on_scene_unknown",
)


def _relative(path: Path, value: str) -> Path:
    found = Path(value)
    return found if found.is_absolute() else path.parent / found


def load_job(path) -> dict:
    """The job as ``{"config": RunConfig, "spec": JobSpec, "contract",
    "seed", "mode", "group", "rollout_root", "texts", "plan",
    "plan_sha256", "ignored", "warnings"}``; ``JobError`` says what is
    wrong.

    The plan (and its digest) holds what the run reads, with defaults
    filled in: the rollout root resolved (``realpath``, relative to the job
    file), the Initial State Contract with the sha256 of its file's bytes,
    and none of the keys the reset mode ignores (``ignored`` lists those)."""
    path = Path(path)
    try:
        raw = sa.parse_document(path.read_text())
    except OSError as exc:
        raise JobError(f"{path}: {exc.strerror or exc}") from None
    except sa.ContractError as exc:
        raise JobError(f"{path}: {exc}") from None
    try:
        spec = aeri.parse_job(raw)
    except aeri.AeriError as exc:
        raise JobError(exc.detail) from None
    run_id = spec.experiment.name
    contract, contract_sha = None, None
    if spec.task.initial_state_spec is not None:
        where = _relative(path, spec.task.initial_state_spec)
        try:
            data = where.read_bytes()
            contract = sa.load_contract(data.decode("utf-8"))
        except OSError as exc:
            raise JobError(f"task.initial_state_spec: {exc.strerror or exc}") from None
        except UnicodeDecodeError:
            raise JobError("task.initial_state_spec: not UTF-8 text") from None
        except sa.ContractError as exc:
            raise JobError(f"task.initial_state_spec: {exc}") from None
        contract_sha = hashlib.sha256(data).hexdigest()
    human = spec.human_assisted
    steps = {}
    if spec.policies.forward and spec.policies.forward.max_steps is not None:
        steps["forward_max_steps"] = spec.policies.forward.max_steps
    if not human and spec.policies.reset and spec.policies.reset.max_steps is not None:
        steps["reset_max_steps"] = spec.policies.reset.max_steps
    folders = {"forward_folder": spec.forward_folder}
    reset = {}
    if not human:
        folders["reset_folder"] = spec.reset_folder
        reset = {
            key: value
            for key, value in (
                ("reset_enabled", spec.reset.enabled),
                ("max_reset_attempts", spec.reset.max_attempts),
                ("on_scene_unknown", spec.reset.on_unknown),
            )
            if value is not None
        }
    attested = spec.reset.scene_check == "operator_attested"
    try:
        given = spec.termination.model_dump(exclude_none=True)
        if "goal_event_types" in given:
            given["goal_event_types"] = tuple(given["goal_event_types"])
        term = TerminationConfig(**given)
        config = RunConfig(
            run_id=run_id,
            plan_sha256="0" * 64,
            episodes=spec.experiment.episodes,
            reset_strategy=spec.reset_strategy,
            scene_check=spec.reset.scene_check,
            human_scene_timeout_ns=spec.human_scene_timeout_s * 1_000_000_000,
            initial_state=contract,
            termination=term,
            **folders,
            **reset,
            **steps,
        )
    except (ConfigError, TypeError, ValueError) as exc:
        raise JobError(str(exc)) from None
    texts = {"forward": spec.task.instruction or spec.forward_folder}
    if not human:
        texts["reset"] = spec.task.reset_instruction or spec.reset_folder
    root = spec.recording.rollout_root
    root = os.path.realpath(_relative(path, root)) if root is not None else None
    run = _plain(config)
    if human:
        for name in RESET_POLICY_FIELDS:
            run.pop(name)
    if not attested:
        run.pop("human_scene_timeout_ns")
    described = None
    if contract is not None:
        described = {**contract.describe(), "sha256": contract_sha}
    plan = {
        "schema_version": JOB_SCHEMA,
        "run": run,
        "contract": described,
        "mode": spec.experiment.execution_mode,
        "seed": spec.experiment.random_seed,
        "reset_mode": spec.reset_strategy,
        "scene_check": spec.reset.scene_check,
        "recording": {
            "group": spec.recording.group,
            "rollout_root": root,
            **folders,
        },
        "texts": texts,
    }
    digest = hashlib.sha256(
        json.dumps(plan, sort_keys=True, default=str).encode()
    ).hexdigest()
    config = RunConfig(**{**_fields(config), "plan_sha256": digest})
    return {
        "config": config,
        "spec": spec,
        "contract": contract,
        "seed": spec.experiment.random_seed,
        "mode": spec.experiment.execution_mode,
        "group": spec.recording.group,
        "rollout_root": root,
        "texts": texts,
        "plan": plan,
        "plan_sha256": digest,
        "ignored": spec.ignored(),
        "warnings": _warnings(config, contract),
    }


NO_CONTRACT = (
    "no task.initial_state_spec: without an Initial State Contract no scene "
    "is ever ready, so no forward episode can start (no reset runs either: "
    "the run waits for a person at every scene check). Fix: write an Initial "
    "State Contract and set task.initial_state_spec to its file"
)
SCENE_PROVIDER_MISSING = "E_SCENE_PROVIDER_MISSING"
NO_SCENE_CHECK = (
    f"{SCENE_PROVIDER_MISSING}: the human-assisted (policy evaluation only) "
    "mode needs a scene check that can answer: without one the run goes back "
    "and forth between WAIT_HUMAN and VERIFY_INITIAL after every resume. "
    "Configure a scene provider that is reachable (scene_check: provider), set "
    "reset.scene_check: operator_attested (a person answers each required "
    "predicate on a frame the system captures), or use --dry-run"
)
SCENE_UNREACHABLE = (
    f"{SCENE_PROVIDER_MISSING}: the scene provider does not answer its "
    "reachability check ({detail}); a human-assisted run would wait for a "
    "person after every resume"
)
SCENE_NOT_HUMAN = (
    f"{SCENE_PROVIDER_MISSING}: reset.scene_check operator_attested needs the "
    "person's scene check (provider human, levi/automatic/adapters/human.py), "
    "not {kind}"
)


def _warnings(config, contract) -> list:
    """What the operator must know (review C3 fixes, I-b)."""
    return [NO_CONTRACT] if contract is None else []


def scene_provider_problems(config, scene_provider) -> list:
    """``E_SCENE_PROVIDER_MISSING`` problems of a human-assisted run (design
    X2 G6): no provider, one whose ``reachable()`` says no (or raises), or,
    with ``scene_check: operator_attested``, a provider that is not the
    person's (``provider`` attribute ``human``). ``scene_provider`` is the
    provider object, or a name (``"fake"``, ``"human"``) for checks made
    before one exists; None: none is configured."""
    if config.reset_strategy != "human_assisted":
        return []
    if scene_provider is None:
        return [NO_SCENE_CHECK]
    kind = (
        scene_provider
        if isinstance(scene_provider, str)
        else getattr(scene_provider, "provider", type(scene_provider).__name__)
    )
    if config.scene_check == "operator_attested" and kind != "human":
        return [SCENE_NOT_HUMAN.format(kind=kind)]
    if isinstance(scene_provider, str):
        return []  # a name, checked before a provider exists (dry runs)
    probe = getattr(scene_provider, "reachable", None)
    if not callable(probe):
        # A provider that cannot say it answers counts as one that does not.
        return [SCENE_UNREACHABLE.format(detail="it has no reachable() check")]
    try:
        found = probe()
    except Exception as exc:  # noqa: BLE001 - any failure is "no"
        found = f"{type(exc).__name__}: {exc}"
    if found is not True:
        detail = found if isinstance(found, str) and found else "not reachable"
        return [SCENE_UNREACHABLE.format(detail=detail[:200])]
    return []


def launch_problems(job: dict, *, dry_run: bool, scene_provider) -> list:
    """Why a job may not be launched; empty when it may. A real run (not a
    dry run) needs an Initial State Contract; the human-assisted mode needs
    a scene check that can answer (``scene_provider_problems``; this
    version configures no provider for a real run, so a real human-assisted
    run is refused here until the launch entry of design X2 brings one)."""
    problems = []
    if not dry_run and job["contract"] is None:
        problems.append(NO_CONTRACT)
    problems += scene_provider_problems(job["config"], scene_provider)
    return problems


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


def session_folders(config) -> dict:
    """The C2 session file of each role that runs: no reset role in the
    human-assisted mode (design X2 G2)."""
    folders = {"forward": config.forward_folder}
    if config.reset_strategy != "human_assisted":
        folders["reset"] = config.reset_folder
    return folders


def dry_run_provider(config) -> str:
    """The scene provider a dry run uses: a scripted person when a person
    attests the scene, the fake machine provider otherwise."""
    return "human" if config.scene_check == "operator_attested" else "fake"


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

    def __init__(
        self,
        job: dict,
        folder: Path,
        *,
        episodes=None,
        scenes=(),
        restore: bool = False,
        crash_hook=None,
    ):
        """``restore``: take over the run already in ``folder`` after its
        runner stopped (``Orchestrator.restore``: the journal's recovery,
        FAULT_LOCKED unless completed, nothing replayed); the fake clock
        then starts after the journal's last time. ``crash_hook``: the
        orchestrator's crash points (tests)."""
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
        if restore:
            # The journal refuses a clock that goes back within its domain.
            seen = [e.mono_ns for e in Journal.read(self.directory).events]
            if seen:
                clock = fake.FakeClock(start_ns=max(seen) + 1_000_000_000)
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
        if cfg.scene_check == "operator_attested":
            # A scripted person answers on frames the dry run makes up.
            from .adapters import human

            shots = {"n": 0}

            def capture():
                shots["n"] += 1
                views = (contract.preferred_views if contract else ()) or ("side",)
                return {v: f"dry-run frame {v} {shots['n']}".encode() for v in views}

            self.scene = human.HumanSceneProvider(
                contract,
                capture,
                human.ScriptedTransport(scenes),
                clock,
                cfg.run_id,
                wait=clock.advance,
            )
        else:
            self.scene = ev.FakeSceneAssessor(
                [{**ready, "decision": d} for d in scenes],
                clock,
                cfg.run_id,
                default=ready,
                **scene_kw,
            )
        self.sessions = SessionFiles(
            self.folder,
            run_id=cfg.run_id,
            group=job["group"],
            folders=session_folders(cfg),
            texts=job["texts"],
        )
        build = Orchestrator.restore if restore else Orchestrator.create
        self.orch = build(
            self.directory,
            cfg,
            plan=job["plan"],
            crash_hook=crash_hook,
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
            scene=self.scene,
            clock=clock,
            fence=fence,
            listener=self.sessions,
        )

    def run(self) -> dict:
        try:
            state = self.orch.run()
            events = self.orch.journal.events
            try:
                manifest = json.loads((self.directory / MANIFEST).read_text())
            except FileNotFoundError:
                manifest = None  # no episode was opened
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
        for name in (*aeri.SCHEMAS, *aeri.CONFIG_SCHEMAS)
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
            problems = launch_problems(job, dry_run=False, scene_provider=None)
            check(
                "launch",
                not problems,
                "; ".join(problems) or "a real run could be launched",
                required=False,
            )
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
    problems = launch_problems(
        job,
        dry_run=args.dry_run,
        scene_provider=dry_run_provider(job["config"]) if args.dry_run else None,
    )
    if problems:
        message = "; ".join(problems)
        _print({"ok": False, "error": message}, args.json, f"refused: {message}")
        return EXIT_REFUSED
    # ``ignored`` is shown with the plan, never part of its digest.
    plan = {
        **job["plan"],
        "plan_sha256": job["plan_sha256"],
        "ignored": job["ignored"],
    }
    text = (
        (
            f"valid: run {job['config'].run_id}, {job['config'].episodes} episodes, "
            f"reset strategy {job['config'].reset_strategy}, scene check "
            f"{job['config'].scene_check}, contract "
            f"{job['contract'].key if job['contract'] else 'none (no scene is ever ready)'}, "
            f"plan {job['plan_sha256'][:12]}"
        )
        + "".join(f"\nwarning: {w}" for w in job["warnings"])
        + "".join(
            f"\nignored: {k} ({why})" for k, why in sorted(job["ignored"].items())
        )
    )
    _print({"ok": True, "plan": plan, "warnings": job["warnings"]}, args.json, text)
    return EXIT_OK


def cmd_run(args) -> int:
    if args.mode is not None:
        return cmd_run_mode(args)
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
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            message = f"{folder} is not an empty folder"
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
    except Exception as exc:  # noqa: BLE001 - a dry run reports, never a traceback
        message = f"the dry run failed: {type(exc).__name__}: {exc}"
        _print({"ok": False, "error": message}, args.json, message)
        return EXIT_PROBLEM
    finally:
        if not args.keep:
            shutil.rmtree(folder, ignore_errors=True)
    result["dry_run"] = True
    result["warnings"] = job["warnings"]
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


# --- launched runs (T-CL-09) -----------------------------------------------------------------


def _refuse(args, message: str, code: int = EXIT_REFUSED) -> int:
    _print({"ok": False, "error": message}, args.json, message)
    return code


def _request(args, mode, backend="systemd"):
    from . import launch

    overrides = {}
    if args.episodes is not None:
        overrides["episodes"] = args.episodes
    scenes = [x for x in (args.scenes or "").split(",") if x]
    if scenes:
        overrides["scenes"] = scenes
    return launch.LaunchRequest(
        job_path=os.path.abspath(args.config),
        execution_mode=mode,
        overrides=overrides,
        entry="cli",
        backend=backend,
        keep_dir=os.path.abspath(args.keep) if args.keep else None,
    )


def _plan_text(found) -> str:
    lines = [
        (
            f"plan {found.plan_sha256[:12] if found.plan_sha256 else '(none)'}: run "
            f"{found.run_id}, {found.execution_mode}, reset {found.reset_mode}, scene "
            f"check {found.scene_check}, {found.episodes} episodes"
        ),
        f"launchable: {'yes' if found.launchable else 'no'}"
        + (f" ({', '.join(found.refusals)})" if found.refusals else ""),
    ]
    for check in found.checks:
        if not check["ok"]:
            lines.append(f"{check['severity']}: {check['code']}: {check['detail']}")
    return "\n".join(lines)


def cmd_plan(args) -> int:
    """The plan of a job file, as ``launch.plan`` gives it (the API gives
    the same digest)."""
    from . import launch

    found = launch.plan(_request(args, args.mode))
    _print(found.public(), args.json, _plan_text(found))
    return EXIT_OK if found.launchable else EXIT_REFUSED


def cmd_run_mode(args) -> int:
    """``run --mode M --detach|--foreground``: only dry_run launches."""
    from . import launch

    if args.dry_run:
        return _refuse(args, "--dry-run and --mode exclude each other / 二者只能选一")
    if args.mode != launch.DRY_RUN:
        return _refuse(
            args,
            f"E_NO_ROBOT_ADAPTER: {args.mode} needs a real robot adapter, which this "
            "version does not have; use --mode dry_run / 本版本没有真实机器人适配器",
        )
    if args.detach == args.foreground:
        return _refuse(
            args, "give one of --detach, --foreground / 选择 --detach 或 --foreground"
        )
    backend = "systemd" if args.detach else "foreground"
    request = _request(args, args.mode, backend)
    found = launch.plan(request)
    if args.expect_plan and args.expect_plan != found.plan_sha256:
        return _refuse(
            args,
            f"E_PLAN_CHANGED: the plan is {found.plan_sha256}, not {args.expect_plan} "
            "/ 计划已变化",
        )
    try:
        handle = launch.launch(
            request,
            plan_sha256=found.plan_sha256,
            launch_token=found.launch_token,
            wait_s=args.wait if args.detach else 0.0,
        )
    except launch.LaunchRefused as exc:
        return _refuse(args, f"{exc.code}: {exc.detail}")
    value = {"ok": True, "plan_sha256": found.plan_sha256, **handle.public()}
    if args.detach:
        text = (
            f"started {handle.unit} (run {handle.run_id}, plan "
            f"{found.plan_sha256[:12]}); run folder {handle.run_dir}"
        )
        _print(value, args.json, text)
        return EXIT_OK
    value["ok"] = handle.exit_code == EXIT_OK
    text = f"run {handle.run_id}: {handle.final_state}; run folder {handle.run_dir}"
    _print(value, args.json, text)
    return handle.exit_code if handle.exit_code is not None else EXIT_PROBLEM


def cmd_runs(args) -> int:
    from . import launch

    found = launch.runs()
    lines = [
        f"{r.get('run_id')}: {r.get('state') or '-'}"
        f" ({'runner alive' if r.get('runner_alive') else 'no runner'}, "
        f"{r.get('backend')}) {r.get('run_dir')}"
        for r in found
    ]
    _print({"ok": True, "runs": found}, args.json, "\n".join(lines) or "no runs")
    return EXIT_OK


def _launched(args) -> Path:
    """The run folder named by ``--run`` (the index) or ``--run-dir``."""
    from . import launch

    if args.run_dir:
        run_dir = Path(os.path.abspath(args.run_dir))
    else:
        try:
            run_dir = Path(launch.read_record(launch.index_path(args.run))["run_dir"])
        except (OSError, ValueError, KeyError, TypeError, launch.LaunchRefused):
            raise JobError(
                f"no run {args.run} in the index / 索引里没有该运行"
            ) from None
    if not (run_dir / launch.LAUNCH_RECORD).is_file():
        raise JobError(f"{run_dir} is not a launched run / 不是已启动的运行")
    return run_dir


def _ask(question: str) -> str | None:
    """One typed answer at the terminal (the question on stderr)."""
    print(question, end="", file=sys.stderr, flush=True)
    try:
        return input().strip()
    except (EOFError, KeyboardInterrupt):
        return None


NOT_A_TERMINAL = (
    "this command must be confirmed at a terminal; nothing was written / "
    "必须在终端里输入确认，未写入任何内容"
)


def _send(args, run_dir: Path, value: dict) -> int:
    """Queue a confirmed command, wait for the runner's result."""
    from . import control, launch

    try:
        queued = control.write_command(run_dir, value)
    except control.CommandError as exc:
        return _refuse(args, f"{exc.code}: {exc.detail}")
    launch.audit(
        {
            "at_wall_ns": time.time_ns(),
            "phase": "issued",
            "run_dir": str(run_dir),
            "command_id": value["command_id"],
            "kind": value["kind"],
            "principal_id": value["principal_id"],
            "queued": queued,
        }
    )
    result = control.wait_result(run_dir, value["command_id"], args.wait)
    if result is None:
        _print(
            {"ok": None, "queued": queued, "command_id": value["command_id"]},
            args.json,
            f"queued {value['command_id']} ({queued}); no result within {args.wait} s "
            "/ 已排队，尚无结果",
        )
        return EXIT_PROBLEM
    text = (
        f"{value['kind']} {value['command_id']}: {result.get('code')} "
        f"(run {result.get('state')})"
        + (" [repeated]" if result.get("repeated") else "")
    )
    _print(
        {"ok": bool(result.get("ok")), "queued": queued, "result": result},
        args.json,
        text,
    )
    return EXIT_OK if result.get("ok") else EXIT_REFUSED


def _operator_checks(args):
    """The run folder, its journal and a live runner, or a refusal text."""
    from . import control, launch

    if not control.ID.fullmatch(args.principal):
        return (
            None,
            None,
            "--principal is an opaque id (no names, no addresses) / 只能是不透明 ID",
        )
    if args.command_id is not None and not control.ID.fullmatch(args.command_id):
        return None, None, "--command-id is an opaque id / 命令 ID 格式不对"
    try:
        run_dir = _launched(args)
    except JobError as exc:
        return None, None, str(exc)
    scan = Journal.read(run_dir)
    try:
        runner_record = launch.read_record(run_dir / launch.RUNNER_RECORD)
    except (OSError, ValueError):
        runner_record = None
    if not launch.runner_alive(runner_record):
        return (
            None,
            None,
            (
                "E_NO_RUNNER: no runner serves this run; attach first "
                "(levi automatic attach) / 没有运行器，先 attach"
            ),
        )
    return run_dir, scan, None


def cmd_stop(args) -> int:
    from . import control

    run_dir, scan, problem = _operator_checks(args)
    if problem:
        return _refuse(args, problem)
    if not sys.stdin.isatty():
        return _refuse(args, NOT_A_TERMINAL)
    run_id = scan.events[0].run_id if scan.events else run_dir.name
    answer = _ask(
        f"Stop run {run_id} (state {scan.effective_state}): an episode under way is "
        f"brought to an end first. Type `stop {run_id}` to confirm / 输入 "
        f"`stop {run_id}` 确认: "
    )
    if answer != f"stop {run_id}":
        return _refuse(args, "not confirmed; nothing was written / 未确认，未写入")
    value = control.command(
        "stop", args.command_id or control.new_command_id("stop"), args.principal
    )
    return _send(args, run_dir, value)


def cmd_resume(args) -> int:
    from . import control

    run_dir, scan, problem = _operator_checks(args)
    if problem:
        return _refuse(args, problem)
    state = scan.effective_state
    if state not in ("WAIT_HUMAN", "FAULT_LOCKED"):
        return _refuse(
            args,
            f"not_waiting: run is {state}, not waiting for a person / 运行没有在等人",
        )
    if not sys.stdin.isatty():
        return _refuse(args, NOT_A_TERMINAL)
    run_id = scan.events[0].run_id
    expected = args.expected_seq if args.expected_seq is not None else len(scan.events)
    last = next((e for e in reversed(scan.events) if e.record == "committed"), None)
    print(
        f"Run {run_id} waits in {state}"
        + (f" ({last.reason})" if last is not None else "")
        + f"; the resume applies at line {expected}. / 运行在 {state} 等待。",
        file=sys.stderr,
    )
    handled = _ask(
        "The environment was handled (scene put back, workspace clear)? "
        "Type yes / 环境已处理好？输入 yes: "
    )
    health = _ask(
        "The robot's health was checked again? Type yes / 已重新检查机器人健康？输入 yes: "
    )
    answer = _ask(f"Type `resume {run_id}` to confirm / 输入 `resume {run_id}` 确认: ")
    if handled != "yes" or health != "yes" or answer != f"resume {run_id}":
        return _refuse(args, "not confirmed; nothing was written / 未确认，未写入")
    value = control.command(
        "resume",
        args.command_id or control.new_command_id("resume"),
        args.principal,
        expected_seq=expected,
        environment_handled=True,
        health_rechecked=True,
    )
    return _send(args, run_dir, value)


def _predicates(text: str) -> dict:
    values = {"true": True, "false": False, "null": None}
    out = {}
    for item in (text or "").split(","):
        name, sep, value = item.strip().partition("=")
        if not sep or value not in values or not name:
            raise JobError(
                "--predicates is name=true|false|null, comma separated / 格式为 名称=true|false|null"
            )
        out[name] = values[value]
    return out


def cmd_scene_answer(args) -> int:
    from . import control

    run_dir, _, problem = _operator_checks(args)
    if problem:
        return _refuse(args, problem)
    try:
        predicates = _predicates(args.predicates)
        value = control.command(
            "scene_answer",
            args.command_id or control.new_command_id("scene_answer"),
            args.principal,
            request_id=args.request_id,
            predicates=predicates,
        )
    except (JobError, control.CommandError) as exc:
        return _refuse(args, str(exc))
    if not sys.stdin.isatty():
        return _refuse(args, NOT_A_TERMINAL)
    shown = ", ".join(
        f"{k}={'null' if v is None else str(v).lower()}" for k, v in predicates.items()
    )
    answer = _ask(
        f"Answer {args.request_id}: {shown}. Type `answer {args.request_id}` to "
        f"confirm / 输入 `answer {args.request_id}` 确认: "
    )
    if answer != f"answer {args.request_id}":
        return _refuse(args, "not confirmed; nothing was written / 未确认，未写入")
    return _send(args, run_dir, value)


def cmd_attach(args) -> int:
    from . import launch

    if args.detach == args.foreground:
        return _refuse(
            args, "give one of --detach, --foreground / 选择 --detach 或 --foreground"
        )
    try:
        run_dir = _launched(args)
        handle = launch.attach(
            run_dir,
            backend="systemd" if args.detach else "foreground",
            wait_s=args.wait if args.detach else 0.0,
        )
    except JobError as exc:
        return _refuse(args, str(exc))
    except launch.LaunchRefused as exc:
        return _refuse(args, f"{exc.code}: {exc.detail}")
    text = f"attached {handle.unit or 'here'}: run {handle.run_id}" + (
        f", {handle.final_state}" if handle.final_state else ""
    )
    _print({"ok": True, **handle.public()}, args.json, text)
    if args.detach:
        return EXIT_OK
    return handle.exit_code if handle.exit_code is not None else EXIT_PROBLEM


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


def _refuse(args, message: str) -> int:
    _print({"ok": False, "error": message}, args.json, message)
    return EXIT_REFUSED


def cmd_label(args) -> int:
    """An operator's outcome label for an ended forward episode (T-CL-14),
    confirmed by typing the value at a terminal. The automatic verdict is
    never shown here (the operator judges first); only
    ``labels/operator_label.jsonl`` is written."""
    run_dir = Path(args.run_dir)
    if not metrics.PRINCIPAL.fullmatch(args.principal):
        return _refuse(
            args,
            "--principal is an opaque id (no names, no addresses) / 只能是不透明 ID",
        )
    try:
        ended = metrics.ended_forward(Journal.read(run_dir).events)
        current = metrics.LabelStore(run_dir).latest("operator_label")
    except metrics.LabelRefused as exc:
        return _refuse(args, str(exc))
    except OSError as exc:  # not a run folder (a file, no permission)
        return _refuse(args, f"{run_dir}: {exc.strerror or exc}")
    if args.episode not in ended:
        return _refuse(
            args,
            f"{args.episode} is not a forward episode of this run that has ended "
            "/ 不是本运行中已结束的 forward 片段",
        )
    if not sys.stdin.isatty():
        return _refuse(
            args,
            "levi automatic label must be confirmed at a terminal; nothing was "
            "written / 必须在终端里确认，未写入任何内容",
        )
    now = current.get(args.episode)
    # Nothing the run decided, nor how the episode ended (an early stop
    # means the detector fired): the operator judges blind.
    question = (
        f"Label {args.episode} as {args.value}; "
        f"current label: {now or 'none yet'} / 当前标签：{now or '未标'}. "
        f"Type {args.value} to confirm / 输入 {args.value} 确认: "
    )
    # The question goes to stderr: stdout stays the result (``--json``).
    print(question, end="", file=sys.stderr, flush=True)
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        answer = None
    if answer is None or answer.strip() != args.value:
        return _refuse(args, "not confirmed; nothing was written / 未确认，未写入")
    try:
        # The write checks again that the episode has ended.
        record = metrics.label_operator(
            run_dir, args.episode, args.value, by=args.principal, note=args.note
        )
    except metrics.LabelRefused as exc:
        return _refuse(args, str(exc))
    text = (
        f"labelled {args.episode}: {args.value} (by {args.principal}"
        + (f", replaces {now}" if now else "")
        + ")"
    )
    _print({"ok": True, "record": record}, args.json, text)
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
                f"Reset mode: {found.get('reset_mode') or 'unknown'}"
                f" (from {(found.get('mode_source') or {}).get('reset_mode', 'unknown')}); "
                f"scene check: {found.get('scene_check') or 'unknown'}. "
                "Only the fields in `comparable` compare across reset modes."
            ),
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
            "Automatic evaluation pipeline (AERI): check, validate, plan, dry-run "
            "on fakes (here, or launched in a systemd user unit), stop, resume, "
            "attach, inspect and report runs. No real robot run in this version. / "
            "自动测评流水线（AERI）：检查、校验、计划、用 Fake 试运行（本进程或 "
            "systemd 用户单元）、停止、恢复、接管、查看和报告运行。本版本不能在真机上运行。"
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
    validate.add_argument(
        "--dry-run",
        action="store_true",
        help="check for a dry run on the fakes, not a real run / 按 Fake 试运行校验，而不是真机运行",
    )
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
        help="run on the fakes here, in a temporary folder / 在本进程用 Fake 试运行（临时目录）",
    )
    run.add_argument(
        "--mode",
        choices=MODES_ALL,
        default=None,
        help="launch a run through the launch core; only dry_run runs in this "
        "version (other modes exit 2) / 经启动核心启动；本版本只能 dry_run（其他模式退出码 2）",
    )
    hosting = run.add_mutually_exclusive_group()
    hosting.add_argument(
        "--detach",
        action="store_true",
        help="with --mode: run in a systemd user unit (levi-aeri-<run id>) / "
        "配合 --mode：在 systemd 用户单元里运行",
    )
    hosting.add_argument(
        "--foreground",
        action="store_true",
        help="with --mode: run in this terminal / 配合 --mode：在本终端前台运行",
    )
    run.add_argument(
        "--wait",
        type=float,
        default=10.0,
        help="with --detach: seconds to wait for the runner to name itself / "
        "配合 --detach：等待运行器报到的秒数",
    )
    run.add_argument(
        "--expect-plan",
        metavar="SHA",
        help="with --mode: refuse unless the plan is still this one / 配合 --mode：计划摘要不符就拒绝",
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

    plan = sub.add_parser(
        "plan",
        help="the launch plan of a job file, read-only / 只读：作业文件的启动计划",
        description="What a launch would run, every check and the plan digest "
        "(the same as the API's for the same file). Writes nothing but the "
        "core key under LEVI_AERI_HOME, once. Exit 0 when launchable. / "
        "启动会运行什么、各项检查和计划摘要（与 API 对同一文件算出的相同）；"
        "除首次创建 LEVI_AERI_HOME 下的核心密钥外不写任何文件；可启动时退出码 0。",
    )
    plan.add_argument("--config", required=True, help=config_help)
    plan.add_argument(
        "--mode",
        choices=MODES_ALL,
        default=None,
        help="execution mode (default: the job's) / 执行模式（默认取作业文件）",
    )
    plan.add_argument(
        "--episodes",
        type=int,
        default=None,
        help="override the episode count / 覆盖片段数",
    )
    plan.add_argument(
        "--scenes",
        default="",
        help="dry run only: scene decisions the fake answers first / 仅试运行：Fake 依次给出的场景结论",
    )
    plan.add_argument(
        "--keep",
        metavar="DIR",
        help="dry run only: write the run in this new or empty folder / 仅试运行：运行写在此空目录",
    )
    plan.add_argument("--json", action="store_true", help=json_help)
    plan.set_defaults(func=cmd_plan)

    runs = sub.add_parser(
        "runs",
        help="the launched runs (LEVI_AERI_HOME index), read-only / 只读：已启动的运行",
    )
    runs.add_argument("--json", action="store_true", help=json_help)
    runs.set_defaults(func=cmd_runs)

    def which_run(command):
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument("--run", help="run id (from levi automatic runs) / 运行 ID")
        group.add_argument("--run-dir", help="<root>/.aeri/runs/<run id> / 运行目录")

    def operator_options(command):
        command.add_argument(
            "--principal",
            default="operator",
            help="opaque id of who gives the command (no names, no addresses) / "
            "下达命令者的不透明 ID（不写姓名、邮箱）",
        )
        command.add_argument(
            "--command-id",
            default=None,
            help="send again with the same id to repeat safely (once only) / "
            "用同一 ID 重发不会重复执行",
        )
        command.add_argument(
            "--wait",
            type=float,
            default=10.0,
            help="seconds to wait for the runner's result / 等待运行器结果的秒数",
        )
        command.add_argument("--json", action="store_true", help=json_help)

    stop = sub.add_parser(
        "stop",
        help="stop a launched run (typed confirmation at a terminal) / 停止已启动的运行（须在终端输入确认）",
        description="Writes a stop to the run's command channel after you type "
        "`stop <run id>` at a terminal; refused without a terminal. An episode "
        "under way ends first. / 在终端输入 `stop <运行 ID>` 后写入命令通道；"
        "没有终端就拒绝。",
    )
    which_run(stop)
    operator_options(stop)
    stop.set_defaults(func=cmd_stop)

    resume = sub.add_parser(
        "resume",
        help="resume a run that waits for a person (typed confirmation) / 恢复等人的运行（须输入确认）",
        description="Both confirmations (environment handled, health checked "
        "again) and `resume <run id>` typed at a terminal; refused without a "
        "terminal. The run then goes through PREFLIGHT and a fresh initial-state "
        "check. / 须在终端确认两项（环境已处理、健康已复查）并输入 "
        "`resume <运行 ID>`；之后运行重新经过 PREFLIGHT 和初始状态核对。",
    )
    which_run(resume)
    resume.add_argument(
        "--expected-seq",
        type=int,
        default=None,
        help="the journal line the resume applies at (default: the next) / 恢复对应的日志行号",
    )
    operator_options(resume)
    resume.set_defaults(func=cmd_resume)

    answer = sub.add_parser(
        "scene-answer",
        help="answer a person's scene question (typed confirmation) / 回答人工场景核对题（须输入确认）",
    )
    which_run(answer)
    answer.add_argument(
        "--request-id", required=True, help="the question's request id / 题目的请求 ID"
    )
    answer.add_argument(
        "--predicates",
        required=True,
        help="name=true|false|null, comma separated (null: cannot tell) / "
        "名称=true|false|null，逗号分隔（null 表示看不清）",
    )
    operator_options(answer)
    answer.set_defaults(func=cmd_scene_answer)

    attach = sub.add_parser(
        "attach",
        help="bring back a run whose runner is gone (FAULT_LOCKED) / 接管运行器已退出的运行（进入 FAULT_LOCKED）",
        description="A new runner restores the run from its journal: it is "
        "locked (FAULT_LOCKED, recovery_ambiguous), nothing is replayed, and "
        "only a resume leads on. / 新运行器从日志恢复运行：锁定在 FAULT_LOCKED，"
        "不重放任何动作，只能由 resume 继续。",
    )
    which_run(attach)
    hosted = attach.add_mutually_exclusive_group()
    hosted.add_argument(
        "--detach",
        action="store_true",
        help="in a systemd user unit / 在 systemd 用户单元里",
    )
    hosted.add_argument(
        "--foreground", action="store_true", help="in this terminal / 在本终端前台"
    )
    attach.add_argument(
        "--wait",
        type=float,
        default=10.0,
        help="with --detach: seconds to wait for the runner to name itself / "
        "配合 --detach：等待运行器报到的秒数",
    )
    attach.add_argument("--json", action="store_true", help=json_help)
    attach.set_defaults(func=cmd_attach)

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

    label = sub.add_parser(
        "label",
        help="an operator's outcome label for an ended episode / 操作员给已结束片段的成败标签",
        description="Append an operator's label (success, failure, discarded, "
        "unclear) for a forward episode of the run that has ended, during the "
        "run, while it waits for a person, or after it. Confirmed by typing the "
        "value at a terminal; the automatic verdict is not shown; only "
        "labels/operator_label.jsonl is written. / 给运行中已结束的 forward 片段"
        "追加操作员标签（success、failure、discarded、unclear），运行中、等人时、"
        "结束后都可以；须在终端输入该值确认；不显示自动判定；只写 "
        "labels/operator_label.jsonl。",
    )
    label.add_argument(
        "--run-dir", required=True, help="<root>/.aeri/runs/<run id> / 运行目录"
    )
    label.add_argument(
        "--episode",
        required=True,
        help="forward episode id (<run id>.forward.NNNN) / forward 片段 ID",
    )
    label.add_argument(
        "--value",
        required=True,
        choices=metrics.OPERATOR_VALUES,
        help="the operator's judgement / 操作员的判定",
    )
    label.add_argument(
        "--principal",
        default="operator",
        help="opaque id of who labels (no names, no addresses) / 标注者的不透明 ID（不写姓名、邮箱）",
    )
    label.add_argument("--note", default="", help="short note / 简短备注")
    label.add_argument("--json", action="store_true", help=json_help)
    label.set_defaults(func=cmd_label)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
