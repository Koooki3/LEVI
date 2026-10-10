"""HTTP interface of multi-arm evaluation campaigns (T-API-2).

Routes under ``/api/levi/automatic/campaigns`` as the interface contract of
LEVI 2.0 lists them (``levi2/design/aeri-ui-api-contract.md`` §4); the
product's front end proxies them with the person's token. Same rules as the
run routes (``levi/automatic/api.py``, whose helpers this module reuses):

- ids are matched against strict patterns before they touch a path; report
  files are opened component by component with ``O_NOFOLLOW`` below
  ``report/<basis>/`` only;
- a write needs the person (``_person``: an agent's Bearer gets 403), a
  ``command_id`` or ``request_id`` (the same request again returns the
  first answer) and, for actions that change a campaign, a typed
  ``confirm``; one operation at a time per campaign (423 otherwise);
- a GET writes nothing (the one-time ``challenge`` of a to-do is memory
  only).

**The journal has one writer: the controller** (``controller.py``). The
routes read the journal and leave requests as files under ``ctl/``
(``adapters.py``); the controller takes them. A route that needs the
controller answers 409 ``controller_down`` when it is not alive (``attach``
brings it back).

**Blinding.** While ``blinded`` is true the snapshot, the list and the
event counts hold no success rate and no count from which one could be
derived: only counts of episodes done, left, deviated and discarded. A
campaign stops being blind when it reaches ``ANALYZING`` or when a person
unblinds it (a peek, journaled; it makes the conclusion exploratory). The
report is served only when not blind.

**To-do.** The snapshot says what the campaign waits for: ``switch_policy``
(guided: start the arm's policy yourself, then confirm), ``place_cards``
(set the scene; guided campaigns also show the legacy client's command),
``segment_done`` (guided: run the command; say when the segment is done),
``recover_run`` (the campaign waits for a person after a fault).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from levi.automatic import api as base
from levi.automatic import launch

from . import adapters as A
from . import controller as C
from . import guided as G
from . import ledger as L
from . import report as R
from . import schedule as sched
from . import spec
from .conductor import aeri_home, campaign_dir
from .journal import FAULT_REASONS, JOURNAL, CampaignJournal
from .journal import read_plan as read_kept_plan

router = APIRouter(
    prefix="/api/levi/automatic/campaigns", tags=["Automatic evaluation campaigns"]
)

_Strict = base._Strict
_fail = base._fail
_person = base._person
ID = base.ID
CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_LISTED = 50
MAX_FILE_BYTES = 32 << 20
MAX_ANALYSIS_BYTES = 16 << 20
MAX_REPORT_FILES = 500
COUNTS_CACHE_S = 2.0
APPLY_WAIT_S = 3.0
START_WAIT_S = 3.0
FINISHED = ("ANALYZING", "REPORTED")
ENDED = ("REPORTED", "ABORTED")
STOP_RULES = ("stop_rule_faults", "stop_rule_interventions")
OPTION_KEYS = ("override_stop_rule", "accept_short_segment", "relaunch")
CONTENT_TYPES = {
    ".csv": "text/csv; charset=utf-8",
    ".tex": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".parquet": "application/octet-stream",
}

_MEMO = base._Memo()
_CHALLENGES = base._Challenges()
_COUNTS: dict = {}
_COUNTS_LOCK = threading.Lock()


# --- request bodies --------------------------------------------------------------------------


class ArmBody(_Strict):
    id: str = Field(pattern=r"^[A-H]$")
    checkpoint_id: str = Field(pattern=base.ID.pattern)
    role: Literal["candidate", "reference"]


class ScheduleBody(_Strict):
    kind: Literal[sched.KINDS]
    segment_trials: int | None = Field(None, ge=1, le=1000)
    seed: int = Field(0, ge=-(2**62), le=2**62)


class PrimaryBody(_Strict):
    metric: Literal["success"] = "success"
    label_basis: Literal[L.LABEL_BASES] = "operator_label"
    alpha: float = Field(0.05, gt=0.0, le=0.5)


class PlanBody(_Strict):
    job_id: str = Field(min_length=1, max_length=128)
    arms: list[ArmBody] = Field(min_length=2, max_length=spec.MAX_ARMS)
    trials_per_arm: int = Field(ge=1, le=100_000)
    schedule: ScheduleBody
    primary: PrimaryBody = Field(default_factory=PrimaryBody)
    preregistered: bool = False
    # Not in the contract's table: how the campaign is carried out. The page
    # does not send it; the default is the user's own legacy client.
    execution_mode: Literal["guided", "dry_run"] = "guided"


class StartBody(PlanBody):
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm: Literal["start-campaign"]
    request_id: str = Field(pattern=base.ID.pattern)


class ConfirmBody(_Strict):
    command_id: str = Field(pattern=base.ID.pattern)
    kind: Literal["switch_policy", "env", "segment_done"]
    challenge: str = Field(min_length=1, max_length=200)
    options: dict[str, bool] | None = None


class CommandBody(_Strict):
    command_id: str = Field(pattern=base.ID.pattern)
    confirm: str = Field(max_length=32)


class AttachBody(_Strict):
    request_id: str = Field(pattern=base.ID.pattern)


class CardBody(_Strict):
    command_id: str = Field(pattern=base.ID.pattern)
    episode_key: str = Field(min_length=1, max_length=300)
    card: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ReportBody(_Strict):
    command_id: str = Field(pattern=base.ID.pattern)
    basis: Literal[L.LABEL_BASES] | None = None


def _request_of(body: PlanBody) -> dict:
    """What names the campaign: the plan request without the start-only
    fields (the same request is the same campaign)."""
    value = body.model_dump(mode="json", include=set(PlanBody.model_fields))
    value["arms"] = sorted(value["arms"], key=lambda a: a["id"])
    return value


def _prepare(body: PlanBody, *, final: bool) -> C.Prepared:
    path = base._job_path(body.job_id)
    request = _request_of(body)
    try:
        return C.prepare(
            request,
            job_path=path,
            forward=base.checkpoint_ids("forward"),
            final=final,
        )
    except C.PlanRefused as exc:
        extra = {"errors": exc.errors} if exc.errors else {}
        raise _fail(
            exc.status, exc.code, base.scrub(exc.message)[:300], **extra
        ) from None


# --- plan ---------------------------------------------------------------------------------------


@router.post("/plan")
def plan_campaign(body: PlanBody):
    """The campaign plan of a request: segments, policy starts, the design's
    detectable difference (never a post-hoc power), refusals and checks.
    Writes nothing that stays (the job files are planned in a scratch
    folder), except the core key on first use."""
    prepared = _prepare(body, final=False)
    return C.plan_view(prepared, _request_of(body))


@router.post("", status_code=202)
def start_campaign(body: StartBody, request: Request):
    _person(request)
    payload = body.model_dump(mode="json")
    again = _MEMO.get("start", body.request_id, payload)
    if again is not None:
        return again
    with base._operation("campaign-start"):
        preview = _prepare(body, final=False)
        if preview.plan.campaign_sha256 != body.plan_sha256:
            raise _fail(
                412,
                "plan_changed",
                "The plan is now another one: read the new plan first",
            )
        if preview.refusals:
            raise _fail(
                422,
                "plan_refused",
                "The plan cannot start",
                errors=[{"field": None, "message": code} for code in preview.refusals],
            )
        home = launch.ensure_home()
        folder = campaign_dir(aeri_home(), preview.campaign_id)
        known = C.read_host(folder)
        if known is not None or (folder / JOURNAL).exists():
            if known is not None and known.get("request_id") == body.request_id:
                if not (folder / JOURNAL).exists() and not C.controller_alive(folder):
                    # The first start never got its controller going.
                    _spawn(preview.campaign_id)
                answer = {"campaign_id": preview.campaign_id}
                _MEMO.put("start", body.request_id, payload, answer)
                return answer
            raise _fail(
                409,
                "campaign_exists",
                "This campaign was started before; change the seed to run it again",
            )
        robot = preview.plan.spec.block.robot
        if not C.robot_free(aeri_home(), robot, preview.campaign_id):
            raise _fail(
                409, "robot_busy", "Another campaign holds this robot; wait for it"
            )
        final = _prepare(body, final=True)
        if final.plan.campaign_sha256 != body.plan_sha256:
            raise _fail(412, "plan_changed", "The plan changed while it was written")
        (home / "campaigns").mkdir(mode=0o700, exist_ok=True)
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        A.ensure_ctl(folder)
        C.write_host(
            folder,
            host=final.host,
            request_id=body.request_id,
            rollout_root=final.rollout_root,
            created_ms=base._now_ms(),
        )
        if final.base_command is not None:
            A.write_json(A.ctl_dir(folder) / C.GUIDED_FILE, final.base_command)
        try:
            _spawn(final.campaign_id)
        except HTTPException:
            # Nothing is running for it: the campaign never started.
            shutil.rmtree(folder, ignore_errors=True)
            raise
        _wait_for(final.campaign_id, lambda c: bool(c.events), START_WAIT_S)
    answer = {"campaign_id": final.campaign_id}
    _MEMO.put("start", body.request_id, payload, answer)
    base._audit("api_campaign_started", campaign_id=final.campaign_id)
    return answer


def _spawn(campaign_id: str) -> None:
    try:
        C.spawn(campaign_id, home=aeri_home())
    except launch.LaunchRefused as exc:
        raise base._refused(exc) from None


# --- one campaign, as the routes see it ----------------------------------------------------------


class _Campaign:
    def __init__(self, campaign_id: str, folder: Path):
        self.id = campaign_id
        self.folder = folder
        self.host_record = C.read_host(folder) or {}
        self.scan = CampaignJournal.read(folder)
        self.events = self.scan.events
        self.replay = self.scan.replay
        self.state = self.scan.effective_state if self.events else "DRAFT"
        self.plan = self._plan()

    def _plan(self) -> dict | None:
        try:
            found = read_kept_plan(self.folder)
        except Exception:  # noqa: BLE001 - a broken copy: try the job folder
            found = None
        if found is not None:
            return found
        try:
            return spec.read_plan(C.job_folder(self.id, aeri_home()) / spec.PLAN_FILE)
        except spec.CampaignError:
            return None

    @property
    def host(self) -> str:
        return self.host_record.get("host") or "guided"

    @property
    def alive(self) -> bool:
        return C.controller_alive(self.folder)

    @property
    def committed(self) -> int:
        """The sequence number of the last commit (-1: none yet)."""
        return max(
            (e.sequence_no for e in self.events if e.record == "committed"), default=-1
        )

    @property
    def seq(self) -> int:
        """The step the campaign is at, as a number a challenge is bound
        to: it moves when the campaign changes state (a commit) or asks
        another question, not when a note (a pause request, a peek) is
        journaled."""
        committed = self.committed
        question = A.read_question(self.folder) or {}
        step = f"{self.state}:{self.replay.segment}:{committed}:{question.get('request_id')}"
        return int(hashlib.sha256(step.encode()).hexdigest()[:12], 16)

    def arm_of(self, segment) -> str | None:
        if not self.plan or not segment:
            return None
        found = [c for c in self.plan["children"] if c["segment"] == segment]
        return found[0]["arm"] if found else None


def _locate(campaign_id: str) -> _Campaign:
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID.fullmatch(campaign_id):
        raise _fail(404, "not_found", "No such campaign")
    folder = campaign_dir(aeri_home(), campaign_id)
    try:
        info = folder.lstat()
    except OSError:
        raise _fail(404, "not_found", "No such campaign") from None
    if not stat.S_ISDIR(info.st_mode):
        raise _fail(404, "not_found", "No such campaign")
    found = _Campaign(campaign_id, folder)
    if not found.events and not found.host_record:
        raise _fail(404, "not_found", "No such campaign")
    return found


def _wait_for(campaign_id: str, predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        with contextlib.suppress(Exception):
            if predicate(
                _Campaign(campaign_id, campaign_dir(aeri_home(), campaign_id))
            ):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _peeked(c: _Campaign) -> bool:
    return C.peeks_of(c.events) > 0 or bool(A.unblind_requests(c.folder))


def _blinded(c: _Campaign) -> bool:
    return c.state not in FINISHED and not _peeked(c)


# --- the snapshot ---------------------------------------------------------------------------------


def _counts(c: _Campaign) -> dict:
    """Per arm counts (no outcome) of what the children wrote so far; read
    only and cached for a moment."""
    if not c.plan:
        return {}
    key = (c.id, c.committed)
    now = time.monotonic()
    with _COUNTS_LOCK:
        found = _COUNTS.get(key)
        if found and now - found[0] < COUNTS_CACHE_S:
            return found[1]
    try:
        ledger, layout, _ = A.build_ledger(
            c.plan,
            c.host,
            c.folder,
            rollout_root=c.host_record.get("rollout_root"),
        )
        value = L.counts(ledger, layout)
    except (L.LedgerError, G.GuidedError, OSError, ValueError):
        value = {}
    with _COUNTS_LOCK:
        _COUNTS[key] = (now, value)
        if len(_COUNTS) > 256:
            _COUNTS.pop(next(iter(_COUNTS)))
    return value


def _arm_rows(c: _Campaign, blinded: bool) -> list:
    if not c.plan:
        return []
    codes = A.arm_codes(c.plan)
    counts = _counts(c)
    rows = []
    for arm in sorted(c.plan["campaign"]["arms"]):
        entry = counts.get(arm, {})
        planned = entry.get("planned", c.plan["campaign"]["trials_per_arm"])
        done = entry.get("valid", 0)
        discarded = entry.get("discarded", 0)
        shown = codes[arm] if blinded and codes[arm] != arm else arm
        rows.append(
            {
                "id": shown,
                "code": codes[arm],
                "done": done,
                "remaining": max(0, planned - done - discarded),
                "deviated": entry.get("deviated", 0),
                "discarded": discarded,
                "unconfirmed": entry.get("unconfirmed", 0),
            }
        )
    return rows


def _command_of(c: _Campaign, segment: int) -> dict | None:
    """The legacy client's command of a guided segment (rendered from the
    command the campaign froze)."""
    saved = A.read_json(A.ctl_dir(c.folder) / C.GUIDED_FILE)
    if c.host != "guided" or not saved or not c.plan:
        return None
    try:
        base_command = G.BaseCommand.from_dict(saved)
        layout = A.layout_of(c.plan)
        seg = next(s for s in layout.segments if s.segment == segment)
        code = A.arm_codes(c.plan)[seg.arm]
        note = G.eval_note(c.plan["campaign_id"], segment, code)
        text = G.render(
            base_command,
            eval_num=len(seg.cards),
            rollout_group=A.groups_of(c.plan)[seg.arm],
            eval_note=note,
        )
    except (G.GuidedError, StopIteration, KeyError, ValueError):
        return None
    return {"command": text, "eval_note": note}


def _question(c: _Campaign, kind: str) -> dict | None:
    found = A.read_question(c.folder)
    if (
        found
        and found.get("kind") == kind
        and found.get("segment") == c.replay.segment
        and found.get("request_id")
        and found.get("nonce")
        and not A.answer_pending(c.folder, found)
    ):
        return found
    return None


def _todo(c: _Campaign) -> dict | None:
    state, segment = c.state, c.replay.segment
    if not c.plan or state in ENDED or state in FINISHED:
        return None
    arm = c.arm_of(segment)
    code = A.arm_codes(c.plan).get(arm) if arm else None
    slots = []
    if segment:
        slots = list(c.plan["schedule"]["segments"][segment - 1]["slots"])
    todo: dict[str, Any] | None = None
    serving = A.read_serving(c.folder) or {}
    if (
        state == "POLICY_READY"
        and c.host == "guided"
        and arm
        and serving.get("arm") != arm
    ):
        policy = c.plan["campaign"]["arms"][arm]["policy_forward"]
        name = Path(str(policy["checkpoint_dir"]).rstrip("/")).name
        todo = {
            "kind": "switch_policy",
            "segment": segment,
            "arm_code": code,
            "checkpoint": name,
            "config": policy["config"],
            "detail": f"Start the policy server of {name} (config {policy['config']}), "
            "then confirm. LEVI never starts or connects to it.",
        }
    elif state == "ENV_CONFIRM" and _question(c, "env_confirm"):
        todo = {
            "kind": "place_cards",
            "segment": segment,
            "arm_code": code,
            "cards": slots,
            "detail": "Hold the arm still and lay out the cards in this order.",
        }
        todo.update(_command_of(c, segment) or {})
    elif state == "ARM_RUNNING" and c.host == "guided":
        todo = {
            "kind": "segment_done",
            "segment": segment,
            "arm_code": code,
            "cards": slots,
            "detail": "Run the command on your robot; say when the segment is done.",
        }
        todo.update(_command_of(c, segment) or {})
        todo.update(_segment_progress(c, segment, arm))
    elif state in ("WAIT_HUMAN", "FAULT_LOCKED") and _question(c, "resume"):
        todo = {
            "kind": "recover_run",
            "segment": segment,
            "arm_code": code,
            "reason": c.replay.wait_reason,
            "detail": base.scrub(
                f"The campaign waits for you ({c.replay.wait_reason})."
            )[:300],
        }
    if todo is None:
        return None
    todo["challenge"] = _CHALLENGES.issue(f"campaign:{c.id}", c.seq)
    return todo


def _segment_progress(c: _Campaign, segment: int, arm: str | None) -> dict:
    root = c.host_record.get("rollout_root")
    if not root or not c.plan or not arm:
        return {}
    try:
        collected = A.collect(c.plan, c.folder, root, segment)
    except (G.GuidedError, OSError, ValueError, KeyError):
        return {}
    child = A.child_of(c.plan, segment)
    valid = collected["counts"].get(arm, {}).get("valid", 0)
    return {
        "progress": {"done": valid, "planned": child["trials"]},
        "pending_cards": [
            {"key": p["key"], "candidate_card": p["candidate_card"]}
            for p in collected["pending"][:200]
        ],
    }


def _safety(c: _Campaign) -> dict:
    faults = sum(
        1 for e in c.events if e.record == "committed" and e.reason in FAULT_REASONS
    )
    fused = (
        c.state == "FAULT_LOCKED"
        or bool(c.scan.corrupt)
        or (c.state == "WAIT_HUMAN" and c.replay.wait_reason in STOP_RULES)
    )
    return {"faults": faults, "fused": fused}


def snapshot_of(c: _Campaign, *, light: bool = False) -> dict:
    blinded = _blinded(c)
    segment = c.replay.segment or 0
    arm = c.arm_of(segment)
    codes = A.arm_codes(c.plan) if c.plan else {}
    value = {
        "id": c.id,
        "state": c.state,
        "execution_mode": c.host,
        "arms": _arm_rows(c, blinded),
        "segment": {
            "no": segment,
            "total": c.replay.segments or (len(c.plan["children"]) if c.plan else 0),
            "arm_code": codes.get(arm, "") if arm else "",
        },
        "todo": None if light else _todo(c),
        "safety": _safety(c),
        "blinded": blinded,
        "controller": {"alive": c.alive},
        "wait_reason": c.replay.wait_reason
        if c.state in ("WAIT_HUMAN", "FAULT_LOCKED", "PAUSED")
        else None,
        "peeks": C.peeks_of(c.events),
        "updated_at": base._ms(c.events[-1].emitted_wall_ns) if c.events else None,
    }
    launched = c.replay.launched(segment) if segment else None
    if launched is not None and c.host == "dry_run":
        value["child_run_id"] = launched.run_id
    return value


@router.get("")
def list_campaigns():
    root = aeri_home() / "campaigns"
    rows = []
    try:
        names = sorted(n for n in os.listdir(root) if CAMPAIGN_ID.fullmatch(n))
    except OSError:
        names = []
    for name in names[-200:]:
        with contextlib.suppress(Exception):
            c = _locate(name)
            rows.append((c.events[0].emitted_wall_ns if c.events else 0, c))
    rows.sort(key=lambda r: r[0], reverse=True)
    return {"campaigns": [snapshot_of(c, light=True) for _, c in rows[:MAX_LISTED]]}


@router.get("/{campaign_id}")
def get_campaign(campaign_id: str):
    return snapshot_of(_locate(campaign_id))


# --- commands ---------------------------------------------------------------------------------------


def _applied(c: _Campaign, command_id: str, queued: bool = False) -> dict:
    return {"result": "applied", "queued": queued, "command_id": command_id}


def _require_controller(c: _Campaign) -> None:
    if not c.alive:
        raise _fail(
            409,
            "controller_down",
            "The campaign's controller is not running; attach it first",
        )


def _repeated(c: _Campaign, command_id: str) -> bool:
    return A.command_seen(c.folder, command_id) or bool(
        [e for e in c.events if e.authority.command_id == command_id]
    )


def _decide(c: _Campaign, body: ConfirmBody) -> tuple[str, dict]:
    """``(decision, checks)`` the confirm means in the campaign's state, or
    a 409."""
    options = {k: v for k, v in (body.options or {}).items() if k in OPTION_KEYS}
    if body.kind == "env" and c.state == "ENV_CONFIRM" and _question(c, "env_confirm"):
        return "confirm", {check: True for check in ("arm_still", "layout_ready")}
    if (
        body.kind == "env"
        and c.state in ("WAIT_HUMAN", "FAULT_LOCKED")
        and _question(c, "resume")
    ):
        decision = "relaunch" if options.pop("relaunch", False) else "resume"
        return decision, {k: v for k, v in options.items() if v}
    raise _fail(409, "not_waiting", "The campaign does not wait for this")


@router.post("/{campaign_id}/confirm")
def confirm(campaign_id: str, body: ConfirmBody, request: Request):
    _person(request)
    with base._operation(f"campaign:{campaign_id}"):
        c = _locate(campaign_id)
        if _repeated(c, body.command_id):
            return {"result": "repeated", "command_id": body.command_id}
        if c.state not in ENDED and c.state != "DRAFT":
            _require_controller(c)
        if body.kind == "switch_policy":
            if c.host != "guided" or c.state != "POLICY_READY":
                raise _fail(409, "not_waiting", "No policy switch is waited for")
        elif body.kind == "segment_done":
            if c.host != "guided" or c.state != "ARM_RUNNING":
                raise _fail(409, "not_waiting", "No segment is running")
        else:
            decision, checks = _decide(c, body)
        if not _CHALLENGES.take(f"campaign:{c.id}", body.challenge, c.seq):
            return {"result": "refused", "code": "stale_sequence"}
        principal = base.PRINCIPAL
        if body.kind == "switch_policy":
            arm = c.arm_of(c.replay.segment)
            A.confirm_serving(c.folder, arm, c.replay.segment, principal)
            A.write_once(
                A.ctl_dir(c.folder) / "taken" / f"{body.command_id}.json",
                {"command_id": body.command_id, "kind": "switch_policy"},
            )
        elif body.kind == "segment_done":
            launched = c.replay.launched(c.replay.segment)
            if launched is None:
                raise _fail(409, "not_waiting", "No segment is running")
            A.write_segment_done(c.folder, launched.run_id, body.command_id, principal)
            A.write_once(
                A.ctl_dir(c.folder) / "taken" / f"{body.command_id}.json",
                {"command_id": body.command_id, "kind": "segment_done"},
            )
        else:
            question = _question(
                c, "env_confirm" if decision == "confirm" else "resume"
            )
            if not A.write_answer(
                c.folder,
                question=question,
                command_id=body.command_id,
                principal_id=principal,
                decision=decision,
                checks=checks,
            ):
                return {"result": "repeated", "command_id": body.command_id}
        base._audit(
            "api_campaign_confirm", campaign_id=c.id, command_id=body.command_id
        )
        return _applied(c, body.command_id)


@router.post("/{campaign_id}/pause")
def pause_campaign(campaign_id: str, body: CommandBody, request: Request):
    _person(request)
    if body.confirm != "pause":
        raise _fail(422, "confirm_required", 'Type the confirmation "pause"')
    with base._operation(f"campaign:{campaign_id}"):
        c = _locate(campaign_id)
        if _repeated(c, body.command_id):
            return {"result": "repeated", "command_id": body.command_id}
        if c.state in ENDED or c.state in FINISHED or c.state in ("PAUSED", "DRAFT"):
            raise _fail(409, "not_pausable", "The campaign cannot be paused now")
        _require_controller(c)
        A.write_pause(c.folder, body.command_id, base.PRINCIPAL)
        base._audit("api_campaign_pause", campaign_id=c.id, command_id=body.command_id)
        return _applied(c, body.command_id, queued=True)


@router.post("/{campaign_id}/resume")
def resume_campaign(campaign_id: str, body: CommandBody, request: Request):
    _person(request)
    if body.confirm != "resume":
        raise _fail(422, "confirm_required", 'Type the confirmation "resume"')
    with base._operation(f"campaign:{campaign_id}"):
        c = _locate(campaign_id)
        if _repeated(c, body.command_id):
            return {"result": "repeated", "command_id": body.command_id}
        if c.state != "PAUSED":
            raise _fail(409, "not_paused", "The campaign is not paused")
        _require_controller(c)
        # The controller asks its question a moment after it paused.
        _wait_for(c.id, lambda cur: _question(cur, "resume") is not None, APPLY_WAIT_S)
        c = _locate(campaign_id)
        question = _question(c, "resume")
        if c.state != "PAUSED" or question is None:
            raise _fail(409, "not_paused", "The campaign is not paused")
        if not A.write_answer(
            c.folder,
            question=question,
            command_id=body.command_id,
            principal_id=base.PRINCIPAL,
            decision="resume",
        ):
            return {"result": "repeated", "command_id": body.command_id}
        base._audit("api_campaign_resume", campaign_id=c.id, command_id=body.command_id)
        return _applied(c, body.command_id, queued=True)


@router.post("/{campaign_id}/unblind")
def unblind_campaign(campaign_id: str, body: CommandBody, request: Request):
    _person(request)
    if body.confirm != "unblind":
        raise _fail(422, "confirm_required", 'Type the confirmation "unblind"')
    with base._operation(f"campaign:{campaign_id}"):
        c = _locate(campaign_id)
        if _repeated(c, body.command_id):
            return {"result": "repeated", "command_id": body.command_id}
        if not _blinded(c):
            return {"result": "repeated", "command_id": body.command_id}
        if c.state == "DRAFT":
            raise _fail(409, "not_started", "The campaign has not started")
        A.write_unblind(c.folder, body.command_id, base.PRINCIPAL)
        base._audit(
            "api_campaign_unblind", campaign_id=c.id, command_id=body.command_id
        )
    seen = _wait_for(
        campaign_id,
        lambda cur: any(e.authority.command_id == body.command_id for e in cur.events),
        APPLY_WAIT_S,
    )
    return {"result": "applied", "queued": not seen, "command_id": body.command_id}


@router.post("/{campaign_id}/attach", status_code=202)
def attach_campaign(campaign_id: str, body: AttachBody, request: Request):
    """Start a controller for a campaign whose controller is gone: the
    conductor recovers to a place where a person confirms."""
    _person(request)
    with base._operation(f"campaign:{campaign_id}"):
        c = _locate(campaign_id)
        if not c.events:
            raise _fail(409, "not_started", "The campaign has no journal to attach to")
        if c.state in ENDED:
            raise _fail(409, "campaign_ended", "The campaign has ended")
        if c.alive:
            raise _fail(409, "controller_alive", "A controller of this campaign runs")
        robot = c.plan["robot"] if c.plan else "main"
        if not C.robot_free(aeri_home(), robot, c.id):
            raise _fail(409, "robot_busy", "Another campaign holds this robot")
        _spawn(c.id)
        base._audit("api_campaign_attach", campaign_id=c.id)
    return {"campaign_id": campaign_id}


def _pending_cards(c: _Campaign) -> list:
    """Valid legacy-client episodes whose card nobody confirmed yet, with
    the card the schedule expects next (a suggestion, never taken as
    given)."""
    root = c.host_record.get("rollout_root")
    out = []
    if c.host != "guided" or not c.plan or not root:
        return out
    for seg in c.plan["schedule"]["segments"]:
        try:
            found = A.collect(c.plan, c.folder, root, seg["index"])
        except (G.GuidedError, OSError, ValueError):
            continue
        out += [
            {
                "key": p["key"],
                "segment": seg["index"],
                "run_id": p["run_id"],
                "number": p["number"],
                "candidate_card": p["candidate_card"],
            }
            for p in found["pending"]
        ]
    return out[:500]


@router.get("/{campaign_id}/cards")
def pending_cards(campaign_id: str):
    """Guided campaigns: the episodes that wait for a person to say which
    layout card they had (``POST .../cards``). An episode without a
    confirmed card never enters the paired analysis."""
    c = _locate(campaign_id)
    if c.host != "guided":
        raise _fail(409, "not_guided", "Only a guided campaign has card answers")
    return {"pending": _pending_cards(c)}


@router.post("/{campaign_id}/cards")
def confirm_card(campaign_id: str, body: CardBody, request: Request):
    """Guided campaigns: say which layout card a legacy-client episode had
    (``card`` null withdraws it). Only a confirmed card puts the episode
    into the paired analysis."""
    _person(request)
    with base._operation(f"campaign:{campaign_id}"):
        c = _locate(campaign_id)
        root = c.host_record.get("rollout_root")
        if c.host != "guided" or not c.plan or not root:
            raise _fail(409, "not_guided", "Only a guided campaign takes card answers")
        if _repeated(c, body.command_id):
            return {"result": "repeated", "command_id": body.command_id}
        known = set()
        for seg in c.plan["schedule"]["segments"]:
            try:
                collected = A.collect(c.plan, c.folder, root, seg["index"])
            except (G.GuidedError, OSError, ValueError):
                continue
            known.update(row.episode_id for row in collected["rows"])
        if body.episode_key not in known:
            raise _fail(422, "episode_unknown", "No such episode in this campaign")
        cards = {
            card for seg in c.plan["schedule"]["segments"] for card in seg["slots"]
        }
        if body.card is not None and body.card not in cards:
            raise _fail(422, "card_unknown", "No such layout card in this campaign")
        try:
            G.CardConfirmations(c.folder / "cards.jsonl").add(
                body.episode_key, body.card, by=base.PRINCIPAL
            )
        except G.GuidedError as exc:
            raise _fail(422, "card_invalid", str(exc)[:200]) from None
        A.ensure_ctl(c.folder)
        A.write_once(
            A.ctl_dir(c.folder) / "taken" / f"{body.command_id}.json",
            {"command_id": body.command_id, "kind": "card"},
        )
        with _COUNTS_LOCK:
            _COUNTS.clear()
        return _applied(c, body.command_id)


# --- the report ----------------------------------------------------------------------------------------


def _basis_of(c: _Campaign, basis: str | None) -> str:
    chosen = basis or ((c.plan or {}).get("campaign", {}).get("primary") or {}).get(
        "label_basis"
    )
    chosen = chosen or "operator_label"
    if chosen not in L.LABEL_BASES:
        raise _fail(422, "basis_invalid", f"basis is one of {', '.join(L.LABEL_BASES)}")
    return chosen


def _require_reportable(c: _Campaign) -> None:
    if _blinded(c):
        raise _fail(
            409,
            "blinded",
            "The results are blind until the campaign ends or is unblinded",
        )
    if c.state not in FINISHED:
        raise _fail(409, "not_ready", "The report is made when the campaign ends")


def _kind_of(name: str) -> str:
    parts = name.split("/")
    suffix = Path(name).suffix
    if name == "manifest.json":
        return "manifest"
    if parts[0] == "figures":
        return "figure_spec" if suffix == ".json" else "figure"
    if parts[0] == "tables":
        return "table"
    if parts[0] == "data":
        return "data"
    if suffix == ".md":
        return "summary"
    return "file"


def _report_files(basis_dir: Path) -> list:
    out = []
    stack = [("", basis_dir)]
    while stack and len(out) < MAX_REPORT_FILES:
        prefix, folder = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            name = f"{prefix}{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                if prefix.count("/") < 3:
                    stack.append((f"{name}/", Path(entry.path)))
            elif entry.is_file(follow_symlinks=False):
                out.append({"name": name, "kind": _kind_of(name)})
    return sorted(out, key=lambda f: f["name"])


@router.get("/{campaign_id}/report")
def get_report(campaign_id: str, basis: str | None = None):
    c = _locate(campaign_id)
    _require_reportable(c)
    chosen = _basis_of(c, basis)
    root = C.report_root(c.folder)
    basis_dir = root / chosen
    if not basis_dir.is_dir() or basis_dir.is_symlink():
        raise _fail(404, "no_report", "No report for this basis yet")
    analysis = base._json_below(basis_dir, ("data", "analysis.json"))
    manifest = base._json_below(basis_dir, ("manifest.json",)) or {}
    if isinstance(manifest, dict):
        manifest = base.scrub(manifest)
    return {
        "basis": chosen,
        "files": _report_files(basis_dir),
        "analysis": analysis if isinstance(analysis, dict) else {},
        "manifest": manifest,
    }


@router.get("/{campaign_id}/report/files/{name:path}")
def get_report_file(campaign_id: str, name: str, basis: str | None = None):
    c = _locate(campaign_id)
    _require_reportable(c)
    chosen = _basis_of(c, basis)
    parts = tuple(name.split("/"))
    if not parts or any(not PART.fullmatch(p) or p in (".", "..") for p in parts):
        raise _fail(404, "not_found", "No such file")
    basis_dir = C.report_root(c.folder) / chosen
    try:
        data = base._read_below(basis_dir, parts, MAX_FILE_BYTES)
    except OSError:
        raise _fail(404, "not_found", "No such file") from None
    kind = CONTENT_TYPES.get(Path(name).suffix, "application/octet-stream")
    return Response(
        content=data, media_type=kind, headers={"Cache-Control": "no-store"}
    )


@router.post("/{campaign_id}/report")
def recompute_report(campaign_id: str, body: ReportBody, request: Request):
    _person(request)
    with base._operation(f"report:{campaign_id}"):
        c = _locate(campaign_id)
        _require_reportable(c)
        if not c.plan:
            raise _fail(409, "no_plan", "The campaign has no readable plan")
        chosen = _basis_of(c, body.basis)
        try:
            manifest = C.write_reports(
                c.folder,
                c.plan,
                c.host,
                c.state,
                c.events,
                rollout_root=c.host_record.get("rollout_root"),
                basis=chosen,
            )
        except (R.ReportError, L.LedgerError, OSError) as exc:
            raise _fail(422, "report_failed", base.scrub(str(exc))[:300]) from None
        base._audit("api_campaign_report", campaign_id=c.id, basis=chosen)
        return {
            "result": "applied",
            "basis": chosen,
            "conclusion_level": (manifest or {}).get("conclusion_level"),
        }
