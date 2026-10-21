"""The evaluation run of the marvel0 page: the demo client (terminal E) hosted by
the core, in ``--levi-mode off`` only, which is the client a person starts in a
terminal today. The page presses the keys a person presses and nothing else.

What this module may do, and nothing more:

* **Start the client the command form renders** (``command.render`` is the only
  producer of its argv; this module adds ``--no-display-images`` and nothing
  else), through ``hostchild.py`` (a stdlib launcher that owns the client's
  keyboard: a pty) inside a ``systemd-run --user --scope`` of its own, so a
  restart of the product does not signal it. The core keeps the pty's master;
  when the core ends the master closes and the kernel hangs the client up.
* **Press five keys** (``KEY_RULES``) and only in the state that allows each:
  Enter and ``q`` at the start gate; ``s``, ``f`` and (blind) ``q`` while it
  runs, once per episode; ``s``, ``f``, ``d`` at the step-limit prompt, once.
  **Never** ``s``, ``p``, ``i`` or a digit at the start gate: there ``s`` and
  ``p`` choose and start *another task* (2026-10-20: a stray ``s`` ran the
  screws task twice). Labels stay the operator's; the page only presses keys.
* **Hold** (SIGSTOP of the client's process group: no new target is sent, the
  arm stays at its last one) at any time, and **end** (``q`` at the gate;
  SIGKILL of a held, lost or still starting client, never SIGTERM + SIGCONT:
  a resumed step sends one stale action). After a hold nothing resumes.
* **Hold on its own** when the health monitor shows red on two distinct
  writes while the arm may move, when the session heartbeat goes stale, when
  the client runs another task or a second client connects to the policy
  server. A stale monitor only warns and blocks the next Enter.
* **Read passively**: the client's session file, its log, its pty's modes, its
  own process state (it is our child), the health file, the kernel's socket
  tables, the policy server's state as the deploy supervisor reports it. No
  port is connected to; no other process's ``/proc`` entry is read.

**What the gates are worth.** Every gate here stops a mistake, not another
program of the same account: anything that runs as this user can send the same
request, forge the page's headers or edit the files read here. The real
barriers are Franka Desk and the physical emergency stop. Nothing on the page
is an emergency stop; hold freezes the client only.

All of the machine is reached through ``Host``, which tests replace whole.
"""

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from levi import paths
from levi.live import gpumgr, jsonio
from levi.setup import probes
from levi.workspace import Workspace

from . import command, deploy, net, record, stack, status
from .errors import fail

SCHEMA = "levi.marvel0.run.v1"
RECORD_SCHEMA = "levi.marvel0.run_record.v1"
LOG_SCHEMA = "levi.marvel0.run_log.v1"
REL = ("marvel0", "run")
LOCK_FILE = ".marvel0-run.lock"  # next to the stack lock, lab/levi-hub/
HOSTCHILD = "hostchild.py"
UNIT_PREFIX = "levi-aeri-client-marvel0-"
# Seconds the client may take from its launch to the start gate (policy
# connection, ten seconds of camera warm-up, ``uv`` resolving its overlay).
START_TIMEOUT_S = 180.0
STALE_AFTER_S = (
    10.0  # eval_session.STALE_AFTER_S: a session file silent this long is stale
)
INTENT_GRACE_S = 30.0  # a record written, the launch not yet seen
HOLD_GRACE_S = 2.0  # SIGSTOP is taken up by the kernel a moment after killpg
CHALLENGE_TTL_S = 30.0
CHALLENGES_KEPT = 32
KEY_NOT_TAKEN_S = 5.0
KILL_WAIT_S = 3.0
TICK_S = 0.5
RED_WRITES = 2  # distinct monitor writes that show red before the page holds
MONITOR_STALE_S = 3.0
LOG_KEEP = 10
HISTORY_MAX_BYTES = 256 * 1024
# hostchild's own exit codes (the client's are 0-3, 5; see hostchild.py).
EXIT_NO_TTY = 97
EXIT_SPAWN = 98

PHRASES = {
    "start": "start client {group}",
    "end": "end client run",
    "exit_home": "exit and go home",
}
STATES = (
    "idle",
    "preflight",
    "starting",
    "at_gate",
    "homing",
    "running",
    "awaiting_label",
    "finishing",
    "done",
    "failed",
    "held",
    "lost",
)
LIVE = (
    "starting",
    "at_gate",
    "homing",
    "running",
    "awaiting_label",
    "finishing",
    "held",
    "lost",
)
ACTIONS = (
    "start",
    "enter",
    "hold",
    "success",
    "failure",
    "discard",
    "end_at_gate",
    "exit_home",
    "end",
)
# The only bytes the page ever writes to the client's keyboard: Enter, s, f, d,
# q. Never 0x03 (SIGINT), 0x1a, 0x1c, 0x1b, a digit, i or p. Per action: (the
# byte, the states it is offered in). ``s`` is not an action of the start gate
# and has no entry there: at the gate it means "RUN screws"
# (run_robotiq_client.py:127, TASK_KEYS; run_robotiq_client_collect_data.py:1390).
KEY_RULES = {
    "enter": (b"\r", ("at_gate",)),
    "end_at_gate": (b"q", ("at_gate",)),
    "success": (b"s", ("running", "awaiting_label")),
    "failure": (b"f", ("running", "awaiting_label")),
    "discard": (b"d", ("awaiting_label",)),
    "exit_home": (b"q", ("running",)),
}
KEY_BYTES = frozenset(byte for byte, _ in KEY_RULES.values())
# The challenge purpose of each key action (one token per click).
PURPOSES = {
    "enter": "enter",
    "end_at_gate": "end_at_gate",
    "success": "label",
    "failure": "label",
    "discard": "label",
    "exit_home": "exit_home",
}
LABELS = ("success", "failure", "discard")
# Actions after which the client homes the arm without any limit check
# (_go_home, run_robotiq_client.py:924: no min-z, no step limit, the gripper
# opens): the page says so beside each.
BLIND = ("enter", "success", "failure", "discard", "exit_home")
# The floor of each task the client measured, and the form of the prompt it is
# looked up by (command.TASK_TABLE_MIN_Z, the client's TASK_MIN_Z).
RESTRICTED_FLOORS = command.TASK_TABLE_MIN_Z

# --- refusals, reasons, checks, notices -----------------------------------------------

# code -> (HTTP status, message). No message holds a path.
REFUSALS = {
    "request_id_invalid": (
        400,
        "request_id is 8-80 characters of A-Z, a-z, 0-9 and . _ : -",
    ),
    "confirm_required": (400, "Type the confirmation phrase exactly: {phrase}"),
    "attest_required": (
        400,
        (
            "Tick every item: the scene is reset, the area is clear, a hand is on "
            "the physical e-stop"
        ),
    ),
    "run_disabled": (
        403,
        "Hosted runs are switched off: the control site file does not enable them",
    ),
    "not_product": (
        409,
        (
            "Only the product LEVI (levi-product.service in the main checkout) may "
            "act on the real arm; this instance only shows the run"
        ),
    ),
    "busy": (423, "Another action on the run is going on"),
    "lock_unusable": (503, "The machine-wide run lock could not be taken"),
    "state_dir_unusable": (412, "The folder for the run record cannot be used"),
    "run_unreadable": (
        503,
        "The run record could not be read, so no run is assumed absent",
    ),
    "request_id_used": (409, "That request id was already used for another request"),
    "challenge_invalid": (
        409,
        "That confirmation token is not one this page issued; ask for a new one",
    ),
    "challenge_expired": (
        409,
        "That confirmation token expired (30 s); ask for a new one",
    ),
    "challenge_used": (409, "That confirmation token was already used"),
    "challenge_stale": (
        409,
        "The run changed since that confirmation token was issued; ask for a new one",
    ),
    "preflight_failed": (409, "A start check fails: {first}"),
    "min_z_below_task_floor": (
        422,
        (
            "--min-z {min_z} is below {floor}, the floor the client measured for this "
            "task; the fingertips would reach the table"
        ),
    ),
    "start_failed": (502, "The client could not be launched; nothing runs"),
    "key_failed": (
        502,
        "The key could not be written to the client; it is not known to have arrived",
    ),
    "signal_failed": (502, "The client's process group could not be signalled"),
}

# Why an action is not offered (``actions[...].why_not``); the same codes
# refuse the request when a stale page sends it anyway (HTTP 409).
WHY_NOT = {
    "not_enabled": "Hosted runs are switched off in the control site file.",
    "not_product": "Only the product LEVI may act on the real arm; this one only "
    "shows the run.",
    "state_not_allowed": "Not offered while the run is {state}.",
    "run_live": "A client is already running; end it first.",
    "preflight_failed": "A start check fails: {first}.",
    "key_locked": "A key was already sent for this episode; wait for the client's "
    "next prompt.",
    "enter_locked": "Enter was already sent at this gate and is never sent twice; "
    "read the log, or hold and end.",
    "key_in_flight": "A key was just sent; wait for the client to react.",
    "monitor_red": "The health monitor shows a red light.",
    "monitor_not_green": "The health monitor is not fresh and green ({state}).",
    "already_held": "The client is already frozen.",
    "no_client": "There is no client process.",
}

# What the run is doing and why (``reason``). {episode} is the episode number the
# session file shows; {code} the client's exit code.
REASONS = {
    "no_run": "No client is running.",
    "checking": "The start checks are being read.",
    "spawning": "The client is being launched.",
    "waiting_for_session": "The client is starting: it connects to the policy "
    "server and warms up the cameras. Nothing moves yet.",
    "gate_not_confirmed": "The client reports the start gate, but its banner or its "
    "keyboard mode is not seen yet; no key is offered.",
    "waiting_at_gate": "The client waits at the start gate for episode {episode}. "
    "Enter makes the arm go home without any limit check, then the policy runs.",
    "homing": "The arm goes home (30 steps, no limit check). Only hold works.",
    "running": "Episode {episode} is running.",
    "judge_prompt": "The step limit was reached; the client waits for the label. "
    "Success, failure or discard sends the arm home without any limit check.",
    "episode_closing": "Episode {episode} ended; the client saves it. The next "
    "motion is the arm going home.",
    "session_ended": "The client's session ended; it is closing.",
    "closing_env": "The client is closing the cameras.",
    "end_requested": "q was sent at the gate; the client is leaving.",
    "held_person": "Frozen on your request. The arm keeps the last target it was "
    "sent. Nothing resumes: the only action is end.",
    "held_monitor_red": "Frozen by the page: the health monitor showed red on two "
    "writes while the arm could move. Nothing resumes.",
    "held_heartbeat_stale": "Frozen by the page: the client's session file stopped "
    "updating. Nothing resumes.",
    "held_task_changed": "Frozen by the page: the client switched to another task "
    "(a key at the start gate). Nothing resumes.",
    "held_extra_connection": "Frozen by the page: another client is connected to "
    "the policy server. Nothing resumes.",
    "held_host_restarted": "Frozen by the page after the service restarted; the "
    "client has no keyboard any more. Nothing resumes.",
    "held_session_changed": "Frozen by the page: the client's session file belongs "
    "to another session. Nothing resumes.",
    "held_unexpected_state": "Frozen by the page: the client reports a state this "
    "page does not host. Nothing resumes.",
    "held_resumed_elsewhere": "Frozen again: something outside this page continued "
    "the client. Nothing resumes.",
    "held_elsewhere": "The client is stopped, but not by this page. Nothing resumes: "
    "the only action is end.",
    "user_quit": "The client ended at the start gate (q); no motion.",
    "target_reached": "The client reached its episode target and ended.",
    "ended_by_page": "The client was ended by this page.",
    "no_gate_in_time": "The client did not reach the start gate in time and was "
    "ended; the log shows why.",
    "exit_code": "The client exited with code {code}; the log shows why.",
    "exception": "The client stopped with an error; the log shows it.",
    "interrupted": "The client was interrupted and closed.",
    "exit_unexplained": "The client exited without saying why; the log shows what "
    "it did last.",
    "hangup": "The client was hung up (its keyboard went away).",
    "killed_from_outside": "The client was killed by a signal that did not come "
    "from this page.",
    "no_tty": "The launcher could not give the client a keyboard; nothing ran.",
    "spawn_failed": "The client could not be started; the log shows why.",
    "host_restarted": "The service restarted while a client was recorded; nothing "
    "was resumed.",
    "intent_only": "The run was recorded but no client was launched (the service "
    "stopped in between).",
    "task_changed": "The client runs another task than this page started; it is "
    "being frozen.",
    "session_changed": "The client's session file belongs to another session.",
    "heartbeat_stale": "The client's session file stopped updating; it is being "
    "frozen.",
    "resumed_elsewhere": "The client was continued by something outside this page; "
    "it is being frozen again.",
    "process_unreadable": "The client's process state could not be read.",
    "unexpected_state": "The client reports a state this page does not host.",
}

# The start checks. Form-independent ones are in ``GET /run``; the ones that
# need the form (``policy_matches``, ``min_z``) come with the start challenge.
CHECK_OK = {
    "client_enabled": "Hosted runs are enabled in the control site file.",
    "product": "This is the product LEVI.",
    "run_idle": "No client is running.",
    "policy_ready": "The policy server of this page is ready.",
    "policy_matches": "The policy server serves the model and RTC setting of the form.",
    "no_connection": "Nobody is connected to the policy server.",
    "no_session": "No evaluation session is active.",
    "arm_lock": "The arm lock is free.",
    "monitor_green": "The health monitor is fresh and green.",
    "no_aeri": "No automatic evaluation run is alive.",
    "rollout_writable": "The rollout folder can be written.",
    "min_z": "The z floor is not below the floor of this task.",
    "robot_server": "Something listens on the robot server's port.",
}
CHECK_REFUSE = {
    "client_enabled": "Hosted runs are switched off in the control site file.",
    "product": "Only the product LEVI may act on the real arm; this one only shows "
    "the run.",
    "run_idle": "A client is still running; end it first.",
    "policy_ready": "The policy server of this page is not ready ({state}).",
    "policy_matches": "The server serves {served} (RTC {served_rtc}); the form "
    "asks for {wanted} (RTC {wanted_rtc}).",
    "no_connection": "{count} connection(s) to the policy server; another client "
    "may be running.",
    "no_session": "An evaluation session is active ({count}); end it first.",
    "arm_lock": "The arm lock is held: something else uses the arm.",
    "monitor_green": "The health monitor is not fresh and green ({state}).",
    "no_aeri": "An automatic evaluation run is alive.",
    "rollout_writable": "The rollout folder is missing or cannot be written.",
    "min_z": "--min-z {min_z} is below {floor}, the floor the client measured for "
    "this task.",
    "robot_server": "Nothing listens on the robot server's port; the client will "
    "stop at its first step.",
}
CHECK_UNKNOWN = {
    "policy_ready": "The policy server's state could not be read.",
    "no_connection": "The socket tables could not be read, so a client is not "
    "ruled out.",
    "no_session": "The evaluation sessions could not be read, so none is ruled out.",
    "arm_lock": "Who holds the arm lock could not be read.",
    "no_aeri": "The automatic evaluation registry could not be read.",
    "monitor_green": "The health monitor could not be read.",
    "robot_server": "The socket tables could not be read.",
}
# These checks never refuse: a hint on the page.
WARN_CHECKS = ("robot_server",)
NOTICES = {
    "gates_scope": "These checks stop mistakes, not another program of the same "
    "account. The real barriers are Franka Desk and the physical emergency stop; "
    "keep a hand on it.",
    "hold_not_estop": "Hold is not an emergency stop. It freezes the client only: "
    "the arm keeps the last target it was sent.",
    "blind_home": "Enter, success, failure, discard and exit send the arm home "
    "without any limit check; hold freezes the client, nothing resumes.",
    "monitor_stale": "The health monitor is not fresh: the next Enter is blocked.",
    "key_not_taken": "The last key was sent and the client has not reacted. It is "
    "never sent twice; read the log.",
    "extra_connection": "More than one connection to the policy server: another "
    "client may be running.",
    "orphan_dirs": "A killed client leaves its last episode folder without a "
    "'stopped_at'; check it before training on it.",
}


class Refuse(Exception):
    """A request that does not go on: stable ``code``, HTTP ``status``, English
    ``message`` and the facts as ``extra``."""

    def __init__(self, code, **extra):
        if code in REFUSALS:
            self.status, text = REFUSALS[code]
        else:
            self.status, text = 409, WHY_NOT[code]
        self.code = code
        self.extra = extra
        try:
            self.message = text.format(**extra)
        except (KeyError, IndexError):
            self.message = text
        super().__init__(f"{code}: {self.message}")


def http(exc: Refuse):
    """``exc`` as the answer of a route (``errors.fail``)."""
    return fail(exc.status, exc.code, exc.message, **exc.extra)


# --- small things ---------------------------------------------------------------------


def sanitize_task_name(text: str) -> str:
    """The client's task folder name, byte for byte
    (``rollout_writer.sanitize_task_name``, rollout_writer.py:86): the session
    file is ``<group>__<this>.json``."""
    normalized = text.strip().replace(" ", "_")
    normalized = re.sub(r"[\\/]+", "_", normalized)
    normalized = re.sub(r"[^\w\-]+", "_", normalized, flags=re.UNICODE)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        raise ValueError("Empty task description; cannot build a directory name.")
    return normalized


def canonical_prompt(prompt: str) -> str:
    """The prompt the client files its session under: one of its two known
    tasks when the prompt normalises to it (``_canonical_prompt``,
    run_robotiq_client.py:747), else the prompt as given."""
    for task in RESTRICTED_FLOORS:
        if command._normalise(task) == command._normalise(prompt):
            return task
    return prompt


def session_name(group: str, prompt: str) -> str:
    return f"{group}__{sanitize_task_name(canonical_prompt(prompt))}.json"


def task_floor(prompt: str) -> float | None:
    """The floor the client measured for ``prompt`` when it is one of the two
    tasks that have one, else None (an unlisted task only gets a warning)."""
    for task, floor in RESTRICTED_FLOORS.items():
        if command._normalise(task) == command._normalise(prompt):
            return floor
    return None


def min_z_problem(prompt: str, min_z: float) -> dict | None:
    """``{"floor", "min_z"}`` when an explicit ``--min-z`` is below the floor of
    a restricted task. The client lets an explicit ``--min-z`` beat its task
    table (``apply_task_min_z``, run_robotiq_client.py:834: RIG-08), so
    ``command.validate`` alone would hand the screws task a floor of 0.025."""
    floor = task_floor(prompt)
    if floor is not None and min_z < floor:
        return {"floor": floor, "min_z": command.marvel0_site.number(min_z)}
    return None


def child_env(environ) -> dict:
    """The client's whole environment: an allowlist, never a copy of ours. No
    ``LEVI_*`` (the UI token), no Hugging Face token, no ROS, no conda, no
    display. ``UV_OFFLINE`` keeps ``uv run --with`` from reaching a network
    that the allowlist has no proxy for; ``PYTHONUNBUFFERED`` because the
    output goes to a file, not a terminal."""
    home = environ.get("HOME") or ""
    if not home.startswith("/"):
        raise Refuse("start_failed")
    return {
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "USER": environ.get("USER") or Path(home).name,
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1",
        "UV_OFFLINE": "1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }


def hosted_argv(argv: list) -> list:
    """The argv ``command.render`` produced, plus the one switch a hosted run
    needs: no OpenCV window (``--display-images`` defaults on and there is no
    display; setup.md names the flag)."""
    return [*argv, "--no-display-images"]


BUS_ENV = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def scope_launch(unit, hostchild, spec, python, env, environ) -> tuple:
    """``(argv, env of the process)`` of the launch: a transient scope of its
    own (not the product's cgroup), then ``env -i`` with the allowlist, then
    the launcher. ``systemd-run --scope`` replaces itself with the command, and
    ``env`` with the launcher, so the Popen's pid is the launcher's.

    ``systemd-run`` itself needs the user bus (``XDG_RUNTIME_DIR``, the session
    bus address) and nothing else, so that is all its own environment holds; the
    client's environment is the allowlist ``env -i`` sets, whatever the caller's
    environment was."""
    argv = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        "--description=FR3 evaluation client (terminal E), hosted by the marvel0 page",
        "--",
        "/usr/bin/env",
        "-i",
        *[f"{name}={value}" for name, value in env.items()],
        python,
        "-I",
        hostchild,
        spec,
    ]
    return argv, {name: environ[name] for name in BUS_ENV if environ.get(name)}


def direct_launch(unit, hostchild, spec, python, env, environ) -> tuple:
    """The same launch without a scope (tests and the fake profile): the
    launcher with the allowlist as its environment."""
    return [python, "-I", hostchild, spec], dict(env)


def proc_state(pid: int) -> str | None:
    """The state letter of one of OUR OWN child processes from its stat file
    (``R``, ``S``, ``T`` stopped, ``Z`` zombie...), or None when it is gone.
    Only ever called with the pid of the launcher this module started."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


# --- the client's log ------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# The lines the state machine reads (strings of the client, pinned by
# tests/marvel0/fixtures/client_prompts.json and scripts/check_client_prompts.py).
GATE_RE = re.compile(r"=== ready \| ep (\d+) \|")
ENDED_RE = re.compile(r"^Episode (\d+) ended: (\w+)")
JUDGE_RE = re.compile(r"hit the (\d+)-step limit\. Judge it")
HOME_RE = re.compile(r"^Homing over (\d+) steps")
HOME_DONE = "Home reached."
CLOSING = "Closing env."
NO_TTY_RE = re.compile(r"\(no tty: choosing")
REFUSED = "REFUSING TO START"
TRACEBACK = "Traceback (most recent call last)"
INTERRUPTS = (
    "Interrupt received, stopping after this step",
    "SIGTERM received, stopping after this step",
    "Second interrupt, exiting hard",
    "Second termination request",
)
GATE_REASON = (
    "waiting at the start gate"  # the session reason at the gate (collect_data:1372)
)


@dataclass(frozen=True)
class LogView:
    """What the client's log shows, as far as the state machine needs it."""

    gate_ep: int | None = None  # the last "=== ready | ep N" banner
    ended_ep: int | None = None  # the last "Episode N ended: <outcome>"
    ended_outcome: str | None = None
    judge_ep: int | None = None  # the episode whose step-limit prompt was shown
    home_over: int = 0
    home_reached: int = 0
    closing: bool = False
    no_tty: bool = False
    refused: bool = False
    traceback: bool = False
    interrupted: bool = False
    lines: int = 0


class LogScan:
    """The client's log read incrementally: ``feed`` the bytes appended since
    the last call; ``view`` is what has been seen. Partial lines wait for their
    end. A line the client words differently is simply not matched, which makes
    the state machine less sure (it degrades to ``starting`` or ``running``),
    never more."""

    def __init__(self):
        self.offset = 0
        self._carry = b""
        self._v = LogView()

    def feed(self, data: bytes) -> None:
        self.offset += len(data)
        data = self._carry + data
        *whole, self._carry = data.split(b"\n")
        self._carry = self._carry[-4096:]
        for raw in whole:
            self._line(_ANSI.sub("", raw.decode("utf-8", "replace")).strip())

    def _line(self, text: str) -> None:
        if not text:
            return
        v = self._v
        changes: dict = {"lines": v.lines + 1}
        if found := GATE_RE.search(text):
            changes["gate_ep"] = int(found.group(1))
        elif found := ENDED_RE.match(text):
            changes.update(ended_ep=int(found.group(1)), ended_outcome=found.group(2))
        elif JUDGE_RE.search(text):
            if v.ended_outcome == "timeout":
                changes["judge_ep"] = v.ended_ep
        elif HOME_RE.match(text):
            changes["home_over"] = v.home_over + 1
        elif text.startswith(HOME_DONE):
            changes["home_reached"] = v.home_reached + 1
        elif text.startswith(CLOSING):
            changes["closing"] = True
        elif NO_TTY_RE.search(text):
            changes["no_tty"] = True
        elif REFUSED in text:
            changes["refused"] = True
        elif text.startswith(TRACEBACK):
            changes["traceback"] = True
        elif any(text.startswith(mark) for mark in INTERRUPTS):
            changes["interrupted"] = True
        self._v = dataclasses.replace(v, **changes)

    @property
    def view(self) -> LogView:
        return self._v


# --- the state ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Monitor:
    """The health monitor as the run reads it: ``state`` is stack.read_health's
    (ok, red, offline, missing, unknown); ``stamp`` identifies one write."""

    state: str = "unknown"
    age_s: float | None = None
    stamp: float | None = None


@dataclass
class Facts:
    """What ``derive`` decides from; nothing in it is read there."""

    now: float
    record: dict | None  # the run record, None when there never was a run
    proc: str = "gone"  # running | stopped | gone | unknown (the launcher)
    exit: dict | None = None  # {"code", "signal"} once the launcher is gone
    keyboard: bool = True  # this process holds the pty master
    session: dict | None = None  # the client's session file, parsed
    session_age_s: float | None = None
    log: LogView = field(default_factory=LogView)
    icanon: bool | None = None  # the pty's canonical mode; False = cbreak is on
    started_age_s: float | None = None
    preflighting: bool = False  # a POST start is checking right now
    stale_after_s: float = STALE_AFTER_S
    start_timeout_s: float = START_TIMEOUT_S


@dataclass(frozen=True)
class Derived:
    state: str
    reason: str
    may_move: bool = False


def session_episode(session) -> dict:
    """``episode`` of a session file with every number checked."""
    raw = (session or {}).get("episode")
    raw = raw if isinstance(raw, dict) else {}

    def whole(key):
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return {
        "no": whole("no"),
        "target": whole("target"),
        "counted": whole("counted"),
        "step": whole("step"),
        "max_steps": whole("max_steps"),
    }


def _ended(f: Facts, rec: dict, ex: dict) -> Derived:
    """Done or failed, for a client whose launcher is gone."""
    code, sig = ex.get("code"), ex.get("signal")
    session = f.session or {}
    s_state, s_reason = session.get("state"), str(session.get("reason") or "")
    if rec.get("kill_reason"):
        return Derived("failed", rec["kill_reason"])
    if rec.get("killed"):
        return Derived("done", "ended_by_page")
    if code == EXIT_NO_TTY:
        return Derived("failed", "no_tty")
    if code == EXIT_SPAWN:
        return Derived("failed", "spawn_failed")
    if sig == signal.SIGHUP:
        return Derived("failed", "hangup")
    if sig is not None:
        return Derived("failed", "killed_from_outside")
    if code == 0:
        if s_state == "finished" or s_reason == "target reached":
            return Derived("done", "target_reached")
        if rec.get("end_requested") or (
            s_state == "stopped" and s_reason == "user_quit"
        ):
            return Derived("done", "user_quit")
        if s_state == "fault":
            return Derived("failed", "exception")
        if s_reason == "interrupted":
            return Derived("failed", "interrupted")
        return Derived("failed", "exit_unexplained")
    return Derived("failed", "exit_code")


def _session_trouble(f: Facts, rec: dict) -> str | None:
    """Why the client's session file is not the one this page started."""
    session = f.session
    if session is None:
        return None
    if session.get("state") == "stopped" and session.get("reason") == "task_changed":
        return "task_changed"
    if session.get("group") != rec.get("group") or session.get(
        "task_folder"
    ) != rec.get("task_folder"):
        return "task_changed"
    wanted = rec.get("session_id")
    if wanted and session.get("session_id") != wanted:
        return "session_changed"
    return None


def derive(f: Facts) -> Derived:
    """The named state of the run, from the facts alone. The rules run in this
    order and the first that applies wins. A text the client words differently
    never makes the state *more* permissive: the state falls back to
    ``starting`` (no key offered) or ``running`` (labels only)."""
    rec = f.record
    if f.preflighting and (rec is None or rec.get("phase") == "ended"):
        return Derived("preflight", "checking")
    if rec is None:
        return Derived("idle", "no_run")
    phase = rec.get("phase")
    if phase == "ended" or (phase == "started" and f.proc == "gone"):
        ex = rec.get("exit") or f.exit
        if ex is None:
            return Derived("failed", "host_restarted")
        return _ended(f, rec, ex)
    if phase == "intent":
        age = f.now - float(rec.get("created_epoch") or f.now)
        if f.proc == "gone":
            if age < INTENT_GRACE_S:
                return Derived("starting", "spawning")
            return Derived("failed", "intent_only")
    # The launcher is alive from here on (running or stopped), or unreadable.
    held_at = rec.get("held_at")
    if f.proc == "stopped" or (
        held_at is not None
        and f.proc == "running"
        and f.now - float(held_at) < HOLD_GRACE_S
    ):
        return Derived("held", "held_" + str(rec.get("held_by") or "elsewhere"))
    if held_at is not None and f.proc == "running":
        return Derived("lost", "resumed_elsewhere", True)
    if f.proc == "unknown":
        return Derived("lost", "process_unreadable", True)
    if not f.keyboard:
        return Derived("lost", "host_restarted", True)
    if rec.get("end_requested"):
        return Derived("finishing", "end_requested")
    session = f.session
    if session is None:
        if (f.started_age_s or 0.0) > f.start_timeout_s:
            # No motion has happened before the gate: the page ends it (SIGKILL).
            return Derived("failed", "no_gate_in_time")
        return Derived("starting", "waiting_for_session")
    trouble = _session_trouble(f, rec)
    if trouble:
        return Derived("lost", trouble, True)
    state = session.get("state")
    if state in status.ENDED_STATES:
        return Derived("finishing", "session_ended")
    if f.session_age_s is None or f.session_age_s > f.stale_after_s:
        return Derived("lost", "heartbeat_stale", True)
    if f.log.closing:
        return Derived("finishing", "closing_env")
    no = session_episode(session)["no"]
    if state == "standby":
        sure = (
            session.get("reason") == GATE_REASON
            and f.icanon is False
            and f.log.gate_ep == no
        )
        return (
            Derived("at_gate", "waiting_at_gate")
            if sure
            else Derived("starting", "gate_not_confirmed")
        )
    if state == "homing":
        return Derived("homing", "homing", True)
    if state == "running":
        if f.log.judge_ep == no:
            return Derived("awaiting_label", "judge_prompt")
        if f.log.ended_ep == no:
            return Derived("finishing", "episode_closing", True)
        return Derived("running", "running", True)
    return Derived("lost", "unexpected_state", True)


# --- what is offered ---------------------------------------------------------------------


@dataclass
class Context:
    """What the document needs besides the facts; all of it read by the
    supervisor, none of it decided here."""

    profile: str = "real"
    profile_code: str = "profile.product"
    enabled: bool = True
    monitor: Monitor = field(default_factory=Monitor)
    preflight: list = field(default_factory=list)
    seq: int = 0
    label_locked: bool = False  # a label or exit key was sent for this episode
    enter_locked: bool = False  # Enter was sent at this very gate
    keyed: bool = False  # a key was sent and the state has not changed since
    key_age_s: float | None = None
    armed: dict | None = None  # the newest open challenge: purpose, expires_at
    clients: int | None = None  # connections the policy server has accepted


def _fill(text: str, params: dict) -> str:
    try:
        return text.format(**params)
    except (KeyError, IndexError):
        return text


def check_row(code: str, ok, **params) -> dict:
    """One start check as the page lists it. ``ok`` True passes, False refuses,
    None means it could not be told (that refuses too: a source that cannot
    be read is never taken for a good one). A check in ``WARN_CHECKS`` shows
    ``warn`` instead of refusing."""
    if ok is True:
        level, text = "ok", CHECK_OK[code]
    elif code in WARN_CHECKS:
        level = "warn"
        text = (CHECK_REFUSE if ok is False else CHECK_UNKNOWN)[code]
    elif ok is False:
        level, text = "refuse", CHECK_REFUSE[code]
    else:
        level, text = "unknown", CHECK_UNKNOWN.get(code, CHECK_REFUSE[code])
    return {"code": code, "level": level, "text": _fill(text, params), "params": params}


def _why(code: str, **params) -> dict:
    try:
        text = WHY_NOT[code].format(**params)
    except (KeyError, IndexError):
        text = WHY_NOT[code]
    return {"code": code, "text": text, "params": params}


def _entry(code=None, blind=False, **params) -> dict:
    return {
        "allowed": code is None,
        "why_not": None if code is None else _why(code, **params),
        "blind": blind,
    }


def _monitor_block(c: Context, strict: bool):
    """``(code, params)`` of the monitor's veto on a motion key, or ``(None,
    {})``. Red vetoes every key that makes the arm move; a monitor that is not
    fresh and green only vetoes Enter (a hiccup must not strand an episode
    that only a label can end)."""
    state = c.monitor.state
    if state == "red":
        return "monitor_red", {}
    if strict and state != "ok":
        return "monitor_not_green", {"state": state}
    return None, {}


def _start_entry(d: Derived, c: Context) -> dict:
    if not c.enabled:
        return _entry("not_enabled")
    if c.profile != "real":
        return _entry("not_product")
    if d.state in LIVE or d.state == "preflight":
        return _entry("run_live")
    failing = [r for r in c.preflight if r["level"] in ("refuse", "unknown")]
    if failing:
        return _entry("preflight_failed", first=failing[0]["code"])
    return _entry()


def _key_entry(name: str, d: Derived, c: Context) -> dict:
    _, states = KEY_RULES[name]
    blind = name in BLIND
    if c.profile != "real":
        return _entry("not_product", blind)
    if d.state not in states:
        return _entry("state_not_allowed", blind, state=d.state)
    if name == "enter" and c.enter_locked:
        return _entry("enter_locked", blind)
    if name == "end_at_gate" and c.enter_locked:
        # q behind an Enter that was not taken yet would be read by the running
        # loop as "quit": start, home, quit, home.
        return _entry("enter_locked", blind)
    if name in (*LABELS, "exit_home") and c.label_locked:
        return _entry("key_locked", blind)
    if c.keyed:
        return _entry("key_in_flight", blind)
    if name != "end_at_gate":
        code, params = _monitor_block(c, strict=name == "enter")
        if code:
            return _entry(code, blind, **params)
    return _entry(None, blind)


def actions(d: Derived, c: Context) -> dict:
    """``{action: {allowed, why_not, blind}}`` for all nine. A stop-direction
    action (hold, end) never depends on the monitor, a lock or a challenge."""
    out = {"start": _start_entry(d, c)}
    for name in KEY_RULES:
        out[name] = _key_entry(name, d, c)
    if d.state == "held":
        out["hold"] = _entry("already_held")
    elif d.state in LIVE:
        out["hold"] = _entry()
    else:
        out["hold"] = _entry("no_client")
    if d.state in ("held", "lost", "starting"):
        out["end"] = _entry()
    elif d.state in LIVE:
        out["end"] = _entry("state_not_allowed", state=d.state)
    else:
        out["end"] = _entry("no_client")
    return {name: out[name] for name in ACTIONS}


def _text(table: dict, key: str, /, **params) -> str:
    return _fill(table.get(key, key), params)


def _iso(epoch) -> str | None:
    return record.iso(epoch) if isinstance(epoch, (int, float)) else None


def _session_fr3(session) -> dict | None:
    raw = (session or {}).get("fr3")
    if not isinstance(raw, dict):
        return None

    def flag(key):
        value = raw.get(key)
        return value if isinstance(value, bool) else None

    age = raw.get("state_age_s")
    mode = raw.get("robot_mode_name")
    return {
        "ok": flag("ok"),
        "hardware_active": flag("hardware_active"),
        "robot_mode_name": status._text(mode, 40) if isinstance(mode, str) else None,
        "state_age_s": round(age, 1)
        if isinstance(age, (int, float)) and not isinstance(age, bool)
        else None,
    }


def _last_episode(session) -> dict | None:
    raw = (session or {}).get("last_episode")
    if not isinstance(raw, dict):
        return None
    outcome = raw.get("outcome")
    steps = raw.get("steps")
    seconds = raw.get("duration_s")
    return {
        "outcome": status._text(outcome, 24) if isinstance(outcome, str) else None,
        "steps": steps
        if isinstance(steps, int) and not isinstance(steps, bool)
        else None,
        "duration_s": round(seconds, 1)
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
        else None,
    }


def _run_view(f: Facts) -> dict | None:
    rec = f.record
    if not rec:
        return None
    began = rec.get("created_epoch")
    return {
        "run_id": rec.get("run_id"),
        "group": rec.get("group"),
        "task_folder": rec.get("task_folder"),
        "model_id": rec.get("model_id"),
        "rtc": rec.get("rtc"),
        "prompt": rec.get("prompt"),
        "started_at": _iso(began),
        "age_s": round(f.now - began, 1) if isinstance(began, (int, float)) else None,
    }


def notices(f: Facts, d: Derived, c: Context) -> list:
    codes = ["gates_scope"]
    if d.state in LIVE:
        codes.append("hold_not_estop")
    if d.state in ("at_gate", "running", "awaiting_label"):
        codes.append("blind_home")
    if d.state in LIVE and c.monitor.state not in ("ok", "red"):
        codes.append("monitor_stale")
    if (
        d.state == "at_gate"
        and c.enter_locked
        and (c.key_age_s or 0.0) > KEY_NOT_TAKEN_S
    ):
        codes.append("key_not_taken")
    if d.state in LIVE and (c.clients or 0) > 1:
        codes.append("extra_connection")
    rec = f.record or {}
    ex = rec.get("exit") or f.exit or {}
    if d.state in ("failed", "done") and (
        rec.get("killed") or (ex.get("signal") and not rec.get("kill_reason"))
    ):
        # (a client killed before its gate had no episode to leave behind)
        codes.append("orphan_dirs")
    return [{"code": code, "text": NOTICES[code]} for code in codes]


def document(f: Facts, d: Derived, c: Context) -> dict:
    """The ``levi.marvel0.run.v1`` document (no path in it)."""
    rec = f.record or {}
    live = d.state in LIVE
    episode = session_episode(f.session)
    params = {
        "episode": episode["no"],
        "code": (rec.get("exit") or f.exit or {}).get("code"),
    }
    mine = bool(f.session) and d.state not in ("idle", "preflight")
    held = None
    if d.state == "held":
        held = {
            "by": rec.get("held_by") or "elsewhere",
            "at": _iso(rec.get("held_at")),
        }
    ex = rec.get("exit") or f.exit
    acts = actions(d, c)
    return {
        "schema": SCHEMA,
        "sampled_at": round(f.now, 3),
        "profile": c.profile,
        "profile_code": c.profile_code,
        "enabled": c.enabled,
        "state": d.state,
        "state_seq": c.seq,
        "reason": {"code": d.reason, "text": _text(REASONS, d.reason, **params)},
        "may_move": d.may_move,
        "run": _run_view(f),
        "episode": episode if mine else None,
        "last_episode": _last_episode(f.session) if mine else None,
        "fr3": _session_fr3(f.session) if mine and live else None,
        "heartbeat": {
            "age_s": round(f.session_age_s, 1) if f.session_age_s is not None else None
        }
        if live
        else None,
        "held": held,
        "exit": {"code": ex.get("code"), "signal": ex.get("signal")}
        if ex and d.state in ("done", "failed")
        else None,
        "monitor": {
            "state": c.monitor.state,
            "age_s": round(c.monitor.age_s, 1) if c.monitor.age_s is not None else None,
        },
        "clients": c.clients,
        "preflight": list(c.preflight),
        "can_start": acts["start"]["allowed"],
        "actions": acts,
        "keys": {name: _key_name(byte) for name, (byte, _) in KEY_RULES.items()},
        "armed": c.armed,
        "phrases": dict(PHRASES),
        "notices": notices(f, d, c),
    }


def _key_name(byte: bytes) -> str:
    return "Enter" if byte == b"\r" else byte.decode()


# --- the one-time confirmation -----------------------------------------------------------


class Challenges:
    """The 30-second tokens a page asks for before an action that moves the
    arm or starts the client. A token is bound to what the page saw (the run,
    the session, the episode number, the state sequence) and is consumed by
    its first use, so a double click, a stale tab and a replayed request do
    not act twice. **It stops mistakes only**: a script asks for a token and
    uses it in two requests. Memory only; a restart voids all of them."""

    def __init__(self, clock=time.monotonic, wall=time.time, ttl=CHALLENGE_TTL_S):
        self._clock, self._wall, self._ttl = clock, wall, ttl
        self._lock = threading.Lock()
        self._open: dict = {}  # token -> entry
        self._closed: dict = {}  # token -> "used" | "expired", bounded

    def issue(self, purpose: str, bind: dict) -> dict:
        token = secrets.token_urlsafe(16)  # 128 bits
        entry = {
            "purpose": purpose,
            "bind": dict(bind),
            "deadline": self._clock() + self._ttl,
            "expires_at": round(self._wall() + self._ttl, 3),
        }
        with self._lock:
            self._forget()
            self._open[token] = entry
            while len(self._open) > CHALLENGES_KEPT:
                self._open.pop(next(iter(self._open)))
        return {
            "challenge": token,
            "expires_at": entry["expires_at"],
            "ttl_s": self._ttl,
        }

    def consume(self, token, purpose: str, bind: dict) -> None:
        """Use ``token`` once, or raise ``Refuse`` with the reason. A token
        that does not fit is spent too: a stale one is of no use."""
        with self._lock:
            self._forget()
            entry = self._open.pop(token, None) if isinstance(token, str) else None
            if entry is None:
                closed = self._closed.get(token) if isinstance(token, str) else None
                raise Refuse(f"challenge_{closed}" if closed else "challenge_invalid")
            self._close(token, "used")
        if entry["purpose"] != purpose:
            raise Refuse("challenge_invalid")
        if entry["bind"] != bind:
            raise Refuse("challenge_stale")

    def newest(self) -> dict | None:
        """``{"purpose", "expires_at"}`` of the newest token still open."""
        with self._lock:
            self._forget()
            if not self._open:
                return None
            entry = next(reversed(self._open.values()))
            return {"purpose": entry["purpose"], "expires_at": entry["expires_at"]}

    def void(self) -> None:
        with self._lock:
            self._open.clear()

    def _close(self, token: str, how: str) -> None:
        self._closed[token] = how
        while len(self._closed) > CHALLENGES_KEPT * 4:
            self._closed.pop(next(iter(self._closed)))

    def _forget(self) -> None:
        """Move the expired tokens to ``_closed`` (caller holds the lock)."""
        now = self._clock()
        for token in [t for t, e in self._open.items() if e["deadline"] < now]:
            del self._open[token]
            self._close(token, "expired")


# --- the host ------------------------------------------------------------------------------

RESULT_SCHEMA = "levi.marvel0.run_result.v1"
LOG_NAME = re.compile(r"client-\d{8}T\d{6}Z(?:-\d{1,2})?")
LOG_SCAN_MAX = 8 << 20  # bytes of the client's log read per look, at most


def _hub() -> Path:
    from . import api

    return api.marker_path().parent


def _control_enabled() -> bool:
    """``[client] enabled`` of the control site file (``control_site.py``).
    Anything that stops it being read as true (no module, no file, a file that
    does not parse) means off: hosted runs are opt-in."""
    try:
        from . import control_site

        loaded = control_site.load()
        return bool(loaded is not None and loaded.client.enabled is True)
    except Exception:  # noqa: BLE001 - unreadable switches are off
        return False


def _deploy_view() -> dict:
    from . import deploy_api

    return deploy_api.supervisor().status()


def _spawn(argv, *, stdin, stdout, env, cwd):
    """Start the launcher in a session of its own. Only ``systemd-run --user``
    can be started here, whatever a bug builds."""
    if not argv or argv[0] != "systemd-run" or "--user" not in argv[1:3]:
        raise ValueError(f"not a command this page may start: {argv[:1]!r}")
    return subprocess.Popen(
        argv,
        stdin=stdin,
        stdout=stdout,
        stderr=stdout,
        env=env,
        cwd=cwd,
        close_fds=True,
        start_new_session=True,
    )


def _hosted_aeri() -> bool:
    """Whether the AERI terminal D that this page hosts is on, from the click that
    starts it: D takes the arm lock only after its phrase, so ``arm_lock`` alone
    leaves the start itself uncovered. Takes over what a restart left first; a
    supervisor that cannot answer raises, and the asker treats that as unknown."""
    from . import aeri_run_api

    return aeri_run_api.active() is not None


@dataclass
class Host:
    """Everything the run reaches outside Python. Tests build one with fakes
    (a launch without a scope, a clock, a lock folder); ``production_host()``
    builds the real one."""

    spawn: Callable = _spawn
    # (unit, hostchild, spec, python, allowlist, caller's environ) -> (argv, env)
    launch: Callable = scope_launch
    # The client's argv from the rendered one. Tests swap in the fake client
    # (the fake profile); the real one is never run by a test.
    client_argv: Callable = hosted_argv
    openpty: Callable = os.openpty
    killpg: Callable = os.killpg
    tcgetattr: Callable = termios.tcgetattr
    proc_state: Callable = proc_state
    identity: Callable = lambda pid: _identity(pid)
    run_lock_path: Callable = lambda: _hub() / LOCK_FILE
    arm_lock_path: Callable = lambda: Path(command.marvel0_site.ARM_LOCK_FILE)
    locks_path: str = "/proc/locks"
    health_file: Callable = stack._health_file  # -> (path or None, stale seconds)
    net_table: Callable = net.read
    sessions: Callable = status.sessions_part  # (roots, now) -> part
    aeri_runs: Callable = deploy._aeri_runs
    # Whether the page's own AERI terminal D is on (see ``_hosted_aeri``).
    hosted_aeri: Callable = lambda: _hosted_aeri()
    deploy_view: Callable = _deploy_view
    control_enabled: Callable = _control_enabled
    cgroup: Callable = stack._read_self_cgroup
    in_main_checkout: Callable = stack._in_main_checkout
    environ: dict = field(default_factory=lambda: os.environ)
    python: str = sys.executable
    hostchild: str = str(Path(__file__).with_name(HOSTCHILD))
    now: Callable = time.time
    monotonic: Callable = time.monotonic
    sleep: Callable = time.sleep
    # Run the 2 Hz watcher in a thread of its own (tests call ``tick`` by hand).
    background: bool = False


def production_host() -> Host:
    return Host(background=True)


def _identity(pid) -> dict | None:
    """What makes a pid this process and not a later one: its start time on
    this boot (the executable is left out: ``systemd-run --scope`` replaces
    itself with the launcher after the pid is known)."""
    from levi import children

    who = children.identity(pid)
    if who is None:
        return None
    return {"start_ticks": who["start_ticks"], "boot": who["boot"]}


# --- the arm lock ---------------------------------------------------------------------------


def take_arm_lock(path) -> int:
    """The same lock an automatic evaluation run holds while it uses the arm
    (``levi.automatic.fr3_run.RobotLock``), taken for the whole hosted run: a
    deploy and an AERI run then refuse. The kernel drops it when this process
    ends, however it ends. Returns the open fd (close-on-exec: the client does
    not inherit it). Raises ``Refuse("preflight_failed")`` when it cannot."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise Refuse("preflight_failed", first="arm_lock") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OSError("unsafe arm lock file")
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise Refuse("preflight_failed", first="arm_lock") from None
    return fd


# --- the folder ---------------------------------------------------------------------------


def state_dir(avoid, workspace=None, create=True) -> Path:
    """``<workspace state>/marvel0/run`` (``0700``, no link in it, not inside
    source data), like the deploy folder next to it."""
    base = (workspace or Workspace.at(paths.workspace())).state
    target = base / Path(*REL)
    if record.UNSAFE.search(str(target)):
        raise record.StateDirUnusable("the folder path has characters systemd misreads")
    for root in avoid:
        if target.is_relative_to(Path(root).expanduser().resolve()):
            raise record.StateDirUnusable("the folder would lie inside source data")
    if create:
        base.mkdir(parents=True, exist_ok=True)
    for folder in (
        target.parent,
        target,
        *(target / name for name in record.SUBFOLDERS),
    ):
        record._check_dir(folder, create)
    return target


def _fingerprint_body(body: dict) -> dict:
    """A request without what changes between two clicks of the same intent:
    the one-time token and the checklist (``record.fingerprint`` drops the id
    and the phrase)."""
    return {k: v for k, v in body.items() if k not in ("challenge", "attest")}


# --- the supervisor -----------------------------------------------------------------------


def _group_of(form_body: dict) -> str:
    """The rollout group the form will have (``command._validate``'s default)."""
    from . import models

    group = form_body.get("rollout_group")
    if group is None:
        rtc = form_body.get("rtc")
        group = models.default_group(
            str(form_body.get("model_id")), True if rtc is None else bool(rtc)
        )
    return str(group)


def _form_fingerprint(form: command.Form) -> str:
    text = json.dumps(dataclasses.asdict(form), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class FormRefuse(Refuse):
    """A form the command module refuses, with its own stable code (422)."""

    def __init__(self, exc: command.FormError):
        Exception.__init__(self, f"{exc.code}: {exc.message}")
        self.status, self.code, self.message = 422, exc.code, exc.message
        self.extra = {"field": exc.field, **exc.extra}


class Supervisor:
    """The one hosted client of this process: start, state, keys, hold, end and
    log. ``site_loader`` gives the site (or raises the 412 answer), ``catalog``
    has ``get(site, model_id)``, ``workspace`` is the folder the run record
    lives in (default: the process-wide one)."""

    def __init__(self, host, site_loader=None, catalog=None, workspace=None):
        if site_loader is None:
            from . import site

            site_loader = site.require
        if catalog is None:
            from . import models

            catalog = models
        self.host = host
        self.site_loader = site_loader
        self.catalog = catalog
        self.workspace = workspace
        self.challenges = Challenges(host.monotonic, host.now)
        self.stale_after_s = STALE_AFTER_S
        self.start_timeout_s = START_TIMEOUT_S
        self._guard = threading.Lock()  # one write at a time, in this process
        self._mutex = threading.RLock()  # the fields below
        self._rec: dict | None = None  # the run record, kept in step with the disk
        self._dir: Path | None = None
        self._child = None  # the launcher's Popen (None after a restart)
        self._master: int | None = None
        self._arm_fd: int | None = None
        self._scan = LogScan()
        self._keyboard = True
        self._seq = 0
        self._sig = None
        self._keyed = False
        self._key_at: float | None = None
        self._label_ep: int | None = None
        self._enter_ep: int | None = None
        self._red: set = set()
        self._adopted = False
        self._preflighting = False
        self._watcher: threading.Thread | None = None
        self._cache: tuple | None = None

    # ---- small things

    def _iso(self) -> str:
        return record.iso(self.host.now())

    def _profile(self) -> dict:
        h = self.host
        return stack.profile(h.cgroup(), h.in_main_checkout())

    def _require_real(self) -> None:
        if self._profile()["profile"] != "real":
            raise Refuse("not_product")

    def _folder(self, site, create) -> Path:
        avoid = (site.rollout_root, site.checkpoints_root, site.openpi_checkout)
        try:
            return state_dir(avoid, self.workspace, create=create)
        except record.StateDirUnusable:
            raise Refuse("state_dir_unusable") from None

    def _existing_folder(self, site) -> Path:
        """The run folder of a run that exists; with none there is nothing to
        press or end."""
        directory = self._folder(site, create=False)
        if not directory.is_dir():
            raise Refuse("state_not_allowed", state="idle")
        self._dir = directory
        return directory

    def _request_id(self, body) -> str:
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not record.REQUEST_ID.fullmatch(
            request_id
        ):
            raise Refuse("request_id_invalid")
        return request_id

    def _audit(self, event: str, **fields) -> None:
        """One line of the history: who (a person, through the product), what
        and when. Never a transcript, a path or the client's output."""
        if self._dir is None:
            return
        line = {
            "time": self._iso(),
            "actor": "person",
            "via": "product",
            "event": event,
            **{k: v for k, v in fields.items() if v is not None},
        }
        with contextlib.suppress(Exception):
            jsonio.append_line(
                self._dir / record.HISTORY, line, max_bytes=HISTORY_MAX_BYTES, keep=1
            )

    def _update(self, **fields) -> dict:
        """``fields`` into the run record: in memory first (a safety action
        never waits for the disk), then on disk."""
        with self._mutex:
            self._rec = {**(self._rec or {}), **fields}
            self._cache = None
            if self._dir is not None:
                with contextlib.suppress(OSError, record.RecordCorrupt):
                    record.write_record(self._dir, self._rec)
            return self._rec

    # ---- what is read

    def _monitor(self, now) -> Monitor:
        try:
            path, stale_s = self.host.health_file()
        except Exception:  # noqa: BLE001 - no configuration: unknown
            return Monitor("unknown")
        health = stack.read_health(path, min(float(stale_s), MONITOR_STALE_S), now)
        age = health.get("age_s")
        stamp = round(now - age, 6) if age is not None else None
        return Monitor(health["state"], age, stamp)

    def _read_session(self, rec, now) -> tuple:
        """``(session dict or None, seconds since its last update)``."""
        name, root = rec.get("session_file"), rec.get("rollout_root")
        if not (isinstance(name, str) and isinstance(root, str)):
            return None, None
        path = Path(root) / ".eval_sessions" / name
        try:
            raw, truncated, st = probes.read_regular(path, 1 << 18)
            data = None if truncated else json.loads(raw.decode("utf-8", "replace"))
        except (OSError, ValueError, RecursionError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        stamp = status._updated(data.get("updated_at"), st.st_mtime)
        began = rec.get("created_epoch")
        if (
            rec.get("session_id") is None
            and isinstance(began, (int, float))
            and stamp < began - 1.0
        ):
            # The file of an earlier run of the same group and task, which the
            # new client has not rewritten yet (its first write comes after
            # this launch).
            return None, None
        return data, max(0.0, now - stamp)

    def _log_file(self) -> Path | None:
        rec = self._rec
        name = (rec or {}).get("log_file")
        if self._dir is None or not (
            isinstance(name, str) and LOG_NAME.fullmatch(Path(name).stem)
        ):
            return None
        return self._dir / "logs" / name

    def _feed_log(self) -> None:
        path = self._log_file()
        if path is None:
            return
        try:
            fd = os.open(
                path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            )
        except OSError:
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return
            if info.st_size < self._scan.offset:
                self._scan = LogScan()
            if info.st_size - self._scan.offset > LOG_SCAN_MAX:
                self._scan.offset = info.st_size - LOG_SCAN_MAX
            budget = LOG_SCAN_MAX
            while self._scan.offset < info.st_size and budget > 0:
                chunk = os.pread(fd, 1 << 16, self._scan.offset)
                if not chunk:
                    break
                self._scan.feed(chunk)
                budget -= len(chunk)
        except OSError:
            return
        finally:
            os.close(fd)

    def _icanon(self) -> bool | None:
        if self._master is None:
            return None
        try:
            return bool(self.host.tcgetattr(self._master)[3] & termios.ICANON)
        except (OSError, termios.error):
            return None

    def _proc(self, rec) -> tuple:
        """``(proc, exit)``: the launcher's state and, once it is gone, how it
        ended. The launcher is our own child, so its pid may be looked at."""
        h = self.host
        if rec.get("phase") == "ended":
            return "gone", rec.get("exit")  # (no process is looked at any more)
        child = self._child
        if child is not None:
            code = child.poll()
            if code is not None:
                if code >= 0:
                    return "gone", {"code": code, "signal": None}
                return "gone", {"code": None, "signal": -code}
            letter = h.proc_state(child.pid)
            if letter is None:
                return "unknown", None
            return ("stopped" if letter in ("T", "t") else "running"), None
        leader = rec.get("leader") or {}
        pid = leader.get("pid")
        if not isinstance(pid, int) or h.identity(pid) != leader.get("identity"):
            return "gone", None
        letter = h.proc_state(pid)
        if letter is None:
            return "unknown", None
        return ("stopped" if letter in ("T", "t") else "running"), None

    def _clients(self, rec) -> int | None:
        """The connections the policy server has accepted (a loopback client
        has two rows in the table, the accepted one has the port as its local
        port)."""
        table = self.host.net_table()
        port = rec.get("port")
        if not table.ok or not isinstance(port, int):
            return None
        return len([s for s in net.established(table, port) if s.local_port == port])

    def _facts(self, now, preflighting=False) -> Facts:
        rec = self._rec
        limits = {
            "stale_after_s": self.stale_after_s,
            "start_timeout_s": self.start_timeout_s,
        }
        if rec is None:
            return Facts(now=now, record=None, preflighting=preflighting, **limits)
        proc, ex = self._proc(rec)
        if ex is None:
            ex = rec.get("exit")
        session, age = self._read_session(rec, now)
        if session is not None and rec.get("session_id") is None:
            # The first sight of this run's session: its identity from now on.
            sid, pid = session.get("session_id"), session.get("pid")
            if isinstance(sid, str) and sid:
                rec = self._update(
                    session_id=sid, client_pid=pid if isinstance(pid, int) else None
                )
        self._feed_log()
        began = rec.get("created_epoch")
        return Facts(
            now=now,
            record=rec,
            proc=proc,
            exit=ex,
            keyboard=self._keyboard and self._master is not None,
            session=session,
            session_age_s=age,
            log=self._scan.view,
            icanon=self._icanon(),
            started_age_s=now - began if isinstance(began, (int, float)) else None,
            preflighting=preflighting,
            **limits,
        )

    # ---- the state, as the document and the watcher both see it

    def _locks(self, f: Facts, d: Derived) -> tuple:
        no = session_episode(f.session)["no"] if f.session else None
        label = self._label_ep is not None and self._label_ep == no
        enter = (
            self._enter_ep is not None and self._enter_ep == no and d.state == "at_gate"
        )
        return label, enter

    def _observe(self, f: Facts, d: Derived, monitor: Monitor) -> Context:
        """The context for ``f``, and the bookkeeping that follows from what
        was read: the state sequence, the red monitor writes."""
        label, enter = self._locks(f, d)
        session = f.session or {}
        sig = (
            d.state,
            d.reason,
            session_episode(f.session)["no"] if f.session else None,
            session.get("session_id"),
            label,
            enter,
        )
        with self._mutex:
            if sig != self._sig:
                self._sig = sig
                self._seq += 1
                self._keyed = False
                self._cache = None
            if monitor.state == "red":
                if monitor.stamp is not None:
                    self._red.add(monitor.stamp)
            elif monitor.state == "ok":
                self._red.clear()
            profile = self._profile()
            return Context(
                profile=profile["profile"],
                profile_code=profile["code"],
                enabled=bool(self.host.control_enabled()),
                monitor=monitor,
                seq=self._seq,
                label_locked=label,
                enter_locked=enter,
                keyed=self._keyed,
                key_age_s=(self.host.monotonic() - self._key_at)
                if self._key_at is not None
                else None,
                armed=self.challenges.newest(),
                clients=self._clients(self._rec) if d.state in LIVE else None,
            )

    def _current(self) -> tuple:
        """``(facts, derived, context)`` read afresh."""
        now = self.host.now()
        f = self._facts(now, self._preflighting)
        d = derive(f)
        if f.proc == "gone" and (f.record or {}).get("phase") == "started":
            # Whoever looks first settles a run that is over, so no page sees it
            # ended while its locks are still held.
            self._finish(f)
        return f, d, self._observe(f, d, self._monitor(now))

    def _bind(self, f: Facts, d: Derived, c: Context) -> dict:
        rec = f.record or {}
        session = f.session or {}
        return {
            "run_id": rec.get("run_id"),
            "session_id": session.get("session_id") or rec.get("session_id"),
            "episode_no": session_episode(f.session)["no"] if f.session else None,
            "state_seq": c.seq,
        }

    # ---- the start checks

    def _enabled(self) -> bool:
        return bool(self.host.control_enabled())

    def _preflight(self, site, form=None, deep=False) -> list:
        """The start checks, each read passively. ``form`` adds the ones that
        need it; ``deep`` also reads the automatic evaluation registry (too
        much for a page that polls)."""
        h = self.host
        now = h.now()
        rows = [check_row("client_enabled", self._enabled())]
        rows.append(check_row("product", self._profile()["profile"] == "real"))
        rec = self._rec
        live = rec is not None and rec.get("phase") in ("intent", "started")
        rows.append(check_row("run_idle", not live))
        try:
            view = h.deploy_view()
        except Exception:  # noqa: BLE001 - an unreadable server is not a ready one
            view = None
        listener = (view or {}).get("listener") or {}
        served = (view or {}).get("deploy") or {}
        state = (view or {}).get("state") or "unknown"
        ready = None if view is None else state == "ready"
        if ready and listener.get("ours") is not True:
            # up, but a server this page did not start: its model and RTC
            # setting are not known to be what the deploy record says
            ready, state = False, "ready, but not started by this page"
        rows.append(check_row("policy_ready", ready, state=state))
        if form is not None:
            matches = (
                None
                if view is None
                else served.get("model_id") == form.model_id
                and bool(served.get("rtc")) == form.rtc
            )
            rows.append(
                check_row(
                    "policy_matches",
                    matches,
                    served=str(served.get("model_id") or "none"),
                    served_rtc="on" if served.get("rtc") else "off",
                    wanted=form.model_id,
                    wanted_rtc="on" if form.rtc else "off",
                )
            )
        count = listener.get("established")
        count = (
            count if isinstance(count, int) and not isinstance(count, bool) else None
        )
        rows.append(
            check_row(
                "no_connection", None if count is None else count == 0, count=count
            )
        )
        rows.append(self._sessions_row(site, now))
        rows.append(self._arm_row())
        monitor = self._monitor(now)
        rows.append(
            check_row("monitor_green", monitor.state == "ok", state=monitor.state)
        )
        if deep:
            rows.append(self._aeri_row())
        rows.append(check_row("rollout_writable", _writable(site.rollout_root)))
        rows.append(self._robot_server_row(site))
        if form is not None:
            problem = min_z_problem(form.prompt, form.min_z)
            rows.append(check_row("min_z", problem is None, **(problem or {})))
        return rows

    def _sessions_row(self, site, now) -> dict:
        try:
            part = self.host.sessions([site.rollout_root], now)
        except Exception:  # noqa: BLE001
            part = {"state": "unknown", "sessions": []}
        if part.get("state") != "ok":
            return check_row("no_session", None, count=None)
        active = [
            r
            for r in part.get("sessions") or []
            if r.get("status") not in status.ENDED_STATES
            and (r.get("age_s") or 0) <= status.SESSION_BUSY_S
        ]
        if active and self._left_by_our_dead_client(active):
            active = []
        return check_row("no_session", not active, count=len(active))

    def _left_by_our_dead_client(self, active: list) -> bool:
        """The one session file still 'active' is the one our own client left
        when it was killed (a kill leaves the file at its last state for the
        minute the busy window lasts): the process is known gone, so it does
        not block the next start."""
        rec = self._rec
        if rec is None or rec.get("phase") != "ended" or len(active) != 1:
            return False
        if active[0].get("group") != rec.get("group"):
            return False
        session, _ = self._read_session(rec, self.host.now())
        return session is not None and session.get("session_id") == rec.get(
            "session_id"
        )

    def _arm_row(self) -> dict:
        h = self.host
        holders = gpumgr.flock_holders(h.arm_lock_path(), locks=h.locks_path)
        return check_row("arm_lock", None if holders is None else not holders)

    def _aeri_row(self) -> dict:
        # The page's own terminal D counts, from its start click: a client started
        # beside it would be two programs on one arm. An unreadable answer does
        # not rule it out.
        try:
            hosted = bool(self.host.hosted_aeri())
        except Exception:  # noqa: BLE001
            return check_row("no_aeri", None)
        if hosted:
            return check_row("no_aeri", False, hosted=True)
        try:
            runs = self.host.aeri_runs()
        except Exception:  # noqa: BLE001 - an unreadable registry rules nothing out
            return check_row("no_aeri", None)
        live = [
            r
            for r in runs
            if r.get("backend") == "standalone" and r.get("runner_alive")
        ]
        return check_row("no_aeri", not live)

    def _robot_server_row(self, site) -> dict:
        table = self.host.net_table()
        port = _url_port(site.server.robot_server_url)
        if not table.ok or port is None:
            return check_row("robot_server", None)
        return check_row("robot_server", bool(net.listeners(table, port)))

    # ---- the document

    def view(self) -> dict:
        """The ``levi.marvel0.run.v1`` document."""
        self.adopt()
        h = self.host
        with self._mutex:
            cached = self._cache
            live = self._rec is not None and self._rec.get("phase") in (
                "intent",
                "started",
            )
        ttl = 0.4 if live else 2.0
        if cached is not None and h.monotonic() - cached[0] < ttl:
            return cached[1]
        preflight = []
        if not live:
            preflight = self._preflight(self.site_loader())
        with self._mutex:
            f, d, c = self._current()
            c.preflight = preflight
            body = document(f, d, c)
            self._cache = (h.monotonic(), body)
            return body

    def active(self) -> dict | None:
        """The running client as a job (``levi stop`` refuses while it runs),
        or None."""
        with self._mutex:
            rec = self._rec
            if rec is None or rec.get("phase") not in ("intent", "started"):
                return None
            began = rec.get("created_epoch") or self.host.now()
            return {
                "kind": "marvel0-client",
                "id": rec.get("run_id"),
                "running_seconds": round(self.host.now() - began),
            }

    # ---- one request at a time

    @contextlib.contextmanager
    def _operation(self, action):
        if not self._guard.acquire(blocking=False):
            raise Refuse("busy")
        try:
            lock = stack.StackLock(
                self.host.run_lock_path(), self.host.locks_path, self.host.now
            )
            try:
                with lock.hold("marvel0-run", action):
                    yield
            except stack.Refuse as exc:
                raise Refuse(
                    "busy" if exc.code == "stack_busy" else "lock_unusable"
                ) from None
        finally:
            self._guard.release()

    def _replayed(self, directory, request_id, fingerprint, locked):
        """The stored answer of this very request, ``None`` if it is new. A
        request that was accepted or whose key was sent, but never answered
        (the service stopped), is answered as unknown: nothing is sent or
        started a second time."""
        try:
            entry = record.get_request(directory, request_id)
        except record.RecordCorrupt:
            entry = {"status": "sent", "fingerprint": fingerprint}
        if entry is None:
            return None
        if entry.get("fingerprint") != fingerprint:
            raise Refuse("request_id_used")
        if entry.get("status") == "done" and isinstance(entry.get("answer"), dict):
            return {**entry["answer"], "replayed": True}
        if not locked:
            return None
        return {
            "schema": RESULT_SCHEMA,
            "replayed": True,
            "request_id": request_id,
            "done": "unknown",
            "reason": {
                "code": "request_incomplete",
                "text": "This request was accepted but never answered (the service "
                "stopped). Nothing was sent or started again; read the state.",
            },
        }

    def _transaction(self, action, directory, request_id, body, work):
        """Run ``work(mark_sent)`` once for this request, remembering the
        answer. ``mark_sent`` writes (and syncs) that the byte is about to go,
        so a crash after it can never send it again."""
        fingerprint = record.fingerprint(action, _fingerprint_body(body))
        stored = self._replayed(directory, request_id, fingerprint, locked=False)
        if stored:
            return stored
        with self._operation(action):
            stored = self._replayed(directory, request_id, fingerprint, locked=True)
            if stored:
                return stored
            entry = {"fingerprint": fingerprint, "action": action}
            record.put_request(
                directory,
                request_id,
                {**entry, "status": "accepted", "time": self._iso()},
            )
            sent = []

            def mark_sent():
                record.put_request(
                    directory,
                    request_id,
                    {**entry, "status": "sent", "time": self._iso()},
                    replace=True,
                )
                sent.append(True)

            try:
                answer = work(mark_sent)
            except Refuse as exc:
                if not sent:
                    record.drop_request(directory, request_id)
                self._audit(f"{action}_refused", request_id=request_id, code=exc.code)
                raise
            record.put_request(
                directory,
                request_id,
                {**entry, "status": "done", "answer": answer, "time": self._iso()},
                replace=True,
            )
            self._cache = None
            return answer

    # ---- start

    def _form(self, site, form_body):
        """``(model, form, preview)`` of a form body; the preview is
        ``command.render``'s, whose argv is the client's."""
        if not isinstance(form_body, dict):
            raise Refuse("request_id_invalid")
        model = self.catalog.get(site, form_body.get("model_id"))
        try:
            if model is None:
                raise command.FormError(
                    "model_unknown", "model_id", "this model is not in the catalog"
                )
            form = command.validate(site, model, form_body)
            preview = command.render(site, model, form)
        except command.FormError as exc:
            raise FormRefuse(exc) from None
        problem = min_z_problem(form.prompt, form.min_z)
        if problem:
            raise Refuse("min_z_below_task_floor", **problem)
        return model, form, preview

    def _gate_checks(self, site, form, deep=True):
        rows = self._preflight(site, form, deep=deep)
        failing = [r for r in rows if r["level"] in ("refuse", "unknown")]
        if failing:
            raise Refuse(
                "preflight_failed",
                first=failing[0]["code"],
                checks=[r for r in rows if r["level"] != "ok"],
            )
        return rows

    def challenge(self, body) -> dict:
        """A one-time token for the next action of this purpose. For ``start``
        the form is checked first and the answer carries the checks."""
        self.adopt()
        purpose = body["purpose"]
        if purpose == "start":
            return self._start_challenge(body)
        with self._mutex:
            f, d, c = self._current()
            acts = actions(d, c)
            names = [a for a, p in PURPOSES.items() if p == purpose]
            if not names:
                raise Refuse("state_not_allowed", state=d.state)
            if not any(acts[a]["allowed"] for a in names):
                why = acts[names[0]]["why_not"]
                raise Refuse(why["code"], **why["params"])
            issued = self.challenges.issue(purpose, self._bind(f, d, c))
            return {
                "schema": RESULT_SCHEMA,
                "purpose": purpose,
                "state": d.state,
                "state_seq": c.seq,
                **issued,
            }

    def _start_challenge(self, body) -> dict:
        site = self.site_loader()
        if not self._enabled():
            raise Refuse("run_disabled")
        self._require_real()
        _, form, preview = self._form(site, body.get("form"))
        rows = self._gate_checks(site, form)
        issued = self.challenges.issue("start", {"form": _form_fingerprint(form)})
        return {
            "schema": RESULT_SCHEMA,
            "purpose": "start",
            "group": form.rollout_group,
            "confirm": PHRASES["start"].format(group=form.rollout_group),
            "preflight": rows,
            "warnings": preview.warnings,
            **issued,
        }

    def start(self, body) -> dict:
        """Start the client for a form (person only, product LEVI only)."""
        self.adopt()
        request_id = self._request_id(body)
        site = self.site_loader()
        if not self._enabled():
            raise Refuse("run_disabled")
        self._require_real()
        form_body = body.get("form")
        phrase = PHRASES["start"].format(group=_group_of(form_body or {}))
        if body.get("confirm") != phrase:
            raise Refuse("confirm_required", phrase=phrase)
        attest = body.get("attest") or {}
        if not all(
            attest.get(k) is True for k in ("estop", "scene_reset", "area_clear")
        ):
            raise Refuse("attest_required")
        directory = self._folder(site, create=True)
        self._dir = directory
        return self._transaction(
            "start",
            directory,
            request_id,
            body,
            lambda mark_sent: self._start(site, directory, request_id, body),
        )

    def _start(self, site, directory, request_id, body) -> dict:
        with self._mutex:
            if self._rec is None:
                with contextlib.suppress(record.RecordCorrupt):
                    self._rec = record.read_record(directory)
            self._preflighting = True
        try:
            _, form, preview = self._form(site, body.get("form"))
            self.challenges.consume(
                body.get("challenge"), "start", {"form": _form_fingerprint(form)}
            )
            self._gate_checks(site, form)
            return self._launch(site, directory, request_id, form, preview)
        finally:
            with self._mutex:
                self._preflighting = False

    def _launch(self, site, directory, request_id, form, preview) -> dict:
        h = self.host
        argv = h.client_argv(preview.argv)
        if not os.path.isdir(site.openpi_checkout) or not os.path.isfile(h.hostchild):
            raise Refuse("start_failed")
        now = h.now()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        stem = next(
            (
                f"client-{stamp}{'' if n == 1 else f'-{n}'}"
                for n in range(1, 100)
                if not os.path.lexists(
                    directory
                    / "logs"
                    / f"client-{stamp}{'' if n == 1 else f'-{n}'}.log"
                )
            ),
            None,
        )
        if stem is None:
            raise Refuse("start_failed")
        run_id = f"{time.strftime('%Y%m%dT%H%M%S', time.localtime(now))}-{secrets.token_hex(3)}"
        log_name, exit_name, spec_name = (
            f"{stem}.log",
            f"{stem}.exit.json",
            f"{stem}.spec.json",
        )
        unit = f"{UNIT_PREFIX}{run_id}"
        rec = {
            "schema": RECORD_SCHEMA,
            "run_id": run_id,
            "phase": "intent",
            "request_id": request_id,
            "created_epoch": now,
            "created_at": self._iso(),
            "group": form.rollout_group,
            "task_folder": sanitize_task_name(canonical_prompt(form.prompt)),
            "session_file": session_name(form.rollout_group, form.prompt),
            "rollout_root": site.rollout_root,
            "port": site.port,
            "model_id": form.model_id,
            "rtc": form.rtc,
            "prompt": form.prompt,
            "form": dataclasses.asdict(form),
            "argv_sha256": hashlib.sha256(
                "\0".join(hosted_argv(preview.argv)).encode()
            ).hexdigest(),
            "unit": unit,
            "log_file": log_name,
            "exit_file": exit_name,
        }
        arm_fd = take_arm_lock(h.arm_lock_path())
        master = slave = log_fd = None
        try:
            with self._mutex:
                self._reset_run()
                self._rec = rec
                self._dir = directory
                self._update()
            master, slave = h.openpty()
            self._quiet_pty(master, slave)
            log_fd = os.open(
                directory / "logs" / log_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            spec_path = directory / "logs" / spec_name
            self._write_private(
                spec_path,
                json.dumps(
                    {
                        "argv": argv,
                        "cwd": site.openpi_checkout,
                        "exit_file": str(directory / "logs" / exit_name),
                    }
                ),
            )
            line, process_env = h.launch(
                unit,
                h.hostchild,
                str(spec_path),
                h.python,
                child_env(h.environ),
                h.environ,
            )
            child = h.spawn(
                line,
                stdin=slave,
                stdout=log_fd,
                env=process_env,
                cwd=site.openpi_checkout,
            )
        except BaseException as exc:
            for fd in (master, slave, log_fd, arm_fd):
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            self._update(phase="ended", exit={"code": EXIT_SPAWN, "signal": None})
            self._audit("start_failed", request_id=request_id, error=type(exc).__name__)
            if isinstance(exc, Refuse):
                raise
            if isinstance(exc, (OSError, ValueError)):
                raise Refuse("start_failed") from None
            raise
        os.close(slave)
        os.close(log_fd)
        who = h.identity(child.pid)
        with self._mutex:
            self._child, self._master, self._arm_fd = child, master, arm_fd
            self._keyboard = True
            self._update(
                phase="started",
                leader={"pid": child.pid, "identity": who},
            )
        self._audit(
            "started",
            request_id=request_id,
            run_id=run_id,
            model_id=form.model_id,
            group=form.rollout_group,
        )
        self._start_watcher()
        return {
            "schema": RESULT_SCHEMA,
            "created": True,
            "request_id": request_id,
            "run_id": run_id,
            "state": "starting",
            "next": "Watch the run state; the start gate opens when it shows at_gate.",
        }

    def _reset_run(self) -> None:
        self._scan = LogScan()
        self._label_ep = self._enter_ep = None
        self._keyed, self._key_at = False, None
        self._red.clear()
        self._sig = None
        self._keyboard = True
        self.challenges.void()

    @staticmethod
    def _quiet_pty(master, slave) -> None:
        """No echo (nothing is ever read back from the master), and the master
        never blocks a write."""
        import fcntl

        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @staticmethod
    def _write_private(path, text) -> None:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(fd, "w") as handle:
            handle.write(text)

    # ---- keys

    def key(self, body) -> dict:
        """Press one key of ``KEY_RULES`` for the state the run is in."""
        self.adopt()
        request_id = self._request_id(body)
        action = body.get("action")
        if action not in KEY_RULES:
            raise Refuse("state_not_allowed", state="unknown")
        site = self.site_loader()
        self._require_real()
        directory = self._existing_folder(site)
        return self._transaction(
            "key",
            directory,
            request_id,
            body,
            lambda mark_sent: self._press(request_id, action, body, mark_sent),
        )

    def _press(self, request_id, action, body, mark_sent) -> dict:
        with self._mutex:
            f, d, c = self._current()
            entry = actions(d, c)[action]
            if not entry["allowed"]:
                why = entry["why_not"]
                raise Refuse(why["code"], **why["params"])
            if action == "exit_home" and body.get("confirm") != PHRASES["exit_home"]:
                raise Refuse("confirm_required", phrase=PHRASES["exit_home"])
            self.challenges.consume(
                body.get("challenge"), PURPOSES[action], self._bind(f, d, c)
            )
            byte = KEY_RULES[action][0]
            if byte not in KEY_BYTES or self._master is None:
                raise Refuse("key_failed")
            no = session_episode(f.session)["no"] if f.session else None
            # Locks first: a key that may not have arrived is never sent again.
            if action == "enter":
                self._enter_ep = no
            elif action == "end_at_gate":
                self._update(end_requested=True)
            else:
                self._label_ep = no
            self._keyed, self._key_at = True, self.host.monotonic()
            mark_sent()
            self._audit(
                "key",
                key=action,
                request_id=request_id,
                run_id=(f.record or {}).get("run_id"),
                state=d.state,
                episode_no=no,
            )
            try:
                if os.write(self._master, byte) != 1:
                    raise OSError("short write")
            except OSError:
                raise Refuse("key_failed") from None
            self._cache = None
            return {
                "schema": RESULT_SCHEMA,
                "sent": action,
                "request_id": request_id,
                "state": d.state,
                "episode_no": no,
            }

    # ---- hold and end

    def _signal(self, sig) -> bool:
        """``sig`` to the launcher's process group, while the launcher is the
        process this run started (the Popen is unreaped, or its start time
        still matches the record's). SIGSTOP and SIGKILL only."""
        if sig not in (signal.SIGSTOP, signal.SIGKILL):
            raise ValueError("only SIGSTOP and SIGKILL are ever sent")
        rec = self._rec or {}
        leader = rec.get("leader") or {}
        pid = leader.get("pid")
        if not isinstance(pid, int):
            return False
        child = self._child
        if child is not None:
            if child.poll() is not None:
                return False
        elif self.host.identity(pid) != leader.get("identity"):
            return False
        try:
            self.host.killpg(pid, sig)
        except ProcessLookupError:
            return False
        except OSError:
            raise Refuse("signal_failed") from None
        return True

    def _hold(self, by: str) -> bool:
        """Freeze the client (SIGSTOP of its process group). Nothing resumes."""
        if f"held_{by}" not in REASONS:
            by = "elsewhere"
        sent = self._signal(signal.SIGSTOP)
        if sent:
            self._update(held_at=self.host.now(), held_by=by)
            self._audit("held", by=by, run_id=(self._rec or {}).get("run_id"))
        return sent

    @contextlib.contextmanager
    def _run_lock_if_free(self, action):
        """The machine-wide run lock when nobody holds it. A stop-direction
        action takes it so another instance sees it, and goes on without it
        when it is busy or cannot be taken: a hold never waits."""
        lock = stack.StackLock(
            self.host.run_lock_path(), self.host.locks_path, self.host.now
        )
        with contextlib.ExitStack() as held:
            with contextlib.suppress(stack.Refuse):
                held.enter_context(lock.hold("marvel0-run", action))
            yield

    def hold(self, body) -> dict:
        """Hold: always allowed, idempotent, never waits for a lock."""
        self.adopt()
        request_id = self._request_id(body)
        with self._run_lock_if_free("hold"), self._mutex:
            _, d, _ = self._current()
            if d.state == "held":
                return {
                    "schema": RESULT_SCHEMA,
                    "held": True,
                    "already": True,
                    "state": "held",
                }
            if d.state not in LIVE:
                raise Refuse("state_not_allowed", state=d.state)
            if not self._hold("person"):
                raise Refuse("signal_failed")
            self._audit(
                "hold", request_id=request_id, run_id=(self._rec or {}).get("run_id")
            )
            return {
                "schema": RESULT_SCHEMA,
                "held": True,
                "already": False,
                "state": "held",
            }

    def end(self, body) -> dict:
        """End a held, lost or still starting client (SIGKILL)."""
        self.adopt()
        request_id = self._request_id(body)
        if body.get("confirm") != PHRASES["end"]:
            raise Refuse("confirm_required", phrase=PHRASES["end"])
        site = self.site_loader()
        directory = self._existing_folder(site)
        return self._transaction(
            "end", directory, request_id, body, lambda mark_sent: self._end(request_id)
        )

    def _end(self, request_id) -> dict:
        with self._mutex:
            _, d, c = self._current()
            entry = actions(d, c)["end"]
            if not entry["allowed"]:
                why = entry["why_not"]
                raise Refuse(why["code"], **why["params"])
            self._kill("ended_by_page", killed=True)
            self._audit(
                "end", request_id=request_id, run_id=(self._rec or {}).get("run_id")
            )
            self.tick()
            return {
                "schema": RESULT_SCHEMA,
                "ended": True,
                "state": self._current()[1].state,
            }

    def _kill(self, why: str, killed=False) -> None:
        """SIGKILL the client's whole group: no further motion."""
        fields = {"killed": True} if killed else {"kill_reason": why}
        self._update(**fields)
        if not self._signal(signal.SIGKILL):
            return
        child = self._child
        if child is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(KILL_WAIT_S)

    # ---- the watcher

    def _start_watcher(self) -> None:
        if not self.host.background:
            return
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._watcher = threading.Thread(
            target=self._watch, name="levi-marvel0-run", daemon=True
        )
        self._watcher.start()

    def _watch(self) -> None:
        while True:
            self.host.sleep(TICK_S)
            try:
                if not self.tick():
                    return
            except Exception:  # noqa: BLE001, S112 - one bad look never ends the watcher
                continue

    def tick(self) -> bool:
        """One look at the client and what follows from it (the watcher calls
        this twice a second; tests call it by hand). False once the run is
        over."""
        with self._mutex:
            rec = self._rec
            if rec is None or rec.get("phase") not in ("intent", "started"):
                return False
            f, d, c = self._current()
            self._drain()
            self._enforce(f, d, c)
            return (self._rec or {}).get("phase") in ("intent", "started")

    def _drain(self) -> None:
        """Nothing is ever expected from the pty's output side; read and drop
        anything there so it can never fill."""
        if self._master is None:
            return
        with contextlib.suppress(OSError):
            os.read(self._master, 4096)

    def _enforce(self, f: Facts, d: Derived, c: Context) -> None:
        if f.proc == "gone":
            self._finish(f)
            return
        if d.state == "failed":
            self._kill(d.reason)
            return
        if d.state == "held":
            return
        why = None
        if d.state == "lost":
            why = d.reason
        elif len(self._red) >= RED_WRITES and d.may_move:
            why = "monitor_red"
        elif (c.clients or 0) > 1:
            why = "extra_connection"
        if why is not None:
            self._hold(why)

    def _finish(self, f: Facts) -> None:
        """The launcher is gone: record how it ended and let go of everything."""
        if (self._rec or {}).get("phase") == "ended":
            return
        ex = f.exit or {"code": None, "signal": None}
        self._update(phase="ended", exit=ex, ended_epoch=self.host.now())
        self._audit(
            "ended",
            run_id=(self._rec or {}).get("run_id"),
            code=ex.get("code"),
            signal=ex.get("signal"),
        )
        self._release()
        with contextlib.suppress(Exception):
            self._prune()

    def _release(self) -> None:
        for name in ("_master", "_arm_fd"):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._child = None
        self.challenges.void()

    def _prune(self) -> None:
        """Keep the newest ``LOG_KEEP`` logs (with their exit and spec files)
        and the newest request files."""
        if self._dir is None:
            return
        record.prune(self._dir)
        logs = self._dir / "logs"
        names = os.listdir(logs)
        stems = sorted(
            {n.split(".")[0] for n in names if LOG_NAME.fullmatch(n.split(".")[0])}
        )
        keep = {
            *stems[-LOG_KEEP:],
            str((self._rec or {}).get("log_file", "")).split(".")[0],
        }
        for name in names:
            stem = name.split(".")[0]
            if LOG_NAME.fullmatch(stem) and stem not in keep:
                with contextlib.suppress(OSError):
                    os.unlink(logs / name)

    # ---- after a restart

    def adopt(self) -> None:
        """Take over the record a restarted service finds. A launcher that is
        still alive has no keyboard any more (nothing here holds its pty): it
        is frozen at once and can only be ended. One that is gone is over."""
        with self._mutex:
            if self._adopted:
                return
            self._adopted = True
            try:
                site = self.site_loader()
                self._dir = self._folder(site, create=False)
                self._rec = record.read_record(self._dir)
            except Exception:  # noqa: BLE001 - nothing readable: no run is assumed
                self._rec = None
                return
            rec = self._rec
            if rec is None or rec.get("phase") not in ("intent", "started"):
                return
            leader = rec.get("leader") or {}
            pid = leader.get("pid")
            alive = (
                rec.get("phase") == "started"
                and isinstance(pid, int)
                and self.host.identity(pid) == leader.get("identity")
            )
            if not alive:
                self._update(phase="ended", exit=self._exit_file(rec))
                return
            self._keyboard = False
            with contextlib.suppress(Refuse):
                self._arm_fd = take_arm_lock(self.host.arm_lock_path())
            self._hold("host_restarted")
            self._start_watcher()

    def _exit_file(self, rec) -> dict | None:
        name = rec.get("exit_file")
        if self._dir is None or not isinstance(name, str):
            return None
        try:
            raw, _, _ = probes.read_regular(self._dir / "logs" / name, 4096)
            data = json.loads(raw.decode("utf-8", "replace"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        code, sig = data.get("code"), data.get("signal")
        return {
            "code": code if isinstance(code, int) else None,
            "signal": sig if isinstance(sig, int) else None,
        }

    # ---- the log

    def log(self, lines) -> dict:
        """The tail of the client's log (``GET /run/log``), paths cut to their
        last segment."""
        self.adopt()
        with self._mutex:
            path = self._log_file()
            rec = self._rec or {}
        if path is None:
            return {"schema": LOG_SCHEMA, "log_id": None, "size_bytes": 0, "lines": []}
        try:
            size = os.lstat(path).st_size
        except OSError:
            size = 0
        return {
            "schema": LOG_SCHEMA,
            "log_id": rec.get("log_file"),
            "size_bytes": size,
            "lines": deploy.page_lines(path, lines),
        }


def _writable(root) -> bool:
    try:
        return os.path.isdir(root) and os.access(root, os.W_OK | os.X_OK)
    except OSError:
        return False


def _url_port(url) -> int | None:
    from urllib.parse import urlparse

    try:
        return urlparse(url).port
    except ValueError:
        return None
