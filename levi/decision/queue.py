"""Escalation queue: append-only, replayable, idempotent (design 3.2).

An escalated item whose external panel is unavailable, or whose response is not valid,
waits here. The queue never writes a label, never hands back the local answer and never
asks a person: its only states are ``pending_external``, ``answered_external`` and
``external_failed``. The idempotency key is the sha256 of the teacher-visible request.

File: JSON lines (``levi.decision.queue.v1``), one event per line, only appended:
``enqueue`` (request wire and its question kind), ``rejected`` (attempt, problems),
``answered`` (attempt, the response), ``failed``. State is the replay of the file. The
first valid response wins; later submissions are ignored. An unavailable panel is not an
event. A rejected response counts as an attempt; after ``max_attempts`` the item is
``external_failed`` (it stays in the denominator, never answered by the local model).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import leakcheck, safety
from .contracts import (
    PANEL_PROTOCOL,
    REQUEST_CONTRACT,
    TOKEN_FIELDS,
    EscalationRequest,
    EscalationResponse,
    LabelTier,
    State,
    TokenRow,
)

SCHEMA = "levi.decision.queue.v1"
DEFAULT_MAX_ATTEMPTS = 3
MAX_DEPTH = 32
FAILED = "external_failed"
PENDING = State.PENDING_EXTERNAL.value
ANSWERED = State.ANSWERED_EXTERNAL.value
WIRE_KEYS = frozenset(
    {
        "contract",
        "request_id",
        "spec_id",
        "messages",
        "response_schema",
        "signals",
        "panel_protocol",
    }
)


class QueueCorrupt(ValueError):
    """A queue line is not an event this module wrote: refuse rather than guess."""


class PhysicalQuestion(ValueError):
    """A physical-safety question never goes to the external panel: it goes to a person."""


class UnsupportedSchema(ValueError):
    """The response schema uses a keyword this validator cannot check (fail closed)."""


# --- request wire -----------------------------------------------------------------


def to_wire(request: EscalationRequest) -> dict:
    """What the panel sees. No local answer, no confidence, no question kind, no item ref."""
    return {
        "contract": request.contract,
        "request_id": request.request_id,
        "spec_id": request.spec_id,
        "messages": [_plain(m) for m in request.messages],
        "response_schema": _plain(request.response_schema),
        "signals": _plain(request.signals),
        "panel_protocol": request.panel_protocol,
    }


def _plain(node):
    return json.loads(json.dumps(node, allow_nan=False))


def request_sha256(request: EscalationRequest) -> str:
    """The idempotency key: sha256 of the canonical teacher-visible request."""
    return _wire_sha256(to_wire(request))


def _wire_sha256(wire: Mapping) -> str:
    text = json.dumps(
        wire, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reuse_reference_answer(request: EscalationRequest, reference_sha256: str, answer):
    """The recorded teacher answer if it was given to exactly this request, else None (U21)."""
    return answer if request_sha256(request) == reference_sha256 else None


def _from_wire(wire: Mapping, question_kind: str) -> EscalationRequest:
    return EscalationRequest(
        request_id=wire["request_id"],
        question_kind=question_kind,
        spec_id=wire["spec_id"],
        messages=tuple(wire["messages"]),
        response_schema=wire["response_schema"],
        signals=wire["signals"],
        contract=wire["contract"],
        panel_protocol=wire["panel_protocol"],
    )


# --- strict JSON and schema -------------------------------------------------------


def strict_loads(text):
    """json.loads that refuses duplicate keys, NaN/Infinity and nesting deeper than 32."""

    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(items)

    def bad(name):
        raise ValueError(f"non-finite number {name}")

    if isinstance(text, bytes):
        text = text.decode("utf-8")
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=bad)
    except RecursionError:
        raise ValueError("nested too deep") from None
    _depth(value, 0)
    return value


def _depth(node, level):
    if level > MAX_DEPTH:
        raise ValueError("nested too deep")
    if isinstance(node, Mapping):
        for v in node.values():
            _depth(v, level + 1)
    elif isinstance(node, list):
        for v in node:
            _depth(v, level + 1)


_ANNOTATIONS = {"title", "description", "$schema"}
_KEYWORDS = _ANNOTATIONS | {
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
}
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: (
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    ),
}


def check_schema_supported(schema) -> None:
    """Raise ``UnsupportedSchema`` if ``schema`` uses a keyword ``conforms`` does not check."""
    if not isinstance(schema, Mapping):
        raise UnsupportedSchema("schema must be an object")
    for key, val in schema.items():
        if key not in _KEYWORDS:
            raise UnsupportedSchema(f"keyword {key!r}")
        if key == "properties":
            for sub in val.values():
                check_schema_supported(sub)
        elif key == "items":
            check_schema_supported(val)
        elif key == "type":
            names = [val] if isinstance(val, str) else val
            if not all(n in _TYPES for n in names):
                raise UnsupportedSchema(f"type {val!r}")
        elif key == "additionalProperties" and not isinstance(val, bool):
            raise UnsupportedSchema("additionalProperties must be a boolean")


def conforms(value, schema: Mapping) -> bool:
    if "type" in schema:
        names = [schema["type"]] if isinstance(schema["type"], str) else schema["type"]
        if not any(_TYPES[n](value) for n in names):
            return False
    if "enum" in schema and not any(_same(value, e) for e in schema["enum"]):
        return False
    if "const" in schema and not _same(value, schema["const"]):
        return False
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if any(r not in value for r in schema.get("required", ())):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(props):
            return False
        if any(not conforms(v, props[k]) for k, v in value.items() if k in props):
            return False
    return not (
        isinstance(value, list)
        and "items" in schema
        and any(not conforms(v, schema["items"]) for v in value)
    )


def _same(a, b) -> bool:
    return type(a) is type(b) and a == b  # True != 1


# --- response ---------------------------------------------------------------------


@dataclass(frozen=True)
class Parsed:
    response: EscalationResponse | None
    problems: tuple[str, ...]
    token_rows: tuple[
        tuple[TokenRow, str | None], ...
    ] = ()  # (row, source_code), even when rejected


def parse_response(text, request: EscalationRequest) -> Parsed:
    """Check a response against its request. ``problems`` empty = a valid answer.

    Codes: not_json, contract, request_id, label_tier, answer_schema, reviewer_schema,
    adjudication_missing, answer_not_panel_result, panel.
    """
    try:
        obj = strict_loads(text)
        if not isinstance(obj, dict):
            raise TypeError("not an object")
    except (ValueError, TypeError, UnicodeDecodeError):
        return Parsed(None, ("not_json",))
    token_rows = _tokens(obj.get("tokens"))
    panel = obj.get("panel")
    if (
        not isinstance(panel, dict)
        or "reviewer_a" not in panel
        or "reviewer_b" not in panel
    ):
        return Parsed(None, ("panel",), token_rows)
    tier = obj.get("label_tier")
    resp = EscalationResponse(
        request_id=obj.get("request_id"),
        answer=obj.get("answer"),
        reviewer_a=panel["reviewer_a"],
        reviewer_b=panel["reviewer_b"],
        adjudicator=panel.get("adjudicator"),
        tokens=tuple(r for r, _ in token_rows),
        label_tier=LabelTier.AGENT_REVIEWED
        if tier == LabelTier.AGENT_REVIEWED.value
        else None,
        contract=obj.get("contract"),
    )
    problems = list(resp.problems(request))
    schema = request.response_schema
    if not conforms(resp.answer, schema):
        problems.append("answer_schema")
    members = [resp.reviewer_a, resp.reviewer_b] + (
        [resp.adjudicator] if resp.adjudicator is not None else []
    )
    if any(not conforms(m, schema) for m in members):
        problems.append("reviewer_schema")
    if "adjudication_missing" not in problems:
        decided = (
            resp.adjudicator if resp.reviewer_a != resp.reviewer_b else resp.reviewer_a
        )
        if resp.answer != decided:
            problems.append("answer_not_panel_result")
    return Parsed(resp, tuple(problems), token_rows)


def _tokens(raw) -> tuple[tuple[TokenRow, str | None], ...]:
    """Token rows as reported; a field that is not a count is unknown (None), never 0."""
    out = []
    for row in raw if isinstance(raw, list) else ():
        if not isinstance(row, dict):
            continue
        fields = {
            n: (
                row.get(n)
                if isinstance(row.get(n), int)
                and not isinstance(row.get(n), bool)
                and row.get(n) >= 0
                else None
            )
            for n in TOKEN_FIELDS
        }
        exact = row.get("exact") is True and all(v is not None for v in fields.values())
        role = row.get("role") if isinstance(row.get("role"), str) else "unknown"
        code = (
            row.get("source_code") if isinstance(row.get("source_code"), str) else None
        )
        out.append((TokenRow(role=role, exact=exact, **fields), code))
    return tuple(out)


# --- queue ------------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    sha256: str
    state: str  # pending_external | answered_external | external_failed
    attempts: int
    problems: tuple[str, ...] = ()  # of the last rejected response
    response: EscalationResponse | None = None


class Queue:
    def __init__(
        self,
        path,
        denylist: leakcheck.Denylist,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.path = Path(path)
        self.denylist = denylist
        self.max_attempts = max_attempts

    # -- file --
    def _events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for n, line in enumerate(self.path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                if not (
                    ev["schema"] == SCHEMA
                    and ev["event"] in ("enqueue", "rejected", "answered", "failed")
                    and isinstance(ev["sha256"], str)
                ):
                    raise ValueError("event")
            except (ValueError, KeyError, TypeError):
                raise QueueCorrupt(
                    f"line {n} of {self.path} is not a queue event"
                ) from None
            out.append(ev)
        return out

    def _append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (
            json.dumps({"schema": SCHEMA, **event}, sort_keys=True, allow_nan=False)
            + "\n"
        )
        with self.path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _fold(self) -> dict[str, dict]:
        items: dict[str, dict] = {}
        for ev in self._events():
            sha = ev["sha256"]
            if ev["event"] == "enqueue":
                items.setdefault(
                    sha,
                    {
                        "wire": ev["request"],
                        "kind": ev["question_kind"],
                        "state": PENDING,
                        "attempts": 0,
                        "problems": (),
                        "response": None,
                    },
                )
                continue
            it = items.get(sha)
            if it is None:
                raise QueueCorrupt(f"event {ev['event']} for unknown request {sha}")
            if it["state"] != PENDING:
                continue  # first valid answer (or failure) wins; later events change nothing
            if ev["event"] == "rejected":
                it["attempts"] = ev["attempt"]
                it["problems"] = tuple(ev["problems"])
            elif ev["event"] == "answered":
                # Replay trusts no stored answer: a line that is not a valid panel
                # response to this very request is a corrupt file, not an answer.
                problems = parse_response(
                    json.dumps(ev["response"]), _from_wire(it["wire"], it["kind"])
                ).problems
                if problems:
                    raise QueueCorrupt(
                        f"answered event for {sha} is not a valid response: "
                        + ", ".join(problems)
                    )
                it["attempts"], it["state"], it["response"] = (
                    ev["attempt"],
                    ANSWERED,
                    ev["response"],
                )
            else:
                it["state"] = FAILED
        return items

    # -- api --
    def enqueue(self, request: EscalationRequest, context) -> str:
        """Add the request (idempotent on its sha256) and return the sha256.

        ``context`` ("offline" or "live") is the context the question was asked in. A
        physical-safety question (static table, ``levi.decision.safety``; an unlisted
        kind or an unknown context counts as physical) is refused with
        ``PhysicalQuestion``: it goes to a person, never to the panel. The router makes
        the same decision first; this is the second line. Also refused: a request that
        carries a denied id (``LeakFound``) or whose response schema cannot be checked
        (``UnsupportedSchema``). Nothing is written then."""
        if safety.is_physical(request.question_kind, context):
            raise PhysicalQuestion(
                f"question kind {request.question_kind!r} in context {context!r} is "
                "physical safety (or not registered): it goes to a person, not the panel"
            )
        wire = to_wire(request)
        if (
            request.contract != REQUEST_CONTRACT
            or request.panel_protocol != PANEL_PROTOCOL
        ):
            raise ValueError(
                "request contract or panel protocol is not the registered one"
            )
        check_schema_supported(request.response_schema)
        leakcheck.check(wire, self.denylist)
        sha = _wire_sha256(wire)
        if sha not in self._fold():
            self._append(
                {
                    "event": "enqueue",
                    "sha256": sha,
                    "question_kind": request.question_kind,
                    "request": wire,
                }
            )
        return sha

    def status(self, sha: str) -> Status:
        items = self._fold()
        if sha not in items:
            raise KeyError(sha)
        return self._status(sha, items[sha])

    def _status(self, sha, it) -> Status:
        response = None
        if it["response"] is not None:
            parsed = parse_response(json.dumps(it["response"]), self.request(sha, it))
            if (
                parsed.problems
            ):  # _fold checked already; never report an unchecked answer
                raise QueueCorrupt(f"stored answer for {sha} is not a valid response")
            response = parsed.response
        return Status(sha, it["state"], it["attempts"], it["problems"], response)

    def request(self, sha: str, item: dict | None = None) -> EscalationRequest:
        it = item or self._fold()[sha]
        return _from_wire(it["wire"], it["kind"])

    def pending(self) -> list[str]:
        return [s for s, it in self._fold().items() if it["state"] == PENDING]

    def submit(self, sha: str, text) -> tuple[Status, Parsed | None]:
        """Offer a panel response. Valid and first: answered. Invalid: an attempt is used up,
        and after ``max_attempts`` the item is ``external_failed``. Already settled: ignored
        (``Parsed`` is None)."""
        items = self._fold()
        it = items[sha]
        if it["state"] != PENDING:
            return self._status(sha, it), None
        parsed = parse_response(text, _from_wire(it["wire"], it["kind"]))
        attempt = it["attempts"] + 1
        if not parsed.problems:
            self._append(
                {
                    "event": "answered",
                    "sha256": sha,
                    "attempt": attempt,
                    "response": strict_loads(text),
                }
            )
        else:
            self._append(
                {
                    "event": "rejected",
                    "sha256": sha,
                    "attempt": attempt,
                    "problems": list(parsed.problems),
                }
            )
            if attempt >= self.max_attempts:
                self._append({"event": "failed", "sha256": sha, "attempts": attempt})
        return self.status(sha), parsed

    def drain(
        self, send: Callable[[dict], object]
    ) -> tuple[list[tuple[Status, Parsed | None]], bool]:
        """Replay every pending request through ``send(wire)`` in order.

        ``send`` returns the response text, or None / raises OSError, TimeoutError,
        ConnectionError when the panel is unavailable: the run stops there (the rest stays
        pending, nothing is written). Each request is leak-checked again right before it is
        sent (``LeakFound`` propagates, nothing is sent). Returns (results, unavailable)."""
        results = []
        for sha in self.pending():
            wire = self._fold()[sha]["wire"]
            leakcheck.check(wire, self.denylist)
            try:
                text = send(wire)
            except (OSError, TimeoutError, ConnectionError):
                return results, True
            if text is None:
                return results, True
            results.append(self.submit(sha, text))
        return results, False

    def counts(self) -> dict:
        """Items per state. Pending and failed stay in the denominator (scored as wrong)."""
        items = self._fold()
        n = {
            s: sum(1 for it in items.values() if it["state"] == s)
            for s in (PENDING, ANSWERED, FAILED)
        }
        return {"total": len(items), **n, "in_denominator": sum(n.values())}
