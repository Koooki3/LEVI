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
