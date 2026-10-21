"""What an AERI run can leave behind, and the two stops the preparation card
offers for it (``aeri_api.py``):

* **leftover units**: a terminal D that was hung up or killed does not run its
  7/7 shutdown, so its model and policy units stay, hold GPU memory and make the
  next run refuse. ``leftover_units`` picks them out of the user's unit list by
  name: ``levi-aeri-vlm-*`` and ``levi-aeri-policy-*`` services, **never**
  ``levi-aeri-policy-manual-*`` (a policy server a person started by hand may be
  serving a robot, and the page's own deploy has its own stop with the client
  checks) and never the evaluation client's or the page-hosted terminal's scopes;
* **the live annotation service** (``levi live``), which holds port 8100 and the
  GPU lock: ``systemctl --user stop <[live].unit>`` (decided 22 Oct, O-6). It is
  how the product's own live-service stop (``levi/live/service_control.py``)
  stops a service its unit runs. The unit's ``ExecStop`` is ``levi live stop``
  with the live workspace spelt out, ``TimeoutStopSec=180`` bounds the wait and
  ends what is left of its cgroup, and the unit's state is what the card reads
  before and after. ``levi live stop`` without ``--workspace`` finds its service
  by the workspace of the last one started with the same home: for another it
  says "not running" and exits 0, with the service still up.

Both writes are a person's, need their typed phrase, and refuse while an AERI
run is alive, a hosted evaluation client or session is on, or the arm lock is
held (the leftovers also while the GPU lock is held, which a run takes first).
A source that cannot be read refuses too. The refusal of the command is echoed
as it is (paths scrubbed), never worked around.

These stop mistakes, not another program of the same account.
"""

import re
import subprocess

from . import aeri_prep
from .aeri_prep import Refuse

# levi.automatic.subtask_units.VLM_UNIT and levi.own_units.POLICY_UNIT (tests
# compare the patterns, so a rename there cannot silently blind this list).
VLM_UNIT = re.compile(r"levi-aeri-vlm-[a-z0-9][a-z0-9-]{0,46}")
POLICY_UNIT = re.compile(r"levi-aeri-policy-[a-z0-9][a-z0-9-]{0,46}")
MANUAL_PREFIX = "levi-aeri-policy-manual-"
STOP_TIMEOUT_S = 90.0
# The unit's TimeoutStopSec is 180 s and ``systemctl stop`` waits for the stop job
# (``service_control.STOP_TIMEOUT_S``, the same figure).
LIVE_STOP_TIMEOUT_S = 210.0
# ``levi.live.service_control.UNIT_NAME`` (a test compares the patterns): only
# ``levi-live.service`` or a variant of it is ever stopped from here, whatever the
# site file says.
LIVE_UNIT = re.compile(r"levi-live(?:[-@_.:][A-Za-z0-9@._:-]{0,100})?\.service")
LIVE_UNIT_FORBIDDEN = "levi-product"
TAIL_CHARS = 400
# The only variables ``systemctl --user`` gets: no
# ``LEVI_*`` (the UI token among them), no token of any kind.
LIVE_ENV_KEYS = (
    "HOME",
    "USER",
    "LANG",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)
LIVE_ENV_PATH = "/usr/local/bin:/usr/bin:/bin"
ACTIVE_STATES = ("active", "activating", "reloading", "deactivating")


def leftover_units(items) -> list:
    """``[{"name", "kind", "active", "sub"}]`` of the units in ``items`` (the
    ``status.parse_units`` shape) that are leftovers of an AERI run: services
    named ``levi-aeri-vlm-*`` or ``levi-aeri-policy-*`` but not
    ``levi-aeri-policy-manual-*``."""
    out = []
    for item in items:
        name = str(item.get("name") or "")
        if not name.endswith(".service"):
            continue
        stem = name.removesuffix(".service")
        if VLM_UNIT.fullmatch(stem):
            kind = "vlm"
        elif POLICY_UNIT.fullmatch(stem) and not stem.startswith(MANUAL_PREFIX):
            kind = "policy"
        else:
            continue
        out.append(
            {
                "name": name,
                "kind": kind,
                "active": item.get("active"),
                "sub": item.get("sub"),
            }
        )
    return out


def any_aeri_unit(items) -> list:
    """The names of every ``levi-aeri-vlm-*`` and ``levi-aeri-policy-*`` unit in
    ``items``, of any kind and state (the readiness row: such a unit may hold GPU
    memory, a port or a switch record the next run would trip on)."""
    out = []
    for item in items:
        name = str(item.get("name") or "")
        stem = name.removesuffix(".service").removesuffix(".scope")
        if VLM_UNIT.fullmatch(stem) or POLICY_UNIT.fullmatch(stem):
            out.append(name)
    return out


def why_not(*, d_alive, e_active, arm_held, gpu_held=False):
    """``{"code", "text"}`` of the first reason a stop is not offered, or None.
    Each argument is True (the obstacle is there), False (not), or None (could not
    be read: that refuses too, as ``unreadable``). ``gpu_held=False`` leaves the
    GPU lock out (the live-service stop does not need it)."""
    checks = (
        ("d_active", d_alive, "An AERI run is alive."),
        ("e_active", e_active, "A hosted evaluation client or session is on."),
        ("arm_in_use", arm_held, "The arm lock is held."),
        ("gpu_in_use", gpu_held, "The GPU lock is held."),
    )
    for code, value, text in checks:
        if value is True:
            return {"code": code, "text": text}
    for code, value, _ in checks:
        if value is None:
            return {
                "code": "unreadable",
                "text": "A source could not be read, so nothing is offered.",
            }
    return None


def refuse_for(reason: dict | None) -> None:
    """Raise the refusal that matches a ``why_not`` (nothing for None)."""
    if reason is None:
        return
    code = reason["code"]
    if code == "unreadable":
        raise Refuse("state_unreadable")
    raise Refuse(code)


def stop_units(names, run=aeri_prep.run_capture) -> list:
    """``systemctl --user stop`` for each name in turn. Returns ``[{"name",
    "ok", "detail"}]``: ``detail`` is the refusal text as systemd gave it (paths
    scrubbed, bounded), None on success. Never raises."""
    results = []
    for name in names:
        try:
            done = run(["systemctl", "--user", "stop", name], timeout=STOP_TIMEOUT_S)
            ok = done.returncode == 0
            detail = None
            if not ok:
                detail = (
                    aeri_prep.scrub((done.stderr or done.stdout).strip())[-TAIL_CHARS:]
                    or f"exit {done.returncode}"
                )
        except subprocess.TimeoutExpired:
            ok, detail = False, "timeout"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            ok, detail = False, type(exc).__name__
        results.append({"name": name, "ok": ok, "detail": detail})
    return results


def live_env(environ) -> dict:
    """The environment of the stop: the allowlist, nothing else."""
    env = {"PATH": LIVE_ENV_PATH}
    env.update({k: environ[k] for k in LIVE_ENV_KEYS if environ.get(k)})
    return env


def stop_live(unit, environ, run=aeri_prep.run_capture) -> dict:
    """``systemctl --user stop <unit>`` for the live annotation service's unit
    (``[live].unit`` of the site file; no ``--force``, nothing else is stopped).
    Returns ``{"returncode", "tail"}``; raises ``Refuse("live_stop_failed")`` when
    the unit name is not a live unit's, or the command cannot run or times out.
    The caller reads the unit's state again afterwards, and that decides."""
    if (
        not isinstance(unit, str)
        or LIVE_UNIT.fullmatch(unit) is None
        or LIVE_UNIT_FORBIDDEN in unit
    ):
        raise Refuse("live_stop_failed", reason="unit_invalid")
    argv = ["systemctl", "--user", "stop", unit]
    try:
        done = run(argv, env=live_env(environ), timeout=LIVE_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise Refuse("live_stop_failed", reason="timeout") from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise Refuse("live_stop_failed", reason="not_run") from None
    tail = aeri_prep.scrub((done.stdout + done.stderr).strip())[-TAIL_CHARS:]
    return {"returncode": done.returncode, "tail": tail}


def units_items(units_part: dict):
    """The unit list of ``status.units_part`` or None when it could not be read
    (an unreadable list must never look like an empty one)."""
    if not isinstance(units_part, dict) or units_part.get("state") != "ok":
        return None
    items = units_part.get("items")
    return items if isinstance(items, list) else None


__all__ = [
    "ACTIVE_STATES",
    "any_aeri_unit",
    "leftover_units",
    "live_env",
    "refuse_for",
    "stop_live",
    "stop_units",
    "units_items",
    "why_not",
]
