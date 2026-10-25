"""Start and stop the arm stack from the marvel0 page: A (the arm controller),
B (the gripper stack), C (the robot server), the arm-controller marker, the
checks before them, the gripper test and a guided sequence. It sits on top of
``stack.py`` (S4-0): the same ``Host``, ``Refuse``, phrases, request replay,
product-only rule and machine-wide lock, and it adds the ``act`` part of the
stack document.

**What a start needs.** Every start (and the gripper test) is refused unless
all of these hold, in the product LEVI only and under the machine-wide stack
lock: the control file switches the stack on (``control_site``); the exact
phrase was typed; the attestations the step needs were ticked (they are
recorded, and a script can send them: the page says so); the request id was not
used for something else; and the machine says the step is possible, as a list
of blockers the page shows beside the button (``start_blockers``): M fresh,
every layer of the stack known (rule R of ``stack.py``), the unit and its
previous run closed, the ordering (B needs A **ready**, C needs A and B
**ready**: running, or failed but still connected, is not enough), the
realtime limits, the FCI port answering (one TCP connection, made only while A
is positively known to be absent), the marker set and old enough, the arm's
sockets quiet, C's three read-only checks passed, no hosted run (or one that
cannot be told), the arm lock free.

**What a stop does.** It names only a unit this page started (``systemctl
show`` must report the recorded ``Id`` and exact command), sends the unit's
own ``KillSignal`` (SIGINT) through ``systemctl --user stop``, and never kills.
The order is client, C, A, B (optional), M last. Stopping C leaves the
impedance controller holding the arm until A stops: the page says so once C is
stopped. A stop works without the control file and does not wait for a check
or the gripper test (RIG-04: the way out must not depend on the way in): it
ends them (see ``_begin``). A stop is refused only for what makes it wrong: a
part of the stack that must stop first, a hosted run known to be active, a
client connected to C.

**What none of this is.** The gates stop mistakes, not another program of the
same account, which can send the same requests and edit the files read here.
The real barriers are Franka Desk and the physical emergency stop; nothing on
the page is an emergency stop.

**A sequence never outlives the process.** The guided sequence lives in memory
only and is never resumed after a restart. The guided start runs M, the
marker, the wait and A; with ``through = "c"`` it goes on by itself to B and C,
each by the same ``_do_start`` as the single start (every gate again, with the
confirmations recorded at the request), and ``pause_before_b`` /
``pause_before_c`` make it wait for the person's own start instead. Before every
start, touch or check it makes, it looks again, inside the machine-wide lock,
whether it is still the running sequence (a stop or a cancel answered in
between wins). It never stops anything; the guided stop never starts anything.

Everything of the machine is reached through ``ActHost`` (and the base
``stack.Host``), which tests replace.
"""

import contextlib
import copy
import json
import logging
import os
import re
import socket
import stat
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from levi.live import gpumgr
from levi.setup import probes

from . import control_site, deploy, gripper, net, record, site, stack, stack_units
from .gripper import MISSING_RATIO

LOG = logging.getLogger(__name__)
SCHEMA = "levi.marvel0.stack_act.v1"
RECORD_SCHEMA = "levi.marvel0.stack_act_record.v1"
LOG_SCHEMA = "levi.marvel0.stack_log.v1"
LOG_NAME = re.compile(r"[abc]-\d{8}T\d{6}Z(?:-\d{1,2})?\.log")
KEEP_LOGS = 10
# The ticker of a guided start or stop looks again this long after the last look
# (a look is one read of the stack, about 40 ms and six ``systemctl show`` on
# the real machine) while the sequence runs, and at once after anything that
# changed: a step that moved on, a check or a start that finished (``_poke``).
# It used to look every 2 s and move one step per look, so a chain that needed
# ten steps lost 25 s doing nothing. While the sequence only waits for the
# person (``waiting_confirm``) a slow look is enough.
#
# **Once A has a unit the 1 kHz control loop is live** (await_a, the check of B,
# B, the checks before C, C, and the steps of a stop), and a look is about six
# forks of ``systemctl``/``loginctl``: at 0.25 s that is some 28 fork/execs a
# second on a non-real-time kernel, never measured against the loop. So the
# short pause holds only until the look that sees A's unit; after that the pause
# is ``SEQUENCE_TICK_ARM_S``. What it costs is the wait of a stage that came up
# between two looks (at most one pause per stage, about 1.5 s on the whole
# chain); the look right after a change and the wake-up when an operation ends
# stay, so a check that finishes is still acted on at once.
SEQUENCE_TICK_S = 0.25
SEQUENCE_TICK_ARM_S = 1.0
SEQUENCE_TICK_WAITING_S = 2.0
SEQUENCE_BURST = 40  # looks in a row without a pause after a change, at most
STEP_TIMEOUT_S = 600.0  # one automatic step of the guided start may take this long
# What a guided start settles by itself, so it is not a reason to refuse it.
SEQUENCE_SOFT = (
    "gate.monitor_fresh",
    "gate.layers_known",
    "gate.marker_set",
    "marker_young",
    "heavy_jobs_running",
    "gate.fci_probe",
    "gate.arm_sockets_clear",
    "gate.lock_free",
)
# A busy lock or operation is tried again at the next tick, not a failure.
TRANSIENT = ("stack_busy", "operation_running")
ESTOP_TEXT = (
    "Keep a hand on the physical emergency stop for every session. Nothing on "
    "this page is an emergency stop: the stop buttons end a program, they do "
    "not stop the arm."
)
SCOPE_TEXT = (
    "These checks stop mistakes. They do not stop another program of this "
    "account, which can send the same requests; Franka Desk and the physical "
    "emergency stop are the real barriers."
)
CLIENT_OVERRIDE = "stop robot server with client connected"
FCI_TTL_S = 300.0  # an open FCI port is good for starting A for this long
B_PROBE_TTL_S = 600.0  # "no gripper stack is running" is good for this long
# While a guided start waits for the person to start B, the check is made again
# before its time is up: "absent" after this long, anything else (a gripper
# stack seen, or no answer) after the shorter one. The guided start waits up to
# SEQUENCE_WAIT_S (30 min) and the check, which only A's check could refresh
# once, was good for 10 minutes: with A up, B could then not be started at all.
B_PROBE_REFRESH_S = 300.0
B_PROBE_RETRY_S = 60.0
# Why B reads unknown because there is no usable answer to "does a gripper
# stack run?" (never asked, asked too long ago, or asked and not answered): the
# one case a press on B's start settles, by asking again.
B_ASK_AGAIN = ("b.not_probed", "b.probe_stale", "b.probe_no_answer")
C_PREFLIGHT_TTL_S = 300.0
DRAIN_S = 90.0  # sockets to the arm linger (TIME_WAIT) after a probe or a stop
DRAIN_SKEW_S = 1.0  # a mark this far ahead of the clock is still "just now"
PROBE_TIMEOUT_S = 2.0
# --- the heavy jobs of this computer, and the wait for them ---------------------
# Heavy jobs that honour the marker (``levi-hub/heavy_gate.py``: pytest and bun
# runs) stop within about 30 s of it (SIGTERM, 30 s grace, SIGKILL, capped at
# 45 s). The page used to wait a fixed ``marker_wait_s`` (45-60 s) whether any
# ran or not. It now waits until the marker is old enough (the control file's
# ``marker_wait_s``: only the time a gate needs to see it) AND the log of the gate
# shows no job of its that really runs (``heavy_jobs_running``). Two sources
# decide, as in the gate's own ``wait-clear``: its log (the default one, in the
# hub) and the heavy-gate scopes systemd lists (a gate run with another
# ``HEAVY_GATE_LOG`` or ``LEVI_HUB_DIR``, a job older than the log window, one
# logged outside the part of the log that is read: still honours the fixed
# marker, and is seen there). What cannot be told falls back to the fixed wait
# of old, never to "no job". Other programs on this computer do not honour the
# marker and are not looked at.
HEAVY_LOG_NAME = "heavy-gate.jsonl"
HEAVY_LOG_MAX_AGE_S = 6 * 3600.0  # a job last heard of longer ago is not looked at
HEAVY_LOG_TAIL_BYTES = 4 * 1024 * 1024  # of a long log, only its end is read
HEAVY_MAX_UNITS = 64  # more candidates than this are not asked about: unknown
HEAVY_SHOW = "--property=Id,LoadState,ActiveState,TasksCurrent"
# The second source: every heavy-gate scope systemd has loaded, whatever log (or
# no log) its gate wrote. systemd 255 expands the glob in ``show`` itself.
HEAVY_GLOB = "heavy-gate-*"
HEAVY_TERMINAL = ("finish", "interrupted", "cancelled", "aborted", "exhausted")
HEAVY_UNIT = re.compile(
    r"heavy-gate-(pytest-file|pytest-dir|bun-test|bun-validate|bun-build)-\d+-\d+-[0-9a-f]{6}"
)
HEAVY_STATES = ("clear", "running", "unknown")
# What the start before A can be waiting for (``act.sequence.wait.holds``).
WAIT_HOLDS = (
    "marker_absent",
    "marker_young",
    "heavy_jobs_running",
    "heavy_jobs_unknown",
    "arm_sockets",
)
# How the answer about a gripper stack came (``checks.b_probe.by``).
B_PROBE_BY = ("nodes", "probe", "stop")
HEAVY_WAIT_CAP_S = 90.0  # a job that does not yield in this long ends the start
HEAVY_CLEAN_LOOKS = 2  # looks in a row without a running job (heavy_gate wait-clear)
CHECK_TIMEOUT_S = 25.0  # one read-only check through a one-shot unit
SEQUENCE_WAIT_S = 1800.0  # a sequence left waiting for B or C expires
# The desk and e-stop ticks of a guided start are good for this long plus the
# marker's wait: a sequence that has not started A by then is over (it was
# armed by a click that is no longer fresh).
SEQUENCE_ARM_WINDOW_S = 300.0
# The chain (``through = "c"``) goes on by itself on the ticks of the same click,
# so the click must still be fresh when B and C are started: each stage has its
# own deadline, counted from the click, on top of the one before. A pause the
# person takes before B or C makes the next automatic start late, and the
# sequence ends (``expired``) instead of starting on an old tick.
SEQUENCE_A_READY_S = 300.0  # A's start until A is ready
SEQUENCE_B_READY_S = 300.0  # the check of B, B's start and the gripper's calibration
SEQUENCE_C_CHECK_S = 240.0  # the checks before C, one retry included
SEQUENCE_C_READY_S = 240.0  # C's start until it is ready
LOG_TRIM_EVERY_S = 60.0  # how often a ready unit's log is checked for its size
# What the page may see of an operation (the rest is bookkeeping).
OP_PUBLIC = ("kind", "terminal", "state", "started_at", "finished_at", "code")
# A stop does not wait for these: it ends them (the read-only checks).
PREEMPTED_BY_STOP = ("preflight_a", "preflight_c", "probe_b")
# The check whether a gripper stack runs only reads the ROS graph: it may run
# beside the FCI check and beside the start of A (which must not wait for it),
# but not beside another such check. Every other operation is alone.
SIDE_OPS = ("probe_b",)
# A stop of B first ends the gripper test through its one-shot unit and waits for
# that unit to be gone: send_goal's SIGINT handler spins until B's action server
# acknowledges the cancel (ros2action/verb/send_goal.py:146-170), and `timeout
# -k` allows GRIPPER_KILL_AFTER_S for it. The wait is bounded (the way out never
# depends on a unit that will not go) and counted in polls, so a test can run it.
GRIPPER_END_WAIT_S = stack_units.GRIPPER_KILL_AFTER_S + 5.0
GRIPPER_END_POLL_S = 0.5
RELEASE_POLLS = (
    45  # x RELEASE_POLL_S: how long a finished stop of A waits to release the marker
)
RELEASE_POLL_S = 2.0
PROTECTED_PORTS = (5000, 5001, 5100, 7470, 8000)
ANSWERS_KEPT = stack.ANSWERS_KEPT  # request ids remembered across a restart
ATTEST_KEYS = ("desk_done", "estop_hand", "hands_clear", "no_client")
ATTEST_NEEDED = {
    "a": ("desk_done", "estop_hand"),
    "b": ("hands_clear",),
    "c": ("estop_hand", "hands_clear", "no_client"),
    "gripper": ("hands_clear",),
}
PHRASE_NAMES = (
    "arm_start",
    "arm_stop",
    "gripper_start",
    "gripper_stop",
    "robot_server_start",
    "robot_server_stop",
    "marker_touch",
    "marker_adopt",
    "marker_clear",
    "gripper_open",
    "gripper_close",
    "gripper_position",
    "sequence_stop",
)
# Why a guided start (or stop) ended, besides the codes of a refusal (``TEXT``).
SEQUENCE_REASONS = (
    "cancelled",
    "expired",
    "a_lost",
    "a_failed",
    "a_dead",
    "b_lost",
    "b_failed",
    "b_dead",
    "c_failed",
    "c_dead",
    "c_preflight_failed",
    "b_not_seen",
    "marker_not_ours",
    "marker_unusable",
    "marker_gone",
    "step_timeout",
    "heavy_job_still_running",
    "fci_closed",
    "fci_unreachable",
    "preflight_failed",
    "monitor_failed",
    "monitor_foreign_running",
    "monitor_stopping",
    "monitor_unknown",
    "superseded",
)
# The steps of the guided start: the first six are A's (they end with the
# sequence waiting for B when it goes through A only); the others are the chain
# (``through = "c"``): the check whether a gripper stack runs, B's start and its
# calibration, the checks before C, C's start and its readiness. The marker is
# set right after M, so that its age runs while the FCI check (``probe``, about
# 1 s) and the check whether a gripper stack runs (a background operation begun
# with the marker) are made; ``wait`` is then only what is left of the marker's
# minimum age and of the heavy jobs.
A_STEPS = ("monitor", "marker", "probe", "wait", "arm", "await_a")
CHAIN_STEPS = ("b_probe", "b", "c_preflight", "c")
SEQUENCE_STEPS = (*A_STEPS, *CHAIN_STEPS)
SEQUENCE_THROUGH = ("a", "c")
STEP_TERMINAL = {
    "monitor": "m",
    **dict.fromkeys(A_STEPS[1:], "a"),
    "b_probe": "b",
    "b": "b",
    "c_preflight": "c",
    "c": "c",
}
SEQUENCE_KINDS = ("start", "stop")
# The guided stop ("stop whole stack"): C, then A, then B. M stays (the marker
# release already follows A's stop); the order is the single stops' own
# ("stop C first"), here made one request.
STOP_ORDER = ("c", "a", "b")
SEQUENCE_STOP_STEPS = tuple(f"stop_{k}" for k in STOP_ORDER)
# ``skipped``: nothing was asked of the machine (a part that was not there, or
# one that was ready already). ``not_reached``: the sequence ended before it.
SEQUENCE_STEP_STATES = (
    "pending",
    "running",
    "done",
    "skipped",
    "failed",
    "cancelled",
    "not_reached",
)
# One step of the guided stop may take the unit's own stop time (SIGINT, then
# ``TimeoutStopSec``) and this long on top (a busy lock, the worker's start).
STOP_STEP_SLACK_S = 90.0
# What "stop whole stack" refuses at once: the gates of the single stops that
# say what is running cannot be told, or that a person must settle first. The
# rest (a part the page did not start, a record it cannot read for one unit)
# stops the sequence at its own step, with its own code.
STOP_ALL_CODES = (
    "gate.product",
    "systemd_unreadable",
    "record_corrupt",
    "run_active",
    "client_connected",
)
# The one code the guided stop words by itself: C's single stop offers an
# override phrase for a connected client, the sequence never does.
STOP_ALL_RENAMED = {"client_connected": "stop_all_client_connected"}
NOTICES = (
    "notice.c_open_port",
    "notice.arm_still_held",
    "notice.stopping_too_long",
    "notice.stop_unfinished",
)
STATES = (
    "idle",
    "starting",
    "ready",
    "failed",
    "dead",
    "unknown",
    "foreign_running",
    "stopping",
)
ACTIONS = {
    ("start", "a"): "arm_start",
    ("stop", "a"): "arm_stop",
    ("start", "b"): "gripper_start",
    ("stop", "b"): "gripper_stop",
    ("start", "c"): "robot_server_start",
    ("stop", "c"): "robot_server_stop",
}
HAZARDS = {
    "a": (
        "hazard.a",
        (
            "Starting A connects the 1 kHz control loop to the arm. The arm does "
            "not move, but nothing else may touch it. LEVI's own heavy jobs (the "
            "ones that honour the marker) are paused first; other programs on this "
            "computer are not."
        ),
    ),
    "b": (
        "hazard.b",
        (
            "Starting B opens and closes the gripper fully (about 2 s). Keep hands "
            "and objects out of the jaws."
        ),
    ),
    "c": (
        "hazard.c",
        (
            "About 3 s after the server starts, the impedance controller turns on "
            "and the arm may sink 1-2 mm. The server's port has no authentication. "
            "Keep a hand on the physical emergency stop."
        ),
    ),
}
# Said once the stop is done (never before: a stop can still fail).
STOP_NOTES = {
    "c": "c.stop_arm_still_held",  # the impedance controller outlives C
    "a": "a.stop_fci_dropped",
}
STOP_NOTE_TEXT = {
    "c.stop_arm_still_held": (
        "The server stopped, but the arm is still under impedance control "
        "(the controller keeps its last target). Stop A next."
    ),
    "a.stop_fci_dropped": (
        "A stopped: the connection to the arm closed. Desk's Lock and "
        "Deactivate FCI stay a person's."
    ),
}

# code -> (HTTP status, English sentence). The sentences hold no path. Gate
# codes are ``stack.gates``' own; the rest are this module's.
TEXT = {
    "control_disabled": (
        403,
        (
            "Starting and stopping the arm stack from this page is switched off: "
            "the control file is missing, invalid, or has [stack] enabled = false."
        ),
    ),
    "control_health_file": (
        409,
        (
            "The health file this page reads is not the one in the control file's "
            "franka_control checkout."
        ),
    ),
    "operation_running": (423, "Another operation on the arm stack is running."),
    "record_open": (
        409,
        (
            "The last start of this unit was never closed (it failed or vanished): "
            "stop it first, which clears it."
        ),
    ),
    "record_corrupt": (409, "This unit's record cannot be read."),
    "run_active": (
        409,
        "A hosted evaluation run is active (or cannot be told); end it first.",
    ),
    "arm_lock_held": (
        409,
        "The AERI arm lock is held (or cannot be told): something is using the arm.",
    ),
    "marker_young": (
        409,
        (
            "The marker is {age_s} s old; A starts when it is {wait_s} s old and "
            "no heavy job of this computer still runs."
        ),
    ),
    "heavy_jobs_running": (
        409,
        (
            "{count} heavy job(s) of this computer ({kinds}) still run. They "
            "stop within about 30 s of the marker; A starts when they have."
        ),
    ),
    "marker_not_ours": (
        409,
        "The marker was not set by this page; adopt it to let the page remove it later.",
    ),
    "attest_missing": (400, "Tick every confirmation first: {keys}."),
    "attest_invalid": (400, "Unknown confirmation: {keys}."),
    "wrapper_changed": (
        409,
        "The start wrapper is not the reviewed file; nothing is started.",
    ),
    "serial_missing": (409, "The gripper's serial device is not there."),
    "c_preflight": (
        409,
        (
            "The checks before C (QoS, gripper controllers, end-effector frame) have "
            "not all passed in the last {ttl_s} s."
        ),
    ),
    "health_not_ok": (
        409,
        (
            "The health file does not show a ready arm: fresh, hardware active, "
            "idle or moving, no red light."
        ),
    ),
    "port_taken": (409, "Something already listens on the robot server's port {port}."),
    "not_ours": (
        409,
        "That part was not started by this page; stop it in the terminal it runs in.",
    ),
    "unit_foreign": (
        409,
        "A unit of that name runs something else; the page does not touch it.",
    ),
    "stop_c_first": (409, "Stop the robot server C first."),
    "b_running": (
        409,
        "The gripper stack B still runs; the health monitor stops last.",
    ),
    "client_connected": (
        409,
        (
            "A client is connected to the robot server on port {port} (or the "
            "socket table cannot be read); end it first, or type the override phrase."
        ),
    ),
    "start_failed": (502, "systemd-run could not start the unit."),
    "stop_failed": (502, "systemctl could not stop the unit."),
    "marker_exists": (409, "A marker already exists."),
    "marker_unusable": (503, "The marker could not be created or examined."),
    "marker_release_blocked": (
        409,
        "The marker stays while A may run or the arm has a control connection.",
    ),
    "probe_arm_present": (
        409,
        (
            "The arm's FCI port is never checked while A exists or the arm has a "
            "control connection."
        ),
    ),
    "gripper_b_not_ready": (409, "The gripper stack B is not ready."),
    "sequence_active": (409, "A guided start or stop is already in progress."),
    "sequence_idle": (409, "No guided start or stop is in progress."),
    "sequence_stopping": (
        409,
        "A guided stop is in progress; nothing is started or set meanwhile.",
    ),
    "sequence_option_invalid": (
        400,
        "Unknown or invalid option of the guided start: {name}.",
    ),
    "nothing_to_stop": (
        409,
        "Nothing that this page started is running, so there is nothing to stop.",
    ),
    "stop_all_client_connected": (
        409,
        (
            "A client is connected to the robot server on port {port} (or the "
            "socket table cannot be read); end it first. Stopping the whole stack "
            "has no override: that phrase belongs to stopping the robot server alone."
        ),
    ),
    "no_log": (404, "That unit has no log on this page."),
    "record_unusable": (503, "The page's own record folder cannot be used."),
    "gate.product": (
        409,
        "Only the product LEVI may act on the real arm stack; this instance only shows it.",
    ),
    "gate.lock_free": (
        423,
        "Another action on the arm stack holds the machine-wide lock.",
    ),
    "gate.monitor_fresh": (
        409,
        "The health monitor M is not running with a fresh health file.",
    ),
    "gate.layers_known": (
        409,
        (
            "Part of the stack cannot be seen ({unknown}); nothing is changed while "
            "that is so. Run the check."
        ),
    ),
    "gate.a_absent": (
        409,
        "The arm controller A is running, or cannot be told to be stopped.",
    ),
    "gate.a_unit_absent": (409, "A unit of the arm controller exists."),
    "gate.marker_set": (
        409,
        (
            "The arm-controller marker is not set; it pauses heavy jobs on this "
            "computer before A starts."
        ),
    ),
    "gate.arm_sockets_clear": (
        409,
        "The arm has a control connection (or its socket table cannot be read).",
    ),
    "gate.rt_limits": (
        409,
        (
            "The user manager does not grant realtime priority 98 and unlimited "
            "locked memory."
        ),
    ),
    "gate.fci_probe": (
        409,
        (
            "The arm's FCI port has not answered a check in the last {ttl_s} s: "
            "activate FCI in Desk, then run the check."
        ),
    ),
    "gate.linger": (
        409,
        "Logging out stops every unit of the stack within about 5 s (Linger is off).",
    ),
    "gate.a_running": (409, "The arm controller A is not running."),
    "gate.b_absent": (
        409,
        "The gripper stack B is running, or cannot be told to be stopped.",
    ),
    "gate.b_unit_absent": (409, "A unit of the gripper stack exists."),
    "gate.b_known": (
        409,
        "Whether the gripper stack runs is not known; run the check.",
    ),
    "gate.b_running": (409, "The gripper stack B is not running."),
    "gate.c_absent": (
        409,
        "The robot server C is running, or cannot be told to be stopped.",
    ),
    "gate.c_unit_absent": (409, "A unit of the robot server exists."),
    "gate.no_client": (409, "A demo client is connected to the policy server."),
    "a_not_ready": (409, "The arm controller A is not ready (it is {state})."),
    "start_unconfirmed": (
        504,
        (
            "systemd-run did not answer in time: the unit may be starting. Look at "
            "the state before starting again."
        ),
    ),
    "stop_left_processes": (
        409,
        (
            "{unit} did not stop on SIGINT and still has processes, so its record "
            "stays open. SIGINT was sent to them again; the page never kills. "
            "If the arm must be freed, use the physical emergency stop."
        ),
    ),
    "stop_c_unknown": (
        409,
        (
            "Whether the robot server C runs cannot be told ({reason}). A stop of "
            "A or B alone goes ahead with this warning; stopping the whole stack "
            "stops here, before A, because it cannot tell that C is gone."
        ),
    ),
    "b_probe_stale": (
        409,
        (
            "No usable check says whether a gripper stack is already running "
            "(none made, too old, or without an answer): pressing start makes the "
            "check first (a few seconds); press start again when it has finished."
        ),
    ),
    "b_probe_unconfirmed": (
        409,
        (
            "The last check found no gripper stack, but only once: the ROS graph "
            "can miss a stack it has not discovered yet, and a second gripper stack "
            "would fight the first for the serial port. Pressing start repeats the "
            "check first (a few seconds); press start again when it has finished."
        ),
    ),
    "b_probe_started": (
        409,
        (
            "There was no usable check whether a gripper stack is already running "
            "(none made, too old, without an answer, or only one answer of the "
            "graph), so it has been started (a few seconds). Nothing was started: "
            "press start again when it has finished."
        ),
    ),
}


class ActRefuse(stack.Refuse):
    """A refusal with this module's codes next to ``stack.Refuse``'s.

    ``keep_work``: the work the request queued (``_spawn``) is still run
    although the request is refused. Only for a refusal that is the answer to
    "I started something else for you" (``b_probe_started``)."""

    def __init__(self, code, *, keep_work=False, **extra):
        table = TEXT if code in TEXT else stack.REFUSALS
        self.status, text = table[code]
        self.code = code
        self.extra = extra
        self.keep_work = keep_work
        self.message = _format(text, extra)
        Exception.__init__(self, f"{code}: {self.message}")


def _format(text, params) -> str:
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text


def row(code, level="required", **params) -> dict:
    """One blocker for the page: ``{"code", "level", "text", "params"}``."""
    if code in TEXT:
        text = TEXT[code][1]
    elif code in stack.REFUSALS:
        text = stack.REFUSALS[code][1]
    else:
        text = code
    return {
        "code": code,
        "level": level,
        "text": _format(text, params),
        "params": params,
    }


def _codes(rows) -> list:
    return [b["code"] for b in rows]


def catalog() -> dict:
    """Every code this module sends that a page words, by kind. The front
    end's catalogues must hold a text for each (the sentences here are the
    English fallback); a test on that side can read this."""
    rules = [
        item
        for group in (
            stack_units.A_READY,
            stack_units.A_FAIL,
            stack_units.B_READY,
            stack_units.B_FAIL,
            stack_units.C_FAIL,
            stack_units.c_ready(5000),
        )
        for item in group
    ]
    return {
        "blockers": sorted(set(TEXT) | set(stack.REFUSALS)),
        "states": list(STATES),
        "hazards": sorted(code for code, _ in HAZARDS.values()),
        "stop_notes": sorted(STOP_NOTE_TEXT),
        "notices": list(NOTICES),
        "failures": sorted(
            {i.key for i in rules} | {"ready_timeout", "duplicate_text", "unit_failed"}
        ),
        "readiness": sorted(
            {i.key for i in rules}
            | {"health_hardware_active", "health_controller_active", "listening"}
        ),
        "gripper_results": sorted(gripper.RESULT_TEXT),
        "attest": list(ATTEST_KEYS),
        "phrases": list(PHRASE_NAMES),
        "sequence_steps": list(SEQUENCE_STEPS),
        "sequence_throughs": list(SEQUENCE_THROUGH),
        "sequence_reasons": list(SEQUENCE_REASONS),
        "sequence_kinds": list(SEQUENCE_KINDS),
        "sequence_stop_steps": list(SEQUENCE_STOP_STEPS),
        "sequence_step_states": list(SEQUENCE_STEP_STATES),
        "sequence_holds": list(WAIT_HOLDS),
        "heavy_states": list(HEAVY_STATES),
        "b_probe_by": list(B_PROBE_BY),
    }


# --- the host ------------------------------------------------------------------------


def tcp_probe(ip, port, timeout_s=PROBE_TIMEOUT_S) -> dict:
    """One TCP connection to ``ip:port`` and straight closed: ``{"state":
    "open"|"closed"|"unreachable"}``. This is the FCI check ``preflight.sh``
    makes. A protected port (the robot server, the policy server, the learner)
    is never connected to, whatever asks.

    The connection is closed with a reset (``SO_LINGER`` 0), not with a FIN: the
    side that closes first keeps the connection in TIME_WAIT for 60 s, and
    ``arm_guard`` counts such a socket to the arm as a control connection, so
    a FIN would keep A from starting for a minute after the check (the leftover
    that ``Act._draining`` has to explain). A reset leaves no socket behind
    (checked against the kernel's table in ``test_marvel0_stack_act_fast.py``).
    What the arm's own FCI server makes of a reset instead of a close has not
    been seen: it is a plain TCP event any crashed client causes."""
    if int(port) in PROTECTED_PORTS:
        raise ValueError("a protected port is never connected to")
    try:
        sock = socket.create_connection((ip, int(port)), timeout=timeout_s)
    except ConnectionRefusedError:
        return {"state": "closed"}
    except OSError:
        return {"state": "unreachable"}
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()
    return {"state": "open"}


def _state_root() -> Path:
    from levi import paths
    from levi.workspace import Workspace

    return Workspace.at(paths.workspace()).state / "marvel0" / "stack"


def workspace_is_main() -> bool:
    """Is this process's workspace the main checkout's ``.state``? (design
    record 2.12: the real stack is the product LEVI's, and its records are
    written to that workspace. A product unit started with another
    ``LEVI_WORKSPACE`` would write them where no later instance looks.)"""
    from levi import paths

    try:
        return (
            Path(paths.workspace()).resolve()
            == (site.main_checkout() / ".state").resolve()
        )
    except (OSError, IndexError, RuntimeError):
        return False


CGROUP_ROOT = Path("/sys/fs/cgroup")


def cgroup_procs(group) -> list | None:
    """The pids in the control group ``group`` (as ``systemctl show`` prints
    it) of a user unit, or None when they cannot be read. Only the cgroup
    file of a user service under ``/user.slice/`` is ever read."""
    parts = Path(str(group)).parts
    if (
        len(parts) < 3
        or parts[:2] != ("/", "user.slice")
        or ".." in parts
        or not parts[-1].startswith(stack_units.UNIT_PREFIX)
    ):
        return None
    try:
        raw, _, _ = probes.read_regular(
            CGROUP_ROOT.joinpath(*parts[1:], "cgroup.procs"), 1 << 16
        )
    except OSError:
        return None
    return [int(x) for x in raw.decode("ascii", "replace").split() if x.isdigit()]


# --- the heavy jobs of this computer ------------------------------------------------

_HEAVY_CACHE: dict = {}  # log path -> (identity, records): the log is parsed when it changes


def _heavy_records(path) -> list:
    """The records of ``heavy-gate.jsonl`` as ``heavy_gate.read_log`` reads it (a
    line that is not JSON is skipped). Only the end of a long log is read, and a
    log that has not changed is not parsed again. Raises ``OSError``
    (``FileNotFoundError`` when there is none)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        cached = _HEAVY_CACHE.get(str(path))
        if cached is not None and cached[0] == identity:
            return cached[1]
        start = max(0, info.st_size - HEAVY_LOG_TAIL_BYTES)
        begin = max(0, start - 1)  # (one byte more: is the tail on a line start?)
        raw = os.pread(fd, info.st_size - begin, begin)
    finally:
        os.close(fd)
    if start:
        # The first line of a tail is cut, unless the tail begins on a line start;
        # the extra byte read says which: it is then the newline that ends the
        # line before, and what is dropped is that newline alone.
        raw = raw.partition(b"\n")[2]
    lines = raw.decode("utf-8", "replace").splitlines()
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    _HEAVY_CACHE.clear()  # (one log is in use)
    _HEAVY_CACHE[str(path)] = (identity, out)
    return out


def _epoch(text):
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return None


def _count(value):
    """A task count from ``systemctl show`` (``[not set]`` and uint64 max: the
    accounting says nothing)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number < 2**63 else None


def _scope_verdict(shown) -> str:
    """``running`` (the scope has processes), ``empty`` (it is listed and holds
    none: a scope that is left over), ``gone`` (no such unit) or ``unknown``,
    from the properties ``systemctl show`` printed for it."""
    if shown.get("LoadState") == "not-found":
        return "gone"
    tasks = _count(shown.get("TasksCurrent"))
    active = shown.get("ActiveState")
    if active in ("inactive", "failed"):
        return "running" if tasks else "gone"
    if active in ("active", "activating", "deactivating", "reloading"):
        if tasks is None:
            return "unknown"
        return "running" if tasks > 0 else "empty"
    return "unknown"


def _show_blocks(run, names):
    """``{Id: properties}`` from ONE ``systemctl --user show`` of ``names`` (a
    left-over scope costs a look every time, and there are several on this
    machine). The answer is a block of ``key=value`` lines per unit, a blank
    line between them, each with its ``Id`` (a glob that matches nothing prints
    nothing and exits 0). None when systemd does not answer."""
    argv = ["systemctl", "--user", "show", HEAVY_SHOW, *names]
    try:
        code, text = run(argv, stack.SHOW_TIMEOUT_S)
    except (OSError, ValueError):
        return None
    if code != 0:
        return None
    blocks = {}
    for block in re.split(r"\n\s*\n", text.strip("\n")):
        shown = stack.parse_show(block)
        if shown.get("Id"):
            blocks[shown["Id"]] = shown
    return blocks


def _verdicts(blocks, scopes) -> dict:
    """``{scope: verdict}`` (``_scope_verdict``) for ``scopes`` from the blocks
    ``_show_blocks`` read. A unit with no block of its own is unknown, and so is
    every unit when systemd did not answer (``blocks`` None)."""
    if blocks is None:
        return dict.fromkeys(scopes, "unknown")
    return {
        scope: _scope_verdict(blocks[scope]) if scope in blocks else "unknown"
        for scope in scopes
    }


def _scopes_tasks(run, scopes) -> dict:
    """``_verdicts`` for ``scopes`` from ONE ``systemctl --user show`` of all of
    them."""
    return _verdicts(_show_blocks(run, scopes), scopes)


def _heavy_log_candidates(log_path, now):
    """``(units, doubt)``: the jobs the gate's log says run, as the names of their
    scopes' units, and why the log cannot be relied on (None when it can). A job
    is one whose scope was launched (``launching`` or ``start`` in the log,
    within ``HEAVY_LOG_MAX_AGE_S``) and has no terminal record with its scope
    confirmed empty (``heavy_gate.running_jobs`` and ``_ends``: a ``queued``
    after an ``interrupted``, or a ``not_empty``, do not change that)."""
    try:
        records = _heavy_records(log_path)
    except FileNotFoundError:
        return [], "log_missing"
    except OSError:
        return [], "log_unreadable"
    ended, latest = set(), {}
    for rec in records:
        unit, event = rec.get("unit"), rec.get("event")
        if not isinstance(unit, str) or not HEAVY_UNIT.fullmatch(unit):
            continue
        if event in HEAVY_TERMINAL and rec.get("emptied", True) is True:
            ended.add(unit)
        elif event in ("launching", "start") and (
            event == "start" or unit not in latest
        ):
            latest[unit] = rec
    t_now = now()
    candidates = []
    for unit, rec in latest.items():
        if unit in ended:
            continue
        at = _epoch(rec.get("ts"))
        if at is not None and t_now - at > HEAVY_LOG_MAX_AGE_S:
            continue
        candidates.append((at if at is not None else t_now, unit, rec))
    if len(candidates) > HEAVY_MAX_UNITS:
        return [], "too_many_candidates"
    units, doubt = [], None
    for _, unit, rec in sorted(candidates, reverse=True):
        if rec.get("mode", "scope") != "scope":
            doubt = doubt or "not_a_scope"  # (no systemd evidence for it)
        else:
            units.append(unit)
    return units, doubt


def heavy_jobs_running(log_path, run, now=time.time) -> dict:
    """Does a heavy job of ``heavy_gate.py`` really run? ``{"state":
    "clear"|"running"|"unknown", "count", "kinds", "empty", "why"}``.

    **Two sources, and their union decides** (the gate's own ``wait-clear`` also
    uses its log and the scopes systemd lists):

    * the gate's log (``_heavy_log_candidates``): a job it launched and did not
      close, within the last ``HEAVY_LOG_MAX_AGE_S`` and the part of the log that
      is read;
    * the heavy-gate scopes systemd has loaded, whoever launched them and
      wherever they logged: a gate run with another ``HEAVY_GATE_LOG`` or
      ``LEVI_HUB_DIR`` still honours the fixed marker, and a job started long ago
      or logged outside the part read is still a job. The gate's own naming
      (``HEAVY_UNIT``) decides which units are jobs (a ``heavy-gate-probe-*``
      scope is not).

    Both are answered by ONE ``systemctl --user show`` (the glob and the log's
    candidates). A job counts only while its scope still holds a process
    (``TasksCurrent`` > 0). A scope that is listed and holds none is a left-over
    (the open problem of stale ``heavy-gate-*.scope`` units; about 18 on this
    machine): it does not count (``empty``); one that is inactive or failed and
    still holds a process does count (the gate treats it as not cleared). Whatever
    cannot be told is ``unknown`` (never "clear"): no log, an unreadable log, a
    systemd that does not answer (or a glob it did not expand), a scope without a
    task count, more candidates than ``HEAVY_MAX_UNITS``, a job that did not run
    in a scope. ``running`` wins over ``unknown``: a job that is seen decides.

    ``run`` is the host's ``(argv, timeout_s) -> (code, text)``; nothing but
    ``systemctl --user show`` is asked of it."""
    listed, doubtful = _heavy_log_candidates(log_path, now)
    blocks = _show_blocks(run, [HEAVY_GLOB, *(u + ".scope" for u in listed)])
    running, empty = set(), set()
    if blocks is None or any("*" in name for name in blocks):
        # (an old systemd prints a not-found block named like the glob: that
        # proves nothing)
        doubtful = doubtful or "systemd_unreadable"
    else:
        # the log's candidates one by one: a unit systemd gave no block for is
        # unknown, a scope that is not loaded is gone
        verdicts = _verdicts(blocks, [u + ".scope" for u in listed])
        for unit in listed:
            verdict = verdicts[unit + ".scope"]
            if verdict == "running":
                running.add(unit)
            elif verdict == "empty":
                empty.add(unit)
            elif verdict == "unknown":
                doubtful = doubtful or "systemd_unreadable"
        # every heavy-gate scope that is loaded, whoever launched it
        for name, shown in blocks.items():
            unit = name.removesuffix(".scope")
            if name == unit or not HEAVY_UNIT.fullmatch(unit):
                continue  # (a probe scope, a service, anything that is not a job)
            verdict = _scope_verdict(shown)
            if verdict == "running":
                running.add(unit)
            elif verdict == "empty":
                empty.add(unit)
            elif verdict == "unknown":
                doubtful = doubtful or "systemd_unreadable"
    kinds = sorted({HEAVY_UNIT.fullmatch(u).group(1) for u in running})
    if running:
        return {
            "state": "running",
            "count": len(running),
            "kinds": kinds,
            "empty": len(empty),
            "why": None,
        }
    if doubtful:
        return {
            "state": "unknown",
            "count": 0,
            "kinds": [],
            "empty": len(empty),
            "why": doubtful,
        }
    return {
        "state": "clear",
        "count": 0,
        "kinds": [],
        "empty": len(empty),
        "why": None,
    }


@dataclass
class ActHost:
    """What ``Act`` reaches beyond ``stack.Host``."""

    base: stack.Host
    state_root: object = _state_root  # () -> the folder of this page's records
    fci_probe: object = tcp_probe  # (ip, port) -> {"state"}
    arm_lock: str = site.ARM_LOCK_FILE
    path_exists: object = os.path.exists
    workspace_ok: object = workspace_is_main  # () -> the main checkout's workspace?
    cgroup_procs: object = cgroup_procs  # (control group) -> [pids] | None
    background: bool = True  # slow work in a thread (tests run it inline)
    # Two independent commands of one check run at the same time (the checks
    # before C, the FCI check beside the check of B). False: one after the other,
    # for the tests that need one order of the fake machine's calls. A function
    # ``(calls) -> results`` runs them its own way (the timing tests).
    parallel: object = True
    heavy_jobs: object = heavy_jobs_running  # (log path, run, now) -> its answer
    sleep: object = time.sleep


def production_host(base=None) -> ActHost:
    return ActHost(base or stack.production_host())


_RUN_PROBES: list = []


def register_run_probe(probe) -> None:
    """Tell this module how to ask "is a hosted evaluation run active?"
    (``probe() -> bool``; the run package calls this once). Until then, and
    when the probe raises, the answer is None: a run may be active."""
    _RUN_PROBES[:] = [probe]


def hosted_run_active():
    """True/False, or None when it cannot be told (no probe yet, or the probe
    raised). A start treats None as "a run may be active". A stop does not:
    the way out must not depend on a run supervisor that is broken (RIG-04),
    and a client connected to C is judged by the socket table anyway."""
    if not _RUN_PROBES:
        return None
    try:
        return bool(_RUN_PROBES[0]())
    except Exception:  # noqa: BLE001 - an unreadable run state blocks a start
        return None


def default_setup():
    """``(ControlLoad, Specs | None)`` from the control file; specs only for a
    valid file with the stack switched on."""
    loaded = control_site.read()
    specs = None
    if loaded.state == "ok" and loaded.site.stack.enabled:
        try:
            specs = stack_units.real_specs(loaded.site.stack)
        except ValueError:
            specs = None
    return loaded, specs


# --- what one look at the stack found ---------------------------------------------------


@dataclass
class Ctx:
    r: dict
    loaded: object
    specs: object
    holding: bool
    records: dict
    states: dict  # key -> terminal state dict
    terms: dict  # stack terminals with B as this module knows it
    gates: dict
    marker: dict
    table: object
    arm_lock: object  # list of pids, [] or None
    run_active: object
    op: dict | None  # the latest operation, as the page sees it
    fci: dict | None
    b_probe: dict | None
    c_pre: dict | None
    health_matches: bool
    enabled: bool = False
    busy: bool = False  # an operation is running (any, not only the latest)
    # an operation other than the check of B is running (the start of A does not
    # wait for that one)
    busy_main: bool = False
    extra: dict = field(default_factory=dict)


def _same_path(a, b) -> bool:
    """The same file by name (no link is followed): ``~`` and ``..`` settled."""
    return os.path.normpath(os.path.expanduser(str(a))) == os.path.normpath(
        os.path.expanduser(str(b))
    )


# The reads the chain and the document work from are cached for about 2 s, so
# ``now`` can be a little OLDER than a check that finished since: such a result is
# fresh, not "from the future". Found on the first real use of Start whole stack
# (21 Oct): the B probe finished, the next tick read a cached ``now`` 1 s before it,
# the age came out negative, B read "unknown" and the chain ended b_not_seen.
FRESH_SKEW_S = 5.0


def _fresh(item, ttl, now) -> bool:
    return bool(item) and -FRESH_SKEW_S <= now - item.get("at", 0) <= ttl


class Act:
    """The actions and the ``act`` document. One per ``stack.Stack``."""

    def __init__(self, stack_obj, host: ActHost, setup=default_setup):
        self.stack = stack_obj
        self.host = host
        self._setup = setup
        self._op: dict | None = None  # the latest operation (what the page shows)
        self._ops: list = []  # the operations that are running
        self._op_lock = threading.Lock()
        self._seq: dict = {"state": "idle"}
        # Held for an instant, never across a command: the document reads the
        # sequence through it on every GET. ``_adv_lock`` serialises the steps
        # (``advance``), which do run commands.
        self._seq_lock = threading.RLock()
        self._adv_lock = threading.Lock()
        self._ticker: threading.Thread | None = None
        # Held to start the ticker and to let it go: a sequence begun while the
        # ticker is finishing (a stop pressed again the moment one ended) must
        # not be left without one.
        self._ticker_lock = threading.Lock()
        # Anything that changes what the next look would find (a step that moved
        # on, an operation that ended) counts up ``_progress`` and wakes the
        # ticker, so the sequence looks again at once instead of after its pause.
        self._wake = threading.Event()
        self._progress = 0
        # Did the last look of the sequence find a unit of A (True), none (False)
        # or could not tell (None)? It sets the pause of the ticker.
        self._a_unit = False
        # Called after such a change (the product sets it to forget the page's
        # cached read of the stack: ``stack_api.invalidate``).
        self.on_change = None
        self._ready_at: dict = {}  # (key, log name) -> when it first was ready
        self._trimmed_at: dict = {}  # key -> when its log was last looked at
        self._resumed = False
        self._fci: dict | None = None
        self._b_probe: dict | None = None
        self._c_pre: dict | None = None
        self._gripper: dict = {"last": None}
        self._tls = (
            threading.local()
        )  # work to run once a request has let go of its locks

    # -- small reads -------------------------------------------------------------
    def now(self) -> float:
        return self.host.base.now()

    def _loaded(self):
        return self._setup()

    def enabled(self) -> bool:
        loaded, specs = self._loaded()
        return loaded.state == "ok" and loaded.site.stack.enabled and specs is not None

    def require_enabled(self) -> None:
        if not self.enabled():
            raise ActRefuse("control_disabled")

    def _folder(self, key=None) -> Path:
        """``<state>/marvel0/stack[/<key>]`` (0700), created on demand."""
        root = Path(self.host.state_root())
        names = [] if key is None else [key]
        try:
            root.mkdir(parents=True, exist_ok=True)
            for parent in (root.parent, root):
                record._check_dir(parent, True)
            folder = root
            for name in names:
                folder = folder / name
                record._check_dir(folder, True)
            if key in stack_units.KEYS:
                record._check_dir(folder / "logs", True)
        except (record.StateDirUnusable, OSError) as exc:
            raise ActRefuse("record_unusable") from exc
        return folder

    def _record(self, key):
        """The unit's record, None when there is none, ``{"phase": "corrupt"}``
        when it cannot be read. Reads create nothing."""
        folder = Path(self.host.state_root()) / key
        try:
            return record.read_record(folder)
        except (record.RecordCorrupt, OSError):
            return {"phase": "corrupt"}

    def _log_path(self, key, rec):
        """The unit's log as the record names it, only when it is one of the
        page's own log files (a tampered record is not a way to read others)."""
        raw = (rec or {}).get("log")
        if not isinstance(raw, str):
            return None
        path = Path(raw)
        folder = Path(self.host.state_root()) / key / "logs"
        if path.parent != folder or not LOG_NAME.fullmatch(path.name):
            return None
        return path

    def _write_record(self, key, **fields) -> dict:
        """``fields`` merged into the unit's record, read and written under the
        record's lock (workers and requests both write it)."""
        return record.update_record(self._folder(key), **fields)

    # -- the marker ---------------------------------------------------------------
    def _heavy(self) -> dict:
        """The heavy jobs of this computer now (``heavy_jobs_running``, or what
        the host replaces it with); a failure to ask is "unknown"."""
        try:
            log = Path(self.host.base.marker()).parent / HEAVY_LOG_NAME
            got = self.host.heavy_jobs(log, self.host.base.run, self.now)
            state = got.get("state")
            if state not in HEAVY_STATES:
                raise ValueError("odd answer")
            return {
                "state": state,
                "count": int(got.get("count") or 0),
                "kinds": [str(k) for k in got.get("kinds") or []],
                "empty": int(got.get("empty") or 0),
                "why": got.get("why"),
            }
        except Exception:  # noqa: BLE001 - what cannot be told is not "clear"
            return {
                "state": "unknown",
                "count": 0,
                "kinds": [],
                "empty": 0,
                "why": "heavy_failed",
            }

    def _marker_info(self, view, heavy=None) -> dict:
        """The marker as the page shows it: the stack's view of it plus who
        made it (``ours``), the heavy jobs it waits for (``heavy``: asked only
        while the marker is there) and how long is left to wait.

        A start waits until the marker is ``wait_s`` old **and** no heavy job
        runs. ``wait_s`` is the control file's ``marker_wait_s`` (a few seconds)
        when the heavy jobs are known; when they cannot be told it is the fixed
        wait of old (at least ``LEGACY_MARKER_WAIT_S``). While a job runs the
        remaining time is not known (``remaining_s`` None): the job yields
        within about 30 s."""
        _, specs = self._loaded()
        configured = (
            specs.marker_wait_s if specs else control_site.DEFAULT_MARKER_WAIT_S
        )
        wait = configured
        if heavy is not None and heavy["state"] == "unknown":
            wait = max(configured, control_site.LEGACY_MARKER_WAIT_S)
        age = view.get("ctime_age_s")
        remaining = None
        running = heavy is not None and heavy["state"] == "running"
        if view["state"] == "present" and age is not None and not running:
            remaining = max(0.0, wait - age)
        return {
            "state": view["state"],
            "age_s": None if view.get("age_s") is None else round(view["age_s"], 1),
            "ctime_age_s": None if age is None else round(age, 1),
            "ours": self._marker_is_ours(),
            "wait_s": wait,
            "configured_wait_s": configured,
            "remaining_s": None if remaining is None else round(remaining, 1),
            "heavy": None if heavy is None else dict(heavy),
        }

    def _marker_record(self):
        try:
            return record.read_record(Path(self.host.state_root()) / "marker")
        except (record.RecordCorrupt, OSError):
            return None

    def _marker_is_ours(self) -> bool:
        mine = self._marker_record()
        if not mine:
            return False
        try:
            st = os.lstat(self.host.base.marker())
        except OSError:
            return False
        return (
            stat.S_ISREG(st.st_mode)
            and mine.get("inode") == st.st_ino
            and mine.get("dev") == st.st_dev
            and mine.get("mtime_ns") == st.st_mtime_ns
        )

    def _remember_marker(self, how) -> None:
        st = os.lstat(self.host.base.marker())
        folder = self._folder("marker")
        with record.locked(folder):
            record.write_record(
                folder,
                {
                    "inode": st.st_ino,
                    "dev": st.st_dev,
                    "mtime_ns": st.st_mtime_ns,
                    "how": how,
                    "at": round(self.now(), 3),
                },
            )

    # -- reading everything -------------------------------------------------------
    def _terminal_m(self, r) -> dict:
        term, unit = r["terms"]["M"], r["m_unit"]
        fresh = r["health"]["state"] in ("ok", "red")
        mapping = {
            "starting": "starting",
            "stopping": "stopping",
            "failed": "failed",
            "foreign": "foreign_running",
            "elsewhere": "foreign_running",
            "stopped": "idle",
            "unknown": "unknown",
        }
        if term["state"] == "running":
            state = "ready" if fresh else "starting"
        else:
            state = mapping.get(term["state"], "unknown")
        return {
            "state": state,
            "reason": term["code"],
            "failure": None,
            "unit_state": unit["state"],
        }

    def _terminal(self, key, ctx_in) -> dict:
        """The state of A, B or C: from its unit, its record, its log and the
        health file, never from the unit alone."""
        r, specs = ctx_in["r"], ctx_in["specs"]
        now = r["now"]
        shown = r["shown"][key.upper()]
        rec = ctx_in["records"][key]
        spec = specs.units[key] if specs else None
        term = ctx_in["terms"][key.upper()]
        base = {
            "unit_state": None,
            "failure": None,
            "readiness": None,
            "owned": False,  # our unit, running our command: ours to stop
        }
        if shown is None:
            return {**base, "state": "unknown", "reason": f"{key}.systemd_unreadable"}
        if rec and rec.get("phase") == "corrupt":
            return {**base, "state": "unknown", "reason": f"{key}.record_corrupt"}
        loaded = shown.get("LoadState") != "not-found"
        phase = (rec or {}).get("phase")
        if loaded:
            active = shown.get("ActiveState")
            base["unit_state"] = active
            if not stack_units.same_unit(shown, rec):
                return {
                    **base,
                    "state": "foreign_running",
                    "reason": f"{key}.unit_foreign",
                }
            return self._own_unit(key, spec, shown, rec, base, ctx_in, now)
        if phase in ("intent", "started"):
            # (a duplicate launch of A exits 0 and its transient unit is
            # collected at once: the log is then the only thing that says why)
            return {
                **base,
                "state": "dead",
                "reason": f"{key}.unit_gone",
                "failure": self._vanished_failure(key, spec, rec, ctx_in),
            }
        if self._draining(key, rec, ctx_in["health"], now, r["sockets"]["state"]):
            return {**base, "state": "idle", "reason": f"{key}.sockets_draining"}
        if key == "b":
            return self._b_idle(base, ctx_in["b_probe"], now)
        if term["state"] == "running":
            return {
                **base,
                "state": "foreign_running",
                "reason": f"{key}.foreign_running",
            }
        if term["state"] == "stopped":
            return {**base, "state": "idle", "reason": term["code"]}
        return {**base, "state": "unknown", "reason": term["code"]}

    def _vanished_failure(self, key, spec, rec, ctx_in):
        """The failure rule the log of a unit that vanished matches (None for
        one that had been ready: it ended after a good run, whatever the
        reason)."""
        path = self._log_path(key, rec)
        if spec is None or path is None or self._latched(key, rec) is not None:
            return None
        got = stack_units.read_lines(
            spec,
            stack_units.log_lines(path),
            ctx_in["health"],
            ctx_in["listening"],
        )
        return got["failure"]

    def _draining(self, key, rec, health, now, sockets) -> bool:
        """Are the arm's sockets, with no unit of A anywhere, the page's own
        leftovers? A socket to the arm outlives A for about a minute (TIME_WAIT,
        Linux closes the side that ended first for 60 s) and so does the one the
        FCI check makes, and ``arm_guard`` counts both as a control connection.

        What says that no controller of someone else sits behind them is the
        health monitor, and what the real one writes is not what one would
        guess (fr3_health_monitor.py:115-120 and :349-359): with **no controller
        manager** ``hardware_active`` is ``None`` (the service did not answer,
        or no Franka component was listed), and it is ``False`` only while a
        manager answers with the hardware not active, that is, while an A is
        starting or going down. So:

        - after the page's own **stop** of A (the record's ``stopped_at``): not
          ``True``, because the monitor may still show the hardware going down;
        - after its own **FCI check** (``self._fci``): ``None`` only, because a
          manager answering after the check is somebody's A starting up, not
          the check's socket.

        Both need a monitor that is looking (a fresh file: with the monitor off
        the sockets may be a real controller's) and sockets that are *known* to
        be active (an unreadable table is not "leftovers"). The window is
        ``DRAIN_S``."""
        if key != "a" or sockets != "active":
            return False
        if health.get("state") not in ("ok", "red"):
            return False
        hardware = health.get("hardware_active")
        if hardware is True:
            return False

        def recent(mark):
            # (``stopped_at`` is rounded to the millisecond, up as well as down:
            # a read in the same millisecond finds the mark a hair ahead of it)
            return (
                isinstance(mark, (int, float))
                and -DRAIN_SKEW_S <= now - mark <= DRAIN_S
            )

        if recent((rec or {}).get("stopped_at")):
            return True
        return hardware is None and recent((self._fci or {}).get("at"))

    def _b_idle(self, base, probe, now) -> dict:
        if not probe:
            return {**base, "state": "unknown", "reason": "b.not_probed"}
        if not _fresh(probe, B_PROBE_TTL_S, now):
            # Asked once, long ago (or by a clock that has since gone back): it
            # says nothing about B now, and never reads as "absent".
            return {**base, "state": "unknown", "reason": "b.probe_stale"}
        if probe["state"] == "absent":
            return {**base, "state": "idle", "reason": "b.not_running"}
        if probe["state"] == "present":
            return {**base, "state": "foreign_running", "reason": "b.foreign_running"}
        # asked, and the command ended without saying (ended too early, or
        # nothing could run it): not the same as never asked
        return {**base, "state": "unknown", "reason": "b.probe_no_answer"}

    def _own_unit(self, key, spec, shown, rec, base, ctx_in, now) -> dict:
        active = shown.get("ActiveState")
        base["unit_state"] = active
        base["owned"] = True
        if active == "deactivating":
            return {**base, "state": "stopping", "reason": f"{key}.stopping"}
        if active == "failed":
            return {
                **base,
                "state": "failed",
                "reason": f"{key}.unit_failed",
                "failure": "unit_failed",
            }
        if active not in ("active", "activating", "reloading"):
            return {**base, "state": "dead", "reason": f"{key}.unit_gone"}
        if spec is None:
            # (the control file went away: the unit is still ours to stop)
            return {**base, "state": "unknown", "reason": f"{key}.no_specs"}
        path = self._log_path(key, rec)
        latched = self._latched(key, rec)
        # Once ready, the log's rules (the start-up lines and their counts) are
        # not read again: /startimp, the reset paths and a Flask 500 print C's
        # start-up lines or a Traceback long after (franka_server_ros2.py:318-320,
        # :751-754), and that is not a failed server.
        late = latched is not None and stack_units.late_rules(spec)
        lines = (
            stack_units.log_lines(path) if path and (latched is None or late) else []
        )
        listening = ctx_in["listening"]
        got = stack_units.read_lines(
            spec, lines, ctx_in["health"], listening, latched=latched is not None
        )
        waited = max(0.0, now - (rec or {}).get("started_at", now))
        readiness = {
            "seen": got["seen"],
            "missing": got["missing"],
            "waited_s": round(waited, 1),
            "timeout_s": spec.ready_timeout_s,
        }
        base["readiness"] = readiness
        if latched is not None:
            self._trim(key, path, now)
            if got["failure"]:  # (a process of the launch died after the start)
                return {
                    **base,
                    "state": "failed",
                    "reason": f"{key}.failed",
                    "failure": got["failure"],
                }
            if got["missing"]:
                # The health file went stale, or the port has no listener: the
                # unit was ready and nothing says it stopped. Not a failure and
                # not "did not become ready in time".
                return {**base, "state": "unknown", "reason": f"{key}.health_lost"}
            return {**base, "state": "ready", "reason": f"{key}.ready"}
        if got["failure"]:
            return {
                **base,
                "state": "failed",
                "reason": f"{key}.failed",
                "failure": got["failure"],
            }
        if got["duplicate"]:
            return {
                **base,
                "state": "failed",
                "reason": f"{key}.failed",
                "failure": "duplicate_text",
            }
        if got["ready"]:
            self._latch(key, rec, now, self._is_real(ctx_in["r"]))
            return {**base, "state": "ready", "reason": f"{key}.ready"}
        if waited > spec.ready_timeout_s:
            return {
                **base,
                "state": "failed",
                "reason": f"{key}.failed",
                "failure": "ready_timeout",
            }
        return {**base, "state": "starting", "reason": f"{key}.starting"}

    def _latched(self, key, rec):
        """When this run of the unit first was ready (None: it has not been).
        Kept in the record, and in memory in case the record cannot be written."""
        if not rec or rec.get("phase") == "corrupt":
            return None
        at = rec.get("ready_at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            return at
        return self._ready_at.get((key, Path(str(rec.get("log"))).name))

    def _latch(self, key, rec, now, persist=True) -> None:
        """Remember that this run is ready. Only the run the caller looked at
        gets it: a start that replaced the record meanwhile does not inherit
        the latch."""
        name = Path(str(rec.get("log"))).name
        self._ready_at[(key, name)] = round(now, 3)
        if not persist:
            return  # (an instance that only shows the stack writes nothing)
        folder = Path(self.host.state_root()) / key
        try:
            with record.locked(folder):
                current = record.read_record(folder)
                if (
                    current
                    and current.get("started_at") == rec.get("started_at")
                    and current.get("log") == rec.get("log")
                    and "ready_at" not in current
                ):
                    record.write_record(folder, {**current, "ready_at": round(now, 3)})
        except (OSError, record.RecordCorrupt):
            LOG.warning("the ready latch of unit %s could not be written", key)

    def _trim(self, key, path, now) -> None:
        """Cut a ready unit's log to its tail once it passes the size cap (C's
        werkzeug logs every request). Looked at once a minute."""
        last = self._trimmed_at.get(key)
        if path is None or (last is not None and 0 <= now - last < LOG_TRIM_EVERY_S):
            return
        self._trimmed_at[key] = now
        stack_units.trim_log(
            path, cap=stack_units.LOG_CAP_BYTES, keep=stack_units.LOG_KEEP_BYTES
        )

    def _context(self, r, holding=False) -> Ctx:
        loaded, specs = self._loaded()
        keys = stack_units.KEYS
        records = {k: self._record(k) for k in keys}
        table = self.host.base.net_table()
        port = specs.server_port if specs else None
        listening = (
            None if not table.ok or port is None else bool(net.listeners(table, port))
        )
        terms = copy.deepcopy(r["terms"])
        pre = {
            "r": r,
            "specs": specs,
            "records": records,
            "terms": terms,
            "health": r["health"],
            "listening": listening,
            "b_probe": self._b_probe,
        }
        states = {k: self._terminal(k, pre) for k in keys}
        states["m"] = self._terminal_m(r)
        b_word = {
            "idle": "stopped",
            "dead": "stopped",
            "unknown": "unknown",
        }.get(states["b"]["state"], "running")
        terms["B"] = {
            "state": b_word,
            "evidence": [],
            "code": states["b"]["reason"],
        }
        if states["a"]["reason"] == "a.sockets_draining":
            # The arm's sockets are the page's own leftovers (``_draining``), but
            # the passive layer reads any active socket as "A runs"
            # (stack.py:683-684) and would keep the monitor from being stopped for
            # the minute that follows. A is stopped here; its start still waits for
            # quiet sockets (``gate.arm_sockets_clear`` reads them itself).
            terms["A"] = {
                "state": "stopped",
                "evidence": terms["A"]["evidence"],
                "code": "a.sockets_draining",
            }
        gates = stack.gates(
            r["profile"],
            {"state": "free"} if holding else r["lock"],
            r["marker"],
            r["sockets"],
            r["health"],
            r["rt"],
            r["linger"],
            r["units"],
            terms,
        )
        health_file = None
        with contextlib.suppress(Exception):
            health_file = self.host.base.health_file()[0]
        expected = loaded.site.stack.health_file if loaded.state == "ok" else None
        lock_path = self.host.arm_lock
        arm_lock = gpumgr.flock_holders(lock_path, locks=self.host.base.locks_path)
        # (heavy jobs matter only while the marker is there, it is what they
        # yield to, and only until A has a unit: once A runs nothing waits for
        # them any more)
        heavy = (
            self._heavy()
            if r["marker"]["state"] == "present" and r["units"]["A"] is not True
            else None
        )
        return Ctx(
            r=r,
            loaded=loaded,
            specs=specs,
            holding=holding,
            records=records,
            states=states,
            terms=terms,
            gates=gates,
            marker=self._marker_info(r["marker"], heavy),
            table=table,
            arm_lock=arm_lock,
            run_active=hosted_run_active(),
            op=self._op_view(),
            busy=self._busy(),
            busy_main=self._busy(main_only=True),
            fci=self._fci,
            b_probe=self._b_probe,
            c_pre=self._c_pre,
            health_matches=bool(
                health_file and expected and _same_path(health_file, expected)
            ),
            enabled=loaded.state == "ok"
            and loaded.site.stack.enabled
            and specs is not None,
        )

    # -- blockers -----------------------------------------------------------------
    def _checks(self, key, ctx: Ctx) -> list:
        """The gate rows of a start of ``key``: ``stack.gates``' own, with the
        ones this module can now answer answered, and this module's added."""
        now = ctx.r["now"]
        rows = copy.deepcopy(ctx.gates[key.upper()]["checks"])
        client = ctx.terms["client"]
        fci_ok = _fresh(ctx.fci, FCI_TTL_S, now) and ctx.fci.get("state") == "open"
        client_ok = {
            "running": False,
            "unknown": True if client.get("code") == "client.not_connected" else None,
        }.get(client["state"])
        for item in rows:
            code = item["code"]
            if code == "gate.fci_probe":
                item["ok"] = fci_ok
                item["params"] = {"ttl_s": int(FCI_TTL_S)}
            elif code == "gate.no_client":
                item["ok"] = client_ok
            elif code == "gate.lock_free" and ctx.holding:
                item["ok"] = True
            elif code == "gate.product":
                item["ok"] = bool(item["ok"]) and self._workspace_ok()
        rows += self._extra_checks(key, ctx, now)
        return rows

    def _workspace_ok(self) -> bool:
        try:
            return bool(self.host.workspace_ok())
        except Exception:  # noqa: BLE001 - what cannot be told is not "the main one"
            return False

    def _is_real(self, r) -> bool:
        """The product LEVI (``stack.profile``) with the main checkout's
        workspace (design record 2.12)."""
        return r["profile"]["profile"] == "real" and self._workspace_ok()

    def _extra_checks(self, key, ctx: Ctx, now) -> list:
        st = ctx.states
        port = ctx.specs.server_port if ctx.specs else None

        def c(code, ok, **params):
            return stack._check(code, ok, params=params)

        # (the check of B alone is no reason to hold back the start of A: it is
        # a read, and the sequence waits for its answer where it needs it)
        rows = [
            c("control_health_file", ctx.health_matches),
            c("operation_running", not (ctx.busy_main if key == "a" else ctx.busy)),
            c("record_open", st[key]["state"] not in ("dead", "failed")),
            c("run_active", ctx.run_active is False),
            c("arm_lock_held", None if ctx.arm_lock is None else not ctx.arm_lock),
        ]
        # (a missing marker is gate.marker_set's to say)
        if key == "a" and ctx.marker["state"] == "present":
            wait, age = ctx.marker["wait_s"], ctx.marker["ctime_age_s"]
            rows.append(
                c(
                    "marker_young",
                    None if age is None else age >= wait,
                    age_s=age,
                    wait_s=wait,
                    remaining_s=ctx.marker["remaining_s"],
                )
            )
            heavy = ctx.marker.get("heavy")
            if heavy and heavy["state"] == "running":
                rows.append(
                    c(
                        "heavy_jobs_running",
                        False,
                        count=heavy["count"],
                        kinds=", ".join(heavy["kinds"]),
                    )
                )
            rows.append(
                stack._check("marker_not_ours", ctx.marker["ours"], level="warn")
            )
        # A stage needs the one before it ready, not merely running: a stack that
        # failed while its sockets are still connected is running (rule A of
        # ``stack.gates`` reads the arm's sockets), and the checks before C are
        # good for 300 s.
        if key in ("b", "c"):
            a_state = st["a"]["state"]
            rows.append(c("a_not_ready", a_state == "ready", state=a_state))
        if key == "c":
            rows.append(c("gripper_b_not_ready", st["b"]["state"] == "ready"))
        if key == "b":
            serial = ctx.specs.com_port if ctx.specs else None
            rows.append(
                c(
                    "serial_missing",
                    bool(serial) and bool(self.host.path_exists(serial)),
                )
            )
        if key == "c":
            pre = ctx.c_pre
            rows.append(
                c(
                    "c_preflight",
                    _fresh(pre, C_PREFLIGHT_TTL_S, now) and bool(pre.get("ok")),
                    ttl_s=int(C_PREFLIGHT_TTL_S),
                )
            )
            rows.append(c("health_not_ok", self._health_ok(ctx.r["health"])))
            if port is not None:
                free = None if not ctx.table.ok else not net.listeners(ctx.table, port)
                rows.append(c("port_taken", free, port=port))
        return rows

    @staticmethod
    def _health_ok(health) -> bool:
        return (
            health["state"] == "ok"
            and health.get("hardware_active") is True
            and health.get("robot_mode_name") in ("IDLE", "MOVE")
        )

    def _stopping(self) -> bool:
        """Is a guided stop live? Nothing is started, set or tested meanwhile:
        the stop would meet what it is stopping (C started while A is told to
        stop, a gripper test while B is)."""
        with self._seq_lock:
            return self._seq.get("kind") == "stop" and self._seq_live(self._seq)

    def start_blockers(self, key, ctx: Ctx) -> list:
        if not ctx.enabled:
            return [row("control_disabled")]
        out = []
        if self._stopping():
            out.append(row("sequence_stopping"))
        settled = self._settled_by_a_press(key, ctx)
        for item in self._checks(key, ctx):
            if item["level"] == "person" or item["ok"] is True:
                continue
            if settled and item["code"] in ("gate.b_absent", "gate.layers_known"):
                continue  # (said once below)
            out.append(row(item["code"], item["level"], **item["params"]))
        if settled:
            probe = ctx.b_probe
            age = None if not probe else round(max(0.0, ctx.r["now"] - probe["at"]), 1)
            if self._b_absence_unconfirmed(ctx):
                out.append(row("b_probe_unconfirmed", "warn", age_s=age))
            else:
                out.append(row("b_probe_stale", "warn", age_s=age))
        return out

    @staticmethod
    def _b_absence_unconfirmed(ctx: Ctx) -> bool:
        """Does the page know that no gripper stack runs only from ONE answer of
        the ROS graph? That answer is enough for A's start (the guided start must
        not wait for a second), but not for B's: a graph that has not discovered
        the gripper stack's participants yet reads "absent" too, and a second
        gripper stack on the same RS-485 port is a motion risk. B starts only on
        a confirmed absence (two answers of the graph in a row, or the controller
        manager's own word, or B's own stop: ``_probe_b``)."""
        probe = ctx.b_probe
        return (
            ctx.states["b"]["reason"] == "b.not_running"
            and bool(probe)
            and not probe.get("confirmed")
        )

    @staticmethod
    def _settled_by_a_press(key, ctx: Ctx) -> bool:
        """Is B's start held back for one reason only, a check that a press can
        settle? Either B is unknown because there is no fresh check whether a
        gripper stack runs (never made, or too old) with every other layer
        known, or the only answer is a first, unconfirmed "absent" of the graph
        (``_b_absence_unconfirmed``). A press on B's start then makes the check
        (``_do_start``) instead of being refused for a thing it can settle: with
        A up the check of A cannot be made, and the old check was the only way
        to know. Nothing is started by that press, whichever it is."""
        return key == "b" and (
            (
                ctx.states["b"]["reason"] in B_ASK_AGAIN
                and stack.unknown_layers(ctx.terms) == ["B"]
            )
            or Act._b_absence_unconfirmed(ctx)
        )

    def stop_blockers(self, key, ctx: Ctx, override=False) -> list:
        """Why a stop of ``key`` cannot be sent now (rows of level ``required``
        refuse it, ``warn`` rows are shown). Needs no control file. Nothing is a
        blocker when there is nothing of ours to stop (the route then answers
        that it is already stopped).

        The way out does not depend on the way in (RIG-04): what runs beside
        it (a check, the gripper test, another stop) does not block it, a stop
        ends the checks (``_begin``), and a run probe that could not answer
        counts as "a run may be active" for a start only. A client connected to
        C still needs the override phrase."""
        r, st = ctx.r, ctx.states
        out = []
        if not self._is_real(r):
            out.append(row("gate.product"))
        state, reason = st[key]["state"], st[key]["reason"]
        if r["shown"][key.upper()] is None:
            out.append(row("systemd_unreadable"))
        elif reason.endswith("record_corrupt"):
            out.append(row("record_corrupt"))
        elif state == "foreign_running":
            out.append(
                row("unit_foreign" if reason.endswith("unit_foreign") else "not_ours")
            )
        elif state in ("idle", "unknown") and not st[key]["owned"]:
            return out
        if key in ("a", "b"):
            c = st["c"]
            # A unit of C that is the page's own and is up stays "C first"
            # whatever the evidence says: a health file gone stale (M was
            # restarted) or a control file gone (``c.health_lost``,
            # ``c.no_specs``) is the page not being able to vouch for C, not
            # C being gone. Only a C that is not ours to see (the socket table
            # or the record cannot be read) is a warning: it must not lock A
            # and B for good, and C's own stop refuses just the same.
            held = c["owned"] and c["unit_state"] in (
                "active",
                "activating",
                "deactivating",
            )
            if c["state"] not in ("idle", "unknown", "dead") or held:
                out.append(row("stop_c_first"))
            elif c["state"] == "unknown":
                out.append(row("stop_c_unknown", "warn", reason=c["reason"]))
        if ctx.run_active is True:
            out.append(row("run_active"))
        if key == "c" and not override and self._client_on_c(ctx) is not False:
            out.append(row("client_connected", port=self._c_port(ctx)))
        return out

    @staticmethod
    def _has_something(key, ctx: Ctx) -> bool:
        """Is there a unit of ours of ``key`` (or the record of one that failed
        or vanished) for a stop to act on?"""
        st = ctx.states[key]
        return bool(st["owned"]) or st["state"] in ("failed", "dead")

    def stop_all_blockers(self, ctx: Ctx) -> list:
        """Why "stop whole stack" cannot begin now (rows of level ``required``
        refuse it). Needs no control file, and nothing that only a *start*
        needs: a run probe that cannot answer, a stale health file, a check or
        the gripper test running do not count (RIG-04, the way out must not
        depend on the way in). It refuses for what a single stop would refuse
        for, and the sequence could not settle on its own: a hosted run known
        to be active, a client connected to C (or a socket table that cannot be
        read; the override phrase of C's own stop is not part of the sequence),
        systemd or a record that cannot be read, an instance that is not the
        product. A part the page did not start (``not_ours``) is not here: it
        stops the sequence at its own step. ``nothing_to_stop`` says that no
        unit of ours is there."""
        out = []
        with self._seq_lock:
            seq = self._seq
            if seq["state"] in ("running", "waiting_confirm") and (
                seq.get("kind") == "stop"
            ):
                out.append(row("sequence_active"))
        for key in STOP_ORDER:
            for item in self.stop_blockers(key, ctx):
                if item["level"] != "required" or item["code"] not in STOP_ALL_CODES:
                    continue
                code = STOP_ALL_RENAMED.get(item["code"], item["code"])
                if code not in _codes(out):
                    out.append(row(code, **item["params"]))
        if not out and not any(self._has_something(k, ctx) for k in STOP_ORDER):
            out.append(row("nothing_to_stop"))
        return out

    @staticmethod
    def _c_port(ctx: Ctx):
        if ctx.specs:
            return ctx.specs.server_port
        return (ctx.records.get("c") or {}).get("port")

    def _client_on_c(self, ctx: Ctx):
        """True/False/None: is a client connected to the robot server's port?"""
        port = self._c_port(ctx)
        if port is None or not ctx.table.ok:
            return None
        return bool(net.established(ctx.table, port))

    # -- the document -------------------------------------------------------------
    def _op_view(self) -> dict | None:
        """The latest operation, with the keys the page reads (the rest of an
        operation is bookkeeping)."""
        with self._op_lock:
            if self._op is None:
                return None
            return {k: v for k, v in self._op.items() if k in OP_PUBLIC}

    def _busy(self, main_only=False) -> bool:
        """Is an operation running? ``main_only``: leave out the checks that run
        beside the others (``SIDE_OPS``)."""
        with self._op_lock:
            return any(
                o["state"] == "running" and not (main_only and o["kind"] in SIDE_OPS)
                for o in self._ops
            )

    def _poke(self) -> None:
        """Something changed that the next look would find: wake the ticker (the
        sequence looks again at once) and tell whoever caches the page's read."""
        self._progress += 1
        self._wake.set()
        notify = self.on_change
        if notify is not None:
            try:
                notify()
            except Exception:  # a cache that cannot be dropped is no failure
                LOG.exception("the change of the arm stack could not be announced")

    def _parallel(self, *calls) -> list:
        """The results of ``calls`` (no arguments each), run at the same time
        when the host allows it, one after the other when it does not. The first
        exception is raised once all have ended. ``host.parallel`` may also be
        a function that runs the calls itself (the timing tests' clock)."""
        runner = self.host.parallel
        if callable(runner):
            return runner(list(calls))
        if len(calls) == 1 or not runner:
            return [call() for call in calls]
        results = [None] * len(calls)
        errors = [None] * len(calls)

        def work(index, call):
            try:
                results[index] = call()
            except BaseException as exc:  # noqa: BLE001 - raised below, in the caller's thread
                errors[index] = exc

        threads = [
            threading.Thread(
                target=work, args=(i, call), daemon=True, name="levi-marvel0-check"
            )
            for i, call in enumerate(calls)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for exc in errors:
            if exc is not None:
                raise exc
        return results

    def _stop_running(self, key) -> bool:
        with self._op_lock:
            return any(
                o["state"] == "running" and o["kind"] == f"stop_{key}"
                for o in self._ops
            )

    def document(self, r) -> dict:
        """The ``act`` part of the stack document. Never raises: a failure to
        read is reported in the document, not as a failed page."""
        try:
            return self._document(r)
        except Exception as exc:  # (the stack view must still render)
            LOG.exception("the arm stack's act document could not be built")
            return {"schema": SCHEMA, "enabled": False, "error": type(exc).__name__}

    def _document(self, r) -> dict:
        ctx = self._context(r)
        now = r["now"]
        stop_all = self.stop_all_blockers(ctx)
        terminals = {"m": self._doc_m(r, ctx)}
        for key in stack_units.KEYS:
            terminals[key] = self._doc_terminal(key, ctx)
        return {
            "schema": SCHEMA,
            "enabled": ctx.enabled,
            "control": control_site.view(ctx.loaded),
            "spec_profile": ctx.specs.profile if ctx.specs else None,
            "order": list(stack_units.ORDER),
            "terminals": terminals,
            "marker": {
                **ctx.marker,
                # what keeps "set marker" from being offered (a guided stop)
                "touch_blockers": [row("sequence_stopping")]
                if self._stopping()
                else [],
            },
            "sequence": self._seq_view(ctx),
            "gripper_test": self._gripper_view(ctx),
            "operation": ctx.op,
            "checks": self._checks_view(now, ctx, stop_all),
            "stop_all_blockers": stop_all,
            "linger": r["linger"],
            "rt_limits": r["rt"],
            "estop": {"text": ESTOP_TEXT},
            "scope": {"text": SCOPE_TEXT},
            "phrases": {
                **{name: stack.PHRASES[name] for name in PHRASE_NAMES},
                "client_override": CLIENT_OVERRIDE,
            },
            "attest": {k: list(v) for k, v in ATTEST_NEEDED.items()},
            "notices": self._notices(ctx),
        }

    def _doc_m(self, r, ctx: Ctx) -> dict:
        st = ctx.states["m"]
        blockers = stack.monitor_blockers(
            r["profile"], r["m_unit"], ctx.terms, r["script_ok"]
        )
        stop = [row(c) for c in blockers["stop"]]
        if any(
            ctx.states[k]["state"] not in ("idle", "unknown") for k in stack_units.KEYS
        ):
            stop.append(row("stack_running"))
        if r["units"]["B"] is not False:
            stop.append(row("b_running"))
        # (a code once: the page keys its rows by code)
        stop = [b for i, b in enumerate(stop) if b["code"] not in _codes(stop[:i])]
        start = [row(c) for c in blockers["start"]]
        return {
            "key": "m",
            "unit": stack.MONITOR_SERVICE,
            **st,
            "readiness": None,
            "can_start": not start,
            "start_blockers": start,
            "can_stop": not stop,
            "stop_blockers": stop,
            "confirm": {
                "start": stack.PHRASES["monitor_start"],
                "stop": stack.PHRASES["monitor_stop"],
            },
            "attest": {"start": [], "stop": []},
            "hazard": None,
            "stop_note": None,
        }

    def _doc_terminal(self, key, ctx: Ctx) -> dict:
        st = ctx.states[key]
        start = self.start_blockers(key, ctx)
        stop = self.stop_blockers(key, ctx)
        refusing = [b for b in stop if b["level"] == "required"]
        # Something of ours is there to stop, and nothing forbids it.
        has_unit = (st["owned"] and st["state"] != "stopping") or st["state"] in (
            "failed",
            "dead",
        )
        can_stop = not refusing and has_unit
        overridable = (
            key == "c"
            and has_unit
            and [b["code"] for b in refusing] == ["client_connected"]
        )
        code, text = HAZARDS[key]
        phrase_start = stack.PHRASES[ACTIONS[("start", key)]]
        phrase_stop = stack.PHRASES[ACTIONS[("stop", key)]]
        return {
            "key": key,
            "unit": stack.STACK_UNITS[key.upper()],
            **st,
            "can_start": not any(b["level"] == "required" for b in start)
            and (
                st["state"] in ("idle",)
                or (key == "b" and st["reason"] in B_ASK_AGAIN)  # (a press checks)
            ),
            "start_blockers": start,
            "can_stop": can_stop,
            "stop_blockers": stop,
            "confirm": {"start": phrase_start, "stop": phrase_stop},
            "attest": {"start": list(ATTEST_NEEDED[key]), "stop": []},
            "hazard": {"code": code, "text": text},
            "stop_note": self._stop_note(key, ctx),
            "override": CLIENT_OVERRIDE if key == "c" else None,
            "can_stop_with_override": overridable,
        }

    def _stop_note(self, key, ctx: Ctx):
        """What a person should know after a stop, said once it is so: C
        stopped but the arm is still held by the impedance controller (the
        health file still shows it); A gone and the arm's connection closed."""
        state, health = ctx.states[key]["state"], ctx.r["health"]
        if key == "c":
            held = health["state"] in ("ok", "red") and health.get("controller_active")
            return STOP_NOTES["c"] if state == "idle" and held else None
        rec = ctx.records.get("a") or {}
        if (
            key == "a"
            and state == "idle"
            and rec.get("phase") in ("stopped", "acknowledged")
            and ctx.r["units"]["A"] is False
            and ctx.r["sockets"]["state"] == "inactive"
        ):
            return STOP_NOTES["a"]
        return None

    def _checks_view(self, now, ctx: Ctx, stop_all=None) -> dict:
        if stop_all is None:
            stop_all = self.stop_all_blockers(ctx)

        def aged(item, ttl):
            if not item:
                return None
            return {
                **item,
                "age_s": round(max(0.0, now - item["at"]), 1),
                "fresh": -FRESH_SKEW_S <= now - item["at"] <= ttl,
            }

        return {
            "fci": aged(self._fci, FCI_TTL_S),
            "b_probe": aged(self._b_probe, B_PROBE_TTL_S),
            "c_preflight": aged(self._c_pre, C_PREFLIGHT_TTL_S),
            # Is "check the gripper stack" (``probe_b``, B's controller manager
            # only, no FCI contact) offered now? With A up it is the only way to
            # see that a stack started by hand has been stopped.
            "can_probe_b": ctx.enabled
            and not ctx.busy
            and self._probe_b_refusal(ctx.r, ctx) is None,
            # "Stop whole stack" (``sequence_stop``): needs no control file,
            # like every stop; ``stop_all_blockers`` says what refuses it.
            "can_stop_all": not any(b["level"] == "required" for b in stop_all),
        }

    def _gripper_view(self, ctx: Ctx) -> dict:
        op = ctx.op
        running = bool(op and op["state"] == "running" and op["kind"] == "gripper")
        blockers = self.gripper_blockers(ctx)
        return {
            "state": "running" if running else "idle",
            "last": self._gripper.get("last"),
            "can_run": not any(b["level"] == "required" for b in blockers),
            "blockers": blockers,
            "attest": list(ATTEST_NEEDED["gripper"]),
        }

    def _notices(self, ctx: Ctx) -> list:
        out = []
        st = ctx.states
        if st["c"]["state"] in ("ready", "starting") or (
            st["c"]["reason"] == "c.health_lost"  # (open, but no longer vouched for)
        ):
            out.append({"code": "notice.c_open_port"})
        if st["c"]["state"] != "ready" and ctx.r["health"].get("controller_active"):
            out.append({"code": "notice.arm_still_held"})
        for key in stack_units.KEYS:
            rec = ctx.records.get(key) or {}
            if (
                rec.get("phase") == "stopping"
                and st[key]["owned"]
                and st[key]["state"] != "stopping"
                and not self._stop_running(key)
            ):
                # A stop was asked for (the record says so) and nothing carries
                # it: the page restarted in the middle of it, or it failed. The
                # unit is still there; the person decides.
                out.append({"code": "notice.stop_unfinished", "params": {"unit": key}})
            if st[key]["state"] == "stopping" and (
                ctx.states[key].get("unit_state") == "deactivating"
            ):
                rec = ctx.records.get(key) or {}
                since = rec.get("stop_requested_at")
                if since and ctx.r["now"] - since > stack_units.STOPPING_RED_S:
                    out.append(
                        {"code": "notice.stopping_too_long", "params": {"unit": key}}
                    )
        return out

    # -- the gripper test's blockers ---------------------------------------------
    def gripper_blockers(self, ctx: Ctx) -> list:
        if not ctx.enabled:
            return [row("control_disabled")]
        out = []
        if self._stopping():
            out.append(row("sequence_stopping"))
        if not self._is_real(ctx.r):
            out.append(row("gate.product"))
        if ctx.states["b"]["state"] != "ready":
            out.append(row("gripper_b_not_ready"))
        if ctx.run_active is not False:
            out.append(row("run_active"))
        if self._client_on_c(ctx) is not False:
            # (not in the design record: a client driving the arm and the
            # gripper must not be raced by a button)
            out.append(row("client_connected", port=self._c_port(ctx)))
        if ctx.arm_lock is None or ctx.arm_lock:
            out.append(row("arm_lock_held"))
        if ctx.busy:
            out.append(row("operation_running"))
        if not ctx.holding and ctx.r["lock"]["state"] != "free":
            out.append(row("gate.lock_free"))
        return out

    # -- sequence view ------------------------------------------------------------
    def _seq_view(self, ctx: Ctx) -> dict:
        with self._seq_lock:
            seq = self._seq_public(self._seq)
            begun = self._seq.get("begun_at")
        seq.setdefault("kind", None)  # (idle: neither a start nor a stop)
        if seq.get("state") == "running" and seq.get("step") == "wait":
            seq["remaining_s"] = ctx.marker["remaining_s"]
        if (
            seq.get("state") == "running"
            and seq.get("kind") == "start"
            and seq.get("step") in ("marker", "probe", "wait", "arm")
        ):
            seq["wait"] = self._wait_view(ctx, seq)
        if begun is not None and seq.get("state") in ("running", "waiting_confirm"):
            seq["elapsed_s"] = round(max(0.0, ctx.r["now"] - begun), 1)
        return seq

    @staticmethod
    def _wait_view(ctx: Ctx, seq) -> dict:
        """What the start before A waits for and how long it still takes: the
        marker's age against its minimum, the heavy jobs (a count, or that the
        page cannot tell), the arm's sockets. ``holds`` names what still holds A
        back; ``remaining_s`` is None while a heavy job runs (it yields within
        about 30 s of the marker) and the sequence ends after
        ``HEAVY_WAIT_CAP_S`` of that."""
        marker, heavy = ctx.marker, ctx.marker.get("heavy")
        sockets = ctx.r["sockets"]["state"]
        holds = []
        if marker["state"] != "present":
            holds.append("marker_absent")
        else:
            if heavy and heavy["state"] == "running":
                holds.append("heavy_jobs_running")
            elif heavy and heavy["state"] == "unknown":
                holds.append("heavy_jobs_unknown")  # (the fixed wait applies)
            if (marker["remaining_s"] or 0) > 0:
                holds.append("marker_young")
        if sockets != "inactive":
            holds.append("arm_sockets")
        step_at = seq.get("step_at")
        return {
            "age_s": marker["ctime_age_s"],
            "min_s": marker["wait_s"],
            "configured_s": marker["configured_wait_s"],
            "remaining_s": marker["remaining_s"],
            "heavy": heavy,
            "sockets": sockets,
            "holds": holds,
            "cap_s": HEAVY_WAIT_CAP_S,
            "waited_s": None
            if step_at is None or seq.get("step") != "wait"
            else round(max(0.0, ctx.r["now"] - step_at), 1),
        }

    # -- writes: the common frame -------------------------------------------------
    def _write(self, body, action, act, phrase=True):
        """``Stack._write`` (replay of a request id, the typed phrase, the
        product-only rule, the machine-wide lock), with the phrase optional for
        the read-only checks. ``act(r)`` gets a read taken under the lock. Work
        that ``act`` spawns runs after the locks are let go.

        A request id is answered again, never acted on twice: from memory, and
        from the folder of answered requests after a restart of the page (the
        browser may resend a request whose answer it never saw). It is the
        same request when the action **and the body** are the same."""
        s = self.stack
        request_id = body.get("request_id")
        fingerprint = record.fingerprint(action, body)
        try:
            with s._lock:
                done = s._remember(request_id, fingerprint) or self._remembered(
                    request_id, fingerprint
                )
                if done is not None:
                    return {**done, "repeated": True}
                if phrase:
                    s._confirm(body, action)
                answer = self._under_lock(action, act)
                s._store(request_id, fingerprint, answer)
                self._persist_answer(request_id, fingerprint, answer)
        except ActRefuse as refusal:
            if refusal.keep_work:
                self._flush()  # (what the refusal says was started)
            raise
        self._flush()
        return answer

    def _remembered(self, request_id, fingerprint):
        """The answer to ``request_id`` from an earlier run of the page, None
        when there is none; ``request_id_used`` for another request."""
        folder = Path(self.host.state_root()) / "replay"
        try:
            entry = record.get_request(folder, request_id)
        except (record.RecordCorrupt, OSError, ValueError):
            return None
        if not entry:
            return None
        if entry.get("fingerprint") != fingerprint:
            raise stack.Refuse("request_id_used")
        answer = entry.get("answer")
        return answer if isinstance(answer, dict) else None

    def _persist_answer(self, request_id, fingerprint, answer) -> None:
        """Keep the answer for a repeat after a restart (the newest
        ``ANSWERS_KEPT``). The request was acted on: a failure to write this
        down is logged, never the request's."""
        try:
            folder = self._folder("replay")
            record._check_dir(folder / "requests", True)
            record.put_request(
                folder,
                request_id,
                {
                    "fingerprint": fingerprint,
                    "answer": answer,
                    "at": round(self.now(), 3),
                },
                replace=True,
            )
            record._newest(
                folder / "requests",
                re.compile(stack.REQUEST_ID.pattern + r"\.json"),
                ANSWERS_KEPT,
                set(),
            )
        except (ActRefuse, record.StateDirUnusable, OSError, ValueError, TypeError):
            LOG.warning("an answered request could not be written down")

    def _under_lock(self, action, act):
        """``Stack._locked``; work spawned by a refused ``act`` is dropped
        (unless the refusal says it was started: ``ActRefuse.keep_work``)."""
        try:
            return self.stack._locked(action, act)
        except BaseException as exc:
            if not getattr(exc, "keep_work", False):
                self._tls.pending = []
            raise

    def _locked(self, action, act):
        """``_under_lock`` for the sequence, then the spawned work (also for a
        refusal that says the work it queued was started: ``keep_work``)."""
        try:
            answer = self._under_lock(action, act)
        except ActRefuse as refusal:
            if refusal.keep_work:
                self._flush()
            raise
        self._flush()
        return answer

    def _seq_locked(self, seq, action, call, *, live=("running",), keep=None):
        """``_locked`` for a step of the sequence ``seq``: inside the machine-wide
        lock it looks once more whether ``seq`` is still the sequence and is
        still in one of the states ``live``, and does nothing (returns None)
        when it is not. A stop-all or a cancel is answered under the same lock,
        so what it answered cannot be undone by a step that looked before it
        (the step reads the machine without the lock; ``call`` starts
        something). ``keep``: the key of ``seq`` that gets the operation
        ``call`` began."""

        def guarded(r):
            with self._seq_lock:
                if self._seq is not seq or seq["state"] not in live:
                    return None
            out = call(r)
            if keep is not None:
                with self._op_lock:
                    seq[keep] = self._op
            self._poke()  # (something was done: look again at once)
            return out

        return self._locked(action, guarded)

    @staticmethod
    def _attested(key, body) -> dict:
        """The confirmations ``key`` needs, all ticked; recorded with the start.
        They are clicks, not proofs: a script can send them."""
        given = body.get("attest") or {}
        unknown = sorted(k for k in given if k not in ATTEST_KEYS)
        if unknown:
            raise ActRefuse("attest_invalid", keys=", ".join(unknown))
        missing = [k for k in ATTEST_NEEDED[key] if given.get(k) is not True]
        if missing:
            raise ActRefuse("attest_missing", keys=", ".join(missing))
        return {k: True for k in ATTEST_NEEDED[key]}

    def _begin(self, kind, key=None) -> dict:
        """Open an operation. One at a time, with one rule for the way out: a
        **stop never waits** for a read-only check (it ends them: the check is
        marked superseded and its result is dropped) or, when it is B that
        stops, for the gripper test (whose one-shot unit the stop worker stops:
        SIGINT cancels the goal). Stops of different units run side by side
        (they are ordered by ``stop_c_first``); the same stop twice is one."""
        stopping = kind.startswith("stop_")
        with self._op_lock:
            running = [o for o in self._ops if o["state"] == "running"]
            ended = []
            if stopping:
                if any(o["kind"] == kind for o in running):
                    raise ActRefuse("operation_running")
                for other in running:
                    if other["kind"] in PREEMPTED_BY_STOP or (
                        other["kind"] == "gripper" and kind == "stop_b"
                    ):
                        other["_superseded"] = True
                        ended.append(other)
            elif any((o["kind"] in SIDE_OPS) == (kind in SIDE_OPS) for o in running):
                raise ActRefuse("operation_running")
            op = {
                "kind": kind,
                "terminal": key,
                "state": "running",
                "started_at": round(self.now(), 3),
                "finished_at": None,
                "code": None,
                # (bookkeeping, not shown)
                "_superseded": False,
                "_preempted": ended,
                "_unit": None,
            }
            self._ops = [*running, op]
            self._op = op
            return op

    def _finish(self, op, ok, code=None) -> None:
        """End an operation, once: a worker that fails after it has finished
        (a marker release that hits an error after a good stop) does not turn
        the result around."""
        with self._op_lock:
            if op["state"] != "running":
                return
            op.update(
                state="done" if ok else "failed",
                finished_at=round(self.now(), 3),
                code=code,
            )
        self._poke()

    def _spawn(self, op, work) -> None:
        """Queue ``work()`` for ``op``; it runs after the request has let go of
        its locks, in a thread (inline in tests). It ends the operation itself
        with the code it wants; an exception ends it failed."""
        queue = getattr(self._tls, "pending", None)
        if queue is None:
            queue = self._tls.pending = []
        queue.append((op, work))

    def _defer(self, call) -> None:
        """Run ``call()`` once the request has let go of its locks."""
        self._spawn(None, call)

    def _flush(self) -> None:
        queue, self._tls.pending = getattr(self._tls, "pending", []), []
        for op, work in queue:
            if op is None:
                self._run_deferred(work)
            elif self.host.background:
                self._start_thread(op, work)
            else:
                self._run_work(op, work)

    @staticmethod
    def _run_deferred(call) -> None:
        """A deferred call is a convenience (the sequence's ticker): when it
        fails the request that asked for it has still been done."""
        try:
            call()
        except Exception:  # (logged, never the request's failure)
            LOG.exception("a deferred call of the arm stack actions failed")

    def _start_thread(self, op, work) -> None:
        try:
            threading.Thread(
                target=self._run_work,
                args=(op, work),
                daemon=True,
                name="levi-marvel0-act",
            ).start()
        except RuntimeError:  # no thread could be started: end the operation
            LOG.exception("no thread for an operation of the arm stack actions")
            self._finish(op, False, "no_thread")

    def _run_work(self, op, work) -> None:
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - the operation must end
            self._finish(op, False, type(exc).__name__)
        else:
            if op["state"] == "running":
                if op["_superseded"]:
                    self._finish(op, False, "superseded")
                else:
                    self._finish(op, True)

    # -- start --------------------------------------------------------------------
    def start(self, key, body) -> dict:
        if key == "m":
            return self.stack.start_monitor(body)
        self.require_enabled()
        return self._write(
            body, ACTIONS[("start", key)], lambda r: self._do_start(key, body, r)
        )

    def _do_start(self, key, body, r, attested=None) -> dict:
        ctx = self._context(r, holding=True)
        blockers = [
            b for b in self.start_blockers(key, ctx) if b["level"] == "required"
        ]
        if blockers:
            raise ActRefuse(blockers[0]["code"], **blockers[0]["params"])
        attested = attested or self._attested(key, body)
        if key == "b" and self._settled_by_a_press(key, ctx):
            # Nothing is started on a B that cannot be seen: the check is made
            # (in a worker, after the locks are let go) and the person presses
            # again. Never a start that waits for the check behind the click.
            op = self._begin("probe_b", "b")
            self._spawn(op, lambda: self._probe_b_worker(op, ctx))
            raise ActRefuse("b_probe_started", keep_work=True)
        return self._launch(key, ctx, attested, body.get("request_id"))

    def _manager_unset(self) -> list:
        code, text = self.host.base.run(
            ["systemctl", "--user", "show-environment"], 5.0
        )
        if code != 0:
            raise ActRefuse("systemd_unreadable")
        return stack_units.unset_names(text)

    def _new_log(self, folder, key, now) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        for n in range(1, 100):
            suffix = "" if n == 1 else f"-{n}"
            path = folder / "logs" / f"{key}-{stamp}{suffix}.log"
            try:
                fd = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
            except FileExistsError:
                continue
            os.close(fd)
            record._newest(folder / "logs", LOG_NAME, KEEP_LOGS, {path.name})
            return path
        raise ActRefuse("record_unusable")

    def _launch(self, key, ctx: Ctx, attested, request_id) -> dict:
        specs, spec = ctx.specs, ctx.specs.units[key]
        if specs.profile == "real" and (
            stack_units.wrapper_sha256(specs.wrapper) != stack_units.WRAPPER_SHA256
        ):
            raise ActRefuse("wrapper_changed")
        unset = self._manager_unset()
        folder = self._folder(key)
        now = self.now()
        log = self._new_log(folder, key, now)
        argv = stack_units.systemd_run_argv(spec, log, unset)
        # Written before the unit exists: a core that dies in between leaves an
        # intent, which the page reads as "started, then vanished" (dead), and
        # which blocks another start until it is stopped.
        fields = {
            "schema": RECORD_SCHEMA,
            "key": key,
            "unit": spec.unit,
            "phase": "intent",
            "request_id": request_id,
            "attest": attested,
            "started_at": round(now, 3),
            "stopped_at": None,
            "stop_requested_at": None,
            "log": str(log),
            "exec_argv": list(spec.argv),
            "port": specs.server_port,
            "profile": specs.profile,
            "wrapper_sha256": stack_units.WRAPPER_SHA256
            if specs.profile == "real"
            else None,
        }
        with record.locked(folder):
            record.write_record(folder, fields)
        for latched in [k for k in self._ready_at if k[0] == key]:
            del self._ready_at[latched]  # (the new run starts unlatched)
        record.audit(
            folder,
            {"action": f"start_{key}", "phase": "intent", "request_id": request_id},
            now,
        )
        code, text = self.host.base.run(argv, stack_units.START_TIMEOUT_S)
        if code != 0:
            # 124: systemd-run did not answer in time. The unit may be there
            # all the same (and C's port open): the record stays an intent, which
            # blocks another start until the page has seen what became of it.
            unconfirmed = code == 124
            phase = "intent" if unconfirmed else "start_failed"
            self._write_record(key, phase=phase)
            record.audit(
                folder,
                {
                    "action": f"start_{key}",
                    "phase": phase,
                    "code": f"exit {code}",
                    "detail": text,
                },
                now,
            )
            if unconfirmed:
                raise ActRefuse("start_unconfirmed")
            raise ActRefuse("start_failed", detail=record.scrub(text)[:300])
        self._write_record(key, phase="started")
        record.audit(
            folder,
            {"action": f"start_{key}", "phase": "started", "request_id": request_id},
            now,
        )
        return {"done": "started", "terminal": key, "unit": spec.service}

    # -- stop ---------------------------------------------------------------------
    def stop(self, key, body) -> dict:
        if key == "m":
            return self._write(body, "monitor_stop", self._do_stop_m)
        return self._write(
            body, ACTIONS[("stop", key)], lambda r: self._do_stop(key, body, r)
        )

    def _do_stop_m(self, r) -> dict:
        """M comes last: refused while a unit of A, B or C exists or any of
        them may run (``Stack._stop`` adds its own checks)."""
        ctx = self._context(r, holding=True)
        busy = [
            k
            for k in stack_units.KEYS
            if ctx.states[k]["state"] not in ("idle", "unknown")
        ]
        if busy or any(r["units"][k.upper()] is not False for k in stack_units.KEYS):
            raise ActRefuse("b_running" if busy == ["b"] else "stack_running")
        return self.stack._stop({**r, "terms": ctx.terms})  # (A's draining read)

    def _do_stop(self, key, body, r) -> dict:
        ctx = self._context(r, holding=True)
        typed = body.get("client_override") or ""
        if key == "c" and typed not in ("", CLIENT_OVERRIDE):
            raise ActRefuse("confirm_required", phrase=CLIENT_OVERRIDE)
        override = key == "c" and typed == CLIENT_OVERRIDE
        blockers = [
            b
            for b in self.stop_blockers(key, ctx, override)
            if b["level"] == "required"
        ]
        if blockers:
            raise ActRefuse(blockers[0]["code"], **blockers[0]["params"])
        st = ctx.states[key]
        rec = ctx.records[key] or {}
        unit = (
            rec.get("unit") or stack.STACK_UNITS[key.upper()].removesuffix(".service")
        ) + ".service"
        now = self.now()
        if not st["owned"] and st["state"] in ("idle", "unknown"):
            return {"done": "already_stopped", "terminal": key}
        if st["state"] == "dead" or st["unit_state"] == "failed":
            return self._clear_failed(key, unit, now)
        self._write_record(key, phase="stopping", stop_requested_at=round(now, 3))
        op = self._begin(f"stop_{key}", key)
        self._tls.stop_op = op  # (the guided stop follows this one)
        self._spawn(op, lambda: self._stop_worker(key, unit, op, ctx))
        # (what the page says about the stop it says once it is done: the
        # document's ``stop_note``)
        return {"done": "stopping", "terminal": key, "unit": unit, "note": None}

    def _processes_left(self, unit):
        """How many processes the unit's control group still holds (None: it
        cannot be told). ``SendSIGKILL=no`` keeps the page from ever killing,
        so a unit whose stop timed out is ``failed`` with its processes still
        running, and still connected to the arm. TasksCurrent first, then the
        group's own process list, then the main pid."""
        run = self.host.base.run
        code, text = run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=TasksCurrent,MainPID,ControlGroup",
            ],
            stack.SHOW_TIMEOUT_S,
        )
        if code != 0:
            return None
        shown = stack.parse_show(text)

        def count(value):
            # ("[not set]" or uint64 max: accounting is off)
            try:
                number = int(value)
            except (TypeError, ValueError):
                return None
            return number if 0 <= number < 2**63 else None

        tasks = count(shown.get("TasksCurrent"))
        if tasks is not None:
            return tasks
        group = shown.get("ControlGroup")
        if "ControlGroup" in shown and not group:
            # systemd keeps the control group of a unit that still has
            # processes and lets go of an empty one: no group, nothing left
            # (and so a clean failed unit, whose task count reads "[not set]",
            # can be acknowledged).
            return 0
        pids = self.host.cgroup_procs(group) if group else None
        if pids is not None:
            return len(pids)
        # The group exists and cannot be read. A failed unit shows MainPID 0
        # even when processes are left in it: only a positive pid is a count
        # (the pid itself), anything else cannot be told.
        pid = count(shown.get("MainPID"))
        return pid if pid else None

    def _clear_failed(self, key, unit, now) -> dict:
        """A unit that already failed or vanished: reset it and close the
        record. This is "acknowledge", not a stop, and it is refused while the
        unit still has processes: reset-failed would let the unit go and leave
        them running, connected to the arm, under no name the page knows. The
        press sends them SIGINT once more (a second Ctrl+C) and says so; the
        record stays open until they are gone."""
        run = self.host.base.run
        shown = stack.show_unit(run, unit)
        if shown and shown.get("LoadState") != "not-found":
            left = self._processes_left(unit)
            if left is None or left > 0:
                run(
                    ["systemctl", "--user", "kill", "--signal=SIGINT", unit],
                    stack.SHOW_TIMEOUT_S,
                )
                self._write_record(key, phase="stop_failed")
                raise ActRefuse(
                    "stop_left_processes", unit=unit.removesuffix(".service")
                )
            code, _ = run(
                ["systemctl", "--user", "reset-failed", unit], stack.SHOW_TIMEOUT_S
            )
            if code != 0:
                raise ActRefuse("stop_failed")
        self._write_record(key, phase="acknowledged", stopped_at=round(now, 3))
        return {"done": "acknowledged", "terminal": key}

    def _stop_worker(self, key, unit, op, ctx: Ctx) -> None:
        run = self.host.base.run
        # What this stop superseded is ended first: a gripper test is stopped
        # with its own unit (SIGINT cancels the goal), and B is not stopped
        # until that unit is gone.
        for other in op["_preempted"]:
            if other["kind"] == "gripper":
                self._end_gripper_test(other)
        timeout = (
            ctx.specs.units[key].stop_timeout_s
            if ctx.specs
            else stack_units.STOP_TIMEOUT_S
        ) + 15
        code, _ = run(["systemctl", "--user", "stop", unit], timeout)
        now = self.now()
        if code != 0:
            self._finish(op, False, "stop_failed")
            return
        self._write_record(key, phase="stopped", stopped_at=round(now, 3))
        if key == "b":
            # Nothing of ours is left and nothing else was seen: B is known.
            self._b_probe = {
                "at": now,
                "state": "absent",
                "by": "stop",
                "confirmed": True,
            }
        self._finish(op, True, f"{key}.stopped")
        if key == "a":
            self._release_safely()

    def _end_gripper_test(self, test) -> bool:
        """Stop the gripper test's one-shot unit and wait until it is gone, so
        that its cancel is answered by a B that still runs: ``send_goal``'s
        SIGINT handler spins until B's action server answers the cancel
        (ros2action/verb/send_goal.py:146-170), and B's own stop (SIGINT to a
        control group that holds the action server) would otherwise race it,
        leaving a Robotiq that finishes its last command by itself.

        The unit's name is known before ``systemd-run`` has created it
        (``gripper.execute``'s hook), so a stop that lands in between finds no
        unit: it waits for the unit to appear or for the test to end, and
        stops the unit when it does. True when the test is over; False when
        ``GRIPPER_END_WAIT_S`` passed (B is then stopped all the same: the way
        out never depends on a unit that will not go)."""
        run = self.host.base.run
        asked = False
        for _ in range(max(1, int(GRIPPER_END_WAIT_S / GRIPPER_END_POLL_S))):
            if test["state"] != "running":
                return True  # it ended by itself (or was never started)
            unit = test["_unit"]
            shown = stack.show_unit(run, f"{unit}.service") if unit else None
            loaded = shown is not None and shown.get("LoadState") != "not-found"
            if loaded and not asked:
                run(
                    ["systemctl", "--user", "stop", f"{unit}.service"],
                    GRIPPER_END_WAIT_S,
                )
                asked = True
                continue  # (is it gone? look at once)
            if asked and shown is not None and not loaded:
                return True
            self.host.sleep(GRIPPER_END_POLL_S)
        LOG.warning("the gripper test's unit was still there when B was stopped")
        return False

    def _release_safely(self) -> None:
        """The release is a convenience after a stop that has succeeded: an
        error in it is logged, and the stop stays what it was."""
        try:
            self._release_after_stop()
        except Exception:
            LOG.exception("the marker release after a stop of A failed")

    def resume(self) -> None:
        """Finish what an earlier run of the page left half done: the marker of
        an A this page stopped is released once the arm is quiet (the 90 s loop
        of ``_release_after_stop`` lives in memory, and a restart of the
        product ends it). Once per process; a marker that is not ours is left
        alone, as always."""
        if self._resumed:
            return
        self._resumed = True
        left = self._record("a")
        if left and left.get("phase") == "stopping" and self._a_unit_gone(left):
            # The page died between the press and the end of its stop. The unit
            # is gone, so the stop happened: say so (the marker release and the
            # note after a stop read the phase). A unit that is still there, or
            # that cannot be told, leaves the record as it is (the document
            # names it, and the person decides).
            try:
                left = self._write_record(
                    "a", phase="stopped", stopped_at=round(self.now(), 3)
                )
            except (stack.Refuse, record.RecordCorrupt, OSError):
                LOG.warning("the record of a cut-short stop of A could not be closed")
                return
        if not left or left.get("phase") != "stopped":
            return
        if not self.host.background:
            self._release_safely()
            return
        try:
            threading.Thread(
                target=self._release_safely, daemon=True, name="levi-marvel0-resume"
            ).start()
        except RuntimeError:
            LOG.exception("no thread to finish the marker release")

    def _a_unit_gone(self, rec) -> bool:
        name = (rec.get("unit") or stack.STACK_UNITS["A"].removesuffix(".service")) + (
            ".service"
        )
        shown = stack.show_unit(self.host.base.run, name)
        return shown is not None and shown.get("LoadState") == "not-found"

    def _release_after_stop(self) -> None:
        """The marker is the page's to remove once A's unit is gone and the arm
        shows no control connection (its sockets linger for about a minute)."""
        for _ in range(RELEASE_POLLS):
            try:
                if self._try_release():
                    return
            except stack.Refuse:
                pass
            self.host.sleep(RELEASE_POLL_S)

    def _try_release(self) -> bool:
        def release(r):
            if r["marker"]["state"] != "present":
                return True
            if not self._marker_is_ours():
                return True  # not ours: left alone
            self.stack._clear(r)
            return True

        r = self.stack._read()
        if r["marker"]["state"] == "present" and stack.marker_blockers(
            r["profile"], r["marker"], r["units"], r["sockets"], r["terms"]
        ):
            return False
        return bool(self._locked("marker_release", release))

    # -- the marker ---------------------------------------------------------------
    def marker_op(self, op, body) -> dict:
        if op == "release":
            return self._write(body, "marker_clear", self._do_release)
        action = {"touch": "marker_touch", "adopt": "marker_adopt"}[op]
        self.require_enabled()
        return self._write(
            body, action, self._do_touch if op == "touch" else self._do_adopt
        )

    def _do_touch(self, r) -> dict:
        if self._stopping():
            raise ActRefuse("sequence_stopping")
        view = r["marker"]
        if view["state"] == "present":
            raise ActRefuse("marker_exists")
        if view["state"] != "absent":
            raise ActRefuse("marker_unusable")
        path = self.host.base.marker()
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o644,
            )
        except FileExistsError as exc:
            raise ActRefuse("marker_exists") from exc
        except OSError as exc:
            raise ActRefuse("marker_unusable") from exc
        os.close(fd)
        self._remember_marker("created")
        return {"done": "marker_set"}

    def _do_adopt(self, r) -> dict:
        if r["marker"]["state"] != "present":
            raise ActRefuse("marker_absent")
        self._remember_marker("adopted")
        return {"done": "marker_adopted"}

    def _do_release(self, r) -> dict:
        if r["marker"]["state"] != "present":
            raise ActRefuse("marker_absent")
        if not self._marker_is_ours():
            raise ActRefuse("marker_not_ours")
        return self.stack._clear(r)

    # -- the checks before A and before C -----------------------------------------
    def preflight_a(self, body) -> dict:
        self.require_enabled()
        return self._write(body, "preflight_a", self._do_preflight_a, phrase=False)

    def _do_preflight_a(self, r, with_b=True) -> dict:
        """The one TCP connection to the arm's FCI port, made only when A is
        positively known to be absent: no unit of ours, A idle, and the arm's
        sockets known and quiet (or lingering after a stop or a probe of ours,
        with a fresh health file saying the hardware is not active). What cannot
        be told is not "absent".

        ``with_b``: also ask whether a gripper stack runs, at the same time (the
        button does; the guided start makes that check as an operation of its
        own, begun earlier, so that nothing waits for the other)."""
        ctx = self._context(r, holding=True)
        a = ctx.states["a"]
        draining = a["reason"].endswith("sockets_draining")
        sockets = r["sockets"]["state"]
        quiet = sockets == "inactive" or (sockets == "active" and draining)
        if a["state"] != "idle" or r["units"]["A"] is not False or not quiet:
            raise ActRefuse("probe_arm_present")
        if not self._is_real(r):
            raise ActRefuse("not_product")
        op = self._begin("preflight_a")
        self._spawn(op, lambda: self._preflight_a_worker(op, ctx, with_b))
        return {"done": "preflight_started", "kind": "preflight_a"}

    def _preflight_a_worker(self, op, ctx: Ctx, with_b=True) -> None:
        """The FCI port (one TCP connection, never while A exists) and, when
        asked and no unit of ours is B, whether a gripper stack runs anyway, the
        two at the same time. A stop that came meanwhile supersedes it: nothing
        of it is kept."""
        specs, now = ctx.specs, self.now()

        def fci():
            probe = self.host.fci_probe(specs.robot_ip, stack_units.FCI_PORT)
            if not op["_superseded"]:
                # (kept at once: the sequence waits for this, not for the other)
                self._fci = {"at": now, "state": probe.get("state", "unreachable")}

        calls = [fci]
        if with_b and ctx.r["shown"]["B"] is not None and ctx.r["units"]["B"] is False:
            calls.append(lambda: self._probe_b(specs, op))
        elif with_b and ctx.r["units"]["B"] is True:
            self._b_probe = None
        self._parallel(*calls)
        if op["_superseded"]:
            return
        if self._fci["state"] != "open":
            self._finish(op, False, f"fci_{self._fci['state']}")

    def probe_b(self, body) -> dict:
        """The check whether a gripper stack runs, **alone**: it asks B's
        controller manager (a read) and touches no FCI port, so it is allowed
        with A up as well as with A idle. It is the only way to refresh the
        check once A runs (A's own check, which also makes the one TCP
        connection to the arm, is refused then)."""
        self.require_enabled()
        return self._write(body, "probe_b", self._do_probe_b, phrase=False)

    def _do_probe_b(self, r, confirm=True) -> dict:
        """Begin the check of B. ``confirm`` False is for the one case that
        cannot afford a second answer, the check that A's start waits for."""
        ctx = self._context(r, holding=True)
        refusal = self._probe_b_refusal(r, ctx)
        if refusal:
            raise ActRefuse(refusal)
        op = self._begin("probe_b", "b")
        self._spawn(op, lambda: self._probe_b_worker(op, ctx, confirm))
        return {"done": "probe_started", "kind": "probe_b"}

    def _probe_b_refusal(self, r, ctx: Ctx):
        """The code that keeps the check of B alone from being made now (None:
        it can be). A unit of B, ours or not, needs no check: B is known."""
        if not self._is_real(r):
            return "not_product"
        if r["units"]["B"] is None:
            return "systemd_unreadable"
        if r["units"]["B"] is True or ctx.states["b"]["owned"]:
            return "gate.b_unit_absent"
        return None

    def _probe_b_worker(self, op, ctx: Ctx, confirm=True) -> None:
        probe = self._probe_b(ctx.specs, op, confirm)
        if probe is not None and probe["state"] == "unknown":
            self._finish(op, False, "b_probe_no_answer")

    def _b_probe_due(self, ctx: Ctx) -> bool:
        """Is the check whether a gripper stack runs missing, too old, or old
        enough to be made again while a guided start waits for B? ("absent"
        after ``B_PROBE_REFRESH_S``; a gripper stack seen, or no answer, after
        ``B_PROBE_RETRY_S``: a person who stopped a stack by hand should not
        wait out the whole validity.)"""
        if ctx.states["b"]["state"] not in ("idle", "unknown", "foreign_running"):
            return False
        if ctx.states["b"]["owned"] or ctx.r["units"]["B"] is not False:
            return False
        probe, now = ctx.b_probe, ctx.r["now"]
        if not _fresh(probe, B_PROBE_TTL_S, now):
            return True
        if probe.get("state") == "absent" and not probe.get("confirmed"):
            return True  # (one answer of the graph: B needs a second)
        wait = B_PROBE_REFRESH_S if probe.get("state") == "absent" else B_PROBE_RETRY_S
        return now - probe["at"] > wait

    def _graph_check(self, specs, op):
        """One ``ros2 node list`` (about 3.4 s), parsed; None when a stop came
        meanwhile and dropped it."""
        code, text = self._oneshot(
            specs.nodes_cmd,
            "probe-b-nodes",
            "FR3 gripper stack probe (ROS graph)",
            stack_units.NODES_RUNTIME_MAX_S,
        )
        if op["_superseded"]:
            return None
        return stack_units.parse_nodes(text, code)

    def _probe_b(self, specs, op, confirm=True) -> dict | None:
        """Does a gripper stack run that this page did not start?

        **First the ROS graph** (``ros2 node list``, about 3 s): a node of the
        gripper stack's namespace means present; none, with the health
        monitor's own node listed (so that discovery had run), means absent.
        **Then, only when that proves nothing** (the monitor's node is not seen,
        the command failed), the controller manager is asked: the command never
        ends by itself when there is none (it waits for the service and warns
        every 10 s, about 20 s in all), so it is ended by ``timeout`` and the
        verdict is read from what it printed, not from how it ended: the warning
        means absent, a listing means present, and a command ended before it
        could say is *no answer* (unknown), never absent.

        **"Absent" from the graph alone is one answer** (``confirmed`` False in
        the record): a graph that listed the monitor but has not discovered the
        gripper stack's participants yet says the same, and a second gripper
        stack on the same serial port is a motion risk. It is confirmed by a
        second answer: the answer before it, still fresh, was absent too, or (with
        ``confirm``) a second graph check is made now. ``confirm`` is False only
        for the check that A's start waits for (which must not wait for two): the
        guided start makes the second one while A starts up, and B starts only on a
        confirmed absence (``_b_absence_unconfirmed``). A second answer that is
        *present* is final; one that proves nothing goes on to the controller
        manager. The manager's own "absent" (its warning that the service did not
        appear for 10 s) and B's own stop need no second answer."""
        prior, began = self._b_probe, self.now()
        graph, state, by, confirmed = None, None, "nodes", False
        if specs.nodes_cmd:
            fast = self._graph_check(specs, op)
            if fast is None:
                return None
            graph = fast["state"]
            if graph in ("present", "absent"):
                state = graph
            if state == "absent":
                confirmed = (
                    bool(prior)
                    and prior.get("state") == "absent"
                    and _fresh(prior, B_PROBE_TTL_S, began)
                )
                if confirm and not confirmed:
                    again = self._graph_check(specs, op)
                    if again is None:
                        return None
                    graph = again["state"]
                    if graph == "absent":
                        confirmed = True
                    else:  # (present is final; nothing proved goes to the manager)
                        state = graph if graph == "present" else None
        if state is None:
            by = "probe"
            _, text = self._oneshot(
                specs.controllers_cmd,
                "probe-b",
                "FR3 gripper stack probe",
                stack_units.PROBE_RUNTIME_MAX_S,
            )
            if op["_superseded"]:
                return None
            parsed = stack_units.parse_controllers(text)
            state = {"listed": "present", "absent": "absent"}.get(
                parsed["state"], "unknown"
            )
        if by == "probe" and state == "absent":
            confirmed = True  # (the manager's own word: no second answer to add)
        self._b_probe = {
            "at": self.now(),
            "state": state,
            "by": by,
            # (why the slow question was needed, for whoever looks)
            "graph": graph,
            "confirmed": confirmed,
        }
        return self._b_probe

    def _oneshot(self, argv, name, description, runtime_max_s=None) -> tuple:
        """Run a short command in a transient unit and read its output back
        (``systemd-run --pipe --wait``). ``runtime_max_s`` is systemd's backstop
        behind the command's own bound."""
        limit = int(runtime_max_s or CHECK_TIMEOUT_S + 10)
        # The name is unique per call, not per second: two checks of one kind in
        # the same second (the early check of B and a person's click on the FCI
        # check) would otherwise meet in ``systemd-run`` ("already loaded"), one
        # of them would read as no answer, and the slow path would be taken.
        unit = (
            f"{stack_units.ONESHOT_PREFIX}{name}-{int(self.now())}-"
            f"{uuid.uuid4().hex[:6]}"
        )
        full = stack_units.oneshot_argv(unit, argv, limit, description)
        return self.host.base.run(full, limit + 10)

    def preflight_c(self, body) -> dict:
        self.require_enabled()
        return self._write(body, "preflight_c", self._do_preflight_c, phrase=False)

    def _do_preflight_c(self, r) -> dict:
        ctx = self._context(r, holding=True)
        for key, code in (("a", "gate.a_running"), ("b", "gate.b_running")):
            if ctx.states[key]["state"] != "ready":
                raise ActRefuse(code)
        return self._start_preflight_c(ctx)

    def _start_preflight_c(self, ctx: Ctx) -> dict:
        """Begin the checks with the context the caller just validated."""
        op = self._begin("preflight_c")
        self._spawn(op, lambda: self._preflight_c_worker(op, ctx))
        return {"done": "preflight_started", "kind": "preflight_c"}

    def _preflight_c_worker(self, op, ctx: Ctx) -> None:
        specs, now = ctx.specs, self.now()
        qos = None
        if specs.qos_file:
            try:
                data, _, _ = probes.read_regular(specs.qos_file, 1 << 20)
                qos = stack_units.qos_ok(data.decode("utf-8", "replace"))
            except OSError:
                qos = None
        # (the listing and the frame check do not depend on each other: both at
        # once, so the checks take as long as the slower one)
        (_, listed_text), (ee_code, ee_text) = self._parallel(
            lambda: self._oneshot(
                specs.controllers_cmd,
                "check-controllers",
                "FR3 gripper controllers check",
            ),
            lambda: self._oneshot(
                specs.ee_cmd, "check-ee", "FR3 end-effector frame check"
            ),
        )
        listing = stack_units.parse_controllers(listed_text)
        controllers = stack_units.gripper_controllers_ok(listing)
        ee = ee_code == 0 and stack_units.ee_frame_ok(ee_text)
        if op["_superseded"]:
            return  # a stop came meanwhile: this answer is for a stack that is going
        ok = bool(qos) and controllers and ee
        self._c_pre = {
            "at": now,
            "ok": ok,
            "qos": qos,
            "controllers": controllers,
            # listed | absent | unknown: only "listed" is an answer (the guided
            # start asks once more when the manager did not answer)
            "controllers_state": listing["state"],
            "ee_frame": ee,
        }
        if not ok:
            self._finish(op, False, "c_preflight_failed")

    # -- the gripper test ---------------------------------------------------------
    def gripper(self, body) -> dict:
        action = body.get("action")
        try:
            gripper.target_position(
                action, body.get("opening_ratio", gripper.MISSING_RATIO)
            )
        except ValueError:
            raise ActRefuse("attest_invalid", keys="action/opening_ratio") from None
        self.require_enabled()
        return self._write(
            body, f"gripper_{action}", lambda r: self._do_gripper(action, body, r)
        )

    def _do_gripper(self, action, body, r) -> dict:
        ctx = self._context(r, holding=True)
        blockers = [b for b in self.gripper_blockers(ctx) if b["level"] == "required"]
        if blockers:
            raise ActRefuse(blockers[0]["code"], **blockers[0]["params"])
        self._attested("gripper", body)
        op = self._begin("gripper", "b")
        self._spawn(
            op,
            lambda: self._gripper_worker(
                op, ctx, action, body.get("opening_ratio", gripper.MISSING_RATIO)
            ),
        )
        target = (
            {"opening_ratio": body["opening_ratio"]} if action == "position" else {}
        )
        return {"done": "gripper_test_started", "action": action, **target}

    def _gripper_worker(
        self, op, ctx: Ctx, action, opening_ratio=MISSING_RATIO
    ) -> None:
        def told(unit):
            op["_unit"] = unit  # (a stop of B ends the test through its unit)

        result = gripper.execute(
            self.host.base.run,
            ctx.specs,
            action,
            self.now(),
            unit_hook=told,
            cancelled=lambda: op["_superseded"],
            opening_ratio=opening_ratio,
        )
        if op["_superseded"]:
            # Stopped by the stop of B: SIGINT cancelled the goal, or nothing
            # had started. Either way the test did not run to its end.
            result = {**result, "ok": False, "code": "cancelled"}
        self._gripper["last"] = result
        self._finish(op, result["ok"], result["code"])

    # -- the guided sequence ------------------------------------------------------
    def sequence_start(self, body) -> dict:
        self.require_enabled()
        return self._write(body, "sequence_start", lambda r: self._seq_begin(body, r))

    @staticmethod
    def _seq_options(body) -> tuple:
        """``(through, pause_before_b, pause_before_c)`` of a guided start. The
        defaults are the guided start of old: through A, then wait for the
        person's own start of B and of C."""
        through = body.get("through", "a")
        if through not in SEQUENCE_THROUGH:
            raise ActRefuse("sequence_option_invalid", name="through")
        pauses = []
        for name in ("pause_before_b", "pause_before_c"):
            value = body.get(name, False)
            if not isinstance(value, bool):
                raise ActRefuse("sequence_option_invalid", name=name)
            pauses.append(value)
        return through, pauses[0], pauses[1]

    @staticmethod
    def _attested_stages(stages, body) -> dict:
        """The confirmations of every stage the sequence will start itself, all
        ticked; one tick serves every stage that asks for it. A missing one
        refuses the whole request, naming the keys. ``{stage: {key: True}}``:
        each stage's record keeps what that stage needs."""
        given = body.get("attest") or {}
        unknown = sorted(k for k in given if k not in ATTEST_KEYS)
        if unknown:
            raise ActRefuse("attest_invalid", keys=", ".join(unknown))
        missing = []
        for stage in stages:
            for k in ATTEST_NEEDED[stage]:
                if given.get(k) is not True and k not in missing:
                    missing.append(k)
        if missing:
            raise ActRefuse("attest_missing", keys=", ".join(missing))
        return {stage: {k: True for k in ATTEST_NEEDED[stage]} for stage in stages}

    def _seq_begin(self, body, r) -> dict:
        ctx = self._context(r, holding=True)
        through, pause_b, pause_c = self._seq_options(body)
        chain = through == "c"
        st = ctx.states
        with self._seq_lock:
            if self._seq["state"] in ("running", "waiting_confirm"):
                raise ActRefuse("sequence_active")
            # A press again, after a failure further on, skips what is up: the
            # chain does not start A (or B, or C) that is ready or starting.
            # The guided start through A keeps refusing an A that is not idle.
            a_up = chain and st["a"]["state"] in ("ready", "starting")
            stages = [] if a_up else ["a"]
            for key, paused in (("b", pause_b), ("c", pause_c)):
                if (
                    chain
                    and not paused
                    and st[key]["state"] not in ("ready", "starting")
                ):
                    stages.append(key)
            attests = self._attested_stages(stages, body)
            if chain and all(st[k]["state"] == "ready" for k in "abc"):
                return {"done": "already_started"}
            if not a_up:
                hard = [
                    b
                    for b in self.start_blockers("a", ctx)
                    if b["level"] == "required" and b["code"] not in SEQUENCE_SOFT
                ]
                if hard:
                    raise ActRefuse(hard[0]["code"], **hard[0]["params"])
            now = self.now()
            plan = (*A_STEPS, *CHAIN_STEPS) if chain else A_STEPS
            self._seq = {
                "state": "running",
                "kind": "start",
                "step": "monitor",
                "next": None,
                "reason": None,
                "since": round(now, 3),
                "step_at": round(now, 3),
                "begun_at": round(now, 3),
                "reason_params": None,
                "took_s": None,
                "request_id": body.get("request_id"),
                "attest": attests.get("a", {}),
                "through": through,
                # (a guided start through A waits for the person's B and C)
                "pauses": {"b": pause_b or not chain, "c": pause_c or not chain},
                "steps": self._new_steps(plan, lambda name: STEP_TERMINAL[name]),
                "_attests": attests,
                "_a_up": a_up,
            }
        self._defer(self._ensure_ticker)
        return {"done": "sequence_started"}

    def sequence_cancel(self, body) -> dict:
        # Cancel ends the sequence before the machine lock is asked for: a chain
        # step holding the lock (systemd-run, up to START_TIMEOUT_S) must not turn
        # the way out into stack_busy. The cancel itself only stops advancing.
        ended = False
        with self._seq_lock:
            if self._seq["state"] in ("running", "waiting_confirm"):
                self._seq_end("cancelled", "cancelled")
                ended = True
        return self._write(
            body,
            "sequence_cancel",
            lambda r: self._do_cancel(r, ended),
            phrase=False,
        )

    def _do_cancel(self, r, ended=False) -> dict:
        if ended:
            return {"done": "sequence_cancelled"}
        with self._seq_lock:
            if self._seq["state"] not in ("running", "waiting_confirm"):
                raise ActRefuse("sequence_idle")
            self._seq_end("cancelled", "cancelled")
        return {"done": "sequence_cancelled"}

    @staticmethod
    def _seq_public(seq) -> dict:
        """What the page may see of the sequence (not the confirmations the
        person ticked, nor the request id, nor when the confirmations were
        given, nor the bookkeeping: keys that start with ``_``). The steps and
        the pauses are copied: the sequence goes on changing the steps."""
        hidden = ("attest", "request_id", "begun_at")
        return {
            k: copy.deepcopy(v) if k in ("steps", "pauses") else v
            for k, v in seq.items()
            if k not in hidden and not k.startswith("_")
        }

    def _new_steps(self, names, terminal) -> list:
        """The steps of a sequence at its start (the first one running). ``at``
        is when a step last began, ``took_s`` how long it has taken in all (None
        until it has run): the page shows where the time goes."""
        now = round(self.now(), 3)
        return [
            {
                "step": name,
                "terminal": terminal(name),
                "state": "running" if n == 0 else "pending",
                "at": now if n == 0 else None,
                "took_s": None,
            }
            for n, name in enumerate(names)
        ]

    def _item_state(self, item, state) -> None:
        """Set the state of a step and keep its clock. A step that goes back to
        running (the check of B asked again) adds the time it takes to what it
        took before."""
        now = round(self.now(), 3)
        if item["state"] == "running" and state != "running":
            began = item.get("at")
            if began is not None:
                spent = (item.get("took_s") or 0.0) + max(0.0, now - began)
                item["took_s"] = round(spent, 2)
        if state == "running":
            item["at"] = now
        item["state"] = state

    def _mark(self, seq, name, state, only=None) -> None:
        """The state of the step ``name`` (a step the sequence has no entry for
        is ignored); ``only``: change it only from one of these states."""
        with self._seq_lock:
            for item in seq.get("steps", ()):
                if item["step"] == name and (only is None or item["state"] in only):
                    self._item_state(item, state)

    def _skip_pending(self, seq, names) -> None:
        """The steps the person did by hand while the sequence waited."""
        for name in names:
            self._mark(seq, name, "skipped", only=("pending",))

    def _set_step(self, seq, step) -> None:
        with self._seq_lock:
            seq["step"] = step
            seq["step_at"] = round(self.now(), 3)
            self._mark(seq, step, "running")
        self._poke()

    def _goto(self, seq, step, how="done") -> None:
        """Leave the running step (as ``how``: done, skipped when nothing was
        asked of the machine, or pending when it has to be done again) and
        enter ``step``."""
        with self._seq_lock:
            if seq["state"] != "running":
                return  # (cancelled, or ended, while the step was running)
            self._mark(seq, seq["step"], how, only=("running",))
            self._set_step(seq, step)

    def _seq_end(self, state, reason, seq=None, params=None) -> None:
        """End ``seq`` (the current sequence when none is named). A step names
        the sequence it works for: a sequence that a guided stop has taken the
        place of cannot end the new one. The step that was running ends with
        it (failed, or cancelled); the steps after it were not reached.
        ``params``: facts the reason is worded with (a count)."""
        with self._seq_lock:
            target = self._seq if seq is None else seq
            if target["state"] in ("running", "waiting_confirm"):
                target.update(
                    state=state,
                    reason=reason,
                    reason_params=params,
                    step=None,
                    next=None,
                )
                for item in target.get("steps", ()):
                    if item["state"] == "running":
                        self._item_state(
                            item, "failed" if state == "failed" else "cancelled"
                        )
                    elif item["state"] == "pending" and state != "done":
                        self._item_state(item, "not_reached")
                spent = [i["took_s"] for i in target.get("steps", ()) if i["took_s"]]
                target["took_s"] = round(sum(spent), 2)
        self._poke()

    def _ensure_ticker(self) -> None:
        if not self.host.background:
            return
        with self._ticker_lock:
            if self._ticker is not None and self._ticker.is_alive():
                return
            self._ticker = threading.Thread(
                target=self._tick_loop, daemon=True, name="levi-marvel0-sequence"
            )
            self._ticker.start()

    @staticmethod
    def _seq_live(seq) -> bool:
        return seq["state"] in ("running", "waiting_confirm")

    def _wait_tick(self, seconds) -> None:
        """Pause until ``seconds`` have passed or something changed
        (``_poke``)."""
        self._wake.wait(seconds)

    def _tick_s(self, seq) -> float:
        """The pause before the next look of a live sequence: long while it
        waits for the person, short only until A has a unit (nothing but
        ``systemctl`` is asked then, and the control loop is not running), and
        ``SEQUENCE_TICK_ARM_S`` once A has one, or cannot be told."""
        if seq["state"] != "running":
            return SEQUENCE_TICK_WAITING_S
        return SEQUENCE_TICK_S if self._a_unit is False else SEQUENCE_TICK_ARM_S

    def _tick_loop(self) -> None:
        burst = 0
        while True:
            self._wake.clear()
            before = self._progress
            seq = self.advance()
            if self._seq_live(seq):
                if self._progress != before and burst < SEQUENCE_BURST:
                    burst += 1  # (it moved: the next look may move it again)
                    continue
                burst = 0
                self._wait_tick(self._tick_s(seq))
                continue
            with self._ticker_lock:
                with self._seq_lock:
                    live = self._seq_live(self._seq)
                if not live:
                    self._ticker = None
                    return

    def advance(self) -> dict:
        """One step of the guided sequence (the thread calls this every
        ``SEQUENCE_TICK_S``; tests call it by hand). The guided start starts B
        and C only when the request said ``through = "c"`` and did not pause
        before them; the guided stop never starts anything.

        A step reads the machine and may start a command (up to 15 s for
        ``systemd-run``). The sequence's own lock is not held meanwhile: the
        page reads the sequence on every poll, and a cancel must not wait. Only
        one step runs at a time (``_adv_lock``)."""
        with self._adv_lock:
            with self._seq_lock:
                seq = self._seq
                over = seq["state"] not in ("running", "waiting_confirm")
                shown = self._seq_public(seq) if over else None
            if over:
                self._release_unused_marker(seq)  # (ended by a cancel, say)
                return shown
            try:
                ctx = self._context(self.stack._read())
                self._a_unit = ctx.r["units"]["A"]
                with self._seq_lock:
                    state = seq["state"]
                if state == "running" and seq.get("kind") == "stop":
                    self._step_stop(seq, ctx)
                elif state == "running":
                    self._step_running(seq, ctx)
                elif state == "waiting_confirm":
                    self._step_waiting(seq, ctx)
            except stack.Refuse as exc:
                if exc.code not in TRANSIENT:
                    self._seq_end("failed", exc.code, seq)
            except Exception as exc:  # noqa: BLE001 - a sequence must end, not hang
                self._seq_end("failed", type(exc).__name__, seq)
            self._release_unused_marker(seq)
            with self._seq_lock:
                return self._seq_public(seq)

    def _release_unused_marker(self, seq) -> None:
        """A guided start that ended before it started A (the FCI port closed, a
        heavy job that did not stop, a cancel) gives back the marker it set:
        nothing is going to start, and heavy jobs should not stay paused for a
        start that did not happen. Only a marker this very start made (a marker
        that was there before is the person's), only while A has no unit and
        the arm shows no control connection (``_try_release``: the rule of every
        release), and once."""
        with self._seq_lock:
            if (
                seq.get("kind") != "start"
                or not seq.get("_marker_made")
                or seq.get("_arm_started")
                or seq.get("_marker_given_back")
                or self._seq_live(seq)
            ):
                return
            seq["_marker_given_back"] = True
        for _ in range(3):
            try:
                self._try_release()
                return
            except stack.Refuse:  # (the lock is busy: a moment, then again)
                self.host.sleep(0.2)
            except Exception:
                LOG.exception("the marker of a start that did not happen stayed")
                return

    @staticmethod
    def _window_s(step, ctx: Ctx):
        """How long after the click the step may still go on (None: the step has
        no window of its own). The ticks of the click are good for the arm window
        and the marker's wait; each stage of the chain adds the time its
        predecessor and itself may take."""
        if step == "await_a":
            return None
        window = SEQUENCE_ARM_WINDOW_S + (ctx.marker.get("wait_s") or 0)
        if step in CHAIN_STEPS[:2]:
            return window + SEQUENCE_A_READY_S + SEQUENCE_B_READY_S
        if step in CHAIN_STEPS[2:]:
            return (
                window
                + SEQUENCE_A_READY_S
                + SEQUENCE_B_READY_S
                + SEQUENCE_C_CHECK_S
                + SEQUENCE_C_READY_S
            )
        return window

    def _step_running(self, seq, ctx: Ctx) -> None:
        a = ctx.states["a"]["state"]
        step = seq["step"]
        if step in ("arm", "await_a") and a in ("failed", "dead"):
            return self._seq_end("failed", f"a_{a}", seq)
        if step == "await_a" and self._a_was_stopped(ctx):
            # A stopped by a person while the sequence waits for it to become
            # ready: nothing is left to wait for (the step has no arm window and
            # would otherwise hold ``sequence_active`` for ``STEP_TIMEOUT_S``).
            return self._seq_end("failed", "a_lost", seq)
        now = self.now()
        if now - seq.get("step_at", seq["since"]) > STEP_TIMEOUT_S:
            return self._seq_end("failed", "step_timeout", seq)
        # The ticks were given at the click; nothing is started on a click that
        # old (A: the marker's own wait is on top of the window; B and C: the
        # time the stages before them may take).
        window = self._window_s(step, ctx)
        if window is not None and now - seq["begun_at"] > window:
            return self._seq_end("cancelled", "expired", seq)
        # (looks of the heavy jobs in a row without one that runs: the wait wants
        # two, as heavy_gate's wait-clear does; counted at every look, so they
        # are there when the wait is reached)
        heavy = ctx.marker.get("heavy")
        if heavy is not None and seq.get("kind") == "start":
            clean = heavy["state"] == "clear"
            seq["_clean_looks"] = seq.get("_clean_looks", 0) + 1 if clean else 0
        handler = getattr(self, f"_seq_{step}")
        handler(seq, ctx)

    def _seq_monitor(self, seq, ctx: Ctx) -> None:
        m = ctx.states["m"]["state"]
        if m == "ready":
            if seq.get("_a_up"):
                # A is up already (a press again): its checks, the marker, the
                # wait and its start are not made, only its readiness is looked at.
                with self._seq_lock:
                    if seq["state"] != "running":
                        return
                    for name in ("marker", "probe", "wait", "arm"):
                        self._mark(seq, name, "skipped", only=("pending",))
                    self._goto(seq, "await_a")
            else:
                self._goto(seq, "marker")
        elif m == "idle":
            self._seq_locked(seq, "sequence_monitor", self.stack._start)
        elif m != "starting":
            self._seq_end("failed", f"monitor_{m}", seq)

    def _start_b_probe(self, seq, ctx: Ctx) -> None:
        """Begin the check whether a gripper stack runs, as an operation of its
        own that runs beside the others, when A's start needs it (B is unknown for
        want of a usable answer) or, for a start that goes on to B, when the
        answer is old enough to be made again before B (``_b_probe_due``: it is
        then made during A's start-up and not between A and B). Optional here: a
        refusal (another check of B, a B that exists, a busy lock) is tried again
        where the answer is needed (``_seq_probe``)."""
        if seq.get("_early_op") is not None:
            return
        needed = ctx.states["b"]["state"] == "unknown" or (
            seq.get("through") == "c" and self._b_probe_due(ctx)
        )
        if not needed:
            return
        with contextlib.suppress(stack.Refuse):
            self._seq_locked(
                seq,
                "sequence_early_probe_b",
                lambda r: self._do_probe_b(r, confirm=False),
                keep="_early_op",
            )

    def _seq_probe(self, seq, ctx: Ctx) -> None:
        """The checks A's start needs: the arm's FCI port answers (one TCP
        connection, its own operation) and whether a gripper stack runs is
        known (begun with the marker, see ``_start_b_probe``). The marker has
        been ageing since it was set; nothing here waits for it."""
        now = ctx.r["now"]
        if not (_fresh(ctx.fci, FCI_TTL_S, now) and ctx.fci["state"] == "open"):
            op = seq.get("_fci_op")
            if op is not None:
                with self._op_lock:
                    state, code = op["state"], op["code"]
                if state == "running":
                    return
                if state == "failed":
                    return self._seq_end("failed", code or "preflight_failed", seq)
                # (done, and still not a fresh open answer: the clock moved)
            if ctx.busy_main:
                return
            self._seq_locked(
                seq,
                "sequence_probe",
                lambda r: self._do_preflight_a(r, with_b=False),
                keep="_fci_op",
            )
            return
        if ctx.states["b"]["state"] == "unknown":
            op = seq.get("_early_op")
            if op is None:
                # Not begun yet (it was refused, or this is a marker that was
                # there): begun now, and a refusal that will not go away ends the
                # start with its own code (a busy lock is tried again).
                return self._seq_locked(
                    seq,
                    "sequence_early_probe_b",
                    lambda r: self._do_probe_b(r, confirm=False),
                    keep="_early_op",
                )
            with self._op_lock:
                state = op["state"]
            if state == "running":
                return
            return self._seq_end("failed", "b_not_seen", seq)
        self._goto(seq, "wait")

    def _seq_marker(self, seq, ctx: Ctx) -> None:
        state = ctx.marker["state"]
        if state == "absent":
            # Set first, so that its age runs while the checks are made; then the
            # check whether a gripper stack runs is begun in the background (A's
            # start needs its answer and it is the longest of the checks).
            if self._seq_locked(seq, "sequence_marker", self._do_touch) is not None:
                with self._seq_lock:
                    seq["_marker_made"] = True  # (this start made it: it gives it back)
                self._start_b_probe(seq, ctx)
        elif state == "present" and ctx.marker["ours"]:
            self._start_b_probe(seq, ctx)
            self._goto(seq, "probe")
        else:
            self._seq_end(
                "failed",
                "marker_not_ours" if state == "present" else "marker_unusable",
                seq,
            )

    def _seq_wait(self, seq, ctx: Ctx) -> None:
        """Until the marker is old enough and no heavy job of this computer
        runs: heavy jobs yield to the marker within about 30 s, and a job that
        does not in ``HEAVY_WAIT_CAP_S`` ends the start (the person looks at it).
        When the heavy jobs cannot be told, the fixed wait of old applies
        (``marker.wait_s`` is then at least 45 s)."""
        marker = ctx.marker
        if marker["state"] != "present":
            return self._seq_end("failed", "marker_gone", seq)
        heavy = marker.get("heavy") or {"state": "unknown", "count": 0}
        if heavy["state"] == "running":
            waited = ctx.r["now"] - seq.get("step_at", ctx.r["now"])
            if waited > HEAVY_WAIT_CAP_S:
                return self._seq_end(
                    "failed",
                    "heavy_job_still_running",
                    seq,
                    params={"count": heavy["count"]},
                )
            return
        # (two clean looks in a row, as heavy_gate's wait-clear wants; the
        # fixed wait of old needs no looks: its age is the evidence)
        looked = (
            heavy["state"] == "unknown"
            or seq.get("_clean_looks", 0) >= HEAVY_CLEAN_LOOKS
        )
        if (
            marker["remaining_s"] == 0
            and looked
            and ctx.r["sockets"]["state"] == "inactive"
        ):
            self._goto(seq, "arm")

    def _seq_arm(self, seq, ctx: Ctx) -> None:
        with self._seq_lock:
            if seq["state"] != "running":
                return  # (cancelled meanwhile: A is not started)
            attested = seq["attest"]
            body = {"request_id": seq.get("request_id")}
        started = self._seq_locked(
            seq,
            "sequence_arm",
            lambda r: self._do_start("a", body, r, attested=attested),
        )
        if started is not None:  # (None: a stop or a cancel was answered first)
            with self._seq_lock:
                seq["_arm_started"] = True
            self._goto(seq, "await_a")

    @staticmethod
    def _a_was_stopped(ctx: Ctx) -> bool:
        """A is idle or stopping, or its record says a stop was asked for or
        done (the phase is written when the press is made, before the unit is
        gone)."""
        phase = (ctx.records.get("a") or {}).get("phase")
        return ctx.states["a"]["state"] in ("idle", "stopping") or phase in (
            "stopping",
            "stopped",
            "acknowledged",
        )

    def _seq_await_a(self, seq, ctx: Ctx) -> None:
        if ctx.states["a"]["state"] == "ready":
            with self._seq_lock:
                if seq["state"] == "running":
                    self._after_a(seq)
        else:
            self._confirm_b_while_a_starts(seq, ctx)

    def _confirm_b_while_a_starts(self, seq, ctx: Ctx) -> None:
        """A's start takes about 5 s and nothing in it needs B: when the chain
        goes straight on to B and the only answer about it is one unconfirmed
        "absent" (``_b_absence_unconfirmed``), the second answer of the graph is
        asked for now, beside A's start, so that the step after A finds it made
        (``_seq_b_probe`` waits for this operation, and goes on to B at once when
        it is done). A start that stops at A or pauses before B gets its second
        answer while it waits for the person (``_refresh_b_probe``). Optional: a
        refusal (the first check not ended yet, another check, a busy lock) is
        tried again at the next look, and the step after A makes the check itself
        when this one never got made."""
        if (
            seq["pauses"]["b"]  # (a start that stops at A pauses before B)
            or seq.get("_probe_op") is not None
            or not self._b_absence_unconfirmed(ctx)
        ):
            return
        with contextlib.suppress(stack.Refuse):
            self._seq_locked(
                seq, "sequence_probe_b", self._do_probe_b, keep="_probe_op"
            )

    # What follows a stage that is up. All three run under the sequence's lock,
    # from a running step or from the wait for the person's own start.
    def _after_a(self, seq) -> None:
        self._mark(seq, "await_a", "done", only=("running",))
        if seq["through"] == "c" and not seq["pauses"]["b"]:
            seq.update(state="running", next=None)
            self._set_step(seq, "b_probe")
        else:
            seq.update(
                state="waiting_confirm", step=None, next="b", since=round(self.now(), 3)
            )

    def _after_b(self, seq) -> None:
        self._mark(seq, "b", "done", only=("running",))
        if seq["through"] == "c" and not seq["pauses"]["c"]:
            seq.update(state="running", next=None)
            self._set_step(seq, "c_preflight")
        else:
            seq.update(
                state="waiting_confirm", step=None, next="c", since=round(self.now(), 3)
            )

    def _after_c(self, seq) -> None:
        self._mark(seq, "c", "done", only=("running",))
        self._seq_end("done", None, seq)

    def _step_waiting(self, seq, ctx: Ctx) -> None:
        st = ctx.states
        if self.now() - seq["since"] > SEQUENCE_WAIT_S:
            return self._seq_end("cancelled", "expired", seq)
        # A that cannot be vouched for for a while (the health file went stale,
        # the user manager did not answer) is not a lost A: only a unit that is
        # no longer there, or no longer well, ends the wait.
        if st["a"]["state"] == "unknown":
            return
        if st["a"]["state"] != "ready":
            return self._seq_end("failed", "a_lost", seq)
        if seq["next"] == "b" and not ctx.busy and self._b_probe_due(ctx):
            self._refresh_b_probe(seq)
        with self._seq_lock:
            if seq["state"] != "waiting_confirm":
                return
            if seq["next"] == "b" and st["b"]["state"] == "ready":
                self._skip_pending(seq, ("b_probe", "b"))
                self._after_b(seq)
            elif seq["next"] == "c" and st["c"]["state"] == "ready":
                self._skip_pending(seq, ("c_preflight", "c"))
                self._after_c(seq)

    def _refresh_b_probe(self, seq) -> None:
        """Make the check whether a gripper stack runs again while the guided
        start waits for B, so that B's start is there when the person comes to
        it. Optional: a busy lock, another operation, or a B that was started
        meanwhile are tried again at the next tick, never the sequence's end."""
        try:
            self._seq_locked(
                seq, "sequence_probe_b", self._do_probe_b, live=("waiting_confirm",)
            )
        except stack.Refuse:
            pass

    # -- the chain: B and C by the guided start ----------------------------------
    def _chain_up(self, seq, ctx: Ctx, *keys) -> bool:
        """Are the stages before this one still ready? A stage that cannot be
        vouched for for a while (``unknown``) is waited for; one that is no
        longer there ends the sequence (``a_lost``, ``b_lost``). The sequence
        never goes on to B without A ready, nor to C without both."""
        for key in keys:
            state = ctx.states[key]["state"]
            if state == "unknown":
                return False
            if state != "ready":
                self._seq_end("failed", f"{key}_lost", seq)
                return False
        return True

    def _chain_start(self, seq, key, back) -> None:
        """Start B or C by the single start's own ``_do_start`` (every gate
        again, under the machine-wide lock) with the confirmations recorded at
        the request."""
        with self._seq_lock:
            if seq["state"] != "running":
                return
            # (a part that was ready or starting when the request was made has no
            # entry; if it is not now, ``_do_start`` finds nothing ticked for it
            # and refuses with ``attest_missing``)
            attested = seq["_attests"].get(key)
            body = {"request_id": seq.get("request_id")}
        try:
            started = self._seq_locked(
                seq,
                f"sequence_{key}",
                lambda r: self._do_start(key, body, r, attested=attested),
            )
        except ActRefuse as refusal:
            if refusal.code == "b_probe_started":
                # (the check was too old after all: it is made, then B is tried
                # again; nothing was started)
                return self._goto(seq, back, how="pending")
            raise
        if started is not None:
            with self._seq_lock:
                seq[f"_started_{key}"] = True

    def _seq_b_probe(self, seq, ctx: Ctx) -> None:
        if not self._chain_up(seq, ctx, "a"):
            return
        if ctx.states["b"]["state"] in ("ready", "starting", "failed", "dead"):
            # B is up (the person's own start) or its start has to say what
            # became of it: no check to make
            return self._goto(seq, "b", how="skipped")
        op = seq.get("_probe_op")
        if op is not None:
            with self._op_lock:
                state = op["state"]
            if state == "running":
                return
            if state == "failed":
                return self._seq_end("failed", "b_not_seen", seq)
            return self._goto(seq, "b")
        if not self._b_probe_due(ctx):
            return self._goto(seq, "b")
        if ctx.busy:
            return
        self._seq_locked(seq, "sequence_probe_b", self._do_probe_b, keep="_probe_op")

    def _seq_b(self, seq, ctx: Ctx) -> None:
        if not self._chain_up(seq, ctx, "a"):
            return
        b = ctx.states["b"]["state"]
        if b == "ready":
            with self._seq_lock:
                if seq["state"] == "running":
                    self._after_b(seq)
            return
        if b in ("failed", "dead"):
            return self._seq_end("failed", f"b_{b}", seq)
        if b == "starting":
            return
        if seq.get("_started_b"):
            if b == "unknown":
                return  # (cannot be vouched for for a while)
            return self._seq_end("failed", "b_lost", seq)
        self._chain_start(seq, "b", back="b_probe")

    def _seq_c_preflight(self, seq, ctx: Ctx) -> None:
        if not self._chain_up(seq, ctx, "a", "b"):
            return
        if ctx.states["c"]["state"] in ("ready", "starting", "failed", "dead"):
            return self._goto(seq, "c", how="skipped")
        op = seq.get("_pre_op")
        if op is not None:
            with self._op_lock:
                state, code = op["state"], op["code"]
            if state == "running":
                return
            if state == "done":
                return self._goto(seq, "c")
            listing = (ctx.c_pre or {}).get("controllers_state")
            if (
                code == "c_preflight_failed"
                and listing in ("absent", "unknown")
                and not seq.get("_pre_retried")
            ):
                # The listing of the gripper controllers did not answer (the
                # first real run needed a second press): ask once more.
                seq["_pre_retried"] = True
                seq["_pre_op"] = None
                return
            return self._seq_end("failed", code or "c_preflight_failed", seq)
        if ctx.busy:
            return
        self._seq_locked(
            seq, "sequence_preflight_c", lambda r: self._chain_preflight_c(seq, r)
        )

    def _chain_preflight_c(self, seq, r) -> None:
        # Readiness can change after the tick's first look. Under the machine
        # lock, wait for unknown stages just as _chain_up does outside it;
        # never begin the checks with an older, ready context.
        ctx = self._context(r, holding=True)
        if not self._chain_up(seq, ctx, "a", "b"):
            return
        self._start_preflight_c(ctx)
        with self._op_lock:
            seq["_pre_op"] = self._op

    def _seq_c(self, seq, ctx: Ctx) -> None:
        if not self._chain_up(seq, ctx, "a", "b"):
            return
        c = ctx.states["c"]["state"]
        if c == "ready":
            with self._seq_lock:
                if seq["state"] == "running":
                    self._after_c(seq)
            return
        if c in ("failed", "dead"):
            return self._seq_end("failed", f"c_{c}", seq)
        if c == "starting":
            return
        if seq.get("_started_c"):
            if c == "unknown":
                return
            # (stopped by a person, or gone: it vanished after it was started)
            return self._seq_end("failed", "c_dead", seq)
        self._chain_start(seq, "c", back="c_preflight")

    # -- the guided stop ("stop whole stack") ------------------------------------
    def sequence_stop(self, body) -> dict:
        """C, then A, then B, each by the single stop's own code and gates,
        each gone before the next. It starts nothing, leaves M alone (the
        marker's release already follows A's stop) and needs no control file."""
        # The way out does not depend on the way in (RIG-04): a guided start that is
        # still advancing ends FIRST, before the stop is judged. A stop-all the
        # backend refuses (a corrupt record, a busy lock) must not leave the chain
        # going on to start B and C. Every step of the chain re-checks under the
        # machine lock, so only future steps are prevented.
        self._end_live_start("superseded")
        return self._write(
            body, "sequence_stop", lambda r: self._seq_begin_stop(body, r)
        )

    def _end_live_start(self, reason) -> bool:
        with self._seq_lock:
            if self._seq.get("kind") == "start" and self._seq["state"] in (
                "running",
                "waiting_confirm",
            ):
                self._seq_end("cancelled", reason)
                return True
        return False

    def _seq_begin_stop(self, body, r) -> dict:
        given = body.get("attest") or {}
        if unknown := sorted(k for k in given if k not in ATTEST_KEYS):
            raise ActRefuse("attest_invalid", keys=", ".join(unknown))
        ctx = self._context(r, holding=True)
        rows = self.stop_all_blockers(ctx)  # (a stop that runs is one of them)
        codes = _codes(rows)
        refusing = [b for b in rows if b["code"] != "nothing_to_stop"]
        if refusing:
            raise ActRefuse(refusing[0]["code"], **refusing[0]["params"])
        with self._seq_lock:
            # A guided start that is running, or waiting for B or C, ends: it
            # would start what this stops.
            if self._seq.get("kind") == "start":
                self._seq_end("cancelled", "superseded")
            if "nothing_to_stop" in codes:
                return {"done": "already_stopped"}
            now = self.now()
            self._seq = {
                "state": "running",
                "kind": "stop",
                "step": SEQUENCE_STOP_STEPS[0],
                "next": None,
                "reason": None,
                "since": round(now, 3),
                "step_at": round(now, 3),
                "begun_at": round(now, 3),
                "request_id": body.get("request_id"),
                "reason_params": None,
                "took_s": None,
                "steps": self._new_steps(
                    SEQUENCE_STOP_STEPS, lambda step: step.removeprefix("stop_")
                ),
                "_issued": False,  # this step has sent its stop
                "_op": None,  # ... and the operation it began
            }
        self._defer(self._ensure_ticker)
        return {"done": "sequence_stop_started"}

    def _step_stop(self, seq, ctx: Ctx) -> None:
        """One look at the step of a guided stop that is running (the ticker
        calls ``advance`` every ``SEQUENCE_TICK_S``). A step with nothing of
        ours to stop is skipped at once; one that has is stopped by
        ``_do_stop`` (its gates are the single stop's) and waited for until the
        unit is gone. A failure ends the sequence there, with the reason; a
        press on "stop whole stack" again starts over and skips what is gone.
        Nothing here depends on a start's condition."""
        while seq["state"] == "running":
            key = seq["step"].removeprefix("stop_")
            spec = ctx.specs.units[key] if ctx.specs else None
            limit = (spec.stop_timeout_s if spec else stack_units.STOP_TIMEOUT_S) + (
                STOP_STEP_SLACK_S
            )
            if self.now() - seq["step_at"] > limit:
                return self._seq_end("failed", "step_timeout", seq)
            op = seq["_op"]
            if op is not None:
                with self._op_lock:
                    op_state = op["state"]
                if op_state == "failed":
                    return self._seq_end("failed", "stop_failed", seq)
            if seq["_issued"]:
                # Something was sent (a stop, or the acknowledge of a unit that
                # had already failed): all that is asked now is whether the
                # unit is gone. The unit's other evidence is not read again: a
                # health file that goes on showing the hardware for a few
                # seconds after A stopped would read as a part that is not ours.
                # (A read taken just before the worker ended may still show the
                # unit: look again at the next tick.)
                if self._unit_gone(key, ctx):
                    return self._stop_step_done(seq, "done")
                return
            refusing = [
                b for b in self.stop_blockers(key, ctx) if b["level"] == "required"
            ]
            if refusing:
                code = refusing[0]["code"]
                return self._seq_end("failed", STOP_ALL_RENAMED.get(code, code), seq)
            st = ctx.states[key]
            if not st["owned"] and st["state"] in ("idle", "unknown"):
                if key == "c" and st["state"] == "unknown":
                    # Nothing of ours to stop, but whether anything else runs
                    # there cannot be told: "nothing to stop" would be a guess,
                    # and A is not stopped under a C that may be up. A stop of A
                    # alone goes ahead with the warning; this one stops here.
                    return self._seq_end("failed", "stop_c_unknown", seq)
                if self._stop_step_done(seq, "skipped"):
                    continue  # (nothing was asked of the machine: the same read serves)
                return
            if st["state"] == "stopping":
                return  # (a stop is on its way: this one, or a person's)
            return self._stop_step_issue(seq, key)

    @staticmethod
    def _unit_gone(key, ctx: Ctx) -> bool:
        return (not ctx.states[key]["owned"]) and ctx.r["units"][key.upper()] is False

    def _stop_step_issue(self, seq, key) -> None:
        self._tls.stop_op = None
        try:
            answer = self._locked(
                f"sequence_stop_{key}", lambda r: self._do_stop(key, {}, r)
            )
        except stack.Refuse as exc:
            if exc.code not in TRANSIENT:
                self._seq_end("failed", STOP_ALL_RENAMED.get(exc.code, exc.code), seq)
            return
        with self._seq_lock:
            if seq["state"] == "running" and answer.get("done") != "already_stopped":
                seq["_issued"] = True
                seq["_op"] = self._tls.stop_op

    def _stop_step_done(self, seq, how) -> bool:
        """Close the step that is running and open the next (or end). False
        when the sequence is no longer running (cancelled meanwhile)."""
        with self._seq_lock:
            if seq["state"] != "running":
                return False
            steps = seq["steps"]
            here = next(n for n, i in enumerate(steps) if i["state"] == "running")
            self._item_state(steps[here], how)
            if here + 1 == len(steps):
                spent = [i["took_s"] for i in steps if i["took_s"]]
                seq.update(
                    state="done",
                    reason=None,
                    step=None,
                    next=None,
                    took_s=round(sum(spent), 2),
                )
                self._poke()
                return True
            self._item_state(steps[here + 1], "running")
            seq.update(
                step=steps[here + 1]["step"],
                step_at=round(self.now(), 3),
                _issued=False,
                _op=None,
            )
            self._poke()
            return True

    # -- logs ---------------------------------------------------------------------
    def log(self, key, lines) -> dict:
        if key not in stack_units.KEYS:
            raise ActRefuse("no_log")
        path = self._log_path(key, self._record(key))
        if not path:
            return {
                "schema": LOG_SCHEMA,
                "unit": stack.STACK_UNITS[key.upper()],
                "lines": [],
            }
        return {
            "schema": LOG_SCHEMA,
            "unit": stack.STACK_UNITS[key.upper()],
            "lines": deploy.page_lines(path, lines),
        }
