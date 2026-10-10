"""Event, goal-judgement and scene providers as the automatic pipeline (C)
sees them, and scriptable fakes of each (T-C-02).

Providers speak the wire format: every message they hand over is bytes of a
``levi.aeri.*.v1`` contract, and C reads each one with ``aeri.parse`` (so a
provider bug, a newer minor or a smuggled control key is refused at the
door, never trusted). Calls never block a control loop: ``submit`` returns
a ticket at once, ``collect(timeout_ns=0)`` looks without waiting.

Requests C sends (``make_request``) carry no operator or evaluation keys:
operator truth never reaches a model.

The fakes are deterministic: they run on the caller's ``FakeClock`` and a
script; ``collect`` advances that clock when it waits.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from levi.domain import aeri

GOAL_SPEC = ("generic-final", "1")
SCENE_CONTRACT = ("fake-initial-state", "1")
SCENE_PREDICATES = ("object_at_source", "gripper_open")
# Specs the fakes cite, for ``aeri.parse(..., specs=SPECS)``.
SPECS = {
    **aeri.BUILTIN_SPECS,
    SCENE_CONTRACT: frozenset(SCENE_PREDICATES),
}
DEFAULT_DELAY_NS = 300_000_000
DEFAULT_VALID_MS = 5_000


class RequestRefused(ValueError):
    """A request C must not send (operator or control keys in it)."""


def make_request(**fields) -> dict:
    """A judgement or scene request; refused when a key names operator or
    evaluation data, or a robot command."""
    found = aeri.operator_keys(fields) + aeri.control_keys(fields)
    if found:
        raise RequestRefused(f"{', '.join(found)}: never sent to a provider")
    return dict(fields)


@dataclass
class Ticket:
    request_id: str
    number: int
    submitted_ns: int
    ready_ns: int
    request: dict = field(repr=False)
    response: dict = field(repr=False, default_factory=dict)
    delivered: bool = False


class EventStream(Protocol):
    def open(self, episode_id: str, role: str) -> str: ...
    def poll(
        self, stream_id: str, *, through_step: int, max_events: int
    ) -> list[bytes]: ...
    def close(self, stream_id: str) -> None: ...


class GoalVerifier(Protocol):
    def submit(self, request: dict) -> Ticket | bytes: ...  # bytes: unavailable
    def collect(self, ticket: Ticket, *, timeout_ns: int) -> bytes | None: ...
    def cancel(self, ticket: Ticket, reason: str) -> None: ...


class SceneAssessor(GoalVerifier, Protocol):
    """Same calls; the answers are ``levi.aeri.scene.v1``."""


def _envelope(schema: str, run_id: str, minor: int = 0) -> dict:
    return {
        "schema": aeri.SCHEMAS[schema],
        "minor": minor,
        "run_id": run_id,
        "emitted_wall_ns": 0,
    }


def _encode(message: dict, *, control_key: bool = False) -> bytes:
    if control_key:
        message = {**message, "robot_stop": True}
    return json.dumps(message, allow_nan=True).encode()


# --- events ---------------------------------------------------------------------------


class FakeEventStream:
    """Events at scripted steps. ``script`` maps an episode id, or a role
    (``forward``/``reset``) for every episode of it, to a list of
    ``{"step", "event_type", "priority"?, "id"?, "fault"?, "retracts"?}``.

    Faults: ``skip_seq`` (a sequence number is lost), ``duplicate`` (the id
    of the previous event again), ``other_clock``, ``control_key``,
    ``bad_json``, ``wrong_episode``."""

    DETECTOR: ClassVar[dict] = {"name": "fake-events", "version": "1"}

    def __init__(self, script: dict, clock, run_id: str):
        self.script = script
        self.clock = clock
        self.run_id = run_id
        self.streams: dict[str, dict] = {}
        self.opened = 0

    def open(self, episode_id: str, role: str) -> str:
        sid = f"s{self.opened}"
        self.opened += 1
        items = self.script.get(episode_id, self.script.get(role, []))
        self.streams[sid] = {
            "episode_id": episode_id,
            "role": role,
            "items": sorted(items, key=lambda item: item["step"]),
            "next": 0,
            "seq": 0,
            "last_id": None,
        }
        return sid

    def close(self, stream_id: str) -> None:
        self.streams.pop(stream_id, None)

    def poll(
        self, stream_id: str, *, through_step: int, max_events: int
    ) -> list[bytes]:
        stream = self.streams.get(stream_id)
        if stream is None:
            return []
        out = []
        while (
            stream["next"] < len(stream["items"])
            and stream["items"][stream["next"]]["step"] <= through_step
            and len(out) < max_events
        ):
            item = stream["items"][stream["next"]]
            stream["next"] += 1
            out.append(self._message(stream, item))
        return out

    def _message(self, stream: dict, item: dict) -> bytes:
        fault = item.get("fault")
        if fault == "bad_json":
            return b'{"schema": "levi.aeri.event.v1", '
        if fault == "skip_seq":
            stream["seq"] += 1
        event_id = item.get("id") or f"{stream['episode_id']}:ev{stream['seq']}"
        if fault == "duplicate" and stream["last_id"]:
            event_id = stream["last_id"]
        episode = stream["episode_id"]
        if fault == "wrong_episode":
            run, role, number = aeri.episode_parts(episode)
            episode = f"{run}.{role}.{number + 1:04d}"
        retracts = item.get("retracts")
        message = {
            **_envelope("event", self.run_id),
            "event_id": event_id,
            "episode_id": episode,
            "episode_role": stream["role"],
            "seq": stream["seq"],
            "event_type": item["event_type"],
            "status": "retracted" if retracts else "candidate",
            "retracts": retracts,
            "priority": item.get("priority", "goal_candidate"),
            "actor_id": "arm_0",
            "step": item["step"],
            "window_steps": None,
            "observed_ns": self.clock.now(),
            "clock_domain": "robot:fr3-0"
            if fault == "other_clock"
            else self.clock.domain,
            "signal_refs": [],
            "evidence_refs": [],
            "salience": None,
            "detector": dict(self.DETECTOR),
        }
        stream["seq"] += 1
        stream["last_id"] = event_id
        return _encode(message, control_key=fault == "control_key")


# --- judgements and scenes ---------------------------------------------------------------


class _FakeProvider:
    """Answers in submission order from ``script`` (a list of response
    specs, or ``callable(request, number) -> spec``); ``default`` once the
    list runs out.

    A response spec: ``{"decision": ..., "unavailable": code,
    "retryable": bool, "delay_ns", "valid_ms", "expired": True,
    "future": True, "other_clock": True, "minor": 1, "control_key": True,
    "contradict": True, "bad_predicate": True, "episode_id", "request_id",
    "run_id", "target", "unknown_reason", "submit_unavailable": code,
    "observed_through_step", "observed_before_ns"}``; scenes also take
    ``"evidence": n`` (frame references) and ``"contract_id"``."""

    schema = ""

    def __init__(self, script, clock, run_id: str, *, default=None):
        self.script = script
        self.clock = clock
        self.run_id = run_id
        self.default = default or {"decision": "unknown"}
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []

    def _spec(self, request: dict, number: int) -> dict:
        if callable(self.script):
            return dict(self.script(request, number) or self.default)
        if number < len(self.script):
            return dict(self.script[number])
        return dict(self.default)

    def submit(self, request: dict) -> Ticket | bytes:
        number = len(self.submitted)
        self.submitted.append(dict(request))
        spec = self._spec(request, number)
        now = self.clock.now()
        if spec.get("submit_unavailable"):
            return self._unavailable(request, spec, now, spec["submit_unavailable"])
        return Ticket(
            request_id=request["request_id"],
            number=number,
            submitted_ns=now,
            ready_ns=now + int(spec.get("delay_ns", DEFAULT_DELAY_NS)),
            request=dict(request),
            response=spec,
        )

    def cancel(self, ticket: Ticket, reason: str) -> None:
        self.cancelled.append(ticket.request_id)

    def collect(self, ticket: Ticket, *, timeout_ns: int) -> bytes | None:
        if ticket.delivered:
            return None
        now = self.clock.now()
        if ticket.ready_ns > now + timeout_ns:
            if timeout_ns:
                self.clock.advance(timeout_ns)
            return None
        if ticket.ready_ns > now:
            self.clock.advance(ticket.ready_ns - now)
        ticket.delivered = True
        spec = ticket.response
        produced = self.clock.now()
        if spec.get("unavailable"):
            return self._unavailable(
                ticket.request, spec, produced, spec["unavailable"]
            )
        return self._answer(ticket, spec, produced)

    def _unavailable(self, request, spec, produced, code) -> bytes:
        retryable = bool(spec.get("retryable", code in ("gate_closed", "busy")))
        message = {
            **_envelope(self.schema, spec.get("run_id", self.run_id)),
            "kind": "unavailable",
            "request_id": spec.get("request_id", request["request_id"]),
            "code": code,
            "retryable": retryable,
            "retry_after_ms": None,
            "detail": "fake",
            "produced_ns": produced,
            "clock_domain": self.clock.domain,
        }
        return _encode(message)

    def _times(self, ticket, spec, produced):
        valid = int(spec.get("valid_ms", DEFAULT_VALID_MS)) * 1_000_000
        observed = ticket.submitted_ns - int(spec.get("observed_before_ns", 0))
        if spec.get("expired"):
            produced = observed
            valid = 1_000_000
        if spec.get("future"):
            produced = produced + 60_000_000_000
        return observed, produced, produced + valid

    def _common(self, ticket, spec, produced) -> dict:
        observed, produced, until = self._times(ticket, spec, produced)
        domain = "robot:fr3-0" if spec.get("other_clock") else self.clock.domain
        return {
            "observed": observed,
            "produced": produced,
            "until": until,
            "domain": domain,
        }


class FakeGoalVerifier(_FakeProvider):
    """Goal judgements on the online judgement's spec (``generic-final``:
    predicates ``object_state`` and ``stable``)."""

    schema = "judgement"

    def _answer(self, ticket: Ticket, spec: dict, produced: int) -> bytes:
        request = ticket.request
        decision = spec.get("decision", "unknown")
        values = {
            "confirmed": (True, True),
            "rejected": (False, True),
            "unknown": (None, None),
        }[decision]
        if spec.get("contradict"):
            values = (False, True) if decision == "confirmed" else (True, True)
        names = ["object_state", "stable"]
        if spec.get("bad_predicate"):
            names[1] = "gripper_ok"
        times = self._common(ticket, spec, produced)
        message = {
            **_envelope(
                "judgement", spec.get("run_id", self.run_id), spec.get("minor", 0)
            ),
            "kind": "judgement",
            "judgement_id": f"j-{ticket.request_id}",
            "request_id": spec.get("request_id", request["request_id"]),
            "episode_id": spec.get("episode_id", request["episode_id"]),
            "episode_role": request["episode_role"],
            "target": spec.get("target", request["target"]),
            "subgoal": None,
            "event_ids": list(request.get("event_ids", []))[:16],
            "decision": decision,
            "unknown_reason": (
                spec.get("unknown_reason", "insufficient_evidence")
                if decision == "unknown"
                else None
            ),
            "predicate_results": [
                {"name": name, "value": value, "required": True, "evidence_refs": []}
                for name, value in zip(names, values, strict=True)
            ],
            "vetoes": [],
            "observed_from_step": request["observed_from_step"],
            "observed_through_step": spec.get(
                "observed_through_step", request["observed_through_step"]
            ),
            "observed_through_ns": times["observed"],
            "produced_ns": times["produced"],
            "valid_until_ns": times["until"],
            "clock_domain": times["domain"],
            "spec_id": GOAL_SPEC[0],
            "spec_version": GOAL_SPEC[1],
            "provider": "fake",
            "model_name": None,
            "model_digest": None,
            "confidence_kind": "none",
            "calibrated_probability": None,
            "calibration_ref": None,
            "cost": {"elapsed_ms": int((produced - ticket.submitted_ns) // 1_000_000)},
            "legacy_c5": None,
        }
        return _encode(message, control_key=bool(spec.get("control_key")))


class FakeSceneAssessor(_FakeProvider):
    """Scene assessments against ``SCENE_CONTRACT`` (or the ``contract``
    and ``predicates`` given: the first predicate is the one a
    ``reset_required`` or ``unknown`` answer fails or leaves unread)."""

    schema = "scene"

    def __init__(
        self,
        script,
        clock,
        run_id: str,
        *,
        default=None,
        contract=SCENE_CONTRACT,
        predicates=SCENE_PREDICATES,
    ):
        super().__init__(
            script, clock, run_id, default=default or {"decision": "ready"}
        )
        self.contract = tuple(contract)
        self.predicates = tuple(predicates)

    def _answer(self, ticket: Ticket, spec: dict, produced: int) -> bytes:
        request = ticket.request
        decision = spec.get("decision", "ready")
        first = {"ready": True, "reset_required": False, "unknown": None}[decision]
        if spec.get("contradict"):
            first = False
        values = (first,) + (True,) * (len(self.predicates) - 1)
        results = [
            {"name": name, "value": value, "required": True, "evidence_refs": []}
            for name, value in zip(self.predicates, values, strict=True)
        ]
        times = self._common(ticket, spec, produced)
        message = {
            **_envelope("scene", spec.get("run_id", self.run_id), spec.get("minor", 0)),
            "kind": "scene",
            "assessment_id": f"a-{ticket.request_id}",
            "request_id": spec.get("request_id", request["request_id"]),
            "episode_id": spec.get("episode_id", request["episode_id"]),
            "target": spec.get("target", request["target"]),
            "decision": decision,
            "contract_id": spec.get("contract_id", self.contract[0]),
            "contract_version": self.contract[1],
            "predicate_results": results,
            "failed_predicates": [r["name"] for r in results if r["value"] is False],
            "unknown_predicates": [r["name"] for r in results if r["value"] is None],
            "unknown_reason": (
                spec.get("unknown_reason", "occluded")
                if decision == "unknown"
                else None
            ),
            # ``evidence``: how many frame references the assessment cites.
            "evidence_refs": [
                {"kind": "frame", "ref": f"side:{i}", "sha256": None, "step": None}
                for i in range(int(spec.get("evidence", 0)))
            ],
            "observed_ns": times["observed"],
            "produced_ns": times["produced"],
            "valid_until_ns": times["until"],
            "clock_domain": times["domain"],
            "provider": "fake",
            "model_name": None,
            "model_digest": None,
            "confidence_kind": "none",
            "calibrated_probability": None,
            "calibration_ref": None,
            "cost": None,
        }
        return _encode(message, control_key=bool(spec.get("control_key")))


ScriptFn = Callable[[dict, int], dict | None]
