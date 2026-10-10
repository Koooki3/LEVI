"""The campaign controller process and the planning that feeds it (T-API-2,
design X3 §1.6, §1.7, ``levi2/design/aeri-ui-api-contract.md`` §4).

    python -m levi.automatic.campaign.controller --campaign-id C

One controller process drives one campaign: it opens the campaign journal
(the only writer), builds the world the conductor needs for the campaign's
host and calls ``Conductor.advance`` until the campaign ends. It runs under
``systemd-run --user`` (its own unit ``levi-aeri-campaign-<id>``, never the
product's cgroup, no ``Restart``), so restarting the product does not stop
a campaign. A controller that died is brought back by a person
(``POST .../attach``): the conductor recovers to a place where a person
confirms (``Conductor.attach``).

**Hosts** (``execution_mode`` of the plan request, frozen in
``ctl/host.json``):

- ``dry_run``: ``DryRunPolicyHost`` records and starts nothing,
  ``DryRunLauncher`` starts one real dry run (a fake robot) per segment;
  the trial ledger comes from the dry runs' journals. The whole chain can
  be rehearsed without a robot.
- ``guided``: the user's own legacy client does the robot work.
  ``ManualPolicyHost`` turns each policy switch into a request to the
  person; ``GuidedLauncher`` shows the legacy client's command and waits,
  passively, until the client wrote the segment's trials or the person says
  the segment is done. The backend never starts a policy server and never
  connects to a policy or a robot port.

**Planning.** ``prepare`` turns the page's request (a job, the arms as
checkpoint ids, the schedule) into a campaign job file, expands it with
``spec.plan_campaign`` and returns the plan with its checks. A preview is
planned in a scratch folder; ``final=True`` writes the campaign's job files
under ``$LEVI_AERI_HOME/campaign-jobs/campaigns/<id>/`` (new files only).
The campaign id is derived from the request, so the same request gives the
same plan and the same ``campaign_sha256``.

**Reports.** ``write_reports`` derives the trial ledger and writes
``campaigns/<id>/report/<basis>/`` with ``report.write_report``; the
reporter of the conductor calls it at ``ANALYZING``.

**Peeks.** A person's look at the results before the campaign ended is
journaled as a ``unblind_peek`` note; the report counts them and falls back
to exploratory conclusions (design X3 §4.4).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from levi.automatic import analysis as an
from levi.automatic import launch
from levi.automatic import scene_assessment as sa
from levi.children import identity
from levi.setup import recipes

from . import adapters as A
from . import guided as G
from . import ledger as L
from . import report as R
from . import schedule as sched
from . import spec
from .conductor import Conductor, ConductorError, RobotLock, aeri_home, campaign_dir
from .journal import JOURNAL, CampaignJournalError

ROLLOUT_ROOT_ENV = "LEVI_AERI_ROLLOUT_ROOT"
JOBS_FOLDER = "campaign-jobs"
UNIT_PREFIX = "levi-aeri-campaign-"
HOST_FILE = "host.json"
GUIDED_FILE = "guided.json"
CONTROLLER_FILE = "controller.json"
ERROR_FILE = "controller-error.json"
POLICY_PORT = 8000  # recorded in the plan; the backend never connects to it
MAX_GUIDE_BYTES = 2 << 20
GUIDED_READY_TIMEOUT_S = 7 * 24 * 3600.0
DRY_READY_TIMEOUT_S = 600.0
POLL_S = 0.25
HEARTBEAT_S = 2.0
# How a controller is hosted. Tests replace it (``inprocess`` runs the
# controller in a thread of the calling process).
CONTROLLER_BACKEND = "systemd"
# How the dry runs of a dry-run campaign are hosted (``launch`` backends).
CHILD_BACKEND = "systemd"
CHILD_DEADLINE_S: float | None = None
# The report's resamples and PDF output; tests lower them.
REPORT_RESAMPLES = 10_000
REPORT_PDF = True
MAX_CARDS = 999
LABEL_BASES = L.LABEL_BASES


class PlanRefused(Exception):
    """A request that cannot become a campaign: ``status``, ``code`` and,
    per offending field, ``errors``."""

    def __init__(self, status: int, code: str, message: str, errors=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.errors = errors or []


# --- the plan ----------------------------------------------------------------------------


def jobs_root(home=None) -> Path:
    return launch.home(home) / JOBS_FOLDER


def job_folder(campaign_id: str, home=None) -> Path:
    return spec.campaign_folder(jobs_root(home), campaign_id)


def campaign_id_of(request: dict) -> str:
    """The same request always names the same campaign."""
    text = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return "c-" + hashlib.sha256(text.encode()).hexdigest()[:10]


# A dry run asks the fake scene for visible evidence of a contract; the
# wizard's jobs name none, so a dry-run campaign brings its own (draft).
DRY_CONTRACT_FILE = "dry-run-initial-state.yaml"
DRY_CONTRACT = """initial_state:
  id: dry-run-initial
  version: "1"
  predicates:
    required: [object_at_source, gripper_open]
  observations:
    require_visible_evidence: true
    min_evidence_refs: 1
"""


def _layouts_text(count: int) -> str:
    lines = ["schema_version: levi.aeri.layouts.v1", "cards:"]
    for number in range(1, count + 1):
        lines += [f"  c{number:03d}:", f"    description: Layout card {number}"]
    return "\n".join(lines) + "\n"


def _read_job(path: str) -> dict:
    try:
        raw = sa.parse_document(Path(path).read_text())
    except (OSError, sa.ContractError) as exc:
        raise PlanRefused(
            422, "job_invalid", f"The job cannot be read: {str(exc)[:200]}"
        ) from None
    if not isinstance(raw, dict):
        raise PlanRefused(422, "job_invalid", "The job file is not a mapping")
    return raw


def _arm_block(entry, role: str) -> dict:
    status = (
        "none"
        if entry.sha256_state != "recorded"
        else ("verified" if entry.params_hashed else "recorded")
    )
    block = {
        "role": role,
        "policy_forward": {
            "checkpoint_dir": entry.path,
            "config": entry.config,
            "port": POLICY_PORT,
        },
    }
    records = [r.to_dict() for r in entry.sha256_records]
    if status != "none" and records:
        block["checkpoint"] = {
            "sha256_status": status,
            "manifest_sha256": spec.digest(records),
        }
    return block


def read_guide() -> str:
    """The operator guide's text (``LEVI_SETUP_DOC``), or ``ValueError``."""
    doc = os.environ.get(recipes.ENV_DOC, "").strip()
    if not doc:
        raise ValueError(f"{recipes.ENV_DOC} is not set")
    try:
        path = Path(doc).expanduser()
        if path.stat().st_size > MAX_GUIDE_BYTES:
            raise ValueError("the operator guide is too large")
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"the operator guide cannot be read ({getattr(exc, 'strerror', None) or exc})"
        ) from None


@dataclass
class Prepared:
    campaign_id: str
    host: str
    plan: spec.CampaignPlan
    checks: list = field(default_factory=list)
    refusals: list = field(default_factory=list)
    base_command: dict | None = None
    rollout_root: str | None = None
    directory: Path | None = None


def _check(prepared_checks, code, ok, detail, severity="info") -> None:
    prepared_checks.append(
        {
            "code": code,
            "ok": bool(ok),
            "severity": severity if not ok else "info",
            "detail": str(detail)[:300],
        }
    )


def prepare(
    request: dict,
    *,
    job_path: str,
    forward: dict,
    final: bool,
    home=None,
) -> Prepared:
    """Plan the campaign of ``request`` (the page's body without the
    start-only fields). ``forward``: checkpoint id -> discovery entry.
    ``final``: write the job files to their place; otherwise a scratch
    folder that is removed again. ``PlanRefused`` for a request that cannot
    be planned at all; problems that only forbid the start (no guide, no
    rollout root) are ``refusals`` of the result."""
    host = request.get("execution_mode") or "guided"
    if host not in A.HOSTS:
        raise PlanRefused(422, "campaign_invalid", f"execution_mode: {host}")
    errors = []
    arms_in = request["arms"]
    ids = [a["id"] for a in arms_in]
    if len(set(ids)) != len(ids):
        errors.append({"field": "arms", "message": "an arm id is used twice"})
    names = [a["checkpoint_id"] for a in arms_in]
    if len(set(names)) != len(names):
        errors.append({"field": "arms", "message": "two arms use the same checkpoint"})
    for index, arm in enumerate(arms_in):
        entry = forward.get(arm["checkpoint_id"])
        if entry is None:
            errors.append(
                {
                    "field": f"arms.{index}.checkpoint_id",
                    "message": "not a checkpoint the discovery found for this role",
                }
            )
        elif not entry.config:
            errors.append(
                {
                    "field": f"arms.{index}.checkpoint_id",
                    "message": "its config is not stated (VERSION.json): it cannot be paired",
                }
            )
    if sum(a["role"] == "reference" for a in arms_in) > 1:
        errors.append({"field": "arms", "message": "at most one reference arm"})
    if errors:
        raise PlanRefused(422, "campaign_invalid", "The plan is not valid", errors)
    raw = _read_job(job_path)
    reset = raw.get("reset") if isinstance(raw.get("reset"), dict) else {}
    human = reset.get("strategy") == "human_assisted"
    campaign_id = campaign_id_of(request)
    checks: list = []
    refusals: list = []
    document = json.loads(json.dumps(raw))
    document["experiment"] = {
        **(document.get("experiment") or {}),
        "name": "base",
    }
    document["experiment"].pop("episodes", None)
    rollout_root = None
    base_command = None
    if host == "guided":
        recording = document.get("recording")
        recording = dict(recording) if isinstance(recording, dict) else {}
        rollout_root = (
            recording.get("rollout_root")
            or os.environ.get(ROLLOUT_ROOT_ENV, "").strip()
        )
        if not rollout_root or not os.path.isabs(rollout_root):
            rollout_root = None
            refusals.append("rollout_root_missing")
            _check(
                checks,
                "rollout_root_missing",
                False,
                f"set {ROLLOUT_ROOT_ENV} (an absolute folder) or the job's "
                "recording.rollout_root: the legacy client's rollouts are read there",
                "error",
            )
        else:
            recording["rollout_root"] = rollout_root
            document["recording"] = recording
            _check(
                checks, "rollout_root", True, "the legacy client's rollouts are read"
            )
        try:
            prompt = (document.get("task") or {}).get("instruction")
            base = G.base_command(read_guide(), prompt=prompt)
            base_command = base.to_dict()
            _check(
                checks,
                "guide_command",
                True,
                f"operator guide section {base.section}, block {base.block}",
            )
        except (ValueError, G.GuidedError) as exc:
            refusals.append("guide_unavailable")
            _check(checks, "guide_unavailable", False, str(exc)[:250], "error")
    block = {
        "id": campaign_id,
        "robot": "main" if host == "guided" else "dryrun",
        "trials_per_arm": request["trials_per_arm"],
        "schedule": {
            "kind": request["schedule"]["kind"],
            "seed": request["schedule"]["seed"],
        },
        "blinding": {"operator": "arm_codes"},
        "pairing": {},
        "arms": {},
    }
    if request["schedule"].get("segment_trials") is not None:
        block["schedule"]["segment_trials"] = request["schedule"]["segment_trials"]
    for arm in arms_in:
        entry = forward[arm["checkpoint_id"]]
        block["arms"][arm["id"]] = _arm_block(entry, arm["role"])
        block["pairing"][entry.name] = entry.config
    ordered = sorted(arms_in, key=lambda a: (a["role"] != "candidate", a["id"]))
    reference = next((a["id"] for a in arms_in if a["role"] == "reference"), None)
    if reference is not None:
        candidate = next(a["id"] for a in ordered if a["id"] != reference)
        comparison = [candidate, reference]
    else:
        pair = sorted(ids)[:2]
        comparison = [pair[1], pair[0]]
    block["primary"] = {
        "metric": "success",
        "comparison": comparison,
        "alpha": request["primary"]["alpha"],
        "label_basis": request["primary"]["label_basis"],
        "preregistered": bool(request["preregistered"]),
    }
    dry_contract = host == "dry_run" and not (document.get("task") or {}).get(
        "initial_state_spec"
    )
    contract_path = jobs_root(launch.home(home)) / DRY_CONTRACT_FILE
    if dry_contract:
        document["task"] = {
            **(document.get("task") or {}),
            "initial_state_spec": str(contract_path),
        }
    layouts_text = None
    if human:
        size = (
            request["schedule"].get("segment_trials")
            or sched.DEFAULT_SEGMENT_TRIALS[request["schedule"]["kind"]]
        )
        count = min(MAX_CARDS, max(request["trials_per_arm"], size))
        layouts_text = _layouts_text(count)
        block["layouts"] = {"source": "card_set", "file": "layouts.yaml"}
    else:
        block["layouts"] = {"source": "none"}
    document["campaign"] = block

    home_path = launch.ensure_home(home)
    scratch = None
    try:
        if final:
            root = jobs_root(home_path)
            root.mkdir(mode=0o700, exist_ok=True)
        else:
            scratch = Path(tempfile.mkdtemp(prefix=".plan-", dir=home_path))
            root = scratch
        directory = spec.campaign_folder(root, campaign_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if layouts_text is not None:
                spec.write_new(directory / "layouts.yaml", layouts_text.encode())
            if dry_contract:
                # One fixed file for every dry-run campaign: its path is part of
                # the children's plans, so a preview and the real plan agree.
                contract_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                spec.write_new(contract_path, DRY_CONTRACT.encode())
            source = directory / "campaign.yaml"
            spec.write_new(source, spec.to_yaml(document).encode())
            planner = A.DryRunPlanner() if host == "dry_run" else None
            planned = spec.plan_campaign(source, job_root=root, planner=planner)
        except spec.CampaignError as exc:
            status = 409 if exc.code == "E_CAMPAIGN_EXISTS" else 422
            code = "campaign_exists" if status == 409 else "campaign_invalid"
            raise PlanRefused(
                status,
                code,
                exc.detail[:300],
                [{"field": None, "message": f"{exc.code}: {exc.detail[:250]}"}],
            ) from None
        prepared = Prepared(
            campaign_id=campaign_id,
            host=host,
            plan=planned,
            checks=checks,
            refusals=refusals,
            base_command=base_command,
            rollout_root=rollout_root,
            directory=directory if final else None,
        )
        _plan_checks(prepared, human)
        if base_command is not None and not refusals:
            try:
                _render_all(prepared)
            except (G.GuidedError, ValueError) as exc:
                refusals.append("command_unsafe")
                _check(checks, "command_unsafe", False, str(exc)[:250], "error")
        return prepared
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _plan_checks(prepared: Prepared, human: bool) -> None:
    schedule = prepared.plan.schedule
    _check(
        prepared.checks,
        "pairing",
        True,
        "every arm is served with the config its checkpoint states",
    )
    _check(
        prepared.checks,
        "schedule",
        True,
        f"{len(schedule.segments)} segments, {schedule.switches} policy starts",
    )
    if not schedule.balanced_cycles:
        _check(
            prepared.checks,
            "unbalanced_cycles",
            False,
            "the rounds do not make whole cycles of the design: order effects "
            "are not balanced and the conclusion is exploratory",
            "warn",
        )
    if schedule.conclusion_level == "exploratory":
        _check(
            prepared.checks,
            "exploratory_schedule",
            False,
            f"{schedule.kind}: conclusions can only be exploratory",
            "warn",
        )
    spec_ = prepared.plan.spec
    if not any(a.role == "reference" for a in spec_.block.arms.values()):
        _check(
            prepared.checks,
            "no_reference_arm",
            False,
            "no reference arm: drift cannot be checked",
            "warn",
        )
    if not spec_.block.primary or not spec_.block.primary.preregistered:
        _check(
            prepared.checks,
            "not_preregistered",
            False,
            "not preregistered: conclusions are exploratory",
            "warn",
        )
    if prepared.host == "dry_run" and human:
        _check(
            prepared.checks,
            "dry_run_needs_person",
            False,
            "human-assisted dry runs wait for a person to reset the scene after "
            "each episode (open the child run to resume it)",
            "warn",
        )


def _render_all(prepared: Prepared) -> list:
    base = G.BaseCommand.from_dict(prepared.base_command)
    layout = A.layout_of(prepared.plan.to_json())
    return G.segment_commands(
        base,
        layout,
        A.groups_of(prepared.plan.to_json()),
        codes=A.arm_codes(prepared.plan.to_json()),
    )


def power_of(request: dict, human: bool) -> dict:
    n = request["trials_per_arm"]
    alpha = request["primary"]["alpha"]
    table = an.power_table([n], [0.5, 0.3], alpha=alpha)
    design = "paired" if human else "unpaired"
    return {
        "detectable_difference": an.min_detectable_difference(
            n, 0.5, design=design, alpha=alpha
        ),
        "n": n,
        "design": design,
        "baseline": 0.5,
        "rows": table["rows"],
        "post_hoc_power": "not reported",
    }


def plan_view(prepared: Prepared, request: dict) -> dict:
    """The page's answer to a plan request (contract §4)."""
    plan = prepared.plan
    schedule = plan.schedule
    human = plan.spec.block.layouts.source == "card_set"
    return {
        "campaign_id": prepared.campaign_id,
        "execution_mode": prepared.host,
        "plan_sha256": plan.campaign_sha256,
        "campaign_sha256": plan.campaign_sha256,
        "settings_sha256": plan.settings_sha256,
        "segments": [
            {"no": s.index, "arms": [s.arm], "cards": list(s.slots)}
            for s in schedule.segments
        ],
        "switches": schedule.switches,
        "conclusion_level": schedule.conclusion_level,
        "power": power_of(request, human),
        "refusals": list(prepared.refusals),
        "checks": list(prepared.checks),
    }


# --- reports ------------------------------------------------------------------------------


def peeks_of(events) -> int:
    return sum(
        1
        for e in events
        if e.record == "note" and e.note is not None and e.note.code == "unblind_peek"
    )


def shared_task(plan: dict, job_dir) -> str:
    """The task instruction the children share (``task.instruction``)."""
    try:
        document = sa.parse_document(
            (Path(job_dir) / plan["children"][0]["file"]).read_text()
        )
    except (OSError, sa.ContractError):
        return ""
    value = (document.get("task") or {}).get("instruction")
    return value if isinstance(value, str) else ""


def campaign_info(plan: dict, peeks: int, *, basis: str | None = None, task: str = ""):
    """``report.CampaignInfo`` of a frozen plan."""
    block = plan["campaign"]
    primary = block.get("primary") or {}
    reference = None
    arms = []
    for arm_id in sorted(block["arms"]):
        arm = block["arms"][arm_id]
        forward = arm["policy_forward"]
        checkpoint = arm.get("checkpoint") or {}
        arms.append(
            R.ArmInfo(
                id=arm_id,
                role=arm.get("role", "candidate"),
                checkpoint=Path(str(forward["checkpoint_dir"]).rstrip("/")).name,
                config=forward.get("config"),
                sha256_status=checkpoint.get("sha256_status", "none"),
                manifest_sha256=checkpoint.get("manifest_sha256"),
                versions=arm.get("versions") or "",
            )
        )
        reference = reference or (arm_id if arm.get("role") == "reference" else None)
    blind = (block.get("blinding") or {}).get("operator", "arm_codes")
    return R.CampaignInfo(
        campaign_id=plan["campaign_id"],
        arms=tuple(arms),
        task=task,
        schedule=plan["schedule"]["kind"],
        seed=plan["schedule"]["seed"],
        trials_per_arm=block["trials_per_arm"],
        comparison=tuple(primary["comparison"]) if primary.get("comparison") else None,
        alpha=primary.get("alpha", 0.05),
        label_basis=basis or primary.get("label_basis", "operator_label"),
        preregistered=bool(primary.get("preregistered")),
        peeks=peeks,
        operator_blind="partial" if blind == "arm_codes" else "none",
        reset_mode=plan.get("reset_mode"),
        treatment_includes_reset=bool(block.get("treatment_includes_reset")),
        campaign_sha256=plan["campaign_sha256"],
        settings_sha256=plan["settings_sha256"],
        switches=plan["schedule"].get("switches"),
    )


def report_root(folder) -> Path:
    return Path(folder) / "report"


def write_reports(
    folder, plan: dict, host: str, state: str, events, *, rollout_root, basis=None
) -> dict:
    """Derive the ledger from what the children wrote and write
    ``report/<basis>/`` (the primary basis unless ``basis`` is given).
    Returns the report manifest."""
    primary = (plan["campaign"].get("primary") or {}).get("label_basis")
    basis = basis or primary or "operator_label"
    ledger, layout, runs = A.build_ledger(plan, host, folder, rollout_root=rollout_root)
    home = Path(folder).parent.parent
    task = shared_task(plan, job_folder(plan["campaign_id"], home))
    info = campaign_info(plan, peeks_of(events), basis=basis, task=task)
    root = report_root(folder)
    root.mkdir(mode=0o700, exist_ok=True)
    return R.write_report(
        root,
        ledger,
        info,
        basis,
        campaign_state=state,
        layout=layout,
        runs=runs,
        blinded=False,
        seed=plan["schedule"]["seed"],
        resamples=REPORT_RESAMPLES,
        pdf=REPORT_PDF,
    )


# --- the controller -----------------------------------------------------------------------


def read_host(folder) -> dict | None:
    value = A.read_json(A.ctl_dir(folder) / HOST_FILE)
    return value if isinstance(value, dict) else None


def write_host(folder, **fields) -> None:
    A.ensure_ctl(folder)
    A.write_json(A.ctl_dir(folder) / HOST_FILE, fields)


def controller_record(folder) -> dict | None:
    value = A.read_json(A.ctl_dir(folder) / CONTROLLER_FILE)
    return value if isinstance(value, dict) else None


def controller_alive(folder) -> bool:
    return launch.runner_alive(controller_record(folder))


def _heartbeat(folder, host: str, state: str) -> None:
    A.write_json(
        A.ctl_dir(folder) / CONTROLLER_FILE,
        {
            "pid": os.getpid(),
            "identity": identity(os.getpid()),
            "state": state,
            "host": host,
            "at_ms": time.time_ns() // 1_000_000,
        },
    )


def build_deps(folder, plan: dict, host: str, *, rollout_root):
    """The conductor's world for a host (module text)."""
    deps = {
        "confirmations": A.FileConfirmations(folder),
        "principal_id": "controller",
    }

    def reporter(frozen, events):
        write_reports(
            folder,
            frozen,
            host,
            "ANALYZING",
            events,
            rollout_root=rollout_root,
        )
        return "report_written"

    deps["reporter"] = reporter
    if host == "dry_run":
        deps.update(
            host=A.DryRunPolicyHost(folder),
            launcher=A.DryRunLauncher(
                backend=CHILD_BACKEND, deadline_s=CHILD_DEADLINE_S
            ),
            planner=A.DryRunPlanner(),
            ready_timeout_s=DRY_READY_TIMEOUT_S,
        )
    else:
        deps.update(
            host=A.ManualPolicyHost(folder),
            launcher=A.GuidedLauncher(folder, plan, rollout_root or ""),
            planner=spec.LoadJobPlanner(),
            ready_timeout_s=GUIDED_READY_TIMEOUT_S,
        )
    return deps


def _take_unblind(folder, conductor) -> None:
    for request in A.unblind_requests(folder):
        command = request["command_id"]
        if not conductor.journal.by_command(command):
            conductor.journal.note(
                "unblind_peek",
                "results looked at before the campaign ended",
                authority=conductor._auth("operator", request["principal_id"], command),
            )
        A.retire_unblind(folder, command)


def serve(
    campaign_id: str,
    *,
    home=None,
    stop=None,
    poll_s: float = POLL_S,
    max_steps: int | None = None,
) -> int:
    """Drive one campaign until it ends (exit 0), the journal or the world
    breaks (exit 1), or ``stop`` is set (exit 0, the campaign stays where
    it is). ``max_steps``: tests."""
    home = Path(home) if home is not None else aeri_home()
    folder = campaign_dir(home, campaign_id)
    stop = stop or threading.Event()
    host_record = read_host(folder)
    if host_record is None or host_record.get("host") not in A.HOSTS:
        return _fail(folder, "E_HOST", "the campaign has no host record")
    host = host_record["host"]
    job_dir = job_folder(campaign_id, home)
    try:
        plan = spec.read_plan(job_dir / spec.PLAN_FILE)
    except spec.CampaignError as exc:
        return _fail(folder, "E_PLAN", exc.detail)
    rollout_root = host_record.get("rollout_root")
    deps = build_deps(folder, plan, host, rollout_root=rollout_root)
    exists = (folder / JOURNAL).is_file()
    try:
        if exists:
            conductor = Conductor.attach(campaign_id, job_dir, home=home, **deps)
        else:
            conductor = Conductor.create(job_dir, home=home, **deps)
    except (ConductorError, CampaignJournalError, spec.CampaignError) as exc:
        return _fail(folder, getattr(exc, "code", "E_START"), str(exc)[:300])
    code = 0
    last_beat = 0.0
    steps = 0
    try:
        while not stop.is_set():
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_S:
                _heartbeat(folder, host, "running")
                last_beat = now
            try:
                _take_unblind(folder, conductor)
                outcome = conductor.advance()
            except Exception as exc:  # noqa: BLE001 - recorded, the campaign stays recoverable
                _fail(folder, type(exc).__name__, str(exc)[:300])
                code = 1
                break
            steps += 1
            if outcome.kind == "done":
                break
            if outcome.kind == "waiting" and stop.wait(poll_s):
                break
            if max_steps is not None and steps >= max_steps:
                break
    finally:
        with contextlib.suppress(Exception):
            conductor.close()
        with contextlib.suppress(OSError):
            _heartbeat(folder, host, "exited")
    return code


def _fail(folder, code: str, detail: str) -> int:
    with contextlib.suppress(OSError):
        A.ensure_ctl(folder)
        A.write_json(
            A.ctl_dir(folder) / ERROR_FILE,
            {"code": code, "detail": detail, "at_ms": time.time_ns() // 1_000_000},
        )
        _heartbeat(folder, "unknown", "exited")
    return 1


# --- starting a controller ------------------------------------------------------------------

_THREADS: dict = {}
_STOPS: dict = {}


def controller_argv(campaign_id: str, home=None) -> list:
    """The ``systemd-run`` command of a controller (no Restart: a controller
    that died is brought back with ``attach`` by a person)."""
    program = shutil.which("systemd-run")
    if program is None:
        raise launch.LaunchRefused("E_SYSTEMD", "systemd-run is not on PATH")
    pythonpath = os.pathsep.join(
        p for p in (str(launch.PROJECT), os.environ.get("PYTHONPATH", "")) if p
    )
    return [
        program,
        "--user",
        f"--unit={UNIT_PREFIX}{campaign_id}",
        "--collect",
        "-p",
        "KillMode=control-group",
        "-p",
        "TimeoutStopSec=120",
        f"--description=LEVI automatic evaluation campaign {campaign_id}",
        f"--setenv={launch.HOME_ENV}={launch.home(home)}",
        f"--setenv=PYTHONPATH={pythonpath}",
        sys.executable,
        "-m",
        "levi.automatic.campaign.controller",
        "--campaign-id",
        campaign_id,
    ]


def start_unit(argv: list) -> None:
    """Start the unit (tests replace this: no unit is ever started there)."""
    launch.start_systemd(argv)


def spawn(campaign_id: str, *, home=None, backend: str | None = None) -> None:
    """Start the controller of a campaign on ``backend``."""
    backend = backend or CONTROLLER_BACKEND
    if backend == "systemd":
        start_unit(controller_argv(campaign_id, home))
        return
    if backend == "foreground":
        serve(campaign_id, home=home)
        return
    if backend != "inprocess":
        raise launch.LaunchRefused("E_BACKEND", f"controller backend {backend}")
    stop = threading.Event()
    thread = threading.Thread(
        target=serve,
        args=(campaign_id,),
        kwargs={"home": home, "stop": stop, "poll_s": 0.02},
        name=f"campaign-{campaign_id}",
        daemon=True,
    )
    _STOPS[campaign_id] = stop
    _THREADS[campaign_id] = thread
    thread.start()


def stop_all(timeout: float = 10.0) -> None:
    """Stop and join every in-process controller (tests)."""
    for stop in list(_STOPS.values()):
        stop.set()
    for thread in list(_THREADS.values()):
        thread.join(timeout)
    _STOPS.clear()
    _THREADS.clear()


def robot_free(home, robot: str, campaign_id: str) -> bool:
    """No other living controller holds the robot (the kernel drops the
    lock of a dead one)."""
    try:
        RobotLock(home, robot, campaign_id).close()
    except ConductorError:
        return False
    return True


# --- main ------------------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m levi.automatic.campaign.controller",
        description="The controller of one automatic evaluation campaign (started "
        "by the API, not by hand). / 一个自动测评计划的控制器（由接口启动，不要手动运行）。",
    )
    parser.add_argument("--campaign-id", required=True)
    args = parser.parse_args(argv)
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    return serve(args.campaign_id, stop=stop)


if __name__ == "__main__":
    sys.exit(main())
