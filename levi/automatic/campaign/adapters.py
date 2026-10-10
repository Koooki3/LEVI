"""The world a campaign conductor touches (T-API-2, design X3 §1.6, §1.7).

``conductor.Conductor`` reaches the world through four interfaces. This
module holds the two sets of implementations the controller process uses
and the folder protocol that lets the HTTP side talk to the controller.

**Who writes what.** The campaign journal has one writer: the controller
(``controller.py``). The HTTP side only reads the journal and leaves
requests as small files under ``<campaign folder>/ctl/``:

- ``question.json``: the question the conductor waits on (id, one-time
  ``nonce``, kind, segment); written by the controller (``ask``);
- ``answers/<command id>.json``: a person's answer to it, written once by
  the page's request; the controller reads it, moves it to ``taken/``
  (``take``) and the conductor journals the command id;
- ``pause/<command id>.json``: a pause request (takes effect at the next
  segment boundary, ``conductor``);
- ``unblind/<command id>.json``: a look at the results before the end; the
  controller journals it as a ``unblind_peek`` note (it counts as a peek);
- ``segment-done/<run id>.json`` (guided) and ``serving.json`` (guided):
  what the person said about the legacy client's segment and about which
  policy is running; ``controller.json``: who the controller is.

**Policy hosts** (``PolicyHost``). ``DryRunPolicyHost`` records and starts
nothing. ``ManualPolicyHost`` is the guided campaign's: stopping and
starting a policy are the person's, so both only record the wish; the
policy counts as ready once the person confirmed the switch. Nothing here
starts a policy server, opens a socket or knows a port: the backend never
connects to a policy or a robot (design M11).

**Run launchers** (``RunLauncher``). ``DryRunLauncher`` starts a real dry
run (``launch.launch``, a fake robot) per segment and reads its journal.
``GuidedLauncher`` starts nothing: the person runs the legacy client's
command; it watches the rollout folders passively until the segment has
its trials or the person says it is done.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path

from levi.automatic import metrics
from levi.automatic import scene_assessment as sa
from levi.automatic.journal import Journal, fsync_dir, write_durable

from .. import launch
from . import guided as G
from . import ledger as L
from .conductor import (
    ChildStatus,
    Confirmation,
    ConfirmRequest,
    HostResult,
    LaunchResult,
    PauseRequest,
    Readiness,
)
from .conductor import _arm_codes as arm_codes_of

CTL = "ctl"
SUBFOLDERS = ("answers", "taken", "pause", "unblind", "segment-done")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MAX_JSON_BYTES = 256 * 1024
# How long a started child run may take to write its first journal line.
START_GRACE_S = 120.0
PRINCIPAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
HOSTS = ("dry_run", "guided")
MAX_TASK_FOLDERS = 64


# --- small file helpers ----------------------------------------------------------------


def ctl_dir(folder) -> Path:
    return Path(folder) / CTL


def ensure_ctl(folder) -> Path:
    """The ``ctl/`` folders of a campaign, 0700 (created when missing)."""
    base = ctl_dir(folder)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in SUBFOLDERS:
        (base / name).mkdir(mode=0o700, exist_ok=True)
    return base


def read_json(path, limit: int = MAX_JSON_BYTES):
    """The JSON value of a small regular file, or ``None`` (missing, a
    symbolic link, too large, not JSON)."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        data = os.read(descriptor, limit + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(data) > limit:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def write_json(path, value) -> None:
    """Replace ``path`` (the old or the new bytes survive a power cut)."""
    write_durable(
        Path(path), (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    )


def write_once(path, value) -> bool:
    """Create ``path`` with ``value``; ``False`` when it exists already
    (never replaced). The file appears whole or not at all."""
    path = Path(path)
    data = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        fsync_dir(path.parent)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _files(folder: Path) -> list:
    """The ``*.json`` files of a request folder, oldest first."""
    try:
        found = [
            p
            for p in folder.iterdir()
            if p.suffix == ".json" and not p.name.startswith(".")
        ]
    except OSError:
        return []
    keyed = []
    for path in found:
        with contextlib.suppress(OSError):
            keyed.append((path.stat().st_mtime_ns, path.name, path))
    return [p for _, _, p in sorted(keyed)]


def _retire(path: Path, taken: Path) -> None:
    with contextlib.suppress(OSError):
        os.replace(path, taken / path.name)


# --- the request files (HTTP side writes, the controller reads) -------------------------


def read_question(folder) -> dict | None:
    value = read_json(ctl_dir(folder) / "question.json")
    return value if isinstance(value, dict) else None


def command_seen(folder, command_id: str) -> bool:
    """A request with this command id was left, and maybe taken."""
    base = ctl_dir(folder)
    name = f"{command_id}.json"
    return any(
        (base / sub / name).exists() for sub in ("answers", "taken", "pause", "unblind")
    )


def write_answer(
    folder,
    *,
    question: Mapping,
    command_id: str,
    principal_id: str,
    decision: str,
    checks: Mapping | None = None,
) -> bool:
    """Leave a person's answer to ``question``; ``False`` when the command
    id was used already."""
    ensure_ctl(folder)
    if command_seen(folder, command_id):
        return False
    return write_once(
        ctl_dir(folder) / "answers" / f"{command_id}.json",
        {
            "request_id": question["request_id"],
            "nonce": question["nonce"],
            "command_id": command_id,
            "principal_id": principal_id,
            "decision": decision,
            "checks": dict(checks or {}),
            "at_ms": _now_ms(),
        },
    )


def answer_pending(folder, question: Mapping) -> bool:
    """A person's answer to ``question`` was left and not taken yet."""
    for path in _files(ctl_dir(folder) / "answers"):
        value = read_json(path)
        if isinstance(value, dict) and value.get("request_id") == question.get(
            "request_id"
        ):
            return True
    return False


def write_pause(folder, command_id: str, principal_id: str) -> bool:
    ensure_ctl(folder)
    if command_seen(folder, command_id):
        return False
    return write_once(
        ctl_dir(folder) / "pause" / f"{command_id}.json",
        {"command_id": command_id, "principal_id": principal_id, "at_ms": _now_ms()},
    )


def write_unblind(folder, command_id: str, principal_id: str) -> bool:
    ensure_ctl(folder)
    if command_seen(folder, command_id):
        return False
    return write_once(
        ctl_dir(folder) / "unblind" / f"{command_id}.json",
        {"command_id": command_id, "principal_id": principal_id, "at_ms": _now_ms()},
    )


def unblind_requests(folder) -> list:
    """The unblind requests not yet journaled by the controller."""
    out = []
    for path in _files(ctl_dir(folder) / "unblind"):
        value = read_json(path)
        if (
            isinstance(value, dict)
            and ID.fullmatch(str(value.get("command_id")))
            and PRINCIPAL.fullmatch(str(value.get("principal_id")))
        ):
            out.append(value)
    return out


def retire_unblind(folder, command_id: str) -> None:
    base = ctl_dir(folder)
    _retire(base / "unblind" / f"{command_id}.json", base / "taken")


def write_segment_done(folder, run_id: str, command_id: str, principal_id: str) -> bool:
    ensure_ctl(folder)
    return write_once(
        ctl_dir(folder) / "segment-done" / f"{run_id}.json",
        {"command_id": command_id, "principal_id": principal_id, "at_ms": _now_ms()},
    )


def confirm_serving(folder, arm: str, segment: int, principal_id: str) -> None:
    """The person said the policy of ``arm`` runs (guided campaigns)."""
    ensure_ctl(folder)
    write_json(
        ctl_dir(folder) / "serving.json",
        {
            "arm": arm,
            "segment": segment,
            "principal_id": principal_id,
            "at_ms": _now_ms(),
        },
    )


def read_serving(folder) -> dict | None:
    value = read_json(ctl_dir(folder) / "serving.json")
    return value if isinstance(value, dict) else None


# --- Confirmations -------------------------------------------------------------------------


class FileConfirmations:
    """``conductor.Confirmations`` over the ``ctl/`` folder (module text)."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.base = ensure_ctl(folder)

    def ask(self, request: ConfirmRequest) -> None:
        write_json(
            self.base / "question.json",
            {
                "request_id": request.request_id,
                "nonce": request.nonce,
                "kind": request.kind,
                "campaign_id": request.campaign_id,
                "segment": request.segment,
                "arm_code": request.arm_code,
                "slots": list(request.slots),
                "wait_reason": request.wait_reason,
                "checks": list(request.checks),
                "asked_at_ms": _now_ms(),
            },
        )

    def take(self, request: ConfirmRequest) -> Confirmation | None:
        taken = self.base / "taken"
        for path in _files(self.base / "answers"):
            value = read_json(path)
            _retire(path, taken)
            if not isinstance(value, dict):
                continue
            try:
                checks = value.get("checks")
                return Confirmation(
                    request_id=str(value["request_id"]),
                    nonce=str(value["nonce"]),
                    command_id=str(value["command_id"]),
                    principal_id=str(value["principal_id"]),
                    decision=str(value["decision"]),
                    checks=dict(checks) if isinstance(checks, dict) else {},
                )
            except (KeyError, TypeError, ValueError):
                continue
        return None

    def withdraw(self, request: ConfirmRequest) -> None:
        found = read_question(self.folder)
        if found is not None and found.get("request_id") != request.request_id:
            return
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.base / "question.json")

    def pause_requested(self) -> PauseRequest | None:
        taken = self.base / "taken"
        for path in _files(self.base / "pause"):
            value = read_json(path)
            _retire(path, taken)
            if (
                isinstance(value, dict)
                and ID.fullmatch(str(value.get("command_id")))
                and PRINCIPAL.fullmatch(str(value.get("principal_id")))
            ):
                return PauseRequest(value["command_id"], value["principal_id"])
        return None


# --- the plan, as the adapters need it ---------------------------------------------------


def arm_codes(plan: dict) -> dict:
    """The codes the operator sees (``X1``...), or the arm letters."""
    return arm_codes_of(plan["campaign"], plan["schedule"])


def groups_of(plan: dict) -> dict:
    """The recording group (rollout folder) of each arm."""
    out = {}
    for arm, found in plan["campaign"]["arms"].items():
        policy = found["policy_forward"]
        name = Path(str(policy["checkpoint_dir"]).rstrip("/")).name
        out[arm] = found.get("group") or name
    return out


def layout_of(plan: dict, run_ids: Mapping | None = None) -> L.CampaignLayout:
    """The campaign's segments for the trial ledger; ``run_ids``: segment ->
    run ids that ran it."""
    run_ids = run_ids or {}
    return L.CampaignLayout(
        plan["campaign_id"],
        tuple(
            L.SegmentPlan(
                segment=s["index"],
                arm=s["arm"],
                round=s["round"],
                cards=tuple(s["slots"]),
                run_ids=tuple(run_ids.get(s["index"], ())),
            )
            for s in plan["schedule"]["segments"]
        ),
        layout_source=plan["campaign"]["layouts"]["source"],
    )


def child_of(plan: dict, segment: int) -> dict:
    return next(c for c in plan["children"] if c["segment"] == segment)


def rollout_root_of(plan: dict, job_dir) -> str | None:
    """``recording.rollout_root`` the campaign's children write to."""
    child = plan["children"][0]
    try:
        document = sa.parse_document((Path(job_dir) / child["file"]).read_text())
    except (OSError, sa.ContractError):
        return None
    value = (document.get("recording") or {}).get("rollout_root")
    return value if isinstance(value, str) and value else None


# --- policy hosts -------------------------------------------------------------------------


class DryRunPolicyHost:
    """Records what the conductor asks and does nothing else."""

    def __init__(self, folder):
        self.base = ensure_ctl(folder)

    def _log(self, what: str, **fields) -> None:
        line = json.dumps({"at_ms": _now_ms(), "call": what, **fields}, sort_keys=True)
        with open(self.base / "host-calls.jsonl", "ab") as handle:
            handle.write(line.encode() + b"\n")

    def stop(self, arms: list) -> HostResult:
        self._log("stop", arms=[a.arm for a in arms])
        return HostResult("yes", "dry_run_stop")

    def start(self, arm) -> HostResult:
        self._log("start", arm=arm.arm)
        return HostResult("yes", "dry_run_start")

    def passive_ready(self, arm) -> Readiness:
        return Readiness("ready")


class ManualPolicyHost:
    """The guided campaign's host: a person switches the policy."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.base = ensure_ctl(folder)

    def stop(self, arms: list) -> HostResult:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.base / "serving.json")
        return HostResult("yes", "manual_stop")

    def start(self, arm) -> HostResult:
        write_json(
            self.base / "wanted.json",
            {"arm": arm.arm, "unit": arm.unit, "at_ms": _now_ms()},
        )
        return HostResult("yes", "manual_start")

    def passive_ready(self, arm) -> Readiness:
        serving = read_serving(self.folder)
        if serving is not None and serving.get("arm") == arm.arm:
            return Readiness("ready")
        return Readiness("not_ready", "the person has not confirmed the switch")


# --- reading a child AERI run ---------------------------------------------------------------


def aeri_status(run_id: str, *, grace_s: float = START_GRACE_S) -> ChildStatus:
    """A dry run as its own journal says (read only, no lock)."""
    try:
        entry = launch.read_record(launch.index_path(run_id))
        run_dir = Path(entry["run_dir"])
    except (OSError, ValueError, KeyError, TypeError, launch.LaunchRefused):
        return ChildStatus("absent")
    scan = Journal.read(run_dir)
    if scan.corrupt:
        return ChildStatus("fault_locked", detail="the run journal is corrupt")
    record = read_json(run_dir / launch.RUNNER_RECORD)
    if not isinstance(record, dict):
        record = entry.get("runner")
    alive = launch.runner_alive(record)
    if not scan.events:
        age_s = (time.time_ns() - int(entry.get("created_wall_ns") or 0)) / 1e9
        failed = isinstance(record, dict) and record.get("state") in (
            "failed_to_start",
            "refused",
        )
        if failed or (not alive and age_s > grace_s):
            return ChildStatus("crashed", detail="the runner never wrote a journal")
        return ChildStatus("running")
    events = list(scan.events)
    committed = [e for e in events if e.record == "committed"]
    faults = sum(1 for e in committed if e.to_state == "FAULT_LOCKED")
    try:
        unplanned = metrics.automation(events, entry.get("reset_mode"))["unplanned"]
    except Exception:  # noqa: BLE001 - a count for the stop rule, not a gate
        unplanned = 0
    state = scan.effective_state
    done = len(metrics.ended_forward(events))
    if state == "COMPLETED":
        try:
            facts, _ = L.read_aeri_run(run_dir)
            done = sum(f.status == "valid" for f in facts)
        except L.LedgerError:
            pass
        return ChildStatus("completed", done, faults, unplanned)
    if state == "FAULT_LOCKED":
        return ChildStatus("fault_locked", done, faults, unplanned)
    if not alive:
        return ChildStatus("crashed", done, faults, unplanned, "the runner is gone")
    if state == "WAIT_HUMAN":
        return ChildStatus("wait_human", done, faults, unplanned)
    return ChildStatus("running", done, faults, unplanned)


# The scripted scenes of every dry run of a campaign: the fake scene reads
# "ready", so a campaign on fakes does not stop to ask a person.
DRY_SCENES = ["ready"]


def dry_request(job_path, backend: str = "inprocess") -> launch.LaunchRequest:
    return launch.LaunchRequest(
        job_path=str(job_path),
        execution_mode=launch.DRY_RUN,
        overrides={"scenes": list(DRY_SCENES)},
        entry="cli",
        principal={"kind": "operator", "id": "campaign"},
        backend=backend,
    )


class DryRunPlanner:
    """Plans a child as the launch core does for a dry run, so the digest
    frozen in the campaign plan is the digest the launch checks."""

    def plan(self, job_path):
        found = launch.plan(dry_request(job_path))
        if found.plan_sha256 is None:
            detail = next((c["detail"] for c in found.checks if not c["ok"]), "")
            from .spec import CampaignError

            raise CampaignError("E_CAMPAIGN_JOB", f"{Path(job_path).name}: {detail}")
        return found.plan_sha256, found.plan


class DryRunLauncher:
    """Starts one real dry run (a fake robot) per segment."""

    def __init__(self, *, backend: str, deadline_s=None, wait_s=0.0):
        self.backend = backend
        self.deadline_s = deadline_s
        self.wait_s = wait_s

    def robot_busy(self):
        return None

    def launch(self, job_path, run_id, expected_plan_sha256) -> LaunchResult:
        request = dry_request(job_path, self.backend)
        try:
            found = launch.plan(request)
            if found.plan_sha256 != expected_plan_sha256:
                return LaunchResult("no", "plan_changed")
            launch.launch(
                request,
                plan_sha256=expected_plan_sha256,
                launch_token=found.launch_token,
                wait_s=self.wait_s,
                deadline_s=self.deadline_s,
            )
        except launch.LaunchRefused as exc:
            return LaunchResult("no", exc.code.lower()[:60])
        return LaunchResult("yes", "launched")

    def status(self, run_id: str) -> ChildStatus:
        return aeri_status(run_id)


# --- the guided campaign's run launcher -----------------------------------------------------


class GuidedLauncher:
    """Starts nothing: the person runs the legacy client's command. The
    segment is complete when the client wrote its trials (or the person
    said so)."""

    def __init__(self, folder, plan: dict, rollout_root: str):
        self.folder = Path(folder)
        self.plan = plan
        self.root = rollout_root
        self.base = ensure_ctl(folder)
        self.cards = G.CardConfirmations(self.folder / "cards.jsonl")

    def robot_busy(self):
        return None

    def _segment_of(self, run_id: str):
        return next(
            (c for c in self.plan["children"] if c["run_id"] == run_id),
            None,
        )

    def launch(self, job_path, run_id, expected_plan_sha256) -> LaunchResult:
        child = self._segment_of(run_id)
        if child is None:
            return LaunchResult("no", "unknown_run")
        write_once(
            self.base / f"started-{run_id}.json",
            {"run_id": run_id, "segment": child["segment"], "at_ms": _now_ms()},
        )
        return LaunchResult("yes", "guided")

    def status(self, run_id: str) -> ChildStatus:
        child = self._segment_of(run_id)
        if child is None or not (self.base / f"started-{run_id}.json").exists():
            return ChildStatus("absent")
        try:
            collected = collect(self.plan, self.folder, self.root, child["segment"])
        except G.GuidedError as exc:
            return ChildStatus("fault_locked", detail=str(exc)[:200])
        valid = collected["counts"].get(child["arm"], {}).get("valid", 0)
        said = (self.base / "segment-done" / f"{run_id}.json").exists()
        if said or valid >= child["trials"]:
            return ChildStatus("completed", valid)
        return ChildStatus("running", valid)


def records_of(plan: dict, root: str, arm: str) -> list:
    """Every rollout of an arm's group (all its task folders), read only."""
    group = groups_of(plan)[arm]
    try:
        tasks = sorted(p.name for p in (Path(root) / group).iterdir() if p.is_dir())
    except OSError:
        return []
    out = []
    for task in tasks[:MAX_TASK_FOLDERS]:
        out += G.scan_rollouts(root, group, task)
    return out


def collect(plan: dict, folder, root: str, segment: int) -> dict:
    """``guided.collect_segment`` of one segment with the person's card
    confirmations."""
    layout = layout_of(plan)
    arm = next(s.arm for s in layout.segments if s.segment == segment)
    cards = G.CardConfirmations(Path(folder) / "cards.jsonl")
    return G.collect_segment(
        records_of(plan, root, arm),
        layout,
        segment,
        confirmations=cards.latest(),
    )


# --- the ledger of a campaign, from either source --------------------------------------------


def build_ledger(plan: dict, host: str, folder, *, rollout_root: str | None = None):
    """``(ledger, layout, runs)``: the trial ledger derived from what the
    children wrote so far (read only). ``runs``: the AERI run records for
    the report's manifest. A child that cannot be read yet contributes
    nothing."""
    facts: dict = {}
    run_ids: dict = {}
    runs: list = []
    if host == "dry_run":
        for child in plan["children"]:
            try:
                entry = launch.read_record(launch.index_path(child["run_id"]))
                found, record = L.read_aeri_run(entry["run_dir"])
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                launch.LaunchRefused,
                L.LedgerError,
            ):
                continue
            facts[child["run_id"]] = found
            run_ids[child["segment"]] = (child["run_id"],)
            runs.append(record)
        layout = layout_of(plan, run_ids)
        return L.derive(layout, facts), layout, runs
    layout = layout_of(plan)
    collected = []
    if rollout_root:
        for seg in layout.segments:
            try:
                collected.append(collect(plan, folder, rollout_root, seg.segment))
            except G.GuidedError:
                continue
    bound = G.bind_runs(layout, collected)
    for item in collected:
        facts.update(item["facts"])
    return L.derive(bound, facts), bound, runs


__all__ = [
    "HOSTS",
    "DryRunLauncher",
    "DryRunPlanner",
    "DryRunPolicyHost",
    "FileConfirmations",
    "GuidedLauncher",
    "ManualPolicyHost",
    "aeri_status",
    "arm_codes",
    "build_ledger",
    "collect",
    "confirm_serving",
    "ctl_dir",
    "ensure_ctl",
    "groups_of",
    "layout_of",
    "read_json",
    "read_question",
    "write_answer",
    "write_json",
    "write_once",
    "write_pause",
    "write_unblind",
]
