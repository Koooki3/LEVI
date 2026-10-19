"""The parts of the rig status (``GET /api/levi/rig/status``) and the advisory
``robot_idle`` verdict that merges them.

Every part is a pure function of injected inputs (a path, the roots, the
clock, a runner for ``systemctl``, the setup probes' ``guard`` part), so tests
need no real machine. Every part answers ``"state": "unknown"`` with a
``detail`` for what it cannot read and never raises for it:

- ``marker``: the arm-controller-active marker file (``touch`` while the
  controller runs, setup.md). Its presence and age come from ``lstat`` alone,
  the file is never opened; a stale marker is never cleared, here or anywhere.
  A marker whose folder does not exist is ``unknown``, not ``absent``;
- ``eval_sessions``: the demo client's ``<root>/.eval_sessions/*.json`` files
  (``levi.live.sessions``): one that has not ended (``ENDED_STATES``, the
  client's own list) while it was updated within ``SESSION_OPEN_WINDOW_S``,
  an ended one within ``SESSION_WINDOW_S``;
- ``units``: our own ``levi-aeri-*`` systemd user services and scopes (a
  policy server started by hand is a scope), from one ``systemctl --user
  list-units`` (no shell, ``SYSTEMCTL_TIMEOUT_S``);
- ``robot_idle``: ``busy`` when any source shows robot-side activity (a robot
  process or an active arm controller in the setup probes' quiet guard, a
  marker younger than ``MARKER_BUSY_S``, a session updated within
  ``SESSION_BUSY_S``, a ``levi-aeri-*`` unit in ``BUSY_UNITS``), else
  ``unknown`` when a source is unknown (the guard too when it is not
  ``quiet`` and found no robot process: it cannot tell "none" from "could not
  look"; a marker of ``MARKER_BUSY_S`` or more, which may be a leftover; a
  session that has not ended and has been silent for more than
  ``SESSION_BUSY_S``; a unit in a state nobody classified), else ``idle``. It
  is for display only and gates nothing. Each entry of ``reasons`` is
  ``{source, state, text, code, params}``: ``text`` is English detail, ``code``
  a stable dotted name and ``params`` a flat object of strings and numbers
  (never a path) that a page can translate and fill in.

The parts keep their ages at full precision; the verdict decides its limits
from those, and ``document`` rounds them to 0.1 s for output only.

Session files are read through ``probes.read_regular`` (``lstat`` first, then
``O_NONBLOCK | O_NOFOLLOW``, a size cap): a FIFO, device, directory or
symbolic link is never opened. No process directory (``/proc/<pid>``) is
read, not even to ask whether a session's process is alive; no port is
opened. The response holds no path.
"""

import json
import math
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from levi.live import sessions as live_sessions
from levi.setup import probes

SCHEMA = "levi.rig.status.v1"
UNIT_GLOB = "levi-aeri-*"
UNIT_NAME = re.compile(r"levi-aeri-[\w:.@\\-]+", re.ASCII)
SYSTEMCTL_TIMEOUT_S = 2.0
SESSION_WINDOW_S = 600.0  # an ended session older than this is not listed
# One that has not ended is listed this long: the client's own limit (an hour)
# after which a silent file counts as crashed whatever its pid.
SESSION_OPEN_WINDOW_S = 3600.0
SESSION_BUSY_S = 60.0  # a session updated this recently is robot-side activity
SESSION_SKEW_S = 600.0  # a file older than the longest window + skew is not opened
# The states after which the client writes no more (``ENDED`` in
# openpi/examples/fr3_local/eval_session.py); any other state, or none, is a
# session that may still be running.
ENDED_STATES = ("fault", "stopped", "finished")
MARKER_BUSY_S = 12 * 3600.0  # an older marker may be a leftover: unknown, not busy
MARKER_NO_FOLDER = "the marker folder does not exist"
MAX_SESSION_BYTES = 1 << 18
MAX_SESSION_FILES = 512  # entries read per session folder
MAX_SESSIONS = 50
MAX_ROOTS = 16
MAX_UNITS = 50
MAX_REASONS = 5  # busy reasons per source
# systemd ActiveState: these still run (or are starting, stopping, reloading);
# these two add nothing; any other (maintenance, refreshing, ...) is unknown.
BUSY_UNITS = ("active", "activating", "deactivating", "reloading")
QUIET_UNITS = ("inactive", "failed")
GUARD_OLD_SAMPLE = "guard.old_sample"  # the code api.py puts on a guard it dropped


def unknown_marker(detail) -> dict:
    return {"state": "unknown", "age_s": None, "detail": detail}


def unknown_sessions(detail, roots=0) -> dict:
    return {"state": "unknown", "roots": roots, "sessions": [], "detail": detail}


def unknown_units(detail) -> dict:
    return {"state": "unknown", "items": [], "detail": detail}


def _text(value, limit) -> str:
    # Lone surrogates (a "\ud800" escape in a JSON file) cannot be encoded.
    return str(value)[:limit].encode("utf-8", "replace").decode("utf-8")


# ------------------------------------------------------------------ marker


def marker_part(path, now) -> dict:
    """``{"state": absent|present|unknown, "age_s", "detail"}`` of the marker
    file at ``path``; ``age_s`` is the age of its modification time. A regular
    file is present whatever its permissions (it is decided from ``lstat``
    and never opened); a link, directory or FIFO is ``unknown``, and so is a
    file that is missing because its folder is (a wrong checkout, an unmounted
    disk says nothing about the file)."""
    if path is None:
        return unknown_marker("the marker location is not known")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return _marker_missing(path)
    except OSError:
        return unknown_marker("the marker could not be examined")
    if not stat.S_ISREG(st.st_mode):
        return unknown_marker("the marker is not a regular file")
    return {
        "state": "present",
        "age_s": max(0.0, now - st.st_mtime),
        "detail": None,
    }


def _marker_missing(path) -> dict:
    """``absent`` if the folder the file would be in is there, else ``unknown``.
    ``stat``, not ``lstat``: a folder that is a link to nothing is not there."""
    try:
        os.stat(Path(path).parent)
    except FileNotFoundError:
        return unknown_marker(MARKER_NO_FOLDER)
    except OSError:
        return unknown_marker("the marker could not be examined")
    return {"state": "absent", "age_s": None, "detail": None}


# ------------------------------------------------------------------ evaluation sessions


def _updated(value, fallback) -> float:
    try:
        stamp = live_sessions.parse_time(value)
    except (ValueError, OverflowError, OSError):
        stamp = None
    return stamp if stamp is not None and math.isfinite(stamp) else fallback


def _session_row(entry, now):
    """``(row, unusable)`` for one file of a session folder. ``row`` is None
    for a file that is no session or too old to count (an ended session after
    ``SESSION_WINDOW_S``, any other after ``SESSION_OPEN_WINDOW_S``);
    ``unusable`` is true for a recent file that could not be read as one."""
    name = entry.name
    if not name.endswith(".json") or name.startswith("."):
        return None, False
    try:
        mtime = os.lstat(entry.path).st_mtime
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True
    if now - mtime > SESSION_OPEN_WINDOW_S + SESSION_SKEW_S:
        return None, False
    try:
        raw, truncated, st = probes.read_regular(entry.path, MAX_SESSION_BYTES)
        data = None if truncated else json.loads(raw.decode("utf-8", "replace"))
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError, RecursionError):
        return None, True
    if not isinstance(data, dict):
        return None, True
    group = data.get("group")
    if not (isinstance(group, str) and group):
        group, separator, _ = name[: -len(".json")].partition("__")
        if not (separator and group):
            return None, False
    age = max(0.0, now - _updated(data.get("updated_at"), st.st_mtime))
    state = data.get("state")
    ended = state in ENDED_STATES
    if age > (SESSION_WINDOW_S if ended else SESSION_OPEN_WINDOW_S):
        return None, False
    return {
        "group": _text(group, 120),
        "age_s": age,
        "status": _text(state, 40) if isinstance(state, str) and state else None,
    }, False


def sessions_part(roots, now, why=None) -> dict:
    """The evaluation sessions under ``<root>/.eval_sessions`` for each of
    ``roots`` (an ended one updated within ``SESSION_WINDOW_S``, any other
    within ``SESSION_OPEN_WINDOW_S``), freshest first. A root
    that cannot be listed, or a recent file that cannot be read as a session,
    makes the part ``unknown`` (it cannot claim to have seen everything); the
    sessions found are still listed. ``why`` says why there are no roots."""
    roots = list(roots)
    if not roots:
        return unknown_sessions(why or "no rollout roots are known")
    scanned = roots[:MAX_ROOTS]
    rows, bad_roots, unusable, capped = [], 0, 0, False
    for root in scanned:
        folder = Path(root).expanduser() / live_sessions.SESSION_DIR
        try:
            with os.scandir(folder) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_SESSION_FILES:
                        capped = True
                        break
                    row, bad = _session_row(entry, now)
                    unusable += bad
                    if row:
                        rows.append(row)
        except (FileNotFoundError, NotADirectoryError):
            # A root without a session folder holds no sessions; a root that is
            # not there at all (an unmounted disk) tells us nothing.
            try:
                if not Path(root).expanduser().is_dir():
                    bad_roots += 1
            except OSError:  # the probe itself failed (a stale handle)
                bad_roots += 1
        except OSError:
            bad_roots += 1
    problems = []
    if len(roots) > MAX_ROOTS:
        problems.append(f"only the first {MAX_ROOTS} rollout roots were read")
    if bad_roots:
        problems.append(
            f"{bad_roots} of {len(scanned)} rollout root(s) could not be listed"
        )
    if unusable:
        problems.append(f"{unusable} session file(s) could not be read")
    if capped:
        problems.append(f"the scan stopped after {MAX_SESSION_FILES} files in a folder")
    notes = list(problems)
    rows.sort(key=lambda r: r["age_s"])
    if len(rows) > MAX_SESSIONS:
        rows = rows[:MAX_SESSIONS]
        notes.append(f"only the {MAX_SESSIONS} most recent sessions are listed")
    return {
        "state": "unknown" if problems else "ok",
        "roots": len(scanned),
        "sessions": rows,
        "detail": "; ".join(notes) or None,
    }


# ------------------------------------------------------------------ our systemd units


def run_systemctl(argv, timeout=SYSTEMCTL_TIMEOUT_S) -> tuple[int, str]:
    """``(exit code, stdout)`` of one command, without a shell. Raises
    FileNotFoundError (no such program), ``subprocess.TimeoutExpired`` or
    another OSError/ValueError; ``units_part`` turns those into ``unknown``."""
    done = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return done.returncode, done.stdout


def parse_units(text) -> list:
    """``[{"name", "active", "sub"}]`` from ``systemctl list-units
    --no-legend --plain`` lines (UNIT LOAD ACTIVE SUB DESCRIPTION), our
    ``levi-aeri-*`` units only: busy ones first, then those in a state nobody
    classified, then the rest. Anything else is ignored."""
    items = []
    for line in text.splitlines():
        fields = line.lstrip(" \t●○×*").split()
        if len(fields) < 4 or not UNIT_NAME.fullmatch(fields[0]):
            continue
        items.append(
            {"name": fields[0], "active": fields[2][:32], "sub": fields[3][:32]}
        )
    items.sort(key=lambda u: (_unit_rank(u["active"]), u["name"]))
    return items


def _unit_rank(active) -> int:
    if active in BUSY_UNITS:
        return 0
    return 2 if active in QUIET_UNITS else 1


def unreadable_rows(text) -> int:
    """How many rows that start like one of our units ``parse_units`` leaves
    out (a cut-off name, too few fields)."""
    count = 0
    for line in text.splitlines():
        fields = line.lstrip(" \t●○×*").split()
        if not fields or not fields[0].startswith("levi-aeri"):
            continue
        if len(fields) < 4 or not UNIT_NAME.fullmatch(fields[0]):
            count += 1
    return count


def units_part(runner=None) -> dict:
    """Our ``levi-aeri-*`` services and scopes, in any state.
    ``runner(argv, timeout)`` returns ``(exit code, stdout)`` (default
    ``run_systemctl``)."""
    argv = [
        "systemctl",
        "--user",
        "list-units",
        "--type=service,scope",
        "--all",
        "--no-legend",
        "--plain",
        "--full",
        UNIT_GLOB,
    ]
    try:
        code, out = (runner or run_systemctl)(argv, SYSTEMCTL_TIMEOUT_S)
    except FileNotFoundError:
        return unknown_units("systemctl is not installed")
    except subprocess.TimeoutExpired:
        return unknown_units(
            f"systemctl did not answer within {SYSTEMCTL_TIMEOUT_S:g} s"
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return unknown_units(f"systemctl failed ({type(exc).__name__})")
    if code != 0:
        return unknown_units(f"systemctl exited with {code}")
    items = parse_units(out)
    note = None
    if len(items) > MAX_UNITS:
        items = items[:MAX_UNITS]
        note = f"only the first {MAX_UNITS} units are listed (active ones first)"
    lost = unreadable_rows(out)
    if lost:
        # A row of ours we could not read may be a running unit.
        return {
            "state": "unknown",
            "items": items,
            "detail": f"{lost} systemctl row(s) of ours could not be read",
        }
    return {"state": "ok", "items": items, "detail": note}


# ------------------------------------------------------------------ robot_idle


def _reason(source, state, text, code, params=None) -> dict:
    return {
        "source": source,
        "state": state,
        "text": text,
        "code": code,
        "params": params or {},
    }


def _span(seconds) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _age(value):
    """``value`` as a finite age in seconds, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _guard_reasons(guard, busy, unknown):
    guard = _dict(guard)
    found = guard.get("robot")
    robots = (
        [p for p in found if isinstance(p, dict)] if isinstance(found, list) else []
    )
    if robots:
        names = sorted({_text(p.get("name") or "?", 64) for p in robots})
        listed = ", ".join(names[:MAX_REASONS])
        busy.append(
            _reason(
                "guard",
                "busy",
                "robot-side process running: " + listed,
                "guard.robot_process",
                {"names": listed},
            )
        )
    flag = guard.get("controller_active")
    if flag is not None and flag is not False:
        if flag:
            # True, or anything else truthy: the file did not say "no".
            busy.append(
                _reason(
                    "guard",
                    "busy",
                    "the arm controller is active (the FR3 health file says so)",
                    "guard.controller_active",
                )
            )
        else:
            unknown.append(
                _reason(
                    "guard",
                    "unknown",
                    "the controller flag in the FR3 health file is neither true "
                    "nor false, so an active arm controller cannot be ruled out",
                    "guard.controller_value",
                )
            )
    if guard.get("state") != "ok":
        detail = guard.get("detail") or "the real-robot quiet guard did not answer"
        code = GUARD_OLD_SAMPLE if guard.get("code") == GUARD_OLD_SAMPLE else None
        unknown.append(
            _reason("guard", "unknown", _text(detail, 200), code or "guard.unreadable")
        )
    elif not robots and guard.get("quiet") is not True:
        # quiet False without a process is a strained host, and the guard
        # does not say whether it also failed to look at every process.
        strained = guard.get("strained")
        why = (
            "; ".join(_text(s, 80) for s in strained[:2])
            if isinstance(strained, list)
            else ""
        )
        if guard.get("truncated") is True:
            code = "guard.truncated"
        elif guard.get("quiet") is False:
            code = "guard.not_quiet"
        else:
            code = "guard.unreadable"
        unknown.append(
            _reason(
                "guard",
                "unknown",
                "the quiet guard found no robot process but cannot tell whether "
                "it saw them all (hidden or unreadable processes, a cut scan, "
                "missing pressure readings or a strained host"
                + (f": {why}" if why else "")
                + ")",
                code,
            )
        )


def _marker_reasons(marker, busy, unknown):
    marker = _dict(marker)
    state = marker.get("state")
    if state == "absent":
        return
    if state != "present":
        detail = marker.get("detail")
        unknown.append(
            _reason(
                "marker",
                "unknown",
                _text(detail or "the marker could not be read", 200),
                "marker.no_folder"
                if detail == MARKER_NO_FOLDER
                else "marker.unreadable",
            )
        )
        return
    age = _age(marker.get("age_s"))
    params = {} if age is None else {"age_s": round(age, 1)}
    if age is not None and age >= MARKER_BUSY_S:
        # Nothing here tells a leftover of a crash from a very long run, so the
        # verdict is never idle while the file exists.
        unknown.append(
            _reason(
                "marker",
                "unknown",
                f"the arm-controller marker has been there for {_span(age)}; it "
                "may be a leftover of a crash or belong to a very long run "
                "(remove it by hand if the arm stack is down)",
                "marker.old",
                params,
            )
        )
        return
    text = "the arm-controller marker is present"
    if age is not None:
        text += f" (for {_span(age)})"
    busy.append(_reason("marker", "busy", text, "marker.present", params))


def _session_params(row, age) -> dict:
    params = {"group": _text(row.get("group"), 120), "age_s": round(age, 1)}
    status = row.get("status")
    if isinstance(status, str) and status:
        params["status"] = _text(status, 40)
    return params


def _session_reasons(part, busy, unknown):
    part = _dict(part)
    recent, silent = [], []
    for row in part.get("sessions") or []:
        row = _dict(row)
        age = _age(row.get("age_s"))
        if age is None:
            continue
        if age <= SESSION_BUSY_S:
            recent.append((row, age))
        elif row.get("status") not in ENDED_STATES:
            # Not updated for a while and never said it ended: it may have
            # crashed or hung with the robot still moving.
            silent.append((row, age))
    for row, age in recent[:MAX_REASONS]:
        params = _session_params(row, age)
        status = f", {params['status']}" if "status" in params else ""
        busy.append(
            _reason(
                "eval_sessions",
                "busy",
                f"evaluation session {params['group']!r} updated {age:.0f} s ago{status}",
                "sessions.recent",
                params,
            )
        )
    for row, age in silent[:MAX_REASONS]:
        params = _session_params(row, age)
        status = f" ({params['status']})" if "status" in params else ""
        unknown.append(
            _reason(
                "eval_sessions",
                "unknown",
                f"evaluation session {params['group']!r} was last updated "
                f"{_span(age)} ago and has not ended{status}; its client may "
                "have crashed or hung",
                "sessions.silent",
                params,
            )
        )
    if part.get("state") != "ok":
        detail = part.get("detail") or "the evaluation sessions could not be read"
        unknown.append(
            _reason(
                "eval_sessions", "unknown", _text(detail, 200), "sessions.unreadable"
            )
        )


def _unit_reasons(part, busy, unknown):
    part = _dict(part)
    rows = [_dict(row) for row in part.get("items") or []]
    for row in [r for r in rows if r.get("active") in BUSY_UNITS][:MAX_REASONS]:
        name, active, sub = (_text(row.get(k), 120) for k in ("name", "active", "sub"))
        busy.append(
            _reason(
                "units",
                "busy",
                f"{name} is {active} ({sub})",
                "units.active",
                {"name": name, "active": active, "sub": sub},
            )
        )
    odd = [r for r in rows if r.get("active") not in BUSY_UNITS + QUIET_UNITS]
    for row in odd[:MAX_REASONS]:
        name, active = (_text(row.get(k), 120) for k in ("name", "active"))
        unknown.append(
            _reason(
                "units",
                "unknown",
                f"{name} is {active} ({_text(row.get('sub'), 120)}), a state "
                "that is not known to be quiet",
                "units.state_unknown",
                {"name": name, "active": active},
            )
        )
    if part.get("state") != "ok":
        detail = part.get("detail") or "our systemd units could not be listed"
        unknown.append(
            _reason("units", "unknown", _text(detail, 200), "units.unreadable")
        )


def robot_idle(guard, marker, eval_sessions, units) -> dict:
    """``{"state": idle|busy|unknown, "reasons": [{source, state, text, code,
    params}]}``. ``guard`` is the ``guard`` part of the setup probes' status;
    the others are this module's parts, with their ages at full precision (the
    limits are decided on those). Reasons list what is busy, then what is
    unknown."""
    busy, unknown = [], []
    _guard_reasons(guard, busy, unknown)
    _marker_reasons(marker, busy, unknown)
    _session_reasons(eval_sessions, busy, unknown)
    _unit_reasons(units, busy, unknown)
    state = "busy" if busy else "unknown" if unknown else "idle"
    return {"state": state, "reasons": busy + unknown}


def _shown(row) -> dict:
    """A copy of ``row`` with its ``age_s`` rounded for output."""
    age = _age(_dict(row).get("age_s"))
    return row if age is None else {**row, "age_s": round(age, 1)}


def document(now, marker, eval_sessions, units, guard) -> dict:
    """The whole ``levi.rig.status.v1`` answer. Ages are rounded here, after
    the verdict is made from the exact ones; the parts handed in are not
    changed (a finished read is reused for a while)."""
    sampled = datetime.fromtimestamp(now, tz=UTC).astimezone()
    sessions = _dict(eval_sessions)
    rows = sessions.get("sessions")
    shown_sessions = (
        {**sessions, "sessions": [_shown(row) for row in rows]}
        if isinstance(rows, list)
        else eval_sessions
    )
    return {
        "schema": SCHEMA,
        "sampled_at": sampled.isoformat(timespec="seconds"),
        "marker": _shown(marker),
        "eval_sessions": shown_sessions,
        "units": units,
        "robot_idle": robot_idle(guard, marker, eval_sessions, units),
    }
