"""What terminal D is doing and which keys the page may press for it (pure).

``derive`` reads **facts** (D's journal, its screen, its heartbeat, the pty's
modes, the process, what this page already sent) and answers in one of the
states of ``STATES``; ``offer`` lists the keys that state allows and why not;
``document`` builds the ``levi.marvel0.aeri_run.v1`` answer. Nothing here reads
a file, a process or a clock: ``levi/marvel0/aeri_run.py`` reads them and tests
write the facts by hand.

**A key is pressed in terminal D by this page only when three sources agree**:
the journal says D waits for that kind of answer, the screen shows that prompt
as D's last line, and the pty is in the mode that kind needs (single keys: not
canonical; the phrase: canonical), and they have agreed for ``SETTLE_S``. Any
disagreement, a screen sentence the page does not know (D changed its words), a
heartbeat that has stopped while D lives, a termios that cannot be read: the
state is ``unknown``, the only key is Stop. The page never guesses.

**The keys are a closed table** (``KEYS``): the bytes come from here, never from
a request. Never offered: ``\\x04`` (D reads it as ``q``), ``\\x1a``, ``\\x1c``,
Escape, digits, capital letters, any text but ``execute <name>``, Enter at the
object list, ``y`` at the first step of a wait, ``q`` or ``y`` while a round runs,
``s``/``f``/``d`` anywhere but the forward loop (and only once per round),
``m`` (a request for a human reset: not in v1).

**Honest wording.** A click is a key of terminal D. Nothing here is an
emergency stop. The gates stop mistakes, not another program of this account;
the barriers are Franka Desk and the physical emergency stop.
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from pathlib import Path

from . import aeri_screen
from .aeri_journal import JView, RView, SView

SCHEMA = "levi.marvel0.aeri_run.v1"
RESULT_SCHEMA = "levi.marvel0.aeri_run_result.v1"
LOG_SCHEMA = "levi.marvel0.aeri_run_log.v1"
RECORD_SCHEMA = "levi.marvel0.aeri_record.v1"

# --- constants (guesses until the first real use; one place, nobody restates them) -------
SETTLE_S = 1.0  # sources must agree this long before a key is offered
ARM_DELAY_S = 1.5  # a key that appears is greyed this long (both ends)
KEY_GAP_S = 3.0  # between one key being taken and the next being allowed
STOP_ACK_S = 5.0  # D must acknowledge a Ctrl+C within this
RUNNER_STALE_S = 10.0  # the heartbeat of a live D older than this: unknown
STALL_S = 150.0  # a winding-down D without progress this long: stalled
STALL_QUEUE_S = 600.0  # the same while an external answer is awaited
PRESENCE_S = 15.0  # a tab must have reported this recently to press a B-level key
START_TIMEOUT_S = 300.0  # a launch that shows nothing for this long
KEY_NOT_TAKEN_S = 5.0  # a key must be seen taken within this
DRIFT_S = 20.0  # the journal expects a prompt the screen does not show
CHALLENGE_TTL_S = 30.0
HISTORY_KEPT = 20
SCREEN_TAIL = 60

STATES = (
    "idle",
    "launching",
    "await_phrase",
    "await_phrase_variant",
    "services",
    "await_home_o",
    "await_home_p",
    "await_home_h",
    "await_inventory",
    "await_release",
    "forward_running",
    "closing",
    "reverse_running",
    "await_resume",
    "await_resume_confirm",
    "busy",
    "stopping",
    "stalled",
    "fault_locked",
    "orphaned",
    "unknown",
    "ended",
)
ACTIONS = (
    "execute",
    "execute_variant",
    "inventory_yes",
    "inventory_no",
    "home_o",
    "home_p",
    "home_h",
    "release_round",
    "verdict_success",
    "verdict_failure",
    "verdict_discard",
    "resume_ask",
    "resume_confirm",
    "resume_cancel",
    "quit",
)
LEVELS = ("A", "B", "C")
MOTIONS = ("none", "may_move", "arm_moves")
SLOTS = ("first", "second")
VARIANTS = ("confirm_reason", "direct")
STOP_PHASES = ("idle", "sent", "acked", "unconfirmed", "retried")
WHY_NOT = (
    "state_not_allowed",
    "not_settled",
    "key_sent",
    "key_unconfirmed",
    "verdict_once",
    "monitor_not_green",
    "swap_unclean",
    "screen_drift",
    "stop_spent",
    "presence_stale",
    "end_not_allowed",
    "d_gone",
    "aeri_disabled",
    "d_active",
    "readiness_failed",
    "driven_elsewhere",
)
NOTICES = (
    "not_estop",
    "inbox_resume",
    "stop_unconfirmed",
    "presence_lost",
    "runner_stale",
    "screen_drift",
    "key_unconfirmed",
)
REASONS = (
    "no_run",
    "starting",
    "start_timeout",
    "prompt_open",
    "service_step",
    "policy_warmup",
    "forward_active",
    "forward_judging",
    "forward_stopping",
    "robot_home",
    "startup_home",
    "reset_active",
    "reset_judging",
    "planning",
    "waiting_agent",
    "awaiting_prompt",
    "stop_sent",
    "completing",
    "no_progress",
    "fault_locked",
    "adopted_after_restart",
    "screen_drift",
    "runner_stale",
    "source_conflict",
    "termios_mismatch",
    "termios_unreadable",
    "inventory_mismatch",
    "no_rule",
    "no_keyboard",
    "profile_missing",
    "exit_0",
    "exit_1",
    "exit_2",
    "exit_130",
    "exit_signal",
    "exit_other",
    "exit_unknown",
)
PROMPT_KINDS = (
    "phrase",
    "phrase_variant",
    "home_o",
    "home_p",
    "home_h",
    "inventory",
    "release",
    "round",
    "resume",
    "confirm_resume",
)
# The single states of D whose end is not a stop of ours: a hang-up chain
# (SIGINT, then SIGKILL after the grace) ends D with 137.
EXITS = {0: "exit_0", 1: "exit_1", 2: "exit_2", 130: "exit_130"}


# --- refusals ------------------------------------------------------------------------------

# code -> (HTTP status, English message). The same names the hosted client's
# page uses, plus those of terminal D.
REFUSALS = {
    "request_id_invalid": (400, "request_id is 8-80 characters of A-Z a-z 0-9 . _ : -"),
    "request_id_used": (409, "This request_id was used for a different request"),
    "challenge_invalid": (400, "Ask for a new confirmation token"),
    "challenge_expired": (410, "The confirmation token expired"),
    "challenge_used": (409, "The confirmation token was already used"),
    "challenge_stale": (
        409,
        "The run or the readiness changed since the token was issued",
    ),
    "confirm_required": (400, "Type the phrase shown"),
    "confirm_mismatch": (400, "The phrase does not match"),
    "attest_required": (400, "Tick every item of the checklist"),
    "busy": (423, "Another action on the run is going on"),
    "not_product": (403, "Only the product LEVI may do this"),
    "aeri_disabled": (403, "AERI hosting is off in the control file"),
    "profile_unknown": (412, "The pinned AERI code has no known prompt profile"),
    "code_pin_mismatch": (
        412,
        "The AERI worktree is not at the pinned commit or is dirty",
    ),
    "templates_changed": (412, "A template does not match SOURCES.sha256"),
    "run_name_taken": (409, "The run name exists in a registry"),
    "check_failed": (422, "aeri.py check refused"),
    "not_prepared": (412, "No prepared run of that name"),
    "prepared_changed": (
        409,
        "The job, binding or RUN.json changed since it was prepared",
    ),
    "readiness_failed": (409, "A readiness row refuses (unknown refuses too)"),
    "d_active": (409, "A hosted run is live"),
    "driven_elsewhere": (409, "Another terminal drives this run name"),
    "live_service_running": (409, "The live service holds :8100"),
    "stale_page": (409, "The page's view is not the current prompt"),
    "state_not_allowed": (409, "This key is not offered now"),
    "key_sent": (409, "A key was already sent for this prompt"),
    "key_unconfirmed": (409, "The last key was not seen taken; nothing is resent"),
    "verdict_once": (409, "One verdict per round"),
    "monitor_not_green": (409, "The health monitor is not fresh and green"),
    "swap_unclean": (409, "D asks for the policy-switch phrase: use the variant key"),
    "screen_drift": (409, "The terminal's wording is not the one this page knows"),
    "stop_spent": (409, "Stop was already sent for this run"),
    "key_too_soon": (429, "Too soon after the button appeared or the last key"),
    "presence_stale": (409, "This tab has not reported for 15 s"),
    "end_not_allowed": (409, "D can still wrap up"),
    "not_settled": (409, "The sources have not agreed for a second yet"),
    "d_gone": (409, "Terminal D is not running"),
    "start_failed": (500, "The launch failed"),
    "key_failed": (500, "The key could not be written"),
    "signal_failed": (500, "The signal could not be sent"),
}


class Refuse(Exception):
    """A request that does not go on: stable ``code``, HTTP ``status``, English
    ``message`` and the facts as ``extra``."""

    def __init__(self, code, **extra):
        self.status, text = REFUSALS[code]
        self.code = code
        self.extra = extra
        try:
            self.message = text.format(**extra)
        except (KeyError, IndexError):
            self.message = text
        super().__init__(f"{code}: {self.message}")


# --- profiles ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """One AERI code version whose words and flow the page knows."""

    id: str
    commit: str
    title: str
    start_home: bool
    progress_line: bool
    camera_stale: bool
    data: dict = field(repr=False, compare=False, default_factory=dict)

    @property
    def sources_sha256(self) -> dict:
        return self.data["sources_sha256"]

    @property
    def confirm_reasons(self) -> frozenset:
        return frozenset(self.data["confirm_reasons"])

    @property
    def no_resume_reasons(self) -> frozenset:
        return frozenset(self.data["no_resume_reasons"])


def _profiles() -> dict:
    data = aeri_screen.prompts()
    out = {}
    for pid, p in data["profiles"].items():
        caps = p["capabilities"]
        out[pid] = Profile(
            id=pid,
            commit=p["commit"],
            title=p["title"],
            start_home=bool(caps["start_home"]),
            progress_line=bool(caps["progress_line"]),
            camera_stale=bool(caps["camera_stale"]),
            data=p,
        )
    return out


PROFILES: dict = _profiles()
DEFAULT_PROFILE: str = aeri_screen.prompts()["default"]


def profile_known(profile_id) -> bool:
    return isinstance(profile_id, str) and profile_id in PROFILES


def profile_matches(worktree, profile_id) -> tuple:
    """``(ok, detail)``: whether the four source files of the AERI worktree are
    byte for byte the ones the profile was written from. A version the page has
    not read is refused, not guessed."""
    from .aeri_journal import open_regular

    profile = PROFILES.get(profile_id)
    if profile is None:
        return False, "unknown_profile"
    for rel, want in sorted(profile.sources_sha256.items()):
        fd = open_regular(Path(worktree) / rel)
        if fd is None:
            return False, f"unreadable:{rel}"
        try:
            import os

            chunks = []
            while True:
                part = os.read(fd, 1 << 20)
                if not part:
                    break
                chunks.append(part)
        except OSError:
            return False, f"unreadable:{rel}"
        finally:
            os.close(fd)
        if hashlib.sha256(b"".join(chunks)).hexdigest() != want:
            return False, f"differs:{rel}"
    return True, "ok"


def profile_head_ok(profile_id, head) -> bool:
    """Whether ``head`` (the full commit id git prints for the AERI worktree) is
    the commit the profile was written from. With a clean tree this pins every
    file of the worktree, the ones that write the journal included."""
    profile = PROFILES.get(profile_id)
    return (
        profile is not None
        and isinstance(head, str)
        and len(profile.commit) >= 7
        and head.lower().startswith(profile.commit.lower())
    )


# --- the key table -----------------------------------------------------------------------


@dataclass(frozen=True)
class KeySpec:
    id: str
    label: str  # how the document names the byte
    wire: bytes | None  # None: the phrase line of the run
    level: str  # A one click; B + delay, gap, presence; C + the typed phrase
    motion: str
    slot: str
    kinds: tuple  # the prompt kinds it answers


ENTER = b"\r"
KEYS = {
    k.id: k
    for k in (
        KeySpec("execute", "execute", None, "C", "may_move", "first", ("phrase",)),
        KeySpec(
            "execute_variant",
            "execute",
            None,
            "C",
            "may_move",
            "first",
            ("phrase_variant",),
        ),
        KeySpec("inventory_yes", "y", b"y", "B", "none", "first", ("inventory",)),
        KeySpec("inventory_no", "n", b"n", "A", "none", "first", ("inventory",)),
        KeySpec("home_o", "o", b"o", "B", "none", "first", ("home_o",)),
        KeySpec("home_p", "p", b"p", "B", "none", "second", ("home_p",)),
        KeySpec("home_h", "h", b"h", "B", "arm_moves", "second", ("home_h",)),
        KeySpec(
            "release_round", "Enter", ENTER, "B", "arm_moves", "first", ("release",)
        ),
        KeySpec("verdict_success", "s", b"s", "B", "arm_moves", "second", ("round",)),
        KeySpec("verdict_failure", "f", b"f", "B", "arm_moves", "second", ("round",)),
        KeySpec("verdict_discard", "d", b"d", "B", "arm_moves", "second", ("round",)),
        KeySpec("resume_ask", "Enter", ENTER, "B", "may_move", "first", ("resume",)),
        KeySpec(
            "resume_confirm", "y", b"y", "B", "may_move", "second", ("confirm_resume",)
        ),
        KeySpec("resume_cancel", "n", b"n", "A", "none", "second", ("confirm_resume",)),
        KeySpec(
            "quit",
            "q",
            b"q",
            "A",
            "none",
            "first",
            (
                "inventory",
                "home_o",
                "home_p",
                "home_h",
                "release",
                "resume",
                "confirm_resume",
            ),
        ),
    )
}
# Ctrl+C: the one byte that is not a key of D's prompts. Written to the pty with
# ISIG on (the tty turns it into SIGINT for D's foreground group).
STOP_BYTE = b"\x03"
# Every byte the page can ever write (a test pins this set).
ALL_BYTES = frozenset(
    {k.wire for k in KEYS.values() if k.wire is not None} | {STOP_BYTE}
)
VERDICTS = ("verdict_success", "verdict_failure", "verdict_discard")
START_PHRASE = "start aeri {name}"
END_PHRASE = "end terminal D"
EXECUTE_PHRASE = "execute {name}"
VARIANT_SUFFIX = " after policy switch check"
ATTEST = ("estop", "scene_ok", "area_clear", "arm_home")


def phrase_for(action: str, name: str) -> str:
    base = EXECUTE_PHRASE.format(name=name)
    return base + VARIANT_SUFFIX if action == "execute_variant" else base


def token_for(secret: bytes, run_id, seq, prompt_id, action) -> str:
    """The one-time token of a key for what the page saw: the same value on
    every poll while the state is the same, none that fits a later state."""
    msg = "\0".join([str(run_id), str(seq), str(prompt_id), action]).encode()
    return "t-" + hmac.new(secret, msg, hashlib.sha256).hexdigest()[:16]


# --- facts and the answer -----------------------------------------------------------------


@dataclass
class Memory:
    """What the next look needs of this one (the caller keeps it)."""

    sig: tuple | None = None
    sig_since: float = 0.0
    expect: str | None = None
    expect_since: float = 0.0
    mark: tuple | None = None
    mark_since: float = 0.0
    adopted_at: float | None = None


@dataclass
class Facts:
    now: float
    record: dict | None = None
    profile: Profile | None = None
    alive: bool = False  # the launcher is our child / matches the recorded identity
    exit: dict | None = None  # {"code", "signal"} once it is gone
    keyboard: bool = False  # this process holds the pty master
    adopted: bool = False  # found again after a restart: no keyboard
    foreign: bool = False  # another live core owns this run: only watched
    j: JView = field(default_factory=JView)
    screen: aeri_screen.ScreenView = field(default_factory=aeri_screen.ScreenView)
    runner: RView = field(default_factory=RView)
    runner_age_s: float | None = None
    runner_seen: bool = False  # the heartbeat has been seen in this run
    steps: SView = field(default_factory=SView)
    icanon: bool | None = None
    isig: bool | None = None
    queue_pending: int = 0
    queue_oldest_s: float | None = None
    stop_written: str = "idle"  # idle | sent | retried
    stop_age_s: float | None = None
    started_age_s: float | None = None
    external: bool = False
    verdict_round_id: str | None = None  # the round a verdict key was sent in


@dataclass(frozen=True)
class Derived:
    """The answer of ``derive``."""

    state: str
    reason: str
    params: dict
    may_move: bool
    prompt: dict | None  # {id, kind, n, total, line}
    kind: str | None  # the prompt kind the keys answer
    agree: dict
    wait: dict | None
    inventory: dict | None
    progress: dict
    settled: bool
    since: float  # when the sources began to agree on this
    variant: str | None  # resume_ask: confirm_reason | direct
    stop_phase: str
    stop_open: bool
    winding: bool
    stalled: bool
    alive: bool
    round_id: str | None
    mem: Memory


@dataclass
class _V:
    """A verdict before the answer is assembled."""

    state: str
    reason: str
    params: dict = field(default_factory=dict)
    may_move: bool = False
    seen: aeri_screen.Seen | None = None
    kind: str | None = None
    wait: dict | None = None
    variant: str | None = None
    winding: bool = False
    stalled: bool = False
    termios: str | None = None  # required: "cbreak" | "canonical"


# --- derive --------------------------------------------------------------------------------


def expected_prompt(f: Facts) -> str | None:
    """The kind of answer the journal says D waits for (``None``: none).
    ``home`` is one of the three startup-home keys, told apart by the screen."""
    j = f.j
    if j.last_commit is None:
        if j.startup_prompt_seq >= 0 and j.startup_requested_seq < 0:
            return "home"
        return None
    last = j.last_commit.seq
    if j.to_state == "WAIT_HUMAN":
        return "resume"
    if j.to_state in ("VERIFY_INITIAL", "SCENE_ASSESS"):
        if j.inventory_pending:
            return "inventory"
        # The release prompt of a round is asked only where D's loop is idle:
        # at VERIFY_INITIAL (``preflight_passed`` before the first round,
        # ``subtask_reverse_done`` after each one). SCENE_ASSESS is where the
        # forward loop ends (``subtask_forward_done``) and where the reverse loop
        # re-plans (``subtask_continue``): D asks for nothing there and the VLM
        # may take minutes (up to 540 s with an external answer), so expecting a
        # prompt there turned every reverse loop into ``unknown`` (r08 seq 49
        # and 71). Not "released <= reverse rounds done" either: a round that a
        # resume abandoned never completes its reverse loop, and that count
        # would hide every later prompt.
        if (
            j.to_state == "VERIFY_INITIAL"
            and j.inventory_confirmed
            and not j.released_after(last)
            and j.pose_refused_seq < last
            and j.open_transaction is None
        ):
            return "release"
        return None
    if j.to_state == "FORWARD_ACTIVE" and j.released:
        return "round"
    return None


def _mark(f: Facts) -> tuple:
    blank = f.screen.counts.get("blank", 0)
    return (f.j.seq, f.steps.steps, f.screen.lines - blank)


def _waiting_agent(f: Facts) -> bool:
    return bool(
        f.external
        and f.queue_pending
        and f.queue_oldest_s is not None
        and f.queue_oldest_s < STALL_QUEUE_S
    )


def exit_reason(ex) -> str:
    if not isinstance(ex, dict):
        return "exit_unknown"
    if ex.get("signal"):
        return "exit_signal"
    code = ex.get("code")
    if code is None:
        return "exit_unknown"
    return EXITS.get(code, "exit_other")


def interrupted(f: Facts) -> bool:
    """D says it was interrupted (its ``interrupt`` line on the screen or its
    ``operator_interrupt`` note), **whoever sent the Ctrl+C**: this page, the
    out-of-page script, a terminal or ``kill``."""
    return bool(f.screen.interrupt or f.j.interrupt_seq >= 0)


def stop_phase(f: Facts) -> str:
    """idle | sent | acked | unconfirmed | retried, from what was written and
    what D said back. D's acknowledgement counts whoever sent the signal: a Stop
    sent from elsewhere that D answered is ``acked`` here too, so this page
    never offers a second Ctrl+C into a wrap-up that has begun."""
    if interrupted(f):
        return "acked"
    if f.stop_written == "idle":
        return "idle"
    if f.stop_written == "retried":
        return "retried"
    if f.runner_seen and f.stop_age_s is not None and f.stop_age_s > STOP_ACK_S:
        return "unconfirmed"
    return "sent"


def derive(f: Facts, mem: Memory) -> Derived:
    """The state of the run, from ``f``. ``mem`` is what the previous look kept;
    the answer carries the next one."""
    now = f.now
    mark = _mark(f)
    mark_since = mem.mark_since if mem.mark == mark else now
    expect = expected_prompt(f)
    expect_since = mem.expect_since if mem.expect == expect else now
    nxt = Memory(
        sig=mem.sig,
        sig_since=mem.sig_since,
        expect=expect,
        expect_since=expect_since,
        mark=mark,
        mark_since=mark_since,
        adopted_at=mem.adopted_at,
    )
    verdict = _decide(f, expect, now - expect_since, now - mark_since)
    return _assemble(f, mem, nxt, verdict, expect)


def _decide(f: Facts, expect, expect_age: float, progress_age: float) -> _V:
    j, screen, rec = f.j, f.screen, f.record or {}
    if not rec or rec.get("phase") not in ("intent", "started", "ended"):
        return _V("idle", "no_run")
    if rec.get("phase") == "ended" or not f.alive:
        return _V("ended", exit_reason(f.exit or rec.get("exit")))
    if f.profile is None:
        return _V("unknown", "profile_missing", may_move=True)
    if f.foreign:
        # Another live core owns this run: this one only watches (no key, no
        # signal). The owner's page is where the run is driven.
        return _V("unknown", "no_keyboard", may_move=True)
    acked = interrupted(f)
    sent = f.stop_written != "idle"
    winding = sent or acked or j.to_state in ("FAULT_LOCKED", "COMPLETED")
    # A Ctrl+C that D has not answered yet: the arm may still be moving.
    unacked = sent and not acked
    limit = STALL_QUEUE_S if _waiting_agent(f) else STALL_S
    if winding and progress_age >= limit:
        return _V(
            "stalled",
            "no_progress",
            {"since_progress_s": round(progress_age, 1)},
            may_move=unacked,
            winding=True,
            stalled=True,
        )
    if j.to_state == "FAULT_LOCKED":
        code = screen.fault or j.reason
        return _V("fault_locked", "fault_locked", {"code": code}, winding=True)
    if f.adopted:
        return _V(
            "orphaned", "adopted_after_restart", may_move=unacked, winding=winding
        )
    if winding:
        since = round(f.stop_age_s, 1) if f.stop_age_s is not None else None
        reason = "completing" if j.to_state == "COMPLETED" else "stop_sent"
        return _V(
            "stopping",
            reason,
            {} if since is None else {"since_stop_s": since},
            may_move=unacked,
            winding=True,
        )
    if f.runner_seen and f.runner_age_s is not None and f.runner_age_s > RUNNER_STALE_S:
        return _V(
            "unknown",
            "runner_stale",
            {"age_s": round(f.runner_age_s, 1)},
            may_move=True,
        )
    if not f.keyboard:
        return _V("unknown", "no_keyboard", may_move=True)
    if f.icanon is None:
        return _V("unknown", "termios_unreadable", may_move=True)
    tx = j.open_transaction
    if tx is not None and tx.reason == "startup_home_requested":
        # the first home, authorised by o, p, h, is under way: the arm moves
        return _V("closing", "startup_home", may_move=True)
    if j.last_commit is None and expect is None:
        return _before_the_journal(f)
    if expect is not None:
        return _expected(f, expect, expect_age)
    return _no_prompt(f)


def _before_the_journal(f: Facts) -> _V:
    """No line committed and no key asked for: launching, the phrase, the
    services."""
    screen, j = f.screen, f.j
    open_ = screen.open
    if (
        open_ is not None
        and open_.kind in ("phrase", "phrase_variant")
        and j.lines == 0
    ):
        state = (
            "await_phrase_variant" if open_.kind == "phrase_variant" else "await_phrase"
        )
        name = (f.record or {}).get("name") or ""
        phrase = open_.group("phrase") or ""
        want = phrase_for(
            "execute_variant" if open_.kind == "phrase_variant" else "execute", name
        )
        if phrase != want:
            return _V("unknown", "source_conflict", may_move=True)
        return _V(
            state, "prompt_open", seen=open_, kind=open_.kind, termios="canonical"
        )
    if f.steps.steps or j.lines or screen.counts.get("svc"):
        reason = "policy_warmup" if "policy_warmup" in j.notes else "service_step"
        return _V("services", reason)
    started = f.started_age_s
    if started is not None and started > START_TIMEOUT_S:
        return _V("unknown", "start_timeout", may_move=True)
    return _V("launching", "starting")


def _no_prompt(f: Facts) -> _V:
    state = f.j.to_state
    if state in ("FORWARD_STOPPING", "FORWARD_FINALIZE"):
        return _V("closing", "forward_stopping", may_move=True)
    if state == "FORWARD_JUDGING":
        return _V("closing", "forward_judging", may_move=True)
    if state == "ROBOT_HOME":
        return _V("closing", "robot_home", may_move=True)
    if state in ("RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE"):
        return _V("reverse_running", "reset_active", may_move=True)
    if state == "RESET_JUDGING":
        return _V("reverse_running", "reset_judging", may_move=True)
    if state in ("PREFLIGHT", "VERIFY_INITIAL", "SCENE_ASSESS", "FORWARD_ACTIVE"):
        return _V("busy", "waiting_agent" if _waiting_agent(f) else "planning")
    return _V("unknown", "no_rule", may_move=True)


def _expected(f: Facts, expect: str, age: float) -> _V:
    """The journal waits for an answer: the screen must show that prompt, and
    the pty be in the mode it needs, or the page waits and then turns careful."""
    screen = f.screen
    open_ = screen.open
    found = _match_prompt(f, expect, open_)
    if isinstance(found, _V):
        return found
    if found is None:
        # a prompt expected and not (yet) there
        if age >= DRIFT_S:
            reason = (
                "screen_drift" if screen.unrecognised_after_open else "source_conflict"
            )
            return _V("unknown", reason, may_move=True)
        return _V("busy", "awaiting_prompt")
    state, kind, seen, wait, variant, may_move = found
    need = "canonical" if kind in ("phrase", "phrase_variant") else "cbreak"
    if kind == "round":
        need = "cbreak"
    if (f.icanon is True) != (need == "canonical"):
        if age >= DRIFT_S:
            return _V("unknown", "termios_mismatch", may_move=True)
        return _V("busy", "awaiting_prompt", termios=need)
    return _V(
        state,
        "forward_active" if kind == "round" else "prompt_open",
        seen=seen,
        kind=kind,
        wait=wait,
        variant=variant,
        may_move=may_move,
        termios=need,
    )


def _match_prompt(f: Facts, expect: str, open_):
    """``(state, kind, seen, wait, variant, may_move)``, a refusing ``_V``, or
    ``None`` when the prompt is not on the screen (yet)."""
    screen, j = f.screen, f.j
    rec = f.record or {}
    caps = f.profile
    if expect == "home":
        kinds = {
            "home_o": "await_home_o",
            "home_p": "await_home_p",
            "home_h": "await_home_h",
        }
        if open_ is None or open_.kind not in kinds:
            return None
        if not caps.start_home:
            return _V("unknown", "source_conflict", may_move=True)
        return (
            kinds[open_.kind],
            open_.kind,
            open_,
            None,
            None,
            open_.kind == "home_h",
        )
    if expect == "inventory":
        if open_ is None or open_.kind != "inv_todo":
            return None
        ids = [r["id"] for r in screen.inventory_rows]
        if j.inventory_ids is None or sorted(ids) != sorted(j.inventory_ids) or not ids:
            return _V("unknown", "inventory_mismatch", may_move=False)
        return ("await_inventory", "inventory", open_, None, None, False)
    if expect == "release":
        if open_ is None or open_.kind != "release":
            return None
        rounds = int(rec.get("rounds") or 0)
        if open_.total is not None and rounds and open_.total != rounds:
            return _V("unknown", "source_conflict", may_move=False)
        return ("await_release", "release", open_, None, None, False)
    if expect == "round":
        hint = screen.last_index.get("forward_hint", -1)
        if open_ is None or open_.kind != "forward_hint":
            return None
        if hint <= screen.last_index.get("released", -1):
            return None
        return ("forward_running", "round", open_, None, None, True)
    if expect == "resume":
        if (
            open_ is None
            or open_.kind not in ("wait", "confirm_resume")
            or not screen.wait
        ):
            return None
        wait = screen.wait
        if wait.get("reason") not in (j.reason, "other") and not _same_wait(
            wait.get("reason"), j.reason
        ):
            return None
        reason = j.reason or ""
        wait = {
            "reason": reason,
            "confirm": reason in caps.confirm_reasons,
            "enter": reason not in caps.no_resume_reasons,
            "restore_scene": bool(wait.get("restore_scene")),
            "text": wait.get("text") or "",
            "todo": wait.get("todo_key") or "",
            "recent": wait.get("recent") or [],
        }
        variant = "confirm_reason" if wait["confirm"] else "direct"
        if open_.kind == "confirm_resume":
            return (
                "await_resume_confirm",
                "confirm_resume",
                open_,
                wait,
                variant,
                True,
            )
        return ("await_resume", "resume", open_, wait, variant, False)
    return None


def _same_wait(screen_reason, journal_reason) -> bool:
    """The screen's reason (from the sentence) and the journal's (committed):
    one table, so they are equal; ``other`` stands for a reason the table does
    not know (D prints its generic sentence)."""
    return screen_reason == journal_reason


def _assemble(f: Facts, mem: Memory, nxt: Memory, v: _V, expect) -> Derived:
    now = f.now
    j, screen = f.j, f.screen
    rec = f.record or {}
    round_id = j.released[-1][2] if j.released else None
    prompt = None
    if v.seen is not None and v.kind is not None:
        prompt = {
            "id": f"{v.kind}:{v.seen.start}",
            "kind": v.kind,
            "n": v.seen.n,
            "total": v.seen.total,
            "line": v.seen.text,
        }
        if v.kind == "round":
            prompt["id"] = f"round:{round_id or v.seen.start}"
            prompt["n"] = j.released[-1][1] if j.released else v.seen.n
            prompt["total"] = int(rec.get("rounds") or 0) or None
        if v.kind == "release" and prompt["n"] is not None:
            prompt["total"] = prompt["total"] or (int(rec.get("rounds") or 0) or None)
    sig = (v.state, prompt["id"] if prompt else None, v.termios, v.variant)
    sig_since = mem.sig_since if mem.sig == sig else now
    nxt.sig, nxt.sig_since = sig, sig_since
    settled = prompt is not None and (now - sig_since) >= SETTLE_S
    # journal / screen / termios agreement, as the document shows it
    drift = v.reason in ("screen_drift",)
    agree = {
        "journal": "ok" if j.present else "missing",
        "screen": "drift" if drift else ("ok" if screen.bytes else "missing"),
        "termios": "unknown"
        if f.icanon is None
        else ("canonical" if f.icanon else "cbreak"),
    }
    if v.reason == "source_conflict":
        agree["journal"] = "conflict"
    phase = stop_phase(f)
    alive = bool(f.alive) and v.state not in ("ended", "idle")
    stop_open = (
        alive
        and not f.foreign
        and (
            (
                phase == "idle"
                and not v.winding
                and v.state not in ("idle", "ended", "fault_locked", "orphaned")
            )
            or (phase == "unconfirmed" and v.state == "stopping")
        )
    )
    progress = _progress(f, v, rec, round_id)
    inventory = _inventory(f, v)
    return Derived(
        state=v.state,
        reason=v.reason,
        params=v.params,
        may_move=v.may_move,
        prompt=prompt,
        kind=v.kind,
        agree=agree,
        wait=v.wait,
        inventory=inventory,
        progress=progress,
        settled=settled,
        since=sig_since,
        variant=v.variant,
        stop_phase=phase,
        stop_open=bool(stop_open),
        winding=v.winding,
        stalled=v.stalled,
        alive=alive,
        round_id=round_id,
        mem=nxt,
    )


def _progress(f: Facts, v: _V, rec: dict, round_id) -> dict:
    j, screen = f.j, f.screen
    released = j.released[-1][1] if j.released else 0
    latched = bool(round_id) and (
        f.verdict_round_id == round_id
        or any(seq > j.released[-1][0] for seq, _ in j.verdicts)
        or screen.last_index.get("verdict_taken", -1)
        > screen.last_index.get("released", -1)
        and screen.last_index.get("verdict_taken", -1)
        > screen.last_index.get("forward_hint", -1)
        or screen.last_index.get("verdict_refused", -1)
        > screen.last_index.get("forward_hint", -1)
    )
    step = max_steps = None
    if v.state == "forward_running" and screen.steps is not None:
        step, max_steps = screen.steps
    done = max(0, j.reverse_done - j.discarded)
    return {
        "round": _round(screen, v, released),
        "rounds": int(rec.get("rounds") or 0),
        "done": done,
        "released": released,
        "verdict_latched": bool(latched),
        "discarded": j.discarded,
        "step": step,
        "max_steps": max_steps,
    }


def _round(screen, v: _V, released: int) -> int:
    """The round the run is in: the prompt's own (the release prompt of round 2
    comes before anything is released), else the later of the last release and
    the last round tag on the screen."""
    if v.seen is not None and v.seen.n is not None:
        return v.seen.n
    return max(released, screen.round or 0)


def _inventory(f: Facts, v: _V) -> dict | None:
    j, screen = f.j, f.screen
    if j.inventory_seq < 0:
        return None
    rows = screen.inventory_rows_last
    ids = [r["id"] for r in rows]
    shown = (
        rows
        if j.inventory_ids is not None
        and sorted(ids) == sorted(j.inventory_ids)
        and ids
        else []
    )
    return {"confirmed": bool(j.inventory_confirmed), "items": shown}


# --- the keys ------------------------------------------------------------------------------


def seq_signature(d: Derived, keyboard: bool) -> tuple:
    """What changes the state sequence: the tokens of the keys are valid for one
    value of it. The screen tail, the heartbeat's age and the journal's length
    do not (a journal that moves during a forward round would otherwise turn
    every verdict token stale each second)."""
    return (
        d.state,
        d.prompt["id"] if d.prompt else None,
        d.round_id,
        d.progress["verdict_latched"],
        d.stop_phase,
        keyboard,
        d.settled,
    )


@dataclass
class Context:
    """What the page knows besides the run: the monitor, the viewing tabs, what
    it has sent. ``secret`` makes the tokens (a new one per process)."""

    now: float
    secret: bytes = b"\0" * 16
    seq: int = 0
    enabled: bool = True
    run_id: str | None = None
    monitor_state: str = "ok"
    monitor_age_s: float | None = 0.0
    presence_age_s: float | None = None  # the freshest tab
    keyed: frozenset = frozenset()  # prompt ids a key was sent for
    unconfirmed: str | None = None  # the prompt id whose key was not seen taken
    pending: bool = False  # a key was written and is not yet seen taken
    last_taken_at: float | None = None
    start_why: dict | None = None
    history: list = field(default_factory=list)
    locks: dict = field(default_factory=dict)
    tail: list = field(default_factory=list)
    after_exit: dict | None = None
    pid: int | None = None
    exit: dict | None = None


def why(code: str, **params) -> dict:
    return {"code": code, "params": params}


def _iso(stamp: float | None, ms: bool = False) -> str | None:
    if stamp is None:
        return None
    from datetime import datetime

    return (
        datetime.fromtimestamp(stamp)
        .astimezone()
        .isoformat(timespec="milliseconds" if ms else "seconds")
    )


def _actions_of(d: Derived, f: Facts) -> list:
    """The actions of the open prompt, in the order the page shows them."""
    kind = d.kind
    if kind == "phrase":
        return ["execute"]
    if kind == "phrase_variant":
        return ["execute_variant", "execute"]
    if kind in ("home_o", "home_p", "home_h"):
        return [kind, "quit"]
    if kind == "inventory":
        return ["inventory_yes", "inventory_no", "quit"]
    if kind == "release":
        return ["release_round", "quit"]
    if kind == "round":
        return list(VERDICTS)
    if kind == "resume":
        if d.wait and not d.wait["enter"]:
            return ["quit"]
        return ["resume_ask", "quit"]
    if kind == "confirm_resume":
        return ["resume_confirm", "resume_cancel", "quit"]
    return []


def offer(d: Derived, f: Facts, c: Context) -> list:
    """The key entries of the document: the keys of the open prompt, each with
    ``allowed`` or the reason it is not."""
    if d.prompt is None:
        return []
    prompt_id = d.prompt["id"]
    run_name = (f.record or {}).get("name") or ""
    out = []
    for action in _actions_of(d, f):
        spec = KEYS[action]
        level, motion, variant, phrase = spec.level, spec.motion, None, None
        if action == "resume_ask":
            # Level B for both variants. Which variant a wait is rests on an
            # inference (the reason is in the profile's table, ``other`` falls
            # to the direct one): when it is wrong, a "direct" resume would go
            # out as one click, without the delay and the presence check, and
            # the arm moves at once. The price of B for the asking step is 1.5 s.
            variant = d.variant
        if spec.wire is None:
            phrase = phrase_for(action, run_name)
        reason = None
        if not d.alive:
            reason = why("d_gone")
        elif c.unconfirmed is not None and c.unconfirmed == prompt_id:
            reason = why("key_unconfirmed")
        elif prompt_id in c.keyed or c.pending:
            reason = why("key_sent")
        elif action in VERDICTS and d.progress["verdict_latched"]:
            reason = why("verdict_once")
        elif not d.settled:
            reason = why("not_settled")
        elif action == "execute" and d.state == "await_phrase_variant":
            reason = why("swap_unclean")
        elif level in ("B", "C") and c.monitor_state != "ok":
            reason = why("monitor_not_green", state=c.monitor_state)
        elif level in ("B", "C") and not (
            c.presence_age_s is not None and c.presence_age_s < PRESENCE_S
        ):
            reason = why("presence_stale")
        not_before = None
        if reason is None and level in ("B", "C"):
            at = d.since + ARM_DELAY_S
            if c.last_taken_at is not None:
                at = max(at, c.last_taken_at + KEY_GAP_S)
            not_before = _iso(at, ms=True)
        token = None
        if reason is None:
            token = token_for(c.secret, c.run_id, c.seq, prompt_id, action)
        out.append(
            {
                "id": action,
                "byte": spec.label,
                "level": level,
                "motion": motion,
                "slot": spec.slot,
                "variant": variant,
                "phrase": phrase,
                "allowed": reason is None,
                "why_not": reason,
                "token": token,
                "not_before": not_before,
            }
        )
    return out


def find_key(entries: list, action: str) -> dict | None:
    return next((e for e in entries if e["id"] == action), None)


def not_before_epoch(entry: dict) -> float | None:
    from datetime import datetime

    if not entry.get("not_before"):
        return None
    return datetime.fromisoformat(entry["not_before"]).timestamp()


# --- the document ----------------------------------------------------------------------------


def stop_entry(d: Derived, f: Facts) -> dict:
    sent_at = (f.record or {}).get("stop_sent_epoch")
    if d.stop_open:
        return {
            "allowed": True,
            "phase": d.stop_phase,
            "sent_at": _iso(sent_at),
            "why_not": None,
        }
    if not d.alive:
        reason = why("d_gone")
    elif f.foreign:
        reason = why("driven_elsewhere")
    elif d.stop_phase != "idle":
        reason = why("stop_spent")
    else:
        reason = why("state_not_allowed", state=d.state)
    return {
        "allowed": False,
        "phase": d.stop_phase,
        "sent_at": _iso(sent_at),
        "why_not": reason,
    }


def end_entry(d: Derived) -> dict:
    ok = d.stalled and d.alive
    return {
        "allowed": ok,
        "why_not": None if ok else why("end_not_allowed"),
        "phrase": END_PHRASE,
    }


def start_entry(d: Derived, c: Context) -> dict:
    live = d.alive and d.state not in ("idle", "ended")
    reason = c.start_why
    if not c.enabled:
        reason = why("aeri_disabled")
    elif live:
        reason = why("d_active")
    return {
        "allowed": reason is None and not live,
        "why_not": reason,
        "attest": list(ATTEST),
        "phrase": START_PHRASE,
    }


def notices(f: Facts, d: Derived, c: Context) -> list:
    """What the page says beside the state, in order. ``not_estop`` is always
    there: nothing on the page is an emergency stop."""
    out = [{"code": "not_estop", "level": "info"}]
    if c.unconfirmed is not None:
        out.append({"code": "key_unconfirmed", "level": "warn"})
    if d.stop_phase == "unconfirmed":
        out.append({"code": "stop_unconfirmed", "level": "warn"})
    if d.reason == "runner_stale":
        out.append({"code": "runner_stale", "level": "warn"})
    if d.reason == "screen_drift":
        out.append({"code": "screen_drift", "level": "warn"})
    lost = c.presence_age_s is None or c.presence_age_s >= PRESENCE_S
    if d.alive and lost and d.state not in ("stopping", "stalled", "orphaned"):
        out.append({"code": "presence_lost", "level": "warn"})
    # D says a wait was ended from elsewhere than its keyboard (its words for a
    # resume by the AERI inbox, a command file the page cannot stop). A resume
    # by one of this page's keys is D's ordinary "resumed" line, another sentence.
    if any(
        s.kind == "resumed" and s.key == "st_resumed_by_page" for s in f.screen.seen
    ):
        out.append({"code": "inbox_resume", "level": "warn"})
    return out


def document(f: Facts, d: Derived, c: Context) -> dict:
    """The ``levi.marvel0.aeri_run.v1`` document (no path, no judgement of the
    VLM in it)."""
    rec = f.record or {}
    prof = f.profile
    live = d.state not in ("idle",)
    keys = offer(d, f, c)
    runner = None
    if f.runner.present:
        runner = {
            "state": f.runner.state,
            "age_s": None if f.runner_age_s is None else round(f.runner_age_s, 1),
        }
    return {
        "schema": SCHEMA,
        "sampled_at": round(f.now, 3),
        "enabled": c.enabled,
        "run_id": rec.get("run_id") if live else None,
        "name": rec.get("name") if live else None,
        "mode": rec.get("mode") if live else None,
        "task": rec.get("task") if live else None,
        "steps": int(rec.get("steps") or 0),
        "profile": {
            "id": prof.id if prof else rec.get("profile"),
            "start_home": bool(prof.start_home) if prof else False,
            "console_lang": rec.get("console_lang") or "zh",
            "code_sha": prof.commit if prof else None,
        },
        "state": {
            "id": d.state,
            "reason": d.reason,
            "params": d.params,
            "seq": c.seq,
            "since": _iso(d.since),
            "may_move": d.may_move,
            "settled": d.settled,
            "prompt": d.prompt,
        },
        "agree": d.agree,
        "progress": d.progress,
        "inventory": d.inventory,
        "wait": d.wait,
        "keys": keys,
        "start": start_entry(d, c),
        "stop": stop_entry(d, f),
        "presence": {
            "ok": c.presence_age_s is not None and c.presence_age_s < PRESENCE_S,
            "age_s": None if c.presence_age_s is None else round(c.presence_age_s, 1),
            "need_s": PRESENCE_S,
        },
        "end": end_entry(d),
        "monitor": {
            "state": c.monitor_state,
            "age_s": None if c.monitor_age_s is None else round(c.monitor_age_s, 1),
        },
        "d": {
            "alive": d.alive,
            "pid": c.pid,
            "keyboard": bool(f.keyboard),
            "runner": runner,
            "exit": c.exit if d.state == "ended" else None,
        },
        "screen": {
            "tail": c.tail[-SCREEN_TAIL:],
            "bytes": f.screen.bytes,
            "drift": d.reason == "screen_drift",
        },
        "queue": {
            "pending": f.queue_pending,
            "oldest_age_s": None
            if f.queue_oldest_s is None
            else round(f.queue_oldest_s, 1),
        },
        "locks": c.locks,
        "history": c.history[-HISTORY_KEPT:],
        "fallback": {"stop_cli": "scripts/aeri_page_stop.py"},
        "after_exit": c.after_exit,
        "notices": notices(f, d, c),
    }
