"""The ``subtask_loop`` reset strategy: no reset policy, only the evaluation
policy and the local VLM (design: user, 2026-10-16).

One run: the VLM lists the movable objects and places on both cameras
(``inventory``), the operator confirms the list at terminal D and it is frozen
as the run's whitelist. Each round: the operator releases the round at
terminal D (never the page, the VLM or a timeout), the round-start frames are
kept as the reference layout, the VLM plans the forward subtasks, and each
subtask runs as one ordinary forward episode whose policy prompt is rendered
by code from ``INSTRUCTION_TEMPLATES``; after each episode the VLM answers the
subtask's ``done_when`` predicate on still frames (true: next subtask; false:
the same subtask again, at most ``subtask_max_retries`` times; null or
unavailable: a person). Then the arm goes home, the VLM plans the reverse
subtasks that restore the reference layout and they run the same way as
``reset`` episodes, and the arm goes home again.

This module holds what the orchestrator and the page share: the journal note
codes (the run's audit trail of every plan, rendered instruction, check and
retry), the instruction template, the validators of the VLM's answers, and
the injectable interfaces with their fakes (``ScriptedSubtaskVlm``,
``FakeVlmPower``, ``ScriptedGate``). The VLM's ``done`` drives the flow only:
it is a model judgement in ``subtask_check`` notes, never an operator label
and never a truth.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .provider_model import exact, strict_json

STRATEGY = "subtask_loop"

# The policy prompt of one subtask, rendered by code (``render``) from two
# whitelist items and nothing else. The wording is the v3 FR3 training style
# ("pick orange into the brown plate"). One place: change it here only.
# One fixed template per relation (user 2026-10-17: "pick up the X and place
# it on the Y" is too long). The relation is the plan's own ``done_when`` fact
# about ``pick`` and ``place`` (``on`` or ``in``); a place that is the table
# always takes the table form. The verb may be tuned here (only here) to the
# policy's training phrasing.
INSTRUCTION_TEMPLATES = {
    "on": "put the {pick} on the {place}",
    "in": "put the {pick} into the {place}",
    "table": "put the {pick} on the table",
}
RELATIONS = ("on", "in")
MAX_INSTRUCTION = 160

# --- journal note codes (each detail is JSON; ``note_detail`` moves a long
# list into a sidecar file under ``<run_dir>/subtasks/``) ------------------------
NOTE_INVENTORY = "subtask_inventory"
NOTE_INVENTORY_CONFIRMED = "subtask_inventory_confirmed"
NOTE_INVENTORY_REJECTED = "subtask_inventory_rejected"
NOTE_PLAN = "subtask_plan"
NOTE_PLAN_REFUSED = "subtask_plan_refused"
NOTE_STARTED = "subtask_started"
NOTE_CHECK = "subtask_check"
NOTE_RETRY = "subtask_retry"
NOTE_LOOP_DONE = "subtask_loop_done"
NOTE_ROUND_RELEASED = "subtask_round_released"
NOTE_ROUND_ABANDONED = "subtask_round_abandoned"
NOTE_VLM_SLEEP = "vlm_sleep"
NOTE_VLM_WAKE = "vlm_wake"
NOTE_VLM_WAKE_FAILED = "vlm_wake_failed"
NOTE_VLM_WAKE_REFUSED = "vlm_wake_refused"
NOTE_VLM_SLEEP_REFUSED = "vlm_sleep_refused"
# The run's alternation mode (sleep | co_resident), journaled once at start,
# with co_resident's GPU headroom check.
NOTE_ALTERNATION = "subtask_alternation"
# subtask.early_check (default off): a subtask episode ended before its step
# budget so the VLM can check ``done_when`` now (what triggered it: an event
# or the interval), and the same subtask continued in a new episode with the
# rest of its budget after an answer other than true.
NOTE_EARLY_CHECK = "subtask_early_check"
NOTE_CONTINUED = "subtask_continued"
NOTES = (
    NOTE_INVENTORY,
    NOTE_INVENTORY_CONFIRMED,
    NOTE_INVENTORY_REJECTED,
    NOTE_PLAN,
    NOTE_PLAN_REFUSED,
    NOTE_STARTED,
    NOTE_CHECK,
    NOTE_RETRY,
    NOTE_LOOP_DONE,
    NOTE_ROUND_RELEASED,
    NOTE_ROUND_ABANDONED,
    NOTE_VLM_SLEEP,
    NOTE_VLM_WAKE,
    NOTE_VLM_WAKE_FAILED,
    NOTE_VLM_WAKE_REFUSED,
    NOTE_VLM_SLEEP_REFUSED,
    NOTE_ALTERNATION,
    NOTE_EARLY_CHECK,
    NOTE_CONTINUED,
)
SIDECAR_DIR = "subtasks"

# --- journal transition reasons of this mode (aeri.TRANSITION_REASONS) ----------
R_DISPATCHED = "subtask_dispatched"  # a subtask episode starts
R_CONTINUE = "subtask_continue"  # to the next (or the same) subtask, no home
R_ENDED = "subtask_ended"  # a reverse subtask episode sealed
R_FORWARD_DONE = "subtask_forward_done"  # the forward loop ended: home
R_REVERSE_DONE = "subtask_reverse_done"  # the reverse loop ended: home, next round
R_CHECK_UNAVAILABLE = "subtask_check_unavailable"
R_RETRY_LIMIT = "subtask_retry_limit"
R_PLAN_REFUSED = "subtask_plan_refused"
R_INVENTORY_REJECTED = "subtask_inventory_rejected"
R_NOT_RELEASED = "subtask_not_released"
R_VLM_UNAVAILABLE = "vlm_unavailable"
# A subtask episode ended early for the VLM's done check (subtask.early_check):
# its stop reason, also the FORWARD_ACTIVE -> FORWARD_STOPPING reason.
R_EARLY_CHECK = "subtask_early_check"
REASONS = (
    R_DISPATCHED,
    R_CONTINUE,
    R_ENDED,
    R_FORWARD_DONE,
    R_REVERSE_DONE,
    R_CHECK_UNAVAILABLE,
    R_RETRY_LIMIT,
    R_PLAN_REFUSED,
    R_INVENTORY_REJECTED,
    R_NOT_RELEASED,
    R_VLM_UNAVAILABLE,
    R_EARLY_CHECK,
)
# Reasons that send the run from a scene state to a person.
TO_HUMAN = (
    R_PLAN_REFUSED,
    R_INVENTORY_REJECTED,
    R_NOT_RELEASED,
    R_VLM_UNAVAILABLE,
)
# Reasons that send it to a person right after a subtask episode.
AFTER_EPISODE_TO_HUMAN = (R_CHECK_UNAVAILABLE, R_RETRY_LIMIT, R_VLM_UNAVAILABLE)

LOOPS = ("forward", "reverse")
# Free GPU memory a wake needs when neither the job nor the VLM profile says.
DEFAULT_WAKE_NEED_MIB = 23000
SLEEP_FALLBACKS = ("wait_human", "level2", "stay_awake")

# --- what the VLM may name (schemas: Lynx, programs/lynx/AERI-SUBTASK-PROMPTS.md
# on hub branch docs/lynx; the whitelist and the template are checked here) ------
# An object id: colour, kind and a number (``pink_plate_1``).
OBJECT_ID = re.compile(r"^[a-z]+_[a-z]+_[0-9]+$")
TABLE = "table"
NO_TARGET = "-"
FACTS = ("on", "in", "in_gripper", "gripper_empty")
CAMERAS = ("side", "wrist", "both", "none")
MAX_OBJECTS = 16
MAX_FACTS = 3
MAX_REASON = 90
MAX_POLICY_TEXT = 80  # the VLM's own instruction field (kept, never sent)
MAX_PLAN = 64
MAX_TASK = 2000
_WORD = re.compile(r"^[a-z]{1,24}$")


# --- subtask.early_check: when a running subtask stops for a done check ----------
# The fixed interval may not be shorter than this (a check costs a seal, a VLM
# wake and a new episode; below it the checks would eat the step budget).
MIN_EVERY_STEPS = 40
MAX_EARLY_CHECKS = 20
EARLY_EVENT_TYPES = ("gripper_open",)


@dataclass(frozen=True)
class EarlyCheck:
    """``subtask.early_check`` of the job (default: absent, no early check).

    ``on_events``: a frame-bearing event of ``event_types`` (the binding's
    ``signal_events`` stream; default the gripper opening, a release) makes a
    check due ``settle_steps`` steps later. ``every_steps``: a check is also
    due that many steps after the attempt's start or its last check (the
    fallback without events). Never before ``min_steps`` steps of the attempt,
    never within ``cooldown_steps`` of the last check or of the step budget's
    end (the ordinary check comes then anyway), at most ``max_checks`` per
    attempt. At least one trigger must be on."""

    on_events: bool = False
    event_types: tuple = EARLY_EVENT_TYPES
    settle_steps: int = 8
    every_steps: int | None = None
    min_steps: int = 20
    cooldown_steps: int = 20
    max_checks: int = 3

    def __post_init__(self):
        if isinstance(self.event_types, list):
            object.__setattr__(self, "event_types", tuple(self.event_types))
        if type(self.on_events) is not bool:
            raise ValueError("early_check.on_events is true or false")
        from levi.domain import aeri

        types = self.event_types
        if (
            not isinstance(types, tuple)
            or not types
            or len(set(types)) != len(types)
            or any(t not in aeri.EVENT_TYPES for t in types)
        ):
            raise ValueError(
                "early_check.event_types: distinct registered event types "
                f"({', '.join(aeri.EVENT_TYPES)})"
            )
        for name, low in (("settle_steps", 0), ("min_steps", 0), ("cooldown_steps", 1)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= 1_000_000:
                raise ValueError(f"early_check.{name} is a whole number >= {low}")
        every = self.every_steps
        if every is not None and (
            type(every) is not int or not MIN_EVERY_STEPS <= every <= 1_000_000
        ):
            raise ValueError(
                f"early_check.every_steps is a whole number >= {MIN_EVERY_STEPS} or null"
            )
        if (
            type(self.max_checks) is not int
            or not 1 <= self.max_checks <= MAX_EARLY_CHECKS
        ):
            raise ValueError(
                f"early_check.max_checks is a whole number from 1 to {MAX_EARLY_CHECKS}"
            )
        if not self.on_events and every is None:
            raise ValueError(
                "early_check needs on_events: true or every_steps (or leave it out)"
            )

    def describe(self) -> dict:
        return {
            "on_events": self.on_events,
            "event_types": list(self.event_types),
            "settle_steps": self.settle_steps,
            "every_steps": self.every_steps,
            "min_steps": self.min_steps,
            "cooldown_steps": self.cooldown_steps,
            "max_checks": self.max_checks,
        }


def early_due(
    config: EarlyCheck,
    *,
    step: int,
    budget: int,
    checks: int,
    last_check: int | None,
    pending: dict | None,
) -> dict | None:
    """The trigger of an early check due at ``step`` (steps of the current
    attempt, across its continued episodes), or None. ``budget``: the
    attempt's step budget; ``checks``/``last_check``: the attempt's early
    checks so far and the step of the latest; ``pending``: the newest
    trigger event not yet checked (``{"event_type", "event_id", "step"}``,
    its step on the same attempt scale). Pure: decides nothing else."""
    if checks >= config.max_checks or step < config.min_steps:
        return None
    if budget - step < config.cooldown_steps:
        return None
    if last_check is not None and step - last_check < config.cooldown_steps:
        return None
    if (
        config.on_events
        and pending is not None
        and step >= pending["step"] + config.settle_steps
    ):
        return {
            "trigger": "event",
            "event_type": pending["event_type"],
            "event_id": pending["event_id"],
            "event_step": pending["step"],
            "step": step,
        }
    every = config.every_steps
    if every is not None and step - (last_check or 0) >= every:
        return {"trigger": "interval", "every_steps": every, "step": step}
    return None


class SubtaskRefused(ValueError):
    """A VLM answer (or an instruction) the validators refuse; ``code`` says why
    (``bad_json``, ``bad_fields``, ``not_whitelisted``, ``empty_plan``, ...)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _decode(raw):
    if raw is None:
        raise SubtaskRefused("timeout", "no answer")
    if isinstance(raw, bytes | bytearray):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise SubtaskRefused("bad_json", "not UTF-8") from None
    if not isinstance(raw, str):
        raise SubtaskRefused("bad_json", f"a {type(raw).__name__}, not text")
    try:
        return strict_json(raw)
    except (ValueError, TypeError) as exc:
        raise SubtaskRefused("bad_json", str(exc)[:200]) from None


def _exact(value, keys, where):
    try:
        exact(value, keys, where)
    except ValueError as exc:
        raise SubtaskRefused("bad_fields", str(exc)) from None


@dataclass(frozen=True)
class Inventory:
    """The objects the VLM listed: ``ids`` in its order, ``graspable`` the ids
    a subtask may pick. The whitelist of the run is ``ids`` plus ``table``."""

    objects: tuple
    ids: tuple
    graspable: tuple

    @property
    def whitelist(self) -> tuple:
        return (*self.ids, TABLE)

    def describe(self) -> list:
        return [dict(o) for o in self.objects]


def parse_inventory(raw) -> Inventory:
    """``{"objects": [{"id", "colour", "kind", "on", "graspable"}]}``: 1..16
    distinct ids of the form ``colour_kind_n``, at least one graspable."""
    value = _decode(raw)
    _exact(value, ("objects",), "inventory")
    rows = value["objects"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_OBJECTS:
        raise SubtaskRefused("bad_inventory", f"1 to {MAX_OBJECTS} objects")
    objects, ids, graspable = [], [], []
    for row in rows:
        _exact(row, ("id", "colour", "kind", "on", "graspable"), "object")
        oid = row["id"]
        if not isinstance(oid, str) or not OBJECT_ID.fullmatch(oid) or len(oid) > 48:
            raise SubtaskRefused("bad_item", repr(oid)[:80])
        for name in ("colour", "kind", "on"):
            if not isinstance(row[name], str) or not 1 <= len(row[name]) <= 48:
                raise SubtaskRefused("bad_item", f"{oid}: {name}")
        if type(row["graspable"]) is not bool:
            raise SubtaskRefused("bad_item", f"{oid}: graspable")
        if oid in ids:
            raise SubtaskRefused("bad_inventory", f"duplicate {oid}")
        ids.append(oid)
        if row["graspable"]:
            graspable.append(oid)
        objects.append({k: row[k] for k in ("id", "colour", "kind", "on", "graspable")})
    if not graspable:
        raise SubtaskRefused("bad_inventory", "nothing graspable")
    return Inventory(tuple(objects), tuple(ids), tuple(graspable))


def whitelist_sha256(items) -> str:
    return hashlib.sha256(
        json.dumps(list(items), separators=(",", ":")).encode()
    ).hexdigest()


def words(item: str) -> str:
    """``pink_plate_2`` -> ``pink plate`` (the policy never sees an id)."""
    if item == TABLE:
        return TABLE
    if not OBJECT_ID.fullmatch(item):
        raise SubtaskRefused("not_whitelisted", repr(item)[:80])
    parts = item.split("_")[:-1]
    if not all(_WORD.fullmatch(p) for p in parts):
        raise SubtaskRefused("bad_item", repr(item)[:80])
    return " ".join(parts)


def render(pick: str, place: str, relation: str = "on") -> str:
    """The policy prompt of one subtask: the relation's fixed template filled
    with the words of two whitelist items, nothing else."""
    if relation not in RELATIONS:
        raise SubtaskRefused("no_relation", repr(relation)[:40])
    key = "table" if place == TABLE else relation
    text = INSTRUCTION_TEMPLATES[key].format(pick=words(pick), place=words(place))
    if not text.strip() or len(text) > MAX_INSTRUCTION:
        raise SubtaskRefused("bad_instruction", "empty or too long")
    return text


def allowed_instructions(inventory: Inventory) -> frozenset:
    """Every prompt the template can make from the frozen whitelist."""
    return frozenset(
        render(a, b, r)
        for a in inventory.graspable
        for b in inventory.whitelist
        for r in RELATIONS
        if a != b
    )


def instruction_allowed(text, inventory) -> bool:
    """LEVI's own prompt check of a subtask_loop run (the bridge asks it before
    a policy accepts an episode prompt): only template texts from the frozen
    whitelist."""
    return (
        isinstance(text, str)
        and inventory is not None
        and text in allowed_instructions(inventory)
    )


@dataclass(frozen=True)
class Subtask:
    index: int
    pick: str
    place: str
    # ((fact, subject, target), ...): one to three facts, all whitelisted.
    done_when: tuple
    instruction: str
    # The VLM's own suggestion for the policy text: kept for the analysis,
    # never sent (the policy gets ``instruction``, rendered by code).
    suggested: str = ""

    def describe(self) -> dict:
        return {
            "index": self.index,
            "pick": self.pick,
            "place": self.place,
            "done_when": [list(f) for f in self.done_when],
            "instruction": self.instruction,
            "suggested": self.suggested,
        }

    def facts_text(self) -> str:
        lines = []
        for n, (fact, subject, target) in enumerate(self.done_when, 1):
            if fact in RELATIONS:
                lines.append(f"{n}. {subject} {fact} {target}")
            elif fact == "in_gripper":
                lines.append(f"{n}. {subject} in_gripper")
            else:
                lines.append(f"{n}. gripper_empty")
        return "\n".join(lines)


def _fact(row, inventory: Inventory, where: str) -> tuple:
    _exact(row, ("fact", "subject", "target"), where)
    fact, subject, target = row["fact"], row["subject"], row["target"]
    if fact not in FACTS:
        raise SubtaskRefused("bad_done_when", f"{where}: fact {fact!r}"[:120])
    ids = set(inventory.ids)
    if fact in RELATIONS:
        if subject not in ids or target not in set(inventory.whitelist):
            raise SubtaskRefused("not_whitelisted", where)
        if subject == target:
            raise SubtaskRefused("bad_done_when", f"{where}: on itself")
    elif fact == "in_gripper":
        if subject not in ids or target != NO_TARGET:
            raise SubtaskRefused("not_whitelisted", where)
    elif subject not in (NO_TARGET, "gripper") or target != NO_TARGET:
        raise SubtaskRefused("not_whitelisted", where)
    return (fact, subject, target)


def relation_of(pick: str, place: str, done) -> str:
    """The relation the plan states between ``pick`` and ``place`` (its
    ``on``/``in`` fact about exactly them); the table is always ``on``. A
    subtask that states none, or both, is refused."""
    if place == TABLE:
        return "on"
    found = {f for f, subject, target in done if (subject, target) == (pick, place)}
    found &= set(RELATIONS)
    if len(found) != 1:
        raise SubtaskRefused("no_relation", f"{pick} -> {place}")
    return found.pop()


def parse_plan(raw, inventory: Inventory, *, loop: str, max_subtasks=None) -> list:
    """``[Subtask]`` from the VLM's plan ``{"steps": [{"pick", "place",
    "instruction", "done_when": [{"fact", "subject", "target"}]}]}``. ``pick``
    is a graspable whitelist id, ``place`` another whitelist id or ``table``;
    every fact names whitelist ids only. The policy prompt is rendered from
    ``pick`` and ``place`` by the template; the VLM's ``instruction`` is kept
    for the analysis and never sent. An empty reverse plan is ``[]`` (the
    scene is already restored); an empty forward plan, one longer than
    ``max_subtasks``, two subtasks whose instructions would read the same for
    different ids, or anything malformed is refused (the run then holds and
    waits for a person)."""
    if loop not in LOOPS:
        raise SubtaskRefused("bad_loop", loop)
    if inventory is None:
        raise SubtaskRefused("no_whitelist")
    value = _decode(raw)
    _exact(value, ("steps",), "plan")
    rows = value["steps"]
    if not isinstance(rows, list):
        raise SubtaskRefused("bad_fields", "steps is a list")
    if not rows:
        if loop == "reverse":
            # The scene is already as the round started (user 2026-10-17).
            return []
        raise SubtaskRefused("empty_plan")
    limit = MAX_PLAN if max_subtasks is None else min(MAX_PLAN, max_subtasks)
    if len(rows) > limit:
        raise SubtaskRefused("too_many_subtasks", f"{len(rows)} > {limit}")
    out = []
    rendered: dict = {}
    for index, row in enumerate(rows):
        where = f"step {index}"
        _exact(row, ("pick", "place", "instruction", "done_when"), where)
        pick, place = row["pick"], row["place"]
        if pick not in set(inventory.graspable) or place not in set(
            inventory.whitelist
        ):
            raise SubtaskRefused("not_whitelisted", where)
        if pick == place:
            raise SubtaskRefused("same_item", where)
        suggested = row["instruction"]
        if not isinstance(suggested, str) or len(suggested) > MAX_POLICY_TEXT:
            raise SubtaskRefused("bad_fields", f"{where}: instruction")
        facts = row["done_when"]
        if not isinstance(facts, list) or not 1 <= len(facts) <= MAX_FACTS:
            raise SubtaskRefused("bad_done_when", f"{where}: 1 to 3 facts")
        done = tuple(
            _fact(f, inventory, f"{where} fact {n}") for n, f in enumerate(facts)
        )
        text = render(pick, place, relation_of(pick, place, done))
        # Two same-colour items render to the same words: a plan in which one
        # instruction would stand for two different moves is refused (the
        # policy could not tell them apart). Decision 2026-10-17, option 2.
        if rendered.setdefault(text, (pick, place)) != (pick, place):
            raise SubtaskRefused("ambiguous_instruction", f"{where}: {text}")
        out.append(Subtask(index, pick, place, done, text, suggested))
    return out


def reverse_task(task: str) -> str:
    """The reverse loop's task name, made by code (not by the VLM)."""
    return f"undo: {task}"[:MAX_TASK]


def parse_check(raw, subtask: Subtask) -> bool | None:
    """The answers to one subtask's ``done_when`` facts on still frames:
    ``{"answers": [{"index", "value", "camera", "reason"}]}``, one per fact.
    The decision is derived here, never taken from the model: ``True`` only
    when every fact is ``true``, ``False`` when any is ``false``, ``None``
    otherwise (null: the existing "cannot tell" route, a person).
    ``SubtaskRefused`` for anything malformed (treated as unavailable)."""
    value = _decode(raw)
    _exact(value, ("answers",), "check")
    rows = value["answers"]
    n = len(subtask.done_when)
    if not isinstance(rows, list) or len(rows) != n:
        raise SubtaskRefused("bad_check", f"exactly {n} answers")
    found = {}
    for row in rows:
        _exact(row, ("index", "value", "camera", "reason"), "answer")
        index, answer = row["index"], row["value"]
        if type(index) is not int or not 1 <= index <= n or index in found:
            raise SubtaskRefused("bad_check", "answer index")
        if answer is not None and type(answer) is not bool:
            raise SubtaskRefused("bad_check", "true/false/null only")
        if row["camera"] not in CAMERAS:
            raise SubtaskRefused("bad_check", "camera")
        if not isinstance(row["reason"], str) or len(row["reason"]) > MAX_REASON:
            raise SubtaskRefused("bad_check", "reason")
        found[index] = answer
    values = [found[i] for i in range(1, n + 1)]
    if any(v is False for v in values):
        return False
    if all(v is True for v in values):
        return True
    return None


def frames_brief(frames) -> list:
    """``{ref, sha256}`` per frame, plus ``seq``/``t_capture_ns`` when the
    camera frame bus gave them (CAMERA-FRAME-BUS.md)."""
    out = []
    for f in frames:
        item = {"ref": f["ref"], "sha256": f["sha256"]}
        if "seq" in f:
            item["seq"] = f["seq"]
            item["t_capture_ns"] = f.get("t_capture_ns")
        out.append(item)
    return out


# --- the prompts (Lynx, AERI-SUBTASK-PROMPTS.md; two images first, then text) ----
PROMPT_INVENTORY = (
    "Image 1 is the side camera, image 2 is the wrist camera, both taken now.\n"
    "List the objects on the table that the robot could pick up or place something on.\n"
    "Give each object a short id made of colour, type and a number (for example pink_plate_1).\n"
    "For each object say where it is: on the table, on top of another object (give its id), or in the gripper.\n"
    "Ignore objects outside the robot's work area and anything you cannot see clearly.\n"
    "Answer only with the JSON."
)
PROMPT_PLAN = (
    "Task: {task}.\n"
    "Allowed objects: {whitelist}.\n"
    "Direction: {direction}.\n"
    "{direction_instructions}\n"
    "Each step moves exactly one allowed object: pick it, place it on an allowed object or on the table.\n"
    "For each step give done_when: the facts that are true in the images once the step is finished.\n"
    "For on/in facts, subject must be an object id and target an allowed object or table; they must differ.\n"
    "For in_gripper, subject must be an object id and target must be '-'.\n"
    "For gripper_empty, subject must be '-' or 'gripper' and target must be '-'.\n"
    "Use at most 6 steps. If the goal already holds, return no steps.\n"
    "Answer only with the JSON."
)
PLAN_DIRECTIONS = {
    "forward": (
        "Image 1 is the side camera now, image 2 is the wrist camera now; "
        "plan the steps that complete the task. Cover every object that still "
        "needs to move to complete the task; leave objects already in their "
        "required positions where they are."
    ),
    "reverse": (
        "Image 1 is the side camera now, image 2 is the side camera at the "
        "start of this round; plan the steps that put every moved object "
        "back where it was in image 2."
    ),
}
PROMPT_CHECK = (
    "Image 1 is the side camera now, image 2 is the wrist camera now.\n"
    "For each fact below, answer true if both images clearly show it, false if they clearly show it is not so, null if you cannot tell.\n"
    "Facts:\n"
    "{facts}\n"
    "Give the camera that shows it best and at most 12 words of reason.\n"
    "Answer only with the JSON."
)
SCHEMA_INVENTORY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["objects"],
    "properties": {
        "objects": {
            "type": "array",
            "maxItems": MAX_OBJECTS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "colour", "kind", "on", "graspable"],
                "properties": {
                    "id": {"type": "string", "pattern": OBJECT_ID.pattern},
                    "colour": {"type": "string"},
                    "kind": {"type": "string"},
                    "on": {"type": "string"},
                    "graspable": {"type": "boolean"},
                },
            },
        }
    },
}
SCHEMA_PLAN = {
    "type": "object",
    "additionalProperties": False,
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["pick", "place", "instruction", "done_when"],
                "properties": {
                    "pick": {"type": "string"},
                    "place": {"type": "string"},
                    "instruction": {"type": "string", "maxLength": MAX_POLICY_TEXT},
                    "done_when": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_FACTS,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["fact", "subject", "target"],
                            "properties": {
                                "fact": {"type": "string", "enum": list(FACTS)},
                                "subject": {"type": "string"},
                                "target": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
}
SCHEMA_CHECK = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answers"],
    "properties": {
        "answers": {
            "type": "array",
            "maxItems": MAX_FACTS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "value", "camera", "reason"],
                "properties": {
                    "index": {"type": "integer", "minimum": 1, "maximum": MAX_FACTS},
                    "value": {"type": ["boolean", "null"]},
                    "camera": {"type": "string", "enum": list(CAMERAS)},
                    "reason": {"type": "string", "maxLength": MAX_REASON},
                },
            },
        }
    },
}
MAX_TOKENS = {"inventory": 400, "plan": 500, "check": 200}


def prompt_text(kind: str, **fill) -> str:
    if kind == "inventory":
        return PROMPT_INVENTORY
    if kind == "plan":
        return PROMPT_PLAN.format(
            task=fill["task"],
            whitelist=", ".join(fill["whitelist"]),
            direction=fill["direction"],
            direction_instructions=PLAN_DIRECTIONS[fill["direction"]],
        )
    if kind == "check":
        return PROMPT_CHECK.format(facts=fill["facts"])
    raise ValueError(kind)


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class VlmReply:
    """What a real VLM adapter returns: the raw text plus the request's
    measurements (all optional; a fake returns the bare text)."""

    raw: str | None
    prompt_sha256: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    request_id: str | None = None
    # This client's own id of the request, the frames actually sent, the
    # timeout it waited with, whether the HTTP answer had the expected shape
    # (choices[0].message.content a string), and why not.
    client_request_id: str | None = None
    image_refs: list | None = None
    # Frame bus: ``{ref, view, seq, t_capture_ns}`` of each sent image that
    # had camera metadata (None: no metadata, the old behaviour).
    image_meta: list | None = None
    timeout_s: float | None = None
    response_schema_ok: bool | None = None
    error: str | None = None
    # Additive evidence metadata; older adapters need not provide it.
    usage_source: str = "unavailable"
    finish_reason: str | None = None
    # Optional evidence only; never used as an action or operator label.
    external_evidence: dict | None = None
    prompt_text: str | None = None
    prompt_bytes: int | None = None
    prompt_text_truncated: bool = False


# --- the injectable parts and their fakes ---------------------------------------


class VlmPower:
    """Sleep/wake of the local VLM and the two read-only memory readings
    (interface). ``wake``/``sleep`` return True once done, False on failure or
    timeout; a reading returns None when it cannot be read."""

    def wake(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def sleep(self, level: int = 1) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def free_gpu_mib(self) -> int | None:  # pragma: no cover - interface
        raise NotImplementedError

    def mem_available_gib(self) -> float | None:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class FakeVlmPower(VlmPower):
    """In process. ``trace`` (shared with the other fakes when given) gets
    ``("sleep", level)`` / ``("wake",)`` in call order."""

    free_mib: int | None = 30_000
    mem_gib: float | None = 64.0
    wake_ok: bool = True
    sleep_ok: bool = True
    awake: bool = True
    trace: list = field(default_factory=list)
    sleeps: list = field(default_factory=list)
    wakes: int = 0

    def wake(self) -> bool:
        self.trace.append(("wake",))
        self.wakes += 1
        if not self.wake_ok:
            return False
        self.awake = True
        return True

    def sleep(self, level: int = 1) -> bool:
        self.trace.append(("sleep", level))
        self.sleeps.append(level)
        if not self.sleep_ok:
            return False
        self.awake = False
        return True

    def free_gpu_mib(self):
        return self.free_mib

    def mem_available_gib(self):
        return self.mem_gib

    def gpu_totals(self):
        return (32607 - (self.free_mib or 0), 32607)


class ScriptedSubtaskVlm:
    """The VLM's three questions, answered from scripts (lists of raw texts,
    ``None`` for a timeout, or an exception instance to raise). ``capture``
    returns two still frames per call. ``on_call(kind)`` lets a test act at
    the moment of a call (register a stop, read the run's state). A call
    while ``power`` says the VLM sleeps raises (the alternation broken)."""

    def __init__(
        self,
        *,
        inventory=None,
        plans=None,
        checks=None,
        power=None,
        trace=None,
        on_call=None,
        default_check=None,
        default_plan=None,
    ):
        self.inventory_script = list(inventory or [])
        self.plan_script = list(plans or [])
        self.check_script = list(checks or [])
        self.default_check = default_check
        self.default_plan = default_plan
        self.power = power
        self.trace = trace if trace is not None else []
        self.on_call = on_call
        self.calls: list = []
        self._frames = 0

    def _ask(self, kind, script, **info):
        self.trace.append((kind,))
        self.calls.append((kind, info))
        if self.power is not None and not self.power.awake:
            raise RuntimeError(f"{kind}: the VLM is asleep")
        if self.on_call is not None:
            self.on_call(kind, info)
        if script:
            found = script.pop(0)
        elif kind == "check":
            found = self.default_check
        elif kind == "plan":
            found = self.default_plan
        else:
            found = None
        if isinstance(found, BaseException):
            raise found
        if callable(found):
            found = found(info)
        return found

    def capture(self, purpose: str) -> list:
        self._frames += 1
        out = []
        for view in ("front", "wrist"):
            ref = f"{view}:{purpose}-{self._frames}"
            out.append(
                {
                    "ref": ref,
                    "sha256": hashlib.sha256(ref.encode()).hexdigest(),
                    "view": view,
                }
            )
        return out

    def inventory(self, *, frames):
        return self._ask("inventory", self.inventory_script, frames=frames)

    def plan(self, *, loop, task, whitelist, frames, reference=None, round_no=None):
        return self._ask(
            "plan",
            self.plan_script,
            loop=loop,
            task=task,
            whitelist=list(whitelist),
            frames=frames,
            reference=reference,
        )

    def check(self, *, subtask, frames):
        return self._ask("check", self.check_script, subtask=subtask, frames=frames)


def check_answer(value, n: int = 1) -> str:
    """A well-formed check answer to ``n`` facts, all ``value`` (fakes, tests)."""
    return json.dumps(
        {
            "answers": [
                {"index": i, "value": value, "camera": "side", "reason": "seen"}
                for i in range(1, n + 1)
            ]
        }
    )


def checks(*values):
    """Check script entries answering ``values`` (one fact per subtask)."""
    return [check_answer(v) for v in values]


def inventory_answer(objects) -> str:
    """A well-formed inventory: ``objects`` of (id, graspable)."""
    rows = []
    for oid, graspable in objects:
        colour, kind, _ = oid.split("_")
        rows.append(
            {
                "id": oid,
                "colour": colour,
                "kind": kind,
                "on": "table",
                "graspable": graspable,
            }
        )
    return json.dumps({"objects": rows})


def plan_answer(pairs, relation: str = "on") -> str:
    """A well-formed plan (for fakes and tests): ``pairs`` of (pick, place)."""
    rows = [
        {
            "pick": a,
            "place": b,
            "instruction": f"put {a} on {b}",
            "done_when": [{"fact": relation, "subject": a, "target": b}],
        }
        for a, b in pairs
    ]
    return json.dumps({"steps": rows})


class Gate:
    """Terminal D's two questions of this mode (interface; the console
    implements it, nothing else does: no page, inbox or command route).

    ``confirm_inventory(items)`` -> ``"yes"`` | ``"no"`` | ``"stopped"``;
    ``release_round(round_no, total)`` -> ``"released"`` | ``"stopped"`` |
    ``"refused"``."""

    def confirm_inventory(self, items) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def release_round(self, round_no: int, total: int) -> str:  # pragma: no cover
        raise NotImplementedError


class ScriptedGate(Gate):
    def __init__(self, *, inventory=("yes",), releases=None, trace=None, on_call=None):
        self.inventory_answers = list(inventory)
        # None: release every round.
        self.release_answers = None if releases is None else list(releases)
        self.trace = trace if trace is not None else []
        self.on_call = on_call
        self.asked: list = []

    def confirm_inventory(self, items) -> str:
        self.trace.append(("confirm",))
        self.asked.append(("inventory", tuple(items)))
        if self.on_call is not None:
            self.on_call("confirm", items)
        return self.inventory_answers.pop(0) if self.inventory_answers else "no"

    def release_round(self, round_no, total) -> str:
        self.trace.append(("release", round_no))
        self.asked.append(("release", round_no, total))
        if self.on_call is not None:
            self.on_call("release", round_no)
        if self.release_answers is None:
            return "released"
        return self.release_answers.pop(0) if self.release_answers else "refused"


# --- the run's state of this mode (memory only: a restart never continues a
# round; the whitelist and the round counts are read back from the journal) -----


@dataclass
class SubtaskState:
    inventory: Inventory | None = None
    whitelist_sha256: str | None = None
    # idle | forward | reverse_pending | reverse
    phase: str = "idle"
    round_no: int = 0
    task: str | None = None
    plan: list = field(default_factory=list)
    index: int = 0
    attempt: int = 1
    retries: int = 0
    checks: int = 0
    dispatched: int = 0
    reference: list = field(default_factory=list)
    outcome: str | None = None
    # unknown | awake | asleep | idle_awake (stay_awake fallback)
    vlm: str = "unknown"
    # The state before the latest wake and how long it took (analysis).
    last_wake: dict = field(default_factory=dict)
    alternation_checked: bool = False
    round_id: str | None = None
    round_labelled: bool = False
    round_episodes: list = field(default_factory=list)
    reverse_episodes: list = field(default_factory=list)
    # subtask.early_check, per attempt: the steps its ended episodes used (a
    # continued episode gets the rest of the budget), the episode number in
    # the attempt (1, 2, ...), its early checks and the step of the latest.
    used_steps: int = 0
    segment: int = 1
    early_checks: int = 0
    early_last: int | None = None
    # The episode this attempt continues (its predecessor), for the metadata.
    continues: str | None = None

    def new_attempt(self) -> None:
        """A new subtask, or the same one again after a false check: the
        whole step budget and no early checks yet."""
        self.used_steps = 0
        self.segment = 1
        self.early_checks = 0
        self.early_last = None
        self.continues = None

    def start_loop(self, loop, task, plan) -> None:
        self.phase = loop
        self.task = task
        self.plan = list(plan)
        self.index = 0
        self.attempt = 1
        self.retries = 0
        self.checks = 0
        self.dispatched = 0
        self.new_attempt()
        if loop == "forward":
            # The forward subtask episodes of this round that ran a step: the
            # members of the round's label (never labelled one by one).
            self.round_episodes = []
            self.reverse_episodes = []

    def current(self) -> Subtask:
        return self.plan[self.index]

    def loop_name(self) -> str:
        return "reverse" if self.phase in ("reverse", "reverse_pending") else "forward"


def dry_run_parts():
    """``(vlm, power, gate)`` for ``levi automatic run --dry-run`` of a
    subtask_loop job: two plates, one forward and one reverse subtask per
    round, every check true, every round released."""
    power = FakeVlmPower()
    vlm = ScriptedSubtaskVlm(
        inventory=[inventory_answer([("pink_plate_1", True), ("pink_plate_2", True)])],
        power=power,
        default_check=check_answer(True),
        default_plan=lambda info: plan_answer(
            [("pink_plate_2", "pink_plate_1")]
            if info["loop"] == "forward"
            else [("pink_plate_2", "table")]
        ),
    )
    return vlm, power, ScriptedGate(inventory=("yes",))
