"""The termination arbiter (T-C-07): when may an episode stop early?

Four stages, never skipped: a **candidate** (an event says the goal may be
reached) -> **evidence** (C asks for a judgement over a window that covers
the candidate and a settling time after it) -> **confirmation** (a fresh
``confirmed`` judgement, for this run, episode, target and request, not
overtaken by a retraction; ``confirmations`` of them in a row) -> a **stop
request**, which the orchestrator turns into a controlled stop.

What never stops an episode: ``unknown``, ``unavailable``, a timeout, a
judgement for another episode or run, an unsolicited or repeated answer,
one that has expired or comes from another clock domain, one whose evidence
ends before the candidate, a contract violation. Each is dropped with a
note for the journal and the episode goes on (``on_unknown:
continue_to_horizon``, the only v1 policy).

Jittery events are merged into one candidate (the latest step), requests
are spaced by ``cooldown_steps`` and capped at ``max_requests`` per
episode, so a flapping detector cannot flood the judge. A share of episodes
(``control_fraction``, chosen deterministically from the episode id) runs
without early stops, so false early stops can be measured against episodes
that ran to the horizon.

Pure logic: no I/O, no clock of its own (the caller passes its clock to
``on_result``), no threads.
"""

import hashlib
from dataclasses import dataclass, field

from levi.domain import aeri

ON_UNKNOWN = ("continue_to_horizon",)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TerminationConfig:
    allow_early_stop: bool = True
    # No request before this step (the policy needs time to act at all).
    min_steps: int = 10
    # The evidence must reach at least this many steps past the candidate.
    settle_steps: int = 8
    # At least this many steps between two requests.
    cooldown_steps: int = 15
    max_requests: int = 6
    # Consecutive confirmed judgements needed for a stop.
    confirmations: int = 1
    on_unknown: str = "continue_to_horizon"
    # Share of episodes run with early stops disabled (the control group).
    control_fraction: float = 0.0
    control_seed: str = "aeri-control"
    # Event types that make a goal candidate (besides priority goal_candidate).
    goal_event_types: tuple = ("object_settled",)
    # Contract violations by the judge in one episode before it is ignored.
    violation_limit: int = 3

    def __post_init__(self):
        ints = (
            "min_steps",
            "settle_steps",
            "cooldown_steps",
            "max_requests",
            "confirmations",
            "violation_limit",
        )
        for name in ints:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ConfigError(f"{name} must be a whole number >= 0")
        if self.confirmations < 1:
            raise ConfigError("confirmations must be at least 1")
        if self.on_unknown not in ON_UNKNOWN:
            raise ConfigError(
                f"on_unknown {self.on_unknown!r}: v1 supports only continue_to_horizon"
            )
        if not 0.0 <= float(self.control_fraction) <= 1.0:
            raise ConfigError("control_fraction is between 0 and 1")
        if type(self.allow_early_stop) is not bool:
            raise ConfigError("allow_early_stop is true or false")

    def is_control(self, episode_id: str) -> bool:
        """Deterministic: the same episode id is always in the same group."""
        if self.control_fraction <= 0:
            return False
        digest = hashlib.sha256(f"{self.control_seed}:{episode_id}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64 < self.control_fraction


@dataclass
class Note:
    code: str
    detail: str = ""


@dataclass
class Verdict:
    stop: bool = False
    reason: str | None = None
    judgement_id: str | None = None
    notes: list = field(default_factory=list)


@dataclass
class Candidate:
    event_id: str
    step: int


class TerminationArbiter:
    """One episode's arbiter."""

    def __init__(
        self,
        config: TerminationConfig,
        *,
        run_id: str,
        episode_id: str,
        role: str,
        target: str,
    ):
        self.config = config
        self.run_id = run_id
        self.episode_id = episode_id
        self.role = role
        self.target = target
        self.control = config.is_control(episode_id)
        self.stage = "idle"  # idle | candidate | evidence | stop_requested
        self.candidate: Candidate | None = None
        self.outstanding: dict[str, Candidate] = {}  # request id -> candidate
        self.answered: set = set()
        self.requests = 0
        self.last_request_step: int | None = None
        self.consecutive = 0
        self.seen_events: set = set()
        self.retracted: set = set()
        self.last_seq: dict = {}
        self.violations = 0
        self.event_violations = 0
        self.unknowns = 0
        self.unavailables = 0
        self.confirmed_ids: list = []
        self.judgement_ids: list = []
        self.stopped: Verdict | None = None

    @property
    def disabled(self) -> bool:
        """The judge broke the contract too often: ignored for the episode."""
        return self.violations >= self.config.violation_limit

    @property
    def events_disabled(self) -> bool:
        """The event provider broke the contract too often: its events are
        ignored for the episode (and the run waits for a person after it)."""
        return self.event_violations >= self.config.violation_limit

    # --- events --------------------------------------------------------------------

    def on_event(self, raw: bytes) -> list:
        """Read one event message; notes for the journal."""
        try:
            event = aeri.parse(raw, "event")
        except aeri.AeriError as exc:
            self.event_violations += 1
            return [Note("contract_violation", f"event: {exc}"[:300])]
        notes = []
        if event.episode_id != self.episode_id:
            return [
                Note("event_dropped_stale", f"{event.event_id} of {event.episode_id}")
            ]
        if event.event_id in self.seen_events:
            return [Note("event_dropped_duplicate", event.event_id)]
        self.seen_events.add(event.event_id)
        key = (event.detector.name, event.detector.version)
        last = self.last_seq.get(key)
        if last is not None and event.seq != last + 1:
            notes.append(Note("event_gap", f"seq {last} -> {event.seq}"))
        if last is None or event.seq > last:
            self.last_seq[key] = event.seq
        if event.status == "retracted":
            self.retracted.add(event.retracts)
            if self.candidate and self.candidate.event_id == event.retracts:
                self.candidate = None
                self.consecutive = 0
                self.stage = "idle" if not self.outstanding else self.stage
                notes.append(Note("candidate_retracted", event.retracts))
            return notes
        goal = (
            event.priority == "goal_candidate"
            or event.event_type in self.config.goal_event_types
        )
        if goal and self.stage != "stop_requested":
            # Merge: a newer candidate replaces the older one; an older event
            # arriving late never moves the candidate back (it would shorten
            # the settling window).
            if self.candidate is not None and event.step < self.candidate.step:
                return notes
            self.candidate = Candidate(event.event_id, event.step)
            if self.stage == "idle":
                self.stage = "candidate"
        return notes

    # --- requests ------------------------------------------------------------------

    def want_request(self, step: int) -> dict | None:
        """The window to ask about now, or None. The caller issues the
        request (with ``request_id``) and calls ``issued``."""
        c = self.config
        if self.disabled or self.stage == "stop_requested" or self.candidate is None:
            return None
        if self.outstanding or self.requests >= c.max_requests or step < c.min_steps:
            return None
        if (
            self.last_request_step is not None
            and step - self.last_request_step < c.cooldown_steps
        ):
            return None
        if step < self.candidate.step + c.settle_steps:
            return None
        return {
            "request_id": f"{self.episode_id}:q{self.requests}",
            "observed_from_step": self.candidate.step,
            "observed_through_step": step,
            "event_ids": [self.candidate.event_id],
        }

    def issued(self, request_id: str, step: int) -> None:
        self.outstanding[request_id] = self.candidate
        self.requests += 1
        self.last_request_step = step
        self.stage = "evidence"

    def withdraw(self, request_id: str) -> None:
        """A request that will not be answered (unavailable at submit, a
        timeout, the episode ended)."""
        self.outstanding.pop(request_id, None)
        self.answered.add(request_id)
        if not self.outstanding and self.stage == "evidence":
            self.stage = "candidate" if self.candidate else "idle"

    # --- results -------------------------------------------------------------------

    def accept(
        self, raw: bytes, *, now_ns: int, clock_domain: str, specs=None, expected=None
    ):
        """The judgement in ``raw`` if it may be used now, else a ``Note``.
        Fences: contract, run, episode, request (issued, unanswered),
        target, clock domain and freshness. ``expected``: the request whose
        ticket produced ``raw``; when the answer is dropped, that request
        is withdrawn (its ticket will not answer again), so a malformed or
        foreign answer cannot leave the arbiter waiting for ever."""
        found = self._accept(raw, now_ns=now_ns, clock_domain=clock_domain, specs=specs)
        if isinstance(found, Note) and expected is not None:
            self.withdraw(expected)
        elif expected is not None and found.request_id != expected:
            self.withdraw(expected)
            return Note(
                "judgement_dropped_unsolicited", f"{found.request_id} for {expected}"
            )
        return found

    def _accept(self, raw: bytes, *, now_ns: int, clock_domain: str, specs=None):
        try:
            message = aeri.parse(raw, "judgement", specs=specs)
        except aeri.AeriError as exc:
            self.violations += 1
            return Note("contract_violation", f"judgement: {exc}"[:300])
        if message.run_id != self.run_id:
            return Note("judgement_dropped_stale", f"run {message.run_id}")
        request = message.request_id
        if request in self.answered or request not in self.outstanding:
            return Note("judgement_dropped_unsolicited", request)
        if message.kind == "unavailable":
            self.withdraw(request)
            self.unavailables += 1
            return Note("judgement_unavailable", f"{request}: {message.code}")
        if message.episode_id != self.episode_id:
            return Note("judgement_dropped_stale", f"{request} of {message.episode_id}")
        if message.target != self.target:
            return Note("judgement_dropped_stale", f"{request} judges {message.target}")
        try:
            aeri.check_fresh(message, now_ns=now_ns, local=clock_domain)
        except aeri.AeriError as exc:
            self.withdraw(request)
            return Note("judgement_dropped_stale", f"{request}: {exc.code}")
        return message

    def on_result(
        self, raw: bytes, *, now_ns: int, clock_domain: str, specs=None, expected=None
    ) -> Verdict:
        verdict = Verdict()
        found = self.accept(
            raw,
            now_ns=now_ns,
            clock_domain=clock_domain,
            specs=specs,
            expected=expected,
        )
        if isinstance(found, Note):
            verdict.notes.append(found)
            return verdict
        message = found
        request = message.request_id
        candidate = self.outstanding.get(request)
        self.withdraw(request)
        self.judgement_ids.append(message.judgement_id)
        if message.decision == "unknown":
            self.unknowns += 1
            self.consecutive = 0
            verdict.notes.append(
                Note("judgement_unknown", f"{request}: {message.unknown_reason}")
            )
            return verdict
        if message.decision == "rejected":
            self.consecutive = 0
            return verdict
        # confirmed: does it still stand for the current candidate?
        if candidate is None or candidate.event_id in self.retracted:
            verdict.notes.append(Note("judgement_dropped_retracted", request))
            return verdict
        if (
            message.observed_from_step > candidate.step
            or message.observed_through_step < candidate.step + self.config.settle_steps
        ):
            verdict.notes.append(
                Note("judgement_dropped_stale", f"{request}: before settling")
            )
            return verdict
        self.consecutive += 1
        self.confirmed_ids.append(message.judgement_id)
        if self.consecutive < self.config.confirmations:
            return verdict
        if self.control or not self.config.allow_early_stop:
            verdict.notes.append(
                Note(
                    "early_stop_withheld",
                    "control episode" if self.control else "disabled",
                )
            )
            return verdict
        self.stage = "stop_requested"
        verdict.stop = True
        verdict.reason = "goal_verified"
        verdict.judgement_id = message.judgement_id
        self.stopped = verdict
        return verdict


GOAL_VERIFICATION = {
    "confirmed": "verified",
    "rejected": "contradicted",
    "unknown": "undecided",
}


def verification_of(message) -> str:
    """``goal_verification`` of a final judgement (None or unavailable:
    ``unavailable``). Never ``verified`` unless it was confirmed."""
    if message is None or getattr(message, "kind", None) != "judgement":
        return "unavailable"
    return GOAL_VERIFICATION[message.decision]
