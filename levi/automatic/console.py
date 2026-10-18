"""The operator terminal of ``fr3_run --execute`` (task T-C-CONSOLE).

One terminal runs the whole AERI flow, the way the old client did: a start
summary, scene questions answered by one key, one Enter per round, a prompt
line per step that says where the run is and what to do now, and a readable
report when the run locks. Nothing here changes what the state machine does:
every decision still goes through the same orchestrator calls (``stop``,
``control.resume_checked``) and the same scene provider (``adapters.human``);
this module only reads state and keys, and leaves journal notes
(``RESET_NOTE``, ``INTERRUPT_NOTE``) through the existing note mechanism.

Where it plugs into ``fr3_run`` (all optional; ``None`` means the old
behaviour):

- ``from_environment`` returns a ``Console`` only when stdin **and** stdout
  are terminals (pipes and tests keep the old code path);
- ``human_scene`` builds the scene provider: terminal and page both answer,
  and (human-assisted) the "reset the environment, press Enter" prompt is
  shown *before* the evidence frames are captured;
- ``tee`` mirrors the orchestrator's state notifications into round banners;
- ``wait_human`` replaces the typed ``resume`` of ``WAIT_HUMAN``;
- ``interrupted`` is told of Ctrl+C / SIGTERM; ``finish`` prints the fault
  summary when the run ends locked.

What a prompt line says comes from one table (``STATUS``: state, committed
reason or note code -> sentence) and one for ``WAIT_HUMAN`` (``WAIT_REASONS``);
a new state or reason is a new row, not new logic. The words are in
``console_text.py`` (zh default, en with ``LEVI_AERI_CONSOLE_LANG=en``).

Blindness: the automatic verdict (``goal_verification``, ``task_outcome``) is
never printed before the operator's label of that episode exists, and neither
is how the episode ended when an early stop is allowed (``goal_verified``
versus the step limit would give the verdict away). Without early stop the
banner shows the step count and how the episode ended, as the old client did.
"""

import contextlib
import json
import os
import select
import signal
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

from . import console_text, control, metrics
from .adapters import human

ENTER = ("\n", "\r")
QUIT = ("q", "Q", "\x04")
LABEL_KEYS = {"s": "success", "f": "failure", "u": "unclear", "d": "discarded"}
# While a forward episode runs (FORWARD_ACTIVE, legacy_async): one key ends
# this episode with the person's verdict (L2-AERI-OPERATOR-EARLY-STOP, as the
# old client's s/f/d did); the run goes on with the next round. Read single
# keys from the fd in cbreak, never blocking; keys typed before the round are
# dropped when it starts, and keys typed while homing, resetting or waiting do
# nothing here (they are dropped when the round ends or the next one starts).
VERDICT_KEYS = {"s": "success", "f": "failure", "d": "discard"}
VERDICT_WORDS = {"success": "v_success", "failure": "v_failure", "discard": "v_discard"}
ENV_SWITCH = "LEVI_AERI_CONSOLE"
ENV_LABELS = "LEVI_AERI_CONSOLE_LABELS"
ENV_PRINCIPAL = "LEVI_AERI_CONSOLE_PRINCIPAL"
ENV_LANG = console_text.ENV_LANG
POLL_S = 0.1
# Journal notes written by the console (the existing note mechanism; no new
# schema field). The first is the person's reset: with Enter before the
# post_forward frames and ``y`` skipping WAIT_HUMAN, no ``scene_*`` wait and
# ``human_resumed`` remain to say a person put the scene back.
RESET_NOTE = "operator_reset_confirmed"
# Ctrl+C / SIGTERM: the entry holds the arm and registers the stop, but a
# stop registered while the run waits for a person is not always a journal
# line (the run may be left in WAIT_HUMAN); this note says who stopped it,
# with which signal and where the person was.
INTERRUPT_NOTE = "operator_interrupt"


class Row(NamedTuple):
    """One line of the state table: what has just happened, for a run that
    is in ``state`` (any if ``None``), whose latest committed reason is
    ``reason`` and/or whose latest note is ``note``. The first matching row
    wins; ``announce`` prints a line when the run enters it."""

    say: str
    state: str | None = None
    reason: str | None = None
    note: str | None = None
    announce: bool = False


# Most specific rows first. The first two are the rows of the home-verification
# branch (``fix/aeri-home-gate``: committed reason ``robot_home_reached``, note
# ``robot_homed``); they are harmless before it is merged.
STATUS = (
    Row("st_home_done", note="robot_homed"),
    Row("st_home_done", reason="robot_home_reached"),
    Row("st_home_done", reason="subtask_forward_done"),
    Row("st_stopped", state="COMPLETED", reason="operator_stop", announce=True),
    Row("st_completed", state="COMPLETED", announce=True),
    Row("st_event_pause", state="FORWARD_JUDGING", announce=True),
    Row("st_forward", state="FORWARD_ACTIVE"),
    Row("st_stopping", state="FORWARD_STOPPING"),
    Row("st_stopping", state="FORWARD_FINALIZE"),
    Row("st_home", state="ROBOT_HOME"),
    Row("st_scene", state="SCENE_ASSESS"),
    Row("st_scene", state="VERIFY_INITIAL"),
    Row("st_reset_policy", state="RESET_ACTIVE", announce=True),
)
# WAIT_HUMAN, by committed reason: (what happened, what to do). The
# ``scene_reset_required`` row is also the one of the multi-segment run of
# ``fix/aeri-home-gate``.
WAIT_REASONS = {
    "scene_reset_required": ("wait_scene_reset_required", "todo_reset_resume"),
    "scene_unknown": ("wait_scene_unknown", "todo_reset_resume"),
    "preflight_failed": ("wait_preflight_failed", "todo_check_resume"),
    "policy_error": ("wait_policy_error", "todo_policy_resume"),
    "operator_stop": ("wait_operator_stop", "todo_stop_resume"),
    "contract_violation_limit": ("wait_contract_violation_limit", "todo_judge_resume"),
    "reset_horizon_exhausted": ("wait_reset_horizon_exhausted", "todo_reset_resume"),
    "startup_home_not_executed": (
        "wait_startup_home_not_executed",
        "todo_startup_new_run",
    ),
    "home_not_executed": ("wait_home_not_executed", "todo_home_resume"),
    "home_pose_not_reached": ("wait_home_pose_not_reached", "todo_home_pose_resume"),
    # Frozen camera frames (camera_meta.py): the arm was not homed.
    "camera_stale": ("wait_camera_stale", "todo_camera_stale_resume"),
    # subtask_loop (levi/automatic/subtask_loop.py): the concrete reason comes
    # from the latest note of the wait (``SUBTASK_WAIT_NOTES``).
    "subtask_plan_refused": ("wait_subtask_plan_refused", "todo_subtask_replan"),
    "subtask_check_unavailable": (
        "wait_subtask_check_unavailable",
        "todo_subtask_restart",
    ),
    "subtask_retry_limit": ("wait_subtask_retry_limit", "todo_subtask_restart_confirm"),
    "subtask_inventory_rejected": (
        "wait_subtask_inventory_rejected",
        "todo_subtask_inventory",
    ),
    "subtask_not_released": ("wait_subtask_not_released", "todo_subtask_release"),
    "vlm_unavailable": ("wait_vlm_unavailable", "todo_vlm_resume"),
}
# Which note explains a subtask_loop wait, and which sentence it gets
# (``vlm_unavailable`` has three: the wake refused, failed, or the sleep).
SUBTASK_WAIT_NOTES = {
    "subtask_plan_refused": {"subtask_plan_refused": "wait_subtask_plan_refused"},
    "subtask_check_unavailable": {"subtask_check": "wait_subtask_check_unavailable"},
    "subtask_retry_limit": {"subtask_retry": "wait_subtask_retry_limit"},
    "subtask_inventory_rejected": {
        "subtask_inventory_rejected": "wait_subtask_inventory_rejected"
    },
    "vlm_unavailable": {
        "vlm_wake_refused": "wait_vlm_wake_refused",
        "vlm_wake_failed": "wait_vlm_wake_failed",
        "vlm_sleep_refused": "wait_vlm_sleep_refused",
    },
}
WAIT_OTHER = ("wait_other", "todo_other_resume")
# Waits a person causes or expects: no list of notes under the line.
ROUTINE_WAITS = frozenset({"scene_reset_required", "scene_unknown", "operator_stop"})
# Waits where one Enter must not be enough (user decision 2026-10-14): the
# person asked to stop, the judge broke its contract, or the policy failed.
# Enter asks, a flush empties the keys typed meanwhile, and only y confirms.
CONFIRM_REASONS = frozenset(
    {
        "operator_stop",
        "contract_violation_limit",
        "policy_error",
        "home_not_executed",
        "home_pose_not_reached",
        # The cameras stopped and the arm stands where it stopped.
        "camera_stale",
        # subtask_loop: the VLM's wake/sleep/memory, and a subtask that failed
        # its check more often than the limit (user 2026-10-17).
        "vlm_unavailable",
        "subtask_retry_limit",
    }
)
CONFIRM_KEYS = ("y", "Y")
CANCEL_KEYS = ("n", "N")
# Note codes whose detail may be shown on the terminal: operational facts of
# the robot, recorder, policy chunks and session files. Everything else
# (``early_stop_disputed`` carries the final judgement, ``judgement_*``,
# ``contract_violation``, ``scene_*``, ...) stays in the journal and on the
# page: a detail printed here could give the verdict away before the label of
# that episode. A new note code is hidden until it is listed here.
DISPLAY_NOTES = frozenset(
    {
        "recorder_error",
        "evidence_write_failed",
        "evidence_budget_exhausted",
        "home_failed_detail",
        "home_measured",
        "robot_homed",
        "start_pose_checked",
        # Which camera stopped and its frame number/age (camera_meta.py).
        "camera_stale",
        "hold_failed",
        "motion_refused",
        "motion_unacknowledged",
        "orchestrator_exception",
        "note_flush_failed",
        "chunk_timeout",
        "chunk_dropped_contract",
        "chunk_dropped_stale",
        "chunk_dropped_unsolicited",
        "session_write_failed",
        "stop_command_lost",
        "stop_commands_merged",
        "report_failed",
        "run_header_mismatch",
        "rollouts_recovered",
        "strategy_refused",
        "plan_changed",
        "halted",
        # subtask_loop: the flow's own notes. ``subtask_check`` (the VLM's
        # answer) is left out: a model judgement stays off the screen, as the
        # automatic verdict does, until the round is labelled.
        "subtask_alternation",
        "subtask_inventory",
        "subtask_inventory_confirmed",
        "subtask_inventory_rejected",
        "subtask_round_released",
        "subtask_round_abandoned",
        "subtask_plan",
        "subtask_plan_refused",
        "subtask_started",
        "subtask_retry",
        "subtask_loop_done",
        # subtask.early_check: what triggered the check (no answer in it).
        "subtask_early_check",
        "subtask_service_step",
        "vlm_sleep",
        "vlm_wake",
        "vlm_wake_failed",
        "vlm_wake_refused",
        "vlm_sleep_refused",
    }
)
# A second, separate class (L2-REAL-ERRLOG): the error text the policy adapter
# or the policy service gave (``websocket closed (1011)``). It names the
# failing service, not an outcome, and the operator needs it to act. Cut to
# POLICY_ERROR_LIMIT characters; judgement notes are still not listed.
POLICY_ERROR_NOTES = frozenset({"policy_unavailable"})
POLICY_ERROR_LIMIT = 300
# C0 controls and DEL (\n, \r, ESC, ...): a newline, carriage return or ANSI
# escape in the error text must not break or overwrite the terminal lines.
_CONTROL = {c: " " for c in [*range(0x20), 0x7F]}


def shown_notes(notes):
    """The notes the terminal may show, in order: DISPLAY_NOTES as they are,
    POLICY_ERROR_NOTES with control characters (0x00-0x1f, 0x7f) replaced by
    spaces, then cut to POLICY_ERROR_LIMIT characters; nothing else."""
    shown = []
    for note in notes:
        if note[0] in DISPLAY_NOTES:
            shown.append(note)
        elif note[0] in POLICY_ERROR_NOTES:
            text = str(note[1]).translate(_CONTROL)
            shown.append((note[0], text[:POLICY_ERROR_LIMIT]))
    return shown


FAULT_CODES = frozenset(
    {
        "recorder_failed",
        "safety_stop",
        "watchdog_timeout",
        "policy_error",
        "home_failed",
        "judgement_unproven",
        "recovery_ambiguous",
    }
)


def status_row(state=None, reason=None, note=None):
    """The table row for a run in ``state`` whose latest committed reason is
    ``reason`` and latest note code ``note`` (``None`` if no row fits)."""
    for row in STATUS:
        if (
            (row.state is None or row.state == state)
            and (row.reason is None or row.reason == reason)
            and (row.note is None or row.note == note)
        ):
            return row
    return None


def wait_reason(orch) -> str:
    """The reason of the latest committed move into WAIT_HUMAN (``""`` if
    none); the console and the old terminal path of ``fr3_run`` both use it
    to decide whether a resume needs the second confirmation."""
    for event in reversed(orch.journal.events):
        if event.record == "committed" and event.to_state == "WAIT_HUMAN":
            return event.reason or ""
    return ""


def latest(events):
    """``(state, reason, note)`` from journal events: the last committed
    line's state and reason, and the latest note code since the committed
    line before it (a note belongs to the move it is written beside)."""
    commits = [i for i, e in enumerate(events) if e.record == "committed"]
    if not commits:
        return None, None, None
    last = events[commits[-1]]
    since = commits[-2] + 1 if len(commits) > 1 else 0
    known = {row.note for row in STATUS if row.note}
    note = None
    for event in reversed(events[since:]):
        if (
            event.record == "note"
            and event.note is not None
            and event.note.code in known
        ):
            note = event.note.code
            break
    return last.to_state, last.reason, note


class StdinKeys:
    """Single keys from a terminal, without ever blocking.

    Reads the file descriptor directly (no Python buffer, so a flush really
    empties what is pending). ``cbreak`` makes keys arrive unbuffered while
    keeping Ctrl+C a signal."""

    def __init__(self, fd=None):
        self.fd = sys.stdin.fileno() if fd is None else fd

    def poll(self, timeout=0.0):
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return None
        data = os.read(self.fd, 1)
        if not data:
            return "\x04"  # the terminal went away: treated as a quit
        char = data.decode("utf-8", "replace")
        if char == "\x1b":
            # Arrow keys and the like send ESC + letters: not answers.
            while select.select([self.fd], [], [], 0.02)[0]:
                os.read(self.fd, 1)
        return char

    def flush(self):
        try:
            import termios

            termios.tcflush(self.fd, termios.TCIFLUSH)
        except (ImportError, OSError, ValueError):
            pass

    @contextlib.contextmanager
    def cbreak(self):
        try:
            import termios
            import tty

            saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except (ImportError, OSError, ValueError):
            yield
            return
        try:
            yield
        finally:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, saved)


def from_environment(*, stdin=None, stdout=None, environ=None, keys=None, **options):
    """A ``Console`` when this is an interactive terminal, else ``None``."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    environ = os.environ if environ is None else environ
    if environ.get(ENV_SWITCH) == "0":
        return None
    if not (stdin.isatty() and stdout.isatty()):
        return None
    options.setdefault("labels", environ.get(ENV_LABELS) == "1")
    options.setdefault("principal", environ.get(ENV_PRINCIPAL, "operator"))
    options.setdefault("lang", console_text.language(environ))
    return Console(keys or StdinKeys(), stdout, **options)


def _display_step_budget(config) -> int:
    """Use the same per-episode limit as the configured execution mode."""
    return (
        config.subtask_max_steps
        if getattr(config, "reset_strategy", None) == "subtask_loop"
        else config.forward_max_steps
    )


def summary_text(job, binding=None, lang=None) -> str:
    """The screen shown before ``execute <run id>`` is typed."""
    t = console_text.Texts(console_text.language(os.environ) if lang is None else lang)
    cfg = job["config"]
    human_mode = cfg.reset_strategy == "human_assisted"
    subtask_mode = cfg.reset_strategy == "subtask_loop"
    texts = job.get("texts", {})
    deployments = job.get("plan", {}).get("deployments", {})
    term = cfg.termination
    lines = [
        "",
        "=" * 60,
        t("sum_title", run_id=cfg.run_id),
        "-" * 60,
        t(
            "sum_mode_subtask"
            if subtask_mode
            else "sum_mode_human"
            if human_mode
            else "sum_mode_policy"
        ),
        t(
            "sum_rounds_subtask" if subtask_mode else "sum_rounds",
            episodes=cfg.episodes,
            max_steps=_display_step_budget(cfg),
        ),
        t("sum_task", text=texts.get("forward") or t("sum_task_unset")),
    ]
    early = getattr(cfg, "subtask_early_check", None) if subtask_mode else None
    if early is not None:
        lines.append(
            t(
                "sum_early_check",
                events=", ".join(early.event_types) if early.on_events else "-",
                every=early.every_steps or "-",
                max_checks=early.max_checks,
            )
        )
    for role, deployment in deployments.items():
        lines.append(
            t(
                "sum_checkpoint",
                role=role,
                checkpoint=deployment.get("checkpoint_id", "?"),
                directory=deployment.get("checkpoint_dir", "?"),
            )
        )
    lines.append(
        t(
            "sum_judge",
            scene=t(
                "sum_scene_operator"
                if cfg.scene_check == "operator_attested"
                else "sum_scene_provider"
            ),
            outcome=t(
                "sum_outcome_auto" if term.allow_early_stop else "sum_outcome_budget",
                mode=term.inference_mode,
            ),
        )
    )
    lines += [
        t("sum_pose"),
        t(
            "sum_ops_subtask"
            if subtask_mode
            else "sum_ops_human"
            if human_mode
            else "sum_ops_policy"
        ),
        t("sum_stop"),
        "=" * 60,
        "",
    ]
    return "\n".join(lines)


class GateStopped(Exception):
    """The person asked to end the run at a prompt."""


class ConsoleScene(human.HumanSceneProvider):
    """The human scene provider, with one prompt before the evidence frames
    are captured: after the arm is home the person resets the environment
    and presses Enter, and only then are the frames taken. Without it the
    first post-forward frames would show the environment as it was before
    the person touched it."""

    def __init__(self, *args, before_capture=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.before_capture = before_capture

    def submit(self, request):
        if self.before_capture is not None:
            try:
                self.before_capture(request)
            except GateStopped as exc:
                self._note("scene_check_failed", f"GateStopped: {exc}")
                return self._unavailable(request, "provider_error", str(exc))
        return super().submit(request)


def human_scene(console, contract, capture, file_transport, clock, run_id):
    """The scene provider of an operator-attested run: with a console, the
    question is shown on the page and at the terminal, only the terminal's
    y/n/u answers it (decision UI-2: an answer on the page is moved away,
    noted ``scene_answer_page_ignored`` and never releases motion), and the
    reset prompt precedes the capture; without, the plain provider on the
    file protocol (the real-robot entry refuses that case, ``fr3_run``)."""
    if console is None:
        return human.HumanSceneProvider(
            contract, capture, file_transport, clock, run_id
        )
    terminal = console.terminal()
    return ConsoleScene(
        contract,
        capture,
        _terminal_only(terminal, file_transport),
        clock,
        run_id,
        before_capture=console.reset_prompt,
    )


def _terminal_only(terminal, file_transport):
    """The page's file channel (shows the question, its answers never count)
    beside the terminal, whose y/n/u alone answers (UI-2)."""

    def page_ignored(request_id):
        if terminal.waiting():
            terminal.say(terminal.t("scene_page_ignored"))

    return human.BothTransport(
        human.AskOnlyTransport(file_transport, on_ignored=page_ignored), terminal
    )


MODEL_DISAGREED = "scene_model_disagreed"
MODEL_DECISIONS = ("ready", "reset_required", "unknown")


class ConfirmedScene:
    """The scene check of a real run whose ``scene_check`` is ``provider``
    (decision 2026-10-15, L2-UI2-PROVIDER-RELEASE; AERI-IDEAL-FLOW.md lines
    27-30): before every round the person at the terminal releases motion.

    ``submit``: the reset prompt first (Enter, then the frames are taken;
    post-forward only), then the model provider is asked and waited for (up
    to ``model_timeout_ns``) and its answer is printed for reference only,
    then the person's question (``person``, a ``HumanSceneProvider`` on the
    terminal) is asked. The assessment the orchestrator receives is the
    person's: y on every required predicate is ``ready``, an n or a u takes
    the existing routes. The model's answer never releases anything; when
    it differs from the person's, ``scene_model_disagreed`` is noted (both
    answers) and the person's decision stands."""

    # The orchestrator waits for a person (human_scene_timeout_ns, in slices).
    person_answers = True

    def __init__(
        self,
        model,
        person,
        contract,
        *,
        say,
        text,
        specs,
        model_timeout_ns,
        before_capture=None,
    ):
        self.model = model
        self.person = person
        self.contract = contract
        self.say = say
        self.t = text
        self.specs = specs
        self.model_timeout_ns = model_timeout_ns
        self.before_capture = before_capture
        self._lock = threading.Lock()
        self._model: dict[str, str] = {}
        self._notes: list = []

    def reachable(self):
        return self.person.reachable()

    def use_evidence(self, store) -> None:
        attach = getattr(self.model, "use_evidence", None)
        if attach is not None:
            attach(store)
        self.person.use_evidence(store)

    def submit(self, request):
        if self.before_capture is not None:
            try:
                self.before_capture(request)
            except GateStopped as exc:
                self._note("scene_check_failed", f"GateStopped: {exc}")
                return self.person._unavailable(request, "provider_error", str(exc))
        found = self._ask_model(request)
        with self._lock:
            self._model[request["request_id"]] = found
        key = found if found in MODEL_DECISIONS else "unavailable"
        self.say(self.t("scene_model_hint", decision=self.t("scene_model_" + key)))
        return self.person.submit(request)

    def _ask_model(self, request) -> str:
        """The model's decision on the contract (``ready``, ``reset_required``,
        ``unknown``) or ``unavailable``; never raises."""
        from levi.domain import aeri

        from . import scene_assessment as sa

        try:
            got = self.model.submit(request)
            if not isinstance(got, bytes | bytearray):
                ticket = got
                try:
                    got = self.model.collect(ticket, timeout_ns=self.model_timeout_ns)
                except BaseException:
                    self.model.cancel(ticket, "error")
                    raise
                if got is None:
                    self.model.cancel(ticket, "timeout")
                    return "unavailable"
            message = aeri.parse(bytes(got), "scene", specs=self.specs)
            if message.request_id != request["request_id"]:
                return "unavailable"
            return sa.arbitrate(message, self.contract).decision
        except Exception as exc:  # noqa: BLE001 - a reference answer only
            self._note("scene_model_failed", f"{type(exc).__name__}: {exc}")
            return "unavailable"

    def collect(self, ticket, *, timeout_ns):
        raw = self.person.collect(ticket, timeout_ns=timeout_ns)
        if raw is not None:
            with self._lock:
                model = self._model.pop(ticket.request_id, "unavailable")
            try:
                message = json.loads(raw)
                person = (
                    message.get("decision")
                    if message.get("kind") == "scene"
                    else "unavailable"
                )
            except (ValueError, AttributeError):
                person = "unavailable"
            if person != model:
                self._note(
                    MODEL_DISAGREED,
                    f"{ticket.request_id}: model={model} operator={person}",
                )
        return raw

    def cancel(self, ticket, reason) -> None:
        with self._lock:
            self._model.pop(ticket.request_id, None)
        self.person.cancel(ticket, reason)

    def frames(self, request_id):
        return self.person.frames(request_id)

    def unsaved(self, request_id):
        return self.person.unsaved(request_id)

    def _note(self, code: str, detail: str) -> None:
        with self._lock:
            self._notes.append((code, detail[:300]))

    def drain_notes(self) -> list:
        found = self.person.drain_notes()
        with self._lock:
            mine, self._notes = self._notes, []
        return mine + found


def confirmed_scene(
    console,
    model,
    contract,
    capture,
    file_transport,
    clock,
    run_id,
    *,
    specs,
    model_timeout_ns,
):
    """The scene provider of a real run with ``scene_check: provider``: the
    model answers for reference, the terminal's y/n/u decides
    (``ConfirmedScene``). There is no such provider without a terminal: the
    real-robot entry refuses that run before any connection (``fr3_run``)."""
    if console is None:
        raise ValueError("a real run's scene check needs the operator terminal")
    terminal = console.terminal()
    person = human.HumanSceneProvider(
        contract, capture, _terminal_only(terminal, file_transport), clock, run_id
    )
    return ConfirmedScene(
        model,
        person,
        contract,
        say=terminal.say,
        text=terminal.t,
        specs=specs,
        model_timeout_ns=model_timeout_ns,
        before_capture=console.reset_prompt,
    )


def tee(console, listener):
    """The orchestrator's state listener, mirrored into round banners."""
    if console is None:
        return listener

    def both(state, info):
        try:
            if listener is not None:
                listener(state, info)
        finally:
            console.on_state(state, info)

    return both


class Console:
    def __init__(
        self,
        keys,
        out,
        *,
        labels=False,
        principal="operator",
        health_probe=None,
        run_dir=None,
        idle=None,
        lang="zh",
    ):
        self.keys = keys
        self.out = out
        self.labels = labels
        self.principal = principal
        self.health_probe = health_probe
        self.run_dir = None if run_dir is None else Path(run_dir)
        self.idle = idle or (lambda: None)
        self.t = console_text.Texts(lang)
        self.orch = None
        self.robot = None
        self._say_lock = threading.Lock()
        self._quit = False
        self._finished = False
        self._transport = None
        self._progress = (None, None)
        self._at = None
        self._interrupted = False
        self._signum = lambda: None
        self._interrupt_lock = threading.Lock()
        # The verdict keys of the forward episode under way: open from its
        # FORWARD_ACTIVE until the run leaves that state (the episode id),
        # the cbreak it holds, and the verdict sent in it. One lock: the
        # heartbeat's read and the orchestrator thread's close never overlap,
        # so no key of a later prompt is ever read here.
        self._verdict_lock = threading.Lock()
        self._verdict_episode = None
        self._verdict_cbreak = None
        self._verdict_sent = None
        # subtask_loop: the verdict keys are read during a reverse episode
        # too, only to say they do nothing there.
        self._verdict_reverse = False

    # ------------------------------------------------------------ plumbing

    def bind(self, orch, robot, run_dir=None, signum=None):
        """``signum()``: the signal that asked the entry to stop, if any."""
        self.orch, self.robot = orch, robot
        if signum is not None:
            self._signum = signum
        if run_dir is not None:
            self.run_dir = Path(run_dir)

    def say(self, text: str = "") -> None:
        with self._say_lock:
            print(text, file=self.out, flush=True)

    def _say_quietly(self, text: str = "") -> None:
        """``say`` for the end of a run: a dead terminal must not stop the
        cleanup that follows (``fr3_run`` closes the fence, the orchestrator
        and the recorder after the console)."""
        with contextlib.suppress(Exception):
            self.say(text)

    def note(self, code: str, detail: str) -> None:
        """A journal note as the operator (the audit mechanism ``control``
        uses); a failure is shown, never raised: a note must not end a run."""
        try:
            orch = self.orch
            orch.journal.note(code, detail[:300], authority=orch._auth("operator"))
        except Exception as exc:  # noqa: BLE001 - the screen is the fallback
            self.say(self.t("note_failed", code=code, why=exc))

    def tag(self, target: str | None = None) -> str:
        """``[Round n/N]``, or ``[Setup]`` before the first round starts. The
        question that opens a round (``initial_state``) belongs to the next
        round, the others to the round that just ended."""
        if self.orch is None:
            return self.t("tag_setup")
        number = self.orch.numbers["forward"]
        total = self.orch.config.episodes
        if target == "initial_state":
            number = min(number + 1, total)
        if not number:
            return self.t("tag_setup")
        return self.t("tag", n=number, total=total)

    def line(self, happened: str, todo: str | None = None) -> str:
        """One prompt line: where we are, what just happened, what to do."""
        body = self.t("join", happened=happened, todo=todo) if todo else happened
        return self.tag() + " " + body

    def _where(self) -> str:
        if self._at is not None:
            return self._at
        if self._transport is not None and self._transport.waiting():
            return "scene question"
        return "while running"

    def interrupted(self, signum=None) -> None:
        """The run was asked to stop by a signal (the entry's heartbeat calls
        this after holding the arm and registering the stop; a prompt calls it
        when it sees the stop first): say so on the screen and leave one
        journal note, once. Never raises."""
        with self._interrupt_lock:
            if self._interrupted or self.orch is None:
                return
            self._interrupted = True
        with contextlib.suppress(Exception):
            signum = signum or self._signum()
            name = signal.Signals(signum).name if signum else "STOP"
            where = self._where()
            self.say("")
            self.say(self.t("interrupt", name=name))
            self.note(
                INTERRUPT_NOTE,
                f"{name} {where}; state {self.orch.state}; "
                f"round {self.orch.numbers['forward']}/{self.orch.config.episodes}",
            )

    def _stopped(self) -> bool:
        return bool(self.robot is not None and self.robot.stopped())

    def _health_warning(self):
        if self.health_probe is None:
            return None
        try:
            return None if self.health_probe() else self.t("health_warning")
        except Exception:  # noqa: BLE001 - a probe never stops the prompt
            return None

    def terminal(self) -> human.TerminalTransport:
        self._transport = human.TerminalTransport(
            self.keys,
            self.say,
            run_dir=self.run_dir,
            stopped=self._stopped,
            status=self._health_warning,
            text=self.t,
            tag=self.tag,
        )
        return self._transport

    @contextlib.contextmanager
    def _cbreak(self):
        cm = self.keys.cbreak()
        cm.__enter__()
        try:
            yield
        finally:
            cm.__exit__(None, None, None)

    # ------------------------------------------------------------ prompts

    def _wait_key(self, handle, *, moved=lambda: False):
        """Poll keys until ``handle(key)`` returns a result; ``"stopped"``
        when the robot was told to stop, ``"moved"`` when the run left the
        state by another route (the page)."""
        warned = None
        with self._cbreak():
            while True:
                if self._stopped():
                    self.interrupted()
                    return "stopped"
                if moved():
                    return "moved"
                warning = self._health_warning()
                if warning != warned:
                    warned = warning
                    if warning:
                        self.say(warning)
                key = self.keys.poll(POLL_S)
                if key is None:
                    self.idle()
                    continue
                result = handle(key)
                if result is not None:
                    return result

    def _handler(self, enter):
        def handle(key):
            if key in ENTER:
                return enter
            if key in QUIT:
                return "quit"
            if self.labels and key in LABEL_KEYS:
                self._label(LABEL_KEYS[key])
            return None

        return handle

    def _label_hint(self) -> str:
        return self.t("label_hint") if self.labels else ""

    def _last_forward(self):
        ended = metrics.ended_forward(self.orch.journal.events)
        return list(ended)[-1] if ended else None, ended

    def _labelled(self, episode) -> bool:
        if not self.labels or self.run_dir is None:
            return False
        try:
            lines = metrics.LabelStore(self.run_dir).lines("operator_label")
        except (metrics.LabelRefused, OSError):
            return False
        return any(line.get("episode_id") == episode for line in lines)

    def _label(self, value: str) -> None:
        episode, ended = self._last_forward()
        if episode is None:
            self.say(self.t("label_none"))
            return
        try:
            metrics.label_operator(
                self.run_dir, episode, value, by=self.principal, note="terminal"
            )
        except (metrics.LabelRefused, OSError) as exc:
            self.say(self.t("label_refused", why=exc))
            return
        self.say(
            self.t("label_done", episode=episode, value=value, principal=self.principal)
        )
        # Revealed only now, after the blind label (as the label card does).
        result = ended[episode]
        self.say(
            self.t(
                "label_reveal",
                verdict=self.t(
                    "verdict_" + metrics.automatic_of(result.goal_verification)
                ),
                ended=self.t("ended_" + metrics.ended_by(result.stop_reason)),
            )
        )

    def reset_prompt(self, request: dict) -> None:
        """Before the evidence frames of a post-forward check are taken."""
        if request.get("target") != "post_forward":
            return
        self.keys.flush()
        self.say("")
        self.say(
            self.line(
                self.t("st_home_done"),
                self.t("todo_reset_enter") + self._label_hint(),
            )
        )
        self._at = "reset prompt"
        try:
            found = self._wait_key(self._handler("enter"))
        finally:
            self._at = None
        if found == "enter":
            self.note(
                RESET_NOTE,
                f"after {request.get('episode_id')}: Enter before the post_forward frames",
            )
            return
        self._quit = True
        self.orch.stop("terminal-quit-" + str(time.time_ns()))
        raise GateStopped(
            "operator ended the run at the reset prompt" if found == "quit" else found
        )

    # ------------------------------------------------------------ subtask_loop
    # Terminal D's two questions of the subtask_loop strategy (the gate of
    # ``subtask_loop.Gate``): only these keys confirm the inventory or release
    # a round. The page, the command inbox and the VLM have no route here.

    def _subtask_stop(self, found) -> str:
        self._quit = True
        self.orch.stop("terminal-quit-" + str(time.time_ns()))
        return "stopped"

    def confirm_inventory(self, objects) -> str:
        """``"yes"`` (y), ``"no"`` (n) or ``"stopped"`` (q, or the robot told
        to stop)."""
        self.keys.flush()
        self.say("")
        self.say(self.line(self.t("sub_inv_title")))
        for row in objects:
            self.say(
                self.t(
                    "sub_inv_row",
                    id=row.get("id"),
                    colour=row.get("colour"),
                    kind=row.get("kind"),
                    on=row.get("on"),
                    grasp=self.t(
                        "sub_inv_grasp" if row.get("graspable") else "sub_inv_place"
                    ),
                )
            )
        self.say(self.line(self.t("sub_inv_todo")))

        def handle(key):
            if key in CONFIRM_KEYS:
                return "yes"
            if key in CANCEL_KEYS:
                return "no"
            if key in QUIT:
                return "quit"
            return None

        self._at = "confirming the object list"
        try:
            found = self._wait_key(handle)
        finally:
            self._at = None
        if found in ("quit", "stopped"):
            return self._subtask_stop(found)
        self.say(self.line(self.t("sub_inv_yes" if found == "yes" else "sub_inv_no")))
        return found

    def confirm_start_home(self):
        """Three fresh explicit keys; Enter and resume's y cannot home."""
        confirmations = {}
        for field, key in (
            ("object_handled", "o"),
            ("path_clear", "p"),
            ("gripper_will_open", "h"),
        ):
            self.keys.flush()
            self.say(self.t("startup_home_" + field))
            self._at = "confirming startup home"
            try:
                found = self._wait_key(
                    lambda pressed, expected=key: (
                        "confirmed"
                        if pressed == expected
                        else ("quit" if pressed in QUIT else None)
                    ),
                    moved=lambda: self.orch.state != "PREFLIGHT",
                )
            finally:
                self._at = None
            if found != "confirmed":
                self._subtask_stop(found)
                return None
            confirmations[field] = True
        return confirmations

    def release_round(self, round_no, total) -> str:
        """``"released"`` (Enter) or ``"stopped"`` (q, or the robot told to
        stop). Nothing else releases a round: no timeout, no page, no model."""
        self.keys.flush()  # a stale Enter must not release a round
        self.say("")
        self.say(self.t("sub_release", n=round_no, total=total))
        self._at = "releasing a round"
        try:
            found = self._wait_key(self._handler("released"))
        finally:
            self._at = None
        if found != "released":
            return self._subtask_stop(found)
        self.say(self.t("sub_released", n=round_no, total=total))
        return "released"

    # ------------------------------------------------------------ WAIT_HUMAN

    def _wait_reason(self) -> str:
        return wait_reason(self.orch)

    def wait_human(self) -> bool:
        """The run waits for a person. ``True``: resumed (here or by the
        page), the driver runs again; ``False``: the run was ended."""
        orch = self.orch
        if self._quit:
            orch.stop("terminal-quit-" + str(time.time_ns()))
            return False
        while True:
            self.keys.flush()  # a stale Enter must not resume or stop anything
            expected_seq = orch.journal.next_seq
            reason = self._wait_reason()
            happened_key, todo_key = WAIT_REASONS.get(reason, WAIT_OTHER)
            happened_key, fields = self._wait_detail(reason, happened_key)
            self.say("")
            self.say(
                self.line(
                    self.t(happened_key, **fields),
                    self.t(todo_key) + self._label_hint(),
                )
            )
            phase = self._subtask_phase_line()
            if phase:
                self.say(self.line(phase))
            if reason not in ROUTINE_WAITS:
                for code, detail in self._recent_notes():
                    self.say(self.t("recent_note", code=code, detail=detail))
            self._at = "waiting for a person"
            try:
                found = self._wait_key(
                    self._handler("enter"),
                    moved=lambda: orch.state != "WAIT_HUMAN",
                )
            finally:
                self._at = None
            if found == "enter" and reason in CONFIRM_REASONS:
                found = self._confirm(orch)
                if found == "no":
                    self.say(self.t("confirm_cancelled"))
                    continue
            if found == "moved":
                self.say(self.line(self.t("st_resumed_by_page")))
                return True
            if found in ("quit", "stopped"):
                if found == "quit":
                    orch.stop("terminal-quit-" + str(time.time_ns()))
                else:
                    orch.stop("terminal-stop-" + str(time.time_ns()))
                return False
            if self._resume(expected_seq):
                return True

    def _subtask_phase_line(self) -> str | None:
        """subtask_loop: where in the round the run stopped and whether the
        reverse reset ran (the person restores the scene by hand when not)."""
        sub = getattr(self.orch, "sub", None)
        if sub is None:
            return None
        key = {
            "forward": "sub_wait_forward",
            "reverse_pending": "sub_wait_after_forward",
            "reverse": "sub_wait_reverse",
        }.get(sub.phase, "sub_wait_idle")
        if sub.phase in ("forward", "reverse") and not sub.dispatched:
            # The loop was planned but no subtask was sent (a wait at the
            # first sleep): nothing of it moved.
            key = (
                "sub_wait_idle" if sub.phase == "forward" else "sub_wait_after_forward"
            )
        return self.t(key, n=sub.round_no)

    def _wait_detail(self, reason: str, key: str) -> tuple:
        """The sentence of a subtask_loop wait and its fields, from the latest
        note that explains it (``SUBTASK_WAIT_NOTES``); unknown fields show
        as ``?``. Other waits keep their sentence."""
        wanted = SUBTASK_WAIT_NOTES.get(reason)
        if not wanted:
            return key, {}
        detail = None
        for event in reversed(self.orch.journal.events):
            if (
                event.record == "note"
                and event.note is not None
                and event.note.code in wanted
            ):
                key = wanted[event.note.code]
                try:
                    detail = json.loads(event.note.detail)
                except ValueError:
                    detail = None
                break
        detail = detail if isinstance(detail, dict) else {}
        fields = {
            "loop": detail.get("loop", "?"),
            "reason": detail.get("reason", "?"),
            "index": detail.get("index", "?"),
            "attempt": detail.get("attempt", "?"),
            "by": detail.get("by", "?"),
            "free": detail.get("free_mib", "?"),
            "threshold": detail.get("threshold_mib", detail.get("need_mib", "?")),
            "mem": detail.get("mem_available_gib", "?"),
            "need": detail.get("need_gib", detail.get("need_mib", "?")),
            "limit": getattr(self.orch.config, "subtask_max_retries", "?"),
        }
        return key, fields

    def _confirm(self, orch) -> str:
        """Second step of a dangerous wait: ``keys.flush()`` first (a double
        press or keys typed ahead are dropped), then the prompt, then only
        ``y`` confirms; Enter and every other key are ignored. Returns
        ``"enter"`` (confirmed), ``"no"``, ``"quit"``, ``"stopped"`` or
        ``"moved"``."""
        self.keys.flush()
        self.say(self.line(self.t("confirm_resume")))

        def handle(key):
            if key in CONFIRM_KEYS:
                return "enter"
            if key in CANCEL_KEYS:
                return "no"
            if key in QUIT:
                return "quit"
            return None

        self._at = "confirming a resume"
        try:
            return self._wait_key(handle, moved=lambda: orch.state != "WAIT_HUMAN")
        finally:
            self._at = None

    def _recent_notes(self, count=3):
        return shown_notes(self.orch.notes)[-count:]

    def _resume(self, expected_seq: int) -> bool:
        try:
            health = self.robot.health()
        except Exception as exc:  # noqa: BLE001 - a health failure is red
            health = {"state": "red", "reasons": [str(exc)]}
        if health.get("state") != "ok":
            why = "; ".join(map(str, health.get("reasons", [])))[:200]
            self.say(self.t("health_red", why=why))
            return False
        found = control.resume_checked(
            self.orch,
            "terminal-resume-" + str(time.time_ns()),
            expected_seq=expected_seq,
            environment_handled=True,
            health_rechecked=True,
        )
        if not found.ok:
            self.say(self.t("resume_refused", code=found.code))
            return False
        self.say(self.line(self.t("st_resumed")))
        return True

    # ------------------------------------------------------------ banners

    def _subtask_mode(self) -> bool:
        orch = self.orch
        return orch is not None and getattr(orch, "sub", None) is not None

    def on_state(self, state: str, info: dict) -> None:
        reverse = (
            state in ("RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE")
            and self._subtask_mode()
        )
        if state != "FORWARD_ACTIVE" and not reverse:
            # Before anything else is shown or asked: no verdict key is read
            # once the episode is no longer running.
            with contextlib.suppress(Exception):
                self._close_verdict()
        with contextlib.suppress(Exception):  # a banner never disturbs a run
            self._banner(state, info)
        if state == "FORWARD_ACTIVE" or reverse:
            with contextlib.suppress(Exception):
                self._open_verdict(info.get("episode_id"), reverse=reverse)

    # ------------------------------------------------------------ verdict keys

    def _verdicts_allowed(self) -> bool:
        orch = self.orch
        return (
            orch is not None
            and getattr(orch, "intervention", None) is None
            and callable(getattr(orch, "episode_verdict", None))
        )

    def _open_verdict(self, episode_id, reverse=False) -> None:
        """A forward episode started: drop the keys typed before it, put the
        terminal in cbreak (single keys) and say which keys end it. A reverse
        subtask episode (``reverse``): the keys are read only to say they do
        nothing there."""
        if not episode_id or not self._verdicts_allowed():
            return
        with self._verdict_lock:
            if self._verdict_episode == episode_id:
                return
            self._close_verdict_locked()
            self.keys.flush()  # a key typed before the round never ends it
            cm = self.keys.cbreak()
            cm.__enter__()
            self._verdict_cbreak = cm
            self._verdict_episode = episode_id
            self._verdict_sent = None
            self._verdict_reverse = reverse
            self._reverse_scope = self.orch.reverse_human_scope() if reverse else None
        if reverse:
            self.say(self.line(self.t("sub_reverse_hint")))
        else:
            hint = "sub_forward_hint" if self._subtask_mode() else "verdict_hint"
            self.say(self.line(self.t(hint)))

    def _close_verdict(self) -> None:
        with self._verdict_lock:
            self._close_verdict_locked()

    def _close_verdict_locked(self) -> None:
        cm, self._verdict_cbreak = self._verdict_cbreak, None
        was_open = self._verdict_episode is not None
        self._verdict_episode = None
        self._verdict_sent = None
        self._verdict_reverse = False
        self._reverse_scope = None
        try:
            if cm is not None:
                cm.__exit__(None, None, None)
        finally:
            if was_open:
                # Keys typed during the episode's end, the home or a reset
                # answer nothing: the next prompt flushes again anyway.
                self.keys.flush()

    def poll_verdict(self) -> None:
        """One non-blocking look for a verdict key (the entry's heartbeat
        calls it every 50 ms); never raises: a heartbeat failure would stop
        the run."""
        with contextlib.suppress(Exception):
            self._poll_verdict()

    def _poll_verdict(self) -> None:
        with self._verdict_lock:
            episode = self._verdict_episode
            orch = self.orch
            if episode is None or self._verdict_sent is not None or orch is None:
                return
            ctx = orch.ctx
            reverse = self._verdict_reverse
            if (
                orch.state
                not in (
                    ("RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE")
                    if reverse
                    else ("FORWARD_ACTIVE",)
                )
                or ctx is None
                or ctx.episode_id != episode
            ):
                return
            if reverse and (
                self._reverse_scope is None
                or orch.reverse_human_scope() != self._reverse_scope
            ):
                # The backend can close before a slow home returns and its
                # state notification arrives. Stop reading keys immediately.
                self._close_verdict_locked()
                return
            key = self.keys.poll(0)
            if reverse and key == "m":
                scope = self._reverse_scope
                if scope is None:
                    return
                found = orch.reverse_human_request(
                    "terminal-human-" + str(time.time_ns()),
                    **scope,
                    source="terminal",
                    by=self.principal,
                )
                if found.ok:
                    self._verdict_sent = "human"
                self.say(
                    self.line(
                        self.t(
                            "sub_reverse_requested" if found.ok else "verdict_refused",
                            code=found.code,
                        )
                    )
                )
                return
            label = VERDICT_KEYS.get(key) if key is not None else None
            if label is None:
                return  # no key, or a key that means nothing here
            if reverse:
                # R9: the reverse loop restores the scene; s/f/d end nothing.
                ignored = True
            else:
                ignored = False
            if not ignored:
                found = orch.episode_verdict(
                    "terminal-verdict-" + str(time.time_ns()),
                    label,
                    source="terminal",
                    by=self.principal,
                )
                if found.ok:
                    self._verdict_sent = label
        if ignored:
            self.say(self.line(self.t("sub_reverse_ignored")))
            return
        if found.ok:
            said = self.t(VERDICT_WORDS[label])
            self.say(self.line(self.t("verdict_taken", verdict=said)))
        else:
            self.say(self.line(self.t("verdict_refused", code=found.code)))

    def _banner(self, state, info):
        orch = self.orch
        if orch is None:
            return
        total = orch.config.episodes
        reason = info.get("reason")
        if state == "FORWARD_ACTIVE" and reason != "inference_resumed":
            self.say(
                self.t(
                    "round_start",
                    n=orch.numbers["forward"],
                    total=total,
                    episode=info.get("episode_id"),
                )
            )
        elif state == "ROBOT_HOME" and info.get("episode_id"):
            self._round_end(info)
        else:
            row = status_row(state, reason)
            if row is not None and row.announce:
                self.say(
                    self.line(self.t(row.say, done=orch.completed_forward, total=total))
                )

    def _round_end(self, info):
        orch = self.orch
        episode = info["episode_id"]
        fields = {"n": orch.numbers["forward"], "total": orch.config.episodes}
        # With an early stop allowed, how the episode ended (and the step
        # count) would give the verdict away: shown after the label only.
        if orch.config.termination.allow_early_stop and not self._labelled(episode):
            self.say(
                self.t(
                    "round_end_hidden_labels" if self.labels else "round_end_hidden",
                    **fields,
                )
            )
            return
        trigger = getattr(orch.ctx, "early_trigger", None)
        if info.get("reason") == "subtask_early_check" and isinstance(trigger, dict):
            # subtask.early_check: the episode ended for the VLM's done check.
            # Only what triggered it is shown, never the VLM's answer.
            what = (
                self.t("sub_early_event", event=trigger.get("event_type"))
                if trigger.get("trigger") == "event"
                else self.t("sub_early_interval", every=trigger.get("every_steps"))
            )
            self.say(
                self.t(
                    "subtask_early_shown",
                    step=trigger.get("step"),
                    max_steps=_display_step_budget(orch.config),
                    trigger=what,
                )
            )
            return
        self.say(
            self.t(
                "subtask_end_shown"
                if getattr(orch.config, "reset_strategy", None) == "subtask_loop"
                else "round_end_shown",
                steps=getattr(orch.ctx, "steps_done", "?"),
                max_steps=_display_step_budget(orch.config),
                ended=self.t("ended_" + metrics.ended_by(info.get("reason"))),
                **fields,
            )
        )

    def tick(self) -> None:
        """Progress line while an episode runs (called by the heartbeat);
        never raises: a heartbeat failure would stop the run."""
        with contextlib.suppress(Exception):
            self._tick()

    def _tick(self) -> None:
        orch = self.orch
        if orch is None or orch.state != "FORWARD_ACTIVE" or orch.ctx is None:
            return
        episode = orch.ctx.episode_id
        if orch.config.termination.allow_early_stop and not self._labelled(episode):
            # A step count below the limit would show that the episode ended
            # early: before the label of this episode only "running", once.
            if self._progress != (episode, None):
                self._progress = (episode, None)
                self.say(
                    self.t(
                        "progress_hidden",
                        n=orch.numbers["forward"],
                        total=orch.config.episodes,
                    )
                )
            return
        mark = (episode, orch.ctx.steps_done)
        if mark != self._progress:
            self._progress = mark
            self.say(
                self.t(
                    "progress_subtask"
                    if getattr(orch.config, "reset_strategy", None) == "subtask_loop"
                    else "progress",
                    n=orch.numbers["forward"],
                    total=orch.config.episodes,
                    steps=mark[1],
                    max_steps=_display_step_budget(orch.config),
                )
            )

    # ------------------------------------------------------------ the end

    def finish(self, state: str | None = None) -> None:
        """Called once as the driver leaves: the fault summary of a locked
        run (code, meaning, note details, advice, log path)."""
        if self._finished or self.orch is None:
            return
        self._finished = True
        orch = self.orch
        state = state or orch.state
        if self._signum() is not None:
            # The run may end before the heartbeat saw the signal (a motion
            # refused under the stop flag locks it): the note still belongs.
            self.interrupted()
        if state != "FAULT_LOCKED":
            return
        code = ""
        for event in reversed(orch.journal.events):
            if event.record == "committed" and event.to_state == "FAULT_LOCKED":
                code = event.reason or ""
                break
        code = code or orch.halted or orch.journal.corrupt or "unknown"
        known = code in FAULT_CODES
        meaning = self.t("fault_" + code if known else "fault_unknown_meaning")
        advice = self.t("advice_" + code if known else "fault_unknown_advice")
        self._say_quietly("")
        self._say_quietly("=" * 60)
        self._say_quietly(self.t("fault_title", code=code))
        self._say_quietly(self.t("fault_meaning", meaning=meaning))
        details = shown_notes(orch.notes)[-6:]
        if orch.halted:
            details.append(("halted", str(orch.halted)[:300]))
        for note_code, detail in details:
            key = (
                "policy_error_detail"
                if note_code in POLICY_ERROR_NOTES
                else "fault_detail"
            )
            self._say_quietly(self.t(key, code=note_code, detail=detail))
        if self._signum() is not None:
            self._say_quietly(self.t("fault_ctrl_c"))
        self._say_quietly(self.t("fault_advice", advice=advice))
        self._say_quietly(self.t("fault_log", path=orch.journal.directory))
        self._say_quietly("=" * 60)

    def close(self, state: str | None = None) -> None:
        with contextlib.suppress(Exception):
            self._close_verdict()  # the terminal leaves cbreak
        try:
            self.finish(state)
        finally:
            if self._transport is not None:
                self._transport.close()
